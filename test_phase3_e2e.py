"""
test_phase3_e2e.py -- Phase 3 approval workflow, end-to-end through the real
Flask routes. Sibling of test_phase3.py; same plain-Python style
(check(label, cond); exit code == number of failures; run with
`python test_phase3_e2e.py`).

Server contract under test (as-built, Post/Redirect/Get with flash on
validation/no-op failures -- NOT 4xx; that's deliberate, confirmed by Joe):
  - /bills/<id>/approve  New->AP_Reviewed (gated on classification, approver,
    approval_channel, approval_date) -> AP_Reviewed->Controller_Reviewed
  - /bills/<id>/reject   controller-only; required reason -> append-only Note
    prefixed bills.REJECT_NOTE_PREFIX + audit reject_to_new; state -> New
  - /inbox dispatcher; /inbox/controller; /inbox/cfo Phase-4 stub

DB safety: every test seeds a FRESH temp DB and points db.DB_PATH at it, so the
live payables.db is never opened. No live server -- Flask test_client only.
Needs a SECRET_KEY in .env (same as running the app).
"""
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
    print("CANNOT RUN: no SECRET_KEY in .env (needed to import the app)")
    sys.exit(1)

import db
import init_db
from bills import REJECT_NOTE_PREFIX

PW = generate_password_hash("testpw")
USERS = [("marilyn", "ap_clerk"), ("joe", "controller"), ("shaun", "cfo")]


def fresh_db():
    """New seeded temp DB; redirect ALL connections to it (live DB untouched)."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    db.DB_PATH = d / "e2e.db"
    cn = sqlite3.connect(db.DB_PATH)
    cn.executescript(init_db.SCHEMA)
    for u, r in USERS:
        cn.execute("INSERT INTO users (username,name,role,password_hash,is_active) "
                   "VALUES (?,?,?,?,1)", (u, u, r, PW))
    cn.commit()
    cn.close()


# app import does not open the DB; the scheduler only starts under __main__.
from app import app
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


def seed_bill(bid, state="New", classification=None, approver_name=None,
              approval_channel=None, approval_date=None):
    cn = _conn()
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,amount_cents,open_balance_cents,"
               "last_synced_at) VALUES (?,?,?,?,?)", (bid, "Acme", 10000, 10000, "2026-05-22"))
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,classification,"
               "approver_name,approval_channel,approval_date,created_at,updated_at) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (bid, state, classification, approver_name, approval_channel,
                approval_date, "2026-05-22", "2026-05-22"))
    cn.commit()
    cn.close()


def state_of(bid):
    cn = _conn()
    r = cn.execute("SELECT approval_state FROM bill_metadata WHERE qb_bill_id=?", (bid,)).fetchone()
    cn.close()
    return r["approval_state"] if r else None


def audit_actions(bid):
    cn = _conn()
    rows = cn.execute("SELECT action FROM audit_log WHERE entity_id=? ORDER BY id", (bid,)).fetchall()
    cn.close()
    return [r["action"] for r in rows]


def notes_of(bid):
    cn = _conn()
    rows = cn.execute("SELECT body FROM note WHERE qb_bill_id=? ORDER BY id", (bid,)).fetchall()
    cn.close()
    return [r["body"] for r in rows]


FULL = {"classification": "Real", "approver_name": "M",
        "approval_channel": "Email", "approval_date": "2026-05-20"}
LABELS = [("classification", "classification"), ("approver_name", "approver"),
          ("approval_channel", "approval channel"), ("approval_date", "approval date")]


# ----------------------------------------------------------------------

def test_1_required_fields_gate():
    fresh_db()
    m = client("marilyn")
    for miss_key, miss_label in LABELS:
        meta = dict(FULL)
        meta[miss_key] = None
        bid = "G_" + miss_key
        seed_bill(bid, state="New", **meta)
        r = m.post("/bills/%s/approve" % bid, follow_redirects=True)
        body = r.get_data(as_text=True)
        check("gate missing %s: 302->200, gate flash + label '%s' shown" % (miss_key, miss_label),
              r.status_code == 200 and "Fill these before AP review:" in body and miss_label in body)
        check("gate missing %s: state stays New" % miss_key, state_of(bid) == "New")
        check("gate missing %s: no audit row" % miss_key, audit_actions(bid) == [])
    seed_bill("G_all", state="New")
    body = m.post("/bills/G_all/approve", follow_redirects=True).get_data(as_text=True)
    check("gate all-4-missing: all four labels listed",
          all(lbl in body for _k, lbl in LABELS))
    check("gate all-4-missing: state stays New", state_of("G_all") == "New")


def test_2_new_to_ap_happy():
    fresh_db()
    seed_bill("B2", state="New", **FULL)
    r = client("marilyn").post("/bills/B2/approve", follow_redirects=True)
    check("New->AP: request ok (200 after redirect)", r.status_code == 200)
    check("New->AP: state AP_Reviewed", state_of("B2") == "AP_Reviewed")
    check("New->AP: audit approve_ap_reviewed", "approve_ap_reviewed" in audit_actions("B2"))


def test_3_ap_to_controller_happy():
    fresh_db()
    seed_bill("B3", state="AP_Reviewed")
    client("joe").post("/bills/B3/approve", follow_redirects=True)
    check("AP->Controller: state Controller_Reviewed", state_of("B3") == "Controller_Reviewed")
    check("AP->Controller: audit approve_controller_reviewed",
          "approve_controller_reviewed" in audit_actions("B3"))


def test_4_empty_reason_reject_blocked():
    fresh_db()
    seed_bill("B4", state="AP_Reviewed")
    r = client("joe").post("/bills/B4/reject", data={"reason": "   "})
    check("empty reject: 302 (PRG)", r.status_code == 302)
    check("empty reject: state unchanged", state_of("B4") == "AP_Reviewed")
    check("empty reject: no Note row", notes_of("B4") == [])
    check("empty reject: no audit row", audit_actions("B4") == [])


def test_5_reject_with_reason():
    fresh_db()
    seed_bill("B5", state="AP_Reviewed")
    client("joe").post("/bills/B5/reject", data={"reason": "missing receipt"}, follow_redirects=True)
    check("reject: state New", state_of("B5") == "New")
    ns = notes_of("B5")
    check("reject: one Note, prefix + reason verbatim",
          len(ns) == 1 and ns[0] == REJECT_NOTE_PREFIX + "missing receipt")
    check("reject: audit reject_to_new", "reject_to_new" in audit_actions("B5"))


def test_6_reject_is_append_only():
    fresh_db()
    seed_bill("B6", state="AP_Reviewed")
    cn = _conn()
    cn.execute("INSERT INTO note (qb_bill_id,user_id,body,created_at) "
               "VALUES ('B6',1,'pre-existing note','2026-05-01')")
    cn.commit()
    cn.close()
    client("joe").post("/bills/B6/reject", data={"reason": "redo"}, follow_redirects=True)
    ns = notes_of("B6")
    check("append-only: pre-existing note still present", "pre-existing note" in ns)
    check("append-only: reject note added alongside (2 total)", len(ns) == 2)


def test_7_ap_cannot_approve_to_controller():
    fresh_db()
    seed_bill("B7", state="AP_Reviewed")
    r = client("marilyn").post("/bills/B7/approve")
    check("AP approve on AP_Reviewed: 302 not 403", r.status_code == 302)
    check("AP approve: state unchanged", state_of("B7") == "AP_Reviewed")
    check("AP approve: no audit row", audit_actions("B7") == [])


def test_8_ap_cannot_reject():
    fresh_db()
    seed_bill("B8", state="AP_Reviewed")
    r = client("marilyn").post("/bills/B8/reject", data={"reason": "x"})
    check("AP reject: 403", r.status_code == 403)
    check("AP reject: state unchanged", state_of("B8") == "AP_Reviewed")
    check("AP reject: no Note added", notes_of("B8") == [])


def test_9_inbox_ap_user():
    fresh_db()
    r = client("marilyn").get("/inbox")
    body = r.get_data(as_text=True)
    check("/inbox AP: 200 inline", r.status_code == 200)
    check("/inbox AP: New-queue marker present", "New (your queue)" in body)


def test_10_inbox_controller_redirect():
    fresh_db()
    r = client("joe").get("/inbox")
    check("/inbox controller: 302", r.status_code == 302)
    check("/inbox controller: Location ends /inbox/controller",
          r.headers.get("Location", "").endswith("/inbox/controller"))


def test_11_inbox_cfo_stub():
    fresh_db()
    # /inbox/cfo is @login_required (open to any logged-in user); hit as joe.
    r = client("joe").get("/inbox/cfo")
    check("/inbox/cfo: 200 not 404 (controller)", r.status_code == 200)
    check("/inbox/cfo: seeded CFO user can reach it",
          client("shaun").get("/inbox/cfo").status_code == 200)


def test_12_audit_chronology():
    fresh_db()
    seed_bill("B12", state="New", **FULL)
    cn = _conn()  # pre-existing Phase-2-style row
    cn.execute("INSERT INTO audit_log (user_id,entity_type,entity_id,action,created_at) "
               "VALUES (2,'bill_metadata','B12','state_transition','2026-05-01 00:00:00')")
    cn.commit()
    cn.close()
    j = client("joe")
    j.post("/bills/B12/approve", follow_redirects=True)              # -> approve_ap_reviewed
    j.post("/bills/B12/reject", data={"reason": "redo"}, follow_redirects=True)  # -> reject_to_new
    acts = audit_actions("B12")  # ordered by id (chronological)
    check("chronology: old state_transition row preserved", "state_transition" in acts)
    check("chronology: new specific-named rows present",
          "approve_ap_reviewed" in acts and "reject_to_new" in acts)
    check("chronology: order state_transition < approve_ap_reviewed < reject_to_new",
          acts.index("state_transition") < acts.index("approve_ap_reviewed")
          < acts.index("reject_to_new"))


def test_13_utf8_prefix_roundtrip():
    fresh_db()
    seed_bill("B13", state="AP_Reviewed")
    client("joe").post("/bills/B13/reject", data={"reason": "unicode check"}, follow_redirects=True)
    cn = _conn()
    row = cn.execute("SELECT body FROM note WHERE qb_bill_id='B13' ORDER BY id DESC LIMIT 1").fetchone()
    cn.close()
    check("utf8: reject prefix round-trips cleanly through SQLite",
          row["body"].startswith(REJECT_NOTE_PREFIX))
    first = REJECT_NOTE_PREFIX[0]
    check("utf8: first char stored as valid UTF-8 (not mojibake/HTML entity)",
          row["body"].encode("utf-8").startswith(first.encode("utf-8")))


TESTS = [
    test_1_required_fields_gate, test_2_new_to_ap_happy, test_3_ap_to_controller_happy,
    test_4_empty_reason_reject_blocked, test_5_reject_with_reason, test_6_reject_is_append_only,
    test_7_ap_cannot_approve_to_controller, test_8_ap_cannot_reject, test_9_inbox_ap_user,
    test_10_inbox_controller_redirect, test_11_inbox_cfo_stub, test_12_audit_chronology,
    test_13_utf8_prefix_roundtrip,
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
        print("ALL PHASE 3 E2E CHECKS PASSED")
    sys.exit(len(FAILURES))


if __name__ == "__main__":
    main()
