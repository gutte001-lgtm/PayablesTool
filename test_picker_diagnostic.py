"""
test_picker_diagnostic.py -- the Draft pay-run picker diagnostic + proof that
the _fence_gates() refactor left candidate_bills behavior unchanged.

Plain-Python style (no pytest): check(label, cond); exit code == failures.
PURE (sqlite + payruns helpers, throwaway temp DBs) plus one acceptance check
against a COPY of the live payables.db. The live DB is NEVER opened for write.

Covers:
  * Equivalence: candidate_bills() returns the SAME bill set as the verbatim
    pre-refactor fence SQL (the byte-identical-behavior proof), on a fixture
    that exercises every gate AND on a copy of live data.
  * Drift guard: the composed gate predicate equals the documented string.
  * Diagnostic: per-gate FAIL counts over the OPEN-bill universe are correct,
    counts are independent, zero-count gates are omitted, claimed-by-another-run
    is counted.
  * Acceptance (live copy): open_total=257, approval=257, due=257, and no other
    reason -- matching the verified live breakdown.
"""
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
import init_db   # noqa: E402
import bills     # noqa: E402
import payruns   # noqa: E402

# Verbatim pre-refactor fence (the SQL candidate_bills used before _fence_gates).
# The equivalence test asserts the refactored candidate_bills returns the same
# rows this produces (after the same Python claimed/on-this-run filtering).
_ORIGINAL_FENCE_SQL = (
    "SELECT b.qb_bill_id FROM bill b JOIN bill_metadata m "
    "ON m.qb_bill_id=b.qb_bill_id "
    "WHERE m.approval_state='Controller_Reviewed' AND b.open_balance_cents>0 "
    "  AND (m.classification IS NULL OR m.classification NOT IN (?,?)) "
    "  AND m.due_state='due' "
    "  AND m.obligation_type IN ('ordinary_ap','debt_service') "
    "ORDER BY b.vendor, b.bill_number")


def _reference_candidates(conn, run_id):
    """Old candidate_bills logic: original SQL + the same Python-side filters."""
    rows = conn.execute(_ORIGINAL_FENCE_SQL, tuple(bills.CEO_EXCLUDED)).fetchall()
    claimed = payruns.claimed_bill_ids(conn)
    on_this_run = {r["qb_bill_id"] for r in conn.execute(
        "SELECT qb_bill_id FROM pay_run_line WHERE pay_run_id=?", (run_id,))}
    return [r["qb_bill_id"] for r in rows
            if r["qb_bill_id"] not in claimed and r["qb_bill_id"] not in on_this_run]


def _new_path():
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    return d / "t.db"


def _conn(p):
    cn = sqlite3.connect(p); cn.row_factory = sqlite3.Row
    return cn


def add_bill(cn, bid, approval="Controller_Reviewed", due="due",
             obligation="ordinary_ap", classification=None, open_bal=10000):
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
               "open_balance_cents,bill_date,due_date,is_paid,last_synced_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, "V" + bid, "B" + bid, 10000, open_bal, "2026-05-01",
                "2026-06-15", 0 if open_bal > 0 else 1, "t"))
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,classification,"
               "obligation_type,due_state,created_at,updated_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (bid, approval, classification, obligation, due, "t", "t"))


def fixture():
    """A DB exercising every gate. Draft run id=1; a second run id=2 claims one
    eligible bill so the claimed-elsewhere path is covered."""
    p = _new_path(); cn = _conn(p)
    cn.executescript(init_db.SCHEMA)
    cn.execute("INSERT INTO pay_run (id,name,status,created_at,updated_at) "
               "VALUES (1,'R1','Draft','t','t')")
    cn.execute("INSERT INTO pay_run (id,name,status,created_at,updated_at) "
               "VALUES (2,'R2','Draft','t','t')")
    # eligible
    add_bill(cn, "ELIG")
    add_bill(cn, "ELIG2", classification="Real", obligation="debt_service")
    # gate failures
    add_bill(cn, "NEW", approval="New")
    add_bill(cn, "APREV", approval="AP_Reviewed")
    add_bill(cn, "NOTDUE", due="not_due")
    add_bill(cn, "EXCL", classification="Refund-Visibility")
    add_bill(cn, "NOTREAL", obligation="not_real_ap")          # not_real_ap, due forced
    add_bill(cn, "CLOSED", open_bal=0)                          # excluded from universe
    # claimed by another run (run 2) -> eligible by SQL, filtered in Python
    add_bill(cn, "CLAIMED")
    cn.execute("INSERT INTO pay_run_line (pay_run_id,qb_bill_id,included,line_state,"
               "amount_to_pay_cents) VALUES (2,'CLAIMED',1,'Pending',10000)")
    cn.commit()
    return p, cn


# --------------------------------------------------------------------------
print("=" * 60); print("drift guard: composed predicate is the documented one"); print("=" * 60)
fence_sql = " AND ".join(g[2] for g in payruns._fence_gates())
EXPECTED_FENCE = ("m.approval_state = 'Controller_Reviewed' AND "
                  "(m.classification IS NULL OR m.classification NOT IN (?,?)) AND "
                  "m.due_state = 'due' AND "
                  "m.obligation_type IN ('ordinary_ap', 'debt_service')")
check("composed fence predicate matches the documented string", fence_sql == EXPECTED_FENCE)
check("classification gate binds exactly the CEO_EXCLUDED params",
      tuple(p for g in payruns._fence_gates() for p in g[3]) == tuple(bills.CEO_EXCLUDED))

# --------------------------------------------------------------------------
print("=" * 60); print("equivalence: candidate_bills == pre-refactor SQL (fixture)"); print("=" * 60)
p, cn = fixture()
new_ids = sorted(c["qb_bill_id"] for c in payruns.candidate_bills(cn, 1))
ref_ids = sorted(_reference_candidates(cn, 1))
check("candidate_bills returns exactly {ELIG, ELIG2}", new_ids == ["ELIG", "ELIG2"])
check("candidate_bills == verbatim pre-refactor fence (same set)", new_ids == ref_ids)
check("CLAIMED excluded (claimed by run 2)", "CLAIMED" not in new_ids)
cn.close()

# --------------------------------------------------------------------------
print("=" * 60); print("diagnostic: per-gate FAIL counts over OPEN bills (fixture)"); print("=" * 60)
p, cn = fixture()
diag = payruns.picker_diagnostic(cn, 1)
by = {r["key"]: r["count"] for r in diag["reasons"]}
check("open_total excludes CLOSED (8 open of 9 bills)", diag["open_total"] == 8)
check("approval fail = 2 (NEW + APREV)", by.get("approval") == 2)
check("classification fail = 1 (EXCL)", by.get("classification") == 1)
check("due fail = 1 (NOTDUE)", by.get("due") == 1)
check("obligation fail = 1 (NOTREAL)", by.get("obligation") == 1)
check("claimed-elsewhere = 1 (CLAIMED)", by.get("claimed") == 1)
check("zero-count gates are omitted (no key with count 0)",
      all(r["count"] > 0 for r in diag["reasons"]))
cn.close()

# --------------------------------------------------------------------------
print("=" * 60); print("diagnostic: counts drop as a bill clears gates"); print("=" * 60)
p, cn = fixture()
# Promote NEW through both gates -> approval fail and (it was already due) should drop by 1
cn.execute("UPDATE bill_metadata SET approval_state='Controller_Reviewed' WHERE qb_bill_id='NEW'")
cn.commit()
by2 = {r["key"]: r["count"] for r in payruns.picker_diagnostic(cn, 1)["reasons"]}
check("after promoting NEW: approval fail drops 2 -> 1", by2.get("approval") == 1)
check("after promoting NEW: it is now a candidate",
      "NEW" in [c["qb_bill_id"] for c in payruns.candidate_bills(cn, 1)])
cn.close()

# --------------------------------------------------------------------------
print("=" * 60); print("ACCEPTANCE: live copy (read-only) matches verified breakdown"); print("=" * 60)
live = ROOT / "payables.db"
if live.exists():
    copy = _new_path()
    shutil.copy(live, copy)
    cn = _conn(copy)
    run = cn.execute("SELECT id FROM pay_run WHERE status='Draft' ORDER BY id LIMIT 1").fetchone()
    run_id = run["id"] if run else 1
    diag = payruns.picker_diagnostic(cn, run_id)
    by = {r["key"]: r["count"] for r in diag["reasons"]}
    cand = payruns.candidate_bills(cn, run_id)
    # equivalence holds on real data too
    check("live copy: candidate_bills == pre-refactor fence",
          sorted(c["qb_bill_id"] for c in cand) == sorted(_reference_candidates(cn, run_id)))
    check("live copy: 0 eligible candidates", len(cand) == 0)
    check("live copy: open_total == 257", diag["open_total"] == 257)
    check("live copy: not-Controller-reviewed == 257", by.get("approval") == 257)
    check("live copy: not-due == 257", by.get("due") == 257)
    check("live copy: no excluded-classification reason", "classification" not in by)
    check("live copy: no not_real_ap reason", "obligation" not in by)
    check("live copy: no claimed reason", "claimed" not in by)
    cn.close()
else:
    print("   (skipped: payables.db not present)")

# --------------------------------------------------------------------------
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(len(FAILURES))
print("ALL PASS")
sys.exit(0)
