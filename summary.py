"""
summary.py -- Phase 6 Spend Summary dashboard at /summary.

Read-only analytics over current bill data. No schema change, no migration, no
DB writes. "Open AP" = open_balance_cents > 0 AND is_paid = 0; sums
open_balance_cents. Same convention used in bills.py / followup.py.

One landscape-printable page, four sections:
  1. Header band  -- Total Open AP, bill count, Uncategorized count, "as of"
  2. Aging        -- Current / 1-30 / 31-60 / 61-90 / 90+ / No due date
  3. Categories   -- by bill_metadata.app_category; Uncategorized pinned bottom
  4. Top vendors  -- top 20 by Open $ + "All other vendors (N)" reconciling row

All three pivots tie to the same grand total (the header band Total Open AP).

Pure helpers (compute_*, build_summary_workbook) take fetched row dicts and
return shape-ready structures, so math + workbook are unit-testable without
Flask or a DB -- the excel_payrun.py pattern.

Excel export: single in-memory .xlsx with 4 sheets (Summary/Aging/Categories/
Top Vendors). Snapshot-on-demand, not written to exports/ and not audited --
this is read-only analytics, not a financial artifact of record like the
Phase 5 CFO/CEO pay-run exports.
"""
from datetime import date, datetime, timezone
from io import BytesIO

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from flask import Blueprint, render_template, send_file
from flask_login import login_required

import db
import dates as dates_mod

bp = Blueprint("summary", __name__)


def init_summary(app):
    app.register_blueprint(bp)


# -- constants --------------------------------------------------------------

UNCATEGORIZED = "Uncategorized"
TOP_VENDORS_N = 20

# Display order. NO_DUE_DATE_LABEL is a separate row appended below the buckets.
AGING_LABELS = ["Current", "1–30", "31–60", "61–90", "90+"]
NO_DUE_DATE_LABEL = "No due date"
AGING_ROWS = AGING_LABELS + [NO_DUE_DATE_LABEL]

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# -- DB queries -------------------------------------------------------------

def _fetch_open_bills(conn):
    """Open AP rows. LEFT JOIN bill_metadata so a missing metadata row degrades
    to NULL app_category (folded into Uncategorized) instead of dropping the bill."""
    rows = conn.execute(
        "SELECT b.qb_bill_id, b.vendor, b.bill_date, b.due_date, "
        "       b.open_balance_cents, m.app_category "
        "FROM bill b LEFT JOIN bill_metadata m ON m.qb_bill_id = b.qb_bill_id "
        "WHERE b.open_balance_cents > 0 AND b.is_paid = 0"
    ).fetchall()
    return [dict(r) for r in rows]


def _last_sync_at(conn):
    """Canonical 'as of' timestamp: latest audit_log action='sync_run' row's
    created_at (the same source admin._latest_sync uses). Falls back to
    MAX(bill.last_synced_at). Returns ISO string or None."""
    r = conn.execute(
        "SELECT created_at FROM audit_log WHERE action='sync_run' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if r and r["created_at"]:
        return r["created_at"]
    r = conn.execute("SELECT MAX(last_synced_at) AS m FROM bill").fetchone()
    return r["m"] if r and r["m"] else None


# -- pure pivot computations ------------------------------------------------

def _open_total(rows):
    return sum((r.get("open_balance_cents") or 0) for r in rows)


def aging_bucket(due_iso, today):
    """Bucket label for a due_date. NULL -> NO_DUE_DATE_LABEL; dpd<=0 -> Current;
    1..30, 31..60, 61..90 -> their bands; dpd>=91 -> '90+'."""
    if not due_iso:
        return NO_DUE_DATE_LABEL
    dd = dates_mod._as_date(due_iso)
    if dd is None:
        return NO_DUE_DATE_LABEL
    dpd = (today - dd).days
    if dpd <= 0:
        return "Current"
    if dpd <= 30:
        return "1–30"
    if dpd <= 60:
        return "31–60"
    if dpd <= 90:
        return "61–90"
    return "90+"


def compute_header(rows, last_sync_iso):
    return {
        "total_open_cents": _open_total(rows),
        "bill_count": len(rows),
        "uncategorized_count": sum(
            1 for r in rows
            if (r.get("app_category") or UNCATEGORIZED) == UNCATEGORIZED
        ),
        "as_of": last_sync_iso,
    }


def compute_aging_buckets(rows, today):
    """Returns 6 rows in display order: [{label, bill_count, open_cents, pct}].
    Sums to the grand total (modulo rounding on pct)."""
    total = _open_total(rows)
    agg = {lbl: {"bill_count": 0, "open_cents": 0} for lbl in AGING_ROWS}
    for r in rows:
        lbl = aging_bucket(r.get("due_date"), today)
        agg[lbl]["bill_count"] += 1
        agg[lbl]["open_cents"] += r.get("open_balance_cents") or 0
    out = []
    for lbl in AGING_ROWS:
        a = agg[lbl]
        pct = (a["open_cents"] / total * 100.0) if total else 0.0
        out.append({"label": lbl, "bill_count": a["bill_count"],
                    "open_cents": a["open_cents"], "pct": pct})
    return out


def compute_category_concentration(rows):
    """Group by app_category (NULL -> 'Uncategorized'). Sort Open $ desc;
    Uncategorized pinned to the bottom so the eye lands on real categories
    but it stays visible as a hygiene flag."""
    total = _open_total(rows)
    agg = {}
    for r in rows:
        cat = r.get("app_category") or UNCATEGORIZED
        a = agg.setdefault(cat, {"bill_count": 0, "open_cents": 0})
        a["bill_count"] += 1
        a["open_cents"] += r.get("open_balance_cents") or 0
    items = sorted(agg.items(), key=lambda kv: kv[1]["open_cents"], reverse=True)
    items = ([kv for kv in items if kv[0] != UNCATEGORIZED]
             + [kv for kv in items if kv[0] == UNCATEGORIZED])
    out = []
    for cat, a in items:
        pct = (a["open_cents"] / total * 100.0) if total else 0.0
        out.append({"label": cat, "bill_count": a["bill_count"],
                    "open_cents": a["open_cents"], "pct": pct})
    return out


def compute_top_vendors(rows, n=TOP_VENDORS_N):
    """Top N vendors by Open $ desc, each with oldest_bill_date. Appends an
    'All other vendors (N)' reconciling row so the column ties to the grand
    total. Returns [{label, bill_count, oldest_bill_date, open_cents, pct,
    vendor_exact, other_vendor_count}]; vendor_exact is the literal bill.vendor
    string for drill-down (None on the reconciling row)."""
    total = _open_total(rows)
    agg = {}
    for r in rows:
        v = r.get("vendor") or ""
        a = agg.setdefault(v, {"bill_count": 0, "open_cents": 0, "oldest": None})
        a["bill_count"] += 1
        a["open_cents"] += r.get("open_balance_cents") or 0
        bd = dates_mod._as_date(r.get("bill_date"))
        if bd is not None and (a["oldest"] is None or bd < a["oldest"]):
            a["oldest"] = bd
    items = sorted(agg.items(), key=lambda kv: kv[1]["open_cents"], reverse=True)
    top, rest = items[:n], items[n:]
    out = []
    for v, a in top:
        pct = (a["open_cents"] / total * 100.0) if total else 0.0
        out.append({
            "label": v or "(blank)",
            "bill_count": a["bill_count"],
            "oldest_bill_date": a["oldest"].isoformat() if a["oldest"] else None,
            "open_cents": a["open_cents"], "pct": pct,
            "vendor_exact": v or None,
            "other_vendor_count": None,
        })
    if rest:
        bc = sum(a["bill_count"] for _, a in rest)
        oc = sum(a["open_cents"] for _, a in rest)
        pct = (oc / total * 100.0) if total else 0.0
        out.append({
            "label": f"All other vendors ({len(rest)})",
            "bill_count": bc, "oldest_bill_date": None,
            "open_cents": oc, "pct": pct,
            "vendor_exact": None,
            "other_vendor_count": len(rest),
        })
    return out


# -- workbook builder (pure) ------------------------------------------------

_MONEY_FMT = "#,##0.00"
_PCT_FMT = "0.0%"
_DATE_FMT = "mm/dd/yyyy"

_HDR_FONT = Font(name="Arial", size=10, bold=True)
_BODY_FONT = Font(name="Arial", size=10)
_TOTAL_FONT = Font(name="Arial", size=11, bold=True)
_TITLE_FONT = Font(name="Arial", size=12, bold=True)
_FOOT_FONT = Font(name="Arial", size=8, italic=True)


def _setup_landscape(ws):
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "1:1"
    ws.oddFooter.center.text = "Page &P of &N"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")


def _write_table(ws, headers, rows, widths, *, money_cols=(), pct_cols=(),
                 date_cols=(), total_row=None):
    """Generic 1-indexed table writer starting at row 1. money/pct/date cols
    are 1-based indices. total_row is an optional bold footer."""
    for c, h in enumerate(headers, 1):
        ws.cell(1, c, h).font = _HDR_FONT
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    r = 2
    for row in rows:
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v); cell.font = _BODY_FONT
            if c in money_cols:
                cell.number_format = _MONEY_FMT
            elif c in pct_cols:
                cell.number_format = _PCT_FMT
            elif c in date_cols and v is not None:
                cell.number_format = _DATE_FMT
        r += 1
    if total_row is not None:
        for c, v in enumerate(total_row, 1):
            cell = ws.cell(r, c, v); cell.font = _TOTAL_FONT
            if c in money_cols:
                cell.number_format = _MONEY_FMT
            elif c in pct_cols:
                cell.number_format = _PCT_FMT
        r += 1
    ws.freeze_panes = "A2"


def build_summary_workbook(data):
    """data keys: header, aging, categories, top_vendors, today_iso.
    Returns an openpyxl.Workbook with 4 sheets: Summary, Aging, Categories,
    Top Vendors. All sheets are set up for landscape print."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ----- Summary (header band) -----
    ws = wb.create_sheet("Summary")
    _setup_landscape(ws)
    ws.cell(1, 1, "Open AP Summary").font = _TITLE_FONT
    ws.cell(2, 1, f"As of: {data['header'].get('as_of') or 'never synced'}"
            ).font = _BODY_FONT
    ws.cell(3, 1, f"Generated: {_now_iso()}").font = _FOOT_FONT
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 22
    ws.cell(5, 1, "Total Open AP").font = _HDR_FONT
    v = ws.cell(5, 2, data["header"]["total_open_cents"] / 100.0)
    v.font = _TOTAL_FONT; v.number_format = _MONEY_FMT
    ws.cell(6, 1, "Open bill count").font = _HDR_FONT
    ws.cell(6, 2, data["header"]["bill_count"]).font = _BODY_FONT
    ws.cell(7, 1, "Uncategorized count").font = _HDR_FONT
    ws.cell(7, 2, data["header"]["uncategorized_count"]).font = _BODY_FONT

    # ----- Aging -----
    ws = wb.create_sheet("Aging")
    _setup_landscape(ws)
    rows = [[b["label"], b["bill_count"], b["open_cents"] / 100.0, b["pct"] / 100.0]
            for b in data["aging"]]
    tot_c = sum(b["open_cents"] for b in data["aging"])
    tot_n = sum(b["bill_count"] for b in data["aging"])
    _write_table(
        ws, ["Bucket", "Bill count", "Open $", "% of total"], rows,
        widths=[16, 12, 16, 12], money_cols=(3,), pct_cols=(4,),
        total_row=["TOTAL", tot_n, tot_c / 100.0, 1.0 if tot_c else 0.0],
    )

    # ----- Categories -----
    ws = wb.create_sheet("Categories")
    _setup_landscape(ws)
    rows = [[c["label"], c["bill_count"], c["open_cents"] / 100.0, c["pct"] / 100.0]
            for c in data["categories"]]
    tot_c = sum(c["open_cents"] for c in data["categories"])
    tot_n = sum(c["bill_count"] for c in data["categories"])
    _write_table(
        ws, ["Category", "Bill count", "Open $", "% of total"], rows,
        widths=[40, 12, 16, 12], money_cols=(3,), pct_cols=(4,),
        total_row=["TOTAL", tot_n, tot_c / 100.0, 1.0 if tot_c else 0.0],
    )

    # ----- Top Vendors -----
    ws = wb.create_sheet("Top Vendors")
    _setup_landscape(ws)
    rows = []
    for v in data["top_vendors"]:
        oldest = dates_mod._as_date(v["oldest_bill_date"]) if v["oldest_bill_date"] else None
        rows.append([v["label"], v["bill_count"], oldest,
                     v["open_cents"] / 100.0, v["pct"] / 100.0])
    tot_c = sum(v["open_cents"] for v in data["top_vendors"])
    tot_n = sum(v["bill_count"] for v in data["top_vendors"])
    _write_table(
        ws, ["Vendor", "Bill count", "Oldest bill date", "Open $", "% of total"], rows,
        widths=[40, 12, 18, 16, 12], money_cols=(4,), pct_cols=(5,), date_cols=(3,),
        total_row=["TOTAL", tot_n, None, tot_c / 100.0, 1.0 if tot_c else 0.0],
    )
    return wb


# -- routes -----------------------------------------------------------------

def _gather(conn, today):
    rows = _fetch_open_bills(conn)
    return {
        "header": compute_header(rows, _last_sync_at(conn)),
        "aging": compute_aging_buckets(rows, today),
        "categories": compute_category_concentration(rows),
        "top_vendors": compute_top_vendors(rows),
        "today_iso": today.isoformat(),
    }


@bp.route("/summary")
@login_required
def summary():
    data = _gather(db.get_db(), date.today())
    return render_template("summary.html", **data)


@bp.route("/summary/export.xlsx")
@login_required
def export_xlsx():
    today = date.today()
    data = _gather(db.get_db(), today)
    wb = build_summary_workbook(data)
    bio = BytesIO(); wb.save(bio); bio.seek(0)
    filename = f"MRP_AP_Summary_{today.isoformat()}.xlsx"
    return send_file(bio, as_attachment=True, download_name=filename,
                     mimetype=_XLSX_MIME)
