"""
migrations/004_phase_5_rules_engine.py -- one-shot, idempotent migration for the
Phase 5 rollup rules-engine prerequisites. Same pattern as 001/002/003.

It brings an existing payables.db up to support rollup-based GL rules:
  1. ADD COLUMN bill_line.gl_account_number_canonical TEXT   (if missing)
  2. ADD COLUMN bill_line.gl_account_path           TEXT     (if missing)
  3. Rebuild gl_rule so its match_type CHECK accepts 'gl_account_path_like'
     (SQLite cannot ALTER a CHECK -> create gl_rule_new, copy rows, drop, rename;
     the index + all existing rule rows are preserved)

It does NOT backfill the two new bill_line columns -- the next warehouse sync
replaces bill_line and fills them. A re-sync is required after this migration.

Idempotent: checks PRAGMA table_info and the gl_rule CHECK text before each
change, prints what it did, exits 0. Re-running on a migrated DB is a no-op.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/004_phase_5_rules_engine.py            # migrates ./payables.db
    python migrations/004_phase_5_rules_engine.py <db_path>  # migrates a specific DB
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402

_NEW_COLUMNS = [
    ("gl_account_number_canonical", "TEXT"),
    ("gl_account_path", "TEXT"),
]
_NEW_MATCH_TYPE = "gl_account_path_like"

# gl_rule with the widened CHECK. Mirrors init_db.py exactly except the CHECK now
# lists 'gl_account_path_like'. Used only when the live table predates Phase 5.
_GL_RULE_NEW = """
CREATE TABLE gl_rule_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type      TEXT NOT NULL
        CHECK (match_type IN
               ('gl_account_number','gl_account_name_like','class_name','gl_and_class',
                'gl_account_path_like')),
    match_value     TEXT NOT NULL,
    target_category TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    active          INTEGER NOT NULL DEFAULT 1,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _gl_rule_supports_path(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gl_rule'"
    ).fetchone()
    return bool(row and row[0] and _NEW_MATCH_TYPE in row[0])


def migrate(db_path, verbose=True):
    """Apply the Phase 5 rules-engine schema. Returns a dict of what happened."""
    actions = {}

    def say(msg):
        if verbose:
            print("   -", msg)

    conn = sqlite3.connect(str(db_path))
    try:
        # 1/2. new bill_line columns (ALTER has no IF NOT EXISTS).
        cols = _columns(conn, "bill_line")
        for name, decl in _NEW_COLUMNS:
            if name in cols:
                actions[name] = "already present"
                say(f"bill_line.{name}: already present")
            else:
                conn.execute(f"ALTER TABLE bill_line ADD COLUMN {name} {decl}")
                actions[name] = "added"
                say(f"bill_line.{name}: ADDED")

        # 3. widen the gl_rule.match_type CHECK by rebuilding the table.
        if _gl_rule_supports_path(conn):
            actions["gl_rule_check"] = "already present"
            say("gl_rule CHECK already allows gl_account_path_like")
        else:
            n = conn.execute("SELECT COUNT(*) FROM gl_rule").fetchone()[0]
            conn.executescript(
                "PRAGMA foreign_keys=OFF;\n"
                "BEGIN;\n"
                + _GL_RULE_NEW +
                "INSERT INTO gl_rule_new "
                "(id, match_type, match_value, target_category, priority, active, "
                " created_by, created_at, updated_at) "
                "SELECT id, match_type, match_value, target_category, priority, active, "
                "       created_by, created_at, updated_at FROM gl_rule;\n"
                "DROP TABLE gl_rule;\n"
                "ALTER TABLE gl_rule_new RENAME TO gl_rule;\n"
                "CREATE INDEX IF NOT EXISTS idx_glrule_active_priority "
                "    ON gl_rule(active, priority);\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys=ON;\n"
            )
            actions["gl_rule_check"] = f"rebuilt (preserved {n} row(s))"
            say(f"gl_rule: REBUILT to allow gl_account_path_like "
                f"({n} existing rule row(s) preserved)")

        conn.commit()
    finally:
        conn.close()

    actions["already_migrated"] = (
        actions["gl_account_number_canonical"] == "already present"
        and actions["gl_account_path"] == "already present"
        and actions["gl_rule_check"] == "already present")
    return actions


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"Phase 5 rules-engine migration -> {db_path}")
    actions = migrate(db_path)
    if actions["already_migrated"]:
        print("Already fully migrated; nothing to do.")
    else:
        print("Migration complete. NOTE: the two new bill_line columns are EMPTY "
              "until the next warehouse sync repopulates bill_line -- run a sync "
              "(Pull Now or the scheduled job) before authoring path rules.")
    sys.exit(0)


if __name__ == "__main__":
    main()
