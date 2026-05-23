"""
payruns.py -- Phase 4 pay-run builder.

A pay run is a new entity layered on top of bill approval (it does NOT touch the
Phase 3 bill approval state machine). An ap_clerk/controller builds a Draft from
Controller_Reviewed open bills, sets payment method + amount per line, then the
run walks a lifecycle to the CFO and is locked:

    Draft -> Submitted_to_Controller -> Controller_Approved
          -> Submitted_to_CFO -> CFO_Approved -> Locked   (Exported = Phase 5)

Controller and CFO can reject individual lines (with a note); a rejected or
excluded line is not paid and frees its bill back into the next run's pool
("push to next week"). Detail groups payable lines like the sample workbook:
a Contractor section (via the Phase 3.5 GL flag, bills.CONTRACTOR_GL_ACCOUNT_
LEAF_NAMES) sub-grouped by payment method, then an Other section by method, with
subtotals and a grand total. Per-app_category sections (Buys, Refunds, ...) need
app_category, which is empty until GL rules exist -> Phase 5 export.

Reuses (no changes to prior-phase logic): bills.CONTRACTOR_GL_ACCOUNT_LEAF_NAMES
+ bills.CEO_EXCLUDED + bills.METHODS, followup._contractor_bill_ids /
_section_past_sla, tags.* counts, dates.business_days_ago. PRG (302 + flash)
throughout; role gating via auth.role_required and per-state checks.
"""

from datetime import date

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

import bills
import db
import followup
import sync
import tags
from auth import role_required

bp = Blueprint("payruns", __name__)

# Lifecycle. Exported is reserved for Phase 5 (the Excel generation).
RUN_STATES = ("Draft", "Submitted_to_Controller", "Controller_Approved",
              "Submitted_to_CFO", "CFO_Approved", "Locked", "Exported")
# action -> (from_state, to_state, allowed_roles)
TRANSITIONS = {
    "submit_controller": ("Draft", "Submitted_to_Controller", ("ap_clerk", "controller")),
    "approve_controller": ("Submitted_to_Controller", "Controller_Approved", ("controller",)),
    "submit_cfo": ("Controller_Approved", "Submitted_to_CFO", ("controller",)),
    "approve_cfo": ("Submitted_to_CFO", "CFO_Approved", ("cfo",)),
    "lock": ("CFO_Approved", "Locked", ("controller", "cfo")),
}
# Who can review (approve/reject) individual lines, and in which run state.
LINE_REVIEW = {
    "Submitted_to_Controller": ("controller",),
    "Submitted_to_CFO": ("cfo",),
}


def init_payruns(app):
    app.register_blueprint(bp)


# ----------------------------------------------------------------------
# Queries / helpers
# ----------------------------------------------------------------------

def _run(conn, run_id):
    return conn.execute("SELECT * FROM pay_run WHERE id=?", (run_id,)).fetchone()


def claimed_bill_ids(conn, exclude_run_id=None):
    """Bills already spoken for: an included, non-Rejected line on any run.
    Excluding/​rejecting a line releases the bill (that's "push to next week")."""
    sql = ("SELECT DISTINCT qb_bill_id FROM pay_run_line "
           "WHERE included=1 AND line_state<>'Rejected'")
    params = ()
    if exclude_run_id is not None:
        sql += " AND pay_run_id<>?"
        params = (exclude_run_id,)
    return {r["qb_bill_id"] for r in conn.execute(sql, params)}


def candidate_bills(conn, run_id):
    """Controller_Reviewed + open bills eligible to add to this run: not a
    pay-run-excluded classification, not already claimed by another run, not
    already a line on this run. Each row carries soft-warn flags (contractor,
    past_sla, open_items, tags) -- nothing is hidden for those, they just warn."""
    today = date.today()
    contractor_ids = followup._contractor_bill_ids(conn)
    past_sla = {r["qb_bill_id"] for r in followup._section_past_sla(conn, today, contractor_ids)}
    claimed = claimed_bill_ids(conn)            # claimed by ANY run (incl. this one's lines)
    excl = ",".join("?" * len(bills.CEO_EXCLUDED))
    rows = conn.execute(
        f"""SELECT b.qb_bill_id, b.vendor, b.bill_number, b.bill_date, b.due_date,
                   b.amount_cents, b.open_balance_cents,
                   m.classification, m.proposed_payment_method
            FROM bill b JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id
            WHERE m.approval_state='Controller_Reviewed' AND b.open_balance_cents>0
              AND (m.classification IS NULL OR m.classification NOT IN ({excl}))
            ORDER BY b.vendor, b.bill_number""",
        tuple(bills.CEO_EXCLUDED)).fetchall()
    ids = [r["qb_bill_id"] for r in rows]
    open_items = tags.open_item_counts_for_bills(conn, ids)
    tag_counts = tags.tag_counts_for_bills(conn, ids)
    out = []
    for r in rows:
        if r["qb_bill_id"] in claimed:          # already on a run -> not selectable
            continue
        d = dict(r)
        d["is_contractor"] = r["qb_bill_id"] in contractor_ids
        d["past_sla"] = r["qb_bill_id"] in past_sla
        d["open_item_count"] = open_items.get(r["qb_bill_id"], 0)
        d["tag_count"] = tag_counts.get(r["qb_bill_id"], 0)
        out.append(d)
    return out


def lines_for_run(conn, run_id):
    return conn.execute(
        "SELECT pl.*, b.vendor, b.bill_number, b.bill_date, b.due_date, "
        "       b.amount_cents, b.open_balance_cents, u.name AS reviewed_by_name "
        "FROM pay_run_line pl "
        "JOIN bill b ON b.qb_bill_id=pl.qb_bill_id "
        "LEFT JOIN users u ON u.id=pl.reviewed_by_user_id "
        "WHERE pl.pay_run_id=? ORDER BY b.vendor, b.bill_number", (run_id,)).fetchall()


def _payable(line):
    return line["included"] and line["line_state"] != "Rejected"


def grouped_lines(conn, run_id):
    """Group payable lines: Contractor (by method) then Other (by method), with
    subtotals + grand total, mirroring the sample workbook. Non-payable lines
    (excluded / rejected) are returned separately as 'deferred'."""
    contractor_ids = followup._contractor_bill_ids(conn)
    lines = [dict(r) for r in lines_for_run(conn, run_id)]
    for ln in lines:
        ln["is_contractor"] = ln["qb_bill_id"] in contractor_ids

    methods = list(bills.METHODS) + [None]      # None = method not set yet

    def section(is_contractor):
        groups = []
        for method in methods:
            ml = [ln for ln in lines if _payable(ln) and ln["is_contractor"] == is_contractor
                  and (ln["payment_method"] or None) == method]
            if ml:
                subtotal = sum(ln["amount_to_pay_cents"] or 0 for ln in ml)
                groups.append({"method": method or "(no method)", "lines": ml,
                               "subtotal": subtotal})
        total = sum(g["subtotal"] for g in groups)
        return groups, total

    contractor_groups, contractor_total = section(True)
    other_groups, other_total = section(False)
    deferred = [ln for ln in lines if not _payable(ln)]
    return {
        "contractor_groups": contractor_groups, "contractor_total": contractor_total,
        "other_groups": other_groups, "other_total": other_total,
        "grand_total": contractor_total + other_total,
        "deferred": deferred,
        "payable_count": sum(1 for ln in lines if _payable(ln)),
        "line_count": len(lines),
    }


def _parse_amount_cents(s, open_balance_cents):
    s = (s or "").strip().replace(",", "").lstrip("$")
    if not s:
        return None, "Amount is required."
    try:
        cents = int(round(float(s) * 100))
    except ValueError:
        return None, "Amount must be a number."
    if cents <= 0:
        return None, "Amount must be positive."
    if cents > open_balance_cents:
        return None, "Amount can't exceed the bill's open balance."
    return cents, None


# ----------------------------------------------------------------------
# Routes -- list / create
# ----------------------------------------------------------------------

@bp.route("/pay-runs")
@login_required
def list_runs():
    conn = db.get_db()
    runs = conn.execute(
        "SELECT pr.*, u.name AS created_by_name, "
        "  (SELECT COUNT(*) FROM pay_run_line pl WHERE pl.pay_run_id=pr.id "
        "     AND pl.included=1 AND pl.line_state<>'Rejected') AS line_count, "
        "  (SELECT COALESCE(SUM(pl.amount_to_pay_cents),0) FROM pay_run_line pl "
        "     WHERE pl.pay_run_id=pr.id AND pl.included=1 AND pl.line_state<>'Rejected') AS total_cents "
        "FROM pay_run pr LEFT JOIN users u ON u.id=pr.created_by "
        "ORDER BY pr.id DESC").fetchall()
    return render_template("payruns_list.html", runs=runs,
                           can_create=current_user.has_role("ap_clerk", "controller"))


@bp.route("/pay-runs", methods=["POST"])
@role_required("ap_clerk", "controller")
def create_run():
    name = (request.form.get("name") or "").strip()
    week_ending = bills._valid_date(request.form.get("week_ending")) \
        if request.form.get("week_ending") else None
    now = sync._now_iso()
    if not name:
        name = "Pay Run " + (week_ending or date.today().isoformat())
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO pay_run (name, week_ending, created_by, status, created_at, updated_at) "
        "VALUES (?,?,?, 'Draft', ?, ?)", (name, week_ending, current_user.id, now, now))
    run_id = cur.lastrowid
    sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_created",
                   None, {"name": name, "week_ending": week_ending})
    conn.commit()
    flash(f"Created “{name}”.", "ok")
    return redirect(url_for("payruns.detail", run_id=run_id))


@bp.route("/pay-runs/<int:run_id>")
@login_required
def detail(run_id):
    conn = db.get_db()
    run = _run(conn, run_id)
    if not run:
        abort(404)
    grouped = grouped_lines(conn, run_id)
    is_draft = run["status"] == "Draft"
    candidates = candidate_bills(conn, run_id) if is_draft else []
    # who can review lines right now?
    review_roles = LINE_REVIEW.get(run["status"], ())
    can_review = current_user.has_role(*review_roles) if review_roles else False
    # available forward transition for this user
    next_action = None
    for action, (frm, to, roles) in TRANSITIONS.items():
        if run["status"] == frm and current_user.has_role(*roles):
            next_action = {"action": action, "to": to}
            break
    return render_template(
        "payrun_detail.html", run=run, grouped=grouped, candidates=candidates,
        is_draft=is_draft, can_edit=current_user.has_role("ap_clerk", "controller"),
        can_review=can_review, next_action=next_action, methods=bills.METHODS)


# ----------------------------------------------------------------------
# Routes -- line add / edit
# ----------------------------------------------------------------------

@bp.route("/pay-runs/<int:run_id>/lines", methods=["POST"])
@role_required("ap_clerk", "controller")
def add_lines(run_id):
    conn = db.get_db()
    run = _run(conn, run_id)
    if not run:
        abort(404)
    if run["status"] != "Draft":
        flash("Lines can only be added while the run is a Draft.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    wanted = set(request.form.getlist("bill_ids"))
    if not wanted:
        flash("Select at least one bill.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    eligible = {c["qb_bill_id"]: c for c in candidate_bills(conn, run_id)}
    now = sync._now_iso()
    added = 0
    for bid in wanted:
        c = eligible.get(bid)
        if not c:
            continue                            # not eligible (claimed/excluded/etc.)
        conn.execute(
            "INSERT INTO pay_run_line (pay_run_id, qb_bill_id, payment_method, "
            "amount_to_pay_cents, included, line_state) VALUES (?,?,?,?,1,'Pending')",
            (run_id, bid, c["proposed_payment_method"], c["open_balance_cents"]))
        added += 1
    if added:
        conn.execute("UPDATE pay_run SET updated_at=? WHERE id=?", (now, run_id))
        sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_lines_added",
                       None, {"count": added})
        conn.commit()
    flash(f"Added {added} bill(s).", "ok")
    return redirect(url_for("payruns.detail", run_id=run_id))


@bp.route("/pay-runs/<int:run_id>/lines/<int:line_id>", methods=["POST"])
@role_required("ap_clerk", "controller")
def edit_line(run_id, line_id):
    conn = db.get_db()
    run = _run(conn, run_id)
    if not run:
        abort(404)
    line = conn.execute("SELECT pl.*, b.open_balance_cents FROM pay_run_line pl "
                        "JOIN bill b ON b.qb_bill_id=pl.qb_bill_id "
                        "WHERE pl.id=? AND pl.pay_run_id=?", (line_id, run_id)).fetchone()
    if not line:
        abort(404)
    if run["status"] != "Draft":
        flash("Lines can only be edited while the run is a Draft.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    method = (request.form.get("payment_method") or "").strip() or None
    if method and method not in bills.METHODS:
        flash("Invalid payment method.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    included = 1 if request.form.get("included") else 0
    amount_cents, err = _parse_amount_cents(request.form.get("amount_to_pay"),
                                            line["open_balance_cents"])
    if err:
        flash(err, "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    conn.execute("UPDATE pay_run_line SET payment_method=?, amount_to_pay_cents=?, "
                 "included=? WHERE id=?", (method, amount_cents, included, line_id))
    conn.execute("UPDATE pay_run SET updated_at=? WHERE id=?", (sync._now_iso(), run_id))
    sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_line_updated",
                   {"line_id": line_id},
                   {"payment_method": method, "amount_to_pay_cents": amount_cents,
                    "included": included})
    conn.commit()
    flash("Line updated.", "ok")
    return redirect(url_for("payruns.detail", run_id=run_id))


@bp.route("/pay-runs/<int:run_id>/lines/<int:line_id>/review", methods=["POST"])
@login_required
def review_line(run_id, line_id):
    conn = db.get_db()
    run = _run(conn, run_id)
    if not run:
        abort(404)
    roles = LINE_REVIEW.get(run["status"])
    if not roles:
        flash("Lines can't be reviewed in this run's current state.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    if not current_user.has_role(*roles):
        abort(403)
    line = conn.execute("SELECT * FROM pay_run_line WHERE id=? AND pay_run_id=?",
                        (line_id, run_id)).fetchone()
    if not line:
        abort(404)
    action = request.form.get("action")
    if action not in ("approve", "reject"):
        flash("Unknown review action.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    note = (request.form.get("note") or "").strip() or None
    if action == "reject" and not note:
        flash("A note is required to reject a line.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    new_state = "Approved" if action == "approve" else "Rejected"
    conn.execute("UPDATE pay_run_line SET line_state=?, cfo_note=?, "
                 "reviewed_by_user_id=?, reviewed_at=? WHERE id=?",
                 (new_state, note, current_user.id, sync._now_iso(), line_id))
    conn.execute("UPDATE pay_run SET updated_at=? WHERE id=?", (sync._now_iso(), run_id))
    sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_line_reviewed",
                   {"line_id": line_id, "qb_bill_id": line["qb_bill_id"]},
                   {"line_state": new_state, "note": note})
    conn.commit()
    flash(f"Line {new_state.lower()}.", "ok")
    return redirect(url_for("payruns.detail", run_id=run_id))


# ----------------------------------------------------------------------
# Routes -- lifecycle transitions
# ----------------------------------------------------------------------

@bp.route("/pay-runs/<int:run_id>/advance", methods=["POST"])
@login_required
def advance(run_id):
    conn = db.get_db()
    run = _run(conn, run_id)
    if not run:
        abort(404)
    action = request.form.get("action")
    spec = TRANSITIONS.get(action)
    if not spec:
        flash("Unknown action.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    frm, to, roles = spec
    if not current_user.has_role(*roles):
        abort(403)
    if run["status"] != frm:
        flash(f"Can't {action.replace('_', ' ')} from {run['status']}.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))
    # Guard: don't submit an empty run (no payable lines).
    if action == "submit_controller":
        g = grouped_lines(conn, run_id)
        if g["payable_count"] == 0:
            flash("Add at least one included bill before submitting.", "error")
            return redirect(url_for("payruns.detail", run_id=run_id))
    conn.execute("UPDATE pay_run SET status=?, updated_at=? WHERE id=?",
                 (to, sync._now_iso(), run_id))
    sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_advanced",
                   {"status": frm}, {"status": to, "action": action})
    conn.commit()
    flash(f"Run is now {to.replace('_', ' ')}.", "ok")
    return redirect(url_for("payruns.detail", run_id=run_id))


def cfo_queue(conn):
    """Pay runs awaiting the CFO -- powers the /inbox/cfo queue (Phase 4)."""
    return conn.execute(
        "SELECT pr.*, u.name AS created_by_name, "
        "  (SELECT COALESCE(SUM(amount_to_pay_cents),0) FROM pay_run_line pl "
        "     WHERE pl.pay_run_id=pr.id AND pl.included=1 AND pl.line_state<>'Rejected') AS total_cents "
        "FROM pay_run pr LEFT JOIN users u ON u.id=pr.created_by "
        "WHERE pr.status='Submitted_to_CFO' ORDER BY pr.updated_at").fetchall()
