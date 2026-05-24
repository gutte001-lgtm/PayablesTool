# PayablesTool

Flask + SQLite app that owns bill metadata (approval, dates, notes,
classification) and produces the CFO check-run Excel and the CEO payables
printout from one dataset. Bills are read **read-only** from the Azure SQL
warehouse `QuickBooksReplica`; QuickBooks stays the system of record.

See [`BUILD_PLAN.md`](BUILD_PLAN.md) for the full spec and phase plan, and
[`AGENTS.md`](AGENTS.md) for working rules.

> **Current state (2026-05-23): Phases 0–4 complete.** Working today: real
> login (Flask-Login) with four seed users and a `/health` route; a 15-minute
> APScheduler warehouse sync (`sync.py`) that mirrors open bills + lines from
> `QuickBooksReplica` and auto-categorizes them via the GL/Class rules engine;
> the bill list/detail UI (`/bills`) with the approval state machine
> (`New → AP_Reviewed → Controller_Reviewed`), append-only notes, to-dos,
> status pills, `@mention` tags, and open items; the `/follow-up` workspace;
> the GL-rules + vendor-default admin (`/admin/rules`) and sync dashboard
> (`/admin/sync`); and the pay-run builder (`/pay-runs`).
> **Not built yet:** Excel exports (Phase 5), the spend dashboard (Phase 6),
> and multi-user hosting (Phase 8). See [`BUILD_PLAN.md`](BUILD_PLAN.md) for the
> phase plan.

## Prerequisites

- **Python 3.12** on PATH (`python --version`).
- **Microsoft ODBC Driver 18 for SQL Server.** Azure SQL effectively requires
  it; the legacy "SQL Server" driver will not connect. Check what's installed:

  ```powershell
  python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'SQL Server' in d])"
  ```

  If `ODBC Driver 18 for SQL Server` is **not** in the list, install it:

  ```powershell
  winget install Microsoft.msodbcsql.18
  ```

  (or download from Microsoft:
  https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)

  When the driver is missing, `warehouse.pick_driver()` raises a clear error
  rather than silently falling back to a driver that can't reach Azure.
- **Warehouse credentials** at `%LOCALAPPDATA%\AzureWarehouse\azure.env`
  (the same file CloseTool uses — already present on Joe's box). The warehouse
  firewall is whitelisted to Joe's box only; from anywhere else the warehouse
  leg of `/health` will report `ok:false`, which is expected.

## Setup

```powershell
# from the repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# configure secrets
Copy-Item .env.example .env
#   then edit .env and set:
#     SECRET_KEY            (python -c "import secrets; print(secrets.token_urlsafe(48))")
#     SEED_DEFAULT_PASSWORD (your chosen dev password for the seed users)

# create payables.db, seed the four users, and seed status pills
python init_db.py

# run
python app.py
# -> http://127.0.0.1:5000
```

## Seed users

`init_db.py` seeds one account per role. The three active accounts share the
`SEED_DEFAULT_PASSWORD` you set in `.env` and are flagged
`must_change_password`. `init_db.py` will not seed without that value set.

| username  | name              | role         | login in v1 |
|-----------|-------------------|--------------|-------------|
| `marilyn` | Marilyn Carson    | `ap_clerk`   | yes         |
| `joe`     | Joe Guttenplan    | `controller` | yes         |
| `shaun`   | Shaun Groat       | `cfo`        | yes         |
| `ceo`     | CEO (name TBD)    | `ceo`        | seeded inactive (no login in v1) |

Emails are placeholders pending `[AP_CLERK_USER_LIST]` in the build plan.
Additional AP clerks (Allen, Robby, …) are added once their emails are known.

## Database migrations

`init_db.py` builds the **full current schema** for a fresh DB. For an existing
`payables.db`, apply the incremental migrations in order — each is idempotent
and exits 0 if already applied. Pause OneDrive first (see
[`AGENTS.md`](AGENTS.md) §8):

```powershell
python migrations/001_phase_3_5.py   # status pills + bill tags
python migrations/002_phase_3_6.py   # open items
python migrations/003_phase_4.py     # pay-run line-review columns + unique index
```

## Tests

Tests are plain Python scripts (no pytest); each prints `ok`/`FAIL` lines and
exits with the failure count. They build throwaway temp DBs and never touch the
live `payables.db`, but they read `SECRET_KEY` from `.env`.

```powershell
python test_phase3.py        # approval workflow + rules regression
python test_phase3_e2e.py    # approval end-to-end
python test_phase_3_5.py     # follow-up workspace
python test_phase_3_6.py     # open items
python test_phase_4.py       # pay-run builder
```

## Phase 0 smoke test

1. `python init_db.py` succeeds and reports the four seeded users.
2. `python app.py` boots without error.
3. Visit `/` → redirected to `/login`. Log in as `marilyn`, then `joe`, then
   `shaun` (the `SEED_DEFAULT_PASSWORD`). Each lands on the home page showing
   the correct role.
4. Hit `/health` (JSON). It reports `app`, `db`, the logged-in `user`, and a
   `warehouse` block. On Joe's whitelisted box with ODBC Driver 18 installed,
   `warehouse.ok` is `true` with a latency. Elsewhere it is `false` with a
   clear reason — expected.
5. `/logout` returns you to the login page.
