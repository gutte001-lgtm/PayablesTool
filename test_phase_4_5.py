"""
test_phase_4_5.py -- Phase 4.5 AP dueness classification tests.

Plain-Python style (no pytest), matching test_phase_4.py: check(label, cond);
exit code == number of failures; run with `python test_phase_4_5.py`.

Two kinds of tests:
  * PURE (sqlite only, throwaway temp DBs) -- migration, liability detection,
    default classification, debt-service auto-promote, invoice_due_date sync
    audit, the pay-run fence predicate, and the AP-report asymmetry.
  * ROUTE (Flask test client, fresh temp DB pointed at by db.DB_PATH) -- the
    classify access model, impossible-cell coercion, invoice_due_date being
    unreachable from a team edit, the fence via a forced add_lines POST, and
    /triage access + apply.

DB safety: the live payables.db is NEVER opened. Needs SECRET_KEY in .env for
the route tests.
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


ROOT = Path(__file__).resolve().parent
import db          # noqa: E402
import init_db     # noqa: E402
import sync        # noqa: E402
import tags        # noqa: E402
import payruns     # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mig006", ROOT / "migrations" / "006_phase_4_5.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

TODAY = date.today().isoformat()
PAST = (date.today() - timedelta(days=3)).isoformat()
FUTURE = (date.today() + timedelta(days=30)).isoformat()


def _new_path(name="t.db"):
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    return d / name


def _conn(p):
    cn = sqlite3.connect(p)
    cn.row_factory = sqlite3.Row
    cn.execute("PRAGMA foreign_keys = ON")
    return cn


def fresh_sqlite():
    """A throwaway DB built straight from init_db.SCHEMA (Phase 4.5 native)."""
    p = _new_path()
    cn = _conn(p)
    cn.executescript(init_db.SCHEMA)
    cn.execute("INSERT INTO users (username,name,role,is_active) "
               "VALUES ('joe','Joe','controller',1)")
    cn.commit()
    return p, cn


def add_bill(cn, bid, amount=10000, open_bal=10000, due="2026-06-15"):
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
               "open_balance_cents,bill_date,due_date,is_paid,last_synced_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, "V" + bid, "B" + bid, amount, open_bal, "2026-05-01", due,
                0 if open_bal > 0 else 1, "2026-05-22"))


def add_line(cn, bid, n=1, name="56100 OUTBOUND SHIPPING", canon=None, amt=10000):
    cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,"
               "gl_account_number,gl_account_number_canonical,line_amount_cents) "
               "VALUES (?,?,?,?,?,?)",
               (bid, n, name, sync.parse_gl_number(name), canon, amt))


def add_meta(cn, bid, obligation="ordinary_ap", due_state="not_due",
             approval_state="Controller_Reviewed", classification=None,
             invoice_due=None, reason=None):
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,classification,"
               "obligation_type,due_state,invoice_due_date,classification_reason,"
               "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, approval_state, classification, obligation, due_state,
                invoice_due, reason, "t", "t"))


# ======================================================================
print("=" * 60); print("test_migration"); print("=" * 60)

LEGACY = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, name TEXT,
    role TEXT, password_hash TEXT, is_active INTEGER DEFAULT 1);
CREATE TABLE bill (qb_bill_id TEXT PRIMARY KEY, due_date TEXT, last_synced_at TEXT);
CREATE TABLE bill_metadata (
    qb_bill_id TEXT PRIMARY KEY REFERENCES bill(qb_bill_id),
    classification TEXT, status_pill TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def legacy_path():
    p = _new_path("legacy.db")
    cn = sqlite3.connect(p)
    cn.executescript(LEGACY)
    cn.execute("INSERT INTO bill (qb_bill_id,due_date,last_synced_at) VALUES ('B1','2026-06-01','t')")
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,created_at,updated_at) VALUES ('B1','t','t')")
    cn.commit(); cn.close()
    return p


p = legacy_path()
a = mig.migrate(p, verbose=False)
check("mig: legacy not flagged already_migrated", a["already_migrated"] is False)
cn = _conn(p)
cols = [r[1] for r in cn.execute("PRAGMA table_info(bill_metadata)")]
for c in ("invoice_due_date", "expected_payment_date", "obligation_type", "due_state",
          "classification_reason", "classification_note", "classified_by", "classified_at"):
    check(f"mig: column {c} added", c in cols)
check("mig: classification_reason_lookup created",
      cn.execute("SELECT 1 FROM sqlite_master WHERE name='classification_reason_lookup'").fetchone() is not None)
check("mig: classification_audit created",
      cn.execute("SELECT 1 FROM sqlite_master WHERE name='classification_audit'").fetchone() is not None)
check("mig: 8 reasons seeded",
      cn.execute("SELECT COUNT(*) FROM classification_reason_lookup WHERE is_seed=1").fetchone()[0] == 8)
row = cn.execute("SELECT obligation_type,due_state FROM bill_metadata WHERE qb_bill_id='B1'").fetchone()
check("mig: existing row backfilled to ordinary_ap/not_due",
      row["obligation_type"] == "ordinary_ap" and row["due_state"] == "not_due")
cn.close()
a2 = mig.migrate(p, verbose=False)
check("mig: second run flagged already_migrated", a2["already_migrated"] is True)

fp, fcn = fresh_sqlite()
init_db.seed_classification_reasons(fcn)  # init_db.init() seeds these on a real fresh DB
fcn.close()
a3 = mig.migrate(fp, verbose=False)
check("mig: fresh init_db DB (schema+seeds) already fully migrated",
      a3["already_migrated"] is True)


# ======================================================================
print("=" * 60); print("test_reason_lookup_seed_and_uniqueness"); print("=" * 60)
p, cn = fresh_sqlite()
seeds = init_db.seed_classification_reasons(cn)  # already seeded by SCHEMA? no -- SCHEMA only creates table
check("reasons: 8 seeds present",
      cn.execute("SELECT COUNT(*) FROM classification_reason_lookup WHERE is_seed=1").fetchone()[0] == 8)
check("reasons: debt_service among seeds", tags.reason_exists(cn, "debt_service"))
check("reasons: case-insensitive dup detected", tags.reason_exists_ci(cn, "DEPOSIT"))
check("reasons: unknown reason absent", not tags.reason_exists(cn, "totally_new"))
cn.close()


# ======================================================================
print("=" * 60); print("test_liability_detection_and_default_classification"); print("=" * 60)
check("detect: 26110 canonical line is a liability line",
      sync.is_liability_account_line({"gl_account_number_canonical": "26110", "gl_account_number": None}))
check("detect: 26110 parsed-number line is a liability line",
      sync.is_liability_account_line({"gl_account_number_canonical": None, "gl_account_number": "26110"}))
check("detect: ordinary COGS line is NOT a liability line",
      not sync.is_liability_account_line({"gl_account_number_canonical": "56100", "gl_account_number": "56100"}))
check("detect: bill_reduces_liability True when any line hits 26110",
      sync.bill_reduces_liability([{"gl_account_number_canonical": "56100", "gl_account_number": "56100"},
                                   {"gl_account_number_canonical": "26110", "gl_account_number": "26110"}]))

p, cn = fresh_sqlite()
add_bill(cn, "ORD", due="2026-06-15")
created, debt, idd = sync._ensure_metadata(cn, "ORD", None, None, False, "t",
                                           invoice_due_date="2026-06-15", is_debt_service=False)
cn.commit()
m = cn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id='ORD'").fetchone()
check("default: created True / not debt", created and not debt)
check("default: obligation_type ordinary_ap", m["obligation_type"] == "ordinary_ap")
check("default: due_state not_due", m["due_state"] == "not_due")
check("default: invoice_due_date snapshotted", m["invoice_due_date"] == "2026-06-15")
check("default: expected_payment_date defaults to invoice_due_date",
      m["expected_payment_date"] == "2026-06-15")
check("default: reason + note NULL", m["classification_reason"] is None and m["classification_note"] is None)

add_bill(cn, "DEBT", due="2026-06-15")
created2, debt2, _ = sync._ensure_metadata(cn, "DEBT", None, None, False, "t",
                                           invoice_due_date="2026-06-15", is_debt_service=True)
cn.commit()
m2 = cn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id='DEBT'").fetchone()
check("debt: detected flag returned", debt2 is True)
check("debt: obligation_type debt_service", m2["obligation_type"] == "debt_service")
check("debt: due_state not_due", m2["due_state"] == "not_due")
check("debt: reason debt_service", m2["classification_reason"] == "debt_service")
check("debt: classified_at stamped", m2["classified_at"] is not None)
ca = cn.execute("SELECT field,from_value,to_value,changed_by FROM classification_audit "
                "WHERE bill_id='DEBT' ORDER BY id").fetchall()
fields = {r["field"] for r in ca}
check("debt: audit row for obligation_type (system)",
      any(r["field"] == "obligation_type" and r["to_value"] == "debt_service"
          and r["changed_by"] is None for r in ca))
check("debt: audit row for classification_reason", "classification_reason" in fields)
cn.close()


# ======================================================================
print("=" * 60); print("test_debt_service_auto_promote"); print("=" * 60)
p, cn = fresh_sqlite()
add_bill(cn, "DPAST", due=PAST);   add_meta(cn, "DPAST", "debt_service", "not_due", invoice_due=PAST)
add_bill(cn, "DFUT", due=FUTURE);  add_meta(cn, "DFUT", "debt_service", "not_due", invoice_due=FUTURE)
add_bill(cn, "OPAST", due=PAST);   add_meta(cn, "OPAST", "ordinary_ap", "not_due", invoice_due=PAST)
cn.commit()
n = sync.promote_debt_service_due(cn, TODAY, "now")
cn.commit()
check("promote: count == 1 (only the past-due debt bill)", n == 1)
check("promote: past debt_service -> due",
      cn.execute("SELECT due_state FROM bill_metadata WHERE qb_bill_id='DPAST'").fetchone()[0] == "due")
check("promote: future debt_service stays not_due",
      cn.execute("SELECT due_state FROM bill_metadata WHERE qb_bill_id='DFUT'").fetchone()[0] == "not_due")
check("promote: NEGATIVE -- past ordinary_ap NOT promoted (safety gate)",
      cn.execute("SELECT due_state FROM bill_metadata WHERE qb_bill_id='OPAST'").fetchone()[0] == "not_due")
check("promote: audit row written for the promotion",
      cn.execute("SELECT COUNT(*) FROM classification_audit WHERE bill_id='DPAST' "
                 "AND field='due_state' AND to_value='due'").fetchone()[0] == 1)
# idempotent re-run does not re-promote / re-audit
n2 = sync.promote_debt_service_due(cn, TODAY, "now")
cn.commit()
check("promote: idempotent re-run promotes 0", n2 == 0)
check("promote: no duplicate audit row on re-run",
      cn.execute("SELECT COUNT(*) FROM classification_audit WHERE bill_id='DPAST' "
                 "AND field='due_state'").fetchone()[0] == 1)
cn.close()


# ======================================================================
print("=" * 60); print("test_invoice_due_date_sync_audit"); print("=" * 60)
p, cn = fresh_sqlite()
add_bill(cn, "IDD", due="2026-06-15")
sync._ensure_metadata(cn, "IDD", None, None, False, "t",
                      invoice_due_date="2026-06-15", is_debt_service=False)
cn.commit()
# vendor revises the term in QB -> next sync sees a different due date
_c, _d, changed = sync._ensure_metadata(cn, "IDD", None, None, False, "t2",
                                        invoice_due_date="2026-07-01", is_debt_service=False)
cn.commit()
check("idd: sync update flagged changed", changed is True)
check("idd: invoice_due_date updated from QB",
      cn.execute("SELECT invoice_due_date FROM bill_metadata WHERE qb_bill_id='IDD'").fetchone()[0] == "2026-07-01")
check("idd: change audited (system, from->to)",
      cn.execute("SELECT COUNT(*) FROM classification_audit WHERE bill_id='IDD' "
                 "AND field='invoice_due_date' AND from_value='2026-06-15' "
                 "AND to_value='2026-07-01' AND changed_by IS NULL").fetchone()[0] == 1)
# expected_payment_date is NOT auto-followed (CFO owns it)
check("idd: expected_payment_date NOT auto-followed",
      cn.execute("SELECT expected_payment_date FROM bill_metadata WHERE qb_bill_id='IDD'").fetchone()[0] == "2026-06-15")
# same date again -> no-op, no new audit row
_c, _d, changed2 = sync._ensure_metadata(cn, "IDD", None, None, False, "t3",
                                         invoice_due_date="2026-07-01", is_debt_service=False)
cn.commit()
check("idd: unchanged date -> not flagged", changed2 is False)
check("idd: no duplicate audit row",
      cn.execute("SELECT COUNT(*) FROM classification_audit WHERE bill_id='IDD' "
                 "AND field='invoice_due_date'").fetchone()[0] == 1)
cn.close()


# ======================================================================
print("=" * 60); print("test_payrun_fence_predicate"); print("=" * 60)
p, cn = fresh_sqlite()
# four Controller_Reviewed open bills, one per relevant cell
add_bill(cn, "E1"); add_meta(cn, "E1", "ordinary_ap", "due")          # eligible
add_bill(cn, "E2"); add_meta(cn, "E2", "debt_service", "due")         # eligible
add_bill(cn, "X1"); add_meta(cn, "X1", "ordinary_ap", "not_due")      # not_due -> excluded
add_bill(cn, "X2"); add_meta(cn, "X2", "not_real_ap", "not_due")      # not_real -> excluded
cn.commit()
run = cn.execute("INSERT INTO pay_run (name,status,created_at,updated_at) "
                 "VALUES ('R','Draft','t','t')").lastrowid
cn.commit()
cands = {c["qb_bill_id"] for c in payruns.candidate_bills(cn, run)}
check("fence: ordinary_ap+due is a candidate", "E1" in cands)
check("fence: debt_service+due is a candidate", "E2" in cands)
check("fence: ordinary_ap+not_due is EXCLUDED", "X1" not in cands)
check("fence: not_real_ap is EXCLUDED", "X2" not in cands)
check("fence: exactly the two due cells", cands == {"E1", "E2"})

# AP-report asymmetry: debt_service+due is pay-run eligible but NOT in the
# Right-Now-AP report (obligation_type='ordinary_ap' AND due_state='due').
ap_report = {r["qb_bill_id"] for r in cn.execute(
    "SELECT b.qb_bill_id FROM bill b JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id "
    "WHERE m.obligation_type='ordinary_ap' AND m.due_state='due' "
    "AND b.open_balance_cents>0")}
check("asymmetry: ordinary_ap+due IS in the AP report", "E1" in ap_report)
check("asymmetry: debt_service+due is NOT in the AP report (booked as liability)",
      "E2" not in ap_report)
cn.close()


# ======================================================================
# ROUTE TESTS (Flask test client)
# ======================================================================
if not dotenv_values(ROOT / ".env").get("SECRET_KEY"):
    print("\nSKIPPING ROUTE TESTS: no SECRET_KEY in .env")
else:
    PW = generate_password_hash("testpw")
    USERS = [("marilyn", "Marilyn", "ap_clerk"), ("joe", "Joe", "controller"),
             ("shaun", "Shaun", "cfo")]

    def fresh_route_db():
        d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
        db.DB_PATH = d / "p45.db"
        cn = sqlite3.connect(db.DB_PATH)
        cn.executescript(init_db.SCHEMA)
        for u, name, role in USERS:
            cn.execute("INSERT INTO users (username,name,role,password_hash,is_active) "
                       "VALUES (?,?,?,?,1)", (u, name, role, PW))
        cn.commit(); cn.close()
        # seed the reasons the classify route validates against
        cn = sqlite3.connect(db.DB_PATH)
        init_db.seed_classification_reasons(cn)
        cn.close()

    from app import app  # noqa: E402
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["TESTING"] = True

    def client(username):
        c = app.test_client()
        c.post("/login", data={"username": username, "password": "testpw"})
        return c

    def rconn():
        cn = sqlite3.connect(db.DB_PATH); cn.row_factory = sqlite3.Row
        return cn

    def seed_route_bill(bid, obligation="ordinary_ap", due_state="not_due",
                        approval_state="Controller_Reviewed", invoice_due="2026-06-15"):
        cn = rconn()
        cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
                   "open_balance_cents,bill_date,due_date,is_paid,last_synced_at) "
                   "VALUES (?,?,?,?,?,?,?,0,?)",
                   (bid, "V" + bid, "B" + bid, 10000, 10000, "2026-05-01",
                    "2026-06-15", "t"))
        cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,obligation_type,"
                   "due_state,invoice_due_date,expected_payment_date,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (bid, approval_state, obligation, due_state, invoice_due,
                    invoice_due, "t", "t"))
        cn.commit(); cn.close()

    def meta(bid):
        cn = rconn()
        r = cn.execute("SELECT * FROM bill_metadata WHERE qb_bill_id=?", (bid,)).fetchone()
        cn.close()
        return r

    def caudit(bid):
        cn = rconn()
        rows = cn.execute("SELECT * FROM classification_audit WHERE bill_id=? ORDER BY id",
                          (bid,)).fetchall()
        cn.close()
        return rows

    print("=" * 60); print("test_classify_access_model"); print("=" * 60)
    fresh_route_db()
    seed_route_bill("C1", "ordinary_ap", "not_due")
    # ap_clerk cannot change obligation_type (server-side, not just UI)
    client("marilyn").post("/bills/C1/classify",
        data={"obligation_type": "debt_service", "due_state": "not_due",
              "classification_reason": "debt_service"})
    check("access: clerk CANNOT change obligation_type",
          meta("C1")["obligation_type"] == "ordinary_ap")
    # ap_clerk CAN flip due_state (trigger met) with a reason
    client("marilyn").post("/bills/C1/classify",
        data={"obligation_type": "ordinary_ap", "due_state": "due",
              "classification_reason": "waiting_on_inventory"})
    check("access: clerk CAN flip due_state to due", meta("C1")["due_state"] == "due")
    check("access: clerk flip writes a classification_audit row (changed_by set)",
          any(r["field"] == "due_state" and r["to_value"] == "due"
              and r["changed_by"] is not None for r in caudit("C1")))
    # controller CAN change obligation_type (with a reason)
    client("joe").post("/bills/C1/classify",
        data={"obligation_type": "not_real_ap", "due_state": "not_due",
              "classification_reason": "placeholder"})
    check("access: controller CAN change obligation_type",
          meta("C1")["obligation_type"] == "not_real_ap")

    print("=" * 60); print("test_classify_validation"); print("=" * 60)
    fresh_route_db()
    seed_route_bill("C2", "ordinary_ap", "not_due")
    # impossible cell: not_real_ap + due -> coerced to not_due
    client("joe").post("/bills/C2/classify",
        data={"obligation_type": "not_real_ap", "due_state": "due",
              "classification_reason": "placeholder"})
    check("validate: not_real_ap forces due_state=not_due (impossible cell)",
          meta("C2")["obligation_type"] == "not_real_ap" and meta("C2")["due_state"] == "not_due")
    # reason required for a non-default classification
    seed_route_bill("C3", "ordinary_ap", "not_due")
    client("joe").post("/bills/C3/classify",
        data={"obligation_type": "debt_service", "due_state": "not_due"})  # no reason
    check("validate: non-default classification rejected without a reason",
          meta("C3")["obligation_type"] == "ordinary_ap")

    print("=" * 60); print("test_invoice_due_date_locked_from_team"); print("=" * 60)
    fresh_route_db()
    seed_route_bill("C4", "ordinary_ap", "not_due", invoice_due="2026-06-15")
    before = meta("C4")["invoice_due_date"]
    # try to push invoice_due_date through the classify form...
    client("joe").post("/bills/C4/classify",
        data={"obligation_type": "ordinary_ap", "due_state": "not_due",
              "invoice_due_date": "2030-01-01", "expected_payment_date": "2026-07-01"})
    check("locked: classify route ignores posted invoice_due_date",
          meta("C4")["invoice_due_date"] == before == "2026-06-15")
    # ...and through the general metadata save form
    client("joe").post("/bills/C4/metadata",
        data={"classification": "Real", "approver_name": "Joe",
              "approval_channel": "Email", "approval_date": "2026-05-20",
              "invoice_due_date": "2030-01-01"})
    check("locked: save_metadata route ignores posted invoice_due_date",
          meta("C4")["invoice_due_date"] == "2026-06-15")
    check("locked: expected_payment_date IS editable via classify",
          meta("C4")["expected_payment_date"] == "2026-07-01")

    print("=" * 60); print("test_payrun_fence_via_forced_post"); print("=" * 60)
    fresh_route_db()
    seed_route_bill("F1", "ordinary_ap", "due")        # eligible
    seed_route_bill("F2", "debt_service", "due")       # eligible
    seed_route_bill("F3", "ordinary_ap", "not_due")    # excluded
    seed_route_bill("F4", "not_real_ap", "not_due")    # excluded
    cn = rconn()
    run = cn.execute("INSERT INTO pay_run (name,status,created_by,created_at,updated_at) "
                     "VALUES ('R','Draft',2,'t','t')").lastrowid
    cn.commit(); cn.close()
    # forced POST tries to add ALL FOUR bills, including the ineligible ones
    client("joe").post(f"/pay-runs/{run}/lines",
        data={"bill_ids": ["F1", "F2", "F3", "F4"]})
    cn = rconn()
    on_run = {r["qb_bill_id"] for r in cn.execute(
        "SELECT qb_bill_id FROM pay_run_line WHERE pay_run_id=?", (run,))}
    cn.close()
    check("fence(route): only the two due bills landed on the run", on_run == {"F1", "F2"})
    check("fence(route): not_due bill could NOT be forced on", "F3" not in on_run)
    check("fence(route): not_real_ap bill could NOT be forced on", "F4" not in on_run)

    print("=" * 60); print("test_triage_access_and_apply"); print("=" * 60)
    fresh_route_db()
    seed_route_bill("T1", "ordinary_ap", "not_due")  # untriaged (default, no classified_by)
    check("triage: ap_clerk gets 403", client("marilyn").get("/triage").status_code == 403)
    check("triage: controller gets 200", client("joe").get("/triage").status_code == 200)
    # apply a decision -> bill leaves the queue (classified_by set) + audit row
    client("joe").post("/triage/T1",
        data={"obligation_type": "not_real_ap", "due_state": "not_due",
              "classification_reason": "placeholder"})
    check("triage: apply sets obligation_type", meta("T1")["obligation_type"] == "not_real_ap")
    check("triage: apply stamps classified_by (leaves queue)", meta("T1")["classified_by"] is not None)
    check("triage: apply writes a classification_audit row", len(caudit("T1")) >= 1)


# ======================================================================
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
else:
    print("ALL PHASE 4.5 CHECKS PASSED")
sys.exit(len(FAILURES))
