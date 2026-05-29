"""
triage.py -- Phase 4.5 pre-go-live triage workspace.

Walks the untriaged bills one at a time, biggest open balance first, each
pre-populated with a DETERMINISTIC heuristic suggestion (no LLM / no API call).
The controller accepts the suggestion or overrides it in one submit; the bill is
stamped classified_by/at so it leaves the queue, and every change is recorded in
classification_audit. Controller + CFO only -- they own obligation_type.

Heuristics (Joe's spec):
  1. line reduces a known liability account  -> debt_service, reason=debt_service
  2. New/Pre-owned Device Purchases category AND a payment/credit already applied
     (the deposit pattern)                   -> not_real_ap,  reason=deposit
  3. Notes Payable category                  -> debt_service, reason=debt_service
  4. otherwise                               -> ordinary_ap,  not_due (no reason)

"Payment already applied in QB" is inferred locally as open_balance < amount
(a partial payment left a residual) OR a linked vendor credit -- PayablesTool
does not mirror BillPayment rows, so this is the closest deterministic signal.
"""

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user

import bills
import db
import sync
import tags
from auth import role_required

bp = Blueprint("triage", __name__)

# Untriaged = still at the shipped defaults and never touched by a human classify
# nor auto-detected (debt-service auto-detect sets a non-default obligation_type).
_UNTRIAGED = ("m.obligation_type='ordinary_ap' AND m.due_state='not_due' "
              "AND m.classification_reason IS NULL AND m.classified_by IS NULL "
              "AND m.classified_at IS NULL")

# Triage is scoped to OPEN AP only -- the canonical open-bill definition shared
# with summary.py / bills.py. Paid/closed bills (open_balance=0) are out of
# pay-runs and out of the AP report, so triaging them is busywork; they can
# still be classified from the bill-detail page if ever needed. This is also why
# the queue count is the open-untriaged set, not the whole table.
_OPEN_AP = "b.open_balance_cents > 0 AND b.is_paid = 0"


def init_triage(app):
    app.register_blueprint(bp)


def suggest(bill_row, lines, meta):
    """Deterministic heuristic -> (obligation_type, due_state, reason, rationale)."""
    if sync.bill_reduces_liability([dict(l) for l in lines]):
        return ("debt_service", "not_due", "debt_service",
                "a line reduces a known liability account (debt service)")
    cat = (meta["app_category"] or "")
    if cat in ("New Device Purchases", "Pre-owned Device Purchases"):
        amt = bill_row["amount_cents"] or 0
        opn = bill_row["open_balance_cents"] or 0
        if amt > opn or meta["has_credit_applied"]:
            return ("not_real_ap", "not_due", "deposit",
                    "device-purchase bill with a payment/credit already applied "
                    "(deposit pattern)")
    if "notes payable" in cat.lower():
        return ("debt_service", "not_due", "debt_service",
                "Notes Payable category (likely a loan)")
    return ("ordinary_ap", "not_due", None, "default — ordinary AP, not due")


def _remaining(conn):
    """Count of DISTINCT open, untriaged bills (one per bill -- the join to
    bill_metadata is 1:1 on qb_bill_id, so no fan-out; never joins bill_line)."""
    return conn.execute(
        "SELECT COUNT(*) FROM bill b JOIN bill_metadata m "
        f"ON m.qb_bill_id=b.qb_bill_id WHERE {_UNTRIAGED} AND {_OPEN_AP}").fetchone()[0]


def _next_bill(conn):
    """The next untriaged open bill: biggest open balance first (most
    impactful), then a stable id tiebreak."""
    return conn.execute(
        "SELECT b.* FROM bill b JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id "
        f"WHERE {_UNTRIAGED} AND {_OPEN_AP} "
        "ORDER BY b.open_balance_cents DESC, b.qb_bill_id LIMIT 1").fetchone()


@bp.route("/triage")
@role_required("controller", "cfo")
def triage():
    conn = db.get_db()
    remaining = _remaining(conn)
    bill = _next_bill(conn)
    if not bill:
        return render_template("triage.html", bill=None, remaining=0)
    meta = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?",
                        (bill["qb_bill_id"],)).fetchone()
    lines = conn.execute("SELECT * FROM bill_line WHERE qb_bill_id=? ORDER BY line_number",
                         (bill["qb_bill_id"],)).fetchall()
    s_obl, s_due, s_reason, s_why = suggest(bill, lines, meta)
    return render_template(
        "triage.html", bill=bill, meta=meta, lines=lines, remaining=remaining,
        sug_obligation=s_obl, sug_due=s_due, sug_reason=s_reason, sug_why=s_why,
        obligation_types=bills.OBLIGATION_TYPES, due_states=bills.DUE_STATES,
        reasons=tags.classification_reasons(conn),
        jira_base=bills._jira_base(),
    )


@bp.route("/triage/<bill_id>", methods=["POST"])
@role_required("controller", "cfo")
def apply(bill_id):
    """Apply a triage decision and advance. Always stamps classified_by/at so the
    bill leaves the queue -- even when the default is kept (that records a human
    reviewed it). Same validation as bills.classify (not_real_ap forces not_due;
    a non-default classification requires a reason)."""
    conn = db.get_db()
    old = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?",
                       (bill_id,)).fetchone()
    if not old:
        abort(404)
    f = request.form

    new_obl = (f.get("obligation_type") or "").strip()
    if new_obl not in bills.OBLIGATION_TYPES:
        flash("Invalid obligation type.", "error")
        return redirect(url_for("triage.triage"))
    new_due = (f.get("due_state") or "").strip()
    if new_due not in bills.DUE_STATES:
        flash("Invalid due state.", "error")
        return redirect(url_for("triage.triage"))
    coerced = False
    if new_obl == "not_real_ap" and new_due == "due":
        new_due = "not_due"
        coerced = True
    new_reason = (f.get("classification_reason") or "").strip() or None
    new_note = (f.get("classification_note") or "").strip() or None
    if new_reason and not tags.reason_exists(conn, new_reason):
        flash("Unknown classification reason.", "error")
        return redirect(url_for("triage.triage"))
    differs_from_default = (new_obl != "ordinary_ap") or (new_due == "due")
    if differs_from_default and not new_reason:
        flash("A reason is required for a non-default classification.", "error")
        return redirect(url_for("triage.triage"))

    proposed = {"obligation_type": new_obl, "due_state": new_due,
                "classification_reason": new_reason, "classification_note": new_note}
    changed = {k: v for k, v in proposed.items()
               if (old[k] if old[k] != "" else None) != v}
    now = sync._now_iso()
    if changed:
        sets = ", ".join(f"{k}=?" for k in changed)
        conn.execute(
            f"UPDATE bill_metadata SET {sets}, classified_by=?, classified_at=?, "
            "updated_at=? WHERE qb_bill_id=?",
            (*[changed[k] for k in changed], current_user.id, now, now, bill_id))
        for field, to_val in changed.items():
            sync.log_classification_change(conn, bill_id, field, old[field], to_val,
                                           current_user.id, now)
    else:
        # Default kept: stamp the review so the bill leaves the queue, and record
        # the decision so the triage pass is auditable.
        conn.execute(
            "UPDATE bill_metadata SET classified_by=?, classified_at=?, updated_at=? "
            "WHERE qb_bill_id=?", (current_user.id, now, now, bill_id))
        sync.log_classification_change(conn, bill_id, "triaged", None,
                                       "kept ordinary_ap / not_due",
                                       current_user.id, now)
    conn.commit()
    flash("Triaged." + (" not_real_ap forced not due." if coerced else "")
          + f" {_remaining(conn)} left.", "ok")
    return redirect(url_for("triage.triage"))
