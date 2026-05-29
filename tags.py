"""
tags.py -- Phase 3.5 data helpers for status pills, bill tags, @mention
parsing, and per-bill "last activity". Leaf module: depends only on db, so
both bills.py and followup.py can use it without an import cycle.

Audit logging is intentionally NOT done here -- routes call sync.log_audit so
the audit trail stays owned at the route layer, like the rest of the app.

Tag lifecycle: a row with cleared_at IS NULL is an ACTIVE tag. Tags never
auto-clear on view or on action -- only an explicit "mark done" by the tagged
user (or a controller) sets cleared_at. (Confirmed with Joe.)
"""

import re

# @mention token: @ + word chars. Matched case-insensitively against username.
MENTION_RE = re.compile(r"@([A-Za-z0-9_]+)")


def parse_mentions(body):
    """Distinct @usernames in a note body, lowercased, in first-seen order."""
    seen, out = set(), []
    for m in MENTION_RE.finditer(body or ""):
        u = m.group(1).lower()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _placeholders(ids):
    return ",".join("?" * len(ids))


# ----------------------------------------------------------------------
# Users (for tag validation + mentions)
# ----------------------------------------------------------------------

def active_user(conn, user_id):
    return conn.execute(
        "SELECT id, name, username FROM users WHERE id=? AND is_active=1",
        (user_id,)).fetchone()


def active_user_by_username(conn, username):
    return conn.execute(
        "SELECT id, name, username FROM users WHERE LOWER(username)=LOWER(?) "
        "AND is_active=1", (username,)).fetchone()


def active_users_excluding(conn, user_id):
    """Active users other than `user_id` -- the tag-someone dropdown."""
    return conn.execute(
        "SELECT id, name, username FROM users WHERE is_active=1 AND id<>? "
        "ORDER BY name", (user_id,)).fetchall()


# ----------------------------------------------------------------------
# Tags
# ----------------------------------------------------------------------

def has_active_tag(conn, bill_id, user_id):
    return conn.execute(
        "SELECT 1 FROM bill_tag WHERE qb_bill_id=? AND tagged_user_id=? "
        "AND cleared_at IS NULL", (bill_id, user_id)).fetchone() is not None


def insert_tag(conn, bill_id, tagged_user_id, tagged_by_user_id, now, note=None):
    cur = conn.execute(
        "INSERT INTO bill_tag (qb_bill_id, tagged_user_id, tagged_by_user_id, "
        "tagged_at, note) VALUES (?,?,?,?,?)",
        (bill_id, tagged_user_id, tagged_by_user_id, now, note))
    return cur.lastrowid


def get_active_tag(conn, tag_id, bill_id):
    return conn.execute(
        "SELECT * FROM bill_tag WHERE id=? AND qb_bill_id=? AND cleared_at IS NULL",
        (tag_id, bill_id)).fetchone()


def clear_tag(conn, tag_id, cleared_by_user_id, now):
    conn.execute(
        "UPDATE bill_tag SET cleared_at=?, cleared_by_user_id=? "
        "WHERE id=? AND cleared_at IS NULL", (now, cleared_by_user_id, tag_id))


def active_tags_for_bill(conn, bill_id):
    """Active tags on one bill with tagged/by names (chip display)."""
    return conn.execute(
        "SELECT t.*, tu.name AS tagged_user_name, bu.name AS tagged_by_name "
        "FROM bill_tag t "
        "LEFT JOIN users tu ON tu.id=t.tagged_user_id "
        "LEFT JOIN users bu ON bu.id=t.tagged_by_user_id "
        "WHERE t.qb_bill_id=? AND t.cleared_at IS NULL ORDER BY t.id",
        (bill_id,)).fetchall()


def active_tags_for_bills(conn, ids):
    """{qb_bill_id: [tag rows with names]} for a set of bills (one query)."""
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT t.qb_bill_id, t.id, t.note, tu.name AS tagged_user_name, "
        "       bu.name AS tagged_by_name "
        "FROM bill_tag t "
        "LEFT JOIN users tu ON tu.id=t.tagged_user_id "
        "LEFT JOIN users bu ON bu.id=t.tagged_by_user_id "
        f"WHERE t.cleared_at IS NULL AND t.qb_bill_id IN ({_placeholders(ids)}) "
        "ORDER BY t.id", tuple(ids)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["qb_bill_id"], []).append(r)
    return out


def tag_counts_for_bills(conn, ids):
    """{qb_bill_id: active_tag_count} for a set of bills (one query)."""
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT qb_bill_id, COUNT(*) AS n FROM bill_tag "
        f"WHERE cleared_at IS NULL AND qb_bill_id IN ({_placeholders(ids)}) "
        "GROUP BY qb_bill_id", tuple(ids)).fetchall()
    return {r["qb_bill_id"]: r["n"] for r in rows}


def tag_count_for_user(conn, user_id):
    """Active tags assigned to a user -- the nav badge. Recomputed per request."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM bill_tag WHERE tagged_user_id=? "
        "AND cleared_at IS NULL", (user_id,)).fetchone()["n"]


def tagged_bills_for_user(conn, user_id):
    """Bills with an active tag for this user -- the "Tagged for you" section.
    One row per bill (dedup guarantees one active tag per user per bill)."""
    return conn.execute(
        "SELECT b.qb_bill_id, b.vendor, b.bill_number, b.amount_cents, "
        "       b.open_balance_cents, m.approval_state, m.status_pill, "
        "       t.id AS tag_id, t.note AS tag_note, t.tagged_at, "
        "       bu.name AS tagged_by_name "
        "FROM bill_tag t "
        "JOIN bill b ON b.qb_bill_id=t.qb_bill_id "
        "LEFT JOIN bill_metadata m ON m.qb_bill_id=t.qb_bill_id "
        "LEFT JOIN users bu ON bu.id=t.tagged_by_user_id "
        "WHERE t.tagged_user_id=? AND t.cleared_at IS NULL "
        "ORDER BY t.tagged_at DESC", (user_id,)).fetchall()


# ----------------------------------------------------------------------
# Status pills
# ----------------------------------------------------------------------

def pill_values(conn):
    """All lookup values, seeds first then alphabetical (dropdown order)."""
    return [r["value"] for r in conn.execute(
        "SELECT value FROM status_pill_lookup ORDER BY is_seed DESC, value")]


def pill_exists(conn, value):
    """Exact-match existence -- used to validate a pill being SET on a bill."""
    return conn.execute(
        "SELECT 1 FROM status_pill_lookup WHERE value=?", (value,)).fetchone() \
        is not None


def pill_exists_ci(conn, value):
    """Case-insensitive existence -- used to reject duplicate ADDs."""
    return conn.execute(
        "SELECT 1 FROM status_pill_lookup WHERE LOWER(value)=LOWER(?)",
        (value,)).fetchone() is not None


# ----------------------------------------------------------------------
# Classification reasons (Phase 4.5) -- extensible dropdown, mirrors the pill
# lookup. Seeds first, then alphabetical. is_seed distinguishes shipped seeds.
# ----------------------------------------------------------------------

def classification_reasons(conn):
    """All reason values, seeds first then alphabetical (dropdown order)."""
    return [r["value"] for r in conn.execute(
        "SELECT value FROM classification_reason_lookup ORDER BY is_seed DESC, value")]


def reason_exists(conn, value):
    """Exact-match existence -- validates a reason being SET on a bill."""
    return conn.execute(
        "SELECT 1 FROM classification_reason_lookup WHERE value=?",
        (value,)).fetchone() is not None


def reason_exists_ci(conn, value):
    """Case-insensitive existence -- rejects duplicate ADDs."""
    return conn.execute(
        "SELECT 1 FROM classification_reason_lookup WHERE LOWER(value)=LOWER(?)",
        (value,)).fetchone() is not None


# ----------------------------------------------------------------------
# Last activity (per bill)
# ----------------------------------------------------------------------

def last_activity_for_bills(conn, ids):
    """{qb_bill_id: latest-activity ISO ts}. Activity = max of last note
    created_at, last bill-scoped audit created_at, and metadata.created_at
    (the floor, so a freshly-synced bill with no notes/audit still has a
    well-defined age). ISO strings sort lexicographically, so MAX works."""
    if not ids:
        return {}
    ph = _placeholders(ids)
    out = {}
    for r in conn.execute(
        f"SELECT qb_bill_id, MAX(created_at) AS ts FROM note "
        f"WHERE qb_bill_id IN ({ph}) GROUP BY qb_bill_id", tuple(ids)):
        out[r["qb_bill_id"]] = r["ts"]
    for r in conn.execute(
        f"SELECT entity_id AS bid, MAX(created_at) AS ts FROM audit_log "
        f"WHERE entity_type IN ('bill','bill_metadata') "
        f"AND entity_id IN ({ph}) GROUP BY entity_id", tuple(ids)):
        if r["ts"] and (r["bid"] not in out or r["ts"] > out[r["bid"]]):
            out[r["bid"]] = r["ts"]
    for r in conn.execute(
        f"SELECT qb_bill_id, created_at AS ts FROM bill_metadata "
        f"WHERE qb_bill_id IN ({ph})", tuple(ids)):
        if r["ts"] and (r["qb_bill_id"] not in out or r["ts"] > out[r["qb_bill_id"]]):
            # only as a floor: keep the later of meta.created_at vs found activity
            out[r["qb_bill_id"]] = max(out.get(r["qb_bill_id"], ""), r["ts"])
    return out


# ----------------------------------------------------------------------
# Open items (Phase 3.6): explicit "this bill needs work" flags. Junction-style
# like tags -- multiple per bill; resolved_at IS NULL = open.
# ----------------------------------------------------------------------

def create_open_item(conn, bill_id, description, created_by_user_id, now):
    cur = conn.execute(
        "INSERT INTO bill_open_item (qb_bill_id, description, created_by_user_id, "
        "created_at) VALUES (?,?,?,?)",
        (bill_id, description, created_by_user_id, now))
    return cur.lastrowid


def resolve_open_item(conn, item_id, bill_id, resolution_note, resolved_by_user_id, now):
    """Resolve an OPEN item. Returns True if it flipped an open item to resolved,
    False if it was already resolved / not found (the WHERE guards double-resolve)."""
    cur = conn.execute(
        "UPDATE bill_open_item SET resolved_at=?, resolved_by_user_id=?, "
        "resolution_note=? WHERE id=? AND qb_bill_id=? AND resolved_at IS NULL",
        (now, resolved_by_user_id, resolution_note, item_id, bill_id))
    return cur.rowcount > 0


def get_open_item(conn, item_id, bill_id):
    return conn.execute(
        "SELECT * FROM bill_open_item WHERE id=? AND qb_bill_id=?",
        (item_id, bill_id)).fetchone()


def open_items_for_bill(conn, bill_id):
    """Unresolved items on one bill, oldest first, with creator name."""
    return conn.execute(
        "SELECT oi.*, u.name AS created_by_name FROM bill_open_item oi "
        "LEFT JOIN users u ON u.id=oi.created_by_user_id "
        "WHERE oi.qb_bill_id=? AND oi.resolved_at IS NULL ORDER BY oi.created_at",
        (bill_id,)).fetchall()


def all_open_items(conn):
    """Every unresolved open item, oldest first (surfaces longest-ignored),
    joined with bill facts + creator name + the bill's status pill."""
    return conn.execute(
        "SELECT oi.id, oi.qb_bill_id, oi.description, oi.created_at, "
        "       u.name AS created_by_name, b.vendor, b.bill_number, "
        "       b.amount_cents, b.open_balance_cents, m.status_pill "
        "FROM bill_open_item oi "
        "JOIN bill b ON b.qb_bill_id=oi.qb_bill_id "
        "LEFT JOIN bill_metadata m ON m.qb_bill_id=oi.qb_bill_id "
        "LEFT JOIN users u ON u.id=oi.created_by_user_id "
        "WHERE oi.resolved_at IS NULL ORDER BY oi.created_at ASC").fetchall()


def open_item_counts_for_bills(conn, ids):
    """{qb_bill_id: open_item_count} for list/inbox row decoration (one query)."""
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT qb_bill_id, COUNT(*) AS n FROM bill_open_item "
        f"WHERE resolved_at IS NULL AND qb_bill_id IN ({_placeholders(ids)}) "
        "GROUP BY qb_bill_id", tuple(ids)).fetchall()
    return {r["qb_bill_id"]: r["n"] for r in rows}


def open_item_total(conn):
    """Total unresolved open items across all bills -- the home nav badge."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM bill_open_item WHERE resolved_at IS NULL"
    ).fetchone()["n"]
