# AGENTS.md — PayablesTool

Operating rules for any agent (or human) working in this repo. These mirror
CloseTool's conventions; the git/merge section (§6) is carried over verbatim
in intent because the same Windows + sandbox failure modes apply here.

## 1. What this app is

A standalone Flask + SQLite app that becomes the single source of truth for
bill metadata (approval, dates, notes, classification) and produces the CFO
check-run Excel and the CEO payables printout from one dataset. It reads bills
and bill payments **read-only** from the boss-maintained Azure SQL warehouse
`QuickBooksReplica`. See `BUILD_PLAN.md` for the full spec and phase plan.

## 2. Hard guardrails (never violate)

- **No QuickBooks write-back, ever.** QB stays system of record for bill
  payment and check printing. Even if an API would make a write easy, don't.
  The warehouse connection opens `readonly=True` to enforce this at the driver.
- **All financial dates are typed dates**, never strings and never Excel
  serials. HTML5 date pickers backed by `DATE` columns. (The sample workbook
  leaks both string and serial dates — that's the bug we're replacing.)
- **Notes are append-only** — never edited or deleted; they render as a
  timestamped log.
- **Bill-metadata edits write to `AuditLog`** with before/after.
- **Refund-Visibility and Prepayment-Deposit classifications are excluded
  from pay runs at the data layer**, not just hidden in the UI.
- **Build with fixtures in the sandbox; hand live warehouse legs to Joe** (§7).

## 3. Stack / conventions

- Python 3.12 venv. Raw `sqlite3` for the data layer (thin row helpers, like
  CloseTool — no ORM). Flask-Login for auth, Flask-WTF for CSRF, APScheduler
  for the Phase 1+ sync job, openpyxl for exports, pyodbc for the warehouse.
- SQLite app DB at `payables.db` (gitignored; regenerate via `init_db.py`).
- Secrets in `.env` (gitignored). Warehouse creds live OUTSIDE the repo at
  `%LOCALAPPDATA%\AzureWarehouse\azure.env` — the same file CloseTool reads.

## 4. Phases / branches

Each phase is its own branch off `master`, named `claude/phase-N-<slug>`, and
merged back with `git merge --no-ff -m "<msg>" <branch>`. Don't combine phases.

## 5. Windows git setup

`git config --global core.editor notepad` must be set (it is on Joe's box).
Without it, `git merge --no-ff` (without `-m`) drops into vim and a botched
editor session can leave a merge half-finished with no obvious signal.

## 6. Closing out a feature branch

When the user confirms the work (browser + smoke test):

1. On `master`: `git checkout master && git pull` (if a remote exists), then
   `git merge --no-ff -m "<message>" <feature-branch>`.
2. **Verify the merge landed before deleting anything:** `git log --oneline -3`
   on `master` must show the merge commit at the top. Strict order:
   **merge → (push) → verify on master → only then delete the branch.**
3. Delete the branch once verified.

Two habits that prevent the most common breakage:

1. **Always use `git merge --no-ff -m "<message>" <branch>`**, never bare
   `git merge --no-ff <branch>`. The `-m` supplies the message inline and
   skips the editor entirely.
2. **Verify `master` shows the merge commit before deleting any branch.** If
   it doesn't, the merge didn't land and deleting orphans the work.

### When direct push to `master` is blocked (sandbox HTTP 403)

The cloud sandbox sometimes 403s on direct pushes to `master`. Route through a
shim branch and fast-forward from Windows:

1. **Cloud:** `git push -u origin claude/<short-desc>` (not master).
2. **Windows:** `git fetch origin claude/<short-desc>` →
   `git checkout master` → `git merge --ff-only origin/claude/<short-desc>` →
   `git push origin master` → `git push origin --delete claude/<short-desc>`.

Use `--ff-only` deliberately: the shim should be exactly one commit ahead of
`master`; any other merge mode signals a mistake. If it fails because `master`
moved, stop and reconcile — don't force.

## 7. Two environments — "local" ≠ the cloud sandbox

The warehouse firewall (`quickbooks-sq1`) is whitelisted to Joe's box only.

1. **Joe's Windows box** — its IP is on the allowlist, so `connect_azure()`
   succeeds and `/health` shows the warehouse green. Live legs (sync, tie-out)
   run here.
2. **The cloud Claude Code sandbox** — different egress IP, **not** on the
   allowlist, and may lack the `ODBC Driver 18` install. The warehouse leg of
   `/health` will report `ok:false` here. That is expected. Build + mock with
   fixtures; hand the live warehouse-OK confirmation to Joe.
