# PayablesTool

Flask + SQLite app that owns bill metadata (approval, dates, notes,
classification) and produces the CFO check-run Excel and the CEO payables
printout from one dataset. Bills are read **read-only** from the Azure SQL
warehouse `QuickBooksReplica`; QuickBooks stays the system of record.

See [`BUILD_PLAN.md`](BUILD_PLAN.md) for the full spec and phase plan, and
[`AGENTS.md`](AGENTS.md) for working rules.

> **Phase 0 — Scaffold.** This is the foundation only: project config, the
> ported Azure connector, a real login (Flask-Login), four seed users, and a
> `/health` smoke-test route. No bill sync, no bill UI, no exports yet — those
> are Phases 1–5.

## Prerequisites

- **Python 3.12** on PATH (`python --version`).
- **Microsoft ODBC Driver 18 for SQL Server.** Azure SQL effectively requires
  it; the legacy "SQL Server" driver will not connect. Check what's installed:

  ```powershell
  python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'SQL Server' in d])"
  ```

  If `ODBC Driver 18 for SQL Server` is **not** in the list, install it:

  ```powershell
  winget install Microsoft.ODBCDriver18ForSQLServer
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

# create payables.db and seed the four users
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
