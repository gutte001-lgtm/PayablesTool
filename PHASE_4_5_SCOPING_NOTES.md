# Phase 4.5 — AP Dueness Classification — Scoping Notes

**Status:** Scoping complete, awaiting full spec.
**Owner:** Joe Guttenplan.
**Stakeholder agreement:** CFO (Shaun) walked through and bought in.
**Scoped:** 2026-05-26 with review-Claude.
**Supersedes:** the prior "Phase 4.5 partial payments" placeholder, which
was scoping based on incorrect framing. Real problem is classification, not
partials.

## The actual problem

QuickBooks shows "open AP" = $4.43M today, but that number is wrong in two
independent ways:

1. **Not all of it is real AP.** Some bills are deposit-holding patterns
   (CEO's wife sets up bills against device manufacturing deposits; bills
   exist in QB but the underlying obligation isn't real yet). Some bills are
   placeholders entered by the AP team (Marilyn's "fake bills" for inventory
   not yet produced) — these distort BOD reporting and aging metrics.

2. **Even bills that are real AP aren't necessarily due.** Two independent
   conditions must both hold: date condition (contractual due date reached)
   AND trigger condition (inventory received, services performed, device
   shipped). Today the team manages this with notes that don't survive
   handoffs, and by pushing due_date forward in QB — which destroys the
   contractual due date and pollutes aging.

Today's manual workaround: Joe takes notes, transfers them to weekly reports,
sends to CFO. CFO reviews, annotates, trims for CEO. CFO recreates notes
weekly. Errors break CEO trust ("any error makes CEO assume everything on
list is wrong"). CEO manages by memory, occasionally forgets, sometimes
unreachable — notes age silently.

## The three-state classification

Every bill, on sync from QB, gets one of three classifications:

| State | Meaning | BOD report? | Pay-run eligible? |
|---|---|---|---|
| `not_real_ap` | Bill exists in QB but doesn't represent a true obligation (deposits, placeholders, holding patterns) | No | No |
| `real_not_due` | Real obligation, but date OR trigger condition not met | Yes | No |
| `real_due` | Real obligation AND date met AND trigger met | Yes | Yes |

Classification can change over time. A device deposit syncs in as
`not_real_ap` → ship-ready → promotes to `real_not_due` → due date hits AND
device confirmed shipped → `real_due`.

## Data model (per bill, in bill_metadata)

- `invoice_due_date` — the contractual due date from QB, **locked at sync**.
  Source of truth for aging. Never modified by the team.
- `expected_payment_date` — editable. Defaults to `invoice_due_date` on
  sync. What the team has been informally using `due_date` for.
- `ap_classification` — enum: `real_due` / `real_not_due` / `not_real_ap`.
  Default: `real_not_due`.
- `classification_reason` — text. Required whenever classification is
  non-default. The CFO-visible "why."
- `payment_trigger_type` — enum: `date_only` (default; majority of bills
  including Decathlon-style recurring), `inventory_received`,
  `services_performed`, `device_shipped`, `other`.
- `payment_trigger_met` — boolean. For `date_only`, auto-true when
  `expected_payment_date <= today`. For others, manually flipped.
- `classified_by` — username (for audit).
- `classified_at` — timestamp (for audit).

## State machine

```text
sync from QB
    ↓
real_not_due (default; trigger_type=date_only; trigger_met=date<=today)
    ↓                                       ↓
manual: → not_real_ap                  date passes OR trigger
        + reason required               manually flipped
                                            ↓
                                       real_due (pay-run eligible)
```

Routine bills (ordinary AP, Decathlon loans) auto-promote `real_not_due` →
`real_due` when their date arrives. **No manual intervention needed for
routine bills.** Manual work only happens for the exception cases — a few
times a month per Joe.

## Surface area (what changes across the existing tool)

- **Schema:** bill_metadata gets new columns; migration sets defaults for
  existing 497 bills.
- **Sync logic:** populate `invoice_due_date` on first sync (locked
  thereafter); populate `expected_payment_date` as editable.
- **`/bills`:** classification badge per row; filter by classification.
- **Bill detail page:** edit `expected_payment_date`, classification,
  reason, trigger type, trigger met. Show `invoice_due_date` read-only with
  a label like "Contractual — from QB, locked."
- **`/follow-up`:** real_due bills only (with a separate section for
  real_not_due "waiting on triggers"?).
- **`/summary`:** redesign. Header band stacks "Real Due: $X / Real Not
  Due: $Y / Not Real AP: $Z / Total in books: $X+Y+Z." Aging uses
  `invoice_due_date` and is real-AP-only. Categories and Top Vendors
  real-AP-only with toggle.
- **Pay-run builder:** hard-fenced to `real_due` only. Operational safety.
- **Phase 5 CFO/CEO exports:** filter to `real_due` for pay-run exports;
  new BOD AP report shows `real_due` + `real_not_due`, excludes
  `not_real_ap`.
- **New `/classifications` page (or similar):** CFO review surface. Lists
  Joe's classification decisions with reasons, recent changes, filterable.
- **Audit log:** every classification or trigger flip, with who/when/from-
  what-to-what. Non-negotiable given BOD reporting implications.

## Sub-phase split

Three sequential branches, all merged before team onboarding. Build the
whole rethink coherently rather than shipping incrementally to the live
team.

- **Phase 4.5 — Dueness data model:** schema, sync logic, bill detail UI,
  audit log, pay-run safety fence, `/bills` filter/badge.
- **Phase 4.6 — Reporting alignment:** `/summary` redesign, Phase 5 export
  filtering, new BOD AP report, `/classifications` review page.
- **Phase 4.7 — CFO notes lifecycle:** notes with audience (internal /
  for_ceo_discussion / ceo_visible), states (open / resolved / superseded),
  aging, resolution capture, CFO Briefing report combining notes with
  classification status.

## Key decisions made during scoping

1. **Naming.** `invoice_due_date` (contractual, locked) and
   `expected_payment_date` (editable). Push back during spec writing if the
   AP team has different internal vocabulary.
2. **Migration strategy.** Full scrub before go-live. Tool hasn't been used
   in daily team workflow yet; all 497 existing bills triaged by Joe before
   handoff to team. Eliminates data pollution entirely.
3. **Build best tool, then onboard.** All three sub-phases merge before
   team starts using. No "live with broken aging while we finish dashboard"
   window.
4. **Default classification on sync = `real_not_due`** with
   `trigger_type=date_only` and `trigger_met=(date<=today)`. Routine bills
   work without manual intervention; exception cases get explicit
   classification.
5. **CFO is bought in.** Shaun reviewed the workflow concept and agreed.
   Removes the largest adoption risk.
6. **The placeholder-bill habit (Marilyn's "fake bills") gets a better
   replacement.** Team can mark bills `not_real_ap` with reason rather than
   entering fictional bills. Second-order win: cleans up QB itself, not
   just the local tool. Requires onboarding conversation with team.

## What this is not

- **Not partial payments.** PayablesTool doesn't originate partial payments.
  Partials in this business are upstream events (CEO's wife applies deposits
  in QB). The tool models the *consequence* — open balance with classification
  state — not the partial-payment mechanic itself.
- **Not a CEO interface.** CEO never logs in. All CEO-facing artifacts are
  Excel/PDF exports the CFO sends.
- **Not an automatic trigger detector.** Inventory receipt, services
  performed, device shipped — these are manual flags. No integration with
  shipping/ops systems in scope.

## Tests of "right"

This phase ships correctly when:

1. `/summary` Total Open AP can be defended to the board — the number on
   the dashboard matches what's actually a real obligation.
2. The AP team's date-pushing habit stops causing aging pollution
   (architecturally prevented; `invoice_due_date` is locked).
3. A pay run cannot accidentally include a `not_real_ap` or
   `real_not_due` bill (hard fence in pay-run builder).
4. CFO can pull up any classification and see Joe's reason without asking
   Joe.
5. The weekly CFO-to-CEO report becomes a generated artifact, not a
   typed-from-scratch document. (This is Phase 4.7's specific job.)
6. Audit log has a complete trail of every classification change for
   regulatory/BOD defense.
