"""
exports.py -- Phase 5 export routes: CFO Pay-Run Excel + CEO printout.

  GET /pay-runs/<id>/export/cfo.xlsx   (controller, cfo)
  GET /pay-runs/<id>/export/ceo.xlsx   (cfo only)

GET (idempotent re-download), role-gated, audited. Read-only over the data
tables; the only writes are the generated .xlsx into exports/ and one append-only
audit_log row per successful export. Exports are allowed only when the run is
Locked. Versioned filenames (never overwrite). Workbook construction lives in
excel_payrun.py (pure, testable).
"""
import re
from datetime import date
from pathlib import Path

from flask import (Blueprint, abort, flash, redirect, send_file, url_for)
from flask_login import current_user

import db
import excel_payrun
import payruns                   # held_and_notdue_tiers (CEO workpaper tiers 2/3)
import sync                      # log_audit (route layer owns the audit trail)
from auth import role_required

bp = Blueprint("exports", __name__)
EXPORTS_DIR = Path(__file__).resolve().parent / "exports"

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_LINE_SQL = """
SELECT pl.id AS line_id, pl.qb_bill_id, pl.payment_method, pl.amount_to_pay_cents,
       pl.included, pl.line_state, pl.cfo_note,
       b.vendor, b.bill_number, b.bill_date, b.due_date, b.amount_cents, b.qb_memo,
       m.app_category, m.approver_name, m.approval_channel, m.approval_date,
       m.receipt_delivery_date, m.ok_for_ceo, m.obligation_type, m.due_state
FROM pay_run_line pl
JOIN bill b ON b.qb_bill_id = pl.qb_bill_id
LEFT JOIN bill_metadata m ON m.qb_bill_id = pl.qb_bill_id
WHERE pl.pay_run_id = ?
  -- Phase 4.6 export fence (same predicate as the pay-run candidate fence):
  -- a not_due or not_real_ap bill must NEVER appear on a check-run export, even
  -- if it was reclassified after the run was locked. Belt-and-suspenders with
  -- the add-time fence in payruns.candidate_bills.
  AND m.obligation_type IN ('ordinary_ap', 'debt_service')
  AND m.due_state = 'due'
"""


def init_exports(app):
    app.register_blueprint(bp)


def _run_lines(conn, run_id):
    return [dict(r) for r in conn.execute(_LINE_SQL, (run_id,))]


def _next_version(prefix):
    """Next un-used version int + filename for `prefix`. Never overwrites."""
    EXPORTS_DIR.mkdir(exist_ok=True)
    pat = re.compile(re.escape(prefix) + r"_v(\d+)\.xlsx$")
    used = [int(m.group(1)) for p in EXPORTS_DIR.glob(prefix + "_v*.xlsx")
            for m in [pat.search(p.name)] if m]
    v = (max(used) + 1) if used else 1
    return v, f"{prefix}_v{v:02d}.xlsx"


def _datestr(run):
    return run["week_ending"] or date.today().isoformat()


def _generate(run_id, ceo):
    conn = db.get_db()
    run = conn.execute("SELECT * FROM pay_run WHERE id=?", (run_id,)).fetchone()
    if not run:
        abort(404)
    if run["status"] != "Locked":
        flash("Exports are available only once the run is Locked.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))

    lines = _run_lines(conn, run_id)
    payable = excel_payrun.payable_rows(lines, ceo=ceo)
    if not payable:
        flash("No CEO-approved lines (ok_for_ceo) on this run yet." if ceo
              else "This run has no payable lines to export.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))

    if ceo:
        wb = excel_payrun.build_ceo_workbook(run, lines, current_user.name)
        kind, prefix = "ceo", f"PayRun_{run_id}_CEO_{_datestr(run)}"
    else:
        wb = excel_payrun.build_cfo_workbook(run, lines, current_user.name)
        kind, prefix = "cfo", f"PayRun_{run_id}_{_datestr(run)}"

    version, filename = _next_version(prefix)
    path = EXPORTS_DIR / filename
    wb.save(path)

    total_cents = sum((r.get("amount_to_pay_cents") or 0) for r in payable)
    sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_exported",
                   None, {"export": kind, "filename": filename, "version": version,
                          "row_count": len(payable), "total_cents": total_cents,
                          "generated_by": current_user.name})
    conn.commit()
    return send_file(path, as_attachment=True, download_name=filename,
                     mimetype=_XLSX_MIME)


@bp.route("/pay-runs/<int:run_id>/export/cfo.xlsx")
@role_required("controller", "cfo")
def export_cfo(run_id):
    return _generate(run_id, ceo=False)


@bp.route("/pay-runs/<int:run_id>/export/ceo.xlsx")
@role_required("cfo")
def export_ceo(run_id):
    return _generate(run_id, ceo=True)


# Pre-lock states the CEO workpaper may be generated in. Unlike the cfo/ceo
# check-run exports (Locked-only, because they drive disbursement), the
# workpaper is a discussion/review artifact and is allowed from CFO_Approved on.
_WORKPAPER_STATUSES = ("CFO_Approved", "Locked")


@bp.route("/pay-runs/<int:run_id>/export/ceo-workpaper.xlsx")
@role_required("controller", "cfo")
def export_ceo_workpaper(run_id):
    conn = db.get_db()
    run = conn.execute("SELECT * FROM pay_run WHERE id=?", (run_id,)).fetchone()
    if not run:
        abort(404)
    if run["status"] not in _WORKPAPER_STATUSES:
        flash("The CEO workpaper is available once the run is CFO-approved.", "error")
        return redirect(url_for("payruns.detail", run_id=run_id))

    # Tier 1 PAID THIS RUN: the existing payable-lines query, so the paid-tier
    # grand total ties to the CFO export total by construction.
    paid = excel_payrun.payable_rows(_run_lines(conn, run_id), ceo=False)
    # Tiers 2 (HELD BY CHOICE) + 3 (NOT YET DUE) + processing footnote.
    tiers = payruns.held_and_notdue_tiers(conn, run_id)

    wb = excel_payrun.build_ceo_workpaper_workbook(
        run, paid, tiers["held"], tiers["not_yet_due"],
        current_user.name, tiers["processing_count"])

    prefix = f"PayRun_{run_id}_CEO_Workpaper_{_datestr(run)}"
    version, filename = _next_version(prefix)
    path = EXPORTS_DIR / filename
    wb.save(path)

    paid_total_cents = sum((r.get("amount_to_pay_cents") or 0) for r in paid)
    sync.log_audit(conn, current_user.id, "pay_run", run_id, "pay_run_exported",
                   None, {"export": "ceo_workpaper", "filename": filename,
                          "version": version, "paid_total_cents": paid_total_cents,
                          "held_count": len(tiers["held"]),
                          "not_yet_due_count": len(tiers["not_yet_due"]),
                          "processing_count": tiers["processing_count"],
                          "generated_by": current_user.name})
    conn.commit()
    return send_file(path, as_attachment=True, download_name=filename,
                     mimetype=_XLSX_MIME)
