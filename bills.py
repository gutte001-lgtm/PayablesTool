"""
bills.py -- Phase 2 bill list + detail UI (where Marilyn lives).

  /bills                 filterable/searchable/sortable/paginated list + KPI bar
  /inbox                 role-scoped queue (ap_clerk: New; controller: AP_Reviewed)
  /bills/<id>            detail: read-only QB facts + GL lines/breakdown,
                         editable metadata (one Save), notes, todos, audit panel
  POST .../metadata      save metadata (ap_clerk, controller); audited
  POST .../review        forward transition New->AP_Reviewed->Controller_Reviewed
  POST .../notes         add append-only note (any logged-in)
  POST .../todos[/<t>/complete]  todo add / complete (ap_clerk, controller)
  POST /bills/bulk-classify      set classification on many bills at once

Only the FORWARD transitions are here; rejections/CFO actions are Phase 3.
See PHASE_2_DESIGN_NOTES.md for the workflow these screens must match.
"""

import json
from datetime import date

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

import db
import sync
from auth import role_required

bp = Blueprint("bills", __name__)

PER_PAGE = 50
CLASSIFICATIONS = ("Real", "Refund-Visibility", "Prepayment-Deposit", "Other")
CEO_EXCLUDED = ("Refund-Visibility", "Prepayment-Deposit")
CHANNELS = ("Pur Board", "MS List", "NSPO", "Email", "Other")
METHODS = ("Check", "Wire", "Credit Card", "ACH")
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

def _render_list(args, title=None, base_endpoint="bills.list_bills", locked=None):
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
                   m.approval_state, m.ops_number, m.rush_flag, m.ok_for_ceo
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

    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    # filter-only params (no sort/dir/page) for building sort + pagination links
    fparams = {k: v for k in ("status", "classification", "app_category",
               "approval_state", "ok_for_ceo", "rush", "future", "uncat", "due", "q")
               for v in [state.get(k)] if v}
    return render_template(
        "bills_list.html",
        bills=bills, kpis=kpis, total=total, page=page, pages=pages,
        sort=sort, dir=direction.lower(), state=state, title=title,
        classifications=CLASSIFICATIONS, can_edit=current_user.has_role("ap_clerk", "controller"),
        jira_base=_jira_base(), locked=locked or {},
        endpoint=base_endpoint, fparams=fparams,
    )


@bp.route("/bills")
@login_required
def list_bills():
    return _render_list(request.args)


@bp.route("/inbox")
@login_required
def inbox():
    # role-scoped queue: ap_clerk -> New, controller -> AP_Reviewed
    if current_user.has_role("controller"):
        state = "AP_Reviewed"
    elif current_user.has_role("ap_clerk"):
        state = "New"
    else:
        flash("Inbox is for AP clerks and the controller.", "error")
        return redirect(url_for("bills.list_bills"))
    args = request.args.to_dict()
    args["approval_state"] = state
    args["status"] = args.get("status", "open")
    return _render_list(args, title=f"Inbox — {state}", base_endpoint="bills.inbox",
                        locked={"approval_state": state})


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
    breakdown = []
    if meta and meta["app_category_breakdown"]:
        try:
            breakdown = json.loads(meta["app_category_breakdown"])
        except ValueError:
            breakdown = []
    return render_template(
        "bill_detail.html", bill=bill, meta=meta, lines=lines, notes=notes,
        todos=todos, audit=audit, breakdown=breakdown,
        classifications=CLASSIFICATIONS, channels=CHANNELS, methods=METHODS,
        can_edit=current_user.has_role("ap_clerk", "controller"),
        jira_base=_jira_base(), next_state=_next_state(meta),
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


@bp.route("/bills/<bill_id>/review", methods=["POST"])
@role_required("ap_clerk", "controller")
def review(bill_id):
    conn = db.get_db()
    meta = conn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?", (bill_id,)).fetchone()
    if not meta:
        abort(404)
    nxt = _next_state(meta)
    if not nxt:
        flash("No forward transition available to you for this bill.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    conn.execute("UPDATE bill_metadata SET approval_state=?, updated_at=? WHERE qb_bill_id=?",
                 (nxt, sync._now_iso(), bill_id))
    sync.log_audit(conn, current_user.id, "bill_metadata", bill_id, "state_transition",
                   {"approval_state": meta["approval_state"]}, {"approval_state": nxt})
    conn.commit()
    flash(f"Marked {nxt.replace('_', ' ')}.", "ok")
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
    conn.execute("INSERT INTO note (qb_bill_id, user_id, body, created_at) VALUES (?,?,?,?)",
                 (bill_id, current_user.id, body, sync._now_iso()))
    conn.commit()
    flash("Note added.", "ok")
    return redirect(url_for("bills.detail", bill_id=bill_id))


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

def _jira_base():
    from os import environ
    from dotenv import dotenv_values
    from pathlib import Path
    return (dotenv_values(Path(__file__).resolve().parent / ".env").get("JIRA_BASE_URL")
            or environ.get("JIRA_BASE_URL") or "").rstrip("/")
