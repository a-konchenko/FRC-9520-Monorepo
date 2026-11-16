# Inventory

A small inventory manager: a Python (Flask) web server with a SQLite database and a browser UI.

## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python app.py                  # whole local network, port 8000
.venv/bin/python app.py --host 127.0.0.1 # this computer only
.venv/bin/python app.py --port 9000      # different port
```

The server prints both addresses. Open `http://<LAN-IP>:8000` on any phone or computer on the same Wi-Fi.
If macOS asks whether to allow incoming connections for Python, click **Allow**.

## Files

- `app.py`: the server and JSON API
- `mainui.html`: the web UI
- `inventory.db`: your data, created on first run
- `uploads/`: item images

To back up, copy both `inventory.db` and `uploads/`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/items?q=&category=&low=1` | List or search items |
| POST | `/api/items` | Create an item (`sku` and `name` are required; optional `links: [{label, url}]`) |
| GET/PUT/DELETE | `/api/items/<id>` | Read, update or delete one item |
| POST | `/api/items/<id>/adjust` | Change the total: `{"change": 5, "reason": "Received"}` |
| POST | `/api/items/<id>/use` | Mark in use (total unchanged): `{"quantity": 2, "group_id": 1}` (`group_id` can be null) |
| POST | `/api/items/<id>/return` | Return from use: `{"quantity": 2, "group_id": 1}` |
| GET/POST | `/api/groups` | List groups with their in-use items, or create one (`{"name", "notes"}`) |
| PUT/DELETE | `/api/groups/<id>` | Rename or delete a group (deleting returns its items to available) |
| POST | `/api/items/<id>/images` | Upload images (multipart field `images`, can repeat) |
| DELETE | `/api/images/<id>` | Delete one image |
| GET | `/api/items?tools=1` | Only items in categories with checkout allowed |
| GET | `/api/checkouts?status=open\|returned\|all&item_id=` | Tool checkouts |
| POST | `/api/checkouts` | Check out: `{"item_id": 3, "person": "Sam", "quantity": 1, "due_at": "2026-11-20T17:00", "note": ""}` |
| POST | `/api/checkouts/<id>/return` | Check a tool back in, optional `{"note": "..."}` |
| GET | `/api/people` | Names used in past checkouts |
| GET/POST | `/api/categories` | List or create categories (`checkout: true` puts its items on the checkout page) |
| PUT/DELETE | `/api/categories/<id>` | Edit or delete a category (its items become uncategorised) |
| GET | `/api/movements?item_id=` | Stock history |
| GET | `/api/stats` | Totals |
| GET | `/api/export.csv` | Download all items as CSV |

There is no login. Anyone on your network can view and edit the data, so don't expose this to the internet.
