"""
db.py -- thin raw-sqlite3 helpers (CloseTool convention, no ORM).

A connection is opened per request and cached on Flask's `g`, then closed on
teardown. Outside a request context (e.g. init_db.py), get_db() opens a plain
connection the caller is responsible for closing.
"""

import sqlite3
from pathlib import Path

from flask import g

DB_PATH = Path(__file__).parent / "payables.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db() -> sqlite3.Connection:
    """Per-request connection cached on g. Falls back to a fresh connection
    when there is no app/request context (scripts)."""
    try:
        if "db" not in g:
            g.db = _connect()
        return g.db
    except RuntimeError:
        # No application context (e.g. called from init_db.py as a script).
        return _connect()


def close_db(exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app) -> None:
    """Register teardown so request-scoped connections always close."""
    app.teardown_appcontext(close_db)


# ---- convenience query helpers -------------------------------------------

def q1(sql: str, params: tuple = ()):
    """First matching row (sqlite3.Row) or None."""
    return get_db().execute(sql, params).fetchone()


def qa(sql: str, params: tuple = ()):
    """All matching rows (list of sqlite3.Row)."""
    return get_db().execute(sql, params).fetchall()


def execute(sql: str, params: tuple = (), commit: bool = True):
    """Run a write statement; return the cursor (lastrowid, rowcount)."""
    db = get_db()
    cur = db.execute(sql, params)
    if commit:
        db.commit()
    return cur
