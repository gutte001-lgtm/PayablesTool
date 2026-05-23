"""
test_phase_3_6.py -- Phase 3.6 open-items tests.

Plain-Python style (no pytest), matching test_phase_3_5.py: check(label, cond);
exit code == number of failures; run with `python test_phase_3_6.py`.

DB safety: every route test seeds a FRESH temp DB and points db.DB_PATH at it,
so the live payables.db is NEVER opened. Migration tests use throwaway DBs (a
"legacy" post-3.5/pre-3.6 DB built by dropping bill_open_item from the full
schema, and a fresh init_db one). Needs SECRET_KEY in .env.
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


if not dotenv_values(Path(__file__).resolve().parent / ".env").get("SECRET_KEY"):
    print("CANNOT RUN: no SECRET_KEY in .env")
    sys.exit(1)

import db          # noqa: E402
import init_db     # noqa: E402
import tags        # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mig002", Path(__file__).resolve().parent / "migrations" / "002_phase_3_6.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

PW = generate_password_hash("testpw")
USERS = [("marilyn", "Marilyn", "ap_clerk"), ("joe", "Joe", "controller"),
         ("shaun", "Shaun", "cfo")]
TODAY = date.today()


def days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def fresh_db():
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    db.DB_PATH = d / "p36.db"
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


def seed_bill(bid, state="New", status_pill=None, open_balance=10000):
    cn = _conn()
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
               "open_balance_cents,is_paid,last_synced_at) VALUES (?,?,?,?,?,?,?)",
               (bid, "Acme " + bid, "B-" + bid, 10000, open_balance,
                0 if open_balance > 0 else 1, TODAY.isoformat()))
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,status_pill,"
               "created_at,updated_at) VALUES (?,?,?,?,?)",
               (bid, state, status_pill, TODAY.isoformat(), TODAY.isoformat()))
    cn.commit()
    cn.close()


def seed_open_item(bid, description, created_by=None, created_at=None,
                   resolved_at=None, resolved_by=None, resolution_note=None):
    cn = _conn()
    cur = cn.execute(
        "INSERT INTO bill_open_item (qb_bill_id,description,created_by_user_id,"
        "created_at,resolved_at,resolved_by_user_id,resolution_note) "
        "VALUES (?,?,?,?,?,?,?)",
        (bid, description, created_by or uid("joe"), created_at or TODAY.isoformat(),
         resolved_at, resolved_by, resolution_note))
    cn.commit()
    rid = cur.lastrowid
    cn.close()
    return rid


def seed_tag(bid, tagged_user_id, tagged_by=None):
    cn = _conn()
    cn.execute("INSERT INTO bill_tag (qb_bill_id,tagged_user_id,tagged_by_user_id,"
               "tagged_at) VALUES (?,?,?,?)",
               (bid, tagged_user_id, tagged_by or uid("joe"), TODAY.isoformat()))
    cn.commit()
    cn.close()


def scalar(sql, args=()):
    cn = _conn()
    r = cn.execute(sql, args).fetchone()
    cn.close()
    return r[0] if r else None


def audit_actions(bid):
    cn = _conn()
    rows = cn.execute("SELECT action FROM audit_log WHERE entity_id=? ORDER BY id",
                      (bid,)).fetchall()
    cn.close()
    return [r["action"] for r in rows]


def open_count(bid=None):
    if bid:
        return scalar("SELECT COUNT(*) FROM bill_open_item WHERE qb_bill_id=? "
                      "AND resolved_at IS NULL", (bid,))
    return scalar("SELECT COUNT(*) FROM bill_open_item WHERE resolved_at IS NULL")


# ======================================================================
# Migration
# ======================================================================

def _has_table(p, name):
    cn = sqlite3.connect(p)
    out = cn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() \
        is not None
    cn.close()
    return out


def legacy_db():
    """Full current schema with bill_open_item dropped -> simulates a DB at
    Phase 3.5 but not yet 3.6. Returns its path."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    p = d / "legacy36.db"
    cn = sqlite3.connect(p)
    cn.executescript(init_db.SCHEMA)
    cn.execute("DROP TABLE bill_open_item")     # indexes drop with the table
    cn.commit()
    cn.close()
    return p


def test_migration_creates_table():
    p = legacy_db()
    check("mig: legacy lacks bill_open_item before run", not _has_table(p, "bill_open_item"))
    a = mig.migrate(p, verbose=False)
    check("mig: reports created", a["bill_open_item"] == "created")
    check("mig: table present after run", _has_table(p, "bill_open_item"))
    check("mig: index idx_openitem_bill present",
          _has_table(p, "idx_openitem_bill"))
    check("mig: index idx_openitem_open present",
          _has_table(p, "idx_openitem_open"))
    check("mig: not flagged already_migrated on first run", a["already_migrated"] is False)


def test_migration_idempotent():
    p = legacy_db()
    mig.migrate(p, verbose=False)
    a2 = mig.migrate(p, verbose=False)
    check("mig: rerun reports already present", a2["bill_open_item"] == "already present")
    check("mig: rerun flagged already_migrated", a2["already_migrated"] is True)


def test_migration_noop_on_fresh_initdb():
    fresh_db()
    a = mig.migrate(db.DB_PATH, verbose=False)
    check("mig: fresh init_db DB already has the table", a["already_migrated"] is True)


# ======================================================================
# Create
# ======================================================================

def test_create_open_item():
    fresh_db()
    seed_bill("C1")
    r = client("marilyn").post("/bills/C1/open_items", data={"description": "call vendor for W-9"})
    check("create: 302 PRG", r.status_code == 302)
    check("create: row inserted, open", open_count("C1") == 1)
    check("create: resolved_at IS NULL", scalar("SELECT resolved_at FROM bill_open_item WHERE qb_bill_id='C1'") is None)
    check("create: audit open_item_created", "open_item_created" in audit_actions("C1"))


def test_create_empty_rejected():
    fresh_db()
    seed_bill("C2")
    r = client("marilyn").post("/bills/C2/open_items", data={"description": "   "})
    check("create empty: 302+flash", r.status_code == 302)
    check("create empty: no row", open_count("C2") == 0)
    check("create empty: no audit", "open_item_created" not in audit_actions("C2"))


def test_create_cfo_forbidden():
    fresh_db()
    seed_bill("C3")
    r = client("shaun").post("/bills/C3/open_items", data={"description": "x"})
    check("create CFO: 403", r.status_code == 403)
    check("create CFO: no row", open_count("C3") == 0)


def test_two_open_items_same_bill():
    fresh_db()
    seed_bill("C4")
    m = client("marilyn")
    m.post("/bills/C4/open_items", data={"description": "first"})
    m.post("/bills/C4/open_items", data={"description": "second"})
    check("two items: both open", open_count("C4") == 2)


# ======================================================================
# Resolve
# ======================================================================

def test_resolve_with_note():
    fresh_db()
    seed_bill("R1")
    iid = seed_open_item("R1", "do the thing")
    client("joe").post(f"/bills/R1/open_items/{iid}/resolve",
                       data={"resolution_note": "done, vendor sent it"})
    check("resolve: resolved_at set", scalar("SELECT resolved_at FROM bill_open_item WHERE id=?", (iid,)) is not None)
    check("resolve: resolved_by set", scalar("SELECT resolved_by_user_id FROM bill_open_item WHERE id=?", (iid,)) == uid("joe"))
    check("resolve: note stored", scalar("SELECT resolution_note FROM bill_open_item WHERE id=?", (iid,)) == "done, vendor sent it")
    check("resolve: audit open_item_resolved", "open_item_resolved" in audit_actions("R1"))


def test_resolve_empty_note():
    fresh_db()
    seed_bill("R2")
    iid = seed_open_item("R2", "do the thing")
    r = client("joe").post(f"/bills/R2/open_items/{iid}/resolve", data={"resolution_note": "  "})
    check("resolve empty: 302+flash", r.status_code == 302)
    check("resolve empty: stays open", scalar("SELECT resolved_at FROM bill_open_item WHERE id=?", (iid,)) is None)
    check("resolve empty: no audit", "open_item_resolved" not in audit_actions("R2"))


def test_resolve_already_resolved():
    fresh_db()
    seed_bill("R3")
    iid = seed_open_item("R3", "do the thing")
    j = client("joe")
    j.post(f"/bills/R3/open_items/{iid}/resolve", data={"resolution_note": "first"})
    r = j.post(f"/bills/R3/open_items/{iid}/resolve", data={"resolution_note": "second"})
    check("resolve twice: 302+flash", r.status_code == 302)
    check("resolve twice: note unchanged (no double-resolve)",
          scalar("SELECT resolution_note FROM bill_open_item WHERE id=?", (iid,)) == "first")
    check("resolve twice: exactly one open_item_resolved audit row",
          audit_actions("R3").count("open_item_resolved") == 1)


def test_resolve_cfo_forbidden():
    fresh_db()
    seed_bill("R4")
    iid = seed_open_item("R4", "do the thing")
    r = client("shaun").post(f"/bills/R4/open_items/{iid}/resolve", data={"resolution_note": "x"})
    check("resolve CFO: 403", r.status_code == 403)
    check("resolve CFO: stays open", scalar("SELECT resolved_at FROM bill_open_item WHERE id=?", (iid,)) is None)


# ======================================================================
# Display
# ======================================================================

def test_home_shows_section_with_items():
    fresh_db()
    seed_bill("H1")
    seed_open_item("H1", "chase the invoice copy")
    body = client("marilyn").get("/").get_data(as_text=True)
    check("home: 'Open Items' header present", "Open Items" in body)
    check("home: description shown", "chase the invoice copy" in body)


def test_home_empty_state():
    fresh_db()
    body = client("marilyn").get("/").get_data(as_text=True)
    check("home empty: shows empty state", "No open items" in body)


def test_home_sort_oldest_first():
    fresh_db()
    seed_bill("H2")
    seed_open_item("H2", "OLDER TASK", created_at=days_ago(10))
    seed_open_item("H2", "NEWER TASK", created_at=days_ago(1))
    body = client("marilyn").get("/").get_data(as_text=True)
    check("home sort: older item appears above newer",
          body.index("OLDER TASK") < body.index("NEWER TASK"))


def test_resolved_not_shown():
    fresh_db()
    seed_bill("H3")
    seed_open_item("H3", "RESOLVED ALREADY", resolved_at=days_ago(1),
                   resolved_by=uid("joe"), resolution_note="handled")
    body = client("marilyn").get("/").get_data(as_text=True)
    check("resolved: not on home page", "RESOLVED ALREADY" not in body)
    check("resolved: not in per-bill open list",
          len(tags.open_items_for_bill(_conn(), "H3")) == 0)


def test_detail_section_visibility():
    fresh_db()
    seed_bill("H4")
    seed_bill("H5")
    seed_open_item("H4", "needs attention on H4")
    m = client("marilyn")
    body4 = m.get("/bills/H4").get_data(as_text=True)
    body5 = m.get("/bills/H5").get_data(as_text=True)
    check("detail: section shown when items exist", "Open Items on this bill" in body4)
    check("detail: section hidden when none", "Open Items on this bill" not in body5)


def test_nav_badge_count():
    fresh_db()
    seed_bill("N1")
    seed_bill("N2")
    seed_open_item("N1", "a")
    seed_open_item("N2", "b")
    seed_open_item("N1", "c")
    cn = _conn()
    check("nav badge: open_item_total == all_open_items count",
          tags.open_item_total(cn) == len(tags.all_open_items(cn)) == 3)
    cn.close()


# ======================================================================
# Cross-cutting
# ======================================================================

def test_open_items_alongside_tags():
    fresh_db()
    seed_bill("X1")
    seed_tag("X1", uid("marilyn"))
    iid = seed_open_item("X1", "do thing while tagged")
    check("cross: tag active", scalar("SELECT COUNT(*) FROM bill_tag WHERE qb_bill_id='X1' AND cleared_at IS NULL") == 1)
    check("cross: open item present", open_count("X1") == 1)
    # resolving the open item must not touch the tag
    client("joe").post(f"/bills/X1/open_items/{iid}/resolve", data={"resolution_note": "ok"})
    check("cross: tag still active after resolve",
          scalar("SELECT COUNT(*) FROM bill_tag WHERE qb_bill_id='X1' AND cleared_at IS NULL") == 1)
    check("cross: open item now resolved", open_count("X1") == 0)


def test_status_pill_in_home_table():
    fresh_db()
    seed_bill("X2", status_pill="In Review")
    seed_open_item("X2", "pill should show")
    body = client("marilyn").get("/").get_data(as_text=True)
    check("pill in home: status pill rendered in open-items table", "In Review" in body)


def test_regression_phase3_approval():
    fresh_db()
    seed_bill("Z1", state="AP_Reviewed")
    client("joe").post("/bills/Z1/approve", follow_redirects=True)
    check("regression: AP_Reviewed -> Controller_Reviewed",
          scalar("SELECT approval_state FROM bill_metadata WHERE qb_bill_id='Z1'") == "Controller_Reviewed")


TESTS = [
    test_migration_creates_table, test_migration_idempotent,
    test_migration_noop_on_fresh_initdb,
    test_create_open_item, test_create_empty_rejected, test_create_cfo_forbidden,
    test_two_open_items_same_bill,
    test_resolve_with_note, test_resolve_empty_note, test_resolve_already_resolved,
    test_resolve_cfo_forbidden,
    test_home_shows_section_with_items, test_home_empty_state,
    test_home_sort_oldest_first, test_resolved_not_shown,
    test_detail_section_visibility, test_nav_badge_count,
    test_open_items_alongside_tags, test_status_pill_in_home_table,
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
        print("ALL PHASE 3.6 CHECKS PASSED")
    sys.exit(len(FAILURES))


if __name__ == "__main__":
    main()
