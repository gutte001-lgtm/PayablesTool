"""
app.py -- PayablesTool Flask entrypoint (Phase 0 scaffold).

Wires Flask-Login (auth.py), Flask-WTF CSRF, the raw-sqlite3 data layer
(db.py), and a /health smoke-test route that reports app + DB + warehouse
connectivity. APScheduler is initialized but registers no jobs yet -- the
15-minute warehouse sync arrives in Phase 1.
"""

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, url_for
from flask_login import current_user, login_required
from flask_wtf import CSRFProtect

import db
from auth import init_auth
from warehouse import health_check

load_dotenv()

app = Flask(__name__)

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. Copy .env.example to .env and set it "
        "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")."
    )
app.config["SECRET_KEY"] = _secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

csrf = CSRFProtect(app)
db.init_app(app)
init_auth(app)


@app.route("/")
@login_required
def index():
    return render_template("index.html")


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
    """Initialize APScheduler. No jobs in Phase 0 -- the 15-min warehouse
    sync is registered here in Phase 1. Guarded against the Flask reloader
    starting it twice."""
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(daemon=True)
    # Phase 1: scheduler.add_job(sync_bills, "interval", minutes=15, id="bill_sync")
    scheduler.start()
    app.config["SCHEDULER"] = scheduler


if __name__ == "__main__":
    # Only start the scheduler in the main process, not the reloader child.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        start_scheduler()
    app.run(host="127.0.0.1", port=5000, debug=True)
