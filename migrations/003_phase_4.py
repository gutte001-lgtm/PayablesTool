"""
migrations/003_phase_4.py -- one-shot, idempotent migration for Phase 4
(pay-run builder). Same pattern as 001/002.

The pay_run / pay_run_line tables already exist as stubs (Phase 1b). Phase 4
only needs two new line-review columns on pay_run_line:
  1. ADD COLUMN pay_run_line.reviewed_by_user_id INTEGER   (if missing)
  2. ADD COLUMN pay_run_line.reviewed_at         TEXT      (if missing)

Idempotent: checks PRAGMA table_info before each ALTER (ADD COLUMN has no
IF NOT EXISTS), prints what it did, exits 0. Re-running is a no-op.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/003_phase_4.py            # migrates ./payables.db
    python migrations/003_phase_4.py <db_path>  # migrates a specific DB
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402

_NEW_COLUMNS = [
    ("reviewed_by_user_id", "INTEGER"),
    ("reviewed_at", "TEXT"),
]


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def migrate(db_path, verbose=True):
    """Apply the Phase 4 schema to the DB at db_path. Returns a dict of what
    happened per column ('added' vs 'already present')."""
    actions = {}

    def say(msg):
        if verbose:
            print("   -", msg)

    conn = sqlite3.connect(str(db_path))
    try:
        cols = _columns(conn, "pay_run_line")
        for name, decl in _NEW_COLUMNS:
            if name in cols:
                actions[name] = "already present"
                say(f"pay_run_line.{name}: already present")
            else:
                conn.execute(f"ALTER TABLE pay_run_line ADD COLUMN {name} {decl}")
                actions[name] = "added"
                say(f"pay_run_line.{name}: ADDED")
        conn.commit()
    finally:
        conn.close()

    actions["already_migrated"] = all(v == "already present" for v in actions.values())
    return actions


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"Phase 4 migration -> {db_path}")
    actions = migrate(db_path)
    print("Already fully migrated; nothing to do." if actions["already_migrated"]
          else "Migration complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
