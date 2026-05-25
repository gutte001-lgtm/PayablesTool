# GL_CODING_MAP_FINAL.md — final rollup GL → app_category rules

Status: **final taxonomy + rule set, approved by Joe's review of
`GL_CODING_MAP_V2.md`** (kept as historical reference). The **engine change** to
support these rules shipped this phase (sync + schema + migration + tests), and
**the rules are now loaded — `gl_rule` holds 25 rows** (the 22 documented below
plus 3 small-opex rollups added at insert time; see §5). Authored 2026-05-23 from
read-only warehouse pulls.

## §1 — Category list (final 17)

| # | Category | GL source | Notes |
|---|---|---|---|
| 1 | Contractor - Outside Sales Commissions | 74960 (SELLING EXPENSES) | leaf |
| 2 | Contractor - Service & Repair | SERVICE & TRAINING COGS rollup, minus Training | rollup default |
| 3 | Contractor - Training | 53200/53400 (`%Training COGS%`) | leaf override |
| 4 | Freight | OUTBOUND SHIPPING COGS rollup (56xxx) | rollup |
| 5 | Information Technology | TECHNOLOGY EXPENSES rollup (73xxx) | ongoing tech only — **not** prepaid software |
| 6 | New Device Purchases | `%New Device COGS` (512xx) | leaf |
| 7 | Occupancy | OCCUPANCY+UTILITY+FACILITY rollups (70xxx) + prepaid rent 14900 | rollup + leaf |
| 8 | Other Operating Expenses | telephone leaf today; small opex rollups pending (§6) | mostly manual/pending |
| 9 | Parts & Products | PRODUCT COGS rollup (52xxx) | rollup |
| 10 | Pre-owned Device Purchases | `%Pre-Owned Device COGS` (511xx) | leaf |
| 11 | Refunds | CUSTOMER DEPOSITS & DEFERRED REVENUE rollup (25xxx) | rollup (blanket; rare reimbursement = manual) |
| 12 | Legal Fees | 72510 | leaf override of Consulting |
| 13 | Contract Labor | DIRECT STAFF EXPENSES rollup (59xxx) | new — Milton |
| 14 | Consulting | PROFESSIONAL FEES rollup minus Legal | new — Freestone etc. |
| 15 | CAPEX | FIXED ASSETS rollup (17xxx) | new |
| 16 | CAPEX - Software | prepaid software/SaaS 14300 + 14400 | new — leaf |
| 17 | Notes Payable | NOTES PAYABLE (26xxx) + ACCRUED EXPENSES (23xxx) rollups | new |

**Retired** (GL category wins; the rare real case gets a per-bill manual
override): Manufacturer / Distributor Product Purchases, Employee
Reimbursements, Reimbursement, and the umbrella "Capital / Balance Sheet
Transactions" (split into CAPEX / CAPEX - Software / Notes Payable).

## §2 — Rule set (concrete `gl_rule` rows, sorted by priority)

Lower `priority` = higher precedence. **Tier B leaf exceptions (priority 10–18)
override Tier A rollups (100–112).** `$`/`bills` are the dominant-line estimate
over the 496 open+lookback bills. **Do not INSERT yet** — this is the authored
set for the next pass.

| priority | match_type | match_value | app_category | exp $ | exp bills | source |
|---|---|---|---|---|---|---|
| 10 | gl_account_number | `72510` | Legal Fees | 351,963 | 16 | leaf |
| 11 | gl_account_number | `14900` | Occupancy | 348,176 | 9 | leaf (prepaid rent) |
| 12 | gl_account_number | `14300` | CAPEX - Software | 58,705 | 1 | leaf (prepaid SaaS) |
| 13 | gl_account_number | `14400` | CAPEX - Software | 10,315 | 2 | leaf (prepaid tech) |
| 14 | gl_account_number | `74960` | Contractor - Outside Sales Commissions | 7,475 | 10 | leaf |
| 15 | gl_account_name_like | `%Training COGS%` | Contractor - Training | 10,575 | 24 | leaf (overrides A102; catches 53200+53400) |
| 16 | gl_account_name_like | `%Telephone & Internet Access` | Other Operating Expenses | 7,101 | 6 | leaf (overrides A108) |
| 17 | gl_account_name_like | `%New Device COGS` | New Device Purchases | 697,954 | 56 | leaf (device split) |
| 18 | gl_account_name_like | `%Pre-Owned Device COGS` | Pre-owned Device Purchases | 609,747 | 68 | leaf (device split) |
| 100 | gl_account_path_like | `%PRODUCT COST OF GOODS SOLD:%` | Parts & Products | 80,007 | 95 | rollup |
| 101 | gl_account_path_like | `OUTBOUND SHIPPING COST OF GOODS SOLD:%` | Freight | 21,882 | 39 | rollup |
| 102 | gl_account_path_like | `%SERVICE AND TRAINING COST OF GOODS SOLD:%` | Contractor - Service & Repair | 7,200 | 40 | rollup (Training carved by p15) |
| 103 | gl_account_path_like | `PROFESSIONAL FEES:%` | Consulting | 61,898 | 12 | rollup (Legal carved by p10) |
| 104 | gl_account_path_like | `TECHNOLOGY EXPENSES:%` | Information Technology | 11,777 | 16 | rollup |
| 105 | gl_account_path_like | `CUSTOMER DEPOSITS & DEFERRED REVENUE:%` | Refunds | 197,468 | 7 | rollup |
| 106 | gl_account_path_like | `DIRECT STAFF EXPENSES:%` | Contract Labor | 96,000 | 17 | rollup |
| 107 | gl_account_path_like | `OCCUPANCY EXPENSES:%` | Occupancy | 246,048 | 6 | rollup |
| 108 | gl_account_path_like | `UTILITY EXPENSES:%` | Occupancy | 3,897 | 18 | rollup (telephone carved by p16) |
| 109 | gl_account_path_like | `FACILITY EXPENSES:%` | Occupancy | 2,699 | 6 | rollup |
| 110 | gl_account_path_like | `NOTES PAYABLE:%` | Notes Payable | 1,450,000 | 14 | rollup |
| 111 | gl_account_path_like | `ACCRUED EXPENSES:%` | Notes Payable | 63,000 | 1 | rollup |
| 112 | gl_account_path_like | `FIXED ASSETS:%` | CAPEX | 68,687 | 3 | rollup |

**22 rules documented here** (9 leaf + 13 rollup); **25 are loaded** in `gl_rule`
— the extra 3 are the small-opex rollups added at insert time (priorities
113–115; see §5). Device new/pre-owned are leaf `name_like` rules (work in the
current engine); the rollups need the `gl_account_path_like` match_type (§4).

## §3 — Leaf-exception rationale

| priority | Exception | Rollup it overrides → what the rollup would do | Why override |
|---|---|---|---|
| 10 | 72510 → Legal Fees | A103 PROFESSIONAL FEES → Consulting | Legal is its own category. |
| 11 | 14900 → Occupancy | (none — 14xxx has no rollup rule) | Prepaid **rent** (Joe-confirmed); PREPAID rollup is mixed so it gets leaf rules, not a rollup. |
| 12/13 | 14300/14400 → CAPEX - Software | (none) | Prepaid software/SaaS (Salesforce etc.) — distinct from ongoing IT. |
| 14 | 74960 → Contractor - Outside Sales Commissions | (SELLING rollup not ruled) | Commission category; SELLING rollup also holds marketing accounts, so only the commission leaf is ruled. |
| 15 | `%Training COGS%` → Contractor - Training | A102 SERVICE & TRAINING COGS → Contractor - Service & Repair | Training (53200/53400) splits out from Service & Repair. |
| 16 | `%Telephone & Internet Access` → Other Operating Expenses | A108 UTILITY → Occupancy | Joe books telephone/internet as opex, not occupancy. |
| 17/18 | `%New Device COGS` / `%Pre-Owned Device COGS` | (DEVICE COGS rollup not ruled — splits 2 ways) | 51200 (new) vs 51100 (pre-owned) are different categories; no shared rollup default. |

## §4 — Engine spec (implemented this phase)

**New match_type `gl_account_path_like`** — LIKE (`%`,`_`) against the line's
canonical `account_path` from `reporting.dim_account`, case-insensitive,
anchored `^…$` (identical semantics to `gl_account_name_like`). Returns False
when the path is NULL.

Code touchpoints (all done in this phase; no rule rows inserted):

1. **`init_db.py` SCHEMA** — two new `bill_line` columns:
   `gl_account_number_canonical TEXT` (← `dim_account.account_number`) and
   `gl_account_path TEXT` (← `dim_account.account_path`). And `'gl_account_path_like'`
   added to the `gl_rule.match_type` CHECK.
2. **`sync.py`** — `load_dim_accounts(cur)` builds `{account_id: {number, path}}`
   once per run; `fetch_bill_lines` stamps each line with the canonical number +
   path (joined by `distribution_account_id`); `_replace_lines` persists both;
   `recompute_for_bill` / `recompute_all` SELECT the path so re-runs match path
   rules.
3. **`sync._line_matches`** — one new branch:
   `if mt == "gl_account_path_like": path = line.get("gl_account_path") or ""; return bool(_like_to_regex(mv).match(path)) if (mv and path) else False`.
4. **`migrations/004_phase_5_rules_engine.py`** — idempotent: adds the two
   columns; rebuilds `gl_rule` (create-copy-drop-rename) so the CHECK accepts the
   new value (SQLite can't ALTER a CHECK), preserving rows + index; does **not**
   backfill (the next sync repopulates `bill_line`).

The existing match_types (`gl_account_number`, `gl_account_name_like`,
`class_name`, `gl_and_class`) and the vendor-default code path are **unchanged**
and still work (the leaf rules above rely on them).

## §5 — Coverage check (simulated over 496 bills / $4,434,255 open)

| Category | Open $ | Bills |
|---|---|---|
| Notes Payable | 1,513,000 | 15 |
| New Device Purchases | 697,954 | 56 |
| Pre-owned Device Purchases | 609,747 | 68 |
| Occupancy | 600,820 | 39 |
| Legal Fees | 351,963 | 16 |
| Refunds | 197,468 | 7 |
| Contract Labor | 96,000 | 17 |
| Parts & Products | 80,007 | 95 |
| CAPEX - Software | 69,020 | 3 |
| CAPEX | 68,687 | 3 |
| Consulting | 61,898 | 12 |
| Freight | 21,882 | 39 |
| **Uncategorized** | **21,680** | **30** |
| Information Technology | 11,777 | 16 |
| Contractor - Training | 10,575 | 24 |
| Contractor - Outside Sales Commissions | 7,475 | 10 |
| Contractor - Service & Repair | 7,200 | 40 |
| Other Operating Expenses | 7,101 | 6 |

**Covered: $4,412,575 = 99.51%. Uncategorized: $21,680 = 0.49%.** Remaining
buckets (all small opex, none ruled at simulation time):

| Rollup | $ | bills |
|---|---|---|
| PEOPLE & TEAM DEVELOPMENT EXPENSES | 15,779 | 3 |
| MEALS, TRAVEL, & ENTERTAINMENT EXPENSES | 4,904 | 2 |
| SUPPLIES EXPENSES | 984 | 7 |
| GENERAL ADMINISTRATION EXPENSES | 13 | 7 |
| SALES & PAYROLL TAX / SELLING:Customer Acquisition / Other COGS | 0 | 11 |

**At insert time, three of these were loaded as rollup rules → Other Operating
Expenses** (`PEOPLE & TEAM DEVELOPMENT EXPENSES`, `MEALS, TRAVEL, & ENTERTAINMENT
EXPENSES`, `SUPPLIES EXPENSES` — priorities 113–115), bringing `gl_rule` to 25
rows. `GENERAL ADMINISTRATION EXPENSES` (which holds `72960 Finance Charges &
Processing Fees`) and the ~$0 sales-tax / Customer-Acquisition buckets were **not**
loaded and stay Uncategorized for manual handling — which is why, e.g., the SIMCO
`$12.71` finance-charge bill (GL 72960) still lands Uncategorized.

## §6 — Legacy validation (84 hand-labeled bills)

**59/84 = 70% reproduce the hand label.** 25 mismatches — **24 expected by
design, 1 minor coverage gap**:

| Class | Count | What |
|---|---|---|
| Expected — freight-on-COGS | 15 | Carriers (UPS, Kings Cargo, Traffic Tech, CEVA) coded to device/parts/service COGS → labeled by GL, not Freight. The GL account carries no freight signal; manual-override per cycle (accepted decision). |
| Expected — retired category | 5 | Mfr/Distributor (Parker Hannifin ×2 → New Device/Parts), Employee Reimbursements (Mark Kosiba → Consulting), Reimbursement (→ Refunds), Capital/Balance-Sheet umbrella (Decathlon other-prof → Consulting). GL category wins by design. |
| Expected — locked split decisions | 2 | CWIP Leasehold → CAPEX (was hand-labeled Occupancy); VLCM Prepaid Technology Services → CAPEX - Software (was IT). Both follow Joe's locked 14300/14400→CAPEX-Software and FIXED ASSETS→CAPEX. |
| Expected — GL-wins-over-vendor | 2 | CONMED Service Parts COGS → Parts & Products (hand: New Device); SIMCO Service & Repair COGS → Contractor - Service & Repair (hand: Parts). The line sits on that GL account. |
| Expected — utility/facility nuance | 1 | Non-Capital FF&E + Trash → Occupancy (hand: Other Op Ex). |
| **Coverage gap — to review** | **1** | SIMCO Finance Charges & Processing Fees (72960, **$13**) → Uncategorized (hand: Parts & Products). 72960 is unruled; trivially fixed by a GENERAL ADMINISTRATION → Other Operating Expenses rule if desired. |

**No rule produces a wrong category for a covered account.** The 24 "expected"
mismatches are all cases where the hand label used vendor/context knowledge the
GL account doesn't carry (and which Joe chose to handle by GL-wins + manual
override), or are direct consequences of the locked taxonomy changes. The single
"review" item is a $13 coverage gap, not a miscategorization.
