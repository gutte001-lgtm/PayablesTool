"""
test_phase_7_codify_rules.py -- tests for migrations/007_codify_gl_rules.py and
init_db.seed_gl_rules().

Plain-Python style (no pytest), matching test_phase_4_5.py: check(label, cond);
exit code == number of failures; run with `python test_phase_7_codify_rules.py`.

All PURE (sqlite only, throwaway temp DBs). The live payables.db is NEVER opened
-- the fresh-rebuild-vs-live diff is a separate one-time verification run against
a backup copy, not part of this committed test.

Covers:
  * 007 loads all 26 rules into an empty gl_rule.
  * Idempotency: a second run is a no-op (already_migrated, no dupes, count stays 26).
  * Natural-key guard never overwrites an existing (match_type, match_value) row.
  * 005 + 007 compose with zero duplication in either order (GENERAL ADMIN once).
  * init_db.seed_gl_rules() and the migration produce an IDENTICAL rule set.
  * init_db.SCHEMA + 005 + 007 yields exactly the 26 codified rows.
  * Missing-004 schema (no gl_account_path_like) stops with a clear error.
"""
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

FAILURES = []
_TMPDIRS = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import init_db     # noqa: E402


def _load(modname, relpath):
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig005 = _load("mig005", "migrations/005_general_admin_rollup.py")
mig007 = _load("mig007", "migrations/007_codify_gl_rules.py")

# The codified set, keyed for comparison: {(match_type, match_value):
# (target_category, priority, active)}.
EXPECTED = {
    (mt, mv): (cat, prio, 1)
    for (mt, mv, cat, prio, _ts) in mig007.GL_RULES
}


def _new_path(name="t.db"):
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    return d / name


def _fresh_db():
    """Throwaway DB with the full base schema (empty gl_rule, like init_db.py)."""
    p = _new_path()
    cn = sqlite3.connect(p)
    cn.executescript(init_db.SCHEMA)
    cn.commit()
    cn.close()
    return p


def _rules(p):
    """Rule set as {(match_type, match_value): (target_category, priority, active)}."""
    cn = sqlite3.connect(p)
    try:
        rows = cn.execute(
            "SELECT match_type, match_value, target_category, priority, active "
            "FROM gl_rule").fetchall()
    finally:
        cn.close()
    return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}


def _count(p):
    cn = sqlite3.connect(p)
    try:
        return cn.execute("SELECT COUNT(*) FROM gl_rule").fetchone()[0]
    finally:
        cn.close()


# --------------------------------------------------------------------------
print("=" * 60); print("data sanity"); print("=" * 60)

check("GL_RULES holds exactly 26 rules", len(mig007.GL_RULES) == 26)
check("GL_RULES natural keys are unique (no dup match_type+match_value)",
      len({(mt, mv) for (mt, mv, *_x) in mig007.GL_RULES}) == 26)
check("every match_type is in the gl_rule CHECK vocabulary",
      all(mt in ("gl_account_number", "gl_account_name_like", "class_name",
                 "gl_and_class", "gl_account_path_like")
          for (mt, *_x) in mig007.GL_RULES))

# --------------------------------------------------------------------------
print("=" * 60); print("007 loads + idempotency"); print("=" * 60)

p = _fresh_db()
a1 = mig007.migrate(p, verbose=False)
check("007 first run inserts 26", a1["inserted"] == 26 and a1["skipped"] == 0)
check("007 first run not flagged already_migrated", a1["already_migrated"] is False)
check("count is 26 after first run", _count(p) == 26)
check("loaded rule set matches the codified set exactly", _rules(p) == EXPECTED)

a2 = mig007.migrate(p, verbose=False)
check("007 second run inserts 0 (no dupes)", a2["inserted"] == 0)
check("007 second run flagged already_migrated", a2["already_migrated"] is True)
check("count still 26 after rerun", _count(p) == 26)

# --------------------------------------------------------------------------
print("=" * 60); print("natural-key guard never overwrites"); print("=" * 60)

p = _fresh_db()
cn = sqlite3.connect(p)
# Pre-seed one rule with the SAME natural key but a DIFFERENT category/priority,
# as if a controller had edited it via /admin/rules.
cn.execute(
    "INSERT INTO gl_rule (match_type, match_value, target_category, priority, "
    "active, created_by, created_at, updated_at) "
    "VALUES ('gl_account_number','72510','EDITED CATEGORY',5,1,NULL,'x','x')")
cn.commit(); cn.close()
mig007.migrate(p, verbose=False)
rs = _rules(p)
check("pre-existing edited rule is NOT overwritten",
      rs[("gl_account_number", "72510")] == ("EDITED CATEGORY", 5, 1))
check("the other 25 are still inserted (count 26)", _count(p) == 26)

# --------------------------------------------------------------------------
print("=" * 60); print("005 + 007 compose, either order"); print("=" * 60)

# Order A: 005 then 007 (the fresh-rebuild order).
p = _fresh_db()
mig005.migrate(p, verbose=False)
check("after 005 alone: 1 rule (GENERAL ADMIN)", _count(p) == 1)
a = mig007.migrate(p, verbose=False)
check("007 after 005 inserts the other 25", a["inserted"] == 25 and a["skipped"] == 1)
check("005->007 yields exactly the 26 codified rules", _rules(p) == EXPECTED)
gen = [k for k in _rules(p) if k[1] == "GENERAL ADMINISTRATION EXPENSES:%"]
check("GENERAL ADMINISTRATION rule appears exactly once", len(gen) == 1)

# Order B: 007 then 005.
p = _fresh_db()
mig007.migrate(p, verbose=False)
a = mig005.migrate(p, verbose=False)
check("005 after 007 is a no-op (already present)", a["already_migrated"] is True)
check("007->005 still exactly the 26 codified rules", _rules(p) == EXPECTED)
check("007->005 count is 26 (no GENERAL ADMIN dup)", _count(p) == 26)

# --------------------------------------------------------------------------
print("=" * 60); print("init_db.seed_gl_rules == migration"); print("=" * 60)

# DB 1: rules loaded via init_db.seed_gl_rules().
p_init = _fresh_db()
cn = sqlite3.connect(p_init)
created = init_db.seed_gl_rules(cn)
cn.close()
check("init_db.seed_gl_rules() inserts 26", len(created) == 26)

# DB 2: rules loaded via the migration.
p_mig = _fresh_db()
mig007.migrate(p_mig, verbose=False)

check("init_db path and migration path produce IDENTICAL rule sets",
      _rules(p_init) == _rules(p_mig) == EXPECTED)

# init_db.seed_gl_rules() is itself idempotent.
cn = sqlite3.connect(p_init)
created2 = init_db.seed_gl_rules(cn)
cn.close()
check("init_db.seed_gl_rules() is idempotent (0 on rerun)", len(created2) == 0)
check("init_db DB still 26 after rerun", _count(p_init) == 26)

# --------------------------------------------------------------------------
print("=" * 60); print("clean-rebuild simulation"); print("=" * 60)

# Simulate a from-scratch rebuild's rule outcome: base schema + 005 + 007.
p = _fresh_db()
mig005.migrate(p, verbose=False)
mig007.migrate(p, verbose=False)
check("rebuild (schema+005+007): exactly 26 rules", _count(p) == 26)
check("rebuild rule set equals the codified set on "
      "(match_type,match_value,target_category,priority,active)",
      _rules(p) == EXPECTED)

# --------------------------------------------------------------------------
print("=" * 60); print("guard: missing migration 004 schema"); print("=" * 60)

# A gl_rule table whose CHECK predates 004 (no gl_account_path_like).
p = _new_path()
cn = sqlite3.connect(p)
cn.executescript("""
CREATE TABLE gl_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL CHECK (match_type IN
        ('gl_account_number','gl_account_name_like','class_name','gl_and_class')),
    match_value TEXT NOT NULL, target_category TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100, active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
""")
cn.commit(); cn.close()
stopped = False
try:
    mig007.migrate(p, verbose=False)
except SystemExit:
    stopped = True
check("007 stops cleanly when gl_rule predates migration 004", stopped)
check("nothing inserted into a pre-004 gl_rule", _count(p) == 0)

# --------------------------------------------------------------------------
import shutil
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(len(FAILURES))
print("ALL PASS")
sys.exit(0)
