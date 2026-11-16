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
"""

ITEM_FIELDS = ("sku", "name", "category", "location", "quantity",
               "reorder_level", "unit_price", "notes")
CATEGORY_FIELDS = ("name", "color", "description",
                   "default_reorder_level", "default_location")


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
        else:
            v = str(v or "").strip()
        out[f] = v
    if ("name" in out or not partial) and not out.get("name"):
        return None, "name is required"
    return out, None


def ensure_category(db, name):
    if name:
        db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))


def save_links(db, item_id, links):
    db.execute("DELETE FROM item_links WHERE item_id = ?", (item_id,))
    db.executemany(
        "INSERT INTO item_links (item_id, label, url) VALUES (?, ?, ?)",
        [(item_id, l["label"], l["url"]) for l in links],
    )


def log_movement(db, item_id, change, reason, qty_after):
    db.execute(
        "INSERT INTO movements (item_id, change, reason, qty_after, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, change, reason, qty_after, now()),
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
    colors = {r["name"].lower(): r["color"] for r in db.execute("SELECT name, color FROM categories")}
    for i in items:
        i["links"] = links.get(i["id"], [])
        i["images"] = images.get(i["id"], [])
        i["category_color"] = colors.get((i["category"] or "").lower())
    return items


def fetch_item(db, item_id):
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
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

    sql = "SELECT * FROM items WHERE 1=1"
    params = []
    if q:
        sql += " AND (name LIKE ? OR sku LIKE ? OR location LIKE ? OR notes LIKE ?)"
        params += [f"%{q}%"] * 4
    if category:
        sql += " AND category = ? COLLATE NOCASE"
        params.append(category)
    if low_only:
        sql += " AND quantity <= reorder_level"
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
    except sqlite3.IntegrityError:
        return jsonify(error=f"SKU '{data['sku']}' already exists"), 409
    ensure_category(db, data.get("category"))
    if links:
        save_links(db, cur.lastrowid, links)
    if data["quantity"]:
        log_movement(db, cur.lastrowid, data["quantity"], "Initial stock", data["quantity"])
    db.commit()
    return jsonify(fetch_item(db, cur.lastrowid)), 201


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
    old = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not old:
        return jsonify(error="Not found"), 404

    if data:
        data["updated_at"] = now()
        try:
            db.execute(
                f"UPDATE items SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                [*data.values(), item_id],
            )
        except sqlite3.IntegrityError:
            return jsonify(error=f"SKU '{data.get('sku')}' already exists"), 409
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
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify(error="Not found"), 404
    new_qty = item["quantity"] + change
    if new_qty < 0:
        return jsonify(error=f"Only {item['quantity']} in stock"), 400

    db.execute("UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
               (new_qty, now(), item_id))
    log_movement(db, item_id, change, reason, new_qty)
    db.commit()
    return jsonify(fetch_item(db, item_id))


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
        "COALESCE(SUM(quantity * unit_price), 0) AS total_value, "
        "COALESCE(SUM(quantity <= reorder_level), 0) AS low_stock "
        "FROM items"
    ).fetchone()
    return jsonify(dict(s))


@app.get("/api/export.csv")
def export_csv():
    db = get_db()
    items = with_extras(db, db.execute(
        "SELECT * FROM items ORDER BY name COLLATE NOCASE").fetchall())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", *ITEM_FIELDS, "links", "image_count", "created_at", "updated_at"])
    for i in items:
        links = " | ".join(f"{l['label']} {l['url']}".strip() for l in i["links"])
        w.writerow([i["id"], *(i[f] for f in ITEM_FIELDS), links, len(i["images"]),
                    i["created_at"], i["updated_at"]])
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
        serve(app, host=args.host, port=args.port)
    except ImportError:
        app.run(host=args.host, port=args.port, debug=args.debug)
