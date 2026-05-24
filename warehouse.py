"""
warehouse.py -- read-only Azure SQL connection for PayablesTool.

Ported from CloseTool's warehouse_finance.py (the connection block only --
none of the trial-balance roll-forward logic applies here). Four deliberate
changes were made during the port:

  1. Errors RAISE typed exceptions instead of print()+sys.exit(1). The
     original is a CLI script; this runs inside a long-lived Flask app, so a
     bad connection must be catchable, not fatal to the process.
  2. pick_driver() REFUSES to fall back to the legacy "SQL Server" driver.
     Azure SQL needs ODBC Driver 18 (or 17); the legacy driver yields a
     cryptic TLS failure. We raise a clear, actionable error instead.
  3. The connection opens readonly=True -- PayablesTool never writes to the
     warehouse, and QuickBooks stays system of record (BUILD_PLAN guardrail).
  4. health_check() is added for the /health route: it never raises,
     returning a structured status dict so the route degrades gracefully when
     the warehouse is unreachable (sandbox firewall, missing driver, etc.).

Credentials live OUTSIDE the repo at %LOCALAPPDATA%\\AzureWarehouse\\azure.env
(the same file CloseTool reads), overridable via the AZURE_WAREHOUSE_ENV env
var. Expected keys: AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USER,
AZURE_SQL_PASSWORD.

Scope: this module is connection + health check only. The bill/payment queries
live in sync.py, against the schema documented in WAREHOUSE_SCHEMA.md.
"""

import os
import time
from pathlib import Path


# Preferred ODBC drivers, in order. The legacy "SQL Server" driver is
# intentionally NOT acceptable for Azure SQL.
_ACCEPTABLE_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
)


# ----------------------------------------------------------------------
# Typed errors so Flask routes can catch and report, not crash.
# ----------------------------------------------------------------------

class WarehouseError(Exception):
    """Base class for any warehouse connectivity problem."""


class WarehouseConfigError(WarehouseError):
    """Credentials file missing or incomplete."""


class WarehouseDriverError(WarehouseError):
    """No suitable ODBC driver installed."""


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------

def _resolve_env_path() -> Path:
    """Where the Azure credentials file lives. Honors AZURE_WAREHOUSE_ENV,
    else defaults to %LOCALAPPDATA%\\AzureWarehouse\\azure.env."""
    override = os.environ.get("AZURE_WAREHOUSE_ENV")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "AzureWarehouse" / "azure.env"


def load_credentials() -> dict:
    """Read the Azure SQL credentials. Raises WarehouseConfigError if the
    file is missing or any value is blank."""
    env_path = _resolve_env_path()
    if not env_path.exists():
        raise WarehouseConfigError(
            f"Azure credentials file not found at {env_path}. "
            "Set AZURE_WAREHOUSE_ENV or create the file with "
            "AZURE_SQL_SERVER/DATABASE/USER/PASSWORD."
        )
    from dotenv import dotenv_values
    # dotenv_values reads the file WITHOUT mutating os.environ, so the
    # warehouse creds never leak into the app's process environment.
    values = dotenv_values(env_path)
    creds = {
        "server":   (values.get("AZURE_SQL_SERVER") or "").strip(),
        "database": (values.get("AZURE_SQL_DATABASE") or "").strip(),
        "user":     (values.get("AZURE_SQL_USER") or "").strip(),
        "password": (values.get("AZURE_SQL_PASSWORD") or "").strip(),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise WarehouseConfigError(
            f"{env_path} is missing values for: {', '.join(missing)}."
        )
    return creds


# ----------------------------------------------------------------------
# Driver selection
# ----------------------------------------------------------------------

def pick_driver() -> str:
    """Return the best installed ODBC driver for Azure SQL. Raises
    WarehouseDriverError if neither Driver 18 nor 17 is installed -- we do
    NOT fall back to the legacy 'SQL Server' driver, which cannot reach
    Azure SQL and fails with a confusing TLS error."""
    import pyodbc
    installed = set(pyodbc.drivers())
    for preferred in _ACCEPTABLE_DRIVERS:
        if preferred in installed:
            return preferred
    raise WarehouseDriverError(
        "Microsoft ODBC Driver 18 for SQL Server is not installed "
        f"(found: {sorted(d for d in installed if 'SQL Server' in d) or 'none'}). "
        "Install it with:  winget install Microsoft.msodbcsql.18  "
        "or download from https://learn.microsoft.com/sql/connect/odbc/"
        "download-odbc-driver-for-sql-server"
    )


# ----------------------------------------------------------------------
# Connection (read-only)
# ----------------------------------------------------------------------

def connect_azure(timeout: int = 30):
    """Open a read-only pyodbc connection to the warehouse. Raises a
    WarehouseError subclass on any failure. Caller owns closing it."""
    import pyodbc
    creds = load_credentials()
    driver = pick_driver()
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER=tcp:{creds['server']},1433;"
        f"DATABASE={creds['database']};"
        f"UID={creds['user']};PWD={creds['password']};"
        f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout={timeout};"
    )
    try:
        # readonly=True: PayablesTool never writes to QuickBooks' warehouse.
        return pyodbc.connect(conn_str, readonly=True, timeout=timeout)
    except pyodbc.Error as e:
        raise WarehouseError(f"Azure connection failed: {e}") from e


# ----------------------------------------------------------------------
# Health check -- never raises; returns a status dict for /health.
# ----------------------------------------------------------------------

def health_check() -> dict:
    """Probe warehouse connectivity for the /health route.

    Returns a dict that is always safe to jsonify:
        {ok, driver, server, database, latency_ms, error}
    On failure, ok=False and error carries a human-readable reason; the
    other fields are filled in as far as they were determined.
    """
    result = {
        "ok": False,
        "driver": None,
        "server": None,
        "database": None,
        "latency_ms": None,
        "error": None,
    }
    conn = None
    try:
        creds = load_credentials()
        result["server"] = creds["server"]
        result["database"] = creds["database"]
        result["driver"] = pick_driver()

        start = time.perf_counter()
        conn = connect_azure()
        conn.cursor().execute("SELECT 1").fetchone()
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
        result["ok"] = True
    except WarehouseError as e:
        result["error"] = str(e)
    except Exception as e:  # pyodbc/network errors not wrapped above
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return result


if __name__ == "__main__":
    # Quick CLI probe: python warehouse.py
    import json
    print(json.dumps(health_check(), indent=2))
