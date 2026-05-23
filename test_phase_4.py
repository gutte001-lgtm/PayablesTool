"""
test_phase_4.py -- Phase 4 pay-run builder tests.

Plain-Python style (no pytest), matching test_phase_3_6.py: check(label, cond);
exit code == number of failures; run with `python test_phase_4.py`.

DB safety: every route test seeds a FRESH temp DB and points db.DB_PATH at it,
so the live payables.db is NEVER opened. Migration tests use throwaway DBs.
Needs SECRET_KEY in .env.
"""
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

FAILURES = []
_TMPDIRS = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


if not dotenv_values(Path(__file__).resolve().parent / ".env").get("SECRET_KEY"):
    print("CANNOT RUN: no SECRET_KEY in .env")
    sys.exit(1)

import db          # noqa: E402
import init_db     # noqa: E402
import payruns     # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mig003", Path(__file__).resolve().parent / "migrations" / "003_phase_4.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

PW = generate_password_hash("testpw")
USERS = [("marilyn", "Marilyn", "ap_clerk"), ("joe", "Joe", "controller"),
         ("shaun", "Shaun", "cfo")]


def fresh_db():
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    db.DB_PATH = d / "p4.db"
    cn = sqlite3.connect(db.DB_PATH)
    cn.executescript(init_db.SCHEMA)
    for u, name, role in USERS:
        cn.execute("INSERT INTO users (username,name,role,password_hash,is_active) "
                   "VALUES (?,?,?,?,1)", (u, name, role, PW))
    cn.commit()
    cn.close()


from app import app  # noqa: E402
app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True


def client(username):
    c = app.test_client()
    c.post("/login", data={"username": username, "password": "testpw"})
    return c


def _conn():
    cn = sqlite3.connect(db.DB_PATH)
    cn.row_factory = sqlite3.Row
    return cn


def uid(username):
    cn = _conn()
    r = cn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    cn.close()
    return r["id"]


def seed_bill(bid, state="Controller_Reviewed", classification=None,
              open_balance=10000, proposed_method=None):
    cn = _conn()
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
               "open_balance_cents,bill_date,due_date,is_paid,last_synced_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, "Acme " + bid, "B-" + bid, open_balance, open_balance,
                "2026-05-01", "2026-05-15", 0 if open_balance > 0 else 1, "2026-05-22"))
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,classification,"
               "proposed_payment_method,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               (bid, state, classification, proposed_method, "2026-05-01", "2026-05-01"))
    cn.commit()
    cn.close()


def seed_line(bid, gl_account_name):
    cn = _conn()
    cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,"
               "line_amount_cents) VALUES (?,?,?,?)", (bid, 1, gl_account_name, 10000))
    cn.commit()
    cn.close()


def seed_run(name="Run", status="Draft", created_by="joe"):
    cn = _conn()
    cur = cn.execute("INSERT INTO pay_run (name,week_ending,created_by,status,created_at,"
                     "updated_at) VALUES (?,?,?,?,?,?)",
                     (name, "2026-05-22", uid(created_by), status, "2026-05-22", "2026-05-22"))
    cn.commit()
    rid = cur.lastrowid
    cn.close()
    return rid


def seed_payline(run_id, bid, method="Check", amount=10000, included=1, line_state="Pending"):
    cn = _conn()
    cur = cn.execute("INSERT INTO pay_run_line (pay_run_id,qb_bill_id,payment_method,"
                     "amount_to_pay_cents,included,line_state) VALUES (?,?,?,?,?,?)",
                     (run_id, bid, method, amount, included, line_state))
    cn.commit()
    lid = cur.lastrowid
    cn.close()
    return lid


def scalar(sql, args=()):
    cn = _conn()
    r = cn.execute(sql, args).fetchone()
    cn.close()
    return r[0] if r else None


def audit_actions(entity_id):
    cn = _conn()
    rows = cn.execute("SELECT action FROM audit_log WHERE entity_id=? ORDER BY id",
                      (str(entity_id),)).fetchall()
    cn.close()
    return [r["action"] for r in rows]


# ======================================================================
# Migration
# ======================================================================

def _cols(p, table):
    cn = sqlite3.connect(p)
    out = [r[1] for r in cn.execute(f"PRAGMA table_info({table})")]
    cn.close()
    return out


def _idx(p, table):
    cn = sqlite3.connect(p)
    out = [r[1] for r in cn.execute(f"PRAGMA index_list({table})")]
    cn.close()
    return out


def legacy_db():
    """Pay_run_line WITHOUT the Phase 4 review columns -> simulates pre-Phase-4."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    p = d / "legacy4.db"
    cn = sqlite3.connect(p)
    cn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
        CREATE TABLE bill (qb_bill_id TEXT PRIMARY KEY);
        CREATE TABLE pay_run (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);
        CREATE TABLE pay_run_line (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pay_run_id INTEGER, qb_bill_id TEXT,
            payment_method TEXT, amount_to_pay_cents INTEGER, included INTEGER DEFAULT 1,
            line_state TEXT DEFAULT 'Pending', cfo_note TEXT);
    """)
    cn.commit()
    cn.close()
    return p


def test_migration_adds_columns():
    p = legacy_db()
    check("mig: legacy lacks reviewed_by_user_id", "reviewed_by_user_id" not in _cols(p, "pay_run_line"))
    a = mig.migrate(p, verbose=False)
    check("mig: reviewed_by_user_id added", a["reviewed_by_user_id"] == "added")
    check("mig: reviewed_at added", a["reviewed_at"] == "added")
    check("mig: columns present after run",
          "reviewed_by_user_id" in _cols(p, "pay_run_line") and "reviewed_at" in _cols(p, "pay_run_line"))
    check("mig: unique index added", a["unique_index"] == "added")
    check("mig: unique index present after run", "idx_payrunline_unique" in _idx(p, "pay_run_line"))
    check("mig: not already_migrated on first run", a["already_migrated"] is False)


def test_migration_idempotent():
    p = legacy_db()
    mig.migrate(p, verbose=False)
    a2 = mig.migrate(p, verbose=False)
    check("mig: rerun already present", a2["reviewed_by_user_id"] == "already present")
    check("mig: rerun unique index already present", a2["unique_index"] == "already present")
    check("mig: rerun flagged already_migrated", a2["already_migrated"] is True)


def test_migration_noop_on_fresh():
    fresh_db()
    a = mig.migrate(db.DB_PATH, verbose=False)
    check("mig: fresh init_db DB already migrated", a["already_migrated"] is True)


# ======================================================================
# Create
# ======================================================================

def test_create_run():
    fresh_db()
    r = client("marilyn").post("/pay-runs", data={"name": "May wk4", "week_ending": "2026-05-29"})
    check("create: 302 PRG", r.status_code == 302)
    check("create: run row Draft", scalar("SELECT status FROM pay_run WHERE name='May wk4'") == "Draft")
    rid = scalar("SELECT id FROM pay_run WHERE name='May wk4'")
    check("create: audit pay_run_created", "pay_run_created" in audit_actions(rid))


def test_create_run_cfo_forbidden():
    fresh_db()
    r = client("shaun").post("/pay-runs", data={"name": "x"})
    check("create CFO: 403", r.status_code == 403)


# ======================================================================
# Candidates + add lines
# ======================================================================

def test_candidate_pool_filters():
    fresh_db()
    seed_bill("OK1", state="Controller_Reviewed")
    seed_bill("NEW1", state="New")                          # not reviewed -> excluded
    seed_bill("AP1", state="AP_Reviewed")                   # not reviewed -> excluded
    seed_bill("PAID1", state="Controller_Reviewed", open_balance=0)  # paid -> excluded
    seed_bill("REF1", state="Controller_Reviewed", classification="Refund-Visibility")
    seed_bill("PRE1", state="Controller_Reviewed", classification="Prepayment-Deposit")
    rid = seed_run()
    cands = {c["qb_bill_id"] for c in payruns.candidate_bills(_conn(), rid)}
    check("candidates: includes Controller_Reviewed+open", "OK1" in cands)
    check("candidates: excludes New", "NEW1" not in cands)
    check("candidates: excludes AP_Reviewed", "AP1" not in cands)
    check("candidates: excludes paid", "PAID1" not in cands)
    check("candidates: excludes Refund-Visibility", "REF1" not in cands)
    check("candidates: excludes Prepayment-Deposit", "PRE1" not in cands)


def test_candidate_flags():
    fresh_db()
    seed_bill("FLAG1", state="Controller_Reviewed")
    seed_line("FLAG1", "Training COGS")                     # contractor
    rid = seed_run()
    c = [x for x in payruns.candidate_bills(_conn(), rid) if x["qb_bill_id"] == "FLAG1"][0]
    check("candidates: contractor flag set", c["is_contractor"] is True)


def test_add_lines():
    fresh_db()
    seed_bill("A1", state="Controller_Reviewed", open_balance=25000, proposed_method="Check")
    rid = seed_run()
    client("marilyn").post(f"/pay-runs/{rid}/lines", data={"bill_ids": "A1"})
    check("add: line created", scalar("SELECT COUNT(*) FROM pay_run_line WHERE pay_run_id=? AND qb_bill_id='A1'", (rid,)) == 1)
    check("add: amount defaults to open balance", scalar("SELECT amount_to_pay_cents FROM pay_run_line WHERE qb_bill_id='A1'") == 25000)
    check("add: payment_method default from proposed", scalar("SELECT payment_method FROM pay_run_line WHERE qb_bill_id='A1'") == "Check")
    check("add: audit pay_run_lines_added", "pay_run_lines_added" in audit_actions(rid))


def test_add_lines_not_draft():
    fresh_db()
    seed_bill("A2", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_Controller")
    r = client("marilyn").post(f"/pay-runs/{rid}/lines", data={"bill_ids": "A2"})
    check("add not-draft: 302+flash", r.status_code == 302)
    check("add not-draft: no line", scalar("SELECT COUNT(*) FROM pay_run_line WHERE pay_run_id=?", (rid,)) == 0)


def test_claimed_bill_excluded():
    fresh_db()
    seed_bill("C1", state="Controller_Reviewed")
    r1 = seed_run(name="R1")
    seed_payline(r1, "C1", included=1, line_state="Pending")   # claimed by R1
    r2 = seed_run(name="R2")
    cands = {c["qb_bill_id"] for c in payruns.candidate_bills(_conn(), r2)}
    check("claimed: bill on another run is hidden", "C1" not in cands)


def test_no_duplicate_line_same_run():
    fresh_db()
    seed_bill("DUP1", state="Controller_Reviewed")
    rid = seed_run()
    m = client("marilyn")
    m.post(f"/pay-runs/{rid}/lines", data={"bill_ids": "DUP1"})
    m.post(f"/pay-runs/{rid}/lines", data={"bill_ids": "DUP1"})  # second add is a no-op
    check("dup: bill appears at most once per run",
          scalar("SELECT COUNT(*) FROM pay_run_line WHERE pay_run_id=? AND qb_bill_id='DUP1'", (rid,)) == 1)


# ======================================================================
# Edit line
# ======================================================================

def test_edit_line():
    fresh_db()
    seed_bill("E1", state="Controller_Reviewed", open_balance=10000)
    rid = seed_run()
    lid = seed_payline(rid, "E1", method=None, amount=10000)
    client("marilyn").post(f"/pay-runs/{rid}/lines/{lid}",
                           data={"payment_method": "Wire", "amount_to_pay": "75.50", "included": "1"})
    check("edit: method set", scalar("SELECT payment_method FROM pay_run_line WHERE id=?", (lid,)) == "Wire")
    check("edit: amount locked to full open balance (partials deferred)",
          scalar("SELECT amount_to_pay_cents FROM pay_run_line WHERE id=?", (lid,)) == 10000)


def test_amount_locked_to_open_balance():
    fresh_db()
    seed_bill("E2", state="Controller_Reviewed", open_balance=10000)
    rid = seed_run()
    lid = seed_payline(rid, "E2", amount=10000)
    # v1 pays the full open balance; a posted partial/over amount is ignored.
    r = client("marilyn").post(f"/pay-runs/{rid}/lines/{lid}",
                               data={"payment_method": "Check", "amount_to_pay": "200", "included": "1"})
    check("amount locked: 302 PRG", r.status_code == 302)
    check("amount locked: stays full open balance",
          scalar("SELECT amount_to_pay_cents FROM pay_run_line WHERE id=?", (lid,)) == 10000)


def test_exclude_line_frees_bill():
    fresh_db()
    seed_bill("E3", state="Controller_Reviewed")
    rid = seed_run()
    lid = seed_payline(rid, "E3", included=1)
    # exclude it (include checkbox absent)
    client("marilyn").post(f"/pay-runs/{rid}/lines/{lid}",
                           data={"payment_method": "Check", "amount_to_pay": "100.00"})
    check("exclude: included=0", scalar("SELECT included FROM pay_run_line WHERE id=?", (lid,)) == 0)
    rid2 = seed_run(name="R2")
    cands = {c["qb_bill_id"] for c in payruns.candidate_bills(_conn(), rid2)}
    check("exclude: bill freed back to pool (push to next week)", "E3" in cands)


# ======================================================================
# Lifecycle
# ======================================================================

def test_full_lifecycle():
    fresh_db()
    seed_bill("L1", state="Controller_Reviewed")
    rid = seed_run()
    seed_payline(rid, "L1", method="Check", included=1)
    m, j, s = client("marilyn"), client("joe"), client("shaun")
    m.post(f"/pay-runs/{rid}/advance", data={"action": "submit_controller"})
    check("life: -> Submitted_to_Controller", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Submitted_to_Controller")
    j.post(f"/pay-runs/{rid}/advance", data={"action": "approve_controller"})
    check("life: -> Controller_Approved", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Controller_Approved")
    j.post(f"/pay-runs/{rid}/advance", data={"action": "submit_cfo"})
    check("life: -> Submitted_to_CFO", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Submitted_to_CFO")
    s.post(f"/pay-runs/{rid}/advance", data={"action": "approve_cfo"})
    check("life: -> CFO_Approved", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "CFO_Approved")
    j.post(f"/pay-runs/{rid}/advance", data={"action": "lock"})
    check("life: -> Locked", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Locked")
    check("life: audit pay_run_advanced logged", audit_actions(rid).count("pay_run_advanced") == 5)


def test_submit_empty_run_blocked():
    fresh_db()
    rid = seed_run()                                        # no lines
    r = client("marilyn").post(f"/pay-runs/{rid}/advance", data={"action": "submit_controller"})
    check("empty submit: 302+flash", r.status_code == 302)
    check("empty submit: stays Draft", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Draft")


def test_transition_wrong_role():
    fresh_db()
    rid = seed_run(status="Submitted_to_Controller")
    r = client("marilyn").post(f"/pay-runs/{rid}/advance", data={"action": "approve_controller"})
    check("wrong role: ap_clerk approve_controller -> 403", r.status_code == 403)
    check("wrong role: status unchanged", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Submitted_to_Controller")


def test_transition_wrong_state():
    fresh_db()
    rid = seed_run(status="Draft")
    r = client("shaun").post(f"/pay-runs/{rid}/advance", data={"action": "approve_cfo"})
    check("wrong state: approve_cfo from Draft -> 302+flash", r.status_code == 302)
    check("wrong state: status unchanged", scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Draft")


def test_reject_all_then_block_advance():
    # Reject the only line at the controller stage, approve the (now empty) run,
    # then try to push it to the CFO -> blocked (payable_count == 0).
    fresh_db()
    seed_bill("RA1", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_Controller")
    lid = seed_payline(rid, "RA1")
    j = client("joe")
    j.post(f"/pay-runs/{rid}/lines/{lid}/review", data={"action": "reject", "note": "drop it"})
    j.post(f"/pay-runs/{rid}/advance", data={"action": "approve_controller"})
    check("reject-all: -> Controller_Approved",
          scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Controller_Approved")
    r = j.post(f"/pay-runs/{rid}/advance", data={"action": "submit_cfo"})
    check("reject-all: empty run blocked from CFO (302+flash)", r.status_code == 302)
    check("reject-all: stays Controller_Approved",
          scalar("SELECT status FROM pay_run WHERE id=?", (rid,)) == "Controller_Approved")


# ======================================================================
# Line review
# ======================================================================

def test_controller_rejects_line():
    fresh_db()
    seed_bill("RJ1", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_Controller")
    lid = seed_payline(rid, "RJ1")
    client("joe").post(f"/pay-runs/{rid}/lines/{lid}/review",
                       data={"action": "reject", "note": "wrong amount"})
    check("reject: line_state Rejected", scalar("SELECT line_state FROM pay_run_line WHERE id=?", (lid,)) == "Rejected")
    check("reject: note stored", scalar("SELECT cfo_note FROM pay_run_line WHERE id=?", (lid,)) == "wrong amount")
    check("reject: reviewed_by set", scalar("SELECT reviewed_by_user_id FROM pay_run_line WHERE id=?", (lid,)) == uid("joe"))
    check("reject: audit pay_run_line_reviewed", "pay_run_line_reviewed" in audit_actions(rid))


def test_reject_requires_note():
    fresh_db()
    seed_bill("RJ2", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_Controller")
    lid = seed_payline(rid, "RJ2")
    r = client("joe").post(f"/pay-runs/{rid}/lines/{lid}/review", data={"action": "reject", "note": "  "})
    check("reject no note: 302+flash", r.status_code == 302)
    check("reject no note: still Pending", scalar("SELECT line_state FROM pay_run_line WHERE id=?", (lid,)) == "Pending")


def test_cfo_approves_line():
    fresh_db()
    seed_bill("AP9", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_CFO")
    lid = seed_payline(rid, "AP9")
    client("shaun").post(f"/pay-runs/{rid}/lines/{lid}/review", data={"action": "approve"})
    check("cfo approve: line Approved", scalar("SELECT line_state FROM pay_run_line WHERE id=?", (lid,)) == "Approved")


def test_review_wrong_role():
    fresh_db()
    seed_bill("RW1", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_CFO")
    lid = seed_payline(rid, "RW1")
    # at CFO stage only cfo may review; controller (joe) cannot
    r = client("joe").post(f"/pay-runs/{rid}/lines/{lid}/review", data={"action": "approve"})
    check("review wrong role: controller at CFO stage -> 403", r.status_code == 403)


def test_review_wrong_state():
    fresh_db()
    seed_bill("RS1", state="Controller_Reviewed")
    rid = seed_run(status="Draft")
    lid = seed_payline(rid, "RS1")
    r = client("joe").post(f"/pay-runs/{rid}/lines/{lid}/review", data={"action": "approve"})
    check("review wrong state: Draft not reviewable -> 302+flash", r.status_code == 302)


def test_rejected_line_frees_bill():
    fresh_db()
    seed_bill("RF1", state="Controller_Reviewed")
    rid = seed_run(status="Submitted_to_Controller")
    lid = seed_payline(rid, "RF1")
    client("joe").post(f"/pay-runs/{rid}/lines/{lid}/review", data={"action": "reject", "note": "next week per CEO"})
    rid2 = seed_run(name="R2")
    cands = {c["qb_bill_id"] for c in payruns.candidate_bills(_conn(), rid2)}
    check("rejected frees bill: returns to pool", "RF1" in cands)


# ======================================================================
# Grouping / totals + CFO inbox + render
# ======================================================================

def test_grouping_and_totals():
    fresh_db()
    seed_bill("G1", state="Controller_Reviewed", open_balance=10000)
    seed_line("G1", "Training COGS")                        # contractor
    seed_bill("G2", state="Controller_Reviewed", open_balance=20000)  # non-contractor
    seed_bill("G3", state="Controller_Reviewed", open_balance=5000)
    rid = seed_run()
    seed_payline(rid, "G1", method="Check", amount=10000)
    seed_payline(rid, "G2", method="Wire", amount=20000)
    seed_payline(rid, "G3", method="Check", amount=5000, included=0)  # excluded
    g = payruns.grouped_lines(_conn(), rid)
    check("group: contractor total = 10000", g["contractor_total"] == 10000)
    check("group: other total = 20000 (G3 excluded)", g["other_total"] == 20000)
    check("group: grand total = 30000", g["grand_total"] == 30000)
    check("group: G3 in deferred", any(l["qb_bill_id"] == "G3" for l in g["deferred"]))
    methods = {grp["method"] for grp in g["contractor_groups"]}
    check("group: contractor Check group present", "Check" in methods)


def test_cfo_inbox_lists_submitted():
    fresh_db()
    seed_run(name="ForCFO", status="Submitted_to_CFO")
    seed_run(name="StillDraft", status="Draft")
    body = client("shaun").get("/inbox/cfo").get_data(as_text=True)
    check("cfo inbox: lists Submitted_to_CFO run", "ForCFO" in body)
    check("cfo inbox: excludes Draft run", "StillDraft" not in body)


def test_detail_renders():
    fresh_db()
    seed_bill("D1", state="Controller_Reviewed")
    rid = seed_run()
    seed_payline(rid, "D1", method="Check")
    r = client("joe").get(f"/pay-runs/{rid}")
    check("detail: 200", r.status_code == 200)
    check("detail: shows Grand Total", "Grand Total" in r.get_data(as_text=True))


def test_regression_phase3_approval():
    fresh_db()
    cn = _conn()
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,amount_cents,open_balance_cents,last_synced_at) "
               "VALUES ('Z1','Acme',10000,10000,'2026-05-22')")
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,created_at,updated_at) "
               "VALUES ('Z1','AP_Reviewed','2026-05-22','2026-05-22')")
    cn.commit()
    cn.close()
    client("joe").post("/bills/Z1/approve", follow_redirects=True)
    check("regression: AP_Reviewed -> Controller_Reviewed",
          scalar("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='Z1'") == "Controller_Reviewed")


TESTS = [
    test_migration_adds_columns, test_migration_idempotent, test_migration_noop_on_fresh,
    test_create_run, test_create_run_cfo_forbidden,
    test_candidate_pool_filters, test_candidate_flags, test_add_lines,
    test_add_lines_not_draft, test_claimed_bill_excluded, test_no_duplicate_line_same_run,
    test_edit_line, test_amount_locked_to_open_balance, test_exclude_line_frees_bill,
    test_full_lifecycle, test_submit_empty_run_blocked, test_transition_wrong_role,
    test_transition_wrong_state, test_reject_all_then_block_advance,
    test_controller_rejects_line, test_reject_requires_note, test_cfo_approves_line,
    test_review_wrong_role, test_review_wrong_state, test_rejected_line_frees_bill,
    test_grouping_and_totals, test_cfo_inbox_lists_submitted, test_detail_renders,
    test_regression_phase3_approval,
]


def main():
    try:
        for t in TESTS:
            print("=" * 64)
            print(t.__name__)
            print("=" * 64)
            t()
            print()
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    if FAILURES:
        print("%d FAILURE(S): %s" % (len(FAILURES), FAILURES))
    else:
        print("ALL PHASE 4 CHECKS PASSED")
    sys.exit(len(FAILURES))


if __name__ == "__main__":
    main()
