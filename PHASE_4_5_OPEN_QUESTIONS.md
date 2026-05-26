# Phase 4.5 — Open Questions Before Spec Drafting

**Created:** 2026-05-26
**Status:** Awaiting Joe's answers. Spec drafting blocked until resolved.

These are the gaps in the scoping done on 2026-05-26. None require
implementation work; all require Joe's decisions before the spec can be
finalized. Answer each in this doc inline (under the question), then the
spec can be drafted from a complete picture.

## Schema and data model

### Q1. Naming sanity check with AP team.
The scoping settled on `invoice_due_date` (locked contractual) and
`expected_payment_date` (editable forecast). These are textbook accounting
terms. Does this match how Marilyn, Anita, Mandy, and the rest of the AP
team actually talk about these concepts? Or do they have an internal term
("invoice date" / "pay-by date" / "real due" / something else) that should
be used instead?

Answer:

### Q2. Where does `payment_trigger_type` get its default?
Sync defaults all bills to `trigger_type=date_only`. But for categories
like "New Device Purchases" or "Pre-owned Device Purchases" (127 of 497
bills), the trigger probably ought to default to `device_shipped` so they
land in `real_not_due` until you flip the trigger. Should there be a
category→default-trigger mapping at sync time, or does everything start at
`date_only` and you upgrade as needed?

Answer:

### Q3. What does `payment_trigger_met` mean for `date_only`?
Proposal: `trigger_met` auto-derives from `expected_payment_date <= today`
for `date_only` bills. So the boolean isn't stored, it's computed.
Alternative: store the boolean explicitly, with a sync-time job that flips
it daily. Which?

Answer:

### Q4. Can `invoice_due_date` ever change?
QB allows due-date edits. If a vendor revises terms, the contractual due
date legitimately moves. Two options:
(a) Locked at first sync, never updates (preserves audit trail but loses
truth when terms change).
(b) Updates from QB on every sync (preserves truth but the team can game it
through QB).
A middle path: locked from team edits, but updates from QB sync —
documented in audit log every time.

Answer:

## Workflow and access

### Q5. Who can classify?
Joe decides classification per scoping. But who else has the ability to
*flip* the trigger_met flag? AP clerks (Marilyn, Anita) probably know when
inventory was received before Joe does. Is trigger-met flipping a clerk
action, with classification itself locked to Joe + CFO?

Answer:

### Q6. Can CFO override Joe's classification?
Joe classifies, CFO reviews. If CFO disagrees, does he edit directly in
the tool, or does he discuss with Joe and Joe edits? The former is
faster; the latter preserves the "Joe is the classifier" workflow Joe
described.

Answer:

### Q7. What happens to a classification when the bill is paid?
A `real_due` bill goes into a pay run, gets paid, syncs back as paid in
QB. Does classification just become irrelevant (filtered out by `is_paid`)
or does it freeze at last-known state for audit purposes? Probably the
latter, but worth confirming.

Answer:

## UI behavior

### Q8. Bill detail page layout.
The bill detail page gains several fields. Rough sketch needed:
- Read-only: invoice_due_date (with "Contractual — from QB, locked" label).
- Editable: expected_payment_date, classification, classification_reason,
  trigger_type, trigger_met.
- History: last N classification changes, with who/when.

Two questions:
(a) Should classification_reason be a structured field with optional
sub-fields (deposit / placeholder / disputed / waiting-on-service /
other), or pure free text?
(b) Should the team see the audit history on bill detail, or only the
current state? (Audit log still gets every change; question is what's
visible on the detail page.)

Answer:

### Q9. `/summary` aging — does deposit-pending have its own aging?
For `not_real_ap` bills, "days past due" doesn't really apply. For
`real_not_due` bills (waiting on triggers), aging *might* apply — a device
deposit aging 200 days past its expected ship date is meaningful. Should
the dashboard age all three buckets, or only `real_due` + `real_not_due`,
or only `real_due`?

Answer:

### Q10. The `/classifications` (CFO review) page — what's the right shape?
Options:
(a) Flat list of all classified bills (everything not at default), sortable
by last-changed.
(b) Inbox-style: "new since your last view" / "changes this week" /
"open questions."
(c) Side-by-side: pending Joe decisions vs current state vs recent
changes.
What's Shaun's actual review pattern — does he scan everything weekly,
look only at changes since last visit, or react to alerts?

Answer:

## Migration and onboarding

### Q11. The pre-go-live scrub — what's the actual process?
Joe needs to triage all 497 existing bills before team onboarding. Options:
(a) Bulk triage UI: spreadsheet-like view, classify many at once.
(b) Bill-by-bill via the new detail page (slower but more careful).
(c) SQL-direct + spot-check (fastest but no UI).
Which fits Joe's working style?

Answer:

### Q12. What does "go-live" mean operationally?
Defining the moment the team starts using the new model:
- Joe completes 497-bill triage
- All three sub-phases (4.5/4.6/4.7) merged
- A specific announcement / training session with AP team
- A specific pay-run cycle to use as the first "real" cycle
What's the right sequencing? Is there a specific upcoming pay-run cycle
this should land before/after?

Answer:

### Q13. The conversation with the team about no more date-pushing.
When does it happen — before tool change is announced, during onboarding,
or after they hit the locked invoice_due_date field and ask? Different
answers produce different organizational dynamics.

Answer:

## Phase 4.7 preview (notes lifecycle)

Not for this spec, but worth answering now while context is fresh.

### Q14. Notes — who can write them?
Joe + CFO only, or AP clerks too? The "for_ceo_discussion" notes are
CFO-originated by nature; "internal" notes might be useful for clerks too.

Answer:

### Q15. The CFO Briefing report — when is it generated?
Weekly on a fixed day? On-demand by CFO? Both? What's the right anchor for
the "since last report" sections?

Answer:
