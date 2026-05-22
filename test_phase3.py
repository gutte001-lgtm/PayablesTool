"""
test_phase3.py -- Phase 3 approval-workflow tests (+ a regression guard).

Self-contained: builds a throwaway SQLite DB with fixture users/bills (no
warehouse, per the Phase 1 fixture pattern) and drives the real Flask routes.
Run:  python test_phase3.py

The pure-function regression test (gl_account_name_like) always runs. The
route tests need a SECRET_KEY in .env (same as running the app); if it's
absent they are skipped with a message rather than erroring.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

FAILURES = []
def check(label, cond):
    print(("ok  " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)

# ---- pure regression test (no app/.env needed): the Phase 1b %-wildcard bug --
import sync
_rule = {"id": 7, "match_type": "gl_account_name_like", "match_value": "%COGS%",
         "target_category": "COGS", "priority": 10}
_line = {"gl_account_number": None, "gl_account_name": "Pre-Owned Device COGS",
         "qb_class_name": None, "line_amount_cents": 50000}
_cat, _src, _bd = sync.compute_app_category([_line], [_rule], None, None)
check("regression gl_account_name_like '%COGS%' matches", (_cat, _src) == ("COGS", "gl_rule:7"))
check("regression name_like non-match stays Uncategorized",
      sync.compute_app_category([{"gl_account_number": None, "gl_account_name": "Rent",
                                  "qb_class_name": None, "line_amount_cents": 1}],
                                [_rule], None, None)[0] == "Uncategorized")

# ---- route tests (need .env SECRET_KEY) -------------------------------------
if not dotenv_values(Path(__file__).resolve().parent / ".env").get("SECRET_KEY"):
    print("SKIP route tests: no SECRET_KEY in .env")
    sys.exit(1 if FAILURES else 0)

import db
_tmp = Path(tempfile.mkdtemp()) / "test_phase3.db"
db.DB_PATH = _tmp                       # redirect all connections to the temp DB

import init_db
_c = sqlite3.connect(_tmp)
_c.executescript(init_db.SCHEMA)
PW = generate_password_hash("testpw")
for uname, role in [("tclerk", "ap_clerk"), ("tctrl", "controller"), ("tcfo", "cfo")]:
    _c.execute("INSERT INTO users (username,name,email,role,password_hash,is_active) "
               "VALUES (?,?,?,?,?,1)", (uname, uname, uname + "@x.com", role, PW))
def _seed_bill(bid, state="New", **meta):
    _c.execute("INSERT INTO bill (qb_bill_id,vendor,amount_cents,open_balance_cents,"
               "last_synced_at) VALUES (?,?,?,?,?)", (bid, "Acme", 10000, 10000, "2026-05-22"))
    cols = {"approval_state": state, "created_at": "2026-05-22", "updated_at": "2026-05-22"}
    cols.update(meta)
    keys = ",".join(cols); qs = ",".join("?" * len(cols))
    _c.execute(f"INSERT INTO bill_metadata (qb_bill_id,{keys}) VALUES (?,{qs})",
               (bid, *cols.values()))
_seed_bill("NEW1")                                   # New, no required fields
_seed_bill("AP1", state="AP_Reviewed")               # ready for controller
_seed_bill("AP2", state="AP_Reviewed")               # for reject test
_seed_bill("AP3", state="AP_Reviewed")               # for role test
_c.commit(); _c.close()

from app import app
app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True

def login(c, u):
    c.post("/login", data={"username": u, "password": "testpw"})
def q(sql, args=()):
    cn = sqlite3.connect(_tmp); cn.row_factory = sqlite3.Row
    r = cn.execute(sql, args).fetchall(); cn.close(); return r

clerk = app.test_client(); login(clerk, "tclerk")
ctrl = app.test_client(); login(ctrl, "tctrl")

# 1. required-field gate: New -> AP_Reviewed blocked when fields missing
clerk.post("/bills/NEW1/approve")
st = q("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='NEW1'")[0][0]
check("required-field gate blocks approve (state stays New)", st == "New")

# fill required fields, then approve advances
clerk.post("/bills/NEW1/metadata", data={"classification": "Real", "approver_name": "M",
           "approval_channel": "Pur Board", "approval_date": "2026-05-20"})
clerk.post("/bills/NEW1/approve")
st = q("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='NEW1'")[0][0]
check("happy: New -> AP_Reviewed after required filled", st == "AP_Reviewed")
acts = [r[0] for r in q("SELECT action FROM audit_log WHERE entity_id='NEW1'")]
check("audit has approve_ap_reviewed", "approve_ap_reviewed" in acts)

# 2. controller advances AP_Reviewed -> Controller_Reviewed
ctrl.post("/bills/AP1/approve")
st = q("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='AP1'")[0][0]
check("happy: AP_Reviewed -> Controller_Reviewed (controller)", st == "Controller_Reviewed")
check("audit has approve_controller_reviewed",
      "approve_controller_reviewed" in [r[0] for r in q("SELECT action FROM audit_log WHERE entity_id='AP1'")])

# 3. reject AP_Reviewed -> New with required reason -> Note + audit
ctrl.post("/bills/AP2/reject", data={"reason": "missing receipt"})
st = q("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='AP2'")[0][0]
notes = q("SELECT body FROM note WHERE qb_bill_id='AP2'")
check("reject: AP_Reviewed -> New", st == "New")
check("reject: note created with reason", notes and "missing receipt" in notes[0][0])
check("reject: audit reject_to_new",
      "reject_to_new" in [r[0] for r in q("SELECT action FROM audit_log WHERE entity_id='AP2'")])
# empty reason rejected
ctrl.post("/bills/AP3/reject", data={"reason": "  "})
check("reject requires a reason (state unchanged)",
      q("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='AP3'")[0][0] == "AP_Reviewed")

# 4. role enforcement
r = clerk.post("/bills/AP3/approve")   # ap_clerk has no AP_Reviewed->next
check("ap_clerk cannot advance AP_Reviewed (state unchanged)",
      q("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='AP3'")[0][0] == "AP_Reviewed")
r = clerk.post("/bills/AP3/reject", data={"reason": "x"})
check("ap_clerk cannot reject (403)", r.status_code == 403)

# inbox routes
check("/inbox/controller 200 for controller", ctrl.get("/inbox/controller").status_code == 200)
check("/inbox/controller 403 for ap_clerk", clerk.get("/inbox/controller").status_code == 403)
check("/inbox/cfo 200 (stub)", ctrl.get("/inbox/cfo").status_code == 200)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("ALL PHASE 3 TESTS PASSED")
