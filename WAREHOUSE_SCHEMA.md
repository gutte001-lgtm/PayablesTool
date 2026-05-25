# WAREHOUSE_SCHEMA.md — PayablesTool ↔ QuickBooksReplica

Authored from the read-only discovery run
`exports/payables_warehouse_discovery_20260522_141232.txt`
(script `explore_payables_warehouse.py` v1.0, run 2026-05-22 against
`quickbooks-sq1.database.windows.net / QuickBooksReplica`).

`QuickBooksReplica` is a **Skyvia-synced full QuickBooks replica**. Every raw
table carries a `_skyvia_sync` column; the curated `reporting.*` layer carries
`replicated_at` / `created_on` / `updated_at`. Bill data was live at run time
(`dbo.Bill.MetaData_LastUpdatedTime` max = 2026-05-22 19:53).

> **Categorization context (the design driver).** The "Vendor Type" column in
> the weekly Excel is **hand-typed by the AP team** — there is no source of
> truth for it anywhere. Automating it is a primary v1 goal. The warehouse has
> **no vendor_type field** (`dbo.Vendor` and `reporting.dim_vendor` confirm
> this), but it **does** expose a line-level GL account and Class. So
> categorization is driven by a **GL+Class rules engine** (§4) — the former
> "Phase 7" work is now part of the Phase 1b design.

---

## 0. Bill-relevant objects found

### Raw QB mirror (`dbo`)
| Object | Rows | Role | Freshness column |
|---|---|---|---|
| `dbo.Bill` | 28,267 | **Bill header** (the only place with `Balance` + `DueDate`) | `MetaData_LastUpdatedTime` |
| `dbo.Bill_Line` | 109,461 | Bill lines, **two detail shapes** (Account- vs Item-based) | `_skyvia_sync` |
| `dbo.Bill_LinkedTxn` | 52,189 | Links a bill → payments/POs/credits (`TxnType`) | `_skyvia_sync` |
| `dbo.BillPayment` | 25,824 | Payment header (`PayType`, `TotalAmt`) | `MetaData_LastUpdatedTime` |
| `dbo.BillPayment_Line` | 29,922 | Amount applied per payment line | `_skyvia_sync` |
| `dbo.BillPayment_Line_LinkedTxn` | 29,922 | **Payment→Bill bridge** (`TxnType='Bill'`, `TxnId=Bill.Id`) | `_skyvia_sync` |
| `dbo.Vendor` | 7,087 | Vendor master — **no type/category column** | `MetaData_LastUpdatedTime` |
| `dbo.VendorCredit`, `dbo.VendorCredit_Line` | — | Vendor credits (source for `has_credit_applied`) | `_skyvia_sync` |

### Curated layer (`reporting`)
| Object | Rows | Role |
|---|---|---|
| `reporting.fact_bill_line` (VIEW) | 109,461 | Bill lines **with both shapes unified** into one `distribution_account_name` + `class_name`, **GL overrides already applied**, vendor resolved, `jira_epic_id` column |
| `reporting.bill_line_gl_override` (TABLE) | 9 | Boss's manual GL fix: `(transaction_id, item_id) → expense_account_name`. `fact_bill_line` applies it. |
| `reporting.fact_billpayment_entry` (VIEW) | 51,648 | Bill-payment GL entries (`entry_role` = Cash/Bank vs AP), signed `line_amount` |
| `reporting.vw_bill_payment_by_gl` (VIEW) | 18,075 | Bill payments by GL account |
| `reporting.dim_vendor` (VIEW) | 7,084 | Clean vendor dim (`vendor_id`, `vendor_name`, `is_active`) — **no type** |

There is **no curated bill-header view** — nothing in `reporting.*` exposes
`Balance`/`DueDate`/`TotalAmt`. That single fact forces the source decision.

---

## 1. Source-table decision: `dbo` raw vs `reporting.*` curated

Evaluated per thing we need.

### Bill header → **`dbo.Bill`** (no alternative)
Only `dbo.Bill` exposes `Balance` and `DueDate`. There is no curated header
view. Decision is forced. `dbo.Bill` is raw QB structure (stable, boss can't
silently redefine it) — fine for a header.

```sql
-- the only option
SELECT Id, DocNumber, TxnDate, DueDate, VendorRefId, VendorRefName,
       TotalAmt, Balance, PrivateNote, CurrencyRefName, MetaData_LastUpdatedTime
FROM dbo.Bill WHERE Balance > 0;
```

### Bill lines → **`reporting.fact_bill_line`** (recommended), raw as cross-check
This is the consequential choice, because lines drive categorization.

**Option A — `reporting.fact_bill_line` (RECOMMENDED).** One row per line with a
single resolved `distribution_account_name` (+ `distribution_account_id`),
`class_name`, `item_name`, `line_amount`, `line_description`, `jira_epic_id`,
regardless of whether QB stored it as Account-based or Item-based — and with
`reporting.bill_line_gl_override` **already applied**. This is exactly the
signal the rules engine needs.
```sql
SELECT transaction_id, line_id, line_number, detail_type, line_description,
       line_amount, item_id, item_name, class_id, class_name,
       distribution_account_id, distribution_account_name
FROM reporting.fact_bill_line
WHERE transaction_type = 'Bill' AND transaction_id = ?
ORDER BY line_number;
```
*Pros:* unified GL across both shapes; overrides applied for free; vendor
resolved; less join code; the boss already curates the GL mapping we'd
otherwise have to reverse-engineer.
*Cons:* coupled to the boss's view definition — columns could change without
notice.

**Option B — `dbo.Bill_Line` (raw).** GL account lives in *different columns*
per `DetailType`, and the override table is **not** applied:
```sql
SELECT bl.Bill_Id, bl.LineNum, bl.DetailType, bl.Amount, bl.Description,
       COALESCE(bl.AccountBasedExpenseLineDetail_AccountRefName,
                bl.ItemBasedExpenseLineDetail_ItemRefName)        AS account_or_item,
       COALESCE(bl.AccountBasedExpenseLineDetail_AccountRefId,
                bl.ItemBasedExpenseLineDetail_ItemRefId)          AS account_or_item_id,
       COALESCE(bl.AccountBasedExpenseLineDetail_ClassRefName,
                bl.ItemBasedExpenseLineDetail_ClassRefName)       AS class_name
FROM dbo.Bill_Line bl
WHERE bl.Bill_Id = ?
ORDER BY bl.LineNum;
-- and to match the curated view we'd ALSO have to LEFT JOIN
-- reporting.bill_line_gl_override ON (transaction_id, item_id) and
-- resolve item → account ourselves. That logic is what fact_bill_line is.
```
*Pros:* raw QB, stable structure, no dependence on a boss-owned view.
*Cons:* two-shape COALESCE, item→account resolution, and override replication
all fall on us — fragile and exactly the wheel `fact_bill_line` already turns.

**Recommendation:** source lines from **`reporting.fact_bill_line`**. The
categorization payoff outweighs the coupling risk. **Mitigations:** (a)
`WAREHOUSE_SCHEMA.md` pins the exact column list we depend on; (b) the sync
asserts those columns exist each run and writes a clear error to `AuditLog` if
the view shape changes; (c) `explore_payables_warehouse.py`'s metadata header
lets us diff the schema on demand. Header stays on raw `dbo.Bill`, so the
core mirror never depends on the curated layer.

### Payments → **raw chain** for per-bill applied amounts
Paid-status itself comes from `dbo.Bill.Balance` (0 ⇒ paid). For payment
*detail* (which payment, how much applied to *this* bill), the curated
payment views are GL-entry-level and don't expose the per-bill applied amount;
the raw bridge does (§2, query 4). Recommend raw chain for payment detail.

### Vendor → **`reporting.dim_vendor`** (for the default-category mapping)
The sync doesn't strictly need a vendor table (the vendor name is on
`dbo.Bill.VendorRefName` and on `fact_bill_line.entity_name`). We use
`dim_vendor` only to populate/refresh the `VendorCategoryDefault` keys (§4) —
cleaner columns (`vendor_id`, `vendor_name`, `is_active`) than raw `dbo.Vendor`.

---

## 2. The four canonical sync queries (actual SQL)

Parameters are pyodbc `?` placeholders. Dates bind as ISO strings.

**(1) Open bills** — the steady-state pull (~280 rows today).
```sql
SELECT Id, DocNumber, VendorRefId, VendorRefName, TxnDate, DueDate,
       TotalAmt, Balance, PrivateNote, CurrencyRefName, DepartmentRefName,
       APAccountRefName, SalesTermRefName,
       MetaData_CreateTime, MetaData_LastUpdatedTime
FROM dbo.Bill
WHERE Balance > 0;
```

**(2) Recently-updated bills** — look-back window to catch paid/edited bills
(`Balance` dropped to 0, or any field changed). `?` = `now − LOOKBACK_DAYS`.
```sql
SELECT Id, DocNumber, VendorRefId, VendorRefName, TxnDate, DueDate,
       TotalAmt, Balance, PrivateNote, CurrencyRefName, DepartmentRefName,
       APAccountRefName, SalesTermRefName,
       MetaData_CreateTime, MetaData_LastUpdatedTime
FROM dbo.Bill
WHERE MetaData_LastUpdatedTime >= ?;
```

**(3) Line detail for a bill** — drives categorization. `?` = `Bill.Id`.
```sql
SELECT transaction_id, line_id, line_number, detail_type, line_description,
       line_amount, item_id, item_name, class_id, class_name,
       distribution_account_id, distribution_account_name
FROM reporting.fact_bill_line
WHERE transaction_type = 'Bill' AND transaction_id = ?
ORDER BY line_number;
```

**(4) Payment records for a bill** — applied amount per payment. `?` = `Bill.Id`.
```sql
SELECT bp.Id            AS billpayment_id,
       bp.TxnDate,
       bp.PayType,                       -- Check | CreditCard
       bp.DocNumber,
       bp.VendorRefName,
       bpl.Amount       AS amount_applied
FROM dbo.BillPayment_Line_LinkedTxn lt
JOIN dbo.BillPayment_Line bpl
  ON bpl.BillPayment_Id = lt.BillPayment_Line_BillPayment_Id
 AND bpl.InternalIndex  = lt.BillPayment_Line_InternalIndex
JOIN dbo.BillPayment bp
  ON bp.Id = bpl.BillPayment_Id
WHERE lt.TxnType = 'Bill' AND lt.TxnId = ?
ORDER BY bp.TxnDate;
```

---

## 3. Column mapping (Azure → local PayablesTool)

Money is stored as **INTEGER cents**: `int(round(Decimal(str(v)) * 100))`
(warehouse `numeric` carries 7 dp, e.g. `1369.8900000` → `136989`; `0E-7` → `0`).
Dates: `datetime2` → ISO **date** `YYYY-MM-DD` for business dates, ISO
**datetime** for sync timestamps (`value.isoformat()`; reject/quarantine on
parse failure → store NULL + flag, count in `AuditLog`). QB ids are `nvarchar`
(numeric-looking or GUID) → keep as **TEXT**.

### `bill` ← `dbo.Bill`
| local column | type | source column | conversion |
|---|---|---|---|
| `qb_bill_id` (PK) | TEXT | `Id` | — |
| `bill_number` | TEXT | `DocNumber` | may be NULL |
| `vendor_ref` | TEXT | `VendorRefId` | — |
| `vendor` | TEXT | `VendorRefName` | — |
| `bill_date` | TEXT (ISO date) | `TxnDate` | datetime2→date |
| `due_date` | TEXT (ISO date) | `DueDate` | datetime2→date; NULL→flag |
| `amount_cents` | INTEGER | `TotalAmt` | Decimal×100 |
| `open_balance_cents` | INTEGER | `Balance` | Decimal×100 |
| `qb_memo` | TEXT | `PrivateNote` | — (OPS parsing → metadata) |
| `currency` | TEXT | `CurrencyRefName` | — (assume USD) |
| `department` | TEXT | `DepartmentRefName` | — |
| `ap_account` | TEXT | `APAccountRefName` | — |
| `sales_term` | TEXT | `SalesTermRefName` | — (for later pay-date logic) |
| `qb_created_at` | TEXT (ISO dt) | `MetaData_CreateTime` | — |
| `qb_updated_at` | TEXT (ISO dt) | `MetaData_LastUpdatedTime` | drives look-back |
| `is_paid` | INTEGER | derived | `1 if open_balance_cents == 0` |
| `last_synced_at` | TEXT (ISO dt) | app clock | — |

### `bill_line` ← `reporting.fact_bill_line`
| local column | type | source column | conversion |
|---|---|---|---|
| `qb_bill_id` (FK) | TEXT | `transaction_id` | — |
| `line_number` | INTEGER | `line_number` | numeric→int |
| `qb_line_id` | TEXT | `line_id` | nullable (DescriptionOnly) |
| `detail_type` | TEXT | `detail_type` | Account/Item/DescriptionOnly |
| `line_description` | TEXT | `line_description` | — |
| `line_amount_cents` | INTEGER | `line_amount` | Decimal×100 |
| `gl_account_id` | TEXT | `distribution_account_id` | **rules-engine key** |
| `gl_account_name` | TEXT | `distribution_account_name` | **rules-engine key** |
| `gl_account_number_canonical` | TEXT | `dim_account.account_number` (join on `distribution_account_id`) | Phase 5; canonical GL number |
| `gl_account_path` | TEXT | `dim_account.account_path` (join on `distribution_account_id`) | Phase 5; rollup path — **`gl_account_path_like` key** |
| `qb_class_id` | TEXT | `class_id` | — |
| `qb_class_name` | TEXT | `class_name` | **rules-engine key** |
| `item_id` | TEXT | `item_id` | — |
| `item_name` | TEXT | `item_name` | — |
| PK | — | (`qb_bill_id`, `line_number`) | composite |

### `bill_metadata` (app-owned; auto-created `approval_state='New'` on first sight)
Not from the warehouse except `ops_number`, parsed from `bill.qb_memo`
(`PrivateNote`): regex `(?i)OPS-?\s*0*(\d{3,})`, normalized `OPS-<digits>`,
first match → `ops_number`, all matches → `ops_numbers_all`. The memo regex is
the **primary** `ops_number` source; `fact_bill_line.jira_epic_id` is a
**cross-check only** (§5), never a replacement. `app_category` is computed per
§4, with the per-category split stored in `app_category_breakdown` (JSON).
`has_credit_applied` is a bill-level UI flag (optionally auto-set when
`dbo.Bill_LinkedTxn` shows a `VendorCredit` linked txn) — **no net-AP math**
(§5). All other fields per BUILD_PLAN.

### `vendor_default` keys ← `reporting.dim_vendor`
`vendor_id` ← `vendor_id`, `vendor_name` ← `vendor_name`, `is_active` ← `is_active`.

---

## 4. `app_category` derivation (layered; Phase 7 folded into Phase 1b)

No vendor_type exists, so categorization is a layered evaluation per bill.
**Stored** on `bill_metadata.app_category` (+ `app_category_source` for audit),
recomputed on sync and on rule changes.

**Evaluation order (first hit wins):**
1. **Manual override** — if a user set `bill_metadata.app_category_manual`, it
   wins (auditable via `AuditLog`). Computation never overwrites it.
2. **GL+Class rules** (`GLRule`) — evaluated in `priority` order against the
   bill's `bill_line` rows (using the override-applied `gl_account_*` /
   `qb_class_name` from `fact_bill_line`). First rule that matches **any** line
   sets the category. Tie-break across lines: the line with the largest
   `line_amount_cents` (the bill's dominant spend). **[Decided 2026-05-22.]**
   The full split is *also* stored in `bill_metadata.app_category_breakdown` —
   a JSON array of `{category, amount_cents, line_count}` — so the UI can show
   mixed-bill allocations and Marilyn can tell a clean bill from a split one.
3. **Vendor default** (`VendorCategoryDefault`) — if no GL rule matched, fall
   back to the vendor's default category.
4. **`Uncategorized`** — if nothing matched. Surfaced in the UI as requiring
   manual classification; these drive new-rule creation.

### Data model
```
GLRule
  id            INTEGER PK
  match_type    TEXT   -- 'gl_account_number' | 'gl_account_name_like'
                       -- | 'gl_account_path_like' | 'class_name' | 'gl_and_class'
                       -- ('gl_account_path_like' added in Phase 5 for rollup rules)
  match_value   TEXT   -- e.g. '56100' (number/prefix), '%COGS%' (name like),
                       -- 'Pre-Owned' (class); for gl_and_class: 'acct||class'
  target_category TEXT
  priority      INTEGER  -- lower = evaluated first
  active        INTEGER
  created_by, created_at, updated_at

VendorCategoryDefault
  vendor_id        TEXT PK   -- ← reporting.dim_vendor.vendor_id
  vendor_name      TEXT
  default_category TEXT
  active           INTEGER
  created_by, created_at, updated_at
```
GL-account values to match against come straight from the discovered
`distribution_account_name` (e.g. `"56100 OUTBOUND SHIPPING COST OF GOODS
SOLD:Outbound Shipping"`, `"Pre-Owned Device COGS"`, `"Service Parts COGS"`) —
so both `gl_account_number` (parse leading digits) and `gl_account_name_like`
matching are useful. Admin UI to manage both tables; "re-run rules across all
bills" recomputes `app_category`. The boss's `reporting.bill_line_gl_override`
is upstream of us (already applied by `fact_bill_line`), so our rules always
see corrected accounts — no need to replicate it.

---

## 5. AP-balance tie-out (stamped to `AuditLog` every sync)

Each sync run computes and records the gross open-AP total so Joe can eyeball
drift against QB's AP balance (account 20100) weekly.
```sql
SELECT COUNT(*)        AS open_bill_count,
       SUM(Balance)    AS open_ap_total      -- dollars; store as cents
FROM dbo.Bill
WHERE Balance > 0;
```
Written into the `AuditLog` `sync_run` row as
`{open_bill_count, open_ap_total_cents, ...}`.

The `sync_run` record also carries per-run **data-quality counts**:
`date_parse_warnings` (§3) and `ops_jira_mismatch_warnings` — the latter
incremented when `fact_bill_line.jira_epic_id` is populated **and** disagrees
with the memo-parsed `ops_number`. Mismatches are surfaced for review, never
block the sync. `has_credit_applied` is recorded as a bill-level metadata flag
only; **no net-AP math** is performed (VendorCredit handling deferred until/
unless a CEO number discrepancy surfaces).

**Caveats to watch when comparing to QB's AP:** (a) `SUM(Balance)` is the gross
open-bill total — unapplied **vendor credits** (`dbo.VendorCredit`) can make
QB's net AP lower; (b) only `Balance > 0` bills are counted (credit-balance
bills excluded); (c) assumes single currency (all sample rows USD); (d) tied to
`dbo.Bill` freshness (`MetaData_LastUpdatedTime`), which was live at discovery.
Drift beyond a small threshold is a signal to investigate, not an automatic
error.

---

## Decisions (resolved 2026-05-22)
1. **Lines source** — `reporting.fact_bill_line` ✅. Joe will relay if his boss
   warns of future column renames; proceeding assuming a stable interface.
2. **Multi-line tie-break** — largest-line-amount wins for `app_category`,
   **plus** store `app_category_breakdown` JSON for split visibility ✅.
3. **`jira_epic_id`** — cross-check only; memo regex stays primary; mismatches
   counted to `AuditLog` per run ✅.
4. **VendorCredit** — deferred; ship gross `SUM(Balance)` tie-out; add
   `has_credit_applied` UI flag, no net-AP math ✅.
