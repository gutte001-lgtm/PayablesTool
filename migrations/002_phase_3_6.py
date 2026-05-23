"""
migrations/002_phase_3_6.py -- one-shot, idempotent migration for Phase 3.6
(open items). Same pattern as 001_phase_3_5.py.

It brings an existing payables.db (already at Phase 3.5) up to Phase 3.6:
  1. CREATE TABLE bill_open_item + its two indexes   (if missing)

Idempotent: checks sqlite_master before reporting and uses IF NOT EXISTS DDL, so
re-running on an already-migrated DB is a no-op and exits 0. Prints what it did.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/002_phase_3_6.py            # migrates ./payables.db
    python migrations/002_phase_3_6.py <db_path>  # migrates a specific DB

The DDL is imported from init_db (PHASE_3_6_SCHEMA) so a fresh DB (init_db.py)
and a migrated DB end up byte-for-byte identical.
"""

import sqlite3
import sys
from pathlib import Path

# Allow `python migrations/002_phase_3_6.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402
import init_db     # noqa: E402


def _object_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def migrate(db_path, verbose=True):
    """Apply the Phase 3.6 schema to the DB at db_path. Returns a dict of what
    happened ('created' vs 'already present')."""
    actions = {}

    def say(msg):
        if verbose:
            print("   -", msg)

    conn = sqlite3.connect(str(db_path))
    try:
        had_table = _object_exists(conn, "bill_open_item")
        # IF NOT EXISTS DDL: idempotent in itself; the pre-check only drives the
        # human-readable report.
        conn.executescript(init_db.PHASE_3_6_SCHEMA)
        actions["bill_open_item"] = "already present" if had_table else "created"
        say(f"bill_open_item table + indexes: {actions['bill_open_item']}")
        conn.commit()
    finally:
        conn.close()

    actions["already_migrated"] = (actions["bill_open_item"] == "already present")
    return actions


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"Phase 3.6 migration -> {db_path}")
    actions = migrate(db_path)
    print("Already fully migrated; nothing to do." if actions["already_migrated"]
          else "Migration complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
