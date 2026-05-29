"""
bills.py -- Phase 2 bill list + detail UI (where Marilyn lives).

  /bills                 filterable/searchable/sortable/paginated list + KPI bar
  /inbox                 role dispatcher -> controller queue or ap_clerk New queue
  /inbox/controller      AP_Reviewed queue (controller)
  /inbox/cfo             pay runs Submitted_to_CFO awaiting CFO approval
  /bills/<id>            detail: read-only QB facts + GL lines/breakdown,
                         editable metadata (one Save), notes, todos, audit panel
  POST .../metadata      save metadata (ap_clerk, controller); audited
  POST .../approve       forward transition New->AP_Reviewed->Controller_Reviewed
                         (New->AP_Reviewed gated on required metadata fields)
  POST .../reject        controller bounces AP_Reviewed -> New with required
                         reason (stored as a Note + audit)
  POST .../notes         add append-only note (any logged-in)
  POST .../todos[/<t>/complete]  todo add / complete (ap_clerk, controller)
  POST /bills/bulk-classify      set classification on many bills at once

Phase 3 = the BILL approval state machine (forward + reject-to-New). The
pay-run state machine + CFO actions are Phase 4.
See PHASE_2_DESIGN_NOTES.md for the workflow these screens must match.
"""

import json
from datetime import date

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

import db
import dates
import sync
import tags
from auth import role_required

bp = Blueprint("bills", __name__)

PER_PAGE = 50
CLASSIFICATIONS = ("Real", "Refund-Visibility", "Prepayment-Deposit", "Other")
CEO_EXCLUDED = ("Refund-Visibility", "Prepayment-Deposit")
CHANNELS = ("Pur Board", "MS List", "NSPO", "Email", "Other")
METHODS = ("Check", "Wire", "Credit Card", "ACH")
# Phase 4.5: the 2-D dueness model. obligation_type is owned by controller+cfo;
# ap_clerk may flip due_state (the trigger-met flip) but not obligation_type.
OBLIGATION_TYPES = ("ordinary_ap", "debt_service", "not_real_ap")
DUE_STATES = ("due", "not_due")
# audit_log/classification actions surfaced in the bill-detail change history
CLASSIFICATION_AUDIT_FIELDS = ("obligation_type", "due_state",
                               "classification_reason", "classification_note",
                               "expected_payment_date", "invoice_due_date")
# Fields an ap_clerk must fill before New -> AP_Reviewed (key, human label).
REQUIRED_FOR_AP = (("classification", "classification"), ("approver_name", "approver"),
                   ("approval_channel", "approval channel"), ("approval_date", "approval date"))
REJECT_NOTE_PREFIX = "⮌ Rejected: "

# Phase 3.5 follow-up: a bill is a "contractor bill" if ANY of its bill_line
# rows hits one of these GL accounts (vendors paid out of Service & Training
# COGS). Contractor bills get a tighter 14-day SLA. These four leaf names are
# the "53xxx Service & Training COGS" family, discovered from the warehouse's
# reporting.dim_account view (2026-05-22). EDIT POINT if the chart of accounts
# changes: re-run the dim_account lookup (account name/path LIKE training/service
# + cogs) and update this list. We match on the LEAF account name, not the
# parsed number, because reporting.fact_bill_line returns the same account in
# two formats -- a numbered path AND a name-only form whose gl_account_number is
# NULL -- so number-only matching would silently drop the name-only lines.
CONTRACTOR_GL_ACCOUNT_LEAF_NAMES = frozenset({
    "Service and Repair COGS",            # 53100
    "Training COGS",                      # 53200
    "Service COGS - MET Reimbursements",  # 53300
    "Training COGS - MET Reimbursements", # 53400
})


def _account_leaf(name):
    """Leaf of a GL account name: last ':'-segment, with any leading numeric
    code token stripped ('53200 ... :Training COGS' -> 'Training COGS';
    'Training COGS' -> 'Training COGS')."""
    if not name:
        return ""
    seg = str(name).split(":")[-1].strip()
    head, _, rest = seg.partition(" ")
    if head.isdigit() and rest:
        seg = rest.strip()
    return seg


def is_contractor_account_name(name):
    return _account_leaf(name) in CONTRACTOR_GL_ACCOUNT_LEAF_NAMES
SORTS = {  # ?sort= -> SQL column
    "vendor": "b.vendor", "bill_number": "b.bill_number", "bill_date": "b.bill_date",
    "due_date": "b.due_date", "amount": "b.amount_cents",
    "open_balance": "b.open_balance_cents", "app_category": "m.app_category",
    "approval_state": "m.approval_state",
}


def init_bills(app):
    app.register_blueprint(bp)


# ----------------------------------------------------------------------
# Filters
# ----------------------------------------------------------------------

def _build_filters(args, for_kpi=False):
    """Return (where_sql, params, state). When for_kpi, the status filter is
    ignored and 'open' is forced (KPIs are always over open AP)."""
    today = date.today().isoformat()
    conds, params, state = [], [], {}

    status = args.get("status", "open")
    state["status"] = status
    if for_kpi or status == "open":
        conds.append("b.open_balance_cents > 0")
    elif status == "paid":
        conds.append("b.is_paid = 1")
    # status == "all": no clause

    cl = args.get("classification", "")
    state["classification"] = cl
    if cl == "unset":
        conds.append("m.classification IS NULL")
    elif cl in CLASSIFICATIONS:
        conds.append("m.classification = ?"); params.append(cl)

    cat = args.get("app_category", "")
    state["app_category"] = cat
    if cat:
        conds.append("m.app_category = ?"); params.append(cat)

    obl = args.get("obligation_type", "")
    state["obligation_type"] = obl
    if obl in OBLIGATION_TYPES:
        conds.append("m.obligation_type = ?"); params.append(obl)

    ds = args.get("due_state", "")
    state["due_state"] = ds
    if ds in DUE_STATES:
        conds.append("m.due_state = ?"); params.append(ds)

    vendor = args.get("vendor", "")
    state["vendor"] = vendor
    if vendor:
        conds.append("b.vendor = ?"); params.append(vendor)

    ast = args.get("approval_state", "")
    state["approval_state"] = ast
    if ast:
        conds.append("m.approval_state = ?"); params.append(ast)

    ceo = args.get("ok_for_ceo", "")
    state["ok_for_ceo"] = ceo
    if ceo in ("0", "1"):
        conds.append("m.ok_for_ceo = ?"); params.append(int(ceo))

    if args.get("rush") == "1":
        state["rush"] = "1"; conds.append("m.rush_flag = 1")
    if args.get("future") == "1":
        state["future"] = "1"
        conds.append("b.bill_date IS NOT NULL AND b.bill_date > ?"); params.append(today)
    if args.get("uncat") == "1":
        state["uncat"] = "1"; conds.append("m.app_category = 'Uncategorized'")

    due = args.get("due", "")
    state["due"] = due
    if due == "overdue":
        conds.append("b.open_balance_cents>0 AND b.due_date IS NOT NULL AND b.due_date < ?")
        params.append(today)
    elif due == "current":
        conds.append("b.open_balance_cents>0 AND (b.bill_date IS NULL OR b.bill_date<=?) "
                     "AND (b.due_date IS NULL OR b.due_date>=?)")
        params.extend([today, today])
    elif due == "notdue":
        conds.append("b.bill_date IS NOT NULL AND b.bill_date > ?"); params.append(today)

    q = args.get("q", "").strip()
    state["q"] = q
    if q:
        like = f"%{q}%"
        conds.append("(b.vendor LIKE ? OR b.bill_number LIKE ? OR "
                     "m.ops_number LIKE ? OR b.qb_memo LIKE ?)")
        params.extend([like, like, like, like])

    where = " AND ".join(conds) if conds else "1=1"
    return where, params, state


def _due_status(row, today):
    if row["is_paid"]:
        return "Paid"
    if row["bill_date"] and row["bill_date"] > today:
        return "Not Due"
    if row["due_date"] and row["due_date"] < today:
        return "Overdue"
    return "Current"


def _split_count(breakdown_json):
    if not breakdown_json:
        return 0
    try:
        return len(json.loads(breakdown_json))
    except (ValueError, TypeError):
        return 0


# ----------------------------------------------------------------------
# List + inbox
# ----------------------------------------------------------------------

def _render_list(args, title=None, base_endpoint="bills.list_bills", locked=None,
                 tagged_bills=None):
    today = date.today().isoformat()
    where, params, state = _build_filters(args)

    sort = args.get("sort", "due_date")
    sort_col = SORTS.get(sort, "b.due_date")
    direction = "DESC" if args.get("dir", "asc").lower() == "desc" else "ASC"
    try:
        page = max(1, int(args.get("page", "1")))
    except ValueError:
        page = 1

    conn = db.get_db()
    total = conn.execute(
        f"SELECT COUNT(*) FROM bill b LEFT JOIN bill_metadata m "
        f"ON m.qb_bill_id=b.qb_bill_id WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT b.qb_bill_id, b.vendor, b.bill_number, b.bill_date, b.due_date,
                   b.amount_cents, b.open_balance_cents, b.is_paid,
                   m.app_category, m.app_category_breakdown, m.classification,
                   m.approval_state, m.ops_number, m.rush_flag, m.ok_for_ceo,
                   m.status_pill, m.obligation_type, m.due_state
            FROM bill b LEFT JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id
            WHERE {where}
            ORDER BY {sort_col} {direction}, b.qb_bill_id
            LIMIT ? OFFSET ?""",
        (*params, PER_PAGE, (page - 1) * PER_PAGE)).fetchall()

    # KPI bar over the active (non-status) filter, always open
    kwhere, kparams, _ = _build_filters(args, for_kpi=True)
    kpis = sync.compute_kpis(conn, kwhere, kparams)

    bills = []
    for r in rows:
        d = dict(r)
        d["due_status"] = _due_status(r, today)
        d["split"] = _split_count(r["app_category_breakdown"]) > 1
        bills.append(d)

    # Phase 3.5: per-row active-tag count + business-days since last activity.
    page_ids = [d["qb_bill_id"] for d in bills]
    tag_counts = tags.tag_counts_for_bills(conn, page_ids)
    open_item_counts = tags.open_item_counts_for_bills(conn, page_ids)
    activity = tags.last_activity_for_bills(conn, page_ids)
    today_d = date.today()
    for d in bills:
        d["tag_count"] = tag_counts.get(d["qb_bill_id"], 0)
        d["open_item_count"] = open_item_counts.get(d["qb_bill_id"], 0)
        la = activity.get(d["qb_bill_id"])
        d["last_activity_bd"] = dates.business_days_ago(la, today_d) if la else None

    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    # filter-only params (no sort/dir/page) for building sort + pagination links
    fparams = {k: v for k in ("status", "classification", "app_category", "vendor",
               "approval_state", "ok_for_ceo", "rush", "future", "uncat", "due", "q",
               "obligation_type", "due_state")
               for v in [state.get(k)] if v}
    return render_template(
        "bills_list.html",
        bills=bills, kpis=kpis, total=total, page=page, pages=pages,
        sort=sort, dir=direction.lower(), state=state, title=title,
        classifications=CLASSIFICATIONS, can_edit=current_user.has_role("ap_clerk", "controller"),
        jira_base=_jira_base(), locked=locked or {},
        endpoint=base_endpoint, fparams=fparams,
        tagged_bills=tagged_bills or [],
        obligation_types=OBLIGATION_TYPES, due_states=DUE_STATES,
    )


@bp.route("/bills")
@login_required
def list_bills():
    return _render_list(request.args)


@bp.route("/inbox")
@login_required
def inbox():
    """Role dispatcher: controller -> their AP_Reviewed queue; ap_clerk -> their
    New queue. Keeps the Phase 2 nav link working."""
    if current_user.has_role("controller"):
        return redirect(url_for("bills.inbox_controller", **request.args.to_dict(flat=True)))
    if current_user.has_role("ap_clerk"):
        args = request.args.to_dict()
        args["approval_state"] = "New"
        args["status"] = args.get("status", "open")
        return _render_list(args, title="Inbox — New (your queue)",
                            base_endpoint="bills.inbox", locked={"approval_state": "New"},
                            tagged_bills=tags.tagged_bills_for_user(db.get_db(), current_user.id))
    flash("Inbox is for AP clerks and the controller.", "error")
    return redirect(url_for("bills.list_bills"))


@bp.route("/inbox/controller")
@role_required("controller")
def inbox_controller():
    args = request.args.to_dict()
    args["approval_state"] = "AP_Reviewed"
    args["status"] = args.get("status", "open")
    return _render_list(args, title="Inbox — Controller (AP Reviewed)",
                        base_endpoint="bills.inbox_controller",
                        locked={"approval_state": "AP_Reviewed"},
                        tagged_bills=tags.tagged_bills_for_user(db.get_db(), current_user.id))


@bp.route("/inbox/cfo")
@login_required
def inbox_cfo():
    # Phase 4: the CFO inbox shows pay runs Submitted_to_CFO awaiting approval.
    # Local import avoids a bills<->payruns import cycle (payruns imports bills).
    import payruns
    runs = payruns.cfo_queue(db.get_db())
    return render_template("inbox_cfo.html", runs=runs)


# ----------------------------------------------------------------------
# Detail
# ----------------------------------------------------------------------

@bp.route("/bills/<bill_id>")
@login_required
def detail(bill_id):
    conn = db.get_db()
    bill = conn.execute("SELECT * FROM bill WHERE qb_bill_id=?", (bill_id,)).fetchone()
    if not bill:
        abort(404)
    meta = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?", (bill_id,)).fetchone()
    lines = conn.execute("SELECT * FROM bill_line WHERE qb_bill_id=? ORDER BY line_number",
                         (bill_id,)).fetchall()
    notes = conn.execute(
        "SELECT n.*, u.name AS author FROM note n LEFT JOIN users u ON u.id=n.user_id "
        "WHERE n.qb_bill_id=? ORDER BY n.id DESC", (bill_id,)).fetchall()
    todos = conn.execute(
        "SELECT t.*, cu.name AS done_by FROM todo t LEFT JOIN users cu ON cu.id=t.completed_by "
        "WHERE t.qb_bill_id=? ORDER BY (t.completed_at IS NOT NULL), t.id DESC",
        (bill_id,)).fetchall()
    audit = conn.execute(
        "SELECT a.*, u.name AS who FROM audit_log a LEFT JOIN users u ON u.id=a.user_id "
        "WHERE a.entity_id=? AND a.entity_type IN ('bill','bill_metadata') "
        "ORDER BY a.id DESC LIMIT 20", (bill_id,)).fetchall()
    # Phase 4.5: classification change history (who / when / field / from -> to).
    class_history = conn.execute(
        "SELECT c.*, u.name AS who FROM classification_audit c "
        "LEFT JOIN users u ON u.id=c.changed_by "
        "WHERE c.bill_id=? ORDER BY c.id DESC LIMIT 20", (bill_id,)).fetchall()
    breakdown = []
    if meta and meta["app_category_breakdown"]:
        try:
            breakdown = json.loads(meta["app_category_breakdown"])
        except ValueError:
            breakdown = []
    cur_state = meta["approval_state"] if meta else "New"
    missing_required = _missing_required(meta) if cur_state == "New" else []
    can_reject = current_user.has_role("controller") and cur_state == "AP_Reviewed"
    # Phase 3.5: relative age per note (yellow >=5 BD, red >=10 BD), status pill
    # dropdown values, active tag chips, and the tag-someone dropdown.
    today_d = date.today()
    notes = [{**dict(n), "age_bd": dates.business_days_ago(n["created_at"], today_d)}
             for n in notes]
    return render_template(
        "bill_detail.html", bill=bill, meta=meta, lines=lines, notes=notes,
        todos=todos, audit=audit, breakdown=breakdown,
        classifications=CLASSIFICATIONS, channels=CHANNELS, methods=METHODS,
        can_edit=current_user.has_role("ap_clerk", "controller"),
        jira_base=_jira_base(), next_state=_next_state(meta),
        missing_required=missing_required, can_reject=can_reject,
        reject_prefix=REJECT_NOTE_PREFIX,
        pills=tags.pill_values(conn),
        active_tags=tags.active_tags_for_bill(conn, bill_id),
        taggable_users=tags.active_users_excluding(conn, current_user.id),
        open_items=tags.open_items_for_bill(conn, bill_id),
        # Phase 4.5 classification panel
        obligation_types=OBLIGATION_TYPES, due_states=DUE_STATES,
        reasons=tags.classification_reasons(conn),
        class_history=class_history,
        can_classify=current_user.has_role("ap_clerk", "controller", "cfo"),
        can_edit_obligation=current_user.has_role("controller", "cfo"),
    )


def _next_state(meta):
    """The forward transition available to the current user, or None."""
    cur = meta["approval_state"] if meta else "New"
    if cur == "New" and current_user.has_role("ap_clerk", "controller"):
        return "AP_Reviewed"
    if cur == "AP_Reviewed" and current_user.has_role("controller"):
        return "Controller_Reviewed"
    return None


# ----------------------------------------------------------------------
# Mutations
# ----------------------------------------------------------------------

def _valid_date(s):
    """'' -> None (cleared); 'YYYY-MM-DD' -> itself; anything else -> raises."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        raise ValueError(f"'{s}' is not a valid YYYY-MM-DD date")


@bp.route("/bills/<bill_id>/metadata", methods=["POST"])
@role_required("ap_clerk", "controller")
def save_metadata(bill_id):
    conn = db.get_db()
    old = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?", (bill_id,)).fetchone()
    if not old:
        abort(404)
    f = request.form
    try:
        new = {
            "classification": (f.get("classification") or None),
            "approver_name": (f.get("approver_name") or None),
            "approval_channel": (f.get("approval_channel") or None),
            "approval_date": _valid_date(f.get("approval_date")),
            "service_performed_date": _valid_date(f.get("service_performed_date")),
            "receipt_delivery_date": _valid_date(f.get("receipt_delivery_date")),
            "proposed_payment_method": (f.get("proposed_payment_method") or None),
            "proposed_pay_date": _valid_date(f.get("proposed_pay_date")),
            "rush_flag": 1 if f.get("rush_flag") else 0,
            "partial_payment_flag": 1 if f.get("partial_payment_flag") else 0,
            "app_category_manual": (f.get("app_category_manual") or "").strip() or None,
        }
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    if new["classification"] and new["classification"] not in CLASSIFICATIONS:
        flash("Invalid classification.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    # Refund-Visibility / Prepayment-Deposit force ok_for_ceo off; else use checkbox
    if new["classification"] in CEO_EXCLUDED:
        new["ok_for_ceo"] = 0
    else:
        new["ok_for_ceo"] = 1 if f.get("ok_for_ceo") else 0

    manual_changed = (old["app_category_manual"] or None) != new["app_category_manual"]
    conn.execute(
        """UPDATE bill_metadata SET classification=?, approver_name=?,
           approval_channel=?, approval_date=?, service_performed_date=?,
           receipt_delivery_date=?, proposed_payment_method=?, proposed_pay_date=?,
           rush_flag=?, partial_payment_flag=?, app_category_manual=?, ok_for_ceo=?,
           updated_at=? WHERE qb_bill_id=?""",
        (new["classification"], new["approver_name"], new["approval_channel"],
         new["approval_date"], new["service_performed_date"], new["receipt_delivery_date"],
         new["proposed_payment_method"], new["proposed_pay_date"], new["rush_flag"],
         new["partial_payment_flag"], new["app_category_manual"], new["ok_for_ceo"],
         sync._now_iso(), bill_id))

    before = {k: old[k] for k in new}
    changed_before = {k: v for k, v in before.items() if v != new[k]}
    changed_after = {k: new[k] for k in new if before[k] != new[k]}
    if changed_after:
        sync.log_audit(conn, current_user.id, "bill_metadata", bill_id,
                       "metadata_update", changed_before, changed_after)
    if manual_changed:
        sync.recompute_for_bill(conn, bill_id)   # override wins / reverts
    conn.commit()
    flash("Saved." + (" Category recomputed." if manual_changed else ""), "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/classify", methods=["POST"])
@role_required("ap_clerk", "controller", "cfo")
def classify(bill_id):
    """Phase 4.5 classification save: obligation_type x due_state +
    expected_payment_date + reason/note. Separate from save_metadata so the
    role gate (controller+cfo own obligation_type; ap_clerk may flip due_state)
    and the reason-required validation stay isolated, and the audit trail is
    specific. invoice_due_date is QB-locked: never read from the form. Every
    changed field writes a classification_audit row (changed_by=current user)."""
    conn = db.get_db()
    old = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?",
                       (bill_id,)).fetchone()
    if not old:
        abort(404)
    f = request.form
    is_owner = current_user.has_role("controller", "cfo")

    # --- obligation_type (controller+cfo only) ---
    posted_obl = (f.get("obligation_type") or "").strip()
    if posted_obl and posted_obl not in OBLIGATION_TYPES:
        flash("Invalid obligation type.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    new_obl = posted_obl or old["obligation_type"]
    if new_obl != old["obligation_type"] and not is_owner:
        # ap_clerk cannot change obligation_type (server-side gate, not just UI)
        flash("Only the controller or CFO can change the obligation type.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    # --- due_state (ap_clerk + controller + cfo) ---
    posted_due = (f.get("due_state") or "").strip()
    if posted_due and posted_due not in DUE_STATES:
        flash("Invalid due state.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    new_due = posted_due or old["due_state"]

    # Rule 1: not_real_ap is the impossible-cell guard -> force not_due.
    coerced = False
    if new_obl == "not_real_ap" and new_due == "due":
        new_due = "not_due"
        coerced = True

    # --- expected_payment_date (editable; reject non-dates) ---
    try:
        new_epd = _valid_date(f.get("expected_payment_date"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    # --- reason + note ---
    new_reason = (f.get("classification_reason") or "").strip() or None
    new_note = (f.get("classification_note") or "").strip() or None
    if new_reason and not tags.reason_exists(conn, new_reason):
        flash("Unknown classification reason — add it to the list first.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    # Rule 2: a non-default classification (obligation_type != ordinary_ap, or a
    # manual flip to due) requires a reason from the lookup.
    differs_from_default = (new_obl != "ordinary_ap") or (new_due == "due")
    if differs_from_default and not new_reason:
        flash("A classification reason is required when the classification "
              "differs from the default (ordinary AP, not due).", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    # Diff the five editable fields; invoice_due_date is intentionally excluded.
    proposed = {
        "obligation_type": new_obl,
        "due_state": new_due,
        "classification_reason": new_reason,
        "classification_note": new_note,
        "expected_payment_date": new_epd,
    }
    changed = {k: v for k, v in proposed.items()
               if (old[k] if old[k] != "" else None) != v}
    if not changed:
        flash("No classification changes." + (" (not_real_ap forced not due.)"
              if coerced else ""), "ok")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    now = sync._now_iso()
    sets = ", ".join(f"{k}=?" for k in changed)
    params = [changed[k] for k in changed]
    conn.execute(
        f"UPDATE bill_metadata SET {sets}, classified_by=?, classified_at=?, "
        "updated_at=? WHERE qb_bill_id=?",
        (*params, current_user.id, now, now, bill_id))
    for field, to_val in changed.items():
        sync.log_classification_change(conn, bill_id, field, old[field], to_val,
                                       current_user.id, now)
    conn.commit()
    msg = "Classification saved."
    if coerced:
        msg += " not_real_ap can't be due — due state forced to not due."
    flash(msg, "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


def _missing_required(meta):
    """Required metadata fields still empty before New -> AP_Reviewed."""
    if not meta:
        return [label for _k, label in REQUIRED_FOR_AP]
    return [label for key, label in REQUIRED_FOR_AP if not meta[key]]


@bp.route("/bills/<bill_id>/approve", methods=["POST"])
@role_required("ap_clerk", "controller")
def approve(bill_id):
    conn = db.get_db()
    meta = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?", (bill_id,)).fetchone()
    if not meta:
        abort(404)
    nxt = _next_state(meta)
    if not nxt:
        flash("No forward transition available to you for this bill.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    if meta["approval_state"] == "New":
        missing = _missing_required(meta)
        if missing:
            flash("Fill these before AP review: " + ", ".join(missing), "error")
            return redirect(url_for("bills.detail", bill_id=bill_id))
    action = "approve_ap_reviewed" if nxt == "AP_Reviewed" else "approve_controller_reviewed"
    conn.execute("UPDATE bill_metadata SET approval_state=?, updated_at=? WHERE qb_bill_id=?",
                 (nxt, sync._now_iso(), bill_id))
    sync.log_audit(conn, current_user.id, "bill_metadata", bill_id, action,
                   {"approval_state": meta["approval_state"]}, {"approval_state": nxt})
    conn.commit()
    flash(f"Marked {nxt.replace('_', ' ')}.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/reject", methods=["POST"])
@role_required("controller")
def reject(bill_id):
    conn = db.get_db()
    meta = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?", (bill_id,)).fetchone()
    if not meta:
        abort(404)
    if meta["approval_state"] != "AP_Reviewed":
        flash("Only AP-Reviewed bills can be rejected back to New.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("A rejection reason is required.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    now = sync._now_iso()
    conn.execute("UPDATE bill_metadata SET approval_state='New', updated_at=? WHERE qb_bill_id=?",
                 (now, bill_id))
    conn.execute("INSERT INTO note (qb_bill_id, user_id, body, created_at) VALUES (?,?,?,?)",
                 (bill_id, current_user.id, REJECT_NOTE_PREFIX + reason, now))
    sync.log_audit(conn, current_user.id, "bill_metadata", bill_id, "reject_to_new",
                   {"approval_state": "AP_Reviewed"},
                   {"approval_state": "New", "reason": reason})
    conn.commit()
    flash("Rejected back to New with a note.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/notes", methods=["POST"])
@login_required
def add_note(bill_id):
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Note can't be empty.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM bill WHERE qb_bill_id=?", (bill_id,)).fetchone():
        abort(404)
    cur = conn.execute(
        "INSERT INTO note (qb_bill_id, user_id, body, created_at) VALUES (?,?,?,?)",
        (bill_id, current_user.id, body, sync._now_iso()))
    mentioned = _tag_mentions(conn, bill_id, body, cur.lastrowid)
    conn.commit()
    msg = "Note added."
    if mentioned:
        msg += " Tagged " + ", ".join(mentioned) + "."
    flash(msg, "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


def _tag_mentions(conn, bill_id, body, note_id):
    """Phase 3.5: scan a note body for @username tokens and create an active
    bill_tag for each matching active user. Skips the note author (self-mentions
    are no-ops) and users who already have an active tag on this bill (no
    duplicate). Unknown @handles are ignored. Returns the list of tagged names."""
    tagged = []
    for uname in tags.parse_mentions(body):
        u = tags.active_user_by_username(conn, uname)
        if not u or u["id"] == current_user.id or tags.has_active_tag(conn, bill_id, u["id"]):
            continue
        tags.insert_tag(conn, bill_id, u["id"], current_user.id, sync._now_iso(),
                        note="via @mention in note #%d" % note_id)
        sync.log_audit(conn, current_user.id, "bill", bill_id,
                       "bill_tagged_via_mention", None,
                       {"tagged_user_id": u["id"], "note_id": note_id})
        tagged.append(u["name"])
    return tagged


@bp.route("/bills/<bill_id>/todos", methods=["POST"])
@role_required("ap_clerk", "controller")
def add_todo(bill_id):
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("To-do can't be empty.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM bill WHERE qb_bill_id=?", (bill_id,)).fetchone():
        abort(404)
    conn.execute("INSERT INTO todo (qb_bill_id, body, created_by, created_at) VALUES (?,?,?,?)",
                 (bill_id, body, current_user.id, sync._now_iso()))
    conn.commit()
    flash("To-do added.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/todos/<int:todo_id>/complete", methods=["POST"])
@role_required("ap_clerk", "controller")
def complete_todo(bill_id, todo_id):
    conn = db.get_db()
    conn.execute("UPDATE todo SET completed_at=?, completed_by=? WHERE id=? AND qb_bill_id=?",
                 (sync._now_iso(), current_user.id, todo_id, bill_id))
    conn.commit()
    flash("To-do completed.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/bulk-classify", methods=["POST"])
@role_required("ap_clerk", "controller")
def bulk_classify():
    classification = request.form.get("classification", "")
    ids = request.form.getlist("bill_ids")
    if classification not in CLASSIFICATIONS or not ids:
        flash("Pick a classification and at least one bill.", "error")
        return redirect(request.referrer or url_for("bills.list_bills"))
    conn = db.get_db()
    now = sync._now_iso()
    ceo = 0 if classification in CEO_EXCLUDED else None
    n = 0
    for bid in ids:
        old = conn.execute("SELECT classification, ok_for_ceo FROM bill_metadata "
                           "WHERE qb_bill_id=?", (bid,)).fetchone()
        if not old:
            continue
        if ceo is not None:
            conn.execute("UPDATE bill_metadata SET classification=?, ok_for_ceo=?, updated_at=? "
                         "WHERE qb_bill_id=?", (classification, ceo, now, bid))
            after = {"classification": classification, "ok_for_ceo": ceo}
        else:
            conn.execute("UPDATE bill_metadata SET classification=?, updated_at=? "
                         "WHERE qb_bill_id=?", (classification, now, bid))
            after = {"classification": classification}
        sync.log_audit(conn, current_user.id, "bill_metadata", bid, "bulk_classify",
                       {"classification": old["classification"]}, after)
        n += 1
    conn.commit()
    flash(f"Classified {n} bill(s) as {classification}.", "ok")
    return redirect(request.referrer or url_for("bills.list_bills"))


# ----------------------------------------------------------------------
# Phase 3.5 -- status pills + tags. Metadata only: these never gate the
# Phase 3 approval state machine. PRG contract (302 + flash) matches Phase 3.
# ----------------------------------------------------------------------

@bp.route("/bills/<bill_id>/status_pill", methods=["POST"])
@role_required("ap_clerk", "controller")
def set_status_pill(bill_id):
    conn = db.get_db()
    meta = conn.execute("SELECT status_pill FROM bill_metadata WHERE qb_bill_id=?",
                        (bill_id,)).fetchone()
    if not meta:
        abort(404)
    value = (request.form.get("value") or "").strip()    # "" = clear
    if value and not tags.pill_exists(conn, value):
        flash("Unknown status pill — add it to the list first.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    new, old = (value or None), meta["status_pill"]
    if new == old:
        flash("Status pill unchanged.", "ok")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    conn.execute("UPDATE bill_metadata SET status_pill=?, updated_at=? WHERE qb_bill_id=?",
                 (new, sync._now_iso(), bill_id))
    sync.log_audit(conn, current_user.id, "bill_metadata", bill_id, "status_pill_set",
                   {"status_pill": old}, {"status_pill": new})
    conn.commit()
    flash("Status pill cleared." if new is None else f"Status set to “{new}”.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/tag", methods=["POST"])
@role_required("ap_clerk", "controller")
def tag_create(bill_id):
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM bill WHERE qb_bill_id=?", (bill_id,)).fetchone():
        abort(404)
    try:
        uid = int((request.form.get("user_id") or "").strip())
    except ValueError:
        flash("Pick someone to tag.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    note = (request.form.get("note") or "").strip() or None
    if uid == current_user.id:
        flash("You can't tag yourself.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    u = tags.active_user(conn, uid)
    if not u:
        flash("Unknown or inactive user.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    if tags.has_active_tag(conn, bill_id, uid):
        flash(f"{u['name']} already has an active tag on this bill.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    tag_id = tags.insert_tag(conn, bill_id, uid, current_user.id, sync._now_iso(), note)
    sync.log_audit(conn, current_user.id, "bill", bill_id, "bill_tagged",
                   None, {"tag_id": tag_id, "tagged_user_id": uid, "note": note})
    conn.commit()
    flash(f"Tagged {u['name']}.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/tag/<int:tag_id>/clear", methods=["POST"])
@login_required
def tag_clear(bill_id, tag_id):
    # Tags clear ONLY on explicit "mark done" -- never on view or action.
    # Allowed: the tagged user themselves OR any controller (stale-tag cleanup).
    conn = db.get_db()
    tag = tags.get_active_tag(conn, tag_id, bill_id)
    if not tag:
        abort(404)
    if not (tag["tagged_user_id"] == current_user.id
            or current_user.has_role("controller")):
        abort(403)
    tags.clear_tag(conn, tag_id, current_user.id, sync._now_iso())
    sync.log_audit(conn, current_user.id, "bill", bill_id, "bill_tag_cleared",
                   {"tag_id": tag_id, "tagged_user_id": tag["tagged_user_id"]}, None)
    conn.commit()
    flash("Tag cleared.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


# ----------------------------------------------------------------------
# Phase 3.6 -- open items. Explicit "this bill needs work" flag + description.
# Metadata only (never gate approval). Required-resolution-note mirrors the
# Phase 3 reject pattern: missing input -> 302 + flash, not 4xx.
# ----------------------------------------------------------------------

@bp.route("/bills/<bill_id>/open_items", methods=["POST"])
@role_required("ap_clerk", "controller")
def create_open_item(bill_id):
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM bill WHERE qb_bill_id=?", (bill_id,)).fetchone():
        abort(404)
    description = (request.form.get("description") or "").strip()
    if not description:
        flash("Description is required.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    item_id = tags.create_open_item(conn, bill_id, description, current_user.id,
                                    sync._now_iso())
    sync.log_audit(conn, current_user.id, "bill", bill_id, "open_item_created",
                   None, {"open_item_id": item_id, "description": description})
    conn.commit()
    flash("Open item added.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/bills/<bill_id>/open_items/<int:item_id>/resolve", methods=["POST"])
@role_required("ap_clerk", "controller")
def resolve_open_item(bill_id, item_id):
    conn = db.get_db()
    item = tags.get_open_item(conn, item_id, bill_id)
    if not item:
        abort(404)
    note = (request.form.get("resolution_note") or "").strip()
    if not note:
        flash("Resolution note is required.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    if item["resolved_at"] is not None:
        flash("That open item is already resolved.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    tags.resolve_open_item(conn, item_id, bill_id, note, current_user.id, sync._now_iso())
    sync.log_audit(conn, current_user.id, "bill", bill_id, "open_item_resolved",
                   {"open_item_id": item_id, "description": item["description"]},
                   {"resolution_note": note})
    conn.commit()
    flash("Open item resolved.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


# ----------------------------------------------------------------------

def _jira_base():
    from os import environ
    from dotenv import dotenv_values
    from pathlib import Path
    return (dotenv_values(Path(__file__).resolve().parent / ".env").get("JIRA_BASE_URL")
            or environ.get("JIRA_BASE_URL") or "").rstrip("/")
