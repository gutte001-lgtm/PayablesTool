# PayablesTool — Build Spec

## Project context

PayablesTool is a new standalone Flask + SQLite app at `C:\Users\Joe\OneDrive - Healthcare Markets DBA\Desktop\Claude Projects\PayablesTool`. Follow the same conventions as CloseTool and TrainingBriefing: Python 3.11 venv, APScheduler for background jobs, python-dotenv for secrets, `AGENTS.md` for git/merge hygiene rules (same `git merge --no-ff` and `--ff-only` patterns; same `git config --global core.editor notepad` requirement on Windows).

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
                        Submitted_to_CFO / CFO_Approved / Locked / Exported)
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
  → Locked          (no further edits)
  → Exported        (Pay Run Excel and CEO Excel generated; timestamp recorded)
```

After Export, the warehouse sync continues to update each line's paid status by matching to BillPayment records; once paid, the bill drops off active views.

## Phases / branches

Each phase is its own branch off master, merged with `git merge --no-ff -m "<msg>" <branch>` per CloseTool AGENTS.md §6. Don't combine phases.

### Phase 0 — Scaffold (`claude/phase-0-scaffold`)
Repo init, Python 3.11 venv, Flask, Flask-Login, APScheduler, openpyxl, pyodbc, python-dotenv. SQLite at `payables.db`. User table + login. Four seed users (one per role). Azure connection module ported from CloseTool's `warehouse_finance.py`. Smoke test: log in as each role, hit a stub `/health` route that returns warehouse connectivity status.

### Phase 1 — Bill sync (`claude/phase-1-sync`)
Discover the warehouse Bill and BillPayment table/view names (likely `bill`, `bill_line`, `bill_payment` or similar — agent to explore and document in `WAREHOUSE_SCHEMA.md`). APScheduler 15-min job pulls open bills (open_balance > 0) plus a configurable look-back window for recently-paid. Upsert into local `Bill` and `BillLine` tables. Auto-create `BillMetadata` row with `approval_state = New` for any new bill. Manual "Pull Now" button on a `/admin/sync` page. Log every sync run in `AuditLog`.

### Phase 2 — Bill list + detail UI (`claude/phase-2-bill-ui`)
List view at `/bills`: filter by classification, app_category, vendor, approval_state, ok_for_ceo, due_status (Overdue / Current / Not Due — computed), rush_flag. Sort by any column. Search across vendor, bill_number, ops_number, qb_memo. Pagination. Detail view at `/bills/<qb_bill_id>`: all metadata editable (role-gated), append-only notes log, to-do checklist, OPS-number clickable to Jira, link to source bill in QB if practical.

### Phase 3 — Approval workflow (`claude/phase-3-approval`)
State machine transitions exposed as role-gated buttons. AP_Reviewed → Controller_Reviewed (Joe button), and rejection paths with required reason that lands as a Note. AuditLog every transition. Inbox views: `/inbox/controller` shows AP_Reviewed bills; `/inbox/cfo` shows pay runs Submitted_to_CFO.

### Phase 4 — Pay Run builder (`claude/phase-4-pay-run`)
`/pay-runs/new`: pick Controller_Reviewed bills (exclude Refund-Visibility and Prepayment-Deposit automatically), set payment_method per line (Check / Wire / Credit Card / ACH — default from Bill if QB has one), set amount_to_pay (defaults to open_balance, allows partial), include/exclude toggle. `/pay-runs/<id>` shows the run grouped by Contractor sub-buckets and payment method per the uploaded sample. CFO view exposes per-line Approve / Reject with note. Lock action freezes the run.

### Phase 5 — Excel exports (`claude/phase-5-export`)
Two exports, both via openpyxl, both matching the uploaded sample `Payment_Run_-_05_21_26__002_.xlsx`:

1. **Pay Run Excel** — grouped exactly like the uploaded sample: Contractor Checks → Contractor Wire → Contractor Credit Cards → Contractor Total → Checks (everything else by category) → Buys (Pre-owned Devices) → Refunds/Reimbursements → Credit Cards → ACH/Wire → Total. Sub-subtotals at each break. Columns: Vendor, Vendor Type (display as `app_category`), Bill #, Date, Due Date, Amount, Open Balance, Payment Method, Bill Approval (composite `{approver} - {channel}`), Approval Date, Receipt/Delivery, Memo, Notes.
2. **CEO Excel** — same content filtered to `ok_for_ceo = true`, formatted for **landscape print**: set print area, fit-to-width, repeat header rows on each page, page numbers in footer, Arial 10pt body / 11pt bold subtotals. The CEO prefers paper; this must look right when printed without re-formatting.

Both exports stamped with the PayRun id and export timestamp on a footer line. Saved into `/exports/` and made available for download from the pay run detail page.

### Phase 6 — Spend summary (`claude/phase-6-summary`)
Dashboard at `/summary`: open AP by `app_category`, by vendor (top 20), by payment method, by week-due. Pivot live from current bill data, not from any pay run. Export button for each pivot.

### Phase 7 — GL rules engine (`claude/phase-7-rules`)
`/admin/rules` UI to manage `GLRule` rows. On bill sync, evaluate rules in priority order against each bill's `BillLine` rows; first match sets `app_category`. Vendor-type fallback if no rule matches. Re-run rules across all bills on demand. Joe will provide the initial GL coding map: [GL_CODING_MAP_TO_BE_INSERTED]. Common case: New Device GL accounts → `New Device Purchases`; Pre-owned GL accounts → `Pre-owned Device Purchases`.

### Phase 8 — Hosting for multi-user access (`claude/phase-8-deploy`)
v1 ran on Joe's machine; CFO needs access from his. Move to [HOST_DECISION — Azure App Service is the obvious choice given the warehouse is in Azure; confirm with Joe and whoever owns Azure billing]. SQLite stays for v1; revisit Postgres only if multi-user contention shows up. HTTPS, role-based access intact.

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

1. `[JIRA_BASE_URL]` — Jira URL pattern, e.g. `https://healthcaremarkets.atlassian.net/browse/`
2. `[AP_CLERK_USER_LIST]` — names + emails of AP team members getting logins
3. `[GL_CODING_MAP]` — to be added before Phase 7; Phase 7 can be deferred until then
4. `[HOST_DECISION]` — confirm Azure App Service for Phase 8, or alternative

## Handoff workflow

1. Joe creates the `PayablesTool` directory and runs `git init`.
2. Joe drops this `BUILD_PLAN.md` at the repo root.
3. Joe drops the sample `Payment_Run_-_05_21_26__002_.xlsx` into `samples/`.
4. Joe opens Claude Code in plan mode and prompts: "Cold-read BUILD_PLAN.md and samples/Payment_Run_-_05_21_26__002_.xlsx. Propose Phase 0 implementation plan before writing any code."
5. Iterate phase by phase, merging each branch to master before starting the next.
