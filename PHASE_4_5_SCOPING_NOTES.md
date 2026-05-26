# Phase 4.5 — AP Dueness Classification — Scoping Notes

**Status:** Scoping complete after Q1–Q15 answered. Ready for spec
drafting.
**Owner:** Joe Guttenplan.
**Stakeholder agreement:** CFO (Shaun) walked through the workflow concept
and bought in. CEO never logs in; CFO is the in-app stakeholder.
**Scoped:** 2026-05-26 with review-Claude, revised 2026-05-26 evening
after Q1–Q15 answers.
**Supersedes:** the prior "Phase 4.5 partial payments" placeholder and
the first draft of these notes (single-state classification, fully
manual triggers). Real problem is **classification, not partials.**
Real model is **two-dimensional, not three-state.**

## The actual problem

QuickBooks shows "open AP" = $4.43M today, but that number is wrong in
two independent ways:

1. **Not all of it is real AP.** Some bills are deposit-holding patterns
   (CEO's wife sets up bills against device manufacturing deposits;
   bills exist in QB but the underlying obligation isn't real yet).
   Some bills are placeholders entered by the AP team (Marilyn's
   "fake bills" for inventory not yet produced). Some bills are
   debt-service installments that *are* real obligations but shouldn't
   appear in AP totals because the underlying debt is already booked
   as a liability on the balance sheet.

2. **Even bills that are real AP aren't necessarily due.** Two
   conditions must both hold: a date condition AND a trigger condition
   (inventory received, services performed satisfactorily, device
   manufactured and ready to ship). The QuickBooks `due_date` field
   cannot be trusted as "this is when we should pay" — the AP team has
   been pushing dates as a workaround for CFO's cash forecasting (Q1:
   not bad hygiene, but using the only field QB exposes for the thing
   they actually need to express).

Today's manual workaround: Joe and AP clerks take notes; Joe transfers
notes to weekly reports; CFO reviews, annotates with "need to discuss
with you" items, trims for CEO. CFO recreates notes weekly. Errors
break CEO trust ("any error makes CEO assume everything on list is
wrong"). CEO manages by memory, sometimes unreachable, notes age
silently. The CFO/CEO back-and-forth is actively heavy this month —
this is not a hypothetical workflow.

## The two-dimensional classification model

Every bill has two independent classifications:

**Dimension 1 — Obligation type:** what *kind* of obligation is this?

- `ordinary_ap` — vendor sent an invoice, payment due per terms.
  Ordinary AP lifecycle.
- `debt_service` — paying down a liability already booked on the
  balance sheet (Decathlon loan, other financed obligations). The
  bill in QB represents this month's installment; payment *reduces*
  the liability account. Already recognized as an obligation when the
  loan was signed — not a new AP item.
- `not_real_ap` — bill exists in QB but doesn't represent a true
  obligation yet (deposit-holding pattern, placeholder, anticipated
  inventory not yet produced).

**Dimension 2 — Due state:** is it actually payable right now?

- `due` — yes, pay it
- `not_due` — no, wait

The matrix:

|  | `due` | `not_due` |
|---|---|---|
| `ordinary_ap` | Pay-run eligible. Appears in AP report. | Pipeline / forecast view. Not pay-run eligible. The device-deposit waiting-on-manufacturing case lives here. |
| `debt_service` | Pay-run eligible. **NOT in AP report** (it's a liability paydown). Zero-tolerance for lateness — auto-promotes to `due` on installment date. | Future installment. Not yet visible in AP views. Auto-promotes on date. |
| `not_real_ap` | (impossible cell — if it's not real, it can't be due) | Pipeline / contingent view only. Never pay-run eligible. |

Five meaningful cells, one impossible cell, two independent dimensions.
That's the model.

## Data model (per bill, in bill_metadata)

- `invoice_due_date` — the contractual due date from QB (invoice
  receipt + terms; typically Net 14 or Net 30). Locked from team
  edits; can update from QB sync if the vendor revises terms, with
  an audit log entry every time it changes (option (c) from Q4).
  Source of truth for aging.
- `expected_payment_date` — editable. Defaults to `invoice_due_date`
  on sync. This is **CFO's cash-forecast field** (Q1) — the thing the
  team has been informally using `due_date` for. The team continues
  to use this for forecasting; that's the legitimate use.
- `obligation_type` — enum: `ordinary_ap` (default) / `debt_service`
  / `not_real_ap`.
- `due_state` — enum: `due` / `not_due`. For `debt_service`,
  defaults to `not_due` until installment date arrives, then
  auto-promotes to `due` (Q2 reframe: auto-flip *only* for debt
  service, never for ordinary AP). For `ordinary_ap`, defaults to
  `not_due`; manual flip to `due` when trigger met.
- `classification_reason` — structured dropdown with options:
  `deposit` / `placeholder` / `disputed` / `waiting_on_service` /
  `waiting_on_inventory` / `waiting_on_device_ship` / `debt_service` /
  `other`. Required whenever the classification differs from
  default. Dropdown values are extensible — admin UI to add options
  as needed (Q8a). Optional free-text `classification_note` field
  alongside for context that doesn't fit a dropdown.
- `classified_by` — username. For audit.
- `classified_at` — timestamp. For audit.

For `debt_service` bills, detection is *partly automated*: warehouse
can see which liability account the bill reduces (confirmed by Joe —
warehouse sees everything QB does). Sync logic can default
`obligation_type=debt_service` for bills hitting known liability
accounts. Manual override available.

## State transitions

```text
sync from QB
   │
   ├──→ if bill reduces a known liability account
   │      → obligation_type=debt_service, due_state=not_due
   │      → auto-promote due_state to 'due' when invoice_due_date hits
   │
   └──→ otherwise
          → obligation_type=ordinary_ap (default), due_state=not_due
          │
          ├──→ Joe/clerk classifies as not_real_ap (with reason)
          │     → flagged for CFO visibility, stays in pipeline view
          │
          └──→ trigger met (inventory received / services performed /
                device shipped — manual flip by Joe or AP clerk)
                → due_state=due, pay-run eligible
```

**Auto-promotion rules:**
- `debt_service` bills auto-promote `not_due → due` on
  `invoice_due_date`. Zero-tolerance for lateness; the obligation
  was already decided when the loan was signed.
- `ordinary_ap` bills *never* auto-promote on date alone. A human
  always flips `due_state=due`. This is the safety gate that prevents
  the "blindly trust QB dates" failure mode.

**Workflow effort estimate:** routine debt service (Decathlon, etc.)
is zero-touch — flows through automatically. Ordinary AP requires a
human touch *only* on the exception cases where the trigger isn't met
on first sight (a few times a month per Joe). Routine ordinary AP
(services already complete, inventory already received at time of
sync) gets the trigger flip during normal review, not as separate
work.

## Surface area (what changes across the existing tool)

- **Schema:** `bill_metadata` gets new columns; migration sets
  defaults for existing 497 bills (`obligation_type=ordinary_ap`,
  `due_state=not_due`); Joe's pre-go-live triage corrects them.
- **Sync logic:** populate `invoice_due_date` on first sync; subsequent
  syncs can update it (audit-logged); populate `expected_payment_date`
  as editable; default `obligation_type` based on liability-account
  detection; default `due_state=not_due`.
- **`/bills`:** classification badges (obligation type + due state);
  filter by classification.
- **Bill detail page:** read-only `invoice_due_date` (labeled
  "Contractual — from QB, locked from team edits"); editable
  `expected_payment_date`, `obligation_type`, `due_state`,
  `classification_reason`, `classification_note`. Classification
  change history visible on the page (last N changes, who/when/from/to
  — Q8b). Trigger-met flip is one click; classification change opens a
  reason dropdown.
- **Access:** AP clerks (Marilyn, Anita, etc.) can flip `due_state`
  (trigger-met) and write internal notes. Classification
  (`obligation_type`) is editable by Joe and CFO; CFO can override
  Joe's classification directly (Q6 — Shaun is the boss).
- **`/follow-up`:** `ordinary_ap + due` only (with a separate section
  for `ordinary_ap + not_due` showing "waiting on triggers" with
  expected payment dates).
- **`/summary`:** redesign to two-headline structure (Q9):
  - **"Right Now AP"** — `ordinary_ap + due` only, aged by
    `invoice_due_date` (Current / 1–30 / 31–60 / 61–90 / 90+). This
    is the BOD-defensible AP number.
  - **"Pipeline / Contingent AP"** — `ordinary_ap + not_due` and
    `not_real_ap`, bucketed by `expected_payment_date` (this week /
    this month / next month / later / no date set). CFO's cash
    forecast view.
  - **"Debt Service"** — `debt_service + due` and `debt_service +
    not_due`, shown separately. Upcoming installments visible for
    cash planning, but not folded into AP totals.
  - The dashboard answers two questions at a glance: "what do we
    owe right now?" and "what's coming?"
- **Pay-run builder:** hard-fenced. Only `ordinary_ap + due` and
  `debt_service + due` are eligible. `not_real_ap` and `not_due` bills
  cannot be added even by override. The most important operational
  safety guard in this whole phase.
- **Phase 5 CFO/CEO exports:** updated to filter by classification.
  Pay-run exports include only `ordinary_ap + due` and `debt_service +
  due`. New "BOD AP report" export shows `ordinary_ap` aging
  (`due` + `not_due` in separate sections), excludes `debt_service`
  and `not_real_ap`.
- **New `/classifications` page (CFO review surface):** Shaun's
  weekly-plus-on-demand surface (Q10). Inbox-style with a flat-list
  search underneath:
  - "Changes since your last visit" — top section, surfaces new
    classifications and recently-edited reasons
  - "Open `not_real_ap` items" — what Joe has flagged as not-real,
    with reasons CFO can defend to CEO
  - "Pending trigger flips" — `ordinary_ap + not_due` bills aging
    past their `expected_payment_date` (stale-trigger warning)
  - Searchable flat list below for ad-hoc lookup during CEO
    back-and-forth
- **Audit log:** every classification change (`obligation_type`,
  `due_state`, `classification_reason`, `expected_payment_date`,
  `invoice_due_date` updates from QB sync), with who/when/from-what-
  to-what. Non-negotiable given BOD reporting implications.

## Pre-go-live triage flow (AI-assisted)

Joe triages all 497 bills before any team onboarding (Q11 — manual
review, but AI-assisted heuristic suggester is welcome).

New `/triage` page walks Joe through the 497 bills one at a time
(or a filtered subset), pre-populated with heuristic-based
suggestions:

- Bill reduces a liability account → suggest `obligation_type=
  debt_service`, reason=`debt_service`
- Bill in "New Device Purchases" / "Pre-owned Device Purchases"
  category AND has a BillPayment already applied in QB (deposit
  pattern) → suggest `obligation_type=not_real_ap`, reason=`deposit`
- Bill in "Notes Payable" category → suggest `obligation_type=
  debt_service` (likely a loan)
- Default → suggest `obligation_type=ordinary_ap`, `due_state=
  not_due` (Joe flips trigger if appropriate)

Joe accepts the suggestion or overrides it in one click + reason.
Keyboard shortcuts for speed. 497 bills at ~10s each = ~80 minutes
of focused work. The triage page is itself a Phase 4.5 deliverable.

LLM-assisted classification (calling Claude API with bill details and
asking it to classify) is *out of scope* for v1 — the heuristic-based
suggester gets 80% of the value with no API dependency. Revisit if
Joe finds the heuristics insufficient.

## Sub-phase split — revised phasing

Original recommendation was "build the whole rethink coherently, onboard
the team when 4.5/4.6/4.7 all merge." Revised after Q10 — the CFO/CEO
back-and-forth is heavy *right now*, and Shaun benefits from
classification-capture immediately even before dashboards are redesigned.

**Phase 4.5 — Dueness data model + safety fence (ship FAST):**
- Schema migration: new columns on `bill_metadata`
- Sync logic: liability-account detection for debt service,
  `invoice_due_date` populated and audit-logged on updates,
  `expected_payment_date` editable
- Bill detail UI: classification editor + change history
- `/bills`: classification badges and filters
- `/triage`: AI-assisted pre-go-live triage flow
- Pay-run builder: hard-fenced to `due` only (the most important
  operational safety guard)
- Audit log on all classification changes
- Access model: clerks flip `due_state`; Joe+CFO own `obligation_type`

Ship this first. Onboard Shaun immediately when it lands. He starts
capturing classifications in the tool; the captured data accumulates
value for Phase 4.6 and 4.7 to build against. AP team onboarding
holds until 4.6/4.7 are also live.

**Phase 4.6 — Reporting alignment:**
- `/summary` two-headline redesign (Right Now AP + Pipeline +
  Debt Service)
- Phase 5 export filtering (pay-run exports → `due` only)
- New BOD AP report export
- `/classifications` CFO review page

**Phase 4.7 — Notes lifecycle + CFO Briefing:**
- Notes with audience (`internal` / `for_ceo_discussion` /
  `ceo_visible`)
- Notes states (`open` / `resolved` / `superseded`) with aging
- Resolution capture (CEO weighed in, decision recorded)
- CFO Briefing report — generated weekly on Mondays + on-demand
  (Q15). Combines AP buckets with note status, surfaces stale
  notes (CEO hasn't weighed in), and produces the artifact Shaun
  currently types by hand
- Note write access: AP clerks write `internal`, Joe+CFO write
  `for_ceo_discussion`, only CFO writes `ceo_visible` (Q14)

## Key decisions made during scoping

1. **Two-dimensional classification:** `obligation_type` (ordinary /
   debt / not real) × `due_state` (due / not due). Captures the
   debt-service distinction cleanly.
2. **Debt service auto-promotes on date; ordinary AP never does.**
   Reflects the accounting reality: debt is recognized when the loan
   is signed; AP requires per-bill validation.
3. **Naming:** `invoice_due_date` (locked, contractual) and
   `expected_payment_date` (editable, CFO's cash forecast field).
   The team's existing date-pushing habit is a *legitimate use case*
   we're giving a proper home.
4. **`invoice_due_date` can update from QB sync** (audit-logged) but
   not from team edits. Vendor-revised terms reflected; team gaming
   prevented.
5. **Migration strategy:** Joe triages all 497 bills before team
   onboarding via the AI-assisted `/triage` flow. Eliminates data
   pollution entirely; tool hasn't been used in daily team workflow
   yet so this is feasible.
6. **Phasing:** ship 4.5 fast and onboard Shaun immediately given
   active CEO back-and-forth. 4.6 and 4.7 follow.
7. **Access:** AP clerks flip `due_state` (trigger-met) and write
   internal notes. Joe+CFO own `obligation_type`. CFO can override
   anything; he's the boss.
8. **Classification reason is structured (dropdown), extensible**
   (admin can add options), with optional free-text note alongside.
9. **CFO is bought in.** Shaun reviewed the concept and agreed.
   Largest adoption risk removed.
10. **Team conversation about the new model happens at rollout**
    (Q13 — Joe handles, framed as "we built you a proper field for
    the forecast you've been doing").
11. **The placeholder-bill habit gets a better replacement.** Team
    marks bills `not_real_ap` with reason rather than entering
    fictional bills with fictional due dates. Second-order win:
    cleans up QB itself.

## What this is not

- **Not partial payments.** PayablesTool doesn't originate partial
  payments. Partials in this business are upstream events (CEO's
  wife applies deposits in QB). The tool models the *consequence* —
  open balance with classification state — not the partial-payment
  mechanic itself.
- **Not a CEO interface.** CEO never logs in. All CEO-facing
  artifacts are Excel/PDF exports the CFO sends.
- **Not an automatic trigger detector.** Inventory receipt, services
  performed, device shipped — these are manual flags. The only
  auto-detection in scope is `debt_service` based on liability
  account (warehouse-visible).
- **Not LLM-based classification.** The triage suggester uses
  deterministic heuristics, not LLM calls. Revisit if heuristics
  prove insufficient.

## Tests of "right"

This phase ships correctly when:

1. `/summary` "Right Now AP" can be defended to the board — the
   number on the dashboard matches what's actually a real, currently-
   due obligation. Excludes debt service (booked elsewhere) and
   not-real-AP (no obligation yet).
2. The AP team's date-pushing habit is replaced by `expected_payment_
   date` (a legitimate forecast field) — `invoice_due_date` is
   architecturally locked.
3. A pay run cannot accidentally include a `not_real_ap` or `not_due`
   bill (hard fence in pay-run builder, enforced at SQL level not
   just UI).
4. Debt service installments auto-promote on date and never go late.
5. CFO can pull up any classification and see Joe's reason without
   asking Joe. Change history visible on bill detail.
6. The weekly CFO-to-CEO report becomes a generated artifact, not a
   typed-from-scratch document (Phase 4.7 specifically).
7. Audit log has a complete trail of every classification change for
   regulatory/BOD defense.
8. The pre-go-live triage flow lets Joe classify 497 bills in under
   two hours with AI-assisted suggestions.

## Open follow-ups (not blocking spec drafting)

- **Vendor-by-vendor liability account map.** When Phase 4.5 lands,
  Joe + agent will need to confirm which liability accounts in the
  warehouse correspond to `debt_service` bills. Decathlon is the
  known case; other loans (if any) need to be identified during
  triage.
- **Admin UI for `classification_reason` dropdown options.** Ships
  with the seed set listed in the data model; admin UI to add new
  options can be a small additional task in 4.5 or deferred to 4.6.
- **Migration of the 497 existing bills:** initial state is
  `obligation_type=ordinary_ap, due_state=not_due`, all reasons null.
  Triage corrects them. Joe accepts the "everything starts as
  ordinary_ap and gets triaged" model as the cleanest.
