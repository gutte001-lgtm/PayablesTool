# WAREHOUSE_PAYMENT_METHOD_FINDINGS.md — where does `payment_method` live?

Status: **read-only diagnostic, 2026-05-23.** No code, schema, migration, or DB
changes. Warehouse probed read-only on Joe's box (`ok:true`).

## TL;DR / verdict

**QuickBooks does not store a payment method on an unpaid bill — anywhere.** Not
on the bill, not on the vendor, not on the line. The only payment-method field
in the whole replica is `dbo.BillPayment.PayType`, which exists **only after a
bill is paid** and carries **only `Check` or `CreditCard`** (no Wire, no ACH).

So the PayablesTool blank is **not a sync bug** — there is no upstream field to
pull for the bills that go into a pay run. The legacy spreadsheet's "Payment
Method" column was a **manual, per-cycle planning entry by the AP team** (it even
contains `Wire`, a value QB never records).

| Hypothesis | Verdict |
|---|---|
| 1. Sync isn't pulling it | **Technically yes, but moot** — sync pulls no method, but `dbo.Bill` has no method column to pull. |
| 2. QB has it blank at the bill level | **TRUE (root cause)** — `dbo.Bill` has no payment-method field at all. |
| 3. It's on the vendor (inherited default) | **FALSE** — `dbo.Vendor` has no payment-method field (only payment *terms*). |

## What "payment method" means in PayablesTool today

- [sync.py:208](sync.py#L208) `_BILL_COLS` pulls: `Id, DocNumber, VendorRefId,
  VendorRefName, TxnDate, DueDate, TotalAmt, Balance, PrivateNote,
  CurrencyRefName, DepartmentRefName, APAccountRefName, SalesTermRefName,
  MetaData_CreateTime, MetaData_LastUpdatedTime` — **no method column**.
- `bill` table ([init_db.py](init_db.py)) has **no** method column.
- `bill_metadata.proposed_payment_method` ([init_db.py:126](init_db.py#L126),
  CHECK `Check/Wire/Credit Card/ACH`) is written **only** by the manual UI form
  ([bills.py:399](bills.py#L399) `save_metadata`) — sync never touches it
  (`_ensure_metadata` sets only `ops_number`, `ops_numbers_all`,
  `has_credit_applied`). → with no manual entry yet, it is NULL on all bills.
- `pay_run_line.payment_method` ([init_db.py:235](init_db.py#L235)) defaults from
  `proposed_payment_method` at run-build time → inherits the same blank.

## Warehouse probe results (read-only)

| Object | Cols | Method-like column? | Detail |
|---|---|---|---|
| `dbo.Bill` | 26 | **none** | No `PaymentMethod*`, `PayType`, `*Method*`. Has `SalesTermRef*` (payment **terms**, not method). |
| `dbo.Vendor` | 41 | **none** | Has `TermRefId/TermRefName` (terms) and `AcctNum`, but **no** preferred-payment-method field. |
| `reporting.fact_bill_line` | 22 | **none** | Line-level; nothing method-related (expected). |
| `dbo.BillPayment` | 25 | **`PayType`** | The only one. NOT NULL `25,841/25,841` payments. **Top values: `Check` 14,874 / `CreditCard` 10,967.** Only two values — no Wire/ACH. |

**Vendor spot-check (10 vendors with multiple open bills)** — Luvo (45 open),
Milton (16), Decathlon (14), Dext Capital (11), Wilson Sonsini (10), Pinnacle
(8), Summit Center (8), Freestone (7), Logitech (7), Adam Beals (6): **none have
a vendor-level method field to inherit** (the column doesn't exist).

## The legacy spreadsheet column I ("Payment Method")

Every row in `samples/Payment_Run_-_05_21_26__002_.xlsx` "Pay Run" sheet has a
method (`Check` / `Wire` / `Credit Card`). Since QB carries **nothing** for
unpaid bills and its only realized value-set is `Check`/`CreditCard` (no `Wire`),
those values were **typed in by the AP team each cycle** as a payment *plan* —
exactly the manual step PayablesTool is meant to replace with a per-line picker.

## Coverage: what would auto-populate if we "wired sync up"?

- **From a direct bill/vendor field: 0%** — no such field exists to read.
- **From vendor payment history (a *heuristic*, not a field):** of **240 open
  bills (Balance>0) across 77 vendors**, **225 (93.8%)** have a vendor with ≥1
  prior `BillPayment` → a derivable Check/CreditCard *suggestion*. **But:**
  - It yields **only Check or CreditCard** — never the Wire/ACH the team uses.
  - Of the open-bill vendors with history: **36 Check-only, 0 CreditCard-only,
    28 mixed** (paid both ways). So ~44% of those vendors give an **ambiguous**
    suggestion.
  - It reflects how the vendor *was last paid*, not how this bill *should* be
    paid — a hint, not an answer.

## Recommendation

> **Shipped-behavior note (2026-05-25):** the exports as built do **not** fail-closed on a missing method — a blank `payment_method` defaults into the Check bucket ([`excel_payrun.py`](excel_payrun.py) `_section_key`). The fail-closed guardrail below is a recommendation, not current behavior.

**Workflow fix (primary) + keep fail-closed.** Payment method is a per-cycle
*planning decision* QB never stored; it must be set in PayablesTool. Keep the
Phase 5 decision to **fail-closed at export** (block the CFO/CEO export until
every payable line has a method) — that's the correct guardrail, and there is no
QB field that could remove the need for it.

**Optional code enhancement (nice-to-have, not a fix): pre-fill a *suggestion*.**
Wire up a vendor-history hint to default the pay-run line's method dropdown to
the vendor's dominant historical `PayType` (Check/CreditCard), clearly editable,
to cut typing for the ~94% of bills with history. It cannot produce Wire/ACH and
can't resolve "mixed" vendors, so a human still picks those — net, it reduces but
does not eliminate per-cycle selection. **Do not** treat the suggestion as
authoritative, and **do not** add a column to `bill` (the read-only QB mirror has
no such QB field to mirror).

**Do _not_** pursue a "pull the method from `dbo.Bill`" sync change — there is
nothing to pull.

## Optional sync-change spec (suggestion-only; spec, not implemented)

If the suggestion enhancement is wanted later:

- **Source:** `dbo.BillPayment` grouped by `VendorRefId` → dominant `PayType`.
  Map `CreditCard` → `"Credit Card"` (app `METHODS` spelling); `Check` → `"Check"`.
  Emit a suggestion **only** when a vendor is unambiguous (Check-only or
  CreditCard-only); leave **mixed/no-history** vendors blank.
- **Store:** reuse `bill_metadata.proposed_payment_method` (column already
  exists — **no migration**). Set it **only on first sight of a bill**
  (`_ensure_metadata`, create branch) and **only when currently NULL** — never
  overwrite a human value. Alternatively, a small `vendor_method_hint` table if
  you'd rather keep the hint separate from the user field (that *would* need a
  migration).
- **Recompute / backfill (already-synced bills):** a one-off, idempotent pass
  that sets `proposed_payment_method` where it is NULL **and** the vendor is
  unambiguous in history — never overwriting. Given the data, this fills only the
  **Check-only** vendors' bills (36 vendors); CreditCard-only = 0; the 28 mixed
  vendors and all Wire/ACH bills stay blank for a human to set.
- **Net effect:** a partial pre-fill (Check-heavy) that reduces clicks; the
  fail-closed export still enforces completeness.
