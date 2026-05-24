"""
test_phase_5_rules_engine.py -- Phase 5 rollup rules-engine tests.

Plain-Python style (no pytest), matching test_phase_4.py: check(label, cond);
exit code == number of failures; run with `python test_phase_5_rules_engine.py`.

Pure + sqlite only (no Flask app, no warehouse, no .env needed). Every DB is a
fresh temp file; the live payables.db is never opened.

Covers: migration idempotency (legacy upgrade / fresh no-op / re-run no-op),
the widened CHECK, gl_account_path_like semantics in _line_matches, rollup vs
leaf precedence through compute_app_category, an end-to-end recompute, and
backward-compat of the four existing match types.
"""
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

FAILURES = []
_TMP = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


ROOT = Path(__file__).resolve().parent
import sync       # noqa: E402
import init_db    # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mig004", ROOT / "migrations" / "004_phase_5_rules_engine.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

# ---- pre-Phase-5 ("legacy") schema: no canonical/path cols, narrow CHECK ----
LEGACY = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, name TEXT,
    role TEXT, password_hash TEXT, is_active INTEGER DEFAULT 1);
CREATE TABLE bill (qb_bill_id TEXT PRIMARY KEY, vendor_ref TEXT, last_synced_at TEXT);
CREATE TABLE bill_line (
    qb_bill_id TEXT NOT NULL REFERENCES bill(qb_bill_id),
    line_number INTEGER NOT NULL,
    gl_account_id TEXT, gl_account_name TEXT, gl_account_number TEXT,
    qb_class_name TEXT, line_amount_cents INTEGER,
    PRIMARY KEY (qb_bill_id, line_number));
CREATE TABLE gl_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL CHECK (match_type IN
        ('gl_account_number','gl_account_name_like','class_name','gl_and_class')),
    match_value TEXT NOT NULL, target_category TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100, active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_glrule_active_priority ON gl_rule(active, priority);
"""

_RULE_INS = ("INSERT INTO gl_rule (match_type,match_value,target_category,priority,"
             "active,created_at,updated_at) VALUES (?,?,?,?,1,'t','t')")


def _new_path():
    d = Path(tempfile.mkdtemp()); _TMP.append(d); return d / "t.db"


def legacy_path():
    p = _new_path(); cn = sqlite3.connect(p)
    cn.executescript(LEGACY)
    cn.execute(_RULE_INS, ("gl_account_number", "72510", "Legal Fees", 10))
    cn.commit(); cn.close(); return p


def fresh_path():
    p = _new_path(); cn = sqlite3.connect(p)
    cn.executescript(init_db.SCHEMA)
    cn.commit(); cn.close(); return p


def _cols(cn, t):
    return [r[1] for r in cn.execute(f"PRAGMA table_info({t})")]


def L(**kw):
    return kw


def R(mt, mv):
    return {"match_type": mt, "match_value": mv}


# ====================================================================
print("=" * 60); print("test_migration_upgrades_legacy"); print("=" * 60)
p = legacy_path()
a = mig.migrate(p, verbose=False)
check("mig: legacy not flagged already_migrated", a["already_migrated"] is False)
cn = sqlite3.connect(p)
cols = _cols(cn, "bill_line")
check("mig: gl_account_number_canonical added", "gl_account_number_canonical" in cols)
check("mig: gl_account_path added", "gl_account_path" in cols)
sqltxt = cn.execute("SELECT sql FROM sqlite_master WHERE name='gl_rule'").fetchone()[0]
check("mig: CHECK now allows gl_account_path_like", "gl_account_path_like" in sqltxt)
check("mig: existing rule row preserved",
      cn.execute("SELECT COUNT(*) FROM gl_rule").fetchone()[0] == 1)
check("mig: preserved rule still resolvable",
      cn.execute("SELECT target_category FROM gl_rule WHERE match_value='72510'")
        .fetchone()[0] == "Legal Fees")
check("mig: index preserved",
      any(r[1] == "idx_glrule_active_priority"
          for r in cn.execute("PRAGMA index_list(gl_rule)")))
# a path_like rule now inserts cleanly
cn.execute(_RULE_INS, ("gl_account_path_like", "FIXED ASSETS:%", "CAPEX", 112))
cn.commit()
check("mig: path_like rule accepted after upgrade",
      cn.execute("SELECT COUNT(*) FROM gl_rule").fetchone()[0] == 2)
cn.close()

print("=" * 60); print("test_migration_idempotent"); print("=" * 60)
a2 = mig.migrate(p, verbose=False)
check("mig: second run flagged already_migrated", a2["already_migrated"] is True)
cn = sqlite3.connect(p)
check("mig: rule rows intact after rerun (no clobber)",
      cn.execute("SELECT COUNT(*) FROM gl_rule").fetchone()[0] == 2)
cn.close()

print("=" * 60); print("test_migration_noop_on_fresh_initdb"); print("=" * 60)
fp = fresh_path()
a3 = mig.migrate(fp, verbose=False)
check("mig: fresh init_db DB already fully migrated", a3["already_migrated"] is True)

print("=" * 60); print("test_check_constraint"); print("=" * 60)
cn = sqlite3.connect(fresh_path())
cn.execute(_RULE_INS, ("gl_account_path_like", "%PRODUCT COST OF GOODS SOLD:%", "Parts & Products", 100))
check("check: gl_account_path_like accepted on fresh init_db DB", True)
try:
    cn.execute(_RULE_INS, ("not_a_real_type", "x", "c", 1))
    check("check: garbage match_type rejected", False)
except sqlite3.IntegrityError:
    check("check: garbage match_type rejected", True)
cn.close()

print("=" * 60); print("test_line_matches_path_semantics"); print("=" * 60)
PATH = "COST OF GOODS SOLD:PRODUCT COST OF GOODS SOLD:Service Parts COGS"
check("path: case-insensitive match",
      sync._line_matches(L(gl_account_path=PATH),
                         R("gl_account_path_like", "%product cost of goods sold:%")) is True)
check("path: anchored prefix matches",
      sync._line_matches(L(gl_account_path="OUTBOUND SHIPPING COST OF GOODS SOLD:Outbound Shipping COGS"),
                         R("gl_account_path_like", "OUTBOUND%")) is True)
check("path: ^$-anchored, mid-string pattern does NOT match",
      sync._line_matches(L(gl_account_path="OUTBOUND SHIPPING COST OF GOODS SOLD:Outbound Shipping COGS"),
                         R("gl_account_path_like", "SHIPPING%")) is False)
check("path: NULL path -> False",
      sync._line_matches(L(gl_account_path=None), R("gl_account_path_like", "%x%")) is False)
check("path: empty match_value -> False",
      sync._line_matches(L(gl_account_path=PATH), R("gl_account_path_like", "")) is False)

print("=" * 60); print("test_precedence_leaf_over_rollup"); print("=" * 60)
line = L(gl_account_name="Service Parts COGS", gl_account_path=PATH,
         line_amount_cents=1000, gl_account_number=None, qb_class_name=None)
rules = [
    {"id": 1, "match_type": "gl_account_name_like", "match_value": "%Service Parts COGS",
     "target_category": "LEAF-WINS", "priority": 10},
    {"id": 2, "match_type": "gl_account_path_like", "match_value": "%PRODUCT COST OF GOODS SOLD:%",
     "target_category": "Parts & Products", "priority": 100},
]
cat, src, _bd = sync.compute_app_category([line], rules, None, None)
check("precedence: lower-priority leaf wins over rollup that also matches", cat == "LEAF-WINS")
check("precedence: source records the winning rule id", src == "gl_rule:1")

print("=" * 60); print("test_end_to_end_recompute"); print("=" * 60)
ep = fresh_path()
cn = sqlite3.connect(ep); cn.row_factory = sqlite3.Row
cn.execute("INSERT INTO users (username,name,role,is_active) VALUES ('joe','Joe','controller',1)")
cn.execute("INSERT INTO bill (qb_bill_id,vendor_ref,amount_cents,open_balance_cents,last_synced_at) "
           "VALUES ('B1','V1',10000,10000,'t')")
cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,created_at,updated_at) "
           "VALUES ('B1','New','t','t')")
cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,gl_account_path,line_amount_cents) "
           "VALUES ('B1',1,'Consumable COGS','COST OF GOODS SOLD:PRODUCT COST OF GOODS SOLD:Consumable COGS',1000)")
cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,gl_account_path,line_amount_cents) "
           "VALUES ('B1',2,'New Device COGS','COST OF GOODS SOLD:DEVICE COST OF GOODS SOLD:New Device COGS',9000)")
cn.execute(_RULE_INS, ("gl_account_path_like", "%PRODUCT COST OF GOODS SOLD:%", "Parts & Products", 100))
cn.execute(_RULE_INS, ("gl_account_name_like", "%New Device COGS", "New Device Purchases", 17))
cn.commit()
sync.recompute_for_bill(cn, "B1")
cn.commit()
row = cn.execute("SELECT app_category FROM bill_metadata WHERE qb_bill_id='B1'").fetchone()
check("e2e: dominant (9000) new-device line -> New Device Purchases (path rule loses on amount)",
      row["app_category"] == "New Device Purchases")
bd = cn.execute("SELECT app_category_breakdown FROM bill_metadata WHERE qb_bill_id='B1'").fetchone()[0]
check("e2e: breakdown captures both lines (Parts + New Device)",
      "Parts & Products" in bd and "New Device Purchases" in bd)
cn.close()

print("=" * 60); print("test_backward_compat_existing_match_types"); print("=" * 60)
check("compat: gl_account_number exact match",
      sync._line_matches(L(gl_account_number="72510"), R("gl_account_number", "72510")) is True)
check("compat: gl_account_number non-match",
      sync._line_matches(L(gl_account_number="999"), R("gl_account_number", "72510")) is False)
check("compat: gl_account_name_like",
      sync._line_matches(L(gl_account_name="Legal Fees"), R("gl_account_name_like", "%Legal%")) is True)
check("compat: class_name case-insensitive exact",
      sync._line_matches(L(qb_class_name="D400 - Finance"), R("class_name", "d400 - finance")) is True)
check("compat: gl_and_class",
      sync._line_matches(L(gl_account_number="53100", qb_class_name="X"),
                         R("gl_and_class", "53100||X")) is True)

# ====================================================================
for d in _TMP:
    shutil.rmtree(d, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
else:
    print("ALL PHASE 5 RULES-ENGINE CHECKS PASSED")
sys.exit(len(FAILURES))
