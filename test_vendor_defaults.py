"""
test_vendor_defaults.py -- vendor-default categorization layer (the dormant
vendor_category_default fallback, now activated with a vendor picker + toggle).

Plain-Python style (no pytest), matching test_phase_4_5.py: check(label, cond);
exit code == number of failures; run with `python test_vendor_defaults.py`.

  * PURE (sqlite only, throwaway temp DBs): precedence (manual > GL rule >
    vendor default > Uncategorized), ID-keying (matches on vendor_ref, never
    name), recompute-on-change (add/toggle/delete flip categories via
    recompute_all), and the picklist exclusion query.
  * ROUTE (Flask test client, fresh temp DB at db.DB_PATH): controller add/
    toggle/delete write audit rows and recompute; vendor_name is resolved from
    the bill when the form omits it; ap_clerk is 403 on writes and read-only.

The live payables.db is NEVER opened. Route tests need SECRET_KEY in .env.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

FAILURES = []
_TMPDIRS = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import db          # noqa: E402
import init_db     # noqa: E402
import sync        # noqa: E402


def _new_path():
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    return d / "t.db"


def fresh_sqlite():
    p = _new_path()
    cn = sqlite3.connect(p); cn.row_factory = sqlite3.Row
    cn.executescript(init_db.SCHEMA)
    cn.commit()
    return p, cn


def add_bill(cn, bid, vendor_ref, vendor):
    cn.execute("INSERT INTO bill (qb_bill_id,vendor_ref,vendor,bill_number,"
               "amount_cents,open_balance_cents,bill_date,due_date,is_paid,"
               "last_synced_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
               (bid, vendor_ref, vendor, "B" + bid, 10000, 10000,
                "2026-05-01", "2026-06-15", "t"))
    cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,created_at,"
               "updated_at) VALUES (?, 'New','t','t')", (bid,))


def add_line(cn, bid, name, n=1, amt=10000):
    cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,"
               "gl_account_number,line_amount_cents) VALUES (?,?,?,?,?)",
               (bid, n, name, sync.parse_gl_number(name), amt))


def add_vendor_default(cn, vendor_id, category, vendor_name="V", active=1):
    cn.execute("INSERT INTO vendor_category_default (vendor_id,vendor_name,"
               "default_category,active,created_by,created_at,updated_at) "
               "VALUES (?,?,?,?,NULL,'t','t')",
               (vendor_id, vendor_name, category, active))


def cat_of(cn, bid):
    return cn.execute("SELECT app_category, app_category_source FROM bill_metadata "
                      "WHERE qb_bill_id=?", (bid,)).fetchone()


# ======================================================================
print("=" * 60); print("precedence (compute_app_category, pure)"); print("=" * 60)

RULE = {"id": 1, "match_type": "gl_account_name_like", "match_value": "%WIDGET%",
        "target_category": "Widgets", "priority": 100}
L_HIT = {"line_amount_cents": 10000, "gl_account_name": "WIDGET COGS",
         "gl_account_number": None, "gl_account_path": None, "qb_class_name": None}
L_MISS = {"line_amount_cents": 10000, "gl_account_name": "MYSTERY ACCOUNT",
          "gl_account_number": None, "gl_account_path": None, "qb_class_name": None}

c, s, _ = sync.compute_app_category([L_HIT], [RULE], "VendorCat", None)
check("GL rule wins over vendor default", (c, s) == ("Widgets", "gl_rule:1"))
c, s, _ = sync.compute_app_category([L_MISS], [RULE], "VendorCat", None)
check("vendor default fires when no GL rule matches", (c, s) == ("VendorCat", "vendor_default"))
c, s, _ = sync.compute_app_category([L_MISS], [RULE], None, None)
check("Uncategorized when neither matches", (c, s) == ("Uncategorized", "uncategorized"))
c, s, _ = sync.compute_app_category([L_HIT], [RULE], "VendorCat", "ManualCat")
check("manual override beats both rule and vendor default", (c, s) == ("ManualCat", "manual"))


# ======================================================================
print("=" * 60); print("ID-keying: matches vendor_ref, not name"); print("=" * 60)

p, cn = fresh_sqlite()
# Two bills, SAME vendor name, DIFFERENT vendor_ref. No GL rules at all.
add_bill(cn, "B1", vendor_ref="91", vendor="UPS (V)")
add_bill(cn, "B2", vendor_ref="999", vendor="UPS (V)")
add_line(cn, "B1", "MYSTERY"); add_line(cn, "B2", "MYSTERY")
add_vendor_default(cn, "91", "Freight", vendor_name="UPS (V)")
cn.commit()
sync.recompute_all(cn)
check("ID-keying: matching vendor_ref gets the default",
      cat_of(cn, "B1")["app_category"] == "Freight")
check("ID-keying: same NAME but different vendor_ref does NOT get it",
      cat_of(cn, "B2")["app_category"] == "Uncategorized")
check("ID-keying: _load_vendor_defaults is keyed by vendor_id",
      sync._load_vendor_defaults(cn) == {"91": "Freight"})
cn.close()


# ======================================================================
print("=" * 60); print("recompute-on-change: add / toggle / delete"); print("=" * 60)

p, cn = fresh_sqlite()
add_bill(cn, "U1", vendor_ref="500", vendor="Acme")
add_line(cn, "U1", "MYSTERY")
cn.commit()
sync.recompute_all(cn)
check("baseline: no rule, no default -> Uncategorized",
      cat_of(cn, "U1")["app_category"] == "Uncategorized")

add_vendor_default(cn, "500", "Other Operating Expenses"); cn.commit()
r = sync.recompute_all(cn)
check("add default -> bill recategorized", cat_of(cn, "U1")["app_category"] == "Other Operating Expenses")
check("add default -> source is vendor_default", cat_of(cn, "U1")["app_category_source"] == "vendor_default")
check("recompute_all reports the change", r["changed"] == 1)

# idempotent: a second recompute changes nothing
r2 = sync.recompute_all(cn)
check("recompute idempotent (0 changed on rerun)", r2["changed"] == 0)

# deactivate -> reverts
cn.execute("UPDATE vendor_category_default SET active=0 WHERE vendor_id='500'"); cn.commit()
sync.recompute_all(cn)
check("deactivated default -> reverts to Uncategorized",
      cat_of(cn, "U1")["app_category"] == "Uncategorized")

# reactivate then delete -> reverts
cn.execute("UPDATE vendor_category_default SET active=1 WHERE vendor_id='500'"); cn.commit()
sync.recompute_all(cn)
check("reactivated default -> applies again",
      cat_of(cn, "U1")["app_category"] == "Other Operating Expenses")
cn.execute("DELETE FROM vendor_category_default WHERE vendor_id='500'"); cn.commit()
sync.recompute_all(cn)
check("deleted default -> reverts to Uncategorized",
      cat_of(cn, "U1")["app_category"] == "Uncategorized")
cn.close()


# ======================================================================
print("=" * 60); print("picklist exclusion query"); print("=" * 60)

PICKLIST_SQL = (
    "SELECT b.vendor_ref, b.vendor FROM bill b "
    "JOIN (SELECT vendor_ref, MAX(qb_updated_at) AS mu FROM bill GROUP BY vendor_ref) x "
    "  ON x.vendor_ref = b.vendor_ref AND x.mu = b.qb_updated_at "
    "WHERE b.vendor_ref NOT IN (SELECT vendor_id FROM vendor_category_default) "
    "GROUP BY b.vendor_ref ORDER BY b.vendor")
p, cn = fresh_sqlite()
cn.execute("INSERT INTO bill (qb_bill_id,vendor_ref,vendor,last_synced_at,qb_updated_at) "
           "VALUES ('A','100','Alpha','t','2026-05-02')")
cn.execute("INSERT INTO bill (qb_bill_id,vendor_ref,vendor,last_synced_at,qb_updated_at) "
           "VALUES ('B','200','Beta','t','2026-05-03')")
add_vendor_default(cn, "100", "Freight", vendor_name="Alpha"); cn.commit()
picks = {r["vendor_ref"] for r in cn.execute(PICKLIST_SQL)}
check("picklist excludes vendors that already have a default", "100" not in picks)
check("picklist includes vendors without a default", "200" in picks)
cn.close()


# ======================================================================
# ROUTE TESTS
# ======================================================================
if not dotenv_values(ROOT / ".env").get("SECRET_KEY"):
    print("\nSKIPPING ROUTE TESTS: no SECRET_KEY in .env")
else:
    PW = generate_password_hash("testpw")
    USERS = [("marilyn", "Marilyn", "ap_clerk"), ("joe", "Joe", "controller")]

    def fresh_route_db():
        d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
        db.DB_PATH = d / "vd.db"
        cn = sqlite3.connect(db.DB_PATH)
        cn.executescript(init_db.SCHEMA)
        for u, name, role in USERS:
            cn.execute("INSERT INTO users (username,name,role,password_hash,is_active) "
                       "VALUES (?,?,?,?,1)", (u, name, role, PW))
        # an Uncategorized bill from vendor_ref 91 ('UPS (V)'), no GL rules
        cn.execute("INSERT INTO bill (qb_bill_id,vendor_ref,vendor,bill_number,"
                   "amount_cents,open_balance_cents,bill_date,due_date,is_paid,"
                   "last_synced_at,qb_updated_at) "
                   "VALUES ('RB','91','UPS (V)','B1',10000,10000,'2026-05-01',"
                   "'2026-06-15',0,'t','2026-05-02')")
        cn.execute("INSERT INTO bill_metadata (qb_bill_id,approval_state,app_category,"
                   "app_category_source,created_at,updated_at) "
                   "VALUES ('RB','New','Uncategorized','uncategorized','t','t')")
        cn.execute("INSERT INTO bill_line (qb_bill_id,line_number,gl_account_name,"
                   "line_amount_cents) VALUES ('RB',1,'MYSTERY',10000)")
        # a second vendor with no default, to test picklist
        cn.execute("INSERT INTO bill (qb_bill_id,vendor_ref,vendor,last_synced_at,"
                   "qb_updated_at) VALUES ('RB2','92','VistaPrint (V)','t','2026-05-02')")
        cn.commit(); cn.close()

    from app import app  # noqa: E402
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["TESTING"] = True

    def client(username):
        c = app.test_client()
        c.post("/login", data={"username": username, "password": "testpw"})
        return c

    def rconn():
        cn = sqlite3.connect(db.DB_PATH); cn.row_factory = sqlite3.Row
        return cn

    def audit_actions(entity_id):
        cn = rconn()
        rows = [r["action"] for r in cn.execute(
            "SELECT action FROM audit_log WHERE entity_type='vendor_default' "
            "AND entity_id=? ORDER BY id", (entity_id,))]
        cn.close()
        return rows

    print("=" * 60); print("route: controller add resolves name + recomputes + audits"); print("=" * 60)
    fresh_route_db()
    joe = client("joe")
    # submit ONLY vendor_id + category (picker omits the name) -> route resolves it
    resp = joe.post("/admin/vendor-defaults/add",
                    data={"vendor_id": "91", "default_category": "Freight"},
                    follow_redirects=True)
    check("add: 200 after redirect", resp.status_code == 200)
    cn = rconn()
    row = cn.execute("SELECT * FROM vendor_category_default WHERE vendor_id='91'").fetchone()
    cn.close()
    check("add: row created", row is not None)
    check("add: vendor_name resolved from bill ('UPS (V)')",
          row and row["vendor_name"] == "UPS (V)")
    check("add: default_category stored", row and row["default_category"] == "Freight")
    cn = rconn()
    bcat = cn.execute("SELECT app_category FROM bill_metadata WHERE qb_bill_id='RB'").fetchone()[0]
    cn.close()
    check("add: recompute_all flipped the bill to Freight", bcat == "Freight")
    check("add: audit row vendor_default_set", "vendor_default_set" in audit_actions("91"))

    print("=" * 60); print("route: picklist excludes the now-defaulted vendor"); print("=" * 60)
    body = joe.get("/admin/rules").get_data(as_text=True)
    check("picklist: defaulted vendor_ref 91 not offered", 'value="91"' not in body)
    check("picklist: undefaulted vendor_ref 92 offered", 'value="92"' in body)

    print("=" * 60); print("route: controller toggle"); print("=" * 60)
    joe.post("/admin/vendor-defaults/91/toggle", follow_redirects=True)
    cn = rconn()
    active = cn.execute("SELECT active FROM vendor_category_default WHERE vendor_id='91'").fetchone()[0]
    rbcat = cn.execute("SELECT app_category FROM bill_metadata WHERE qb_bill_id='RB'").fetchone()[0]
    cn.close()
    check("toggle: active flipped to 0", active == 0)
    check("toggle: bill reverted to Uncategorized after disable", rbcat == "Uncategorized")
    check("toggle: audit row vendor_default_toggle", "vendor_default_toggle" in audit_actions("91"))

    print("=" * 60); print("route: controller delete"); print("=" * 60)
    joe.post("/admin/vendor-defaults/91/delete", follow_redirects=True)
    cn = rconn()
    gone = cn.execute("SELECT COUNT(*) FROM vendor_category_default WHERE vendor_id='91'").fetchone()[0]
    cn.close()
    check("delete: row removed", gone == 0)
    check("delete: audit row vendor_default_delete", "vendor_default_delete" in audit_actions("91"))

    print("=" * 60); print("route: ap_clerk access (403 on writes, read-only page)"); print("=" * 60)
    fresh_route_db()
    mar = client("marilyn")
    check("access: ap_clerk add -> 403",
          mar.post("/admin/vendor-defaults/add",
                   data={"vendor_id": "91", "default_category": "Freight"}).status_code == 403)
    check("access: ap_clerk toggle -> 403",
          mar.post("/admin/vendor-defaults/91/toggle").status_code == 403)
    check("access: ap_clerk delete -> 403",
          mar.post("/admin/vendor-defaults/91/delete").status_code == 403)
    page = mar.get("/admin/rules")
    body = page.get_data(as_text=True)
    check("access: ap_clerk sees the rules page (200)", page.status_code == 200)
    check("access: ap_clerk page renders no add-default form (read-only)",
          "/admin/vendor-defaults/add" not in body)


# ----------------------------------------------------------------------
import shutil
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(len(FAILURES))
print("ALL PASS")
sys.exit(0)
