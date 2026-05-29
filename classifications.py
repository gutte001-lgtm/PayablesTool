"""
classifications.py -- Phase 4.6 CFO classification review surface at
/classifications. Read-only review (no editing here -- classification edits stay
on bill detail). Controller + CFO only.

Four parts:
  1. "Changes since your last visit" -- classification_audit rows newer than this
     user's last review marker. The marker is an audit_log row
     (action='classifications_reviewed', user_id=<this user>), so NO new table is
     needed; "Mark reviewed" appends a fresh marker and the list resets.
  2. "Open not-real-AP" -- open bills flagged not_real_ap, with the reason/note
     the CFO can defend to the CEO.
  3. "Pending trigger flips" -- ordinary_ap + not_due open bills whose
     expected_payment_date is already in the past (a stale-trigger warning).
  4. Searchable flat list -- ad-hoc lookup during CEO back-and-forth.
"""
from datetime import date

from flask import (Blueprint, flash, redirect, render_template, request, url_for)
from flask_login import current_user

import db
import dates as dates_mod
import sync
from auth import role_required

bp = Blueprint("classifications", __name__)

_REVIEW_ACTION = "classifications_reviewed"
_OPEN_AP = "b.open_balance_cents > 0 AND b.is_paid = 0"


def init_classifications(app):
    app.register_blueprint(bp)


def _last_review_at(conn, user_id):
    """This user's last 'mark reviewed' timestamp, or '' (matches all history)."""
    r = conn.execute(
        "SELECT created_at FROM audit_log WHERE action=? AND user_id=? "
        "ORDER BY id DESC LIMIT 1", (_REVIEW_ACTION, user_id)).fetchone()
    return (r["created_at"] if r and r["created_at"] else "")


def _changes_since(conn, since_iso, limit=200):
    return conn.execute(
        "SELECT c.id, c.bill_id, c.field, c.from_value, c.to_value, c.changed_at, "
        "       b.vendor, u.name AS who "
        "FROM classification_audit c "
        "JOIN bill b ON b.qb_bill_id = c.bill_id "
        "LEFT JOIN users u ON u.id = c.changed_by "
        "WHERE c.changed_at > ? ORDER BY c.id DESC LIMIT ?",
        (since_iso, limit)).fetchall()


def _open_not_real(conn):
    return conn.execute(
        "SELECT b.qb_bill_id, b.vendor, b.bill_number, b.open_balance_cents, "
        "       m.classification_reason, m.classification_note, m.classified_at, "
        "       u.name AS classified_by_name "
        "FROM bill b JOIN bill_metadata m ON m.qb_bill_id = b.qb_bill_id "
        "LEFT JOIN users u ON u.id = m.classified_by "
        f"WHERE {_OPEN_AP} AND m.obligation_type = 'not_real_ap' "
        "ORDER BY b.open_balance_cents DESC").fetchall()


def _pending_flips(conn, today_iso):
    """ordinary_ap + not_due open bills whose expected_payment_date is in the
    past -- the trigger should have been flipped by now (stale-trigger warning)."""
    return conn.execute(
        "SELECT b.qb_bill_id, b.vendor, b.bill_number, b.open_balance_cents, "
        "       m.expected_payment_date "
        "FROM bill b JOIN bill_metadata m ON m.qb_bill_id = b.qb_bill_id "
        f"WHERE {_OPEN_AP} AND m.obligation_type = 'ordinary_ap' "
        "  AND m.due_state = 'not_due' "
        "  AND m.expected_payment_date IS NOT NULL "
        "  AND m.expected_payment_date < ? "
        "ORDER BY m.expected_payment_date ASC", (today_iso,)).fetchall()


def _flat_list(conn, q, limit=300):
    sql = (
        "SELECT b.qb_bill_id, b.vendor, b.bill_number, b.open_balance_cents, "
        "       m.obligation_type, m.due_state, m.classification_reason, "
        "       m.classification_note, m.classified_at, u.name AS classified_by_name "
        "FROM bill b JOIN bill_metadata m ON m.qb_bill_id = b.qb_bill_id "
        "LEFT JOIN users u ON u.id = m.classified_by "
        f"WHERE {_OPEN_AP}")
    params = []
    if q:
        like = f"%{q}%"
        sql += (" AND (b.vendor LIKE ? OR b.bill_number LIKE ? "
                "OR m.classification_reason LIKE ?)")
        params += [like, like, like]
    sql += " ORDER BY b.vendor, b.bill_number LIMIT ?"
    params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


@bp.route("/classifications")
@role_required("controller", "cfo")
def review():
    conn = db.get_db()
    today = date.today()
    since = _last_review_at(conn, current_user.id)
    q = (request.args.get("q") or "").strip()

    flips = []
    for r in _pending_flips(conn, today.isoformat()):
        d = dict(r)
        d["days_past"] = dates_mod.business_days_ago(r["expected_payment_date"], today)
        flips.append(d)

    return render_template(
        "classifications.html",
        changes=_changes_since(conn, since),
        since=since or None,
        not_real=_open_not_real(conn),
        flips=flips,
        flat=_flat_list(conn, q),
        q=q,
    )


@bp.route("/classifications/mark-reviewed", methods=["POST"])
@role_required("controller", "cfo")
def mark_reviewed():
    """Append this user's review marker (an audit_log row). The 'Changes since
    your last visit' list resets relative to this timestamp."""
    conn = db.get_db()
    sync.log_audit(conn, current_user.id, "classifications", None,
                   _REVIEW_ACTION, None, {"reviewed_by": current_user.name})
    conn.commit()
    flash("Marked reviewed — 'changes since last visit' reset.", "ok")
    return redirect(url_for("classifications.review"))
