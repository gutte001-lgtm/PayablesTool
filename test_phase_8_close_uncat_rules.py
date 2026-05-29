"""
test_phase_8_close_uncat_rules.py -- migration 008 (the 4 rollup rules that close
the Uncategorized bills) + the init_db dual-seed of the full 30-rule set.

Plain-Python style (no pytest): check(label, cond); exit code == failures.
All PURE (sqlite + sync engine, throwaway temp DBs). Live payables.db is never
opened -- the before/after collateral diff over live is a separate one-time
verification run against a backup copy.

Covers:
  * 008 loads its 4 rules on top of 007's 26 (-> 30); idempotent rerun.
  * Natural-key guard never overwrites an existing rule.
  * Fresh-rebuild parity: init_db.seed_gl_rules() (30) == migrations
    005+007+008 (30) == the codified 007+008 set (on the content key).
  * Engine behavior: each new pattern categorizes; the SERVICE AND TRAINING
    PARENT is caught by the new ends-with rule while CHILD paths still route via
    007's rule 12 (different rule, same category) -- proving rule 12 is untouched;
    and the 74960 commission leaf still wins over the new SELLING blanket.
"""
import importlib.util
import shutil
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
import sync        # noqa: E402


def _load(modname, relpath):
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig005 = _load("mig005", "migrations/005_general_admin_rollup.py")
mig007 = _load("mig007", "migrations/007_codify_gl_rules.py")
mig008 = _load("mig008", "migrations/008_close_uncategorized_rules.py")

# Content key: {(match_type, match_value): (target_category, priority, active)}.
EXPECTED_30 = {(mt, mv): (cat, prio, 1)
               for (mt, mv, cat, prio, _ts) in (mig007.GL_RULES + mig008.GL_RULES)}


def _new_path():
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    return d / "t.db"


def _fresh_db():
    p = _new_path()
    cn = sqlite3.connect(p); cn.executescript(init_db.SCHEMA); cn.commit(); cn.close()
    return p


def _rules(p):
    cn = sqlite3.connect(p)
    try:
        rows = cn.execute("SELECT match_type, match_value, target_category, priority, "
                          "active FROM gl_rule").fetchall()
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
check("008 holds 4 rules", len(mig008.GL_RULES) == 4)
check("008 keys unique", len({(mt, mv) for (mt, mv, *_x) in mig008.GL_RULES}) == 4)
check("008 rules are all gl_account_path_like",
      all(mt == "gl_account_path_like" for (mt, *_x) in mig008.GL_RULES))
check("008 priorities are 117-120",
      sorted(p for (*_x, p, _ts) in mig008.GL_RULES) == [117, 118, 119, 120])
check("008 does not collide with any 007 natural key",
      not ({(mt, mv) for (mt, mv, *_x) in mig008.GL_RULES}
           & {(mt, mv) for (mt, mv, *_x) in mig007.GL_RULES}))

# --------------------------------------------------------------------------
print("=" * 60); print("008 loads on top of 007 + idempotency"); print("=" * 60)
p = _fresh_db()
mig007.migrate(p, verbose=False)
check("after 007: 26 rules", _count(p) == 26)
a1 = mig008.migrate(p, verbose=False)
check("008 inserts 4", a1["inserted"] == 4 and a1["skipped"] == 0)
check("after 008: 30 rules", _count(p) == 30)
check("rule set == codified 007+008 set", _rules(p) == EXPECTED_30)
a2 = mig008.migrate(p, verbose=False)
check("008 rerun inserts 0 (idempotent)", a2["inserted"] == 0 and a2["already_migrated"])
check("after rerun: still 30", _count(p) == 30)

# --------------------------------------------------------------------------
print("=" * 60); print("natural-key guard never overwrites"); print("=" * 60)
p = _fresh_db()
cn = sqlite3.connect(p)
cn.execute("INSERT INTO gl_rule (match_type, match_value, target_category, priority, "
           "active, created_by, created_at, updated_at) "
           "VALUES ('gl_account_path_like','SELLING EXPENSES:%','EDITED',9,1,NULL,'x','x')")
cn.commit(); cn.close()
mig008.migrate(p, verbose=False)
rs = _rules(p)
check("pre-existing edited rule not overwritten",
      rs[("gl_account_path_like", "SELLING EXPENSES:%")] == ("EDITED", 9, 1))
check("other 3 still inserted", _count(p) == 4)

# --------------------------------------------------------------------------
print("=" * 60); print("fresh-rebuild parity: init_db == migrations == 30"); print("=" * 60)
# init_db path: SCHEMA + init_db.seed_gl_rules() (007 + 008).
p_init = _fresh_db()
cn = sqlite3.connect(p_init); init_db.seed_gl_rules(cn); cn.close()
# migrations path: SCHEMA + 005 + 007 + 008.
p_mig = _fresh_db()
mig005.migrate(p_mig, verbose=False)
mig007.migrate(p_mig, verbose=False)
mig008.migrate(p_mig, verbose=False)
check("init_db path yields 30 rules", _count(p_init) == 30)
check("migrations path yields 30 rules", _count(p_mig) == 30)
check("init_db == migrations == codified set (match_type,match_value,target,priority,active)",
      _rules(p_init) == _rules(p_mig) == EXPECTED_30)

# --------------------------------------------------------------------------
print("=" * 60); print("engine behavior (rule 12 untouched; leaf wins)"); print("=" * 60)
p = _fresh_db()
cn = sqlite3.connect(p); cn.row_factory = sqlite3.Row
init_db.seed_gl_rules(cn); cn.commit()
rules = sync._load_rules(cn)
cn.close()


def categorize(path=None, num=None, name=""):
    line = {"line_amount_cents": 10000, "gl_account_name": name,
            "gl_account_number": num, "gl_account_path": path, "qb_class_name": None}
    cat, src, _ = sync.compute_app_category([line], rules, None, None)
    return cat, src


PARENT = "COST OF GOODS SOLD:SERVICE AND TRAINING COST OF GOODS SOLD"
CHILD = "COST OF GOODS SOLD:SERVICE AND TRAINING COST OF GOODS SOLD:Service and Repair COGS"

pc, ps = categorize(path=PARENT)
cc, cs = categorize(path=CHILD)
check("S&T parent -> Contractor - Service & Repair", pc == "Contractor - Service & Repair")
check("S&T child  -> Contractor - Service & Repair", cc == "Contractor - Service & Repair")
check("parent and child matched by DIFFERENT rules (rule 12 untouched)", ps != cs)

check("SALES & PAYROLL TAX -> Other Operating Expenses",
      categorize(path="SALES & PAYROLL TAX LIABILITIES:SALES TAX LIABILITY:Sales Tax Liability - Georgia")[0]
      == "Other Operating Expenses")
check("SELLING:Customer Acquisition -> Other Operating Expenses",
      categorize(path="SELLING EXPENSES:Customer Acquisition", num="74940")[0]
      == "Other Operating Expenses")
check("BRAND & MARKETING -> Other Operating Expenses",
      categorize(path="BRAND & MARKETING EXPENSES:Promotional Products", num="74640")[0]
      == "Other Operating Expenses")
check("74960 commission LEAF still wins over the new SELLING blanket",
      categorize(path="SELLING EXPENSES:Outside Sales Commissions", num="74960")[0]
      == "Contractor - Outside Sales Commissions")

# --------------------------------------------------------------------------
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(len(FAILURES))
print("ALL PASS")
sys.exit(0)
