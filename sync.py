"""
sync.py -- warehouse -> payables.db bill sync + categorization.

Pulls open bills (Balance > 0) plus a look-back window of recently-updated
bills from QuickBooksReplica, mirrors them into the local `bill` / `bill_line`
tables, auto-creates `bill_metadata` for new bills, computes `app_category`
via the GL+Class rules engine, and stamps an AP tie-out + data-quality counts
into `audit_log` each run. Read-only against the warehouse; QB stays system of
record.

Design decisions (Phase 1b):
  - LOOKBACK_DAYS is a module constant, not a runtime knob.
  - One transaction PER BILL: a bad bill is caught/counted/logged and the run
    continues; the sync_run audit row commits separately at the end.
  - bill_line is replaced wholesale (delete + reinsert) per bill each sync.
  - Date parse failures quarantine to NULL + bill.date_parse_warning, counted;
    they never block the bill.
  - A module lock prevents the scheduled job and a manual "Pull Now" from
    overlapping.
  - Lines come from reporting.fact_bill_line (unified GL, override-applied);
    headers from dbo.Bill; has_credit_applied from dbo.Bill_LinkedTxn.

See WAREHOUSE_SCHEMA.md for the column mapping and query rationale.
"""

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import warehouse
from db import _connect

LOOKBACK_DAYS = 14
_IN_BATCH = 400                      # SQL Server parameter-count safety
_sync_lock = threading.Lock()

_OPS_RE = re.compile(r"OPS-?\s*0*(\d{3,})", re.IGNORECASE)
_LEADING_DIGITS_RE = re.compile(r"^\s*(\d+)")


# ----------------------------------------------------------------------
# Converters (pure)
# ----------------------------------------------------------------------

def to_cents(v) -> int:
    """Warehouse numeric (e.g. 1369.8900000, '0E-7') -> integer cents."""
    if v is None:
        return 0
    return int((Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_iso_date(v):
    """-> ('YYYY-MM-DD', True) on success; (None, False) if unparseable.
    Accepts datetime/date objects (pyodbc) and ISO-ish strings."""
    if v is None:
        return None, True                 # genuinely absent, not a parse error
    if isinstance(v, datetime):
        return v.date().isoformat(), True
    if hasattr(v, "isoformat") and not isinstance(v, str):   # datetime.date
        return v.isoformat(), True
    s = str(v).strip()
    if not s:
        return None, True
    try:
        return datetime.fromisoformat(s.replace("Z", "")).date().isoformat(), True
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat(), True
        except ValueError:
            return None, False            # quarantine


def to_iso_dt(v):
    """Timestamp -> ISO string ('YYYY-MM-DD HH:MM:SS'), or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    return str(v)


def parse_ops(memo):
    """-> (primary 'OPS-####' or None, comma-joined-all or None)."""
    if not memo:
        return None, None
    seen, out = set(), []
    for m in _OPS_RE.finditer(memo):
        norm = "OPS-" + m.group(1)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    if not out:
        return None, None
    return out[0], ",".join(out)


def parse_gl_number(account_name):
    """'56100 OUTBOUND SHIPPING...' -> '56100'; 'Pre-Owned Device COGS' -> None."""
    if not account_name:
        return None
    m = _LEADING_DIGITS_RE.match(str(account_name))
    return m.group(1) if m else None


def _now_iso():
    return datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")


# ----------------------------------------------------------------------
# Phase 4.5 -- debt-service detection (liability accounts)
# ----------------------------------------------------------------------

# A bill is debt service when ANY of its lines reduces a known liability
# account. Decathlon's note -- GL 26110, "NOTES PAYABLE:Decathlon Alpha IV,
# L.P." -- is the one confirmed liability account in the warehouse (the sole
# NOTES PAYABLE account, and Decathlon-exclusive). Detection runs ONLY on first
# sight (the metadata-create branch), so a human override of obligation_type is
# never re-derived by a later sync. EDIT POINT: add other liability GL account
# numbers here as loans/financed obligations are identified during triage.
# Matched on the canonical account number (reliable for liability lines, which
# -- unlike many COGS lines -- carry a parsed number too) with a fallback to the
# leading-digit parse.
LIABILITY_ACCOUNT_NUMBERS = frozenset({"26110"})


def is_liability_account_line(line):
    """True if a bill_line dict hits a known debt-service liability account."""
    canon = (line.get("gl_account_number_canonical") or "").strip()
    parsed = (line.get("gl_account_number") or "").strip()
    return canon in LIABILITY_ACCOUNT_NUMBERS or parsed in LIABILITY_ACCOUNT_NUMBERS


def bill_reduces_liability(lines):
    """True if ANY line on the bill reduces a known liability account
    (-> default obligation_type='debt_service' on first sight)."""
    return any(is_liability_account_line(ln) for ln in lines)


def log_classification_change(conn, bill_id, field, from_value, to_value,
                              changed_by, now):
    """Append a row to classification_audit (the Phase 4.5 change trail). Every
    classification field change and every sync-driven invoice_due_date update
    goes here. changed_by=None denotes a system/sync-driven change."""
    conn.execute(
        "INSERT INTO classification_audit (bill_id, field, from_value, "
        "to_value, changed_by, changed_at) VALUES (?,?,?,?,?,?)",
        (str(bill_id), field,
         None if from_value is None else str(from_value),
         None if to_value is None else str(to_value),
         changed_by, now))


def promote_debt_service_due(conn, today, now):
    """Auto-promote debt_service bills not_due -> due once their contractual
    invoice_due_date has arrived. Zero-tolerance for lateness: the obligation
    was decided when the loan was signed. ordinary_ap is NEVER promoted on date
    (a human flips it) -- that asymmetry is the phase's key safety gate.

    Runs once per sync pass (covers both the scheduled 15-min job and a manual
    "Pull Now"). Deterministic + idempotent: the due_state='not_due' guard means
    a re-run neither re-promotes nor re-audits an already-due bill. Operates on
    the local bill_metadata directly, so it catches every debt_service bill, not
    only those pulled this run. Returns the count promoted."""
    rows = conn.execute(
        "SELECT qb_bill_id FROM bill_metadata "
        "WHERE obligation_type='debt_service' AND due_state='not_due' "
        "AND invoice_due_date IS NOT NULL AND invoice_due_date <= ?",
        (today,)).fetchall()
    promoted = [r["qb_bill_id"] for r in rows]
    if promoted:
        conn.execute(
            "UPDATE bill_metadata SET due_state='due', classified_by=NULL, "
            "classified_at=? WHERE obligation_type='debt_service' "
            "AND due_state='not_due' AND invoice_due_date IS NOT NULL "
            "AND invoice_due_date <= ?", (now, today))
        for bid in promoted:
            log_classification_change(conn, bid, "due_state", "not_due", "due",
                                      None, now)
    return len(promoted)


# ----------------------------------------------------------------------
# Rules evaluation (pure)
# ----------------------------------------------------------------------

def _like_to_regex(pattern):
    """SQL-LIKE-ish (% and _) -> compiled case-insensitive regex. Translate
    wildcards char-by-char and escape everything else (re.escape does NOT
    escape % or _, so a global replace on the escaped string is unreliable)."""
    parts = []
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def _line_matches(line, rule):
    """Does a bill_line dict match a gl_rule dict?"""
    mt, mv = rule["match_type"], (rule["match_value"] or "")
    acct_num = line.get("gl_account_number")
    acct_name = line.get("gl_account_name") or ""
    cls = line.get("qb_class_name") or ""
    if mt == "gl_account_number":
        return acct_num is not None and acct_num == mv.strip()
    if mt == "gl_account_name_like":
        return bool(_like_to_regex(mv).match(acct_name)) if mv else False
    if mt == "gl_account_path_like":
        # Phase 5: match the canonical rollup path from reporting.dim_account.
        # Same anchored, case-insensitive LIKE as gl_account_name_like. NULL/empty
        # path never matches (a line we couldn't join to dim_account stays uncat).
        path = line.get("gl_account_path") or ""
        return bool(_like_to_regex(mv).match(path)) if (mv and path) else False
    if mt == "class_name":
        if "%" in mv or "_" in mv:
            return bool(_like_to_regex(mv).match(cls))
        return cls.strip().lower() == mv.strip().lower()
    if mt == "gl_and_class":
        acct_part, _, class_part = mv.partition("||")
        acct_ok = (acct_num == acct_part.strip()) or \
                  bool(_like_to_regex(acct_part).match(acct_name)) if acct_part else False
        class_ok = cls.strip().lower() == class_part.strip().lower() if class_part else True
        return acct_ok and class_ok
    return False


def _category_for_line(line, rules):
    """First matching rule (rules already sorted by priority) -> (category, rule_id)."""
    for r in rules:
        if _line_matches(line, r):
            return r["target_category"], r["id"]
    return None, None


def compute_app_category(lines, rules, vendor_default, manual):
    """Return (category, source, breakdown_list).

    Order: manual override -> largest-amount GL+Class-matched line ->
    vendor default -> 'Uncategorized'. breakdown_list aggregates every line by
    its resolved category (matched category or 'Uncategorized') for split
    visibility, regardless of which header category wins.
    """
    # Per-line resolution (for both the winner and the breakdown).
    resolved = []   # (category_or_None, rule_id_or_None, amount_cents)
    for ln in lines:
        cat, rid = _category_for_line(ln, rules)
        resolved.append((cat, rid, ln.get("line_amount_cents") or 0))

    # Breakdown: aggregate by category label (None -> 'Uncategorized').
    agg = {}
    for cat, _rid, amt in resolved:
        label = cat or "Uncategorized"
        e = agg.setdefault(label, {"category": label, "amount_cents": 0, "line_count": 0})
        e["amount_cents"] += amt
        e["line_count"] += 1
    breakdown = sorted(agg.values(), key=lambda e: -abs(e["amount_cents"]))

    if manual:
        return manual, "manual", breakdown

    # Header category: largest-amount line that matched a rule.
    matched = [(cat, rid, amt) for (cat, rid, amt) in resolved if cat]
    if matched:
        cat, rid, _amt = max(matched, key=lambda t: abs(t[2]))
        return cat, f"gl_rule:{rid}", breakdown
    if vendor_default:
        return vendor_default, "vendor_default", breakdown
    return "Uncategorized", "uncategorized", breakdown


# ----------------------------------------------------------------------
# Warehouse fetchers (read-only)
# ----------------------------------------------------------------------

_BILL_COLS = (
    "Id, DocNumber, VendorRefId, VendorRefName, TxnDate, DueDate, "
    "TotalAmt, Balance, PrivateNote, CurrencyRefName, DepartmentRefName, "
    "APAccountRefName, SalesTermRefName, MetaData_CreateTime, MetaData_LastUpdatedTime"
)


def fetch_open_bills(cur):
    cur.execute(f"SELECT {_BILL_COLS} FROM dbo.Bill WHERE Balance > 0")
    return [_bill_row_to_dict(r) for r in cur.fetchall()]


def fetch_recently_updated_bills(cur, since_iso):
    cur.execute(
        f"SELECT {_BILL_COLS} FROM dbo.Bill WHERE MetaData_LastUpdatedTime >= ?",
        (since_iso,),
    )
    return [_bill_row_to_dict(r) for r in cur.fetchall()]


def _bill_row_to_dict(r):
    return {
        "Id": str(r[0]), "DocNumber": r[1], "VendorRefId": r[2],
        "VendorRefName": r[3], "TxnDate": r[4], "DueDate": r[5],
        "TotalAmt": r[6], "Balance": r[7], "PrivateNote": r[8],
        "CurrencyRefName": r[9], "DepartmentRefName": r[10],
        "APAccountRefName": r[11], "SalesTermRefName": r[12],
        "MetaData_CreateTime": r[13], "MetaData_LastUpdatedTime": r[14],
    }


def load_dim_accounts(cur):
    """One-shot {account_id: (number, path)} from reporting.dim_account.

    Phase 5: the rollup rules match on the account's financial-statement path,
    and fact_bill_line delivers COGS lines mostly name-only (the parsed
    gl_account_number is NULL for most of them). dim_account, joined by
    distribution_account_id, gives the *canonical* number and rollup path for
    every line. Read-only reference data; loaded once per sync run."""
    cur.execute(
        "SELECT account_id, account_number, account_path FROM reporting.dim_account")
    return {str(aid): (num, path) for aid, num, path in cur.fetchall()}


def fetch_bill_lines(cur, bill_ids, dim=None):
    """-> {bill_id: [line_dict, ...]} from reporting.fact_bill_line, each line
    stamped with the canonical account number + rollup path from dim_account
    (joined by distribution_account_id). `dim` is loaded once if not supplied."""
    if dim is None:
        dim = load_dim_accounts(cur)
    out = {}
    for batch in _batches(bill_ids, _IN_BATCH):
        ph = ",".join(["?"] * len(batch))
        cur.execute(
            f"""
            SELECT transaction_id, line_number, line_id, detail_type,
                   line_description, line_amount, item_id, item_name,
                   class_id, class_name, distribution_account_id,
                   distribution_account_name, jira_epic_id
            FROM reporting.fact_bill_line
            WHERE transaction_type = 'Bill' AND transaction_id IN ({ph})
            """,
            tuple(batch),
        )
        for r in cur.fetchall():
            bid = str(r[0])
            acct_name = r[11]
            acct_id = r[10]
            canon_num, acct_path = dim.get(str(acct_id), (None, None)) \
                if acct_id is not None else (None, None)
            out.setdefault(bid, []).append({
                "line_number": int(r[1]) if r[1] is not None else 0,
                "qb_line_id": r[2],
                "detail_type": r[3],
                "line_description": r[4],
                "line_amount_cents": to_cents(r[5]),
                "item_id": r[6],
                "item_name": r[7],
                "qb_class_id": r[8],
                "qb_class_name": r[9],
                "gl_account_id": r[10],
                "gl_account_name": acct_name,
                "gl_account_number": parse_gl_number(acct_name),
                "gl_account_number_canonical": canon_num,
                "gl_account_path": acct_path,
                "jira_epic_id": (r[12] or None),
            })
    return out


def fetch_credit_linked_bill_ids(cur, bill_ids):
    """-> set of bill ids that have a VendorCredit linked txn."""
    found = set()
    for batch in _batches(bill_ids, _IN_BATCH):
        ph = ",".join(["?"] * len(batch))
        cur.execute(
            f"""
            SELECT DISTINCT Bill_Id FROM dbo.Bill_LinkedTxn
            WHERE TxnType LIKE 'VendorCredit%' AND Bill_Id IN ({ph})
            """,
            tuple(batch),
        )
        found.update(str(r[0]) for r in cur.fetchall())
    return found


def ap_tie_out(cur):
    """-> (open_bill_count, open_ap_total_cents). Gross SUM(Balance) > 0."""
    cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(Balance), 0) FROM dbo.Bill WHERE Balance > 0"
    )
    cnt, total = cur.fetchone()
    return int(cnt), to_cents(total)


def _batches(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ----------------------------------------------------------------------
# Local KPI computation (Total / Current / Real-payable open AP)
# ----------------------------------------------------------------------

def compute_kpis(conn, extra_where="", extra_params=()):
    """Open-AP KPIs from the local mirror, computed live.

      total    = open bills (open_balance_cents > 0)
      current  = total AND bill_date <= today  (ties to QB's AP aging)
      real     = current AND classification IN ('Real', NULL)

    extra_where/extra_params let /bills scope the KPI bar to the active filter
    (the caller passes the non-status filters; open is always enforced here).
    Returns {total_cents,total_n, current_cents,current_n, real_cents,real_n}.
    """
    today = date.today().isoformat()
    where = "b.open_balance_cents > 0"
    if extra_where:
        where += " AND " + extra_where
    cur_pred = "b.bill_date IS NOT NULL AND b.bill_date <= ?"
    real_pred = cur_pred + " AND (m.classification='Real' OR m.classification IS NULL)"
    row = conn.execute(
        f"""
        SELECT COUNT(*),
               COALESCE(SUM(b.open_balance_cents),0),
               COUNT(CASE WHEN {cur_pred} THEN 1 END),
               COALESCE(SUM(CASE WHEN {cur_pred} THEN b.open_balance_cents ELSE 0 END),0),
               COUNT(CASE WHEN {real_pred} THEN 1 END),
               COALESCE(SUM(CASE WHEN {real_pred} THEN b.open_balance_cents ELSE 0 END),0)
        FROM bill b LEFT JOIN bill_metadata m ON m.qb_bill_id = b.qb_bill_id
        WHERE {where}
        """,
        (today, today, today, today, *extra_params),
    ).fetchone()
    return {"total_n": row[0], "total_cents": row[1],
            "current_n": row[2], "current_cents": row[3],
            "real_n": row[4], "real_cents": row[5]}


def recompute_for_bill(conn, bill_id):
    """Recompute one bill's app_category (used after a manual-override change).
    Respects the manual override. Returns (category, source)."""
    rules = _load_rules(conn)
    vendor_defaults = _load_vendor_defaults(conn)
    b = conn.execute(
        "SELECT b.vendor_ref, m.app_category_manual FROM bill b "
        "JOIN bill_metadata m ON m.qb_bill_id=b.qb_bill_id WHERE b.qb_bill_id=?",
        (bill_id,)).fetchone()
    if not b:
        return None, None
    lines = [dict(r) for r in conn.execute(
        "SELECT line_amount_cents, gl_account_number, gl_account_number_canonical, "
        "gl_account_name, gl_account_path, qb_class_name "
        "FROM bill_line WHERE qb_bill_id=?", (bill_id,))]
    cat, src, breakdown = compute_app_category(
        lines, rules, vendor_defaults.get(b["vendor_ref"]), b["app_category_manual"])
    _store_category(conn, bill_id, cat, src, breakdown, _now_iso())
    return cat, src


# ----------------------------------------------------------------------
# Local writes
# ----------------------------------------------------------------------

def log_audit(conn, user_id, entity_type, entity_id, action, before, after):
    conn.execute(
        "INSERT INTO audit_log (user_id, entity_type, entity_id, action, "
        "before, after, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, entity_type, str(entity_id) if entity_id is not None else None,
         action,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None,
         _now_iso()),
    )


def _load_rules(conn):
    rows = conn.execute(
        "SELECT id, match_type, match_value, target_category, priority "
        "FROM gl_rule WHERE active = 1 ORDER BY priority ASC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _load_vendor_defaults(conn):
    rows = conn.execute(
        "SELECT vendor_id, default_category FROM vendor_category_default "
        "WHERE active = 1"
    ).fetchall()
    return {r["vendor_id"]: r["default_category"] for r in rows}


def _upsert_bill(conn, b, now):
    """Insert/update the read-only mirror. Returns (op, prev_open_cents)."""
    prev = conn.execute(
        "SELECT open_balance_cents FROM bill WHERE qb_bill_id = ?", (b["qb_bill_id"],)
    ).fetchone()
    prev_open = prev["open_balance_cents"] if prev else None
    conn.execute(
        """
        INSERT INTO bill (qb_bill_id, bill_number, vendor_ref, vendor,
            bill_date, due_date, amount_cents, open_balance_cents, qb_memo,
            currency, department, ap_account, sales_term, qb_created_at,
            qb_updated_at, is_paid, date_parse_warning, last_synced_at)
        VALUES (:qb_bill_id,:bill_number,:vendor_ref,:vendor,:bill_date,
            :due_date,:amount_cents,:open_balance_cents,:qb_memo,:currency,
            :department,:ap_account,:sales_term,:qb_created_at,:qb_updated_at,
            :is_paid,:date_parse_warning,:last_synced_at)
        ON CONFLICT(qb_bill_id) DO UPDATE SET
            bill_number=excluded.bill_number, vendor_ref=excluded.vendor_ref,
            vendor=excluded.vendor, bill_date=excluded.bill_date,
            due_date=excluded.due_date, amount_cents=excluded.amount_cents,
            open_balance_cents=excluded.open_balance_cents,
            qb_memo=excluded.qb_memo, currency=excluded.currency,
            department=excluded.department, ap_account=excluded.ap_account,
            sales_term=excluded.sales_term, qb_created_at=excluded.qb_created_at,
            qb_updated_at=excluded.qb_updated_at, is_paid=excluded.is_paid,
            date_parse_warning=excluded.date_parse_warning,
            last_synced_at=excluded.last_synced_at
        """,
        b,
    )
    return ("inserted" if prev is None else "updated"), prev_open


def _replace_lines(conn, bill_id, lines):
    conn.execute("DELETE FROM bill_line WHERE qb_bill_id = ?", (bill_id,))
    for ln in lines:
        conn.execute(
            "INSERT INTO bill_line (qb_bill_id, line_number, qb_line_id, "
            "detail_type, line_description, line_amount_cents, gl_account_id, "
            "gl_account_name, gl_account_number, gl_account_number_canonical, "
            "gl_account_path, qb_class_id, qb_class_name, "
            "item_id, item_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bill_id, ln["line_number"], ln["qb_line_id"], ln["detail_type"],
             ln["line_description"], ln["line_amount_cents"], ln["gl_account_id"],
             ln["gl_account_name"], ln["gl_account_number"],
             ln.get("gl_account_number_canonical"), ln.get("gl_account_path"),
             ln["qb_class_id"], ln["qb_class_name"], ln["item_id"], ln["item_name"]),
        )


def _ensure_metadata(conn, bill_id, ops_primary, ops_all, has_credit, now,
                     invoice_due_date=None, is_debt_service=False):
    """Create metadata for a new bill; else refresh derived-only fields.
    Never touches user-entered fields.

    Phase 4.5:
      - First sight: snapshot invoice_due_date (QB-locked) and default
        expected_payment_date to it; if the bill reduces a known liability
        account, default obligation_type='debt_service' / reason='debt_service'
        (audited). The NOT-NULL column defaults already set ordinary_ap/not_due.
      - Later syncs: invoice_due_date is updated ONLY here (from QB), never from
        a team edit; any change is written to classification_audit.

    Returns (created, debt_service_detected, invoice_due_date_changed)."""
    exists = conn.execute(
        "SELECT invoice_due_date FROM bill_metadata WHERE qb_bill_id = ?",
        (bill_id,)).fetchone()
    if exists:
        conn.execute(
            "UPDATE bill_metadata SET ops_number=?, ops_numbers_all=?, "
            "has_credit_applied=?, updated_at=? WHERE qb_bill_id=?",
            (ops_primary, ops_all, 1 if has_credit else 0, now, bill_id),
        )
        old_idd = exists["invoice_due_date"]
        idd_changed = False
        if invoice_due_date != old_idd:
            # QB-locked contractual date: reflect a vendor-revised term (or the
            # first post-migration capture), audited; team edits can never reach
            # this column (no form path writes it).
            conn.execute(
                "UPDATE bill_metadata SET invoice_due_date=?, updated_at=? "
                "WHERE qb_bill_id=?", (invoice_due_date, now, bill_id))
            log_classification_change(conn, bill_id, "invoice_due_date",
                                      old_idd, invoice_due_date, None, now)
            idd_changed = True
        return False, False, idd_changed

    if is_debt_service:
        conn.execute(
            "INSERT INTO bill_metadata (qb_bill_id, ops_number, ops_numbers_all, "
            "has_credit_applied, approval_state, invoice_due_date, "
            "expected_payment_date, obligation_type, due_state, "
            "classification_reason, classified_at, created_at, updated_at) "
            "VALUES (?,?,?,?, 'New', ?, ?, 'debt_service', 'not_due', "
            "'debt_service', ?, ?, ?)",
            (bill_id, ops_primary, ops_all, 1 if has_credit else 0,
             invoice_due_date, invoice_due_date, now, now, now))
        log_classification_change(conn, bill_id, "obligation_type",
                                  "ordinary_ap", "debt_service", None, now)
        log_classification_change(conn, bill_id, "classification_reason",
                                  None, "debt_service", None, now)
        return True, True, False

    conn.execute(
        "INSERT INTO bill_metadata (qb_bill_id, ops_number, ops_numbers_all, "
        "has_credit_applied, approval_state, invoice_due_date, "
        "expected_payment_date, created_at, updated_at) "
        "VALUES (?,?,?,?, 'New', ?, ?, ?, ?)",
        (bill_id, ops_primary, ops_all, 1 if has_credit else 0,
         invoice_due_date, invoice_due_date, now, now),
    )
    return True, False, False


def _store_category(conn, bill_id, category, source, breakdown, now):
    conn.execute(
        "UPDATE bill_metadata SET app_category=?, app_category_source=?, "
        "app_category_breakdown=?, updated_at=? WHERE qb_bill_id=?",
        (category, source, json.dumps(breakdown), now, bill_id),
    )


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------

def run_sync(trigger="scheduled", user_id=None):
    """Full sync. Returns a summary dict (also written to audit_log)."""
    if not _sync_lock.acquire(blocking=False):
        return {"status": "skipped_locked", "trigger": trigger}

    started = time.perf_counter()
    started_iso = _now_iso()
    counts = {
        "pulled": 0, "inserted": 0, "updated": 0, "metadata_created": 0,
        "marked_paid": 0, "lines_upserted": 0, "date_parse_warnings": 0,
        "ops_jira_mismatch_warnings": 0, "errors": 0,
        "open_bill_count": 0, "open_ap_total_cents": 0,
        "debt_service_detected": 0, "invoice_due_date_synced": 0,
        "debt_service_promoted": 0,
    }
    az = None
    conn = None
    try:
        try:
            az = warehouse.connect_azure()
        except warehouse.WarehouseError as e:
            conn = _connect()
            log_audit(conn, user_id, "sync", None, "sync_run", None,
                      {"status": "error", "error": str(e), "trigger": trigger,
                       "started_at": started_iso})
            conn.commit()
            return {"status": "error", "error": str(e), "trigger": trigger}

        conn = _connect()
        rules = _load_rules(conn)
        vendor_defaults = _load_vendor_defaults(conn)

        cur = az.cursor()
        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        bills = {}
        for b in fetch_open_bills(cur):
            bills[b["Id"]] = b
        for b in fetch_recently_updated_bills(cur, since):
            bills.setdefault(b["Id"], b)
        bill_ids = list(bills.keys())
        counts["pulled"] = len(bill_ids)

        lines_by_bill = fetch_bill_lines(cur, bill_ids) if bill_ids else {}
        credit_ids = fetch_credit_linked_bill_ids(cur, bill_ids) if bill_ids else set()

        for bid, raw in bills.items():
            now = _now_iso()
            try:
                bdate, ok1 = to_iso_date(raw["TxnDate"])
                ddate, ok2 = to_iso_date(raw["DueDate"])
                date_warn = 0 if (ok1 and ok2) else 1
                open_cents = to_cents(raw["Balance"])
                ops_primary, ops_all = parse_ops(raw["PrivateNote"])
                lines = lines_by_bill.get(bid, [])
                has_credit = bid in credit_ids

                brow = {
                    "qb_bill_id": bid,
                    "bill_number": raw["DocNumber"],
                    "vendor_ref": raw["VendorRefId"],
                    "vendor": raw["VendorRefName"],
                    "bill_date": bdate,
                    "due_date": ddate,
                    "amount_cents": to_cents(raw["TotalAmt"]),
                    "open_balance_cents": open_cents,
                    "qb_memo": raw["PrivateNote"],
                    "currency": raw["CurrencyRefName"],
                    "department": raw["DepartmentRefName"],
                    "ap_account": raw["APAccountRefName"],
                    "sales_term": raw["SalesTermRefName"],
                    "qb_created_at": to_iso_dt(raw["MetaData_CreateTime"]),
                    "qb_updated_at": to_iso_dt(raw["MetaData_LastUpdatedTime"]),
                    "is_paid": 1 if open_cents == 0 else 0,
                    "date_parse_warning": date_warn,
                    "last_synced_at": now,
                }

                op, prev_open = _upsert_bill(conn, brow, now)
                _replace_lines(conn, bid, lines)
                is_debt = bill_reduces_liability(lines)
                created, debt_detected, idd_changed = _ensure_metadata(
                    conn, bid, ops_primary, ops_all, has_credit, now,
                    invoice_due_date=ddate, is_debt_service=is_debt)
                # category respects an existing manual override
                man = conn.execute(
                    "SELECT app_category_manual FROM bill_metadata WHERE qb_bill_id=?",
                    (bid,)).fetchone()
                manual = man["app_category_manual"] if man else None
                cat, src, breakdown = compute_app_category(
                    lines, rules, vendor_defaults.get(raw["VendorRefId"]), manual)
                _store_category(conn, bid, cat, src, breakdown, now)

                # OPS vs jira_epic_id cross-check (warn only)
                jira = next((ln["jira_epic_id"] for ln in lines
                             if ln.get("jira_epic_id")), None)
                if jira and ops_primary and _norm_ops(jira) != ops_primary:
                    counts["ops_jira_mismatch_warnings"] += 1
                    log_audit(conn, None, "bill", bid, "ops_jira_mismatch", None,
                              {"memo_ops": ops_primary, "jira_epic_id": jira,
                               "vendor": raw["VendorRefName"]})

                conn.commit()

                counts[op] += 1
                counts["lines_upserted"] += len(lines)
                if created:
                    counts["metadata_created"] += 1
                if debt_detected:
                    counts["debt_service_detected"] += 1
                if idd_changed:
                    counts["invoice_due_date_synced"] += 1
                if date_warn:
                    counts["date_parse_warnings"] += 1
                if prev_open is not None and prev_open > 0 and open_cents == 0:
                    counts["marked_paid"] += 1
            except Exception as e:       # one bad bill must not abort the run
                conn.rollback()
                counts["errors"] += 1
                try:
                    log_audit(conn, None, "bill", bid, "sync_error", None,
                              {"error": f"{type(e).__name__}: {e}"})
                    conn.commit()
                except Exception:
                    conn.rollback()

        # Phase 4.5: auto-promote debt_service not_due -> due on invoice_due_date.
        # Runs once per pass over the local mirror (covers scheduled + manual),
        # so it catches every debt_service bill regardless of which were pulled.
        promo_now = _now_iso()
        counts["debt_service_promoted"] = promote_debt_service_due(
            conn, date.today().isoformat(), promo_now)
        conn.commit()

        cnt, total_cents = ap_tie_out(cur)
        counts["open_bill_count"] = cnt
        counts["open_ap_total_cents"] = total_cents
        # local KPI split (Current ties to QB aging; Real strips prepay/refund)
        k = compute_kpis(conn)
        counts["open_ap_current_cents"] = k["current_cents"]
        counts["open_ap_current_count"] = k["current_n"]
        counts["open_ap_real_cents"] = k["real_cents"]
        counts["open_ap_real_count"] = k["real_n"]

        counts["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        counts["trigger"] = trigger
        counts["started_at"] = started_iso
        counts["finished_at"] = _now_iso()
        counts["status"] = "ok"
        log_audit(conn, user_id, "sync", None, "sync_run", None, counts)
        conn.commit()
        return counts
    finally:
        if az is not None:
            try:
                az.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _sync_lock.release()


def _norm_ops(jira):
    """Normalize a jira_epic_id to OPS-#### form for comparison, if possible."""
    p, _ = parse_ops(str(jira))
    return p or str(jira).strip()


# ----------------------------------------------------------------------
# Recompute pipeline (rule / vendor-default changes)
# ----------------------------------------------------------------------

def recompute_all(conn=None):
    """Recompute app_category for every locally-stored bill. Returns a summary.
    Used by /admin/rules after rule or vendor-default changes."""
    own = conn is None
    if own:
        conn = _connect()
    try:
        rules = _load_rules(conn)
        vendor_defaults = _load_vendor_defaults(conn)
        bills = conn.execute(
            "SELECT b.qb_bill_id, b.vendor_ref, m.app_category, m.app_category_manual "
            "FROM bill b JOIN bill_metadata m ON m.qb_bill_id = b.qb_bill_id"
        ).fetchall()
        n_changed = n_uncat = 0
        now = _now_iso()
        for b in bills:
            bid = b["qb_bill_id"]
            lines = [dict(r) for r in conn.execute(
                "SELECT line_amount_cents, gl_account_number, gl_account_number_canonical, "
                "gl_account_name, gl_account_path, qb_class_name "
                "FROM bill_line WHERE qb_bill_id=?", (bid,))]
            cat, src, breakdown = compute_app_category(
                lines, rules, vendor_defaults.get(b["vendor_ref"]),
                b["app_category_manual"])
            if cat != b["app_category"]:
                n_changed += 1
            if cat == "Uncategorized":
                n_uncat += 1
            _store_category(conn, bid, cat, src, breakdown, now)
        conn.commit()
        return {"bills": len(bills), "changed": n_changed, "uncategorized": n_uncat}
    finally:
        if own:
            conn.close()
