"""
migrations/008_close_uncategorized_rules.py -- idempotent migration that LOADS
four net-new GL rollup rules to close the 11 bills that were landing
Uncategorized (across 5 vendors). Same pattern as 007 (GL_RULES constant +
natural-key-guarded seed_gl_rules() + migrate()).

Why these are GL rules, not vendor defaults
-------------------------------------------
All 11 Uncategorized bills had a clean, consistent rollup path that simply had
no matching rule -- a GL-rule gap, not a vendor-specific case. One rule per
rollup closes them and hardens against future bills from ANY vendor hitting the
same rollup (e.g. SALES & PAYROLL TAX catches both Avalara and Georgia Dept of
Revenue, which a per-vendor default would not).

The four rules (priorities continue after 007's max of 116)
-----------------------------------------------------------
  117  gl_account_path_like  'SALES & PAYROLL TAX LIABILITIES:%'        -> Other Operating Expenses
  118  gl_account_path_like  '%SERVICE AND TRAINING COST OF GOODS SOLD' -> Contractor - Service & Repair
  119  gl_account_path_like  'SELLING EXPENSES:%'                       -> Other Operating Expenses
  120  gl_account_path_like  'BRAND & MARKETING EXPENSES:%'             -> Other Operating Expenses

Rule 118 is a deliberately NET-NEW rule, not a change to the existing rule 12
(`%SERVICE AND TRAINING COST OF GOODS SOLD:%`). Rule 12 requires a child segment
after the colon and matches 53 bills today; its parent-level account
(`COST OF GOODS SOLD:SERVICE AND TRAINING COST OF GOODS SOLD`, no child) was
missed. Rule 118 uses an ENDS-WITH pattern (no trailing `:%`) so it matches ONLY
the parent and leaves every one of rule 12's 53 bills untouched -- verified by a
full before/after recompute diff over the live DB (11 bills flip from
Uncategorized, 0 collateral, rule 12's 53 unchanged).

Category note (for review): the three misc rollups route to
'Other Operating Expenses', the established catch-all (consistent with migration
005's GENERAL ADMINISTRATION rule and the small-opex rollups at 113-116). The
SALES & PAYROLL TAX bills are sales-tax remittances; if you later want a
dedicated 'Taxes' category, change rule 117's target via /admin/rules (audited).

Relationship to 007 / init_db
-----------------------------
007 stays a clean historical snapshot of the original 26 rules; this migration
is purely additive. init_db.seed_gl_rules() seeds BOTH 007's and this file's
GL_RULES, so `init_db.py` alone reproduces all 30 rules and a clean Phase 8
rebuild is fully usable without replaying migrations.

Idempotent: each row is inserted via INSERT ... SELECT ... WHERE NOT EXISTS on
the natural key (match_type, match_value), so a row already present is left
as-is (never overwrites a live /admin/rules edit). Re-running is a no-op.

No recompute here: on the live DB run a rules recompute afterward (/admin/rules
"re-run rules" or sync.recompute_all) to re-categorize the 11 bills; on a fresh
DB there are no bills yet (the next sync categorizes them).

Prerequisite: migration 004 (gl_rule.match_type CHECK must allow
'gl_account_path_like'); all four rules use it.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/008_close_uncategorized_rules.py            # ./payables.db
    python migrations/008_close_uncategorized_rules.py <db_path>  # a specific DB
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402

_PATH_MATCH_TYPE = "gl_account_path_like"

# Authored 2026-05-29. Fixed literal stamp (created_at == updated_at) so a fresh
# rebuild reproduces byte-identical rows on everything but the AUTOINCREMENT id
# (ids are not load-bearing -- app_category_source is regenerated on sync; see
# 007's header). created_by is NULL (migration-loaded, not a logged-in author).
_TS = "2026-05-29T00:00:00+00:00"

# (match_type, match_value, target_category, priority, timestamp). active=1 each.
GL_RULES = [
    ("gl_account_path_like", "SALES & PAYROLL TAX LIABILITIES:%",        "Other Operating Expenses",      117, _TS),
    ("gl_account_path_like", "%SERVICE AND TRAINING COST OF GOODS SOLD", "Contractor - Service & Repair", 118, _TS),
    ("gl_account_path_like", "SELLING EXPENSES:%",                       "Other Operating Expenses",      119, _TS),
    ("gl_account_path_like", "BRAND & MARKETING EXPENSES:%",             "Other Operating Expenses",      120, _TS),
]


def _gl_rule_supports_path(conn):
    """True if the gl_rule.match_type CHECK already allows path rules (migration
    004 applied)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gl_rule'"
    ).fetchone()
    return bool(row and row[0] and _PATH_MATCH_TYPE in row[0])


def seed_gl_rules(conn, verbose=False):
    """Insert any of the four rules that are missing, keyed on the natural key
    (match_type, match_value). Never overwrites an existing rule. Commits.
    Returns (inserted, skipped) as lists of (match_type, match_value).

    Single source of truth: this migration's migrate() and init_db.seed_gl_rules()
    both call this, so a fresh DB and a migrated DB converge."""
    inserted, skipped = [], []
    for match_type, match_value, target_category, priority, ts in GL_RULES:
        cur = conn.execute(
            "INSERT INTO gl_rule "
            "(match_type, match_value, target_category, priority, active, "
            " created_by, created_at, updated_at) "
            "SELECT ?, ?, ?, ?, 1, NULL, ?, ? "
            "WHERE NOT EXISTS "
            "(SELECT 1 FROM gl_rule WHERE match_type=? AND match_value=?)",
            (match_type, match_value, target_category, priority, ts, ts,
             match_type, match_value),
        )
        if cur.rowcount:
            inserted.append((match_type, match_value))
            if verbose:
                print(f"   - gl_rule: INSERTED {match_value!r} -> "
                      f"{target_category!r} (priority {priority})")
        else:
            skipped.append((match_type, match_value))
            if verbose:
                print(f"   - gl_rule: present, skipped {match_value!r}")
    conn.commit()
    return inserted, skipped


def migrate(db_path, verbose=True):
    """Load the four rollup rules into the DB at db_path. Returns
    {inserted, skipped, inserted_rules, already_migrated}."""
    conn = sqlite3.connect(str(db_path))
    try:
        if not _gl_rule_supports_path(conn):
            raise SystemExit(
                "ERROR: gl_rule.match_type CHECK does not allow "
                "'gl_account_path_like'. Run migrations/004_phase_5_rules_engine.py "
                "first, then re-run this migration."
            )
        inserted, skipped = seed_gl_rules(conn, verbose=verbose)
    finally:
        conn.close()
    return {
        "inserted": len(inserted),
        "skipped": len(skipped),
        "inserted_rules": inserted,
        "already_migrated": len(inserted) == 0,
    }


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB_PATH
    if not Path(db_path).exists():
        sys.exit(f"ERROR: DB not found at {db_path}. Nothing to migrate.")
    print(f"Close-uncategorized-rules migration ({len(GL_RULES)} rules) -> {db_path}")
    actions = migrate(db_path)
    if actions["already_migrated"]:
        print(f"All {len(GL_RULES)} rules already present; nothing to do.")
    else:
        print(f"Migration complete: {actions['inserted']} inserted, "
              f"{actions['skipped']} already present. Run a rules recompute "
              "(/admin/rules 're-run rules' or sync.recompute_all) to "
              "re-categorize the Uncategorized bills.")
    sys.exit(0)


if __name__ == "__main__":
    main()
