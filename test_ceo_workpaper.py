"""
test_ceo_workpaper.py -- the three-tier CEO payment workpaper.

Plain-script style (mirrors test_picker_diagnostic.py / test_phase_5_export.py):
check(label, cond); exit code == failures; run `python test_ceo_workpaper.py`.

PART A (pure data): payruns.held_and_notdue_tiers() tier assignment on a fixture
  that exercises every path -- eligible-parked, deselected, rejected-this-run,
  rejected-on-another-run, claimed-elsewhere, paid-this-run, not-yet-due, still-
  in-processing (New/AP), not_real_ap, excluded-classification, closed.

(PART B render + PART C routes are added with the xlsx/route layer.)

The live payables.db is never touched.
"""
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
sys.path.insert(0, str(ROOT))
import init_db   # noqa: E402
import bills      # noqa: E402
import payruns    # noqa: E402


def _new_path():
    d = Path(tempfile.mkdtemp()); _TMP.append(d)
    return d / "t.db"


def _conn(p):
    cn = sqlite3.connect(p); cn.row_factory = sqlite3.Row
    return cn


def add_bill(cn, bid, approval="Controller_Reviewed", due="due",
             obligation="ordinary_ap", classification=None, open_bal=10000,
             cat="New Device Purchases", ok_for_ceo=0):
    cn.execute("INSERT INTO bill (qb_bill_id,vendor,bill_number,amount_cents,"
               "open_balance_cents,bill_date,due_date,is_paid,last_synced_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, "V" + bid, "B" + bid, 10000, open_bal, "2026-05-01",
                "2026-06-15", 0 if open_bal > 0 else 1, "t"))
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,app_category,approval_state,"
               "classification,obligation_type,due_state,ok_for_ceo,created_at,"
               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (bid, cat, approval, classification, obligation, due, ok_for_ceo,
                "t", "t"))


def add_line(cn, run_id, bid, included=1, line_state="Pending", method="Check",
             amount=10000, cfo_note=None):
    cn.execute("INSERT INTO pay_run_line (pay_run_id,qb_bill_id,payment_method,"
               "amount_to_pay_cents,included,line_state,cfo_note) "
               "VALUES (?,?,?,?,?,?,?)",
               (run_id, bid, method, amount, included, line_state, cfo_note))


def fixture():
    """run 1 = the workpaper's run; run 2 = another run."""
    p = _new_path(); cn = _conn(p)
    cn.executescript(init_db.SCHEMA)
    for rid in (1, 2):
        cn.execute("INSERT INTO pay_run (id,name,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?)", (rid, "R%d" % rid, "Draft", "t", "t"))
    # eligible, not on any run -> HELD A (parked)
    add_bill(cn, "PARKED", cat="Occupancy")
    # eligible, claimed by run 2 (included, non-rejected) -> NOT held
    add_bill(cn, "CLAIMED_OTHER"); add_line(cn, 2, "CLAIMED_OTHER")
    # eligible, payable on THIS run (claimed) -> tier 1, NOT held
    add_bill(cn, "PAID_THIS"); add_line(cn, 1, "PAID_THIS")
    # eligible, deselected on THIS run (included=0 releases it) -> HELD A
    add_bill(cn, "DESELECTED", cat="Legal Fees"); add_line(cn, 1, "DESELECTED", included=0)
    # eligible, rejected on THIS run -> HELD B (reason = cfo_note)
    add_bill(cn, "REJ_THIS"); add_line(cn, 1, "REJ_THIS", line_state="Rejected",
                                       cfo_note="vendor dispute")
    # eligible, rejected on ANOTHER run -> released -> HELD A
    add_bill(cn, "REJ_OTHER"); add_line(cn, 2, "REJ_OTHER", line_state="Rejected",
                                        cfo_note="other-run reject")
    # reviewed real AP but not yet due -> NOT YET DUE
    add_bill(cn, "NOTDUE", due="not_due", cat="Parts & Products")
    # still in processing -> footnote count only
    add_bill(cn, "NEWBILL", approval="New")
    add_bill(cn, "APREV", approval="AP_Reviewed")
    # never appears anywhere
    add_bill(cn, "NOTREAL", obligation="not_real_ap")
    add_bill(cn, "EXCLCLASS", classification="Refund-Visibility")
    add_bill(cn, "CLOSED", open_bal=0)
    cn.commit()
    return p, cn


print("=" * 60); print("PART A -- tier assignment (pure data)"); print("=" * 60)
p, cn = fixture()
t = payruns.held_and_notdue_tiers(cn, 1)
held_ids = sorted(r["qb_bill_id"] for r in t["held"])
notdue_ids = sorted(r["qb_bill_id"] for r in t["not_yet_due"])

check("held == {PARKED, DESELECTED, REJ_OTHER, REJ_THIS}",
      held_ids == ["DESELECTED", "PARKED", "REJ_OTHER", "REJ_THIS"])
check("paid-on-this-run bill excluded from held", "PAID_THIS" not in held_ids)
check("claimed-by-another-run bill excluded from held", "CLAIMED_OTHER" not in held_ids)
check("rejected-this-run appears exactly once (no A/B double-count)",
      held_ids.count("REJ_THIS") == 1)

by_id = {r["qb_bill_id"]: r for r in t["held"]}
check("REJ_THIS is held_kind=rejected_this_run with its cfo_note reason",
      by_id["REJ_THIS"]["held_kind"] == "rejected_this_run"
      and by_id["REJ_THIS"]["reason"] == "vendor dispute")
check("PARKED is held_kind=eligible_parked with empty reason",
      by_id["PARKED"]["held_kind"] == "eligible_parked" and by_id["PARKED"]["reason"] == "")
check("DESELECTED (included=0) surfaces as eligible_parked",
      by_id["DESELECTED"]["held_kind"] == "eligible_parked")
check("REJ_OTHER (rejected on another run) surfaces as eligible_parked",
      by_id["REJ_OTHER"]["held_kind"] == "eligible_parked")

check("not_yet_due == {NOTDUE}", notdue_ids == ["NOTDUE"])
check("processing_count == 2 (NEWBILL + APREV)", t["processing_count"] == 2)

absent = set(held_ids) | set(notdue_ids)
check("not_real_ap never appears", "NOTREAL" not in absent)
check("excluded-classification never appears", "EXCLCLASS" not in absent)
check("closed (open_balance=0) never appears", "CLOSED" not in absent)
check("New/AP bills are not in held or not_yet_due (footnote only)",
      "NEWBILL" not in absent and "APREV" not in absent)
cn.close()

# ====================================================================
for d in _TMP:
    shutil.rmtree(d, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
else:
    print("ALL CEO-WORKPAPER (PART A) CHECKS PASSED")
sys.exit(len(FAILURES))
