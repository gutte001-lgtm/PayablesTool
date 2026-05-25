# GL_CODING_MAP_V2.md — rollup-based GL → app_category rules (DRAFT for review)

> **Superseded by [`GL_CODING_MAP_FINAL.md`](GL_CODING_MAP_FINAL.md) — kept for historical context.**

Status: **draft for Joe's approval. Nothing inserted into `gl_rule`; engine not
modified.** Authored 2026-05-23 from read-only warehouse pulls. Supersedes the
leaf-by-leaf approach in `GL_CODING_MAP_DRAFT.md` (kept as historical reference).

**The pivot.** Categorize by where a GL line **rolls up in the financials**, not
leaf-by-leaf. `reporting.dim_account` exposes `account_path` (the
financial-statement rollup, e.g. `COST OF GOODS SOLD:DEVICE COST OF GOODS
SOLD:New Device COGS`) and the chart of accounts is **numbered hierarchically**
(51=device, 52=product, 53=service/training, 56=shipping, 70=occupancy,
72=professional, 73=technology, …). A handful of rollup rules cover the whole
chart; leaf exceptions only where a rollup is too coarse (51100 vs 51200; legal
vs consulting). **No vendor defaults** (per Joe). A new account added next year
auto-categorizes via its rollup.

**Validation.** Simulated against the 496 open + lookback bills and the 84
legacy-labeled bills that join to the warehouse: **99.5% of open $ categorized**,
**60/84 (71%) of legacy bills reproduce their hand label**. The 24 mismatches are
almost all a data-reality problem, not a rule error (see §5/§6).

---

## 1. Category list (18)

Original 15 + the locked **Legal Fees** + two the rollup analysis justifies.

| # | Category | Primary GL source | Status |
|---|---|---|---|
| 1 | Contractor - Outside Sales Commissions | 74960 (SELLING) | original |
| 2 | Contractor - Service & Repair | 531xx/533xx (SERVICE & TRAINING COGS) | original |
| 3 | Contractor - Training | 532xx/534xx | original |
| 4 | Employee Reimbursements | *no clean GL signal* | original — **orphan, see §6** |
| 5 | Freight | 56xxx (OUTBOUND SHIPPING COGS) | original |
| 6 | Information Technology | 73xxx (TECHNOLOGY) + prepaid tech | original |
| 7 | Manufacturer / Distributor Product Purchases | *no clean GL signal* | original — **orphan, see §6** |
| 8 | New Device Purchases | 512xx | original |
| 9 | Occupancy | 70xxx + prepaid rent 14900 | original |
| 10 | Other Operating Expenses | misc opex rollups | original |
| 11 | Parts & Products | 52xxx (PRODUCT COGS) | original |
| 12 | Pre-owned Device Purchases | 511xx | original |
| 13 | Refunds | 25xxx (CUSTOMER DEPOSITS & DEFERRED REVENUE) | original |
| 14 | Reimbursement | *25400, shares acct with Refunds* | original — **see §6** |
| 15 | Capital / Balance Sheet Transactions | 26xxx/17xxx/23xxx | original |
| 16 | **Legal Fees** | 72510 (PROFESSIONAL FEES) | **locked-in this round** |
| 17 | **Contract Labor** *(proposed new)* | 59xxx (DIRECT STAFF EXPENSES) | Milton, $96k — doesn't fit the 3 MET-contractor subtypes. **Confirm (§6).** |
| 18 | **Consulting** *(proposed new)* | 72xxx PROFESSIONAL FEES minus Legal | Freestone, $62k. **Confirm (§6).** |

---

## 2. The rule set (rollup-first, sorted by open $ caught)

Two tiers. **Tier A** rollup rules need a new match_type (§4). **Tier B** leaf
exceptions work in the **current engine today** and sit at a *lower priority
number* (higher precedence) so they win over the rollup. `$` and `bills` are
from the dominant-line simulation over the 496-bill set.

### Tier A — rollup rules (need `gl_account_number_prefix`; see §4)

| # | Rollup | match_type | Pattern | Category | Open $ caught | Bills | Notes |
|---|---|---|---|---|---|---|---|
| A1 | Notes Payable | gl_account_number_prefix | `26` | Capital / Balance Sheet Transactions | 1,450,000 | 14 | Decathlon loan (locked). |
| A2 | Occupancy (rent+utilities+facility) | gl_account_number_prefix | `70` | Occupancy | 600,820 | 39 | Exception B7 carves telephone. |
| A3 | Customer refunds | gl_account_number_prefix | `25` | Refunds | 197,468 | 7 | Refunds vs Reimbursement → §6. |
| A4 | Direct staff contract labor | gl_account_number_prefix | `59` | Contract Labor | 96,000 | 17 | Milton (proposed new cat). |
| A5 | Product COGS | gl_account_number_prefix | `52` | Parts & Products | 80,007 | 95 | Service parts/consumables/handpieces/accessories. |
| A6 | Fixed assets | gl_account_number_prefix | `17` | Capital / Balance Sheet Transactions | 68,687 | 3 | CWIP/FF&E. CWIP-leasehold → §6. |
| A7 | Accrued expenses | gl_account_number_prefix | `23` | Capital / Balance Sheet Transactions | 63,000 | 1 | |
| A8 | Professional fees | gl_account_number_prefix | `72` | Consulting | 61,898 | 12 | Exception B3 carves Legal. |
| A9 | New devices | gl_account_number_prefix | `512` | New Device Purchases | 697,954 | 56 | Split from pre-owned (Joe). |
| A10 | Pre-owned devices | gl_account_number_prefix | `511` | Pre-owned Device Purchases | 609,747 | 68 | Split from new (Joe). |
| A11 | Outbound shipping COGS | gl_account_number_prefix | `56` | Freight | 21,882 | 39 | Clean: 13/13 legacy = Freight. |
| A12 | Technology | gl_account_number_prefix | `73` | Information Technology | 11,777 | 16 | IT services + software licensing. |
| A13 | Service & training COGS | gl_account_number_prefix | `53` | Contractor - Service & Repair | 7,200 | 40 | Default; exception B4 carves Training. |

*A1+A6+A7 are three prefixes that all map to Capital — one logical rule, three
patterns. A9/A10 are two prefixes under 51xxx (the device split Joe wants).*

### Tier B — leaf exceptions (current engine; higher precedence)

| # | Exception | match_type | Pattern | Category | Open $ | Bills | Overrides |
|---|---|---|---|---|---|---|---|
| B1 | New Device COGS leaf | gl_account_name_like | `%New Device COGS` | New Device Purchases | 697,954 | 56 | (alt to A9 — works today w/o engine change) |
| B2 | Pre-Owned Device COGS leaf | gl_account_name_like | `%Pre-Owned Device COGS` | Pre-owned Device Purchases | 609,747 | 68 | (alt to A10) |
| B3 | Legal | gl_account_number | `72510` | Legal Fees | 351,963 | 16 | A8 |
| B4 | Contractor training | gl_account_name_like | `%Training COGS%` | Contractor - Training | 10,575 | 24 | A13 (catches 53200 + 53400) |
| B5 | Prepaid rent | gl_account_number | `14900` | Occupancy | 348,176 | 9 | (no 14xxx rollup rule — locked decision) |
| B6 | Outside sales commissions | gl_account_number | `74960` | Contractor - Outside Sales Commissions | 7,475 | 10 | (no 74xxx rollup rule) |
| B7 | Telephone & Internet *(optional)* | gl_account_name_like | `%Telephone & Internet Access` | Other Operating Expenses | 7,101 | 6 | A2 — reproduces 4 legacy labels; drop if telephone-as-occupancy is fine |
| B8 | Prepaid software/tech *(optional)* | gl_account_number | `14300`, `14400` | Information Technology | 69,020 | 3 | (Salesforce etc.; else Uncategorized) |

**Count: 13 Tier-A rollup + 6 required Tier-B exceptions (B3–B6 + device) = ~16
logical rules**, +2 optional (B7, B8). Device can be A9/A10 (prefix) *or* B1/B2
(name_like) — pick one pair, not both. If Joe accepts "telephone = occupancy"
and drops the prepaid-tech refinement, the core set is **~14 rules**.

---

## 3. Leaf-level exceptions — rationale

Where the rollup is too coarse, ordered by why:

| Exception | Why the rollup is too coarse |
|---|---|
| Device split (A9/A10 or B1/B2) | `DEVICE COST OF GOODS SOLD` rollup holds both 51100 (pre-owned) and 51200 (new) — Joe treats these as different categories. Two rules, no rollup default. |
| Contractor training (B4) | `SERVICE AND TRAINING COGS` rollup (A13) defaults to Service & Repair; Training (53200/53400) must split out. `%Training COGS%` catches both training leaves. |
| Legal (B3) | `PROFESSIONAL FEES` rollup (A8) → Consulting; Legal Fees (72510) is its own category. |
| Prepaid rent (B5) | `PREPAID EXPENSES` is mixed (rent vs software). Only 14900 is rent → Occupancy (Joe-confirmed); no rollup rule for 14xxx. |
| Prepaid tech (B8) | Same rollup, the IT side: 14300 (Salesforce)/14400 → Information Technology. |
| Outside sales (B6) | `SELLING EXPENSES` rollup also holds marketing accounts; only 74960 is the commission category. |
| Telephone (B7) | `UTILITY EXPENSES` (under A2's 70-prefix) → Occupancy, but Joe labels telephone/internet as Other Operating Expenses. |

All exceptions use **existing** match_types (`gl_account_name_like`,
`gl_account_number`), so they categorize the big device/legal/contractor dollars
**even if the Tier-A engine change is deferred**.

---

## 4. Engine impact assessment

**Current `match_type` values** (`init_db.py` CHECK; `sync._line_matches`):
`gl_account_number` (exact on the leading digits parsed from the line's account
name), `gl_account_name_like` (LIKE on the full account name), `class_name`,
`gl_and_class`.

**Do they suffice?** The **Tier-B** exceptions: **yes, today.** The **Tier-A**
rollup rules: **no** — nothing matches on the rollup/number-prefix. Emulating
rollups with the current `gl_account_name_like` means enumerating every leaf
(Parts = 4 rules, Occupancy ≈ 8, …) — i.e. the ~30-rule sprawl this pivot is
replacing.

**Recommended new match_type: `gl_account_number_prefix`.**
- **Semantics:** match if the line's **canonical account number** *starts with*
  `match_value` (e.g. `52` matches 52100/52200/52300/52400).
- **Prerequisite (important):** sync must store the **canonical account number
  from `dim_account`**, not the number parsed from the line name. `fact_bill_line`
  delivers COGS lines **mostly name-only** (e.g. New Device COGS: 1 numbered /
  126 name-only), so today `bill_line.gl_account_number` is NULL for most COGS
  lines and a prefix match would miss them. Fix: in `sync.fetch_bill_lines`,
  join `distribution_account_id` → `dim_account.account_id` (verified **100%
  join coverage** on the current set) and store the canonical `account_number`
  (and ideally `account_path`).
- **Exact code touchpoints (spec only — do not implement now):**
  1. `sync.py`: pull `distribution_account_id`; add a `dim_account` lookup
     (id → number/path/classification) loaded once per run; populate a new
     canonical-number field on each line.
  2. `init_db.py`: add `bill_line.gl_account_number_canonical TEXT` (or backfill
     the existing `gl_account_number` from dim_account instead of the name parse)
     — plus, if path-matching is chosen, `bill_line.gl_account_path TEXT`.
  3. `init_db.py` CHECK: add `'gl_account_number_prefix'` to the `gl_rule.match_type`
     CHECK list. **SQLite caveat:** a CHECK can't be `ALTER`ed in place — the
     migration must rebuild `gl_rule` (create new, copy, drop, rename).
  4. `sync._line_matches`: add one branch —
     `if mt == 'gl_account_number_prefix': return bool(acct_num_canonical) and acct_num_canonical.startswith(mv.strip())`.
  5. `migrations/004_*.py`: add the column(s), backfill from dim_account on next
     sync, rebuild `gl_rule` for the new CHECK value.
- **Alternative: `gl_account_path_like`** — LIKE against the stored
  `account_path` (e.g. `%PRODUCT COST OF GOODS SOLD:%`). Same prerequisite (store
  the path from dim_account), same 5 touchpoints. More **rename-robust** and
  self-documenting than numbers; slightly more storage. Equivalent power — pick
  one. (Number-prefix is marginally simpler to implement; path reads better in
  the admin UI.)

**Fallback if the engine change is deferred:** ship the Tier-B leaf rules now
(they cover New/Pre-owned devices $1.3M, Legal $352k, prepaid rent $348k,
contractor, outside sales — the bulk of the money), and accept the broad rollups
(Parts, Freight, Occupancy-utilities, Consulting, Capital, Refunds, Contract
Labor) stay Uncategorized until the match_type lands.

---

## 5. Coverage check

Simulated over **496 bills / $4,434,255 open** (dominant-line attribution):

| Category | Open $ | Bills |
|---|---|---|
| Capital / Balance Sheet Transactions | 1,581,687 | 18 |
| New Device Purchases | 697,954 | 56 |
| Pre-owned Device Purchases | 609,747 | 68 |
| Occupancy | 600,820 | 39 |
| Legal Fees | 351,963 | 16 |
| Refunds | 197,468 | 7 |
| Contract Labor | 96,000 | 17 |
| Information Technology | 80,796 | 19 |
| Parts & Products | 80,007 | 95 |
| Consulting | 61,898 | 12 |
| Freight | 21,882 | 39 |
| Contractor - Training | 10,575 | 24 |
| Contractor - Outside Sales Commissions | 7,475 | 10 |
| Contractor - Service & Repair | 7,200 | 40 |
| Other Operating Expenses | 7,101 | 6 |
| **Uncategorized** | **21,680** | **30** |

**Covered: $4,412,575 = 99.5%. Uncategorized: $21,680 = 0.5%.**

Still-Uncategorized buckets (all small opex, none ruled):

| Rollup | Open $ | Bills | Easy fix |
|---|---|---|---|
| PEOPLE & TEAM DEVELOPMENT EXPENSES | 15,779 | 3 | prefix `74*` (dues/hiring) → Other Operating Expenses? |
| MEALS, TRAVEL, & ENTERTAINMENT EXPENSES | 4,904 | 2 | prefix `733` → Other Operating Expenses? |
| SUPPLIES EXPENSES | 984 | 7 | prefix `713` → Other Operating Expenses? |
| GENERAL ADMINISTRATION EXPENSES | 13 | 7 | leave Uncategorized (≈$0) |
| SALES & PAYROLL TAX LIABILITIES / SELLING:Customer Acquisition / Other COGS | 0 | 11 | ≈$0 — leave |

One optional rule (`74*`/`733`/`713` → Other Operating Expenses) would push
coverage to ~99.99%. See §6 #5.

---

## 6. Needs-Joe decisions (slim)

Down from 20 to 7. Sorted by dollar/impact.

| # | Item | $ exposure | Question |
|---|---|---|---|
| 1 | **Freight coded into device/parts COGS** | high volume, low $ | **The one rollups can't fix.** 14 of 24 legacy mismatches are freight carriers (UPS, Kings Cargo, Traffic Tech, CEVA) whose bills sit on 511xx/512xx/52xxx COGS — so A5/A9/A10 will label them New/Pre-owned/Parts, but you call them **Freight**. The GL account carries no freight signal; with no vendor defaults, the only fixes are (a) accept it + manual-override those bills each cycle, or (b) add a line-description/vendor signal to the engine (bigger change). Which? |
| 2 | **Confirm new category: Contract Labor** | 96,000 | Milton's `Direct Staff Contract Labor` (59xxx) → new **Contract Labor** category (proposed), or fold into Other Operating Expenses / one of the Contractor subtypes? |
| 3 | **Confirm new category: Consulting** | 61,898 | `PROFESSIONAL FEES` minus Legal (Freestone etc.) → new **Consulting** category (proposed), or Other Operating Expenses? Edge: Mark Kosiba (employee) rode this account and was legacy-labeled Employee Reimbursements — manual-override him? |
| 4 | **CWIP - Leasehold Improvements (17340)** | 40,587 | Under A6 it maps to Capital / Balance Sheet. Legacy labeled it **Occupancy**. Capital or Occupancy? |
| 5 | **Small opex rollups → Other Operating Expenses?** | 21,680 | Add one prefix rule for PEOPLE&TEAM / MEALS / SUPPLIES (`74*`/`733`/`713`) → Other Operating Expenses, or leave them Uncategorized for manual handling? |
| 6 | **Refunds vs Reimbursement (both on 25400)** | (in 197,468) | A3 maps all of 25xxx → Refunds. The one legacy "Reimbursement" (Dermatology Center of East Bay) can't be GL-separated. OK to blanket → Refunds and manual-flag the rare reimbursement? |
| 7 | **Orphan categories: Manufacturer / Distributor + Employee Reimbursements** | (in device $) | Neither has a GL signal — both were vendor-driven relabels (Parker Hannifin → Mfr/Distributor; employees → Employee Reimbursements). Keep the categories for manual tagging, or retire them? |

**Why legacy reproduction is 71%, not higher:** of the 24 mismatches, ~15 are
item #1 (freight-on-COGS), 2 are Parker Hannifin (#7 Mfr/Distributor), 2 are the
Professional-Fees edges (#3), and the rest are #4/#6 and a couple of tiny opex
nuances. **None are rule bugs** — they're cases where your hand label used vendor
context the GL account doesn't carry. Those will always need manual override (or
a vendor/line-text signal the current engine doesn't have).
