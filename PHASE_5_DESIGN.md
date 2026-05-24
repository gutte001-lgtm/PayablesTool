# PHASE_5_DESIGN.md — Excel exports (design spec, not built)

Status: **scoping only.** No code written yet. Review/revise this before any
implementation. Authored 2026-05-23.

## Scope of this document

Covers the **two Excel exports** only:

- **A) CFO Pay-Run Excel** — the check-run workbook the CFO uses for the bank.
- **B) CEO printout** — the `ok_for_ceo`-filtered payables detail.

> **Out of scope here (but also tagged "Phase 5" in BUILD_PLAN):** partial
> payments and pay-run close-out. BUILD_PLAN §"Phase 4 v1 scope decisions"
> defers partial-amount handling "to Phase 5 alongside export + close-out."
> This spec assumes the current v1 rule (each line pays the **full open
> balance**, `amount_to_pay_cents = bill.open_balance_cents`). If partials land
> in the same phase, the export's amount column needs revisiting (see Q19).

## Source material

### Original BUILD_PLAN Phase 5 text (verbatim)

> Two exports, both via openpyxl, both matching the uploaded sample
> `Payment_Run_-_05_21_26__002_.xlsx`:
>
> 1. **Pay Run Excel** — grouped exactly like the uploaded sample: Contractor
>    Checks → Contractor Wire → Contractor Credit Cards → Contractor Total →
>    Checks (everything else by category) → Buys (Pre-owned Devices) →
>    Refunds/Reimbursements → Credit Cards → ACH/Wire → Total. Sub-subtotals at
>    each break. Columns: Vendor, Vendor Type (display as `app_category`),
>    Bill #, Date, Due Date, Amount, Open Balance, Payment Method, Bill Approval
>    (composite `{approver} - {channel}`), Approval Date, Receipt/Delivery,
>    Memo, Notes.
> 2. **CEO Excel** — same content filtered to `ok_for_ceo = true`, formatted for
>    **landscape print**: set print area, fit-to-width, repeat header rows on
>    each page, page numbers in footer, Arial 10pt body / 11pt bold subtotals.
>    The CEO prefers paper; this must look right when printed without
>    re-formatting.
>
> Both exports stamped with the PayRun id and export timestamp on a footer line.
> Saved into `/exports/` and made available for download from the pay run detail
> page.

### What the legacy sample workbook actually contains

`samples/Payment_Run_-_05_21_26__002_.xlsx` (the manual artifact being
replaced). Two sheets:

- **`Data`** (A1:Q280, ~279 rows) — a raw dump of all open bills. Carries the
  data-quality bugs this app exists to fix:
  - Due date appears **twice**: col E is a *text* string (`'05/11/2026'`), col F
    is the Excel formula `=+E2*1` coercing it to a serial number.
  - "Due status" (col N) is the live formula `=IF(F2<TODAY(),"Overdue",…)`.
  - "Vendor Type" (col B) is **blank** in the data dump — it's hand-typed later.
  - Money is in **dollars** (e.g. `2025000` = $2,025,000), not cents.
  - Trailing empty columns O/P/Q ("Bill type", "Bill Status", "Payment Status").
- **`Pay Run`** (A1:N104) — the formatted output the CFO/CEO see. Header row 1,
  then grouped sections with subtotals. **This is the sheet the CFO export must
  reproduce** (cleanly — without the dual-date / formula leaks).

Legacy `Pay Run` section order (with the *exact* subtotal labels and the
discovery that **subtotals sum the Open-balance column, not Amount**):

| # | Section (legacy) | Filter that produced it | Subtotal label | Sample total |
|---|---|---|---|---|
| 1 | Contractor, Check | Vendor Type `Contractor - *` + method Check | `Contractor Checks Total` | 24,225.00 |
| 2 | Contractor, Wire | `Contractor - *` + Wire | `Contractor Wire` | 5,350.00 |
| 3 | Contractor, Credit Card | `Contractor - *` + Credit Card | `Contractor Credit Cards` | 9,410.92 |
| 4 | — | (sum of 1–3) | `Contractor Total` | 38,985.92 |
| 5 | Checks | non-contractor, Check, **excl. Buys/Refunds** | `Checks` | 137,765.16 |
| 6 | Buys | Vendor Type `Pre-owned Device Purchases` | `Buys` | 28,575.00 |
| 7 | Refunds/Reimbursements | Vendor Type `Refunds`/`Reimbursement`, any method | `Refunds/Reimbursements` | 196,066.70 |
| 8 | Credit Cards | non-contractor, non-refund, Credit Card | `Credit Cards` | 32,962.69 |
| 9 | ACH/Wire | non-contractor, non-refund, Wire | `ACH/Wire` | 9,703.00 |
| 10 | — | (sum of contractor + 5–9) | `TOTAL` | 444,058.47 |

Key structural facts learned from the file:
- Grouping is a **hybrid of payment method *and* category** — sections 6 (Buys)
  and 7 (Refunds) are carved out *by category* even though their method is
  Check/Wire/Credit Card; the rest are *by method*. (See Q1–Q4 — the precise
  assignment rules are the main thing this spec can't fully pin down.)
- Subtotals/total are computed off the **Open balance** column.
- Row 68 (SpaDerma) is a real **partial**: Amount 50,000 / Open 25,000 — the
  subtotal used 25,000. Confirms the Open-balance column drives totals.
- Legacy fonts are inconsistent (Arial 8/9 data, Calibri 14 subtotals) and
  subtotals are live `SUBTOTAL(9,…)`/`SUM(…)` formulas. The new export should
  **not** copy that; follow the cleaner BUILD_PLAN font spec and write **static
  computed values** (see Q14).

### What the current code already gives us

`payruns.grouped_lines()` ([payruns.py](payruns.py)) produces **Contractor (by
method) + Other (by method) + deferred**, with per-method subtotals and a grand
total. That covers sections 1–4 of the table above but **not** the category
carve-outs (Buys, Refunds) or the method-section split of "Other" (5, 8, 9).
The export will need a **richer grouping** than what the detail view renders
today — and it depends on `app_category`, which is empty until GL rules exist.

---

## A) CFO Pay-Run Excel

| Aspect | Decision / current best understanding |
|---|---|
| **Purpose** | The approved check run. Source for the CFO's positive-pay upload to the bank and the record of what's being paid this cycle. |
| **Consumer** | CFO (Shaun). Opens it after the run is approved; uses it to drive payments in QB/at the bank. |
| **Trigger — state** | Run at `Locked` (terminal in Phase 4). **Open: also allow at `CFO_Approved`?** (Q8) |
| **Trigger — UI** | A "Download CFO Excel" button on the pay-run detail page, shown for the allowed state(s). |
| **File naming** | `PayRun_<id>_<week_ending or created date>_v<NN>.xlsx`, e.g. `PayRun_7_2026-05-21_v01.xlsx`. (Legacy `__002_` suffix implies versioning is already a habit — see Q15.) |
| **Sheets** | One sheet, `Pay Run`, matching the legacy formatted sheet. **Open: also emit a raw `Data` sheet?** (Q6) |
| **Row grouping** | The 10-section structure in the table above. Exact assignment rules → Q1–Q4. |
| **Subtotals** | One subtotal row per section (label in col A, value in the Open-balance column), a `Contractor Total`, and a grand `TOTAL`. Values = **sum of `amount_to_pay_cents`** for the section's payable lines. |
| **Deferred lines** | Excluded/rejected lines are **not** in the paid sections. **Open: list them in a "Deferred / not paid" block at the bottom (as the detail view does), or omit entirely?** (Q9) |
| **Rejected lines** | Same as deferred — never counted in subtotals/total. |
| **Footer** | One footer line: `PayRun #<id> · <name> · exported <ISO timestamp> by <user>` (per BUILD_PLAN "stamped with PayRun id and export timestamp"). |

### Column list (CFO Excel)

Legacy order, **dropping the duplicate Due-date column** and writing real typed
dates (the core bug fix). All sources confirmed present in the schema.

| Col | Header | Type | Source field | Notes |
|---|---|---|---|---|
| A | Vendor | text | `bill.vendor` | |
| B | Vendor Type | text | `bill_metadata.app_category` | Legacy hand-typed; now computed. **`Uncategorized` for every bill until GL rules are authored** (Q16). |
| C | Bill number | text | `bill.bill_number` | nullable |
| D | Date | date | `bill.bill_date` | real Excel date, `mm/dd/yyyy` |
| E | Due date | date | `bill.due_date` | one column only |
| F | Amount | number 2dp | `bill.amount_cents / 100` | original bill total |
| G | Open balance | number 2dp | `pay_run_line.amount_to_pay_cents / 100` | amount being paid (= full open balance in v1); **this column is what subtotals sum** |
| H | Payment Method | text | `pay_run_line.payment_method` | Check / Wire / Credit Card / ACH |
| I | Bill Approval | text | `"{approver_name} - {approval_channel}"` | composite; e.g. `Marilyn - Pur Board` |
| J | Approval Date | date | `bill_metadata.approval_date` | |
| K | Receipt/Delivery | date | `bill_metadata.receipt_delivery_date` | |
| L | Memo | text | `bill.qb_memo` | multi-line; contains OPS-#### |
| M | Notes | text | **OPEN (Q10)** | legacy column was empty; candidates: append-only note log, CFO line note (`cfo_note`), or leave blank |

### Formatting (CFO Excel)

| Element | Spec |
|---|---|
| Header row | Bold; freeze panes below row 1 (`A2`). Legacy used Arial 9pt bold. |
| Data rows | Arial 10pt (BUILD_PLAN) — *not* the legacy 8pt. |
| Subtotal/total rows | Arial 11pt bold (BUILD_PLAN). |
| Money columns (F, G) | number format `#,##0.00`; subtotals may use accounting format `_(* #,##0.00…`. |
| Date columns (D, E, J, K) | real Excel dates, `mm/dd/yyyy`. **No text dates, no serial-coercion formulas.** |
| Column widths | Seed from legacy (`A≈30, B≈25, L(memo)≈72`); approximate is fine. |
| Subtotal values | **static computed numbers**, not `SUBTOTAL()`/`SUM()` formulas (Q14). |
| Wrap | Memo (L) likely wrap-on; rows auto-height. (Q13) |

---

## B) CEO printout

| Aspect | Decision / current best understanding |
|---|---|
| **Format** | **Excel** (openpyxl), per BUILD_PLAN — built to print cleanly. The app has no PDF/HTML-print toolchain today (openpyxl only). **Open: is a print-styled PDF/HTML actually preferred since the CEO wants paper?** (Q17) |
| **Purpose** | The final payables detail the CEO reviews after Joe + CFO scrub it. High-visibility; must look right on paper without reformatting. |
| **Consumer** | CEO (read-only; no login in v1 — file is handed/emailed to them). |
| **Trigger — state** | Same run state as the CFO Excel (Q8). |
| **Trigger — UI** | "Download CEO Excel" button on pay-run detail. Generated by the **CFO** (BUILD_PLAN: "cfo … Generates the CEO Excel"). (Q11) |
| **Content filter** | Only lines whose bill has `ok_for_ceo = 1`. (Refund-Visibility / Prepayment-Deposit are forced `ok_for_ceo = 0` upstream, so they're already excluded.) |
| **File naming** | `PayRun_<id>_CEO_<date>_v<NN>.xlsx`. |
| **Columns** | **Open: same 13 columns as the CFO Excel, or a trimmed set?** (Q18) Default assumption: same columns, same grouping, filtered rows + recomputed subtotals. |
| **Detail level** | Line-level detail (BUILD_PLAN: "same content filtered"), not totals-only. (Q18) |
| **Print setup** | Landscape; set print area to the used range; fit-to-width = 1; repeat header row on every page (`print_title_rows = '1:1'`); page numbers in footer; export id + timestamp footer line. |
| **Fonts** | Arial 10pt body / 11pt bold subtotals (BUILD_PLAN). |

### How the CEO export differs from the CFO export

| | CFO Excel | CEO Excel |
|---|---|---|
| Audience | CFO → bank | CEO (paper) |
| Rows | all payable lines | payable lines with `ok_for_ceo = 1` |
| Print setup | not critical | landscape, fit-to-width, repeat header, page numbers |
| Subtotals/total | over all payable lines | recomputed over the CEO-visible subset |
| Columns | full 13 | same, or trimmed (Q18) |

---

## Cross-cutting

### Module / route layout (proposal)

- New blueprint **`exports.py`** with `init_exports(app)` (same pattern as
  `admin.py` / `payruns.py`), registered in `app.py`.
- Excel-building logic in the same module or a sibling `excel_payrun.py`
  (pure functions: take a run + grouped data → an `openpyxl.Workbook`).
- Routes (on the pay-run, so permissions/state live next to the run):
  - `GET /pay-runs/<int:run_id>/export/cfo.xlsx`
  - `GET /pay-runs/<int:run_id>/export/ceo.xlsx`
  - (or `POST …/export` to generate + audit, then redirect to a download link —
    Q12 on GET vs POST.)
- Reuse and **extend** `payruns.grouped_lines()` for the richer section model;
  reuse `bills.CONTRACTOR_GL_ACCOUNT_LEAF_NAMES`, `bills.CEO_EXCLUDED`,
  `bills.METHODS`. Do **not** re-derive contractor logic.
- Write files into `exports/` (already gitignored, `.gitkeep` present); serve
  with `flask.send_file`.

### Permissions

| Export | Who can generate | Rationale |
|---|---|---|
| CFO Excel | controller + cfo (Q7) | The run's reviewers/approvers. |
| CEO Excel | cfo (BUILD_PLAN) — and controller? (Q11) | BUILD_PLAN assigns CEO-Excel generation to the CFO. |

Gate with `auth.role_required(...)`. ap_clerk almost certainly excluded (Q7).

### Audit logging

On each successful generation, write one `audit_log` row via `sync.log_audit`:

- `entity_type = "pay_run"`, `entity_id = run_id`,
- `action = "pay_run_exported"`,
- `after = {export: "cfo"|"ceo", filename, version, row_count, total_cents, generated_by}`.

**Open: does generating the CFO export advance the run `Draft…Locked` →
`Exported` (BUILD_PLAN has an `Exported` status), or is export independent of
the lifecycle?** (Q5)

### Idempotency / re-generation

- Re-exporting a `Locked` run is expected (CFO may download more than once).
- **Proposal:** version on each generation — `…_v01`, `…_v02` — never overwrite,
  so prior copies are preserved (matches the legacy `__002_` habit). Audit each.
- **Open:** overwrite the same file vs. keep versions vs. always regenerate
  on-the-fly and don't persist (Q15).

### Edge cases

| Case | Behavior |
|---|---|
| Empty run (0 payable lines) | Can't reach `Locked` — `payruns.advance` blocks empty runs from moving forward. If export is allowed pre-Lock, guard against an empty export (Q8). |
| Locked run | Normal target. |
| All lines rejected | Same as empty — blocked from advancing, so never reaches an exportable state with 0 payable. |
| `app_category = "Uncategorized"` (no GL rules yet) | **Biggest risk.** Sections 5–9 (Checks/Buys/Refunds/Credit Cards/ACH-Wire) lean on category. With everything Uncategorized, the category carve-outs (Buys, Refunds) collapse and "Vendor Type" is blank/`Uncategorized` for every row. The export is structurally valid but **not useful** until `[GL_CODING_MAP]` is authored (Q16). |
| Bill with no approver/channel | "Bill Approval" renders partial (e.g. `Marilyn - ` or blank). (Q10) |
| Multi-line bill spanning categories | One pay-run line = one bill; `app_category` is the bill's single computed header category. No per-line GL split in the export (matches the data model). |

---

## Open questions

Grouping / sections (the hardest, because the legacy mixes method + category):

1. **Section assignment precedence:** when a bill is both a category carve-out
   *and* a method (e.g. a Pre-owned-Device bill paid by Credit Card), does
   category win (goes to "Buys") or method win (goes to "Credit Cards")? Legacy
   put Pre-owned under "Buys" regardless of method — confirm this rule.
2. **"Buys" definition:** is it exactly `app_category = "Pre-owned Device
   Purchases"`, or a broader set (the legacy also has "New Device Purchases",
   which appeared under "Checks", not "Buys" — confirm)?
3. **"Refunds/Reimbursements" definition:** which `app_category` values map here
   (legacy showed `Refunds`, `Reimbursement`, `Employee Reimbursements`)? Note
   `Employee Reimbursements` appeared under "Checks" in the sample, *not* here —
   confirm the exact category-name set.
4. **"Checks" section ordering:** within "Checks", legacy is sorted by Vendor
   Type then vendor. Confirm the intended sort (category, then vendor, then
   bill #?). Same question for every section.

Lifecycle / trigger / files:

5. Should generating the CFO export move the run `Locked → Exported`? Is
   `Exported` a real state we implement now, or leave `Locked` terminal?
6. CFO Excel: one `Pay Run` sheet only, or also a second raw `Data` sheet like
   the legacy file?
7. Exact roles allowed to generate the **CFO** Excel — controller + cfo? Is
   ap_clerk ever allowed?
8. Which run state(s) may be exported — only `Locked`, or also `CFO_Approved`?
   If pre-Lock is allowed, do we watermark "DRAFT / not final"?
9. Should the CFO Excel include a bottom "Deferred / not paid" block (excluded +
   rejected lines, like the detail view), or omit them entirely?

Columns / content:

10. **"Notes" column (M):** what populates it? Options: concatenated append-only
    note log, the CFO line note (`pay_run_line.cfo_note`), the bill's status
    pill, or leave blank as the legacy did.
11. CEO Excel generation: CFO only (per BUILD_PLAN), or controller too?
12. Download via `GET` (idempotent, easy re-download) or `POST` (so the audit
    row is a deliberate action, then redirect)?
13. Memo column: wrap text + auto row height, or truncate to one line?
14. Subtotals as **static computed values** (recommended — avoids the formula
    leaks this app exists to fix) or live Excel `SUM`/`SUBTOTAL` formulas like
    the legacy?
15. Re-export policy: version filenames (`_v02`), overwrite, or generate
    on-the-fly without persisting to `exports/`?
16. Given `gl_rule` ships empty (everything `Uncategorized`), do we build the
    category-driven sections now and accept they're empty until rules exist, or
    block the export / fall back to method-only grouping until `[GL_CODING_MAP]`
    is authored?

CEO-specific:

17. Is the CEO deliverable truly **Excel**, or would a **print-styled PDF/HTML**
    serve "the CEO prefers paper" better? (Would add a new dependency.)
18. CEO Excel columns: identical 13 to the CFO Excel, or a trimmed/cleaner set
    (e.g. drop Memo/Notes, drop Amount-vs-Open-balance duplication)?

Misc:

19. If partial payments land in this same phase, the "Open balance" column must
    show the **per-line amount to pay**, which may be < full open balance —
    confirm the column semantics and header wording in that case.
20. "Amount" vs "Open balance": keep both columns (legacy did), or show only
    the amount being paid? They differ only for bills already partially paid in
    QB (and for future partials).

---

## Implementation plan (lightweight)

| Item | Proposal |
|---|---|
| **Files** | `exports.py` (blueprint + routes + audit) and optionally `excel_payrun.py` (pure workbook builders). Register `init_exports(app)` in `app.py`. |
| **Routes** | `GET/POST /pay-runs/<id>/export/cfo.xlsx` and `…/export/ceo.xlsx`; buttons on `payrun_detail.html`. |
| **Grouping** | Extend `payruns.grouped_lines()` (or a new `export_sections()` helper) to produce the 10-section model once Q1–Q4 are answered; reuse contractor/method/CEO-excluded constants. |
| **openpyxl approach** | **Built-up cells, not template-driven** — row counts and section breaks vary per run. Use a small set of reusable styles (header / data / subtotal / total) and write **typed dates + static numbers**. A static `.xlsx` template can't express the dynamic grouping cleanly. |
| **Output** | Save to `exports/` (versioned filename); serve via `send_file`; audit each generation. |
| **Tests** | New `test_phase_5.py` (same plain-script style): builds a fixture run, generates both workbooks into a temp dir, reloads with openpyxl and asserts section order, subtotal math (sum of `amount_to_pay_cents`), header row, typed dates (no string/serial leak), `ok_for_ceo` filtering, and the audit row. |
| **Size estimate** | **Medium.** openpyxl mechanics are small; the real work is (a) nailing the section-assignment rules (Q1–Q4) and (b) the CEO print setup. Low architectural risk, moderate detail/QA. Most of the risk is *spec ambiguity*, not code. |
| **Dependency on GL rules** | Category-driven sections are only meaningful once `[GL_CODING_MAP]` → `gl_rule` rows exist. Recommend authoring at least the New/Pre-owned/Refund/Freight categories before (or alongside) building the export, or the output is structurally correct but empty of categories (Q16). |
