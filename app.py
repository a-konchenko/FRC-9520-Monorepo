"""Inventory management server.

Run:  python app.py            (serves on port 8000, reachable on your LAN)
      python app.py --port 9000
"""
import argparse
import csv
import io
import re
import socket
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "inventory.db"
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # per request

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sku           TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    category      TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    quantity      INTEGER NOT NULL DEFAULT 0,
    reorder_level INTEGER NOT NULL DEFAULT 0,
    unit_price    REAL NOT NULL DEFAULT 0,
    notes         TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    change     INTEGER NOT NULL,
    reason     TEXT DEFAULT '',
    qty_after  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT UNIQUE NOT NULL COLLATE NOCASE,
    color                 TEXT NOT NULL DEFAULT '#64748b',
    description           TEXT DEFAULT '',
    default_reorder_level INTEGER NOT NULL DEFAULT 0,
    default_location      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS item_links (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    label   TEXT DEFAULT '',
    url     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    original_name TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL COLLATE NOCASE,
    notes      TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
-- How many of an item are in use, per group (group_id NULL = in use, no group).
CREATE TABLE IF NOT EXISTS usage (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES item_groups(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL CHECK (quantity > 0)
);
-- Tools lent to a person. Open while returned_at is NULL.
CREATE TABLE IF NOT EXISTS checkouts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    person         TEXT NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    note           TEXT DEFAULT '',
    due_at         TEXT,
    checked_out_at TEXT NOT NULL,
    returned_at    TEXT,
    return_note    TEXT DEFAULT ''
);
"""

# Items with their in-use, checked-out and available counts. Recreated on startup.
ITEM_VIEW = """
DROP VIEW IF EXISTS item_view;
CREATE VIEW item_view AS
SELECT i.*,
       COALESCE(u.n, 0) AS in_use,
       COALESCE(c.n, 0) AS checked_out,
       i.quantity - COALESCE(u.n, 0) - COALESCE(c.n, 0) AS available
FROM items i
LEFT JOIN (SELECT item_id, SUM(quantity) AS n FROM usage GROUP BY item_id) u
       ON u.item_id = i.id
LEFT JOIN (SELECT item_id, SUM(quantity) AS n FROM checkouts
           WHERE returned_at IS NULL GROUP BY item_id) c
       ON c.item_id = i.id;
"""

ITEM_FIELDS = ("sku", "barcode", "name", "category", "location", "quantity",
               "reorder_level", "unit_price", "notes")
CATEGORY_FIELDS = ("name", "color", "description",
                   "default_reorder_level", "default_location", "checkout")


# ---------- database helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    UPLOAD_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # movements.kind: 'stock' (total changed), 'in_use' or 'return'
        cols = [r[1] for r in db.execute("PRAGMA table_info(movements)")]
        if "kind" not in cols:
            db.execute("ALTER TABLE movements ADD COLUMN kind TEXT NOT NULL DEFAULT 'stock'")
        # categories.checkout: items in the category show on the Checkout page
        cols = [r[1] for r in db.execute("PRAGMA table_info(categories)")]
        if "checkout" not in cols:
            db.execute("ALTER TABLE categories ADD COLUMN checkout INTEGER NOT NULL DEFAULT 0")
        if db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # One-time: a Tools category for checkout (can be renamed or deleted later).
            db.execute("INSERT OR IGNORE INTO categories (name, color, default_location, checkout) "
                       "VALUES ('Tools', '#d97706', 'Site room', 1)")
            db.execute("UPDATE categories SET checkout = 1 WHERE name = 'Tools'")
            db.execute("PRAGMA user_version = 1")
        # items.barcode: optional, unique when set. Scans match barcode or SKU.
        cols = [r[1] for r in db.execute("PRAGMA table_info(items)")]
        if "barcode" not in cols:
            db.execute("ALTER TABLE items ADD COLUMN barcode TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_barcode "
                   "ON items(barcode COLLATE NOCASE) WHERE barcode != ''")
        db.executescript(ITEM_VIEW)
        # Categories typed on items before category config existed.
        db.execute(
            "INSERT OR IGNORE INTO categories (name) "
            "SELECT DISTINCT category FROM items WHERE category != ''"
        )


def now():
    return datetime.now().isoformat(timespec="seconds")


def clean_item(data, partial=False):
    """Validate incoming item JSON. Returns (dict, error)."""
    out = {}
    for f in ITEM_FIELDS:
        if f not in data:
            continue
        v = data[f]
        try:
            if f in ("quantity", "reorder_level"):
                v = int(v or 0)
                if v < 0:
                    return None, f"{f} cannot be negative"
            elif f == "unit_price":
                v = float(v or 0)
                if v < 0:
                    return None, "unit_price cannot be negative"
            else:
                v = str(v or "").strip()
        except (TypeError, ValueError):
            return None, f"{f} must be a number"
        out[f] = v
    if not partial:
        for req in ("sku", "name"):
            if not out.get(req):
                return None, f"{req} is required"
    elif any(k in out and not out[k] for k in ("sku", "name")):
        return None, "sku and name cannot be empty"
    return out, None


def clean_links(raw):
    """Validate a list of {label, url}. Returns (list, error)."""
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, "links must be a list"
    links = []
    for link in raw:
        url = str((link or {}).get("url", "")).strip()
        label = str((link or {}).get("label", "")).strip()
        if not url:
            continue
        if not re.match(r"^https?://", url, re.I):
            # "mailto:", "javascript:" etc. are rejected; "example.com:8080" is not a scheme.
            if re.match(r"^[a-z][a-z0-9+.-]*:(?!\d)", url, re.I):
                return None, f"Only http/https links are allowed: {url}"
            url = "https://" + url
        if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.I):
            return None, f"Invalid link: {url}"
        links.append({"label": label, "url": url})
    return links, None


def clean_category(data, partial=False):
    out = {}
    for f in CATEGORY_FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f == "default_reorder_level":
            try:
                v = int(v or 0)
            except (TypeError, ValueError):
                return None, "default_reorder_level must be a number"
            if v < 0:
                return None, "default_reorder_level cannot be negative"
        elif f == "color":
            v = str(v or "").strip()
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                return None, "color must look like #1a2b3c"
        elif f == "checkout":
            v = 1 if v in (True, 1, "1", "true", "on") else 0
        else:
            v = str(v or "").strip()
        out[f] = v
    if ("name" in out or not partial) and not out.get("name"):
        return None, "name is required"
    return out, None


def duplicate_error(exc, data):
    """Turn a UNIQUE failure on items into a readable message."""
    if "barcode" in str(exc):
        return f"Barcode '{data.get('barcode')}' is already used by another item"
    return f"SKU '{data.get('sku')}' already exists"


def ensure_category(db, name):
    if name:
        db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))


def save_links(db, item_id, links):
    db.execute("DELETE FROM item_links WHERE item_id = ?", (item_id,))
    db.executemany(
        "INSERT INTO item_links (item_id, label, url) VALUES (?, ?, ?)",
        [(item_id, l["label"], l["url"]) for l in links],
    )


def log_movement(db, item_id, change, reason, qty_after, kind="stock"):
    db.execute(
        "INSERT INTO movements (item_id, change, reason, qty_after, kind, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, change, reason, qty_after, kind, now()),
    )


def image_json(row):
    return {"id": row["id"], "url": f"/uploads/{row['filename']}",
            "original_name": row["original_name"]}


def with_extras(db, rows):
    """Attach links, images and category colour to item rows."""
    items = [dict(r) for r in rows]
    if not items:
        return items
    ids = [i["id"] for i in items]
    marks = ",".join("?" * len(ids))
    links, images = {}, {}
    for r in db.execute(f"SELECT * FROM item_links WHERE item_id IN ({marks}) ORDER BY id", ids):
        links.setdefault(r["item_id"], []).append({"label": r["label"], "url": r["url"]})
    for r in db.execute(f"SELECT * FROM item_images WHERE item_id IN ({marks}) ORDER BY id", ids):
        images.setdefault(r["item_id"], []).append(image_json(r))
    usage = {}
    for r in db.execute(
        f"SELECT u.item_id, u.group_id, u.quantity, g.name AS group_name FROM usage u "
        f"LEFT JOIN item_groups g ON g.id = u.group_id WHERE u.item_id IN ({marks}) "
        f"ORDER BY g.name COLLATE NOCASE", ids,
    ):
        usage.setdefault(r["item_id"], []).append(
            {"group_id": r["group_id"], "group_name": r["group_name"], "quantity": r["quantity"]})
    cats = {r["name"].lower(): r for r in db.execute("SELECT name, color, checkout FROM categories")}
    for i in items:
        i["links"] = links.get(i["id"], [])
        i["images"] = images.get(i["id"], [])
        i["usage"] = usage.get(i["id"], [])
        cat = cats.get((i["category"] or "").lower())
        i["category_color"] = cat["color"] if cat else None
        i["is_tool"] = bool(cat and cat["checkout"])
    return items


def fetch_item(db, item_id):
    row = db.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    return with_extras(db, [row])[0] if row else None


# ---------- pages ----------

@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "mainui.html")


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------- items API ----------

@app.get("/api/items")
def list_items():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    low_only = request.args.get("low") == "1"
    tools_only = request.args.get("tools") == "1"

    sql = "SELECT * FROM item_view WHERE 1=1"
    params = []
    if q:
        sql += (" AND (name LIKE ? OR sku LIKE ? OR barcode LIKE ? "
                "OR location LIKE ? OR notes LIKE ?)")
        params += [f"%{q}%"] * 5
    if category:
        sql += " AND category = ? COLLATE NOCASE"
        params.append(category)
    if low_only:
        sql += " AND available <= reorder_level"
    if tools_only:
        sql += (" AND EXISTS (SELECT 1 FROM categories c "
                "WHERE c.checkout = 1 AND c.name = item_view.category)")
    sql += " ORDER BY name COLLATE NOCASE"

    db = get_db()
    return jsonify(with_extras(db, db.execute(sql, params).fetchall()))


@app.post("/api/items")
def create_item():
    body = request.get_json(force=True) or {}
    data, err = clean_item(body)
    if err:
        return jsonify(error=err), 400
    links, err = clean_links(body.get("links"))
    if err:
        return jsonify(error=err), 400
    db = get_db()
    ts = now()
    data.setdefault("quantity", 0)
    cols = list(data) + ["created_at", "updated_at"]
    vals = list(data.values()) + [ts, ts]
    try:
        cur = db.execute(
            f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    except sqlite3.IntegrityError as exc:
        return jsonify(error=duplicate_error(exc, data)), 409
    ensure_category(db, data.get("category"))
    if links:
        save_links(db, cur.lastrowid, links)
    if data["quantity"]:
        log_movement(db, cur.lastrowid, data["quantity"], "Initial stock", data["quantity"])
    db.commit()
    return jsonify(fetch_item(db, cur.lastrowid)), 201


@app.get("/api/lookup")
def lookup_code():
    """Find the item for a scanned code: barcode first, then SKU."""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify(error="code is required"), 400
    db = get_db()
    row = (db.execute("SELECT id FROM items WHERE barcode = ? COLLATE NOCASE", (code,)).fetchone()
           or db.execute("SELECT id FROM items WHERE sku = ? COLLATE NOCASE", (code,)).fetchone())
    if not row:
        return jsonify(error=f"No item with barcode or SKU '{code}'", code=code), 404
    open_checkouts = [checkout_json(r) for r in db.execute(
        CHECKOUT_SELECT + " WHERE c.item_id = ? AND c.returned_at IS NULL "
        "ORDER BY c.checked_out_at", (row["id"],))]
    return jsonify(item=fetch_item(db, row["id"]), open_checkouts=open_checkouts)


@app.get("/api/items/<int:item_id>")
def get_item(item_id):
    item = fetch_item(get_db(), item_id)
    if not item:
        return jsonify(error="Not found"), 404
    return jsonify(item)


@app.put("/api/items/<int:item_id>")
def update_item(item_id):
    body = request.get_json(force=True) or {}
    data, err = clean_item(body, partial=True)
    if err:
        return jsonify(error=err), 400
    links, err = clean_links(body.get("links"))
    if err:
        return jsonify(error=err), 400
    db = get_db()
    old = db.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    if not old:
        return jsonify(error="Not found"), 404
    busy = old["in_use"] + old["checked_out"]
    if data.get("quantity", old["quantity"]) < busy:
        return jsonify(error=f"{busy} are in use or checked out, so the total can't go "
                             "below that. Return them first."), 400

    if data:
        data["updated_at"] = now()
        try:
            db.execute(
                f"UPDATE items SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                [*data.values(), item_id],
            )
        except sqlite3.IntegrityError as exc:
            return jsonify(error=duplicate_error(exc, data)), 409
        ensure_category(db, data.get("category"))
        if "quantity" in data and data["quantity"] != old["quantity"]:
            log_movement(db, item_id, data["quantity"] - old["quantity"],
                         "Manual edit", data["quantity"])
    if links is not None:
        save_links(db, item_id, links)
    db.commit()
    return jsonify(fetch_item(db, item_id))


@app.delete("/api/items/<int:item_id>")
def delete_item(item_id):
    db = get_db()
    files = [r["filename"] for r in db.execute(
        "SELECT filename FROM item_images WHERE item_id = ?", (item_id,))]
    cur = db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Not found"), 404
    for f in files:
        (UPLOAD_DIR / f).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.post("/api/items/<int:item_id>/adjust")
def adjust_stock(item_id):
    """Body: {"change": +5 | -3, "reason": "Received shipment"}"""
    body = request.get_json(force=True) or {}
    try:
        change = int(body.get("change", 0))
    except (TypeError, ValueError):
        return jsonify(error="change must be an integer"), 400
    if change == 0:
        return jsonify(error="change cannot be zero"), 400
    reason = str(body.get("reason", "")).strip()

    db = get_db()
    item = db.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify(error="Not found"), 404
    new_qty = item["quantity"] + change
    if new_qty < item["in_use"] + item["checked_out"]:
        return jsonify(error=f"Only {item['available']} available to remove "
                             f"(the rest are in use or checked out)"), 400

    db.execute("UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
               (new_qty, now(), item_id))
    log_movement(db, item_id, change, reason, new_qty)
    db.commit()
    return jsonify(fetch_item(db, item_id))


# ---------- in use / groups API ----------

def parse_use_body(db, body):
    """Returns (quantity, group_row_or_None, note, error)."""
    try:
        qty = int(body.get("quantity", 0))
    except (TypeError, ValueError):
        return None, None, None, "quantity must be a whole number"
    if qty <= 0:
        return None, None, None, "quantity must be at least 1"
    group = None
    if body.get("group_id") not in (None, ""):
        group = db.execute("SELECT * FROM item_groups WHERE id = ?",
                           (body["group_id"],)).fetchone()
        if not group:
            return None, None, None, "Group not found"
    return qty, group, str(body.get("note", "")).strip(), None


def usage_reason(group, note):
    label = group["name"] if group else "No group"
    return f"{label} · {note}" if note else label


@app.post("/api/items/<int:item_id>/use")
def mark_in_use(item_id):
    """Body: {"quantity": 2, "group_id": 1 | null, "note": ""}. Total is unchanged."""
    db = get_db()
    item = db.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify(error="Not found"), 404
    qty, group, note, err = parse_use_body(db, request.get_json(force=True) or {})
    if err:
        return jsonify(error=err), 400
    if qty > item["available"]:
        return jsonify(error=f"Only {item['available']} available"), 400

    gid = group["id"] if group else None
    row = db.execute("SELECT id FROM usage WHERE item_id = ? AND group_id IS ?",
                     (item_id, gid)).fetchone()
    if row:
        db.execute("UPDATE usage SET quantity = quantity + ? WHERE id = ?", (qty, row["id"]))
    else:
        db.execute("INSERT INTO usage (item_id, group_id, quantity) VALUES (?, ?, ?)",
                   (item_id, gid, qty))
    log_movement(db, item_id, qty, usage_reason(group, note), item["quantity"], "in_use")
    db.commit()
    return jsonify(fetch_item(db, item_id))


@app.post("/api/items/<int:item_id>/return")
def return_from_use(item_id):
    """Body: {"quantity": 2, "group_id": 1 | null, "note": ""}"""
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify(error="Not found"), 404
    qty, group, note, err = parse_use_body(db, request.get_json(force=True) or {})
    if err:
        return jsonify(error=err), 400

    gid = group["id"] if group else None
    row = db.execute("SELECT * FROM usage WHERE item_id = ? AND group_id IS ?",
                     (item_id, gid)).fetchone()
    if not row or qty > row["quantity"]:
        have = row["quantity"] if row else 0
        return jsonify(error=f"Only {have} in use in {group['name'] if group else 'No group'}"), 400
    if qty == row["quantity"]:
        db.execute("DELETE FROM usage WHERE id = ?", (row["id"],))
    else:
        db.execute("UPDATE usage SET quantity = quantity - ? WHERE id = ?", (qty, row["id"]))
    log_movement(db, item_id, qty, usage_reason(group, note), item["quantity"], "return")
    db.commit()
    return jsonify(fetch_item(db, item_id))


@app.get("/api/groups")
def list_groups():
    """All groups with the items in use in each. Ungrouped usage comes last with id null."""
    db = get_db()
    groups = [dict(r, items=[]) for r in db.execute(
        "SELECT * FROM item_groups ORDER BY name COLLATE NOCASE")]
    by_id = {g["id"]: g for g in groups}
    ungrouped = {"id": None, "name": "No group", "notes": "", "items": []}

    rows = db.execute(
        "SELECT u.group_id, u.quantity, i.id AS item_id, i.name, i.sku, "
        "(SELECT filename FROM item_images im WHERE im.item_id = i.id ORDER BY im.id LIMIT 1) AS image "
        "FROM usage u JOIN items i ON i.id = u.item_id ORDER BY i.name COLLATE NOCASE"
    )
    for r in rows:
        target = by_id.get(r["group_id"], ungrouped)
        target["items"].append({
            "item_id": r["item_id"], "name": r["name"], "sku": r["sku"],
            "quantity": r["quantity"],
            "image": f"/uploads/{r['image']}" if r["image"] else None,
        })
    if ungrouped["items"]:
        groups.append(ungrouped)
    for grp in groups:
        grp["total"] = sum(i["quantity"] for i in grp["items"])
    return jsonify(groups)


def clean_group(data):
    name = str(data.get("name", "")).strip()
    if not name:
        return None, "name is required"
    return {"name": name, "notes": str(data.get("notes", "")).strip()}, None


@app.post("/api/groups")
def create_group():
    data, err = clean_group(request.get_json(force=True) or {})
    if err:
        return jsonify(error=err), 400
    db = get_db()
    try:
        cur = db.execute("INSERT INTO item_groups (name, notes, created_at) VALUES (?, ?, ?)",
                         (data["name"], data["notes"], now()))
    except sqlite3.IntegrityError:
        return jsonify(error=f"Group '{data['name']}' already exists"), 409
    db.commit()
    return jsonify(dict(db.execute("SELECT * FROM item_groups WHERE id = ?",
                                   (cur.lastrowid,)).fetchone())), 201


@app.put("/api/groups/<int:group_id>")
def update_group(group_id):
    data, err = clean_group(request.get_json(force=True) or {})
    if err:
        return jsonify(error=err), 400
    db = get_db()
    try:
        cur = db.execute("UPDATE item_groups SET name = ?, notes = ? WHERE id = ?",
                         (data["name"], data["notes"], group_id))
    except sqlite3.IntegrityError:
        return jsonify(error=f"Group '{data['name']}' already exists"), 409
    if cur.rowcount == 0:
        return jsonify(error="Not found"), 404
    db.commit()
    return jsonify(dict(db.execute("SELECT * FROM item_groups WHERE id = ?",
                                   (group_id,)).fetchone()))


@app.delete("/api/groups/<int:group_id>")
def delete_group(group_id):
    """Everything in use in the group is returned to available."""
    db = get_db()
    group = db.execute("SELECT * FROM item_groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        return jsonify(error="Not found"), 404
    for r in db.execute("SELECT u.item_id, u.quantity, i.quantity AS total FROM usage u "
                        "JOIN items i ON i.id = u.item_id WHERE u.group_id = ?", (group_id,)).fetchall():
        log_movement(db, r["item_id"], r["quantity"],
                     usage_reason(group, "group deleted"), r["total"], "return")
    db.execute("DELETE FROM item_groups WHERE id = ?", (group_id,))
    db.commit()
    return jsonify(ok=True)


# ---------- checkout API (tools and any other item) ----------

CHECKOUT_SELECT = (
    "SELECT c.*, i.name AS item_name, i.sku, i.location, "
    "(SELECT filename FROM item_images im WHERE im.item_id = i.id ORDER BY im.id LIMIT 1) AS image "
    "FROM checkouts c JOIN items i ON i.id = c.item_id"
)


def checkout_json(row):
    d = dict(row)
    d["image"] = f"/uploads/{d['image']}" if d["image"] else None
    return d


@app.get("/api/checkouts")
def list_checkouts():
    """?status=open (default) | returned | all, optional item_id, limit."""
    status = request.args.get("status", "open")
    item_id = request.args.get("item_id", type=int)
    limit = min(request.args.get("limit", 200, type=int), 1000)
    sql, params = CHECKOUT_SELECT + " WHERE 1=1", []
    if status == "open":
        sql += " AND c.returned_at IS NULL"
    elif status == "returned":
        sql += " AND c.returned_at IS NOT NULL"
    if item_id:
        sql += " AND c.item_id = ?"
        params.append(item_id)
    if status == "returned":
        sql += " ORDER BY c.returned_at DESC"
    else:
        sql += " ORDER BY c.returned_at IS NOT NULL, c.checked_out_at DESC"
    sql += " LIMIT ?"
    params.append(limit)
    return jsonify([checkout_json(r) for r in get_db().execute(sql, params)])


@app.post("/api/checkouts")
def create_checkout():
    """Body: {"item_id": 1, "person": "Sam", "quantity": 1, "due_at": "2026-11-20T17:00", "note": ""}"""
    body = request.get_json(force=True) or {}
    person = str(body.get("person", "")).strip()
    if not person:
        return jsonify(error="Who is checking it out?"), 400
    try:
        qty = int(body.get("quantity", 1))
        item_id = int(body.get("item_id"))
    except (TypeError, ValueError):
        return jsonify(error="item_id and quantity must be numbers"), 400
    if qty <= 0:
        return jsonify(error="quantity must be at least 1"), 400
    due_at = str(body.get("due_at") or "").strip() or None
    if due_at:
        try:
            datetime.fromisoformat(due_at)
        except ValueError:
            return jsonify(error="due_at must be a date/time like 2026-11-20T17:00"), 400

    db = get_db()
    item = db.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify(error="Item not found"), 404
    if qty > item["available"]:
        return jsonify(error=f"Only {item['available']} available"), 400
    note = str(body.get("note", "")).strip()
    cur = db.execute(
        "INSERT INTO checkouts (item_id, person, quantity, note, due_at, checked_out_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, person, qty, note, due_at, now()),
    )
    log_movement(db, item_id, qty, f"{person} · {note}" if note else person,
                 item["quantity"], "checkout")
    db.commit()
    return jsonify(checkout_json(db.execute(CHECKOUT_SELECT + " WHERE c.id = ?",
                                            (cur.lastrowid,)).fetchone())), 201


@app.post("/api/checkouts/<int:checkout_id>/return")
def return_checkout(checkout_id):
    """Body (optional): {"note": "Blade is dull"}"""
    body = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT c.*, i.quantity AS total FROM checkouts c "
                     "JOIN items i ON i.id = c.item_id WHERE c.id = ?", (checkout_id,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    if row["returned_at"]:
        return jsonify(error="Already returned"), 400
    note = str(body.get("note", "")).strip()
    db.execute("UPDATE checkouts SET returned_at = ?, return_note = ? WHERE id = ?",
               (now(), note, checkout_id))
    log_movement(db, row["item_id"], row["quantity"],
                 f"{row['person']} · {note}" if note else row["person"], row["total"], "checkin")
    db.commit()
    return jsonify(checkout_json(db.execute(CHECKOUT_SELECT + " WHERE c.id = ?",
                                            (checkout_id,)).fetchone()))


@app.get("/api/people")
def list_people():
    """Names used for past checkouts, for autocomplete."""
    rows = get_db().execute(
        "SELECT person FROM checkouts GROUP BY person COLLATE NOCASE "
        "ORDER BY MAX(checked_out_at) DESC LIMIT 200")
    return jsonify([r["person"] for r in rows])


# ---------- images API ----------

@app.post("/api/items/<int:item_id>/images")
def upload_images(item_id):
    """multipart/form-data with one or more 'images' files."""
    db = get_db()
    if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        return jsonify(error="Not found"), 404
    files = request.files.getlist("images")
    if not files:
        return jsonify(error="No files uploaded"), 400
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_IMAGE_EXT:
            return jsonify(error=f"'{f.filename}' is not a supported image type"), 400

    saved = []
    for f in files:
        name = f"{uuid.uuid4().hex}{Path(f.filename).suffix.lower()}"
        f.save(UPLOAD_DIR / name)
        saved.append(name)
        db.execute(
            "INSERT INTO item_images (item_id, filename, original_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (item_id, name, f.filename, now()),
        )
    db.commit()
    return jsonify(fetch_item(db, item_id)), 201


@app.delete("/api/images/<int:image_id>")
def delete_image(image_id):
    db = get_db()
    row = db.execute("SELECT * FROM item_images WHERE id = ?", (image_id,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    db.execute("DELETE FROM item_images WHERE id = ?", (image_id,))
    db.commit()
    (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="Upload too large (max 25 MB per save)"), 413


# ---------- categories API ----------

@app.get("/api/categories")
def list_categories():
    rows = get_db().execute(
        "SELECT c.*, COUNT(i.id) AS item_count, COALESCE(SUM(i.quantity), 0) AS total_units "
        "FROM categories c LEFT JOIN items i ON i.category = c.name COLLATE NOCASE "
        "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/categories")
def create_category():
    data, err = clean_category(request.get_json(force=True) or {})
    if err:
        return jsonify(error=err), 400
    db = get_db()
    try:
        cur = db.execute(
            f"INSERT INTO categories ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
            list(data.values()),
        )
    except sqlite3.IntegrityError:
        return jsonify(error=f"Category '{data['name']}' already exists"), 409
    db.commit()
    return jsonify(dict(db.execute("SELECT * FROM categories WHERE id = ?",
                                   (cur.lastrowid,)).fetchone())), 201


@app.put("/api/categories/<int:cat_id>")
def update_category(cat_id):
    data, err = clean_category(request.get_json(force=True) or {}, partial=True)
    if err:
        return jsonify(error=err), 400
    db = get_db()
    old = db.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    if not old:
        return jsonify(error="Not found"), 404
    if data:
        try:
            db.execute(
                f"UPDATE categories SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                [*data.values(), cat_id],
            )
        except sqlite3.IntegrityError:
            return jsonify(error=f"Category '{data['name']}' already exists"), 409
        if "name" in data and data["name"] != old["name"]:
            db.execute("UPDATE items SET category = ? WHERE category = ? COLLATE NOCASE",
                       (data["name"], old["name"]))
    db.commit()
    return jsonify(dict(db.execute("SELECT * FROM categories WHERE id = ?",
                                   (cat_id,)).fetchone()))


@app.delete("/api/categories/<int:cat_id>")
def delete_category(cat_id):
    """Items in the category are kept and become uncategorised."""
    db = get_db()
    row = db.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    db.execute("UPDATE items SET category = '' WHERE category = ? COLLATE NOCASE", (row["name"],))
    db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    db.commit()
    return jsonify(ok=True)


# ---------- other API ----------

@app.get("/api/movements")
def list_movements():
    item_id = request.args.get("item_id", type=int)
    limit = min(request.args.get("limit", 100, type=int), 1000)
    sql = ("SELECT m.*, i.name AS item_name, i.sku FROM movements m "
           "JOIN items i ON i.id = m.item_id")
    params = []
    if item_id:
        sql += " WHERE m.item_id = ?"
        params.append(item_id)
    sql += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/stats")
def stats():
    db = get_db()
    s = db.execute(
        "SELECT COUNT(*) AS item_count, "
        "COALESCE(SUM(quantity), 0) AS total_units, "
        "COALESCE(SUM(in_use), 0) AS in_use_units, "
        "COALESCE(SUM(checked_out), 0) AS checked_out_units, "
        "COALESCE(SUM(quantity * unit_price), 0) AS total_value, "
        "COALESCE(SUM(available <= reorder_level), 0) AS low_stock "
        "FROM item_view"
    ).fetchone()
    return jsonify(dict(s))


@app.get("/api/export.csv")
def export_csv():
    db = get_db()
    items = with_extras(db, db.execute(
        "SELECT * FROM item_view ORDER BY name COLLATE NOCASE").fetchall())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", *ITEM_FIELDS, "in_use", "checked_out", "available", "in_use_by_group",
                "links", "image_count", "created_at", "updated_at"])
    for i in items:
        links = " | ".join(f"{l['label']} {l['url']}".strip() for l in i["links"])
        groups = " | ".join(f"{u['group_name'] or 'No group'}: {u['quantity']}" for u in i["usage"])
        w.writerow([i["id"], *(i[f] for f in ITEM_FIELDS), i["in_use"], i["checked_out"],
                    i["available"], groups,
                    links, len(i["images"]), i["created_at"], i["updated_at"]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=inventory-{datetime.now():%Y%m%d}.csv"},
    )


# ---------- startup ----------

def lan_ip():
    """Best guess at this machine's address on the local network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inventory management server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="0.0.0.0 = whole local network, 127.0.0.1 = this computer only")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    init_db()
    print(f"\n  Inventory running:")
    print(f"    This computer:  http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"    Local network:  http://{lan_ip()}:{args.port}\n")

    try:
        from waitress import serve  # production-grade server if installed
        # The page loads several things at once; extra threads avoid queueing.
        serve(app, host=args.host, port=args.port, threads=16)
    except ImportError:
        app.run(host=args.host, port=args.port, debug=args.debug)
