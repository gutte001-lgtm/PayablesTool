"""
migrations/006_phase_4_5.py -- one-shot, idempotent migration for Phase 4.5
(AP dueness classification: 2-D obligation_type x due_state). Same pattern as
001-005.

It brings an existing payables.db up to the Phase 4.5 schema:
  1. ADD COLUMN bill_metadata.invoice_due_date       TEXT                 (if missing)
     ADD COLUMN bill_metadata.expected_payment_date  TEXT                 (if missing)
     ADD COLUMN bill_metadata.obligation_type        TEXT NOT NULL DEFAULT 'ordinary_ap' CHECK(...)
     ADD COLUMN bill_metadata.due_state              TEXT NOT NULL DEFAULT 'not_due' CHECK(...)
     ADD COLUMN bill_metadata.classification_reason  TEXT                 (if missing)
     ADD COLUMN bill_metadata.classification_note    TEXT                 (if missing)
     ADD COLUMN bill_metadata.classified_by          INTEGER REFERENCES users(id) (if missing)
     ADD COLUMN bill_metadata.classified_at          TEXT                 (if missing)
  2. CREATE TABLE classification_reason_lookup        (if missing)
  3. CREATE TABLE classification_audit + its index    (if missing)
  4. CREATE INDEX idx_meta_obligation / idx_meta_duestate (if missing)
  5. seed the 8 shipped classification reasons (is_seed=1) (if missing)

Existing rows backfill to the column DEFAULTs (obligation_type='ordinary_ap',
due_state='not_due') the instant the NOT-NULL-DEFAULT columns are added (SQLite
applies the default to existing rows). invoice_due_date / expected_payment_date
stay NULL until the NEXT sync populates them from QB. Run a sync after migrating.

Idempotent: checks PRAGMA table_info / sqlite_master before each change (ALTER
has no IF NOT EXISTS), prints what it did, exits 0. Re-running is a no-op.

The DDL + seed list are imported from init_db so a fresh DB (init_db.py) and a
migrated DB end up byte-for-byte identical.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/006_phase_4_5.py            # migrates ./payables.db
    python migrations/006_phase_4_5.py <db_path>  # migrates a specific DB
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402
import init_db     # noqa: E402

# Declarations MUST match the CREATE TABLE bill_metadata body in init_db.py so a
# migrated DB and a fresh DB converge. NOT NULL DEFAULT '<constant>' and CHECK
# are both accepted by SQLite's ALTER TABLE ADD COLUMN (the default is constant).
_NEW_COLUMNS = [
    ("invoice_due_date", "TEXT"),
    ("expected_payment_date", "TEXT"),
    ("obligation_type",
     "TEXT NOT NULL DEFAULT 'ordinary_ap' "
     "CHECK (obligation_type IN ('ordinary_ap','debt_service','not_real_ap'))"),
    ("due_state",
     "TEXT NOT NULL DEFAULT 'not_due' "
     "CHECK (due_state IN ('due','not_due'))"),
    ("classification_reason", "TEXT"),
    ("classification_note", "TEXT"),
    ("classified_by", "INTEGER REFERENCES users(id)"),
    ("classified_at", "TEXT"),
]


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _object_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def migrate(db_path, verbose=True):
    """Apply the Phase 4.5 schema to the DB at db_path. Returns a dict of what
    happened (values: 'added'/'created'/'seeded N' vs 'already present')."""
    actions = {}

    def say(msg):
        if verbose:
            print("   -", msg)

    conn = sqlite3.connect(str(db_path))
    try:
        # 1. The eight bill_metadata columns (ALTER has no IF NOT EXISTS).
        cols = _columns(conn, "bill_metadata")
        for name, decl in _NEW_COLUMNS:
            if name in cols:
                actions[name] = "already present"
                say(f"bill_metadata.{name}: already present")
            else:
                conn.execute(f"ALTER TABLE bill_metadata ADD COLUMN {name} {decl}")
                actions[name] = "added"
                say(f"bill_metadata.{name}: ADDED")

        # 2/3/4. lookup + audit tables + the two metadata indexes. The shared DDL
        # is all IF NOT EXISTS, so executescript is itself idempotent; the
        # pre-checks only drive the human-readable report. (Runs AFTER the column
        # ALTERs above so idx_meta_obligation/duestate reference live columns.)
        had_lookup = _object_exists(conn, "classification_reason_lookup")
        had_audit = _object_exists(conn, "classification_audit")
        conn.executescript(init_db.PHASE_4_5_SCHEMA)
        actions["classification_reason_lookup"] = (
            "already present" if had_lookup else "created")
        actions["classification_audit"] = (
            "already present" if had_audit else "created")
        say(f"classification_reason_lookup table: {actions['classification_reason_lookup']}")
        say(f"classification_audit table + index: {actions['classification_audit']}")
        say("idx_meta_obligation / idx_meta_duestate: ensured")

        # 5. seed reasons (INSERT OR IGNORE; value is PK).
        created = init_db.seed_classification_reasons(conn)
        actions["reasons_seeded"] = (
            f"seeded {len(created)}: {created}" if created else "already present")
        say(f"classification reasons: {actions['reasons_seeded']}")

        conn.commit()
    finally:
        conn.close()

    actions["already_migrated"] = (
        all(actions[name] == "already present" for name, _ in _NEW_COLUMNS)
        and actions["classification_reason_lookup"] == "already present"
        and actions["classification_audit"] == "already present"
        and actions["reasons_seeded"] == "already present")
    return actions


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"Phase 4.5 migration -> {db_path}")
    actions = migrate(db_path)
    if actions["already_migrated"]:
        print("Already fully migrated; nothing to do.")
    else:
        print("Migration complete. Existing rows backfilled to "
              "obligation_type='ordinary_ap' / due_state='not_due'. "
              "invoice_due_date / expected_payment_date populate on the NEXT "
              "sync -- run a sync after migrating.")
    sys.exit(0)


if __name__ == "__main__":
    main()
