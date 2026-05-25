"""
migrations/005_general_admin_rollup.py -- idempotent migration that LOADS one
gl_rule row, closing the GENERAL ADMINISTRATION coverage gap documented in
GL_CODING_MAP_FINAL.md S5/S6.

  Adds: gl_account_path_like  'GENERAL ADMINISTRATION EXPENSES:%'
        -> 'Other Operating Expenses'   (priority 116, active)

This routes GL 72960 (Finance Charges & Processing Fees) -- e.g. the SIMCO
$12.71 finance-charge bill (qb_bill_id 237344) -- out of Uncategorized,
consistent with the small-opex rollups already loaded at priorities 113-115.
The rule is rollup-wide, so it also folds in 72930 (Business & Franchise Taxes);
that is intentional -- both sit under the GENERAL ADMINISTRATION EXPENSES rollup.

-----------------------------------------------------------------------------
NOTE FOR FUTURE-ME -- this is the FIRST committed rule-LOADING migration
-----------------------------------------------------------------------------
Migrations 001-004 were schema-only. This is the first migration that loads a
gl_rule *row*. The other 25 gl_rule rows currently live ONLY in the live
payables.db (loaded via /admin/rules or an ad-hoc script); they are NOT codified
anywhere in version control. That is a known reproducibility gap: a fresh
init_db.py DB ships gl_rule EMPTY by design, so a from-scratch rebuild does not
reproduce the 26-rule set. Closing that gap (a committed loader/seed for the
full rule set) is deferred to a separate future session. This breadcrumb is
context, not a TODO to act on here.
-----------------------------------------------------------------------------

Prerequisite: migration 004 must have run (the gl_rule.match_type CHECK must
already allow 'gl_account_path_like'). If it hasn't, this migration stops with a
clear message rather than inserting a row that would violate the CHECK.

Idempotent: if the rule already exists (same match_type + match_value) it is a
no-op. Prints what it did, exits 0. Re-running is safe.

Note: this loads the rule only. Already-synced bills are NOT recomputed here --
run a rules recompute (/admin/rules "re-run rules" or sync.recompute_all) after
applying, to re-categorize existing bills (e.g. the SIMCO 72960 bill).

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/005_general_admin_rollup.py            # migrates ./payables.db
    python migrations/005_general_admin_rollup.py <db_path>  # migrates a specific DB
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402

_NEW_MATCH_TYPE = "gl_account_path_like"

_RULE = {
    "match_type": "gl_account_path_like",
    "match_value": "GENERAL ADMINISTRATION EXPENSES:%",
    "target_category": "Other Operating Expenses",
    "priority": 116,
}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _gl_rule_supports_path(conn):
    """True if the live gl_rule.match_type CHECK already allows path rules
    (i.e. migration 004 has been applied)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gl_rule'"
    ).fetchone()
    return bool(row and row[0] and _NEW_MATCH_TYPE in row[0])


def migrate(db_path, verbose=True):
    """Load the GENERAL ADMINISTRATION rollup rule. Returns a dict of what happened."""
    actions = {}

    def say(msg):
        if verbose:
            print("   -", msg)

    conn = sqlite3.connect(str(db_path))
    try:
        if not _gl_rule_supports_path(conn):
            raise SystemExit(
                "ERROR: gl_rule.match_type CHECK does not allow "
                "'gl_account_path_like'. Run migrations/004_phase_5_rules_engine.py "
                "first, then re-run this migration."
            )

        existing = conn.execute(
            "SELECT id FROM gl_rule WHERE match_type=? AND match_value=?",
            (_RULE["match_type"], _RULE["match_value"]),
        ).fetchone()
        if existing:
            actions["rule"] = "already present"
            say(f"GENERAL ADMINISTRATION rule already present (id={existing[0]}); no-op")
        else:
            now = _now_iso()
            cur = conn.execute(
                "INSERT INTO gl_rule "
                "(match_type, match_value, target_category, priority, active, "
                " created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,1,NULL,?,?)",
                (_RULE["match_type"], _RULE["match_value"], _RULE["target_category"],
                 _RULE["priority"], now, now),
            )
            conn.commit()
            actions["rule"] = "inserted"
            say(f"gl_rule: INSERTED {_RULE['match_value']!r} -> "
                f"{_RULE['target_category']!r} "
                f"(priority {_RULE['priority']}, id={cur.lastrowid})")
    finally:
        conn.close()

    actions["already_migrated"] = actions["rule"] == "already present"
    return actions


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"GENERAL ADMINISTRATION rollup-rule migration -> {db_path}")
    actions = migrate(db_path)
    if actions["already_migrated"]:
        print("Already loaded; nothing to do.")
    else:
        print("Migration complete. NOTE: existing bills are NOT recomputed by this "
              "migration -- run a rules recompute (/admin/rules 're-run rules' or "
              "sync.recompute_all) to re-categorize already-synced bills (e.g. the "
              "SIMCO 72960 finance-charge bill).")
    sys.exit(0)


if __name__ == "__main__":
    main()
