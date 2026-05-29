"""
migrations/007_codify_gl_rules.py -- idempotent migration that CODIFIES the full
26-rule gl_rule set into version control, closing the reproducibility gap that
005's "NOTE FOR FUTURE-ME" breadcrumb documented.

Background
----------
Migrations 001-004 were schema-only. 005 was the first rule-LOADING migration,
but it loaded exactly ONE rule (the GENERAL ADMINISTRATION rollup). The other 25
gl_rule rows lived ONLY in the live payables.db (entered via /admin/rules or an
ad-hoc load) and were not codified anywhere -- so a from-scratch init_db.py DB
(which ships gl_rule EMPTY by design) reproduced 0% of them and ~98% of
categorization logic would have been lost on a clean rebuild.

This migration is a FAITHFUL SNAPSHOT of the 26 live rules as of 2026-05-29,
not a taxonomy cleanup. The two judgement calls in the set are intentional and
preserved as-is (any future correction goes through /admin/rules so it is
audited): rule for 'CUSTOMER DEPOSITS & DEFERRED REVENUE:%' -> 'Refunds', and
the split between 'CAPEX - Software' (GL 14300/14400) and 'CAPEX' (FIXED ASSETS
rollup).

Relationship to 005 (deliberate, do NOT change 005)
---------------------------------------------------
005 inserts the GENERAL ADMINISTRATION rule; this migration's GL_RULES list
INCLUDES that same rule. Both are guarded on the natural key (match_type,
match_value), so they compose with zero duplication regardless of run order. On
a fresh rebuild the order is init_db -> 001-006 -> 007: 005 inserts the GENERAL
ADMIN row first, then 007 inserts the other 25 and SKIPS the GENERAL ADMIN row
it already finds. (007 is therefore safe to run with or without 005.)

IDs are NOT preserved
---------------------
gl_rule.id is AUTOINCREMENT. app_category_source stores 'gl_rule:<id>' purely as
a provenance label that sync.recompute_* REWRITES on every run; nothing
dereferences the id back into a rule. A fresh rebuild has no bills until a
warehouse sync, and that sync regenerates every label against the then-current
ids. So fresh-rebuild ids legitimately differ from the live 1-26 ordering and
nothing breaks. This migration does NOT hardcode ids.

Idempotency
-----------
The natural key (match_type, match_value) is unique across all 26 rows in the
data but is NOT enforced by a UNIQUE constraint, so we cannot use
INSERT OR IGNORE. Each row is inserted via INSERT ... SELECT ... WHERE NOT
EXISTS, so a row already present (by natural key) is left exactly as-is -- this
migration only FILLS missing rules, it never overwrites a live edit. Re-running
is a pure no-op. Returns already_migrated=True when all 26 were already present.

No recompute needed (unlike 005): on the live DB all 26 rules already exist, so
this is a no-op; on a fresh DB there are no bills yet, so the next warehouse sync
categorizes everything. There is nothing to re-categorize at migration time.

The GL_RULES constant and seed_gl_rules() defined here are the single source of
truth -- init_db.seed_gl_rules() delegates to them so a fresh DB (init_db.py) and
a migrated DB end up with an identical rule set.

Prerequisite: migration 004 must have run (the gl_rule.match_type CHECK must
allow 'gl_account_path_like'). If it hasn't, this migration stops with a clear
message rather than inserting a row that would violate the CHECK.

Run (from the repo root), AFTER pausing OneDrive (AGENTS.md S8):
    python migrations/007_codify_gl_rules.py            # migrates ./payables.db
    python migrations/007_codify_gl_rules.py <db_path>  # migrates a specific DB
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402

_PATH_MATCH_TYPE = "gl_account_path_like"

# Bulk-load timestamp shared by the 25 rules loaded 2026-05-24; the GENERAL
# ADMINISTRATION rollup (loaded later by migration 005) carries its own stamp.
# created_at == updated_at for every live row, and created_by is NULL (all 26
# were system/ad-hoc loaded, not authored by a logged-in user). Preserving the
# literal timestamps keeps a fresh-rebuild row byte-identical to the live row on
# everything except the AUTOINCREMENT id (see "IDs are NOT preserved" above).
_TS_BULK = "2026-05-24 02:20:26+00:00"
_TS_GENERAL_ADMIN = "2026-05-25T19:12:32+00:00"

# The 26 rules, in live id order. active=1 for all. Each tuple is
# (match_type, match_value, target_category, priority, timestamp).
GL_RULES = [
    ("gl_account_number",    "72510", "Legal Fees",                              10,  _TS_BULK),
    ("gl_account_number",    "14900", "Occupancy",                               11,  _TS_BULK),
    ("gl_account_number",    "14300", "CAPEX - Software",                        12,  _TS_BULK),
    ("gl_account_number",    "14400", "CAPEX - Software",                        13,  _TS_BULK),
    ("gl_account_number",    "74960", "Contractor - Outside Sales Commissions",  14,  _TS_BULK),
    ("gl_account_name_like", "%Training COGS%",                "Contractor - Training",       15,  _TS_BULK),
    ("gl_account_name_like", "%Telephone & Internet Access",   "Other Operating Expenses",    16,  _TS_BULK),
    ("gl_account_name_like", "%New Device COGS",               "New Device Purchases",        17,  _TS_BULK),
    ("gl_account_name_like", "%Pre-Owned Device COGS",         "Pre-owned Device Purchases",  18,  _TS_BULK),
    ("gl_account_path_like", "%PRODUCT COST OF GOODS SOLD:%",              "Parts & Products",              100, _TS_BULK),
    ("gl_account_path_like", "OUTBOUND SHIPPING COST OF GOODS SOLD:%",     "Freight",                       101, _TS_BULK),
    ("gl_account_path_like", "%SERVICE AND TRAINING COST OF GOODS SOLD:%", "Contractor - Service & Repair", 102, _TS_BULK),
    ("gl_account_path_like", "PROFESSIONAL FEES:%",                        "Consulting",                    103, _TS_BULK),
    ("gl_account_path_like", "TECHNOLOGY EXPENSES:%",                      "Information Technology",        104, _TS_BULK),
    ("gl_account_path_like", "CUSTOMER DEPOSITS & DEFERRED REVENUE:%",     "Refunds",                       105, _TS_BULK),
    ("gl_account_path_like", "DIRECT STAFF EXPENSES:%",                    "Contract Labor",                106, _TS_BULK),
    ("gl_account_path_like", "OCCUPANCY EXPENSES:%",                       "Occupancy",                     107, _TS_BULK),
    ("gl_account_path_like", "UTILITY EXPENSES:%",                         "Occupancy",                     108, _TS_BULK),
    ("gl_account_path_like", "FACILITY EXPENSES:%",                        "Occupancy",                     109, _TS_BULK),
    ("gl_account_path_like", "NOTES PAYABLE:%",                            "Notes Payable",                 110, _TS_BULK),
    ("gl_account_path_like", "ACCRUED EXPENSES:%",                         "Notes Payable",                 111, _TS_BULK),
    ("gl_account_path_like", "FIXED ASSETS:%",                             "CAPEX",                         112, _TS_BULK),
    ("gl_account_path_like", "PEOPLE & TEAM DEVELOPMENT EXPENSES:%",       "Other Operating Expenses",      113, _TS_BULK),
    ("gl_account_path_like", "MEALS, TRAVEL, & ENTERTAINMENT EXPENSES:%",  "Other Operating Expenses",      114, _TS_BULK),
    ("gl_account_path_like", "SUPPLIES EXPENSES:%",                        "Other Operating Expenses",      115, _TS_BULK),
    ("gl_account_path_like", "GENERAL ADMINISTRATION EXPENSES:%",          "Other Operating Expenses",      116, _TS_GENERAL_ADMIN),
]


def _gl_rule_supports_path(conn):
    """True if the live gl_rule.match_type CHECK already allows path rules
    (i.e. migration 004 has been applied)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gl_rule'"
    ).fetchone()
    return bool(row and row[0] and _PATH_MATCH_TYPE in row[0])


def seed_gl_rules(conn, verbose=False):
    """Insert any of the 26 codified rules that are missing, keyed on the
    natural key (match_type, match_value). Never overwrites an existing rule.
    Commits. Returns (inserted, skipped) as lists of (match_type, match_value).

    Shared single source of truth: migration 007's migrate() and
    init_db.seed_gl_rules() both call this, so a fresh DB and a migrated DB get
    an identical rule set."""
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
                print(f"   - gl_rule: INSERTED {match_type} {match_value!r} -> "
                      f"{target_category!r} (priority {priority})")
        else:
            skipped.append((match_type, match_value))
            if verbose:
                print(f"   - gl_rule: present, skipped {match_type} {match_value!r}")
    conn.commit()
    return inserted, skipped


def migrate(db_path, verbose=True):
    """Codify the 26 gl_rule rows into the DB at db_path. Returns a dict:
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
    print(f"Codify-GL-rules migration ({len(GL_RULES)} rules) -> {db_path}")
    actions = migrate(db_path)
    if actions["already_migrated"]:
        print(f"All {len(GL_RULES)} rules already present; nothing to do.")
    else:
        print(f"Migration complete: {actions['inserted']} inserted, "
              f"{actions['skipped']} already present. No recompute needed "
              "(fresh DBs categorize on the next sync; the live DB already had "
              "these rules).")
    sys.exit(0)


if __name__ == "__main__":
    main()
