# PayablesTool — Build Spec

## Project context

PayablesTool is a new standalone Flask + SQLite app at `C:\Users\Joe\OneDrive - Healthcare Markets DBA\Desktop\Claude Projects\PayablesTool`. Follow the same conventions as CloseTool and TrainingBriefing: Python 3.12 venv (the build was done on 3.12; the original spec said 3.11), APScheduler for background jobs, python-dotenv for secrets, `AGENTS.md` for git/merge hygiene rules (same `git merge --no-ff` and `--ff-only` patterns; same `git config --global core.editor notepad` requirement on Windows).

The app reads bills and bill payments from the boss-maintained Azure SQL warehouse `QuickBooksReplica` at `quickbooks-sq1.database.windows.net` (creds at `%LOCALAPPDATA%\AzureWarehouse\azure.env`, user `joeguttenplan`). Reuse the connection pattern from CloseTool's `warehouse_finance.py`. The app never writes back to QuickBooks — QB remains system of record for bill payment and check printing. PayablesTool produces (a) an approved Pay Run Excel for the CFO's positive-pay upload to the bank, and (b) a CEO-ready payables detail printout.

Cloud Claude Code sandbox cannot reach the Azure warehouse (firewall whitelisted to Joe's box only). Build + mock in sandbox using fixture data; hand live legs to Joe to run locally. Same governance as CloseTool §6.

## Problem being solved

Joe's team currently re-keys notes between Excel files every pay-run cycle. QB AP includes three different kinds of "bills" mixed together: real payables, refund-visibility bills (kept open so the team can track pending refunds — once approved, deleted in QB and a refund receipt is cut in O2C), and prepayment-deposit bills (e.g., 10% deposits before shipment, not actually owed yet). The CEO is shown a final payables detail after Joe and the CFO scrub it; any mistake is highly visible. The new app becomes the single source of truth for bill metadata (approval, dates, notes, classification) and produces both the CFO check-run Excel and the CEO printout from one dataset.

## Users and roles

- **ap_clerk** — multiple users (Marilyn Carson, Allen, Robby, others TBD: [AP_CLERK_USER_LIST]). Sync warehouse, enter approval metadata, classify bills, propose pay runs.
- **controller** — Joe Guttenplan. Approves pay runs before CFO. Can edit any metadata. First reviewer for CEO-bound output.
- **cfo** — Shaun Groat. Final approval for payment. Can add notes, reject lines back to controller. Generates the CEO Excel.
- **ceo** — read-only consumer. Receives the CEO Excel (no login in v1).

## Data model

> **Note (2026-05-23):** the block below is the *original pre-discovery* design.
> The **authoritative current schema is `init_db.py`** (with the
> `PHASE_3_5_SCHEMA` / `PHASE_3_6_SCHEMA` additions) and the column mapping in
> [`WAREHOUSE_SCHEMA.md`](WAREHOUSE_SCHEMA.md). Field names evolved once the
> warehouse was probed: there is **no `vendor_type`** in the warehouse (so
> categorization is the GL/Class rules engine, not a vendor-type copy); money is
> stored as `*_cents` INTEGER columns; bill/line tables use `qb_*` source-named
> columns; and `GLRule.match_type` is one of
> `gl_account_number | gl_account_name_like | class_name | gl_and_class`
> (plus a `VendorCategoryDefault` fallback table) — not the
> `gl_account | qb_class | vendor_type` shown here.

```
User                  — username, email, role, password_hash
Bill                  — synced read-only from warehouse; qb_bill_id (PK), vendor,
                        vendor_type, bill_number, bill_date, due_date, amount,
                        open_balance, qb_memo, gl_account_primary, qb_class,
                        last_synced_at, is_voided_in_qb
BillLine              — qb_line_id, qb_bill_id, gl_account, amount, line_memo
                        (used by GL rules engine)
BillMetadata          — qb_bill_id (FK, 1:1), classification, app_category,
                        approver_name, approval_channel, approval_date,
                        service_performed_date, receipt_delivery_date, ops_number,
                        proposed_payment_method, proposed_pay_date, ok_for_ceo,
                        approval_state, rush_flag, has_credit_applied,
                        partial_payment_flag, created_at, updated_at
Note                  — id, qb_bill_id, user_id, body, created_at (append-only,
                        never edited or deleted; renders as timestamped log)
Todo                  — id, qb_bill_id, body, completed_at, completed_by
PayRun                — id, name, week_ending, created_by, status (Draft /
                        Submitted_to_Controller / Controller_Approved /
                        Submitted_to_CFO / CFO_Approved / Locked)
PayRunLine            — pay_run_id, qb_bill_id, payment_method, amount_to_pay,
                        included, line_state (Pending / Approved / Rejected),
                        cfo_note
GLRule                — id, match_type (gl_account | qb_class | vendor_type),
                        match_value, target_category, priority, active
AuditLog              — id, user_id, entity_type, entity_id, action, before, after,
                        created_at
```

### Field details

- **classification** (enum): `Real`, `Refund-Visibility`, `Prepayment-Deposit`, `Other`. Refund-Visibility and Prepayment-Deposit default `ok_for_ceo = false` and are excluded from pay runs.
- **app_category** — computed: GL-rule match against `BillLine` rows wins; falls back to `vendor_type`. Stored so spend summary and exports are fast and the override is auditable.
- **approver_name + approval_channel** — replaces the current free-text "Marilyn - Pur Board" field. Channels: `Pur Board`, `MS List`, `NSPO`, `Email`, `Other`. Storing them split enables filtering and reporting.
- **approval_date, service_performed_date, receipt_delivery_date** — ALL date inputs use HTML5 date pickers backed by `DATE` columns. Reject string entry. Solves the Excel-serial-leak problem from the current file.
- **ops_number** — parsed from `qb_memo` on sync (regex `OPS-\d+`). Renders in UI as a clickable link to `[JIRA_BASE_URL]/{ops_number}` — Joe to confirm exact pattern.
- **rush_flag** — manual checkbox for same-day pay requests; surfaces visually in lists.

## Approval state machine

Per-bill `approval_state`:

```
New (just synced, no metadata yet)
  → AP_Reviewed       (ap_clerk fills metadata + sets classification)
  → Controller_Reviewed (controller approves; rejection bounces to AP_Reviewed
                         with a rejection note)
```

Per-pay-run `status` (separate flow, references bills already at Controller_Reviewed):

```
Draft              (ap_clerk builds: picks bills, sets payment_method per line)
  → Submitted_to_Controller
  → Controller_Approved (Joe; can reject lines individually)
  → Submitted_to_CFO
  → CFO_Approved    (Shaun; can reject lines individually with cfo_note)
  → Locked          (terminal — no further edits)
```

(There is no separate `Exported` status in shipped code: the CFO and CEO Excel exports are generated on a **Locked** run as an audited action — `audit_log` action `pay_run_exported` — and the run stays `Locked`.)

Paid-status reconciliation as shipped is **sync-triggered, balance-based, and bill-level** — not export-triggered and not line-level. When a bill is paid in QuickBooks its warehouse `Balance` falls to 0; the regular 15-minute sync then sets `bill.is_paid = 1` (`sync.py` — `is_paid = 1 if open_cents == 0 else 0`), and the bill drops out of the default open views, which filter `open_balance_cents > 0` (`bills.py`). PayablesTool does **not** read `BillPayment` records and does **not** reconcile paid status per `pay_run_line`. Line-level / BillPayment-matched reconciliation is a future phase, tied to partial payments (the deferred pay-run close-out work).

## Phases / branches

> **Status as of 2026-05-25:** Phases 0, 1 (1a + 1b), 2, 3, 3.5, 3.6, 4, and 5
> are implemented and merged to `master`. Phase 5 shipped the CFO + CEO Excel
> exports and the rollup GL rules engine (GL coding map authored and **25 rules
> loaded** into `gl_rule`); it did **not** include partial payments or pay-run
> close-out — those were punted (see the Phase 4 v1 scope note and the Phase 5
> section). Phases 6 (spend summary) and 8 (hosting) are not started. The GL
> rules engine + `/admin/rules` UI originally scoped as "Phase 7" shipped early
> as part of Phase 1b (see [`WAREHOUSE_SCHEMA.md`](WAREHOUSE_SCHEMA.md) §4); the
> GL coding map (`[GL_CODING_MAP]`) is now authored and loaded, so Phase 7 is
> complete.

Each phase is its own branch off master, merged with `git merge --no-ff -m "<msg>" <branch>` per CloseTool AGENTS.md §6. Don't combine phases.

### Phase 0 — Scaffold (`claude/phase-0-scaffold`)
Repo init, Python 3.11 venv, Flask, Flask-Login, APScheduler, openpyxl, pyodbc, python-dotenv. SQLite at `payables.db`. User table + login. Four seed users (one per role). Azure connection module ported from CloseTool's `warehouse_finance.py`. Smoke test: log in as each role, hit a stub `/health` route that returns warehouse connectivity status.

### Phase 1 — Bill sync (`claude/phase-1-sync`)
Discover the warehouse Bill and BillPayment table/view names (likely `bill`, `bill_line`, `bill_payment` or similar — agent to explore and document in `WAREHOUSE_SCHEMA.md`). APScheduler 15-min job pulls open bills (open_balance > 0) plus a configurable look-back window for recently-paid. Upsert into local `Bill` and `BillLine` tables. Auto-create `BillMetadata` row with `approval_state = New` for any new bill. Manual "Pull Now" button on a `/admin/sync` page. Log every sync run in `AuditLog`.

### Phase 2 — Bill list + detail UI (`claude/phase-2-bill-ui`)
List view at `/bills`: filter by classification, app_category, vendor, approval_state, ok_for_ceo, due_status (Overdue / Current / Not Due — computed), rush_flag. Sort by any column. Search across vendor, bill_number, ops_number, qb_memo. Pagination. Detail view at `/bills/<qb_bill_id>`: all metadata editable (role-gated), append-only notes log, to-do checklist, OPS-number clickable to Jira, link to source bill in QB if practical.

### Phase 3 — Approval workflow (`claude/phase-3-approval`)
State machine transitions exposed as role-gated buttons. AP_Reviewed → Controller_Reviewed (Joe button), and rejection paths with required reason that lands as a Note. AuditLog every transition. Inbox views: `/inbox/controller` shows AP_Reviewed bills; `/inbox/cfo` shows pay runs Submitted_to_CFO.

### Phase 3.5 — Follow-up workspace (`claude/phase-3-5-followup`)
A `/follow-up` workspace that surfaces stuck bills four ways (Past SLA, Stale activity, Open to-dos, In process), plus status pills and tagging (explicit + `@mention` parsed from notes) across the app, business-day activity aging, and a "Tagged for you" section above each inbox queue with a nav badge. Contractor SLA: a bill is "contractor" when any line hits a Service & Training COGS account — the four `53xxx` accounts discovered from `reporting.dim_account`, matched by **leaf account name** in `bills.CONTRACTOR_GL_ACCOUNT_LEAF_NAMES` (because `reporting.fact_bill_line` returns the same account in both numbered and name-only forms). Contractor bills are past-SLA at `bill_date + 14d`; everyone else at `due_date` (or `bill_date + 30d` if due_date is null). Status pills and tags are metadata only — they never gate the Phase 3 approval state machine; PRG (302 + flash) throughout, and tags clear only on explicit mark-done by the tagged user or a controller. First schema change since Phase 0: adds `bill_metadata.status_pill` plus the `status_pill_lookup` and `bill_tag` tables/indexes — `init_db.py` builds them for fresh DBs and `migrations/001_phase_3_5.py` upgrades the live DB idempotently (checks PRAGMA/sqlite_master, prints what it did, exits 0; Joe runs it post-merge with OneDrive paused). Tests: `test_phase_3_5.py`.

### Phase 3.6 — Open items (`claude/phase-3-6-open-items`)
An explicit per-bill "this bill needs work" primitive — the counterpart to Phase 3.5's rule-derived sections: a `bill_open_item` row with a free-text description, resolved with a required note, visible to the whole team (shared visibility, no sticky notes). A new "Open Items" section tops the home page (oldest-first, business-day aging tints, status pill, active-tag count, inline resolve form with required note); bill detail gains an "Add open item" button alongside "Tag someone" and an "Open Items on this bill" section above Notes; the bill list and inboxes gain an "Open" count column; and a home nav badge shows the total open items across all bills. Routes are role-gated (ap_clerk + controller) and PRG with required-input validation mirroring the Phase 3 reject pattern; open items are metadata only and change no Phase 3 / 3.5 logic. Adds the `bill_open_item` table + two indexes — `init_db.py` builds it for fresh DBs (`PHASE_3_6_SCHEMA`) and `migrations/002_phase_3_6.py` creates it idempotently (Joe runs it post-merge with OneDrive paused). Tests: `test_phase_3_6.py`.

### Phase 4 — Pay Run builder (`claude/phase-4-pay-run`)
`/pay-runs/new`: pick Controller_Reviewed bills (exclude Refund-Visibility and Prepayment-Deposit automatically), set payment_method per line (Check / Wire / Credit Card / ACH — default from Bill if QB has one), include/exclude toggle. `/pay-runs/<id>` shows the run grouped by Contractor sub-buckets and payment method per the uploaded sample. CFO view exposes per-line Approve / Reject with note. Lock action freezes the run. Contractor sub-bucket grouping reuses `bills.CONTRACTOR_GL_ACCOUNT_LEAF_NAMES` from Phase 3.5 — do not re-derive.

**v1 scope decisions (Joe):**
- **Partial payments are deferred.** Each line pays the bill's **full open balance**; the amount is locked (no per-line partial input). The earlier "allows partial" intent stranded residuals (a partially-paid bill stayed claimed by its Locked run with no release path). Phase 5 shipped the Excel exports + rules engine **only** — partial-amount handling and pay-run close-out were **not** built and remain deferred to a future phase.
- **Lifecycle is forward-only (no reopen).** To correct a submitted run, **reject the bad lines and create a new run.** If this proves painful in practice it becomes Phase 4.5.
- Hardening: `UNIQUE(pay_run_id, qb_bill_id)` on `pay_run_line` (a bill appears at most once per run) and a state-guarded `advance` write (`UPDATE ... WHERE status=?` + rowcount check) to close the lifecycle TOCTOU. `init_db.py` builds the index for fresh DBs; `migrations/003_phase_4.py` adds it (and the two `reviewed_*` columns) idempotently — Joe runs it post-merge with OneDrive paused.

### Phase 5 — Excel exports (`claude/phase-5-export`)
**Shipped 2026-05-25** in `exports.py` + `excel_payrun.py` (plus the rollup GL rules engine and `migrations/004_phase_5_rules_engine.py`). **Partial payments and pay-run close-out were not included and remain deferred** (see the Phase 4 v1 scope note above). Validated against the legacy sample with a penny-tie on both exports.
Two exports, both via openpyxl, both matching the uploaded sample `Payment_Run_-_05_21_26__002_.xlsx`:

1. **Pay Run Excel** — grouped exactly like the uploaded sample: Contractor Checks → Contractor Wire → Contractor Credit Cards → Contractor Total → Checks (everything else by category) → Buys (Pre-owned Devices) → Refunds/Reimbursements → Credit Cards → ACH/Wire → Total. Sub-subtotals at each break. Columns: Vendor, Vendor Type (display as `app_category`), Bill #, Date, Due Date, Amount, Open Balance, Payment Method, Bill Approval (composite `{approver} - {channel}`), Approval Date, Receipt/Delivery, Memo, Notes.
2. **CEO Excel** — same content filtered to `ok_for_ceo = true`, formatted for **landscape print**: set print area, fit-to-width, repeat header rows on each page, page numbers in footer, Arial 10pt body / 11pt bold subtotals. The CEO prefers paper; this must look right when printed without re-formatting.

Both exports stamped with the PayRun id and export timestamp on a footer line. Saved into `/exports/` and made available for download from the pay run detail page.

### Phase 6 — Spend summary (`claude/phase-6-summary`)

Dashboard at `/summary`: a CFO-review briefing for pay-run prep. Joe reviews
before proposing a run; CFO reviews before approving. CEO receives the Excel
export by email (no CEO login in v1). One landscape-printable page, four
sections, all tied to the same Open-AP grand total. Read-only over `bill` +
`bill_metadata`; no schema change, no migration, no DB writes.

**Open AP** = `open_balance_cents > 0 AND is_paid = 0`, summing
`open_balance_cents` — same convention as `bills.py` / `followup.py`.

Sections (in `summary.py` + `templates/summary.html`):

1. **Header band** — Total Open AP, open bill count, Uncategorized count
   (links to `/bills?uncat=1`), "As of <last sync>" pulled from the latest
   `audit_log` row `action='sync_run'` (the same canonical source
   `/admin/sync` uses via `admin._latest_sync`; falls back to
   `MAX(bill.last_synced_at)`).
2. **Aging** — `Current` / `1–30` / `31–60` / `61–90` / `90+` days past due,
   plus a separate `No due date` row for bills with NULL `due_date`.
   Server-side `date.today()` as the anchor; `dpd ≤ 0 → Current`,
   `dpd ≥ 91 → 90+`. The six-row footer ties to the header Total.
3. **Categories** — by `bill_metadata.app_category` (NULL → `Uncategorized`);
   sorted Open $ desc with `Uncategorized` pinned to the bottom (a hygiene
   flag, not a real category). Category names link to
   `/bills?app_category=<X>`.
4. **Top 20 vendors** — by `bill.vendor`, sorted Open $ desc, plus an
   `All other vendors (N)` reconciling row so the column ties to the grand
   total. Vendor names link to `/bills?vendor=<X>` — a new ~3-line exact
   filter added to `bills.py` for this drill-down (mirrors the existing
   `?app_category=` filter).

**Print:** an `@media print` block in `static/style.css` hides
topbar/nav/actions/flashes, strips link decoration, sets landscape `@page`
with `0.5in` margins, and uses `page-break-inside: avoid` per section.
Verified via a Playwright Chromium print-preview capture
(`screenshots/phase6_summary_print.{png,pdf}`, gitignored).

**Excel export:** single in-memory `.xlsx` at `GET /summary/export.xlsx`,
four sheets (Summary / Aging / Categories / Top Vendors); filename
`MRP_AP_Summary_YYYY-MM-DD.xlsx`. Snapshot-on-demand, not written to
`exports/` and not audited (this is read-only analytics, not a financial
artifact of record like the Phase 5 CFO/CEO pay-run exports). All four
sheets set up for landscape print so the emailed CEO copy prints cleanly.

**Access:** `@login_required` for all working roles (ap_clerk, controller,
cfo); nav link visible to those three. CEO has no login in v1.

**Out of scope for v1** (defer to v1.1 if needed): payment-method pivot
(~100% NULL today per [`WAREHOUSE_PAYMENT_METHOD_FINDINGS.md`](WAREHOUSE_PAYMENT_METHOD_FINDINGS.md)
— QB stores no method for unpaid bills, fills in once the team starts setting
methods); week-due bucketing (aging covers "overdue"; pay-run workflow covers
"coming due"); "as-of" date picker / historical snapshots; drill-down beyond
the simple filtered-list links on category/vendor names; charts / sparklines;
caching layer (live query each request; 240 open bills doesn't need it).

Tests: `test_phase_6_summary.py`.

### Phase 7 — GL rules engine (`claude/phase-7-rules`)
**Shipped (engine in Phase 1b; rules authored + loaded in Phase 5).** The
`/admin/rules` UI, the `gl_rule` / `vendor_category_default` tables, sync-time
evaluation, and "re-run rules across all bills" exist (`admin.py`, `sync.py`,
`WAREHOUSE_SCHEMA.md` §4); Phase 5 added the `gl_account_path_like` rollup match
type. The GL coding map is authored and **25 rules are loaded** into `gl_rule`
(see [`GL_CODING_MAP_FINAL.md`](GL_CODING_MAP_FINAL.md)). Original spec:
`/admin/rules` UI to manage `GLRule` rows. On bill sync, evaluate rules in priority order against each bill's `BillLine` rows; first match sets `app_category`. Vendor-type fallback if no rule matches. Re-run rules across all bills on demand. Joe will provide the initial GL coding map: [GL_CODING_MAP_TO_BE_INSERTED]. Common case: New Device GL accounts → `New Device Purchases`; Pre-owned GL accounts → `Pre-owned Device Purchases`.

### Phase 8 — Hosting for multi-user access (`claude/phase-8-deploy`)
v1 ran on Joe's machine; CFO needs access from his. Move to [HOST_DECISION — Azure App Service is the obvious choice given the warehouse is in Azure; confirm with Joe and whoever owns Azure billing]. SQLite stays for v1; revisit Postgres only if multi-user contention shows up. HTTPS, role-based access intact.

## Known gaps / deferred tech debt

- **The `gl_rule` rows are not codified in version control.** The loaded rules
  (26 once `migrations/005_general_admin_rollup.py` runs on the live DB) live
  only in the live `payables.db` — entered via `/admin/rules` or an ad-hoc load,
  never committed. `init_db.py` ships `gl_rule` **empty by design**, so a
  from-scratch rebuild does **not** reproduce the rule set. Migration 005 is the
  **first committed rule-loading migration** (001–004 were schema-only); see its
  header for the full note. Codifying the complete rule set as a committed
  loader/seed is **deferred to a future session**. Not blocking for current
  workflows, but a from-scratch DB rebuild (or a clean-room deployment for
  Phase 8 hosting) would require re-entering the rules by hand.

## Conventions and guardrails

- Follow CloseTool's `AGENTS.md` §6 git/merge rules verbatim.
- No QB write-back, ever. Even if the API would make it easy.
- All financial dates are typed dates, never strings or Excel serials.
- Notes are append-only. Bill metadata edits write to AuditLog with before/after.
- Refund-Visibility and Prepayment-Deposit classifications are excluded from pay runs at the data layer, not just the UI.
- Cloud sandbox cannot hit the warehouse; build with fixtures, hand live runs to Joe (CloseTool pattern).

## Out of scope for v1

OCR of invoice PDFs. Vendor onboarding workflow. 1099 tracking. Mobile UI. Write-back to QuickBooks. Automated wire/ACH initiation. Automated positive-pay file generation (CFO continues to export from QB after check run). Multi-currency. Early-pay discount logic. Email/Slack notifications (revisit after Phase 4 — TrainingBriefing has a working pattern if needed).

## Recurring bills

No special handling in v1. Recurring bills (Decathlon monthly, Milton bi-weekly payroll, RingCentral, etc.) already exist as individual bills in QB and sync through normally. Forecasting/projection is out of scope.

## Volume targets

- ~200–500 open bills at steady state (estimate from sample; confirm against warehouse).
- 20–40 lines per weekly pay run.
- New bills synced per week: estimate 50–100.

## Placeholders Joe needs to fill before handoff

1. `[JIRA_BASE_URL]` — **DONE.** Set in `.env` (`https://medreppro.atlassian.net/browse/`).
2. `[AP_CLERK_USER_LIST]` — names + emails of AP team members getting logins. **Still pending.**
3. `[GL_CODING_MAP]` — **DONE.** Authored and loaded (25 rules; see [`GL_CODING_MAP_FINAL.md`](GL_CODING_MAP_FINAL.md)).
4. `[HOST_DECISION]` — confirm Azure App Service for Phase 8, or alternative. **Still pending.**

## Handoff workflow

1. Joe creates the `PayablesTool` directory and runs `git init`.
2. Joe drops this `BUILD_PLAN.md` at the repo root.
3. Joe drops the sample `Payment_Run_-_05_21_26__002_.xlsx` into `samples/`.
4. Joe opens Claude Code in plan mode and prompts: "Cold-read BUILD_PLAN.md and samples/Payment_Run_-_05_21_26__002_.xlsx. Propose Phase 0 implementation plan before writing any code."
5. Iterate phase by phase, merging each branch to master before starting the next.
