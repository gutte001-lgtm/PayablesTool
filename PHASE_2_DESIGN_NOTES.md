# Phase 2 — design notes & Marilyn's daily workflow

The UI in Phase 2 is built around how Marilyn (ap_clerk) actually works a
pay-run week, plus Joe's (controller) review pass. When the screens land,
verify they match *this sequence*, not just that fields save in isolation.

## Roles in Phase 2
- **ap_clerk (Marilyn, Allen, Robby)** — sync, browse, fill metadata, classify,
  add notes/todos, mark a bill reviewed (`New → AP_Reviewed`).
- **controller (Joe)** — everything ap_clerk can do, plus
  `AP_Reviewed → Controller_Reviewed`, and edits to any bill.
- **cfo (Shaun)** — can browse and add notes in Phase 2. Line approve/reject is
  Phase 3/4.
- **ceo** — no login in v1.

> Phase 2 includes only the **forward** transitions needed to make `/inbox`
> drain (`New → AP_Reviewed → Controller_Reviewed`). Rejections, required-reason
> bounce-backs, and CFO line actions are Phase 3.

## Marilyn's daily workflow (the sequence to verify against)

1. **Open the app → land on Home.** Sees nav: Inbox, Bills, Sync, Rules.
2. **Click Inbox (`/inbox`).** Auto-scoped to her queue: bills at
   `approval_state='New'` (newly synced, not yet reviewed). She is *not*
   filtering 239 bills — she sees only what needs her.
3. **Click the first bill → Bill detail (`/bills/<id>`).**
   - Left/top: **read-only QB facts** (vendor, bill #, dates, amount, open
     balance, memo) and the **GL line table** with the **category breakdown**
     (clean vs split bill obvious at a glance).
   - **Fill metadata** (one form, one **Save**): classification, approver +
     channel, approval/service/receipt dates (HTML5 date pickers), proposed
     payment method + pay date, ok_for_ceo, rush, partial-payment. Optionally
     set a **manual category override** if the rules guessed wrong.
   - **Save changes** → one atomic `audit_log` entry (before/after).
   - Add a **note** (append-only log) or a **to-do** if something's pending.
   - **Mark reviewed** (`New → AP_Reviewed`) → the bill leaves her inbox.
4. **Next bill.** Repeat. The inbox shrinks as she works.
5. **Weekly: bulk-classify the future-dated bills.** Filter Bills by
   **future-dated**, select all, **Bulk classify → Prepayment-Deposit**
   (modal confirms the count + target). Those bills are now permanently
   excluded from "Real payable" totals — no re-keying each week.
6. **Eyeball the KPI bar.** Bills/Sync show **Total open / Current
   (bill_date ≤ today) / Real payable**. The Current number is what ties to
   QB's AP aging; Real payable strips Prepayment-Deposit/Refund-Visibility.

## Joe's review pass (controller)
1. **Inbox (`/inbox`)** scoped to `approval_state='AP_Reviewed'` — what Marilyn
   has finished.
2. Open a bill, sanity-check metadata + classification + category, fix anything,
   add a note, **Mark reviewed** (`AP_Reviewed → Controller_Reviewed`).
3. The per-bill **audit panel** shows the recent change history (who/what/when)
   so review is traceable.

## Key behaviors to verify
- **Dates** are real date pickers; typing a non-ISO string is rejected
  server-side (no Excel-serial leak).
- **Refund-Visibility / Prepayment-Deposit** classification forces
  `ok_for_ceo = 0`.
- **Manual category override** wins over the computed category and survives the
  next sync; clearing it reverts to the rules result.
- **KPI bar on `/bills` reflects the active filter**; on `/admin/sync` it's
  global.
- **Notes never edit/delete** (DB triggers); the audit panel and notes log are
  both append-only.
- **Jira (OPS-#) links open in a new tab** so the filtered list isn't lost.

## Out of scope for Phase 2 (later phases)
Rejections / bounce-backs / CFO line approvals (Phase 3); pay-run builder
(Phase 4); Excel exports (Phase 5); spend dashboard (Phase 6); QB deep-link
(deferred pending a confirmed QBO URL pattern).
