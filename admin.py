"""
admin.py -- Phase 1b admin pages.

  /admin/sync           status dashboard + "Pull Now" (ap_clerk, controller)
  /admin/sync/warnings  data-quality detail: OPS↔jira mismatches, date issues
  /admin/rules          GL+Class rules + vendor defaults (controller edits,
                        ap_clerk reads), "Re-run rules across all bills"

"Pull Now" runs the sync synchronously (Joe's v1 decision); the module lock in
sync.py prevents overlap with the scheduled job. Rule/vendor-default changes
auto-recompute app_category across all stored bills. Every mutation is audited.
"""

import json

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

import db
import sync
import tags
import warehouse
from auth import role_required

bp = Blueprint("admin", __name__, url_prefix="/admin")


def init_admin(app):
    app.register_blueprint(bp)


def _latest_sync():
    row = db.q1("SELECT after, created_at FROM audit_log "
                "WHERE action='sync_run' ORDER BY id DESC LIMIT 1")
    if not row or not row["after"]:
        return None
    data = json.loads(row["after"])
    data["_logged_at"] = row["created_at"]
    return data


def _recent_syncs(limit=10):
    rows = db.qa("SELECT after, created_at FROM audit_log "
                 "WHERE action='sync_run' ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        if r["after"]:
            d = json.loads(r["after"])
            d["_logged_at"] = r["created_at"]
            out.append(d)
    return out


def _next_run_iso():
    sched = current_app.config.get("SCHEDULER")
    if not sched:
        return None
    job = sched.get_job("bill_sync")
    return job.next_run_time.isoformat(sep=" ", timespec="seconds") if job and job.next_run_time else None


# ----------------------------------------------------------------------
# /admin/sync
# ----------------------------------------------------------------------

@bp.route("/sync")
@role_required("ap_clerk", "controller")
def sync_status():
    fresh = db.q1("SELECT COUNT(*) AS n, "
                  "SUM(CASE WHEN open_balance_cents>0 THEN 1 ELSE 0 END) AS open_n, "
                  "MAX(qb_updated_at) AS max_qb, MAX(last_synced_at) AS max_sync "
                  "FROM bill")
    warn = db.q1("SELECT "
                 "(SELECT COUNT(*) FROM bill WHERE date_parse_warning=1) AS date_warn, "
                 "(SELECT COUNT(*) FROM bill_metadata WHERE app_category='Uncategorized') AS uncat")
    return render_template(
        "admin_sync.html",
        health=warehouse.health_check(),
        last=_latest_sync(),
        recent=_recent_syncs(),
        fresh=fresh,
        warn=warn,
        kpis=sync.compute_kpis(db.get_db()),
        next_run=_next_run_iso(),
        can_pull=True,
    )


@bp.route("/sync/pull", methods=["POST"])
@role_required("ap_clerk", "controller")
def sync_pull():
    result = sync.run_sync(trigger="manual", user_id=current_user.id)
    status = result.get("status")
    if status == "ok":
        flash(f"Sync complete: {result['pulled']} pulled, "
              f"{result['inserted']} new, {result['updated']} updated, "
              f"{result['metadata_created']} new metadata, "
              f"{result['marked_paid']} marked paid, {result['errors']} errors. "
              f"Open AP ${result['open_ap_total_cents']/100:,.2f} "
              f"across {result['open_bill_count']} bills.", "ok")
    elif status == "skipped_locked":
        flash("A sync is already running; try again in a moment.", "error")
    else:
        flash(f"Sync failed: {result.get('error', 'unknown error')}", "error")
    return redirect(url_for("admin.sync_status"))


@bp.route("/sync/warnings")
@role_required("ap_clerk", "controller")
def sync_warnings():
    mismatches = []
    for r in db.qa("SELECT entity_id, after, created_at FROM audit_log "
                   "WHERE action='ops_jira_mismatch' ORDER BY id DESC LIMIT 200"):
        d = json.loads(r["after"]) if r["after"] else {}
        mismatches.append({"qb_bill_id": r["entity_id"], "created_at": r["created_at"],
                           "memo_ops": d.get("memo_ops"), "jira_epic_id": d.get("jira_epic_id"),
                           "vendor": d.get("vendor")})
    date_warns = db.qa("SELECT qb_bill_id, vendor, bill_number, bill_date, due_date "
                       "FROM bill WHERE date_parse_warning=1 ORDER BY vendor")
    return render_template("admin_warnings.html",
                           mismatches=mismatches, date_warns=date_warns)


# ----------------------------------------------------------------------
# /admin/rules
# ----------------------------------------------------------------------

@bp.route("/rules")
@role_required("ap_clerk", "controller")
def rules():
    gl_rules = db.qa("SELECT * FROM gl_rule ORDER BY priority ASC, id ASC")
    vendor_defaults = db.qa("SELECT * FROM vendor_category_default ORDER BY vendor_name")
    # coverage helper: distinct GL accounts seen on stored lines + uncategorized count
    coverage = db.qa("SELECT gl_account_name, COUNT(*) AS line_count "
                     "FROM bill_line WHERE gl_account_name IS NOT NULL "
                     "GROUP BY gl_account_name ORDER BY line_count DESC LIMIT 100")
    uncat = db.q1("SELECT COUNT(*) AS n FROM bill_metadata WHERE app_category='Uncategorized'")
    return render_template("admin_rules.html",
                           gl_rules=gl_rules, vendor_defaults=vendor_defaults,
                           coverage=coverage, uncat=uncat,
                           can_edit=current_user.has_role("controller"))


@bp.route("/rules/add", methods=["POST"])
@role_required("controller")
def rules_add():
    now = sync._now_iso()
    mt = request.form.get("match_type", "").strip()
    mv = request.form.get("match_value", "").strip()
    cat = request.form.get("target_category", "").strip()
    try:
        prio = int(request.form.get("priority", "100"))
    except ValueError:
        prio = 100
    if mt not in ("gl_account_number", "gl_account_name_like", "class_name", "gl_and_class") \
            or not mv or not cat:
        flash("Rule needs a valid match type, match value, and category.", "error")
        return redirect(url_for("admin.rules"))
    cur = db.execute(
        "INSERT INTO gl_rule (match_type, match_value, target_category, priority, "
        "active, created_by, created_at, updated_at) VALUES (?,?,?,?,1,?,?,?)",
        (mt, mv, cat, prio, current_user.id, now, now))
    sync.log_audit(db.get_db(), current_user.id, "gl_rule", cur.lastrowid,
                   "rule_create", None,
                   {"match_type": mt, "match_value": mv, "target_category": cat,
                    "priority": prio})
    db.get_db().commit()
    summary = sync.recompute_all()
    flash(f"Rule added. Recomputed {summary['bills']} bills "
          f"({summary['changed']} changed, {summary['uncategorized']} uncategorized).", "ok")
    return redirect(url_for("admin.rules"))


@bp.route("/rules/<int:rule_id>/toggle", methods=["POST"])
@role_required("controller")
def rules_toggle(rule_id):
    row = db.q1("SELECT active FROM gl_rule WHERE id=?", (rule_id,))
    if not row:
        abort(404)
    new_active = 0 if row["active"] else 1
    db.execute("UPDATE gl_rule SET active=?, updated_at=? WHERE id=?",
               (new_active, sync._now_iso(), rule_id))
    sync.log_audit(db.get_db(), current_user.id, "gl_rule", rule_id, "rule_toggle",
                   {"active": row["active"]}, {"active": new_active})
    db.get_db().commit()
    sync.recompute_all()
    flash("Rule toggled and bills recomputed.", "ok")
    return redirect(url_for("admin.rules"))


@bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
@role_required("controller")
def rules_delete(rule_id):
    row = db.q1("SELECT * FROM gl_rule WHERE id=?", (rule_id,))
    if not row:
        abort(404)
    db.execute("DELETE FROM gl_rule WHERE id=?", (rule_id,))
    sync.log_audit(db.get_db(), current_user.id, "gl_rule", rule_id, "rule_delete",
                   dict(row), None)
    db.get_db().commit()
    sync.recompute_all()
    flash("Rule deleted and bills recomputed.", "ok")
    return redirect(url_for("admin.rules"))


@bp.route("/vendor-defaults/add", methods=["POST"])
@role_required("controller")
def vendor_default_add():
    now = sync._now_iso()
    vid = request.form.get("vendor_id", "").strip()
    vname = request.form.get("vendor_name", "").strip()
    cat = request.form.get("default_category", "").strip()
    if not vid or not cat:
        flash("Vendor default needs a vendor id and a category.", "error")
        return redirect(url_for("admin.rules"))
    db.execute(
        "INSERT INTO vendor_category_default (vendor_id, vendor_name, "
        "default_category, active, created_by, created_at, updated_at) "
        "VALUES (?,?,?,1,?,?,?) "
        "ON CONFLICT(vendor_id) DO UPDATE SET vendor_name=excluded.vendor_name, "
        "default_category=excluded.default_category, active=1, updated_at=excluded.updated_at",
        (vid, vname, cat, current_user.id, now, now))
    sync.log_audit(db.get_db(), current_user.id, "vendor_default", vid,
                   "vendor_default_set", None, {"vendor_name": vname, "default_category": cat})
    db.get_db().commit()
    summary = sync.recompute_all()
    flash(f"Vendor default saved. Recomputed {summary['bills']} bills "
          f"({summary['changed']} changed).", "ok")
    return redirect(url_for("admin.rules"))


@bp.route("/vendor-defaults/<vendor_id>/delete", methods=["POST"])
@role_required("controller")
def vendor_default_delete(vendor_id):
    db.execute("DELETE FROM vendor_category_default WHERE vendor_id=?", (vendor_id,))
    sync.log_audit(db.get_db(), current_user.id, "vendor_default", vendor_id,
                   "vendor_default_delete", None, None)
    db.get_db().commit()
    sync.recompute_all()
    flash("Vendor default removed and bills recomputed.", "ok")
    return redirect(url_for("admin.rules"))


# ----------------------------------------------------------------------
# /admin/status_pills -- add a follow-up status pill to the lookup (Phase 3.5)
# ----------------------------------------------------------------------

@bp.route("/status_pills", methods=["POST"])
@role_required("ap_clerk", "controller")
def status_pills_add():
    """Add a new status-pill value to the lookup. Triggered from the inline
    "+ Add new…" affordance on the bill-detail pill dropdown, so we redirect
    back to wherever it was submitted from. Trim + non-empty + case-insensitive
    uniqueness enforced."""
    value = (request.form.get("value") or "").strip()
    back = request.referrer or url_for("bills.list_bills")
    if not value:
        flash("Status pill can't be empty.", "error")
        return redirect(back)
    conn = db.get_db()
    if tags.pill_exists_ci(conn, value):
        flash(f"“{value}” is already a status pill.", "error")
        return redirect(back)
    conn.execute(
        "INSERT INTO status_pill_lookup (value, created_by, created_at, is_seed) "
        "VALUES (?,?,?,0)", (value, current_user.id, sync._now_iso()))
    sync.log_audit(conn, current_user.id, "status_pill_lookup", value,
                   "status_pill_added", None, {"value": value})
    conn.commit()
    flash(f"Added status pill “{value}”.", "ok")
    return redirect(back)


@bp.route("/rules/rerun", methods=["POST"])
@role_required("controller")
def rules_rerun():
    summary = sync.recompute_all()
    flash(f"Re-ran rules across {summary['bills']} bills: "
          f"{summary['changed']} changed, {summary['uncategorized']} uncategorized.", "ok")
    return redirect(url_for("admin.rules"))
