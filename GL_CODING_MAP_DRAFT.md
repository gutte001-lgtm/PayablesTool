# GL_CODING_MAP_DRAFT.md — proposed GL → app_category rules (DRAFT for review)

Status: **draft for Joe's approval. Nothing inserted into `gl_rule`.** Authored
2026-05-23 from read-only warehouse queries + the legacy workbook as a labeled
dataset.

**Method.** Pulled every GL account / class / vendor on the open-bill +
14-day-lookback set (**496 bills, $4,434,255 open**), then joined the 91
hand-labeled rows of `samples/Payment_Run_-_05_21_26__002_.xlsx` back to the
warehouse by bill number + vendor (**84/91 matched**) to learn the real
GL-account → category precedent. "# bills / open $" below use **dominant-line
attribution** (each bill assigned to the account of its largest line — the same
way the rules engine picks a header category), so the numbers estimate each
rule's actual leverage. Only the **15 canonical categories** are used; anything
that doesn't fit is in §5, not invented.

---

## 1. Canonical categories (15)

| Category | What belongs |
|---|---|
| Contractor - Outside Sales Commissions | Sales commissions to outside reps (GL 74960). |
| Contractor - Service & Repair | Field MET techs repairing/servicing devices (GL 53100 / 53300). |
| Contractor - Training | Field MET techs delivering onsite training (GL 53200 / 53400). |
| Employee Reimbursements | Staff/employee out-of-pocket reimbursements. |
| Freight | Inbound/outbound shipping & logistics carriers (GL 56100 / 56200; UPS, Kings Cargo, etc.). |
| Information Technology | Software licenses, IT services, tech subscriptions. |
| Manufacturer / Distributor Product Purchases | Product bought from a manufacturer/distributor (vs. a device unit). |
| New Device Purchases | New device units for resale (GL 51200). |
| Occupancy | Rent, CAM, utilities, building repairs (GL 70xxx facilities). |
| Other Operating Expenses | Misc operating spend not in another bucket (telecom, office, dues). |
| Parts & Products | Service parts, handpieces, accessories, consumables (GL 52xxx). |
| Pre-owned Device Purchases | Used/refurbished device units for resale (GL 51100). |
| Refunds | Customer refunds paid out (GL 25400 Unpaid Customer Refunds). |
| Reimbursement | Customer/partner reimbursements (non-refund). |
| Capital / Balance Sheet Transactions | Loan payments, capital, balance-sheet items (GL 2xxxx / 1xxxx). |

---

## 2. Proposed GL-account rules (sorted by open $ desc)

Confidence: **H** = legacy precedent and/or contractor constant and/or exact
name match; **M** = strong name signal, thin/mixed precedent; rows I can't map
confidently are in §5. **match_type guidance:** COGS accounts (51xxx/52xxx/
53xxx/56xxx) appear in the warehouse mostly as **name-only** lines (number
stripped), so use `gl_account_name_like` with `%<leaf>`; GL/asset/liability
accounts (70xxx/72xxx/74xxx/14xxx/2xxxx) always carry the number, so
`gl_account_number` is safe. (See §6.)

| GL Account (leaf / number) | # bills | Open $ | Example vendors | Proposed category | Conf | Source | Notes (match_type) |
|---|---|---|---|---|---|---|---|
| Decathlon Alpha IV, L.P. (26110) | 14 | 1,450,000 | Decathlon Alpha IV | Capital / Balance Sheet Transactions | M | legacy + inference | Loan-payable account. `gl_account_number 26110`. Vendor-specific; could be a vendor-default instead. |
| New Device COGS (51200) | 57 | 697,954 | Luvo Medical, CONMED | New Device Purchases | M | account-name + legacy(weak) | Legacy mixed (3 New Device / 2 Freight / 1 Mfr). `name_like %New Device COGS`. **See §5: freight-vendor + Mfr/Distributor overlap.** |
| Pre-Owned Device COGS (51100) | 68 | 609,747 | Dext Capital, Kings Cargo | Pre-owned Device Purchases | M | account-name + legacy(weak) | Legacy precedent was actually **4 Freight / 2 Pre-owned** — freight carriers' bills land here. `name_like %Pre-Owned Device COGS`. **See §5.** |
| Rent Payment (70120) | 6 | 246,048 | Summit Center Owners | Occupancy | H | account-name | `gl_account_number 70120`. |
| Unpaid Customer Refunds (25400) | 7 | 197,468 | Haus of Confidence, Tulsa Surgical, Pryor Health | Refunds | H | legacy (vendors) | These are the legacy "Refunds" vendors. `gl_account_number 25400`. **Refunds vs Reimbursement split → §5.** |
| Service Parts COGS (52200) | 55 | 40,130 | Pinnacle Laser, Parts4Laser, CONMED | Parts & Products | H | legacy (4 P&P) | `name_like %Service Parts COGS`. **Pinnacle conflict → §4/§5.** |
| Consumable COGS (52300) | 18 | 26,263 | Luvo, UPS, Medico | Parts & Products | M | account-name | Legacy mixed/thin. `name_like %Consumable COGS`. |
| Outbound Shipping COGS (56100) | 28 | 13,726 | UPS, Kings Cargo, Harmonia | Freight | H | legacy (11 Freight) | `name_like %Outbound Shipping COGS`. |
| Training COGS (53200) | 24 | 10,575 | Cris Brown, Holly Uppencamp | Contractor - Training | H | contractor-constant + legacy (9) | `name_like %Training COGS` (won't match the MET-Reimbursements leaf). |
| Outbound Shipping Supplies & Materials COGS (56200) | 11 | 8,156 | Uline | Freight | H | legacy (2 Freight) | `name_like %Outbound Shipping Supplies & Materials COGS`. |
| Handpiece COGS (52100) | 7 | 8,040 | Luvo, Beijing Sincoheren | Parts & Products | M | account-name | `name_like %Handpiece COGS`. |
| Outside Sales Commissions (74960) | 10 | 7,475 | Marti Hutchinson, Fred Ondris | Contractor - Outside Sales Commissions | H | legacy + account-name | `gl_account_number 74960`. |
| Telephone & Internet Access (70550) | 6 | 7,101 | COMCAST, RingCentral | Other Operating Expenses | H | legacy (3) | Legacy put telecom in Other Operating Expenses, not Occupancy. `gl_account_number 70550`. |
| Accessories COGS (52400) | 16 | 5,575 | Luvo, Laservision | Parts & Products | M | account-name | `name_like %Accessories COGS`. |
| Software Licensing Fees (73750) | 15 | 4,495 | Cloudflare, LinkedIn, SAP Concur | Information Technology | M | account-name | `gl_account_number 73750`. |
| Service and Repair COGS (53100) | 32 | 3,975 | Adam Beals, Eduardo Parker | Contractor - Service & Repair | H | contractor-constant + legacy (15) | `name_like %Service and Repair COGS`. |
| Service COGS - MET Reimbursements (53300) | 8 | 3,225 | Adam Beals, Joshua Downing | Contractor - Service & Repair | H | contractor-constant + legacy (3) | `name_like %Service COGS - MET Reimbursements`. |
| Electric (70510) | 4 | 2,759 | Rocky Mountain Power | Occupancy | H | legacy (2) | `gl_account_number 70510`. |
| Non-Capital FF&E Purchases (70920) | 2 | 1,499 | Copiers Utah | Other Operating Expenses | M | legacy (1) | `gl_account_number 70920`. |
| Building Repairs and Maintenance (70910) | 2 | 1,200 | ProServe, Gunthers | Occupancy | M | legacy (1) | `gl_account_number 70910`. |
| Office Supplies (71310) | 6 | 984 | Uline, Staples | Other Operating Expenses | M | account-name | `gl_account_number 71310`. |
| Trash & Waste Removal (70570) | 4 | 756 | Iron Mountain, Republic Services | Other Operating Expenses | M | legacy (1) | Legacy = Other Operating Expenses (not Occupancy). `gl_account_number 70570`. |
| IT Services (73710) | 1 | 7,282* | StrataTech | Information Technology | M | account-name | *present on more bills as non-dominant. `gl_account_number 73710`. |
| Prepaid Technology Services (14400) | 2 | 10,315 | VLCM, 2Fifteen | Information Technology | M | legacy (1) | Prepaid asset acct. `gl_account_number 14400`. |
| Water (70530) | 4 | 140 | Superior Water & Air | Occupancy | H | legacy (2) | `gl_account_number 70530`. |
| Gas (70520) | 3 | 243 | Summit Center, Enbridge | Occupancy | M | account-name | `gl_account_number 70520`. |
| Cleaning Service (70930) | 2 | ~0 | Violeta Melgar, Sarita's | Occupancy | M | account-name | `gl_account_number 70930`. |
| Alarm (70540) | 3 | ~0 | ADT | Occupancy | M | account-name | `gl_account_number 70540`. |
| Training COGS - MET Reimbursements (53400) | 0 dom (21 present) | — | (MET techs) | Contractor - Training | H | contractor-constant | Never a dominant line in this set, but maps for split visibility. `name_like %Training COGS - MET Reimbursements`. |

\* IT Services/Prepaid Tech open $ shown is approximate (small dominant set).

---

## 3. Proposed Class rules

**Recommendation: none required.** Classes are **org/department** codes
(`D400 - Finance & Accounting`, `D120 - Facilities - Park City, UT`,
`D650 - Biomedical Engineering`, …), not spend categories. They don't map
cleanly onto the 15 categories, and the account rules above are more precise.

One *optional, low-priority* backstop worth Joe's call:

| Class | Could backstop | Why / caveat |
|---|---|---|
| `D120 - Facilities - Park City, UT` | Occupancy | 42 dominant bills, mostly facilities. But it also catches Logitech "Other Prepaid Expenses" — would mis-bucket. Only safe at very low priority, behind account rules. |

Everything else (Finance, Biomed, Marketing, HR, Sales) is too heterogeneous to
rule on. Flagging rather than proposing.

---

## 4. Proposed vendor-default rules

Vendor defaults only fire when **no GL rule matched** (see §6), so they're for
vendors whose account is *ambiguous/uncovered*, not to override account rules.
Candidates with ≥80% concentration that are **non-redundant and engine-valid**:

| Vendor | Open bills | Concentration | Proposed default | Depends on |
|---|---|---|---|---|
| Milton A. Oliva Gonzalez | 16 | 100% Direct Staff Contract Labor | *pending* | Joe's call on GL 59080 (§5). Person-specific → good vendor-default once category set. |
| Freestone Advisory, LLC | 7 | 100% Consulting Fees | Other Operating Expenses (proposed) | Distinguishes consulting from Mark Kosiba's reimbursement on the same account (§5). |
| Wilson Sonsini | 10 | 100% Legal Fees | *pending* | Joe's call on GL 72510 "Legal Fees" (§5). |
| Polsinelli PC | 3 | 100% Legal Fees | *pending* | Same as above. |
| Logitech, Inc. | 7 | 100% Other Prepaid Expenses | *pending* | Joe's call on GL 14900 (§5). |

**Redundant with an account rule (no vendor-default needed):** Luvo Medical
(82% New Device COGS), Dext Capital (100% Pre-Owned Device COGS), Decathlon
(93% loan acct), Summit Center (Rent).

**⚠ Vendor-defaults that WON'T work due to engine precedence** (see §6) — these
need a Joe decision or per-bill manual override, not a vendor default:
- **Pinnacle Laser Services** — 100% on Service Parts COGS (→ Parts & Products
  by rule), but legacy labeled it **Other Operating Expenses**. A vendor default
  can't override the account rule.
- **Freight carriers coded into device COGS** — Kings Cargo, Traffic Tech, Bor
  Logistics, CEVA, Harmonia have freight bills whose largest line sits on
  Pre-Owned/New Device COGS. The 51100/51200 rules will label them
  Pre-owned/New Device, and a "→ Freight" vendor default can't override.

---

## 5. Needs Joe's decision (sorted by open $)

| # | Account / item | Open $ | The specific question |
|---|---|---|---|
| 1 | **Legal Fees (72510)** — Wilson Sonsini, Polsinelli | 351,963 | There is no "Legal" category in the 15. Map legal-firm fees to **Other Operating Expenses**, or **Capital / Balance Sheet Transactions**, or do you want these excluded from pay runs entirely? |
| 2 | **Other Prepaid Expenses (14900)** — Logitech (7 of 9 bills) | 348,176 | Prepaid asset account. Is Logitech spend **New Device Purchases**, **Information Technology**, **Manufacturer / Distributor Product Purchases**, or **Capital / Balance Sheet Transactions**? What is Logitech actually buying? |
| 3 | **Direct Staff Contract Labor (59080)** — Milton A. Oliva Gonzalez (all 16) | 96,000 | Milton is recurring biomed contract labor but doesn't fit the 3 contractor sub-types (Outside Sales / Service & Repair / Training). New category? **Other Operating Expenses**? Or a vendor-default just for Milton? |
| 4 | **Other Accrued Expenses (23900)** — Luvo (1) | 63,000 | Accrued-liability account. **Capital / Balance Sheet Transactions**, or does this get excluded from pay runs? |
| 5 | **Consulting Fees (72520)** — Freestone Advisory (8) + Mark Kosiba (2) | 60,316 | Mixed: Freestone = real consulting; Mark Kosiba (an employee) was legacy-labeled **Employee Reimbursements**. Map the account to **Other Operating Expenses** and override Kosiba via vendor-default/manual? |
| 6 | **Prepaid Software Licensing Fees (14300)** — Salesforce | 58,705 | Prepaid asset. **Information Technology** or **Capital / Balance Sheet Transactions**? |
| 7 | **CWIP - Leasehold Improvements (17340)** — ProServe, Professional Automotive | 40,587 | Capital construction-in-progress. **Occupancy** (legacy precedent) or **Capital / Balance Sheet Transactions**? |
| 8 | **FF&E - Vehicles (17070)** — Marilyn Carson | 28,100 | Vehicle fixed-asset purchase. **Capital / Balance Sheet Transactions**? |
| 9 | **Decathlon Alpha IV loan (26110)** | 1,450,000 | Confirm **Capital / Balance Sheet Transactions** (proposed §2). It's the single biggest $; want it explicit. Also: do loan payments belong in a pay run at all, or are they handled separately? |
| 10 | **Team Member Hiring Expenses (74180)** — Ramp Talent | 10,000 | Recruiting spend. **Other Operating Expenses** or **Employee Reimbursements**? |
| 11 | **New Device COGS (51200) — Manufacturer/Distributor overlap** | (part of 697,954) | Parker Hannifin's New-Device-COGS bills were legacy-labeled **Manufacturer / Distributor Product Purchases**, not New Device Purchases. Is Mfr/Distributor a *vendor-specific* relabel (Parker Hannifin, etc.), or is there a GL signal to separate it? |
| 12 | **51100/51200 freight contamination (strategy)** | (large) | Freight carriers' bills land on device-COGS accounts. Option A: write 51100/51200 rules and fix freight via manual override. Option B: *don't* rule on 51100/51200; rely on vendor-defaults (Luvo→New, Dext→Pre-owned, Kings Cargo→Freight). Option C: add a `vendor` match_type to the engine (code change). Which? |
| 13 | **Pinnacle Laser Services** | 29,350 | Account rule says Parts & Products; legacy said **Other Operating Expenses**. Which wins? (If Other Op Ex, needs the engine change in #12 or manual override.) |
| 14 | **Refunds vs Reimbursement (25400)** | 197,468 | All customer-refund payouts share GL 25400. Legacy used both "Refunds" and "Reimbursement". Is the split meaningful, and if so what distinguishes them (you can't tell from the GL account)? |
| 15 | **Employee Reimbursements — no GL home** | n/a | This category has no dedicated account (Mark Kosiba rode Consulting Fees). Drive it by vendor-default (list the employees), or manual? |
| 16 | **Customer Acquisition (74940)** — all UPS | ~0 | UPS shipping coded to a marketing account; legacy labeled **Freight**. Map to Freight, or leave for the UPS→Freight question? |
| 17 | **Finance Charges & Processing Fees (72960)** | 13 | Tiny $, mixed legacy. **Other Operating Expenses**, or ignore? |
| 18 | **Business & Franchise Taxes (72930) + Sales Tax Liability - <state> (241xx)** | ~0 | ~50 state sales-tax-liability accounts + franchise tax, all ~$0 open here. One `name_like %Sales Tax Liability%` rule → which category (Capital / Balance Sheet Transactions? Other Operating Expenses?), or leave Uncategorized? |
| 19 | **Other Professional Fees (72540)** — ADP TotalSource, Decathlon | 1,582 | **Other Operating Expenses** or **Capital / Balance Sheet Transactions**? |
| 20 | **Misc low-$ Park Meadows Country Club** — Dues (74110) / Meals (73310) / Entertainment (73390) | 10,683 | All one club membership. **Other Operating Expenses** for all three accounts? |

---

## 6. Rules engine notes (read before authoring)

**Supported `match_type` values** (`init_db.py` CHECK + `sync._line_matches`):
- `gl_account_number` — exact match on the **leading-digit number** parsed from
  the account name (`56100 …` → `56100`). Fails on name-only lines (number NULL).
- `gl_account_name_like` — SQL-LIKE (`%`, `_`) on the **full account name**,
  case-insensitive, **anchored `^…$`**. `%New Device COGS` matches both the
  numbered path (`51200 …:New Device COGS`) and the name-only form
  (`New Device COGS`). This is the robust choice for COGS accounts.
- `class_name` — exact (case-insensitive) on class, or LIKE if `%`/`_` present.
- `gl_and_class` — `"<acct>||<class>"`; acct part matches number-exact OR
  name-like, class part exact. Use for vendor/department disambiguation.

**Precedence the engine applies** (`sync.compute_app_category`):
1. **Manual override** (`bill_metadata.app_category_manual`) always wins.
2. **GL rules** — evaluated in `priority ASC, id ASC`; the **first** rule that
   matches a line wins *for that line*. Across a bill's lines, the **largest
   `line_amount_cents` matched line** sets the bill's header category.
3. **Vendor default** — only if **no** line matched any GL rule.
4. **`Uncategorized`** otherwise.

**Consequences for authoring:**
- ⚠ **Vendor defaults cannot override GL rules** (step 3 only fires when step 2
  found nothing). This is why Pinnacle and the freight-on-device-COGS cases
  (§4/§5 #12–13) can't be fixed with a vendor default.
- ⚠ **Dual-format accounts**: most COGS accounts appear *predominantly
  name-only* in `fact_bill_line` (e.g. New Device COGS 1 numbered / 126
  name-only). Use `gl_account_name_like`, not `gl_account_number`, for those, or
  rules silently miss most lines.
- **Largest-line tie-break**: a split bill takes the category of its biggest
  matched line — so a freight line that's larger than the device line will flip
  the header category. Watch the device/freight split bills.
- **Priority discipline**: put **specific** rules (e.g. `%Service COGS - MET
  Reimbursements`) at a *lower priority number* than **broad** ones so the
  specific match wins first. Contractor leaf rules should sit above any generic
  `%COGS%` rule if one is ever added.
- `bill_metadata.app_category_breakdown` already stores the per-line split, so
  mixed bills remain visible even though one header category is chosen.
- The boss's `reporting.bill_line_gl_override` is applied upstream by
  `fact_bill_line`, so rules already see corrected accounts.
