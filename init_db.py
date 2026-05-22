"""
init_db.py -- create payables.db schema and seed users. Idempotent.

Run:  python init_db.py

Phase 0 schema is just the users table. Later phases add their own tables
(Bill, BillMetadata, PayRun, AuditLog, ...) following the same
CREATE TABLE IF NOT EXISTS + idempotent-seed pattern.

Seeding refuses to run without SEED_DEFAULT_PASSWORD set in the environment
(.env) -- it never invents a password. The three active accounts share that
password and are flagged must_change_password. The CEO is seeded inactive
(no login in v1) per BUILD_PLAN.
"""

import os
import sys

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

from db import DB_PATH, _connect

# (username, name, email, role, active)
SEED_USERS = [
    ("marilyn", "Marilyn Carson", "marilyn@healthcaremarkets.com", "ap_clerk", True),
    ("joe",     "Joe Guttenplan", "joe@healthcaremarkets.com",     "controller", True),
    ("shaun",   "Shaun Groat",    "shaun@healthcaremarkets.com",   "cfo", True),
    # CEO: read-only consumer, no login in v1 -> seeded inactive, no password.
    ("ceo",     "CEO (name TBD)", "ceo@healthcaremarkets.com",     "ceo", False),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    UNIQUE NOT NULL,
    name                 TEXT    NOT NULL,
    email                TEXT,
    role                 TEXT    NOT NULL
                                 CHECK (role IN ('ap_clerk','controller','cfo','ceo')),
    password_hash        TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def create_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seed_users(conn) -> list:
    """Insert any missing seed users. Returns the list of usernames created
    this run (empty if all already existed)."""
    default_pw = os.environ.get("SEED_DEFAULT_PASSWORD")
    if not default_pw:
        sys.exit(
            "ERROR: SEED_DEFAULT_PASSWORD is not set. Set it in .env "
            "(see .env.example) before seeding -- init_db.py never invents "
            "a password."
        )

    created = []
    for username, name, email, role, active in SEED_USERS:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            continue
        if active:
            pw_hash = generate_password_hash(default_pw)
            must_change = 1
        else:
            pw_hash = None          # inactive CEO has no usable login
            must_change = 0
        conn.execute(
            "INSERT INTO users "
            "(username, name, email, role, password_hash, is_active, "
            " must_change_password) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, name, email, role, pw_hash, 1 if active else 0, must_change),
        )
        created.append(username)
    conn.commit()
    return created


def init() -> None:
    load_dotenv()
    conn = _connect()
    try:
        create_schema(conn)
        created = seed_users(conn)
        rows = conn.execute(
            "SELECT username, role, is_active FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    print(f"payables.db ready at {DB_PATH}")
    print(f"seeded this run: {created or 'none (all already present)'}")
    print("users:")
    for r in rows:
        state = "active" if r["is_active"] else "inactive (no login v1)"
        print(f"  - {r['username']:<8} {r['role']:<11} {state}")


if __name__ == "__main__":
    init()
