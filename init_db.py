"""
init_db.py -- create payables.db schema and seed users. Idempotent.

Run:  python init_db.py

Phase 0 added the users table. Phase 1b adds the bill data spine:
bill / bill_line / bill_metadata / gl_rule / vendor_category_default /
audit_log / note (append-only, trigger-enforced) / todo, plus pay_run /
pay_run_line stubs (no UI until Phase 4). All idempotent
(CREATE ... IF NOT EXISTS), so re-running never destroys data.

Money is stored as INTEGER cents, dates as ISO 'YYYY-MM-DD' TEXT, timestamps
as ISO TEXT, booleans as INTEGER 0/1.

Seeding refuses to run without SEED_DEFAULT_PASSWORD set in .env -- it never
invents a password. gl_rule and vendor_category_default ship EMPTY by design:
everything starts Uncategorized until rules are authored against real data.
"""

import sys

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

from db import DB_PATH, _connect

# Read .env DIRECTLY from the file rather than load_dotenv() + os.environ.
# load_dotenv() does not override variables already present in the shell, so
# an empty/stale SEED_DEFAULT_PASSWORD= leaked into a PowerShell session would
# silently win over the .env value. dotenv_values reads the file itself.
ENV_PATH = DB_PATH.parent / ".env"

# (username, name, email, role, active)
SEED_USERS = [
    ("marilyn", "Marilyn Carson", "marilyn@healthcaremarkets.com", "ap_clerk", True),
    ("joe",     "Joe Guttenplan", "joe@healthcaremarkets.com",     "controller", True),
    ("shaun",   "Shaun Groat",    "shaun@healthcaremarkets.com",   "cfo", True),
    # CEO: read-only consumer, no login in v1 -> seeded inactive, no password.
    ("ceo",     "CEO (name TBD)", "ceo@healthcaremarkets.com",     "ceo", False),
]

SCHEMA = """
-- ===== Users (Phase 0) =====================================================
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

-- ===== Bill header (read-only mirror of dbo.Bill) ==========================
CREATE TABLE IF NOT EXISTS bill (
    qb_bill_id          TEXT PRIMARY KEY,
    bill_number         TEXT,
    vendor_ref          TEXT,
    vendor              TEXT,
    bill_date           TEXT,            -- ISO date
    due_date            TEXT,            -- ISO date, nullable
    amount_cents        INTEGER NOT NULL DEFAULT 0,
    open_balance_cents  INTEGER NOT NULL DEFAULT 0,
    qb_memo             TEXT,
    currency            TEXT,
    department          TEXT,
    ap_account          TEXT,
    sales_term          TEXT,            -- Bill.SalesTermRefName (for later pay-date logic)
    qb_created_at       TEXT,
    qb_updated_at       TEXT,            -- drives the look-back window
    is_paid             INTEGER NOT NULL DEFAULT 0,
    date_parse_warning  INTEGER NOT NULL DEFAULT 0,
    last_synced_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bill_open    ON bill(open_balance_cents);
CREATE INDEX IF NOT EXISTS idx_bill_due     ON bill(due_date);
CREATE INDEX IF NOT EXISTS idx_bill_vendor  ON bill(vendor_ref);
CREATE INDEX IF NOT EXISTS idx_bill_updated ON bill(qb_updated_at);
CREATE INDEX IF NOT EXISTS idx_bill_paid    ON bill(is_paid);

-- ===== Bill lines (from reporting.fact_bill_line; replaced per sync) =======
CREATE TABLE IF NOT EXISTS bill_line (
    qb_bill_id         TEXT NOT NULL REFERENCES bill(qb_bill_id),
    line_number        INTEGER NOT NULL,
    qb_line_id         TEXT,
    detail_type        TEXT,
    line_description   TEXT,
    line_amount_cents  INTEGER,
    gl_account_id      TEXT,
    gl_account_name    TEXT,
    gl_account_number  TEXT,             -- parsed leading digits, for rule matching
    qb_class_id        TEXT,
    qb_class_name      TEXT,
    item_id            TEXT,
    item_name          TEXT,
    PRIMARY KEY (qb_bill_id, line_number)
);
CREATE INDEX IF NOT EXISTS idx_line_bill  ON bill_line(qb_bill_id);
CREATE INDEX IF NOT EXISTS idx_line_acct  ON bill_line(gl_account_number);
CREATE INDEX IF NOT EXISTS idx_line_class ON bill_line(qb_class_name);

-- ===== Bill metadata (app-owned, 1:1, never overwritten by sync) ===========
CREATE TABLE IF NOT EXISTS bill_metadata (
    qb_bill_id              TEXT PRIMARY KEY REFERENCES bill(qb_bill_id),
    classification          TEXT
        CHECK (classification IS NULL OR classification IN
               ('Real','Refund-Visibility','Prepayment-Deposit','Other')),
    app_category            TEXT,
    app_category_source     TEXT,        -- manual | gl_rule:<id> | vendor_default | uncategorized
    app_category_breakdown  TEXT,        -- JSON [{category, amount_cents, line_count}]
    app_category_manual     TEXT,        -- manual override; wins over computed
    approver_name           TEXT,
    approval_channel        TEXT
        CHECK (approval_channel IS NULL OR approval_channel IN
               ('Pur Board','MS List','NSPO','Email','Other')),
    approval_date           TEXT,
    service_performed_date  TEXT,
    receipt_delivery_date   TEXT,
    ops_number              TEXT,
    ops_numbers_all         TEXT,
    proposed_payment_method TEXT
        CHECK (proposed_payment_method IS NULL OR proposed_payment_method IN
               ('Check','Wire','Credit Card','ACH')),
    proposed_pay_date       TEXT,
    ok_for_ceo              INTEGER NOT NULL DEFAULT 0,
    approval_state          TEXT NOT NULL DEFAULT 'New'
        CHECK (approval_state IN ('New','AP_Reviewed','Controller_Reviewed')),
    rush_flag               INTEGER NOT NULL DEFAULT 0,
    has_credit_applied      INTEGER NOT NULL DEFAULT 0,
    partial_payment_flag    INTEGER NOT NULL DEFAULT 0,
    status_pill             TEXT,        -- Phase 3.5; FK-ish to status_pill_lookup.value
                                         -- (enforced in app code, not DB), nullable = no pill
    created_at              TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meta_state ON bill_metadata(approval_state);
CREATE INDEX IF NOT EXISTS idx_meta_class ON bill_metadata(classification);
CREATE INDEX IF NOT EXISTS idx_meta_cat   ON bill_metadata(app_category);
CREATE INDEX IF NOT EXISTS idx_meta_ceo   ON bill_metadata(ok_for_ceo);
CREATE INDEX IF NOT EXISTS idx_meta_ops   ON bill_metadata(ops_number);

-- ===== GL/Class rules engine ==============================================
CREATE TABLE IF NOT EXISTS gl_rule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type      TEXT NOT NULL
        CHECK (match_type IN
               ('gl_account_number','gl_account_name_like','class_name','gl_and_class')),
    match_value     TEXT NOT NULL,
    target_category TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    active          INTEGER NOT NULL DEFAULT 1,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glrule_active_priority ON gl_rule(active, priority);

CREATE TABLE IF NOT EXISTS vendor_category_default (
    vendor_id        TEXT PRIMARY KEY,
    vendor_name      TEXT,
    default_category TEXT NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1,
    created_by       INTEGER REFERENCES users(id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- ===== Audit log ===========================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),   -- NULL = system/sync
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    action      TEXT NOT NULL,
    before      TEXT,                            -- JSON
    after       TEXT,                            -- JSON
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_entity  ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit_log(action);

-- ===== Notes (append-only, enforced by triggers) ===========================
CREATE TABLE IF NOT EXISTS note (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qb_bill_id  TEXT NOT NULL REFERENCES bill(qb_bill_id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_note_bill ON note(qb_bill_id, created_at);
CREATE TRIGGER IF NOT EXISTS note_no_update
BEFORE UPDATE ON note
BEGIN
    SELECT RAISE(ABORT, 'note is append-only: UPDATE is not allowed');
END;
CREATE TRIGGER IF NOT EXISTS note_no_delete
BEFORE DELETE ON note
BEGIN
    SELECT RAISE(ABORT, 'note is append-only: DELETE is not allowed');
END;

-- ===== To-dos ==============================================================
CREATE TABLE IF NOT EXISTS todo (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    qb_bill_id   TEXT NOT NULL REFERENCES bill(qb_bill_id),
    body         TEXT NOT NULL,
    completed_at TEXT,
    completed_by INTEGER REFERENCES users(id),
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_bill ON todo(qb_bill_id);

-- ===== Pay run (STUBS -- no UI until Phase 4) ==============================
CREATE TABLE IF NOT EXISTS pay_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    week_ending TEXT,
    created_by  INTEGER REFERENCES users(id),
    status      TEXT NOT NULL DEFAULT 'Draft',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pay_run_line (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pay_run_id          INTEGER NOT NULL REFERENCES pay_run(id),
    qb_bill_id          TEXT NOT NULL REFERENCES bill(qb_bill_id),
    payment_method      TEXT,
    amount_to_pay_cents  INTEGER,
    included            INTEGER NOT NULL DEFAULT 1,
    line_state          TEXT NOT NULL DEFAULT 'Pending',
    cfo_note            TEXT,
    reviewed_by_user_id INTEGER REFERENCES users(id),  -- Phase 4: line approve/reject actor
    reviewed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_payrunline_run  ON pay_run_line(pay_run_id);
CREATE INDEX IF NOT EXISTS idx_payrunline_bill ON pay_run_line(qb_bill_id);
-- Phase 4: a bill appears at most once per run (backstops the picker's claim
-- filter against same-run duplicate lines).
CREATE UNIQUE INDEX IF NOT EXISTS idx_payrunline_unique ON pay_run_line(pay_run_id, qb_bill_id);
"""

# ===== Phase 3.5 -- follow-up workspace (status pills + bill tags) ==========
# Kept as a separate constant so the one-shot migration
# (migrations/001_phase_3_5.py) reuses the EXACT DDL when upgrading the live
# 493-bill DB. Every statement is IF NOT EXISTS, so re-running is harmless.
PHASE_3_5_SCHEMA = """
CREATE TABLE IF NOT EXISTS status_pill_lookup (
    value      TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES users(id),  -- NULL for seed pills
    created_at TEXT,
    is_seed    INTEGER NOT NULL DEFAULT 0     -- 1 = shipped seed, 0 = user-added
);

CREATE TABLE IF NOT EXISTS bill_tag (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    qb_bill_id         TEXT    NOT NULL REFERENCES bill(qb_bill_id),
    tagged_user_id     INTEGER NOT NULL REFERENCES users(id),
    tagged_by_user_id  INTEGER NOT NULL REFERENCES users(id),
    tagged_at          TEXT    NOT NULL,
    cleared_at         TEXT,                  -- NULL = active tag
    cleared_by_user_id INTEGER REFERENCES users(id),
    note               TEXT
);
-- "tagged for me" query (active tags for a user); per-bill chip display.
CREATE INDEX IF NOT EXISTS idx_billtag_user ON bill_tag(tagged_user_id, cleared_at);
CREATE INDEX IF NOT EXISTS idx_billtag_bill ON bill_tag(qb_bill_id, cleared_at);
"""

SCHEMA = SCHEMA + PHASE_3_5_SCHEMA

# ===== Phase 3.6 -- open items (explicit "this bill needs work") ============
# A boolean-ish flag (a row = an open item) + free-text description on any bill.
# Junction-style like bill_tag: multiple open items per bill; resolved_at IS NULL
# means open. Reused by migrations/002_phase_3_6.py (all IF NOT EXISTS).
PHASE_3_6_SCHEMA = """
CREATE TABLE IF NOT EXISTS bill_open_item (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    qb_bill_id          TEXT    NOT NULL REFERENCES bill(qb_bill_id),
    description         TEXT    NOT NULL,   -- "what needs to happen"
    created_by_user_id  INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT    NOT NULL,
    resolved_at         TEXT,               -- NULL = open
    resolved_by_user_id INTEGER REFERENCES users(id),
    resolution_note     TEXT                -- required when resolving (UI-enforced)
);
-- per-bill display; global open-list query.
CREATE INDEX IF NOT EXISTS idx_openitem_bill ON bill_open_item(qb_bill_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_openitem_open ON bill_open_item(resolved_at, created_at);
"""

SCHEMA = SCHEMA + PHASE_3_6_SCHEMA

# Seed status pills (is_seed=1). Shared by init_db (fresh DB) and the migration.
SEED_PILLS = ("Waiting on Vendor", "Waiting on Approver", "In Review", "Blocked")


def create_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seed_status_pills(conn) -> list:
    """Insert any missing seed pills (is_seed=1). Idempotent (value is PK).
    Returns the list of pill values created this run."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")
    created = []
    for value in SEED_PILLS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO status_pill_lookup "
            "(value, created_by, created_at, is_seed) VALUES (?, NULL, ?, 1)",
            (value, now),
        )
        if cur.rowcount:
            created.append(value)
    conn.commit()
    return created


def seed_users(conn) -> list:
    """Insert any missing seed users. Returns the list of usernames created
    this run (empty if all already existed)."""
    default_pw = dotenv_values(ENV_PATH).get("SEED_DEFAULT_PASSWORD")
    if not default_pw:
        sys.exit(
            f"ERROR: SEED_DEFAULT_PASSWORD is not set in {ENV_PATH}. Set it "
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
    conn = _connect()
    try:
        create_schema(conn)
        created = seed_users(conn)
        pills_created = seed_status_pills(conn)
        rows = conn.execute(
            "SELECT username, role, is_active FROM users ORDER BY id"
        ).fetchall()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
    finally:
        conn.close()

    print(f"payables.db ready at {DB_PATH}")
    print(f"tables: {tables}")
    print(f"seeded this run: {created or 'none (all already present)'}")
    print(f"status pills seeded this run: {pills_created or 'none (all already present)'}")
    print("users:")
    for r in rows:
        state = "active" if r["is_active"] else "inactive (no login v1)"
        print(f"  - {r['username']:<8} {r['role']:<11} {state}")


if __name__ == "__main__":
    init()
