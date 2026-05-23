"""
app.py -- PayablesTool Flask entrypoint (Phase 0 scaffold).

Wires Flask-Login (auth.py), Flask-WTF CSRF, the raw-sqlite3 data layer
(db.py), and a /health smoke-test route that reports app + DB + warehouse
connectivity. APScheduler is initialized but registers no jobs yet -- the
15-minute warehouse sync arrives in Phase 1.
"""

import os
from datetime import date
from pathlib import Path

from dotenv import dotenv_values
from flask import Flask, jsonify, redirect, render_template, url_for
from flask_login import current_user, login_required
from flask_wtf import CSRFProtect

import dates
import db
import tags
from admin import init_admin
from auth import init_auth
from bills import init_bills
from followup import init_followup
from payruns import init_payruns
from warehouse import health_check

# Read .env DIRECTLY from the file (not via load_dotenv + os.environ). This
# is the same pattern warehouse.py uses, and it avoids a real bug: an empty
# or stale shell variable (e.g. SECRET_KEY= left in a PowerShell session)
# would otherwise win over the .env value, because load_dotenv() does not
# override variables already present in the environment.
_ENV = dotenv_values(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)

_secret = _ENV.get("SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set in .env. Copy .env.example to .env and set it "
        "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")."
    )
app.config["SECRET_KEY"] = _secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

csrf = CSRFProtect(app)
db.init_app(app)
init_auth(app)
init_admin(app)
init_bills(app)
init_followup(app)
init_payruns(app)


@app.context_processor
def inject_nav_badges():
    """Nav badges, recomputed every request (no caching): Phase 3.5 active-tag
    count for the current user, Phase 3.6 total open items. Defensive: if a
    table doesn't exist yet (live DB merged but migration not run), degrade to
    no badge rather than 500 every page."""
    if not current_user.is_authenticated:
        return {}
    out = {}
    try:
        out["nav_tag_count"] = tags.tag_count_for_user(db.get_db(), current_user.id)
    except Exception:
        out["nav_tag_count"] = 0
    try:
        out["nav_open_items_count"] = tags.open_item_total(db.get_db())
    except Exception:
        out["nav_open_items_count"] = 0
    if current_user.has_role("cfo"):
        try:
            out["nav_cfo_queue"] = len(__import__("payruns").cfo_queue(db.get_db()))
        except Exception:
            out["nav_cfo_queue"] = 0
    return out


@app.route("/")
@login_required
def index():
    conn = db.get_db()
    today = date.today()
    try:
        items = [dict(i) for i in tags.all_open_items(conn)]
        ids = list({i["qb_bill_id"] for i in items})
        tagmap = tags.active_tags_for_bills(conn, ids)
        for i in items:
            i["tags"] = tagmap.get(i["qb_bill_id"], [])
            i["age_bd"] = dates.business_days_ago(i["created_at"], today)
    except Exception:
        items = []                          # pre-migration: degrade gracefully
    return render_template(
        "index.html", open_items=items,
        can_edit=current_user.has_role("ap_clerk", "controller"))


@app.route("/health")
@login_required
def health():
    """Smoke-test route: app + DB + warehouse connectivity.

    The warehouse block degrades gracefully -- ok=False with a reason when
    the warehouse is unreachable (sandbox firewall, missing ODBC driver),
    which is expected outside Joe's whitelisted box.
    """
    try:
        db.q1("SELECT 1")
        db_status = "ok"
    except Exception as e:  # pragma: no cover - defensive
        db_status = f"error: {e}"

    return jsonify({
        "app": "ok",
        "db": db_status,
        "user": {"username": current_user.username, "role": current_user.role},
        "warehouse": health_check(),
    })


def start_scheduler() -> None:
    """Start APScheduler with the 15-minute bill sync. coalesce + max_instances
    keep missed/overlapping runs from stacking; the first run fires a few
    seconds after startup so a fresh deploy populates immediately. Guarded
    against the Flask reloader starting it twice."""
    from datetime import datetime, timedelta
    from apscheduler.schedulers.background import BackgroundScheduler
    import sync

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        sync.run_sync, "interval", minutes=15, id="bill_sync",
        coalesce=True, max_instances=1, misfire_grace_time=300,
        next_run_time=datetime.now() + timedelta(seconds=5),
    )
    scheduler.start()
    app.config["SCHEDULER"] = scheduler


if __name__ == "__main__":
    # Only start the scheduler in the main process, not the reloader child.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        start_scheduler()
    app.run(host="127.0.0.1", port=5000, debug=True)
