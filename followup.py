"""
followup.py -- Phase 3.5 /follow-up workspace.

Surfaces stuck bills across four overlapping sections (a bill can appear in
several -- that's the point; nothing is deduped across sections):

  1. Past SLA      -- open bills past their payment SLA. Contractor bills
                      (any line hits a Service & Training COGS account, see
                      bills.CONTRACTOR_GL_ACCOUNT_LEAF_NAMES) are due at
                      bill_date + 14 calendar days; everyone else at due_date
                      (or bill_date + 30d if due_date is NULL -- defensive, as
                      the warehouse currently populates every due_date).
  2. Stale activity -- open bills whose last activity (note / bill audit /
                      metadata floor) is >= 5 business days ago. Yellow at 5 BD,
                      red at 10 BD.
  3. Open to-dos    -- bills with at least one incomplete todo.
  4. In process     -- bills with at least one active tag.

"Not Paid" in the spec maps to bill.is_paid = 0 (there is no 'Paid'
approval_state; paid bills carry open_balance_cents = 0 / is_paid = 1).

Bridges to Phase 4: the pay-run builder will reuse the SLA + status data here
to exclude stuck bills from a run.
"""

from datetime import date

from flask import Blueprint, render_template
from flask_login import login_required

import bills
import dates
import db
import tags

bp = Blueprint("followup", __name__)


def init_followup(app):
    app.register_blueprint(bp)


# Columns every section row carries, for the shared row template.
_BASE = ("b.qb_bill_id, b.vendor, b.bill_number, b.amount_cents, "
         "b.open_balance_cents, m.approval_state, m.status_pill")


def _contractor_bill_ids(conn):
    """Open bills with >=1 line hitting a contractor GL account. Matched on the
    leaf account name (handles both numbered and name-only line formats)."""
    out = set()
    for r in conn.execute(
        "SELECT DISTINCT bl.qb_bill_id AS bid, bl.gl_account_name AS name "
        "FROM bill_line bl JOIN bill b ON b.qb_bill_id=bl.qb_bill_id "
        "WHERE b.open_balance_cents > 0"):
        if bills.is_contractor_account_name(r["name"]):
            out.add(r["bid"])
    return out


def _section_past_sla(conn, today, contractor_ids):
    rows = conn.execute(
        f"SELECT {_BASE}, b.bill_date, b.due_date, m.app_category "
        "FROM bill b JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id "
        "WHERE b.open_balance_cents > 0 AND b.is_paid = 0").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if r["qb_bill_id"] in contractor_ids:
            bd = dates._as_date(r["bill_date"])
            past = bd is not None and (today - bd).days > 14
            d["sla_reason"] = "contractor: >14d since bill date"
        else:
            dd = dates._as_date(r["due_date"])
            if dd is not None:
                past = dd < today
                d["sla_reason"] = "past due date"
            else:                                  # defensive fallback
                bd = dates._as_date(r["bill_date"])
                past = bd is not None and (today - bd).days > 30
                d["sla_reason"] = "no due date: >30d since bill date"
        if past:
            out.append(d)
    return out


def _section_stale(conn, today):
    rows = conn.execute(
        f"SELECT {_BASE} FROM bill b "
        "JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id "
        "WHERE b.is_paid = 0").fetchall()
    actmap = tags.last_activity_for_bills(conn, [r["qb_bill_id"] for r in rows])
    out = []
    for r in rows:
        la = actmap.get(r["qb_bill_id"])
        age = dates.business_days_ago(la, today) if la else None
        if age is not None and age >= 5:
            d = dict(r)
            d["stale_bd"] = age
            out.append(d)
    out.sort(key=lambda d: -d["stale_bd"])
    return out


def _section_open_todos(conn):
    rows = conn.execute(
        f"SELECT {_BASE}, COUNT(t.id) AS open_todos FROM bill b "
        "JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id "
        "JOIN todo t ON t.qb_bill_id=b.qb_bill_id AND t.completed_at IS NULL "
        "GROUP BY b.qb_bill_id ORDER BY open_todos DESC").fetchall()
    return [dict(r) for r in rows]


def _section_in_process(conn):
    rows = conn.execute(
        f"SELECT DISTINCT {_BASE} FROM bill b "
        "JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id "
        "JOIN bill_tag tg ON tg.qb_bill_id=b.qb_bill_id AND tg.cleared_at IS NULL "
        "ORDER BY b.vendor").fetchall()
    return [dict(r) for r in rows]


@bp.route("/follow-up")
@login_required
def followup():
    conn = db.get_db()
    today = date.today()
    contractor_ids = _contractor_bill_ids(conn)
    sections = {
        "past_sla": _section_past_sla(conn, today, contractor_ids),
        "stale": _section_stale(conn, today),
        "open_todos": _section_open_todos(conn),
        "in_process": _section_in_process(conn),
    }
    # Decorate every row (across sections) with active-tag chips + last-activity
    # age, in two batched queries over the union of displayed bills.
    all_ids = list({b["qb_bill_id"] for sec in sections.values() for b in sec})
    tagmap = tags.active_tags_for_bills(conn, all_ids)
    actmap = tags.last_activity_for_bills(conn, all_ids)
    for sec in sections.values():
        for b in sec:
            b["tags"] = tagmap.get(b["qb_bill_id"], [])
            la = actmap.get(b["qb_bill_id"])
            b["last_activity_bd"] = dates.business_days_ago(la, today) if la else None
    return render_template("followup.html", **sections)
