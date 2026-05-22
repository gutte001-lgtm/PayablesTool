# Phase 1a — Warehouse discovery: how to run

`explore_payables_warehouse.py` is a **read-only** probe of the Azure SQL
warehouse `QuickBooksReplica`. It answers the four questions PayablesTool's
sync needs, and writes **one self-contained, timestamped text file** to
`exports/` that you paste back so we can author `WAREHOUSE_SCHEMA.md`.

## What it answers
- **Q1** — which bill **header** table(s) exist
- **Q2** — whether bill **lines** exist as a separate table
- **Q3** — whether a **vendor_type** is available (vendor table column or lookup)
- **Q4** — whether a **gl_account** is exposed at the bill or line level

## Prerequisites (on your whitelisted box)
- `.venv` active (or any Python with `pyodbc` + `python-dotenv`).
- **ODBC Driver 18 for SQL Server** installed
  (`winget install Microsoft.msodbcsql.18`).
- `%LOCALAPPDATA%\AzureWarehouse\azure.env` present (the file CloseTool uses).
- You must run it from your box — the warehouse firewall is whitelisted to it.

## Run
```powershell
.\.venv\Scripts\Activate.ps1
python explore_payables_warehouse.py
```
It prints brief progress to the console and writes the full detail to:
```
exports\payables_warehouse_discovery_<YYYYMMDD_HHMMSS>.txt
```
Takes roughly 1–4 minutes depending on table sizes.

## Safety
- Connects with `readonly=True`; **never writes** to the warehouse.
- **Never touches** `payables.db`.
- **Never raises out of a probe** — each probe captures its own error and the
  run continues, so one bad table can't abort the whole map.

## What to send back
Paste the **entire contents** of the `exports\payables_warehouse_discovery_*.txt`
file. It is self-contained:
- The **password is never printed** (only server / database / user / driver).
- It **does** include real bill and vendor sample rows (TOP 5 per table) and
  every SQL statement run — that's expected and is what lets us map columns
  without another round trip.
- The header block records script version, run timestamps, Python version,
  ODBC driver, and server, so future runs can be diffed if the boss ever
  changes the replica schema.

## Troubleshooting (the error lands in the output file too)
- **Driver error** → `winget install Microsoft.msodbcsql.18`.
- **Firewall / login error (40615 / "not allowed to access the server")** →
  you're not on the whitelisted box, or the IP changed. Run from your box.
- **Credentials error** → check the four keys in `azure.env`.

## After this
From your pasted output we author `WAREHOUSE_SCHEMA.md` (its own commit), then
move to **Phase 1b** (the APScheduler sync + bill list/detail UI).
