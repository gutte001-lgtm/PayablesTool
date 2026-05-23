"""
test_phase_3_5.py -- Phase 3.5 follow-up workspace tests.

Plain-Python style (no pytest), matching test_phase3.py / test_phase3_e2e.py:
check(label, cond); exit code == number of failures; run with
`python test_phase_3_5.py`.

DB safety: every route test seeds a FRESH temp DB and points db.DB_PATH at it,
so the live payables.db is NEVER opened. The migration tests run against their
own throwaway DBs (a hand-built "legacy" pre-3.5 DB, and a fresh init_db one).
Needs a SECRET_KEY in .env (same as running the app).
"""
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

FAILURES = []
_TMPDIRS = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


# ---- pure unit test: business-days helper (no app/.env needed) --------------
import dates  # noqa: E402

check("BD: Fri->Mon = 1 (weekend skipped, not 3)",
      dates.business_days_between("2026-05-22", "2026-05-25") == 1)  # Fri->Mon
check("BD: same day = 0", dates.business_days_between("2026-05-22", "2026-05-22") == 0)
check("BD: Mon->next Mon = 5", dates.business_days_between("2026-05-18", "2026-05-25") == 5)
check("BD: end<start clamps to 0", dates.business_days_between("2026-05-25", "2026-05-22") == 0)
check("BD: ISO datetime string parses (date part only)", dates.business_days_between(
      "2026-05-22 09:00:00", "2026-05-25 17:00:00") == 1)  # Fri->Mon = Fri only

if not dotenv_values(Path(__file__).resolve().parent / ".env").get("SECRET_KEY"):
    print("CANNOT RUN route/migration tests: no SECRET_KEY in .env")
    sys.exit(1 if FAILURES else 0)

import db          # noqa: E402
import init_db     # noqa: E402

# Load the digit-prefixed migration module by path.
_mig_spec = importlib.util.spec_from_file_location(
    "mig001", Path(__file__).resolve().parent / "migrations" / "001_phase_3_5.py")
mig = importlib.util.module_from_spec(_mig_spec)
_mig_spec.loader.exec_module(mig)

PW = generate_password_hash("testpw")
USERS = [("marilyn", "Marilyn", "ap_clerk"), ("joe", "Joe", "controller"),
         ("shaun", "Shaun", "cfo"), ("allen", "Allen", "ap_clerk")]
TODAY = date.today()


def days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def fresh_db():
    """New seeded temp DB (full 3.5 schema + pills); redirect ALL connections."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    db.DB_PATH = d / "p35.db"
    cn = sqlite3.connect(db.DB_PATH)
    cn.executescript(init_db.SCHEMA)
    for u, name, role in USERS:
        cn.execute("INSERT INTO users (username,name,role,password_hash,is_active) "
                   "VALUES (?,?,?,?,1)", (u, name, role, PW))
    cn.commit()
    init_db.seed_status_pills(cn)
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


def seed_bill(bid, state="New", app_category=None, classification=None,
              bill_date=None, due_date=None, open_balance=10000,
              status_pill=None, created_at=None):
    cn = _conn()
    is_paid = 0 if open_balance > 0 else 1
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
               "open_balance_cents,bill_date,due_date,is_paid,last_synced_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, "Acme " + bid, "B-" + bid, 10000, open_balance,
                bill_date, due_date, is_paid, TODAY.isoformat()))
    ca = created_at or TODAY.isoformat()
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,app_category,"
               "classification,status_pill,created_at,updated_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (bid, state, app_category, classification, status_pill, ca, ca))
    cn.commit()
    cn.close()


def seed_line(bid, gl_account_name, line_number=1):
    cn = _conn()
    cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,"
               "line_amount_cents) VALUES (?,?,?,?)",
               (bid, line_number, gl_account_name, 10000))
    cn.commit()
    cn.close()


def seed_note(bid, user_id, body, created_at):
    cn = _conn()
    cn.execute("INSERT INTO note (qb_bill_id,user_id,body,created_at) VALUES (?,?,?,?)",
               (bid, user_id, body, created_at))
    cn.commit()
    cn.close()


def seed_todo(bid, body="follow up", completed=False):
    cn = _conn()
    done = TODAY.isoformat() if completed else None
    cn.execute("INSERT INTO todo (qb_bill_id,body,completed_at,created_by,created_at) "
               "VALUES (?,?,?,?,?)", (bid, body, done, uid("joe"), TODAY.isoformat()))
    cn.commit()
    cn.close()


def seed_tag(bid, tagged_user_id, tagged_by_user_id=None, tagged_at=None,
             cleared_at=None, note=None):
    cn = _conn()
    cn.execute("INSERT INTO bill_tag (qb_bill_id,tagged_user_id,tagged_by_user_id,"
               "tagged_at,cleared_at,note) VALUES (?,?,?,?,?,?)",
               (bid, tagged_user_id, tagged_by_user_id or uid("joe"),
                tagged_at or TODAY.isoformat(), cleared_at, note))
    cn.commit()
    cn.close()


def audit_actions(bid):
    cn = _conn()
    rows = cn.execute("SELECT action FROM audit_log WHERE entity_id=? ORDER BY id",
                      (bid,)).fetchall()
    cn.close()
    return [r["action"] for r in rows]


def scalar(sql, args=()):
    cn = _conn()
    r = cn.execute(sql, args).fetchone()
    cn.close()
    return r[0] if r else None


def active_tags(bid, user_id=None):
    if user_id is None:
        return scalar("SELECT COUNT(*) FROM bill_tag WHERE qb_bill_id=? "
                      "AND cleared_at IS NULL", (bid,))
    return scalar("SELECT COUNT(*) FROM bill_tag WHERE qb_bill_id=? AND tagged_user_id=? "
                  "AND cleared_at IS NULL", (bid, user_id))


# ======================================================================
# Migration
# ======================================================================

def legacy_db():
    """A hand-built pre-3.5 DB: bill_metadata WITHOUT status_pill, and no
    status_pill_lookup / bill_tag. Returns its path."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    p = d / "legacy.db"
    cn = sqlite3.connect(p)
    cn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT,
            name TEXT, role TEXT, is_active INTEGER DEFAULT 1, password_hash TEXT);
        CREATE TABLE bill (qb_bill_id TEXT PRIMARY KEY, vendor TEXT,
            open_balance_cents INTEGER, last_synced_at TEXT);
        CREATE TABLE bill_metadata (qb_bill_id TEXT PRIMARY KEY, approval_state TEXT,
            created_at TEXT, updated_at TEXT);
    """)
    cn.commit()
    cn.close()
    return p


def _has_col(p, table, col):
    cn = sqlite3.connect(p)
    out = any(r[1] == col for r in cn.execute(f"PRAGMA table_info({table})"))
    cn.close()
    return out


def _has_table(p, name):
    cn = sqlite3.connect(p)
    out = cn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() \
        is not None
    cn.close()
    return out


def test_migration_upgrades_legacy():
    p = legacy_db()
    a = mig.migrate(p, verbose=False)
    check("mig: status_pill column ADDED on legacy", a["status_pill_column"] == "added")
    check("mig: status_pill_lookup created", a["status_pill_lookup"] == "created")
    check("mig: bill_tag created", a["bill_tag"] == "created")
    check("mig: column actually present after run", _has_col(p, "bill_metadata", "status_pill"))
    check("mig: bill_tag table actually present", _has_table(p, "bill_tag"))
    cn = sqlite3.connect(p)
    n_seed = cn.execute("SELECT COUNT(*) FROM status_pill_lookup WHERE is_seed=1").fetchone()[0]
    cn.close()
    check("mig: 4 seed pills present with is_seed=1", n_seed == 4)
    check("mig: not flagged already_migrated on first run", a["already_migrated"] is False)


def test_migration_idempotent():
    p = legacy_db()
    mig.migrate(p, verbose=False)
    a2 = mig.migrate(p, verbose=False)            # second run = no-op
    check("mig: rerun reports already_migrated", a2["already_migrated"] is True)
    check("mig: rerun leaves column alone", a2["status_pill_column"] == "already present")
    check("mig: rerun leaves pills alone", a2["pills_seeded"] == "already present")
    n = scalar_path(p, "SELECT COUNT(*) FROM status_pill_lookup")
    check("mig: still exactly 4 pills after rerun", n == 4)


def test_migration_noop_on_fresh_initdb():
    fresh_db()                                    # full 3.5 schema already
    a = mig.migrate(db.DB_PATH, verbose=False)
    check("mig: fresh init_db DB is already fully migrated", a["already_migrated"] is True)


def scalar_path(p, sql, args=()):
    cn = sqlite3.connect(p)
    r = cn.execute(sql, args).fetchone()
    cn.close()
    return r[0] if r else None


def test_fresh_schema_has_3_5():
    fresh_db()
    check("fresh: bill_metadata.status_pill present", _has_col(db.DB_PATH, "bill_metadata", "status_pill"))
    check("fresh: bill_tag table present", _has_table(db.DB_PATH, "bill_tag"))
    check("fresh: status_pill_lookup table present", _has_table(db.DB_PATH, "status_pill_lookup"))
    check("fresh: 4 seed pills is_seed=1",
          scalar("SELECT COUNT(*) FROM status_pill_lookup WHERE is_seed=1") == 4)


# ======================================================================
# Status pills
# ======================================================================

def test_set_pill():
    fresh_db()
    seed_bill("P1")
    r = client("marilyn").post("/bills/P1/status_pill", data={"value": "In Review"})
    check("pill set: 302 PRG", r.status_code == 302)
    check("pill set: DB updated", scalar("SELECT status_pill FROM bill_metadata WHERE qb_bill_id='P1'") == "In Review")
    check("pill set: audit status_pill_set", "status_pill_set" in audit_actions("P1"))


def test_clear_pill():
    fresh_db()
    seed_bill("P2", status_pill="Blocked")
    client("marilyn").post("/bills/P2/status_pill", data={"value": ""})
    check("pill clear: DB now NULL", scalar("SELECT status_pill FROM bill_metadata WHERE qb_bill_id='P2'") is None)
    check("pill clear: audit status_pill_set", "status_pill_set" in audit_actions("P2"))


def test_set_pill_nonexistent_rejected():
    fresh_db()
    seed_bill("P3")
    r = client("marilyn").post("/bills/P3/status_pill", data={"value": "Made Up Pill"})
    check("pill bogus: 302+flash (not 4xx)", r.status_code == 302)
    check("pill bogus: no state change", scalar("SELECT status_pill FROM bill_metadata WHERE qb_bill_id='P3'") is None)
    check("pill bogus: no audit row", "status_pill_set" not in audit_actions("P3"))


def test_add_pill():
    fresh_db()
    before = scalar("SELECT COUNT(*) FROM status_pill_lookup")
    client("marilyn").post("/admin/status_pills", data={"value": "Needs W-9"})
    check("pill add: in lookup", scalar("SELECT COUNT(*) FROM status_pill_lookup WHERE value='Needs W-9'") == 1)
    check("pill add: is_seed=0", scalar("SELECT is_seed FROM status_pill_lookup WHERE value='Needs W-9'") == 0)
    check("pill add: row count +1", scalar("SELECT COUNT(*) FROM status_pill_lookup") == before + 1)
    check("pill add: audit status_pill_added",
          scalar("SELECT COUNT(*) FROM audit_log WHERE action='status_pill_added'") == 1)


def test_add_pill_duplicate_ci():
    fresh_db()
    before = scalar("SELECT COUNT(*) FROM status_pill_lookup")
    r = client("marilyn").post("/admin/status_pills", data={"value": "in review"})  # dup of seed
    check("pill dup: 302+flash", r.status_code == 302)
    check("pill dup: no new row", scalar("SELECT COUNT(*) FROM status_pill_lookup") == before)


def test_add_pill_role():
    fresh_db()
    check("pill add: ap_clerk allowed (302)",
          client("marilyn").post("/admin/status_pills", data={"value": "Escalated"}).status_code == 302)
    check("pill add: CFO forbidden (403)",
          client("shaun").post("/admin/status_pills", data={"value": "Sneaky"}).status_code == 403)


# ======================================================================
# Tags
# ======================================================================

def test_tag_user():
    fresh_db()
    seed_bill("T1")
    client("marilyn").post("/bills/T1/tag", data={"user_id": uid("joe"), "note": "pls review"})
    check("tag: one active tag for joe", active_tags("T1", uid("joe")) == 1)
    check("tag: cleared_at IS NULL", scalar("SELECT cleared_at FROM bill_tag WHERE qb_bill_id='T1'") is None)
    check("tag: audit bill_tagged", "bill_tagged" in audit_actions("T1"))


def test_tag_same_user_twice():
    fresh_db()
    seed_bill("T2")
    m = client("marilyn")
    m.post("/bills/T2/tag", data={"user_id": uid("joe")})
    m.post("/bills/T2/tag", data={"user_id": uid("joe")})
    check("tag dup: still only 1 active tag", active_tags("T2", uid("joe")) == 1)


def test_tag_two_users():
    fresh_db()
    seed_bill("T3")
    m = client("marilyn")
    m.post("/bills/T3/tag", data={"user_id": uid("joe")})
    m.post("/bills/T3/tag", data={"user_id": uid("shaun")})
    check("tag two: joe active", active_tags("T3", uid("joe")) == 1)
    check("tag two: shaun active", active_tags("T3", uid("shaun")) == 1)
    check("tag two: 2 active total", active_tags("T3") == 2)


def test_tagged_user_clears_own():
    fresh_db()
    seed_bill("T4")
    client("marilyn").post("/bills/T4/tag", data={"user_id": uid("allen")})
    tag_id = scalar("SELECT id FROM bill_tag WHERE qb_bill_id='T4'")
    client("allen").post(f"/bills/T4/tag/{tag_id}/clear")
    check("tag clear self: cleared_at set", scalar("SELECT cleared_at FROM bill_tag WHERE id=?", (tag_id,)) is not None)
    check("tag clear self: audit bill_tag_cleared", "bill_tag_cleared" in audit_actions("T4"))


def test_other_clerk_cannot_clear():
    fresh_db()
    seed_bill("T5")
    client("marilyn").post("/bills/T5/tag", data={"user_id": uid("allen")})
    tag_id = scalar("SELECT id FROM bill_tag WHERE qb_bill_id='T5'")
    # marilyn is ap_clerk, not the tagged user (allen), not a controller
    r = client("marilyn").post(f"/bills/T5/tag/{tag_id}/clear")
    check("tag clear other: 403", r.status_code == 403)
    check("tag clear other: still active", active_tags("T5", uid("allen")) == 1)


def test_controller_clears_anyones():
    fresh_db()
    seed_bill("T6")
    client("marilyn").post("/bills/T6/tag", data={"user_id": uid("allen")})
    tag_id = scalar("SELECT id FROM bill_tag WHERE qb_bill_id='T6'")
    client("joe").post(f"/bills/T6/tag/{tag_id}/clear")     # joe is controller
    check("tag clear controller: cleared", scalar("SELECT cleared_at FROM bill_tag WHERE id=?", (tag_id,)) is not None)


def test_self_tag_prevented():
    fresh_db()
    seed_bill("T7")
    r = client("marilyn").post("/bills/T7/tag", data={"user_id": uid("marilyn")})
    check("self-tag: 302+flash (rejected)", r.status_code == 302)
    check("self-tag: no tag created", active_tags("T7") == 0)


# ======================================================================
# @mention parsing in notes
# ======================================================================

def test_mention_creates_tag():
    fresh_db()
    seed_bill("M1")
    client("marilyn").post("/bills/M1/notes", data={"body": "hey @joe can you look?"})
    check("mention: joe tagged", active_tags("M1", uid("joe")) == 1)
    check("mention: audit bill_tagged_via_mention", "bill_tagged_via_mention" in audit_actions("M1"))
    check("mention: tag note references the note id",
          "via @mention in note" in (scalar("SELECT note FROM bill_tag WHERE qb_bill_id='M1'") or ""))


def test_mention_no_double_tag():
    fresh_db()
    seed_bill("M2")
    m = client("marilyn")
    m.post("/bills/M2/notes", data={"body": "@joe look"})
    m.post("/bills/M2/notes", data={"body": "@joe again please"})
    check("mention dup: still 1 active tag for joe", active_tags("M2", uid("joe")) == 1)


def test_self_mention_noop():
    fresh_db()
    seed_bill("M3")
    client("marilyn").post("/bills/M3/notes", data={"body": "note to self @marilyn"})
    check("self-mention: no tag created", active_tags("M3") == 0)


def test_unknown_mention_ignored():
    fresh_db()
    seed_bill("M4")
    r = client("marilyn").post("/bills/M4/notes", data={"body": "@nobody hello"}, follow_redirects=True)
    check("unknown mention: request ok (no error)", r.status_code == 200)
    check("unknown mention: no tag created", active_tags("M4") == 0)
    check("unknown mention: note still saved", scalar("SELECT COUNT(*) FROM note WHERE qb_bill_id='M4'") == 1)


# ======================================================================
# Follow-up view + section queries
# ======================================================================

def test_followup_past_sla_contractor():
    fresh_db()
    seed_bill("FS1", bill_date=days_ago(20), due_date=days_ago(1))  # >14d old
    seed_line("FS1", "Training COGS")                               # contractor leaf
    r = client("joe").get("/follow-up")
    check("followup: 200", r.status_code == 200)
    check("followup: contractor bill in past-SLA", "/bills/FS1" in r.get_data(as_text=True))


def test_followup_past_sla_else_branch():
    fresh_db()
    seed_bill("FS2", app_category="Uncategorized", bill_date=days_ago(3), due_date=days_ago(5))
    seed_line("FS2", "Other COGS")                                  # NOT contractor
    body = client("joe").get("/follow-up").get_data(as_text=True)
    check("followup: non-contractor past-due in past-SLA (else branch)", "/bills/FS2" in body)


def test_followup_stale():
    fresh_db()
    old = days_ago(14)
    seed_bill("ST1", created_at=old)
    seed_note("ST1", uid("joe"), "last touched", old)
    body = client("joe").get("/follow-up").get_data(as_text=True)
    check("followup: stale bill (>5 BD) listed", "/bills/ST1" in body)


def test_followup_open_todos():
    fresh_db()
    seed_bill("TD1")
    seed_todo("TD1", completed=False)
    body = client("joe").get("/follow-up").get_data(as_text=True)
    check("followup: open-todo bill listed", "/bills/TD1" in body)


def test_followup_in_process():
    fresh_db()
    seed_bill("IP1")
    seed_tag("IP1", uid("allen"))
    body = client("joe").get("/follow-up").get_data(as_text=True)
    check("followup: tagged bill in in-process", "/bills/IP1" in body)


def test_followup_multi_section_not_deduped():
    fresh_db()
    old = days_ago(20)
    seed_bill("MULTI", bill_date=old, due_date=days_ago(2), created_at=old)
    seed_line("MULTI", "Service and Repair COGS")  # contractor -> past SLA
    seed_todo("MULTI", completed=False)            # -> open to-dos
    seed_tag("MULTI", uid("allen"))                # -> in process (direct, no audit)
    # no notes/audit + old created_at -> stale
    body = client("joe").get("/follow-up").get_data(as_text=True)
    check("followup: same bill renders in all 4 sections (not deduped)",
          body.count("/bills/MULTI") >= 4)


# ======================================================================
# Regression -- Phase 3 approval flow still works
# ======================================================================

def test_regression_phase3_approval():
    fresh_db()
    seed_bill("R1", state="AP_Reviewed")
    client("joe").post("/bills/R1/approve", follow_redirects=True)
    check("regression: AP_Reviewed -> Controller_Reviewed",
          scalar("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='R1'") == "Controller_Reviewed")


TESTS = [
    test_migration_upgrades_legacy, test_migration_idempotent,
    test_migration_noop_on_fresh_initdb, test_fresh_schema_has_3_5,
    test_set_pill, test_clear_pill, test_set_pill_nonexistent_rejected,
    test_add_pill, test_add_pill_duplicate_ci, test_add_pill_role,
    test_tag_user, test_tag_same_user_twice, test_tag_two_users,
    test_tagged_user_clears_own, test_other_clerk_cannot_clear,
    test_controller_clears_anyones, test_self_tag_prevented,
    test_mention_creates_tag, test_mention_no_double_tag, test_self_mention_noop,
    test_unknown_mention_ignored,
    test_followup_past_sla_contractor, test_followup_past_sla_else_branch,
    test_followup_stale, test_followup_open_todos, test_followup_in_process,
    test_followup_multi_section_not_deduped,
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
        print("ALL PHASE 3.5 CHECKS PASSED")
    sys.exit(len(FAILURES))


if __name__ == "__main__":
    main()
