"""
explore_payables_warehouse.py -- READ-ONLY discovery of the bill-related
schema in the Azure SQL warehouse `QuickBooksReplica`.

Phase 1a. Modeled on CloseTool's azure_explore.py, but scoped to the four
questions PayablesTool needs answered before the sync (Phase 1b) can be
designed:

  Q1. What bill HEADER table(s) exist?
  Q2. Do bill LINES exist as a separate table?
  Q3. Is a VENDOR_TYPE available (column on a vendor table, or a lookup)?
  Q4. Is a GL_ACCOUNT exposed at the bill or line level?

It connects with the already-shipped warehouse.connect_azure() (read-only),
runs a series of catalog + sample probes, and writes ONE self-contained,
timestamped text file to exports/. Every SQL statement is logged inline in
that file with its result, so the output can be pasted back verbatim to
author WAREHOUSE_SCHEMA.md -- no further probing required.

This script NEVER writes to the warehouse and NEVER raises out of a probe:
each probe captures its own error and the run continues. It does not touch
payables.db.

Run on the whitelisted box:   python explore_payables_warehouse.py
"""

import datetime as _dt
import platform
import sys
from pathlib import Path

import warehouse

VERSION = "1.0"

# Schemas to probe (per the Phase 1a spec). Candidate tables in OTHER schemas
# are still listed separately so nothing is missed.
TARGET_SCHEMAS = ["dbo", "reporting", "stg", "raw", "qbo"]

# Name patterns for the header/lookup probe (Q1, Q3).
HEADER_PATTERNS = ["%bill%", "%vendor%", "%purchase%", "%payment%", "%invoice%"]
# Name patterns for the line/detail probe (Q2).
LINE_PATTERNS = ["%bill%line%", "%bill_line%", "%purchase%line%",
                 "%bill%detail%", "%purchase%detail%", "%line%detail%"]

# Column-name hints.
DATEISH_TYPES = {"datetime", "datetime2", "date", "smalldatetime",
                 "datetimeoffset", "timestamp"}
FRESH_HINTS = ("updated", "modified", "refresh", "synced", "sync", "changed",
               "loaded", "created", "_at")
TYPE_HINTS = ("type", "category", "class", "kind")
ACCOUNT_HINTS = ("account", "acct", "gl", "ledger")

SAMPLE_ROWS = 5
COL_WIDTH = 60


# ----------------------------------------------------------------------
# Output writer: everything goes to the file; brief progress to console.
# ----------------------------------------------------------------------

class Out:
    def __init__(self, fh):
        self.fh = fh

    def line(self, s=""):
        self.fh.write(s + "\n")

    def progress(self, s):
        # console only -- so Joe sees it working
        print(s, flush=True)

    def section(self, title, note=""):
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.line()
        self.line("=" * 78)
        self.line(f"[{ts}] {title}")
        if note:
            self.line(f"        {note}")
        self.line("=" * 78)

    def sql(self, sql, params=()):
        self.line("SQL:")
        for ln in sql.strip().splitlines():
            self.line("    " + ln.strip())
        if params:
            self.line(f"PARAMS: {params!r}")

    def result_table(self, cols, rows, max_rows=None):
        if cols is None:
            return
        total = len(rows)
        shown = rows if max_rows is None else rows[:max_rows]
        self.line(f"RESULT: {total} row(s)"
                  + (f" (showing first {len(shown)})" if max_rows and total > max_rows else ""))
        if cols:
            self.line("  | " + " | ".join(str(c) for c in cols))
            self.line("  | " + " | ".join("-" * min(len(str(c)), 12) for c in cols))
        for r in shown:
            cells = []
            for v in r:
                s = "" if v is None else str(v)
                s = s.replace("\n", "\\n").replace("\r", "")
                if len(s) > COL_WIDTH:
                    s = s[:COL_WIDTH] + "..."
                cells.append(s)
            self.line("  | " + " | ".join(cells))

    def error(self, e):
        self.line(f"ERROR: {type(e).__name__}: {e}")


def probe(out, conn, title, sql, params=(), max_rows=None, note=""):
    """Run one SQL probe; log SQL + result (or error) to the file. Returns
    (cols, rows) or (None, None) on error. Never raises."""
    out.section(title, note)
    out.sql(sql, params)
    try:
        cur = conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        out.result_table(cols, rows, max_rows)
        cur.close()
        return cols, rows
    except Exception as e:
        out.error(e)
        return None, None


# ----------------------------------------------------------------------
# Identifier quoting for dynamic schema.table references from the catalog.
# ----------------------------------------------------------------------

def _qi(name):
    return "[" + str(name).replace("]", "]]") + "]"


def _qt(schema, table):
    return f"{_qi(schema)}.{_qi(table)}"


# ----------------------------------------------------------------------
# Per-candidate detail dump (columns, count, freshness, sample).
# ----------------------------------------------------------------------

def dump_table(out, conn, schema, table):
    fq = f"{schema}.{table}"
    out.progress(f"      detailing {fq} ...")

    # columns
    cols, rows = probe(
        out, conn, f"COLUMNS of {fq}",
        """
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    col_meta = []
    if rows:
        for r in rows:
            col_meta.append({"name": r[0], "type": (r[1] or "").lower()})

    # row count
    probe(out, conn, f"ROW COUNT of {fq}",
          f"SELECT COUNT(*) AS row_count FROM {_qt(schema, table)}")

    # freshness: best date-ish column whose name hints at update/modify/etc.
    fresh_col = _pick_freshness_col(col_meta)
    if fresh_col:
        probe(out, conn, f"FRESHNESS of {fq} (MAX {fresh_col})",
              f"SELECT MAX({_qi(fresh_col)}) AS freshest FROM {_qt(schema, table)}",
              note=f"chosen freshness column: {fresh_col}")
    else:
        out.section(f"FRESHNESS of {fq}")
        out.line("(no date/datetime column with an update/created-style name found)")

    # account-ish and type-ish columns flagged for Q3/Q4
    acct_cols = [c["name"] for c in col_meta
                 if any(h in c["name"].lower() for h in ACCOUNT_HINTS)]
    type_cols = [c["name"] for c in col_meta
                 if any(h in c["name"].lower() for h in TYPE_HINTS)]
    if acct_cols:
        out.line(f"  >> account-like columns (Q4 candidates): {acct_cols}")
    if type_cols:
        out.line(f"  >> type/category-like columns (Q3 candidates): {type_cols}")

    # sample rows
    probe(out, conn, f"SAMPLE (TOP {SAMPLE_ROWS}) of {fq}",
          f"SELECT TOP {SAMPLE_ROWS} * FROM {_qt(schema, table)}",
          max_rows=SAMPLE_ROWS)

    return col_meta


def _pick_freshness_col(col_meta):
    dateish = [c for c in col_meta if c["type"] in DATEISH_TYPES]
    if not dateish:
        return None
    for hint in ("updated", "modified", "refresh", "synced", "sync",
                 "changed", "loaded", "created"):
        for c in dateish:
            if hint in c["name"].lower():
                return c["name"]
    # fall back to any date-ish column that hints "_at" or "date"
    for c in dateish:
        n = c["name"].lower()
        if "_at" in n or "date" in n:
            return c["name"]
    return dateish[0]["name"]


# ----------------------------------------------------------------------
# Candidate discovery
# ----------------------------------------------------------------------

def find_candidates(out, conn, title, patterns, note=""):
    """Return list of (schema, table, type) whose name matches any pattern,
    across ALL schemas. Target-schema matches are flagged in the output."""
    where = " OR ".join(["LOWER(table_name) LIKE ?"] * len(patterns))
    sql = (
        "SELECT table_schema, table_name, table_type "
        "FROM information_schema.tables "
        f"WHERE {where} "
        "ORDER BY table_schema, table_name"
    )
    cols, rows = probe(out, conn, title, sql, tuple(patterns), note=note)
    cands = []
    if rows:
        for sch, name, ttype in rows:
            in_target = sch in TARGET_SCHEMAS
            cands.append((sch, name, ttype, in_target))
        out.line()
        out.line("  Candidates (✓ = in a target schema):")
        for sch, name, ttype, in_target in cands:
            flag = "✓" if in_target else " "
            out.line(f"    [{flag}] {sch}.{name}  ({ttype})")
    return cands


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).resolve().parent / "exports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"payables_warehouse_discovery_{ts}.txt"

    print(f"PayablesTool warehouse discovery v{VERSION}")
    print(f"Writing to: {out_path}\n")

    with open(out_path, "w", encoding="utf-8") as fh:
        out = Out(fh)

        # ---- header metadata (for diffing future runs) ----
        out.line("#" * 78)
        out.line("# PAYABLESTOOL WAREHOUSE DISCOVERY")
        out.line(f"# script_version : {VERSION}")
        out.line(f"# run_timestamp  : {_dt.datetime.now().isoformat(timespec='seconds')}")
        out.line(f"# run_utc        : {_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}")
        out.line(f"# python         : {sys.version.split()[0]} ({platform.platform()})")
        try:
            import pyodbc
            out.line(f"# pyodbc         : {pyodbc.version}")
        except Exception as e:
            out.line(f"# pyodbc         : (unavailable: {e})")

        # connect (read-only); driver + server are metadata too.
        try:
            creds = warehouse.load_credentials()           # password not printed
            driver = warehouse.pick_driver()
            out.line(f"# odbc_driver    : {driver}")
            out.line(f"# server         : {creds['server']}")
            out.line(f"# database       : {creds['database']}")
            out.line(f"# user           : {creds['user']}")
            out.line("#" * 78)
            print("Connecting to warehouse (read-only) ...")
            conn = warehouse.connect_azure()
        except warehouse.WarehouseError as e:
            out.line("#" * 78)
            out.error(e)
            out.line("\nABORTED: could not connect. Fix the above and re-run.")
            print(f"\nERROR: {e}")
            print(f"(details written to {out_path})")
            return
        except Exception as e:
            out.line("#" * 78)
            out.error(e)
            print(f"\nERROR: {e}")
            return

        try:
            # server-side metadata
            probe(out, conn, "SERVER METADATA",
                  "SELECT @@VERSION AS sql_server_version, "
                  "DB_NAME() AS current_db, SUSER_SNAME() AS login_name")

            # ---- Section 0: schemas + target-schema inventory ----
            probe(out, conn, "0. ALL SCHEMAS",
                  "SELECT schema_name FROM information_schema.schemata "
                  "ORDER BY schema_name")

            placeholders = ",".join(["?"] * len(TARGET_SCHEMAS))
            probe(out, conn, "0b. ALL TABLES/VIEWS IN TARGET SCHEMAS",
                  "SELECT table_schema, table_type, table_name "
                  "FROM information_schema.tables "
                  f"WHERE table_schema IN ({placeholders}) "
                  "ORDER BY table_schema, table_type, table_name",
                  tuple(TARGET_SCHEMAS),
                  note=f"target schemas: {TARGET_SCHEMAS}")

            # ---- Q1/Q3: header + lookup candidates ----
            print("Probing Q1: bill/vendor/purchase/payment tables ...")
            header_cands = find_candidates(
                out, conn,
                "1. CANDIDATE BILL / VENDOR / PURCHASE / PAYMENT TABLES (Q1, Q3)",
                HEADER_PATTERNS,
                note=f"name LIKE any of {HEADER_PATTERNS}")

            seen = set()
            for sch, name, ttype, in_target in header_cands:
                if not in_target:
                    continue
                seen.add((sch, name))
                dump_table(out, conn, sch, name)

            # ---- Q2: bill-line / detail candidates ----
            print("Probing Q2: bill-line / detail tables ...")
            line_cands = find_candidates(
                out, conn,
                "2. CANDIDATE BILL-LINE / DETAIL TABLES (Q2)",
                LINE_PATTERNS,
                note=f"name LIKE any of {LINE_PATTERNS}")
            for sch, name, ttype, in_target in line_cands:
                if not in_target or (sch, name) in seen:
                    continue
                seen.add((sch, name))
                dump_table(out, conn, sch, name)

            # ---- Findings recap (factual; we interpret together) ----
            out.section("FINDINGS RECAP (factual -- interpret together)")
            hdr = [f"{s}.{n}" for s, n, t, it in header_cands if it]
            lns = [f"{s}.{n}" for s, n, t, it in line_cands if it]
            out.line(f"Q1 header/lookup candidates (target schemas): {hdr or 'NONE'}")
            out.line(f"Q2 line/detail candidates (target schemas):   {lns or 'NONE'}")
            out.line("Q3 (vendor_type) and Q4 (gl_account): see the per-table")
            out.line("  '>> type/category-like' and '>> account-like' notes above,")
            out.line("  plus the SAMPLE rows for actual values.")
            out.line()
            out.line("Next: paste this entire file back to author WAREHOUSE_SCHEMA.md.")

            print("Done.")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    print(f"\nWrote: {out_path}")
    print("Paste the full contents of that file back for the schema write-up.")


if __name__ == "__main__":
    main()
