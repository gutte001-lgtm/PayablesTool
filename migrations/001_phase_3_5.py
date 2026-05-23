"""
migrations/001_phase_3_5.py -- one-shot, idempotent migration for Phase 3.5
(the first schema migration since Phase 0).

It brings an existing payables.db up to the Phase 3.5 schema:
  1. ADD COLUMN bill_metadata.status_pill TEXT          (if missing)
  2. CREATE TABLE status_pill_lookup                     (if missing)
  3. CREATE TABLE bill_tag + its two indexes            (if missing)
  4. seed the 4 shipped status pills (is_seed=1)         (if missing)

Idempotent: it checks PRAGMA table_info / sqlite_master before each change and
prints exactly what it did. Re-running on an already-migrated DB is a no-op and
exits 0.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/001_phase_3_5.py            # migrates ./payables.db
    python migrations/001_phase_3_5.py <db_path>  # migrates a specific DB

The DDL and seed list are imported from init_db so a fresh DB (init_db.py) and
a migrated DB end up byte-for-byte identical.
"""

import sqlite3
import sys
from pathlib import Path

# Allow `python migrations/001_phase_3_5.py` from the repo root: the script's
# own dir is sys.path[0], so add the repo root for `import db, init_db`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402
import init_db     # noqa: E402


def _column_exists(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _object_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def migrate(db_path, verbose=True):
    """Apply the Phase 3.5 schema to the DB at db_path. Returns a dict of what
    happened (values: 'added'/'created'/'seeded N' vs 'already present')."""
    actions = {}

    def say(msg):
        if verbose:
            print("   -", msg)

    conn = sqlite3.connect(str(db_path))
    try:
        # 1. bill_metadata.status_pill column (ALTER has no IF NOT EXISTS).
        if _column_exists(conn, "bill_metadata", "status_pill"):
            actions["status_pill_column"] = "already present"
            say("bill_metadata.status_pill: already present")
        else:
            conn.execute("ALTER TABLE bill_metadata ADD COLUMN status_pill TEXT")
            actions["status_pill_column"] = "added"
            say("bill_metadata.status_pill: ADDED")

        # 2/3. status_pill_lookup, bill_tag tables + indexes. The shared DDL is
        # all IF NOT EXISTS, so executescript is itself idempotent; the
        # pre-checks only drive the human-readable report.
        had_lookup = _object_exists(conn, "status_pill_lookup")
        had_tag = _object_exists(conn, "bill_tag")
        conn.executescript(init_db.PHASE_3_5_SCHEMA)
        actions["status_pill_lookup"] = "already present" if had_lookup else "created"
        actions["bill_tag"] = "already present" if had_tag else "created"
        say(f"status_pill_lookup table: {actions['status_pill_lookup']}")
        say(f"bill_tag table + indexes: {actions['bill_tag']}")

        # 4. seed pills (INSERT OR IGNORE; value is PK).
        created = init_db.seed_status_pills(conn)
        actions["pills_seeded"] = (
            f"seeded {len(created)}: {created}" if created
            else "already present")
        say(f"status pills: {actions['pills_seeded']}")

        conn.commit()
    finally:
        conn.close()

    actions["already_migrated"] = (
        actions["status_pill_column"] == "already present"
        and actions["status_pill_lookup"] == "already present"
        and actions["bill_tag"] == "already present"
        and actions["pills_seeded"] == "already present")
    return actions


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"Phase 3.5 migration -> {db_path}")
    actions = migrate(db_path)
    print("Already fully migrated; nothing to do." if actions["already_migrated"]
          else "Migration complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
