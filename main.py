#!/usr/bin/env python3
"""
Web server for Matthew's Stop and Look Auto Sales (313carloans.com).

Serves the static site (clean directory URLs) plus a mobile-friendly staff
admin at /admin:
  - password login (session cookie signed with SESSION_SECRET)
  - VIN decode via the free NHTSA vPIC API
  - add vehicles with photo uploads (resized server-side)
  - edit price / mileage / mark sold / feature / delete
Every change rewrites data/inventory.js and regenerates the per-vehicle
pages + sitemap via scraper/generate_vehicle_pages.py.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date as date_type
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from io import BytesIO
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory, session
from PIL import Image, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()   # adds HEIC/HEIF support for phone uploads

ROOT = Path(__file__).resolve().parent
DATA_FILE  = ROOT / "data" / "inventory.js"
PHOTOS_DIR = ROOT / "assets" / "photos"

DATABASE_URL = os.environ.get("DATABASE_URL")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GHL_API_KEY = os.environ.get("GHL")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "XaYEM5WGXt0VYVclE4AZ")

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SESSION_SECRET") or os.urandom(32)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # photo batches from phones
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"   # blocks cross-site POSTs (CSRF)
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("REPLIT_DEPLOYMENT") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

import csv
import ftplib
import io
import threading
INVENTORY_LOCK = threading.Lock()  # serialize all read-modify-write cycles

# ---------------------------------------------------------- ADD FTP config
ADD_FTP_HOST  = "ftp.autodealersdigital.com"
ADD_FTP_USER  = "113476_1"
ADD_FTP_PASS  = os.environ.get("ADD_FTP_PASSWORD", "")

# ---------------------------------------------------------- CarGurus SFTP config
CG_FTP_HOST   = os.environ.get("CG_FTP_HOST", "ftp.cargurus.com")
CG_FTP_PORT   = int(os.environ.get("CG_FTP_PORT", "2122"))
CG_FTP_USER   = os.environ.get("CG_FTP_USER", "vaunt")
CG_FTP_PASS   = os.environ.get("CG_FTP_PASSWORD", "")

BASE_URL      = "https://313carloans.com"

DEALERSHIP_INFO = {
    "name":      "Matthew's Stop and Look Auto Sales",
    "legalName": "McQueen Auto, Inc.",
    "phone":     "313-891-8000",
    "phoneHref": "tel:+13138918000",
    "address":   "8146 E 8 Mile Rd, Detroit, MI 48234",
    "mapsUrl":   "https://maps.google.com/?q=8146+E+8+Mile+Rd,+Detroit,+MI+48234",
}

# ---------------------------------------------------------- PostgreSQL helpers

def _pg():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    """Create all tables on first boot (idempotent — safe to run every startup)."""
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute("""
                    -- Persistent photo store — survives container restarts/deploys.
                    CREATE TABLE IF NOT EXISTS vehicle_photos (
                        vehicle_id TEXT    NOT NULL,
                        filename   TEXT    NOT NULL,
                        data       BYTEA   NOT NULL,
                        uploaded_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (vehicle_id, filename)
                    );
                    CREATE INDEX IF NOT EXISTS vehicle_photos_vid_idx
                        ON vehicle_photos(vehicle_id);
                    CREATE TABLE IF NOT EXISTS submissions (
                        id             SERIAL PRIMARY KEY,
                        submitted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        form_type      TEXT NOT NULL,
                        first_name     TEXT,
                        last_name      TEXT,
                        phone          TEXT,
                        email          TEXT,
                        ghl_contact_id TEXT,
                        payload        JSONB
                    );
                    CREATE INDEX IF NOT EXISTS submissions_form_type_idx
                        ON submissions(form_type);
                    CREATE INDEX IF NOT EXISTS submissions_submitted_at_idx
                        ON submissions(submitted_at DESC);

                    CREATE TABLE IF NOT EXISTS page_views (
                        id         SERIAL PRIMARY KEY,
                        visited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        path       TEXT
                    );
                    CREATE INDEX IF NOT EXISTS page_views_visited_at_idx
                        ON page_views(visited_at DESC);

                    -- Legacy blob table kept for fallback; source of truth is now vehicles.
                    CREATE TABLE IF NOT EXISTS inventory_store (
                        id         INT PRIMARY KEY DEFAULT 1,
                        data       JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    -- One row per vehicle — the real source of truth.
                    CREATE TABLE IF NOT EXISTS vehicles (
                        id                 TEXT PRIMARY KEY,
                        stock              TEXT,
                        vin                TEXT        DEFAULT '',
                        year               INT,
                        make               TEXT,
                        model              TEXT,
                        trim               TEXT        DEFAULT '',
                        body_style         TEXT        DEFAULT '',
                        price              INT         DEFAULT 0,
                        mileage            INT         DEFAULT 0,
                        exterior_color     TEXT        DEFAULT '',
                        interior_color     TEXT        DEFAULT '',
                        engine             TEXT        DEFAULT '',
                        transmission       TEXT        DEFAULT 'Automatic',
                        drivetrain         TEXT        DEFAULT 'FWD',
                        fuel               TEXT        DEFAULT 'Gasoline',
                        description        TEXT        DEFAULT '',
                        images             JSONB       DEFAULT '[]',
                        features           JSONB       DEFAULT '[]',
                        featured           BOOLEAN     DEFAULT false,
                        sold               BOOLEAN     DEFAULT false,
                        photos_coming_soon BOOLEAN     DEFAULT true,
                        source_url         TEXT        DEFAULT '',
                        mpg_city           INT         DEFAULT 0,
                        mpg_hwy            INT         DEFAULT 0,
                        created_at         TIMESTAMPTZ DEFAULT NOW(),
                        updated_at         TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS vehicles_stock_idx   ON vehicles(stock);
                    CREATE INDEX IF NOT EXISTS vehicles_sold_idx    ON vehicles(sold);
                    CREATE INDEX IF NOT EXISTS vehicles_updated_idx ON vehicles(updated_at DESC);
                """)
    except Exception as exc:
        app.logger.error("Failed to init DB: %s", exc)

init_db()


def _migrate_to_vehicles_table():
    """One-time migration: move inventory_store blob → individual vehicles rows."""
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM vehicles")
                if cur.fetchone()["cnt"] > 0:
                    return  # already migrated

                # Try inventory_store blob first, then fall back to inventory.js
                source = []
                cur.execute("SELECT data FROM inventory_store WHERE id = 1")
                row = cur.fetchone()
                if row:
                    blob = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
                    source = blob.get("vehicles", [])
                if not source and DATA_FILE.exists():
                    text = DATA_FILE.read_text()
                    blob = json.loads(text.split("window.INVENTORY = ", 1)[1].rstrip().rstrip(";"))
                    source = blob.get("vehicles", [])

                for v in source:
                    _upsert_vehicle_row(cur, v)
                app.logger.info("Migrated %d vehicles → vehicles table", len(source))
    except Exception as exc:
        app.logger.error("vehicles table migration failed: %s", exc)


def _upsert_vehicle_row(cur, v):
    """INSERT or UPDATE a single vehicle dict into the vehicles table."""
    cur.execute(
        """INSERT INTO vehicles (
               id, stock, vin, year, make, model, trim, body_style,
               price, mileage, exterior_color, interior_color,
               engine, transmission, drivetrain, fuel,
               description, images, features, featured, sold,
               photos_coming_soon, source_url, mpg_city, mpg_hwy
           ) VALUES (
               %s, %s, %s, %s, %s, %s, %s, %s,
               %s, %s, %s, %s,
               %s, %s, %s, %s,
               %s, %s::jsonb, %s::jsonb, %s, %s,
               %s, %s, %s, %s
           )
           ON CONFLICT (id) DO UPDATE SET
               stock              = EXCLUDED.stock,
               vin                = EXCLUDED.vin,
               year               = EXCLUDED.year,
               make               = EXCLUDED.make,
               model              = EXCLUDED.model,
               trim               = EXCLUDED.trim,
               body_style         = EXCLUDED.body_style,
               price              = EXCLUDED.price,
               mileage            = EXCLUDED.mileage,
               exterior_color     = EXCLUDED.exterior_color,
               interior_color     = EXCLUDED.interior_color,
               engine             = EXCLUDED.engine,
               transmission       = EXCLUDED.transmission,
               drivetrain         = EXCLUDED.drivetrain,
               fuel               = EXCLUDED.fuel,
               description        = EXCLUDED.description,
               images             = EXCLUDED.images,
               features           = EXCLUDED.features,
               featured           = EXCLUDED.featured,
               sold               = EXCLUDED.sold,
               photos_coming_soon = EXCLUDED.photos_coming_soon,
               source_url         = EXCLUDED.source_url,
               mpg_city           = EXCLUDED.mpg_city,
               mpg_hwy            = EXCLUDED.mpg_hwy,
               updated_at         = NOW()""",
        (
            v.get("id"), v.get("stock", ""), v.get("vin", ""),
            v.get("year", 0), v.get("make", ""), v.get("model", ""),
            v.get("trim", ""), v.get("bodyStyle", ""),
            v.get("price", 0), v.get("mileage", 0),
            v.get("exteriorColor", ""), v.get("interiorColor", ""),
            v.get("engine", ""), v.get("transmission", "Automatic"),
            v.get("drivetrain", "FWD"), v.get("fuel", "Gasoline"),
            v.get("description", ""),
            json.dumps(v.get("images", [])),
            json.dumps(v.get("features", [])),
            bool(v.get("featured", False)), bool(v.get("sold", False)),
            bool(v.get("photosComingSoon", False)),
            v.get("sourceUrl", ""),
            v.get("mpgCity", 0), v.get("mpgHwy", 0),
        ),
    )


def _row_to_vehicle(r):
    """Convert a vehicles table row (RealDictRow) to the dict the app uses."""
    imgs  = r["images"]
    feats = r["features"]
    return {
        "id":              r["id"],
        "stock":           r["stock"] or "",
        "vin":             r["vin"] or "",
        "year":            r["year"],
        "make":            r["make"],
        "model":           r["model"],
        "trim":            r["trim"] or "",
        "bodyStyle":       r["body_style"] or "",
        "price":           r["price"] or 0,
        "mileage":         r["mileage"] or 0,
        "exteriorColor":   r["exterior_color"] or "",
        "interiorColor":   r["interior_color"] or "",
        "engine":          r["engine"] or "",
        "transmission":    r["transmission"] or "Automatic",
        "drivetrain":      r["drivetrain"] or "FWD",
        "fuel":            r["fuel"] or "Gasoline",
        "description":     r["description"] or "",
        "images":          imgs  if isinstance(imgs,  list) else [],
        "features":        feats if isinstance(feats, list) else [],
        "featured":        bool(r["featured"]),
        "sold":            bool(r["sold"]),
        "photosComingSoon": bool(r["photos_coming_soon"]),
        "sourceUrl":       r["source_url"] or "",
        "mpgCity":         r["mpg_city"] or 0,
        "mpgHwy":          r["mpg_hwy"] or 0,
    }


_migrate_to_vehicles_table()


# Fields that must never be written to our database — they go to GHL only.
_SENSITIVE_PAYLOAD_KEYS = frozenset({
    "SSN", "ssn", "social_security", "Social Security Number",
    "Co-Signer SSN", "co_signer_ssn",
})

def _sanitize_payload(payload):
    """Return a copy of the form payload with all sensitive fields removed."""
    return {k: v for k, v in dict(payload).items() if k not in _SENSITIVE_PAYLOAD_KEYS}


def log_submission(form_type, first_name, last_name, phone, email, ghl_contact_id, payload):
    """Persist every form submission to PostgreSQL as a backup.
    SSN and other sensitive fields are stripped before storage — they are
    sent to GHL only and must never touch our database.
    """
    safe_payload = _sanitize_payload(payload)
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute(
                    """INSERT INTO submissions
                       (form_type, first_name, last_name, phone, email, ghl_contact_id, payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (form_type, first_name, last_name, phone, email,
                     ghl_contact_id, json.dumps(safe_payload)),
                )
    except Exception as exc:
        app.logger.error("Failed to log submission to DB: %s", exc)


def _scrub_ssn_from_submissions():
    """One-time cleanup: remove any SSN data previously stored in submissions.
    Safe to run every startup — becomes a no-op once all rows are clean.
    """
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute(
                    """UPDATE submissions
                       SET payload = payload
                           - 'SSN'
                           - 'ssn'
                           - 'Co-Signer SSN'
                           - 'Social Security Number'
                           - 'co_signer_ssn'
                           - 'social_security'
                       WHERE payload::text ILIKE '%ssn%'
                          OR payload ? 'Social Security Number'"""
                )
                if cur.rowcount:
                    app.logger.info("Scrubbed SSN from %d submission rows", cur.rowcount)
    except Exception as exc:
        app.logger.error("SSN scrub failed: %s", exc)

_scrub_ssn_from_submissions()

def log_page_view(path):
    """Log a single website page view to PostgreSQL."""
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute("INSERT INTO page_views (path) VALUES (%s)", (path,))
    except Exception as exc:
        app.logger.error("Failed to log page view: %s", exc)

# ------------------------------------------------------------------ inventory

def load_inventory():
    """Load inventory from the vehicles table (source of truth).

    Falls back to inventory_store blob, then to inventory.js file.
    Always returns the standard dict: {dealership, updated, vehicles}.
    """
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT id, stock, vin, year, make, model, trim, body_style,
                              price, mileage, exterior_color, interior_color,
                              engine, transmission, drivetrain, fuel,
                              description, images, features, featured, sold,
                              photos_coming_soon, source_url, mpg_city, mpg_hwy
                       FROM vehicles
                       ORDER BY
                           CASE WHEN stock ~ '^[0-9]+$' THEN stock::int ELSE 0 END DESC,
                           created_at DESC"""
                )
                rows = cur.fetchall()
                # A successful empty query is authoritative. Falling back to an
                # old file here can resurrect deliberately deleted vehicles.
                return {
                    "dealership": DEALERSHIP_INFO,
                    "updated":    date_type.today().isoformat(),
                    "vehicles":   [_row_to_vehicle(r) for r in rows],
                }
    except Exception as exc:
        app.logger.error("Failed to load from vehicles table: %s", exc)

    # Fallback 1 — legacy blob
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute("SELECT data FROM inventory_store WHERE id = 1")
                row = cur.fetchone()
                if row:
                    return row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    except Exception:
        pass

    # Fallback 2 — flat file
    text = DATA_FILE.read_text()
    return json.loads(text.split("window.INVENTORY = ", 1)[1].rstrip().rstrip(";"))


def save_inventory(data):
    """Upsert every vehicle into the vehicles table.  Raises on any DB failure
    so the calling admin route returns a 500 — inventory.js is never written
    unless the DB write succeeded first.
    """
    vehicles = data.get("vehicles", [])

    with _pg() as con:
        with con.cursor() as cur:
            # Remove rows for vehicles that were deleted from the inventory
            if vehicles:
                new_ids = [v["id"] for v in vehicles]
                cur.execute(
                    "DELETE FROM vehicles WHERE id <> ALL(%s)", (new_ids,)
                )
            # Upsert every current vehicle
            for v in vehicles:
                _upsert_vehicle_row(cur, v)

    # Rebuild inventory.js directly from the DB so the file exactly mirrors it
    fresh = load_inventory()
    text = (
        "/** Inventory for Matthew's Stop and Look Auto Sales. */\n"
        "window.INVENTORY = " + json.dumps(fresh, indent=2) + ";\n"
    )
    tmp = DATA_FILE.with_suffix(".js.tmp")
    tmp.write_text(text)
    tmp.replace(DATA_FILE)

    # Push updated CSVs to AutoDealersDigital and CarGurus (non-blocking background pushes)
    push_add_ftp(fresh)
    push_cargurus_sftp(fresh)


def save_vehicle(vehicle):
    """Persist one vehicle without replacing or deleting any other inventory row."""
    with _pg() as con:
        with con.cursor() as cur:
            _upsert_vehicle_row(cur, vehicle)
    try:
        refresh_inventory_cache()
    except Exception as exc:
        # PostgreSQL is authoritative; a disposable instance cache must never
        # turn a committed save into a false failure response.
        app.logger.warning("Inventory cache refresh failed after vehicle save: %s", exc)


def delete_vehicle_row(vehicle_id):
    """Delete exactly one vehicle and its photos in one database transaction."""
    with _pg() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM vehicle_photos WHERE vehicle_id=%s", (vehicle_id,))
            cur.execute("DELETE FROM vehicles WHERE id=%s", (vehicle_id,))
            if cur.rowcount != 1:
                raise LookupError(vehicle_id)
    try:
        refresh_inventory_cache()
    except Exception as exc:
        app.logger.warning("Inventory cache refresh failed after vehicle delete: %s", exc)


def refresh_inventory_cache():
    """Refresh this instance's legacy static cache from the database."""
    fresh = load_inventory()
    text = (
        "/** Inventory for Matthew's Stop and Look Auto Sales. */\n"
        "window.INVENTORY = " + json.dumps(fresh, indent=2) + ";\n"
    )
    tmp = DATA_FILE.with_suffix(".js.tmp")
    tmp.write_text(text)
    tmp.replace(DATA_FILE)
    push_add_ftp(fresh)
    push_cargurus_sftp(fresh)


@contextmanager
def inventory_mutation_lock():
    """Serialize a full admin read-modify-write cycle across all app instances."""
    con = _pg()
    cur = con.cursor()
    try:
        cur.execute("SELECT pg_advisory_lock(hashtext('inventory_mutation'))")
        yield
    finally:
        try:
            cur.execute("SELECT pg_advisory_unlock(hashtext('inventory_mutation'))")
        finally:
            cur.close()
            con.close()


def _recover_confirmed_stranded_vehicles():
    """One-time recovery for records confirmed missing while photos survived."""
    recovery_ids = {
        "2018-gmc-terrain-10879",
        "2019-buick-encore-10806",
        "2019-cadillac-xts-10884",
    }
    try:
        text = DATA_FILE.read_text()
        snapshot = json.loads(
            text.split("window.INVENTORY = ", 1)[1].rstrip().rstrip(";")
        )
        expected = {
            v["id"]: v for v in snapshot.get("vehicles", [])
            if v.get("id") in recovery_ids
        }
        if not expected:
            return
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT p.vehicle_id
                       FROM vehicle_photos p
                       LEFT JOIN vehicles v ON v.id=p.vehicle_id
                       WHERE v.id IS NULL AND p.vehicle_id=ANY(%s)""",
                    (list(expected),),
                )
                missing_ids = [row["vehicle_id"] for row in cur.fetchall()]
                for vehicle_id in missing_ids:
                    _upsert_vehicle_row(cur, expected[vehicle_id])
        if missing_ids:
            app.logger.warning(
                "Recovered %d stranded vehicle record(s): %s",
                len(missing_ids), ", ".join(missing_ids),
            )
    except Exception as exc:
        app.logger.error("Stranded vehicle recovery failed: %s", exc)


def build_add_csv(data):
    """Return a pipe-delimited CSV string formatted for AutoDealersDigital."""
    vehicles = [v for v in data.get("vehicles", []) if not v.get("sold")]
    dealer   = data.get("dealership", {})
    dealer_name = dealer.get("name", "Matthew's Stop and Look Auto Sales")

    headers = [
        "DealerName", "VIN", "StockNumber", "Year", "Make", "Model", "Trim",
        "BodyStyle", "Mileage", "Price", "ExteriorColor", "InteriorColor",
        "Engine", "Transmission", "Drivetrain", "Description", "Images",
        "VehicleURL", "Condition",
    ]

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(headers)

    for v in vehicles:
        imgs = v.get("images", [])
        img_urls = ",".join(BASE_URL + i for i in imgs[:27])  # ADD supports up to 27
        price = v.get("price", "")
        writer.writerow([
            dealer_name,
            v.get("vin", ""),
            v.get("stock", ""),
            v.get("year", ""),
            v.get("make", ""),
            v.get("model", ""),
            v.get("trim", ""),
            v.get("bodyStyle", ""),
            v.get("mileage", ""),
            price if price else "",
            v.get("exteriorColor", ""),
            v.get("interiorColor", ""),
            v.get("engine", ""),
            v.get("transmission", "Automatic"),
            v.get("drivetrain", ""),
            v.get("description", ""),
            img_urls,
            f"{BASE_URL}/vehicle/{v.get('id', '')}/",
            "Used",
        ])

    return buf.getvalue()


def push_add_ftp(data=None):
    """Upload the ADD CSV to AutoDealersDigital's FTP in a background thread.
    Pass inventory data dict or None to load fresh from DB."""
    if not ADD_FTP_PASS:
        app.logger.warning("ADD_FTP_PASSWORD not set — skipping FTP push")
        return

    def _upload():
        try:
            inv  = data if data is not None else load_inventory()
            csv_bytes = build_add_csv(inv).encode("utf-8")
            with ftplib.FTP(ADD_FTP_HOST, ADD_FTP_USER, ADD_FTP_PASS, timeout=30) as ftp:
                ftp.storbinary("STOR inventory.csv", io.BytesIO(csv_bytes))
            app.logger.info("ADD FTP push OK — %d bytes", len(csv_bytes))
        except Exception as exc:
            app.logger.error("ADD FTP push failed: %s", exc)

    threading.Thread(target=_upload, daemon=True).start()


def push_cargurus_sftp(data=None):
    """Upload the CarGurus CSV to CarGurus' SFTP in a background thread.
    Pass inventory data dict or None to load fresh from DB."""
    if not CG_FTP_PASS:
        app.logger.warning("CG_FTP_PASSWORD not set — skipping CarGurus SFTP push")
        return

    def _upload():
        try:
            import paramiko, io as _io
            inv = data if data is not None else load_inventory()
            csv_text = _build_cargurus_csv(inv)
            csv_bytes = csv_text.encode("utf-8")
            transport = paramiko.Transport((CG_FTP_HOST, CG_FTP_PORT))
            transport.connect(username=CG_FTP_USER, password=CG_FTP_PASS)
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.putfo(_io.BytesIO(csv_bytes), "matthews_stop_look_inventory.csv")
            sftp.close()
            transport.close()
            app.logger.info("CarGurus SFTP push OK — %d bytes, %d vehicles",
                            len(csv_bytes), len(inv.get("vehicles", [])))
        except Exception as exc:
            app.logger.error("CarGurus SFTP push failed: %s", exc)

    threading.Thread(target=_upload, daemon=True).start()


def regenerate_pages():
    """Rebuild /vehicle/<id>/ pages + sitemap. Returns True on success."""
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scraper" / "generate_vehicle_pages.py")],
            check=True, capture_output=True, timeout=60,
        )
        return True
    except Exception as e:  # noqa: BLE001
        app.logger.error("page regeneration failed: %s", e)
        return False


def require_admin():
    if not session.get("admin"):
        abort(401)


# ----------------------------------------------------------------------- auth

@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    if body.get("password") == ADMIN_PASSWORD:
        session["admin"] = True
        session.permanent = True
        return jsonify(ok=True)
    return jsonify(ok=False, error="Wrong password"), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/session")
def session_state():
    return jsonify(authed=bool(session.get("admin")))


# ------------------------------------------------------------------ VIN decode

VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"

BODY_MAP = {
    "sedan": "Sedan", "saloon": "Sedan", "suv": "SUV", "muv": "SUV",
    "crossover": "SUV", "truck": "Truck", "pickup": "Truck", "van": "Van",
    "minivan": "Van", "coupe": "Coupe", "hatchback": "Hatchback",
    "wagon": "Wagon", "convertible": "Convertible", "cabriolet": "Convertible",
}


@app.get("/api/vin/<vin>")
def decode_vin(vin):
    require_admin()
    vin = vin.strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return jsonify(ok=False, error="A VIN is 17 letters/numbers (no I, O, or Q)."), 400
    try:
        with urllib.request.urlopen(VPIC_URL.format(vin=vin), timeout=15) as r:
            row = json.load(r)["Results"][0]
    except Exception:  # noqa: BLE001
        return jsonify(ok=False, error="VIN lookup service unreachable - fill fields manually."), 502

    def g(key):
        val = (row.get(key) or "").strip()
        return "" if val.lower() in ("", "not applicable", "n/a") else val

    body_raw = g("BodyClass").lower()
    body = next((v for k, v in BODY_MAP.items() if k in body_raw), "")
    drive_raw = g("DriveType").upper()
    drivetrain = ("4WD" if "4WD" in drive_raw or "4X4" in drive_raw else
                  "AWD" if "AWD" in drive_raw or "ALL" in drive_raw else
                  "RWD" if "REAR" in drive_raw or "RWD" in drive_raw else
                  "FWD" if "FRONT" in drive_raw or "FWD" in drive_raw else "")
    disp = g("DisplacementL")
    cyl = g("EngineCylinders")
    engine = " ".join(x for x in [
        (disp + "L") if disp else "",
        (("V" if cyl in ("6", "8", "10", "12") else "I") + cyl) if cyl else "",
        "Turbo" if "turbo" in g("Turbo").lower() or "yes" in g("Turbo").lower() else "",
    ] if x).strip()
    trans_raw = g("TransmissionStyle").lower()
    transmission = ("CVT" if "cvt" in trans_raw or "continuously" in trans_raw else
                    "Manual" if "manual" in trans_raw else
                    "Automatic" if trans_raw else "")
    fuel = g("FuelTypePrimary").title() or "Gasoline"

    if not g("Make") and not g("ModelYear"):
        return jsonify(ok=False, error="That VIN didn't decode - double-check it."), 404

    return jsonify(ok=True, vehicle={
        "vin": vin,
        "year": g("ModelYear"),
        "make": g("Make").title(),
        "model": g("Model"),
        "trim": g("Trim") or g("Series"),
        "bodyStyle": body,
        "drivetrain": drivetrain,
        "engine": engine,
        "transmission": transmission,
        "fuel": fuel,
    })


# ------------------------------------------------------------------- vehicles

@app.get("/api/inventory")
def api_inventory():
    require_admin()
    return jsonify(load_inventory())


def photo_storage_health():
    """Report whether every image referenced by inventory has a durable copy."""
    with _pg() as con:
        with con.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS referenced,
                          COUNT(p.filename) AS backed_up
                   FROM vehicles v
                   CROSS JOIN LATERAL jsonb_array_elements_text(
                       COALESCE(v.images, '[]'::jsonb)
                   ) AS image(path)
                   LEFT JOIN vehicle_photos p
                     ON p.vehicle_id = v.id
                    AND p.filename = regexp_replace(image.path, '^.*/', '')
                   WHERE image.path LIKE '/assets/photos/%'"""
            )
            counts = dict(cur.fetchone())
            cur.execute(
                """SELECT v.id, v.stock, regexp_replace(image.path, '^.*/', '') AS filename
                   FROM vehicles v
                   CROSS JOIN LATERAL jsonb_array_elements_text(
                       COALESCE(v.images, '[]'::jsonb)
                   ) AS image(path)
                   LEFT JOIN vehicle_photos p
                     ON p.vehicle_id = v.id
                    AND p.filename = regexp_replace(image.path, '^.*/', '')
                   WHERE image.path LIKE '/assets/photos/%'
                     AND p.filename IS NULL
                   ORDER BY v.updated_at DESC
                   LIMIT 25"""
            )
            missing = [dict(row) for row in cur.fetchall()]

    referenced = int(counts["referenced"] or 0)
    backed_up = int(counts["backed_up"] or 0)
    return {
        "healthy": referenced == backed_up,
        "referenced": referenced,
        "backed_up": backed_up,
        "missing": referenced - backed_up,
        "examples": missing,
    }


@app.get("/api/admin/photo-health")
def admin_photo_health():
    """Expose photo durability status to the authenticated admin dashboard."""
    require_admin()
    try:
        return jsonify(ok=True, **photo_storage_health())
    except Exception as exc:
        app.logger.error("Photo storage health check failed: %s", exc)
        return jsonify(ok=False, error="Photo storage check could not reach the database."), 503


def _store_photo_batch(vehicle_id, photos):
    """Persist a complete upload batch before it is reported as saved."""
    with _pg() as con:
        with con.cursor() as cur:
            for filename, jpeg_bytes in photos:
                cur.execute(
                    """INSERT INTO vehicle_photos (vehicle_id, filename, data)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (vehicle_id, filename)
                       DO UPDATE SET data = EXCLUDED.data, uploaded_at = NOW()""",
                    (vehicle_id, filename, psycopg2.Binary(jpeg_bytes)),
                )


def _verify_photo_batch(vehicle_id, photos):
    """Confirm the exact uploaded bytes committed before declaring success."""
    expected = {filename: len(jpeg_bytes) for filename, jpeg_bytes in photos}
    with _pg() as con:
        with con.cursor() as cur:
            cur.execute(
                """SELECT filename, octet_length(data) AS byte_count
                   FROM vehicle_photos
                   WHERE vehicle_id=%s AND filename=ANY(%s)""",
                (vehicle_id, list(expected)),
            )
            actual = {row["filename"]: int(row["byte_count"]) for row in cur.fetchall()}
    if actual != expected:
        raise RuntimeError("Photo backup verification did not pass.")


def _backfill_photo_backups():
    """Copy any still-present disk photos into the persistent store once."""
    if not PHOTOS_DIR.is_dir():
        return
    saved = 0
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext('vehicle_photo_backfill')) AS acquired")
                if not cur.fetchone()["acquired"]:
                    return
                try:
                    for folder in PHOTOS_DIR.iterdir():
                        if not folder.is_dir() or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", folder.name):
                            continue
                        for photo in folder.glob("*.jpg"):
                            cur.execute(
                                """INSERT INTO vehicle_photos (vehicle_id, filename, data)
                                   VALUES (%s, %s, %s)
                                   ON CONFLICT (vehicle_id, filename) DO NOTHING""",
                                (folder.name, photo.name, psycopg2.Binary(photo.read_bytes())),
                            )
                            saved += cur.rowcount
                finally:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('vehicle_photo_backfill'))")
        if saved:
            app.logger.info("Backfilled %d vehicle photos into persistent storage", saved)
    except Exception as exc:
        app.logger.error("Photo backup backfill failed: %s", exc)


def save_photos(vehicle_id, files, start_index=0):
    """Resize and persist photos; the local copy is only a disposable cache."""
    folder = PHOTOS_DIR / vehicle_id
    prepared = []
    i = start_index
    for f in files:
        try:
            img = Image.open(f.stream)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((1280, 1280))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82, optimize=True)
            prepared.append((f"{i:02d}.jpg", buf.getvalue()))
            i += 1
        except Exception as exc:
            raise ValueError("One of the selected files could not be processed as a photo.") from exc

    if not prepared:
        return []

    try:
        _store_photo_batch(vehicle_id, prepared)
        _verify_photo_batch(vehicle_id, prepared)
    except Exception as exc:
        app.logger.error("Persistent photo save or verification failed for %s: %s", vehicle_id, exc)
        raise RuntimeError("Photos could not be saved securely. Please try again.") from exc

    try:
        folder.mkdir(parents=True, exist_ok=True)
        for filename, jpeg_bytes in prepared:
            (folder / filename).write_bytes(jpeg_bytes)
    except OSError as exc:
        # The database copy is already durable and is served directly below.
        app.logger.warning("Photo cache write failed for %s: %s", vehicle_id, exc)

    return [f"/assets/photos/{vehicle_id}/{filename}" for filename, _ in prepared]


@app.get("/assets/photos/<vehicle_id>/<filename>")
def serve_vehicle_photo(vehicle_id, filename):
    """Serve durable photo bytes from PostgreSQL, with disk as a legacy fallback."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", vehicle_id) or not re.fullmatch(r"\d+\.jpg", filename):
        abort(404)
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT data FROM vehicle_photos WHERE vehicle_id=%s AND filename=%s",
                    (vehicle_id, filename),
                )
                row = cur.fetchone()
        if row:
            return send_file(BytesIO(bytes(row["data"])), mimetype="image/jpeg", max_age=3600)
    except Exception as exc:
        app.logger.error("Persistent photo read failed for %s/%s: %s", vehicle_id, filename, exc)

    legacy = PHOTOS_DIR / vehicle_id / filename
    if legacy.is_file():
        return send_from_directory(PHOTOS_DIR / vehicle_id, filename)
    app.logger.warning("Missing vehicle photo: %s/%s", vehicle_id, filename)
    return send_from_directory(ROOT / "assets", "photos-coming-soon.svg")


_backfill_photo_backups()
_recover_confirmed_stranded_vehicles()


def slugify(*parts):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", "-".join(str(p) for p in parts).lower())).strip("-")


@app.post("/api/vehicles")
def add_vehicle():
    require_admin()
    f = request.form
    required = ["year", "make", "model", "mileage"]
    missing = [k for k in required if not f.get(k, "").strip()]
    if missing:
        return jsonify(ok=False, error="Missing: " + ", ".join(missing)), 400

    with INVENTORY_LOCK:
        with inventory_mutation_lock():
            return _add_vehicle_locked(f)


def _add_vehicle_locked(f):
    data = load_inventory()
    stock = f.get("stock", "").strip() or str(max(
        [10000] + [int(v["stock"]) for v in data["vehicles"] if str(v.get("stock", "")).isdigit()]) + 1)
    vid = slugify(f["year"], f["make"], f["model"], stock)
    if any(v["id"] == vid for v in data["vehicles"]):
        return jsonify(ok=False, error="A car with that year/make/model/stock already exists."), 409

    try:
        photos = save_photos(vid, request.files.getlist("photos"))
    except (RuntimeError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)), 500
    desc = f.get("description", "").strip()
    vehicle = {
        "id": vid,
        "vin": f.get("vin", "").strip().upper(),
        "stock": stock,
        "year": int(f["year"]),
        "make": f["make"].strip(),
        "model": f["model"].strip(),
        "trim": f.get("trim", "").strip(),
        "bodyStyle": f.get("bodyStyle", "").strip() or "Sedan",
        "price": int(re.sub(r"[^0-9]", "", f.get("price", "") or "0") or 0),
        "mileage": int(re.sub(r"[^0-9]", "", f["mileage"]) or 0),
        "exteriorColor": f.get("exteriorColor", "").strip(),
        "interiorColor": f.get("interiorColor", "").strip(),
        "engine": f.get("engine", "").strip(),
        "transmission": f.get("transmission", "").strip() or "Automatic",
        "drivetrain": f.get("drivetrain", "").strip() or "FWD",
        "fuel": f.get("fuel", "").strip() or "Gasoline",
        "mpgCity": 0, "mpgHwy": 0,
        "description": desc,
        "features": [],
        "images": photos,
        "featured": f.get("featured") == "true",
        "sold": False,
        "photosComingSoon": not photos,
        "sourceUrl": "",
    }
    # Auto-generate description if none was provided
    if not desc:
        try:
            from scraper.update_descriptions import build_description as _bd
            raw_desc = _bd(vehicle)
            # Collapse newlines → spaces so the description is single-line for CSV feeds
            import re as _re
            vehicle["description"] = _re.sub(r"\s*[\r\n]+\s*", " ", raw_desc).strip()
        except Exception as _e:
            app.logger.warning("Auto-description failed: %s", _e)

    data["vehicles"].insert(0, vehicle)
    try:
        save_vehicle(vehicle)
    except Exception as exc:
        app.logger.error("save_inventory failed in add_vehicle: %s", exc)
        return jsonify(ok=False, error="Database save failed — vehicle was NOT added. Please try again."), 500
    ok_pages = regenerate_pages()
    return jsonify(
        ok=True, vehicle=vehicle, pagesRegenerated=ok_pages, photosStored=len(photos)
    )


@app.post("/api/vehicles/<vid>")
def update_vehicle(vid):
    require_admin()
    with INVENTORY_LOCK:
        with inventory_mutation_lock():
            return _update_vehicle_locked(vid)


def _update_vehicle_locked(vid):
    data = load_inventory()
    vehicle = next((v for v in data["vehicles"] if v["id"] == vid), None)
    if not vehicle:
        abort(404)

    f = request.form
    for key in ["price", "mileage"]:
        if f.get(key, "").strip():
            vehicle[key] = int(re.sub(r"[^0-9]", "", f[key]) or 0)
    for key in ["make", "model", "trim", "exteriorColor", "interiorColor",
                "engine", "bodyStyle", "drivetrain", "transmission",
                "stock", "vin"]:
        if key in f:
            vehicle[key] = f[key].strip()
    if "description" in f:
        # Collapse newlines so descriptions stay single-line for CSV feeds
        vehicle["description"] = re.sub(r"\s*[\r\n]+\s*", " ", f["description"]).strip()
    # year is numeric
    if f.get("year", "").strip():
        vehicle["year"] = int(re.sub(r"[^0-9]", "", f["year"]) or 0)
    if "sold" in f:
        vehicle["sold"] = f["sold"] == "true"
    if "featured" in f:
        vehicle["featured"] = f["featured"] == "true"

    # remove selected photos - only ones that belong to THIS vehicle
    own_dir = (PHOTOS_DIR / vid).resolve()
    remove = [p for p in f.get("removePhotos", "").split(",")
              if p and p in vehicle.get("images", [])]
    filenames_to_remove = []
    if remove:
        vehicle["images"] = [p for p in vehicle["images"] if p not in remove]
        for p in remove:
            fp = (ROOT / p.lstrip("/")).resolve()
            if fp.is_file() and fp.parent == own_dir:
                fp.unlink(missing_ok=True)
            filenames_to_remove.append(fp.name)

    # append new photos after the highest existing number
    new_files = request.files.getlist("photos")
    if new_files:
        nums = [int(m.group(1)) for p in vehicle["images"]
                if (m := re.search(r"/(\d+)\.jpg$", p))]
        try:
            vehicle["images"] += save_photos(
                vid, new_files, start_index=(max(nums) + 1) if nums else 0
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 500

    if "photosComingSoon" in vehicle or new_files or remove:
        vehicle["photosComingSoon"] = not vehicle.get("images")

    try:
        save_vehicle(vehicle)
    except Exception as exc:
        app.logger.error("save_inventory failed in update_vehicle: %s", exc)
        return jsonify(ok=False, error="Database save failed — changes were NOT saved. Please try again."), 500
    # Delete durable bytes only after the vehicle no longer references them.
    # A cleanup failure leaves harmless orphan bytes instead of broken photos.
    if filenames_to_remove:
        try:
            with _pg() as _con:
                with _con.cursor() as _cur:
                    _cur.execute(
                        "DELETE FROM vehicle_photos WHERE vehicle_id=%s AND filename=ANY(%s)",
                        (vid, filenames_to_remove),
                    )
        except Exception as _e:
            app.logger.error("vehicle_photos cleanup failed for %s: %s", vid, _e)
    ok_pages = regenerate_pages()
    return jsonify(
        ok=True, vehicle=vehicle, pagesRegenerated=ok_pages,
        photosStored=len(new_files)
    )


@app.delete("/api/vehicles/<vid>")
def delete_vehicle(vid):
    require_admin()
    with INVENTORY_LOCK:
        with inventory_mutation_lock():
            data = load_inventory()
            before = len(data["vehicles"])
            data["vehicles"] = [v for v in data["vehicles"] if v["id"] != vid]
            if len(data["vehicles"]) == before:
                abort(404)
            # Delete photo files from disk
            target = (PHOTOS_DIR / vid).resolve()
            if target.parent == PHOTOS_DIR.resolve():
                shutil.rmtree(target, ignore_errors=True)
            try:
                delete_vehicle_row(vid)
            except LookupError:
                abort(404)
            except Exception as exc:
                app.logger.error("delete_vehicle_row failed in delete_vehicle: %s", exc)
                return jsonify(ok=False, error="Database save failed — vehicle was NOT deleted. Please try again."), 500
    ok_pages = regenerate_pages()
    return jsonify(ok=True, pagesRegenerated=ok_pages)


# --------------------------------------------------------------- GHL forms

def _normalize_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone or ""


# GHL contact custom field IDs created by scripts/setup_ghl_custom_fields.py
GHL_CUSTOM_FIELDS = {
    # Standard form fields
    "Monthly Income":           "uk4JG3AD2QEdguOZa4XE",
    "Income Type":              "ILTe0Nei4VG9DU1uat65",
    "Down Payment":             "imEAs1Ty9VTVvXPZsjAE",
    "Vehicle of Interest":      "3G0bBu0xt5xDkuLCSiSo",
    "Trade-In":                 "cPuIiyAvGxkZ2IzPH9v8",
    "Trade-In VIN":             "sT1JUctI98V6q3FZKZdX",
    "Trade-In Mileage":         "BgDAhklVNWhgUv0Jcuy1",
    "Trade-In Photos":          "rsCCdaDkH2ucgFTZvQxP",
    "Message":                  "r9Rce1X3EFG3cn20wqwh",
    "Form Type":                "Yjp6Q8ftSmA9LMrQeNt5",
    "Consent Timestamp":        "l4ponTA748Ruv40DaRWC",
    # New credit app fields
    "Employer":                 "DS51tH8gqU4DBox3DoyA",
    "Hire Date":                "9HZO6uRTwhldrwt7hHPU",
    "Direct Deposits":          "z061h7x02Ehd1nP2E4KQ",
    "Trade In Owe Money":       "nQszHYHWCbdamGZr8col",
    "Social Security Number":   "bKIgCvFcSBloLiag177h",
    "Birth Date":               "wTC0CuvPxz1OMK1pf0UK",
    "Co-Signer":                "yAuXl72DY0ytojZmGqrl",
    # Co-signer fields
    "Co-Signer First Name":     "zSsjECMa4pmFm7ddAueq",
    "Co-Signer Last Name":      "6bMvIeVT7hph77ME3VJA",
    "Co-Signer Date of Birth":  "wKTY5YJjBJXbokWymm1J",
    "Co-Signer SSN":            "z9I8q7Rc0R4wtL3Ep5SA",
    "Co-Signer Phone":          "rAxtrIrizKIeRodjtzdy",
    "Co-Signer Email":          "g7S74VZT9VfnTHG1iOVX",
    "Co-Signer Address":        "gz9gSvKTPzW3fjWVHMM5",
    "Co-Signer Employer":       "GrzBm5H9D4xasoF6tmOS",
    "Co-Signer Hire Date":      "ixIbGQaWzZwsolcRsHx5",
    "Co-Signer Monthly Income": "dIIvQfhRwzuEApnncWlD",
    "Co-Signer Income Type":    "dh8Dl9ivVatf1Fmc5Nuc",
    "Co-Signer Direct Deposits":"gaJNkbEgrYx4JhUu1oA1",
    # New required fields
    "Occupation":               "g8ePtFpbeWH6FuZM9rFR",
    "Length of Residence":      "GVxOUnZSutEyxmSHhjWd",
    "ID Type":                  "r9bbhYVem7oCb5oogVTV",
    "ID Number":                "HvjSfahJl8Jh7KTfyTTk",

    "Employer Address":         "rUFYhqPLhpEIO0QV9JVc",
    "Residence Type":           "F5aXiCorVv1Ni0PrbzRB",
}


def _ghl_request(path, payload, method="POST"):
    if not GHL_API_KEY:
        raise RuntimeError("GHL_API_KEY not configured")
    url = f"https://services.leadconnectorhq.com{path}"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Version": "2021-07-28",
    }
    resp = requests.request(method, url, json=payload, headers=headers, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"GHL error {resp.status_code}: {resp.text}")
    return resp.json()


def _ghl_upload_files(location_id, field_id, files):
    """Upload files to a GHL FILE_UPLOAD custom field. Returns list of public URLs."""
    urls = []
    if not files:
        return urls
    url = f"https://services.leadconnectorhq.com/locations/{location_id}/customFields/upload"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-07-28",
    }
    for file in files:
        try:
            file.stream.seek(0)
            file_bytes = file.stream.read()
            if not file_bytes:
                app.logger.warning("Photo upload skipped: empty stream for %s", file.filename)
                continue
            resp = requests.post(
                url,
                headers=headers,
                data={"id": field_id, "maxFiles": str(len(files))},
                files={
                    "file": (
                        file.filename or "upload.jpg",
                        file_bytes,
                        file.content_type or "image/jpeg",
                    )
                },
                timeout=30,
            )
            if not resp.ok:
                app.logger.error("GHL photo upload failed %s: %s", resp.status_code, resp.text[:300])
                continue
            for meta in resp.json().get("meta", []):
                if meta.get("url"):
                    urls.append(meta["url"])
        except Exception as exc:
            app.logger.error("Photo upload exception: %s", exc)
    return urls


def send_to_ghl(data, files=None):
    """Send a website form submission to GoHighLevel as a contact + note.

    Order matters: the 'website-lead' tag triggers the GHL workflow notification.
    We add it LAST so every field, custom value, photo, and note is already on
    the contact before GHL fires the email — otherwise the notification arrives
    with blank merge tags.
    """
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        return False, "GHL not configured"

    all_tags   = list(data.get("tags", []))          # includes "website-lead"
    # Create/update the contact WITHOUT the trigger tag so the workflow doesn't
    # fire before we've written all the data.
    setup_tags = [t for t in all_tags if t != "website-lead"]

    note          = data.get("note", "")
    custom_values = data.get("custom_fields", {})

    custom_field_payload = []
    for label, field_id in GHL_CUSTOM_FIELDS.items():
        value = custom_values.get(label)
        if value:
            custom_field_payload.append({"id": field_id, "value": value})

    _email = data.get("email", "").strip()
    contact_payload = {
        "locationId":  GHL_LOCATION_ID,
        "firstName":   data.get("first_name", ""),
        "lastName":    data.get("last_name", ""),
        "phone":       _normalize_phone(data.get("phone", "")),
        # GHL rejects the request with 422 if email is present but empty or malformed;
        # omit the field entirely when we don't have a valid-looking address.
        **(({"email": _email}) if _email and "@" in _email else {}),
        "tags":        setup_tags,
        # Standard GHL contact fields (SSN, DOB, address are built-in)
        **({"ssn":         data["ssn"]}          if data.get("ssn")          else {}),
        **({"dateOfBirth": data["date_of_birth"]} if data.get("date_of_birth") else {}),
        **({"address1":    data["address"]}       if data.get("address")       else {}),
        **({"postalCode":  data["zip_code"]}      if data.get("zip_code")      else {}),
        **({"state":       data["state"]}         if data.get("state")         else {}),
        # Include custom fields at creation time so they are never silently lost
        **({"customFields": custom_field_payload} if custom_field_payload else {}),
    }

    # Step 1 — create contact (or find existing duplicate)
    contact_id = None
    try:
        result     = _ghl_request("/contacts/", contact_payload)
        contact_id = result.get("contact", {}).get("id") if isinstance(result, dict) else None
    except RuntimeError as e:
        err_str = str(e)
        import re as _re
        m = _re.search(r'"contactId"\s*:\s*"([^"]+)"', err_str)
        if m:
            # Duplicate contact — update it with all fields including custom
            contact_id = m.group(1)
            try:
                _ghl_request(f"/contacts/{contact_id}", {
                    "firstName":   contact_payload["firstName"],
                    "lastName":    contact_payload["lastName"],
                    "tags":        setup_tags,
                    **(({"email": _email}) if _email and "@" in _email else {}),
                    **({"ssn":         data["ssn"]}          if data.get("ssn")          else {}),
                    **({"dateOfBirth": data["date_of_birth"]} if data.get("date_of_birth") else {}),
                    **({"address1":    data["address"]}       if data.get("address")       else {}),
                    **({"postalCode":  data["zip_code"]}      if data.get("zip_code")      else {}),
                    **({"state":       data["state"]}         if data.get("state")         else {}),
                    **({"customFields": custom_field_payload} if custom_field_payload else {}),
                }, method="PUT")
            except RuntimeError as dup_err:
                app.logger.error("GHL duplicate contact update failed: %s", dup_err)
        else:
            raise

    if not contact_id:
        return False, "could not create or find GHL contact"

    # Step 2 — write custom fields again as a safety net (idempotent)
    if custom_field_payload:
        try:
            _ghl_request(
                f"/contacts/{contact_id}",
                {"customFields": custom_field_payload},
                method="PUT",
            )
        except RuntimeError as e:
            app.logger.error("GHL custom field write failed for %s: %s", contact_id, e)

    # Step 3 — upload trade-in photos; append URLs to note
    if files:
        try:
            photo_urls = _ghl_upload_files(
                GHL_LOCATION_ID,
                GHL_CUSTOM_FIELDS.get("Trade-In Photos"),
                files,
            )
            if photo_urls:
                note += "\n\nTrade-In Photos:\n" + "\n".join(photo_urls)
        except Exception as e:
            app.logger.error("GHL photo upload failed: %s", e)

    # Step 4 — attach note
    if note:
        try:
            _ghl_request(f"/contacts/{contact_id}/notes", {"body": note})
        except RuntimeError as e:
            app.logger.error("GHL note write failed for %s: %s", contact_id, e)

    # Step 5 — add the trigger tag LAST so GHL workflow fires with full data
    try:
        _ghl_request(f"/contacts/{contact_id}", {"tags": all_tags}, method="PUT")
    except RuntimeError as e:
        app.logger.error("GHL trigger tag failed for %s: %s", contact_id, e)

    return True, contact_id


@app.post("/api/submit-form")
def submit_form():
    # Accept both JSON (legacy) and multipart/form-data (file uploads)
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        payload = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
        files = list(request.files.getlist("Trade-In Photos") or [])
    else:
        payload = request.get_json(silent=True) or {}
        files = []
    if not payload:
        return jsonify(ok=False, error="No form data"), 400

    # Accept both snake_case (JSON legacy) and "Title Case" (FormData from HTML fields)
    def _get(key, *aliases):
        for k in (key,) + aliases:
            v = payload.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    first_name = _get("first_name", "First Name")
    last_name  = _get("last_name",  "Last Name")
    email      = _get("email",      "Email")
    phone      = _get("phone",      "Phone")
    trade_in   = _get("trade_in",   "Trade-In")

    if not first_name or not last_name:
        return jsonify(ok=False, error="First and last name are required"), 400

    form_type = _get("form_type") or "lead"

    # Credit app requires SSN, Date of Birth, and Email on the backend
    if form_type == "creditapp":
        ssn_check = _get("SSN", "ssn")
        dob_check = _get("Date of Birth", "date_of_birth")
        if not ssn_check:
            return jsonify(ok=False, error="SSN is required"), 400
        if not dob_check:
            return jsonify(ok=False, error="Date of birth is required"), 400
        if not email or "@" not in email:
            return jsonify(ok=False, error="A valid email address is required"), 400
        if not phone:
            return jsonify(ok=False, error="Phone number is required"), 400
    tags = ["website-lead"]
    if form_type == "creditapp":
        tags.append("credit-app")
    elif form_type == "contact":
        tags.append("contact-form")
    if trade_in == "Yes":
        tags.append("trade-in")

    # Build custom fields + note — skip internal keys and standard GHL contact fields
    # (SSN, Date of Birth, Address are built-in GHL fields sent via contact_payload)
    SKIP = {
        "first_name", "First Name",
        "last_name",  "Last Name",
        "email",      "Email",
        "phone",      "Phone",
        "form_type",  "tags",
        "trade_in",   "Trade-In",
        "SSN",        "ssn",
        "Date of Birth", "date_of_birth",
        "Address",    "address",
        "Zip Code",   "zip_code",
        "State",      "state",
    }
    timestamp = payload.get("Consent Timestamp") or datetime.now(ET).isoformat()
    custom_fields = {"Trade-In": trade_in} if trade_in else {}
    note_lines = [
        f"Form type: {form_type}",
        f"Name: {first_name} {last_name}",
        f"Phone: {phone}",
        f"Email: {email}",
    ]
    for key, value in payload.items():
        if key in SKIP:
            continue
        if value and str(value).strip():
            custom_fields[key] = str(value).strip()
            note_lines.append(f"{key}: {str(value).strip()}")
    if "Consent Timestamp" not in custom_fields:
        custom_fields["Consent Timestamp"] = timestamp
        note_lines.append(f"Consent Timestamp: {timestamp}")
    custom_fields["Form Type"] = form_type

    # SSN and DOB — GHL standard fields reject API writes, so send as custom fields too
    ssn           = _get("SSN",            "ssn")
    date_of_birth = _get("Date of Birth",  "date_of_birth")
    address       = _get("Address",        "address")
    zip_code      = _get("Zip Code",       "zip_code")
    state         = _get("State",          "state")
    if address:
        note_lines.append(f"Address: {address}")
    if state:
        note_lines.append(f"State: {state}")
    if zip_code:
        note_lines.append(f"Zip Code: {zip_code}")
    if ssn:
        custom_fields["Social Security Number"] = ssn
        note_lines.append(f"SSN: {ssn}")
    if date_of_birth:
        custom_fields["Birth Date"] = date_of_birth
        note_lines.append(f"Date of Birth: {date_of_birth}")

    note = "\n".join(note_lines)

    ghl_data = {
        "form_type":     form_type,
        "first_name":    first_name,
        "last_name":     last_name,
        "email":         email,
        "phone":         phone,
        "ssn":           ssn,
        "date_of_birth": date_of_birth,
        "address":       address,
        "zip_code":      zip_code,
        "state":         state,
        "tags":          tags,
        "note":          note,
        "custom_fields": custom_fields,
    }

    # Always persist to DB first — GHL failures must never lose a submission.
    log_submission(form_type, first_name, last_name, phone, email, None, _sanitize_payload(payload))

    try:
        ok, result = send_to_ghl(ghl_data, files=files)
    except RuntimeError as e:
        app.logger.error("GHL submission failed: %s", e)
        return jsonify(ok=False, error=str(e)), 502

    # Update the row with the GHL contact ID now that we have it.
    try:
        with _pg() as con:
            with con.cursor() as cur:
                cur.execute(
                    """UPDATE submissions SET ghl_contact_id=%s
                       WHERE id=(SELECT id FROM submissions
                                 WHERE form_type=%s AND first_name=%s AND last_name=%s
                                   AND (phone=%s OR email=%s)
                                 ORDER BY id DESC LIMIT 1)""",
                    (result, form_type, first_name, last_name, phone, email),
                )
    except Exception as exc:
        app.logger.error("Failed to update ghl_contact_id in DB: %s", exc)

    app.logger.info("Form submitted OK — contact %s", result)
    return jsonify(ok=True, ghl_contact_id=result)


# --------------------------------------------------------- submissions endpoint

@app.get("/api/submissions")
def get_submissions():
    require_admin()
    form_type = request.args.get("type", "")
    with _pg() as con:
        with con.cursor() as cur:
            if form_type:
                cur.execute(
                    "SELECT * FROM submissions WHERE form_type=%s ORDER BY id DESC LIMIT 200",
                    (form_type,)
                )
            else:
                cur.execute("SELECT * FROM submissions ORDER BY id DESC LIMIT 200")
            rows = cur.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
        if d.get("submitted_at"):
            d["submitted_at"] = d["submitted_at"].astimezone(ET).isoformat()
        result.append(d)
    return jsonify(submissions=result)


# ---------------------------------------------------------------- analytics

@app.get("/api/analytics")
def get_analytics():
    require_admin()
    range_ = request.args.get("range", "30d")
    hourly = range_ == "today"
    days = 1 if hourly else (7 if range_ == "7d" else 30)

    now_et = datetime.now(ET)

    with _pg() as con:
        with con.cursor() as cur:
            if hourly:
                cur.execute("""
                    SELECT DATE_TRUNC('hour', visited_at AT TIME ZONE 'America/New_York') AS bucket,
                           COUNT(*) AS count
                    FROM page_views
                    WHERE visited_at >= NOW() - INTERVAL '24 hours'
                    GROUP BY bucket ORDER BY bucket
                """)
                visit_rows = cur.fetchall()
                cur.execute("""
                    SELECT DATE_TRUNC('hour', submitted_at AT TIME ZONE 'America/New_York') AS bucket,
                           form_type, COUNT(*) AS count
                    FROM submissions
                    WHERE submitted_at >= NOW() - INTERVAL '24 hours'
                    GROUP BY bucket, form_type ORDER BY bucket
                """)
                form_rows = cur.fetchall()
            else:
                cur.execute("""
                    SELECT DATE(visited_at AT TIME ZONE 'America/New_York') AS bucket,
                           COUNT(*) AS count
                    FROM page_views
                    WHERE visited_at >= NOW() - (%s * INTERVAL '1 day')
                    GROUP BY bucket ORDER BY bucket
                """, (days,))
                visit_rows = cur.fetchall()
                cur.execute("""
                    SELECT DATE(submitted_at AT TIME ZONE 'America/New_York') AS bucket,
                           form_type, COUNT(*) AS count
                    FROM submissions
                    WHERE submitted_at >= NOW() - (%s * INTERVAL '1 day')
                    GROUP BY bucket, form_type ORDER BY bucket
                """, (days,))
                form_rows = cur.fetchall()

    # Build lookup maps keyed by truncated ISO string
    def _key(b):
        s = b.isoformat() if hasattr(b, "isoformat") else str(b)
        return s[:16] if hourly else s[:10]

    visit_map = {_key(r["bucket"]): r["count"] for r in visit_rows}

    forms_map = {}
    for r in form_rows:
        k = _key(r["bucket"])
        if k not in forms_map:
            forms_map[k] = {"contact": 0, "creditapp": 0}
        forms_map[k][r["form_type"]] = r["count"]

    # Generate full bucket list (no gaps)
    buckets = []
    if hourly:
        start = now_et.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        for i in range(24):
            dt = start + timedelta(hours=i)
            buckets.append(dt.strftime("%Y-%m-%dT%H:%M"))
    else:
        for i in range(days - 1, -1, -1):
            buckets.append(str((now_et - timedelta(days=i)).date()))

    visits = [{"label": b, "count": visit_map.get(b, 0)} for b in buckets]
    forms  = [{"label": b,
               "contact":   forms_map.get(b, {}).get("contact", 0),
               "creditapp": forms_map.get(b, {}).get("creditapp", 0)}
              for b in buckets]

    # Inquiries MTD (calendar month) and today
    with _pg() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (
                        WHERE submitted_at >= DATE_TRUNC('month', NOW() AT TIME ZONE 'America/New_York')
                              AT TIME ZONE 'America/New_York'
                    ) AS mtd,
                    COUNT(*) FILTER (
                        WHERE submitted_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'America/New_York')
                              AT TIME ZONE 'America/New_York'
                    ) AS today
                FROM submissions
            """)
            row = cur.fetchone()
            inquiries_mtd   = row["mtd"]
            inquiries_today = row["today"]

    inv = load_inventory()
    vehicles = inv.get("vehicles", [])
    return jsonify(
        visits=visits,
        forms=forms,
        inquiries_mtd=inquiries_mtd,
        inquiries_today=inquiries_today,
        inventory_active=sum(1 for v in vehicles if not v.get("sold")),
        inventory_sold=sum(1 for v in vehicles if v.get("sold")),
    )


# --------------------------------------------------------------- inventory feed

@app.get("/feeds/cargurus.xml")
@app.get("/feeds/inventory.xml")
def inventory_feed():
    """
    Standard automotive XML inventory feed.
    Works with CarGurus, AutoTrader, Cars.com, and most listing sites.
    Give the URL  https://313carloans.com/feeds/cargurus.xml  to your CarGurus rep.
    """
    data       = load_inventory()
    dealer     = data.get("dealership", {})
    vehicles   = [v for v in data.get("vehicles", []) if not v.get("sold")]

    # Parse dealer address into parts
    addr_full  = dealer.get("address", "8146 E 8 Mile Rd, Detroit, MI 48234")
    addr_parts = [p.strip() for p in addr_full.split(",")]
    street     = addr_parts[0] if len(addr_parts) > 0 else addr_full
    city       = addr_parts[1] if len(addr_parts) > 1 else "Detroit"
    state_zip  = addr_parts[2].strip().split() if len(addr_parts) > 2 else ["MI", "48234"]
    state      = state_zip[0] if len(state_zip) > 0 else "MI"
    zipcode    = state_zip[1] if len(state_zip) > 1 else "48234"

    def esc(s):
        """XML-escape a string value."""
        return (str(s or "")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<inventory>"]
    for v in vehicles:
        vid   = v.get("id", "")
        imgs  = v.get("images", [])
        price = v.get("price", 0)

        lines.append("  <vehicle>")
        lines.append(f"    <vin>{esc(v.get('vin', ''))}</vin>")
        lines.append(f"    <stock_number>{esc(v.get('stock', ''))}</stock_number>")
        lines.append(f"    <new_or_used>U</new_or_used>")
        lines.append(f"    <year>{esc(v.get('year', ''))}</year>")
        lines.append(f"    <make>{esc(v.get('make', ''))}</make>")
        lines.append(f"    <model>{esc(v.get('model', ''))}</model>")
        lines.append(f"    <trim>{esc(v.get('trim', ''))}</trim>")
        lines.append(f"    <body_style>{esc(v.get('bodyStyle', ''))}</body_style>")
        lines.append(f"    <odometer units=\"mi\">{esc(v.get('mileage', 0))}</odometer>")
        lines.append(f"    <price>{price}</price>")
        lines.append(f"    <exterior_color>{esc(v.get('exteriorColor', ''))}</exterior_color>")
        lines.append(f"    <interior_color>{esc(v.get('interiorColor', ''))}</interior_color>")
        lines.append(f"    <engine>{esc(v.get('engine', ''))}</engine>")
        lines.append(f"    <transmission>{esc(v.get('transmission', ''))}</transmission>")
        lines.append(f"    <drivetrain>{esc(v.get('drivetrain', ''))}</drivetrain>")
        lines.append(f"    <fuel_type>{esc(v.get('fuel', 'Gasoline'))}</fuel_type>")
        lines.append(f"    <description>{esc(v.get('description', ''))}</description>")
        lines.append(f"    <vehicle_url>{BASE_URL}/vehicle/{esc(vid)}/</vehicle_url>")
        dealer_name = dealer.get("name", "Matthew's Stop and Look Auto Sales")
        lines.append(f"    <dealer_name>{esc(dealer_name)}</dealer_name>")
        lines.append(f"    <dealer_address>{esc(street)}</dealer_address>")
        lines.append(f"    <dealer_city>{esc(city)}</dealer_city>")
        lines.append(f"    <dealer_state>{esc(state)}</dealer_state>")
        lines.append(f"    <dealer_zip>{esc(zipcode)}</dealer_zip>")
        lines.append(f"    <dealer_phone>{esc(dealer.get('phone', '313-891-8000'))}</dealer_phone>")
        lines.append(f"    <dealer_id>MSLDETROIT01</dealer_id>")
        lines.append(f"    <dealer_crm_email>stopandlook2@yahoo.com</dealer_crm_email>")
        lines.append(f"    <dealer_website>{BASE_URL}</dealer_website>")
        if imgs:
            lines.append("    <photos>")
            for img in imgs:
                lines.append(f"      <photo>{BASE_URL}{esc(img)}</photo>")
            lines.append("    </photos>")
        lines.append("  </vehicle>")

    lines.append("</inventory>")
    xml = "\n".join(lines)
    return app.response_class(xml, mimetype="application/xml",
                               headers={"Cache-Control": "public, max-age=3600"})


@app.get("/feeds/add.csv")
def add_feed_csv():
    """
    Pipe-delimited CSV inventory feed for AutoDealersDigital.
    Pushed automatically to FTP on every inventory change.
    The FTP filename is  inventory.csv  on  ftp.autodealersdigital.com.
    """
    data = load_inventory()
    csv_text = build_add_csv(data)
    return app.response_class(
        csv_text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="inventory.csv"',
            "Cache-Control": "public, max-age=3600",
        },
    )


def _build_cargurus_csv(data):
    """Return a comma-delimited CSV string formatted for CarGurus."""
    import csv as _csv, io as _io, re as _re

    dealer   = data.get("dealership", {})
    vehicles = [v for v in data.get("vehicles", []) if not v.get("sold")]

    addr_full  = dealer.get("address", "8146 E 8 Mile Rd, Detroit, MI 48234")
    addr_parts = [p.strip() for p in addr_full.split(",")]
    street     = addr_parts[0] if len(addr_parts) > 0 else addr_full
    city       = addr_parts[1] if len(addr_parts) > 1 else "Detroit"
    state_zip  = addr_parts[2].strip().split() if len(addr_parts) > 2 else ["MI", "48234"]
    state      = state_zip[0] if len(state_zip) > 0 else "MI"
    zipcode    = state_zip[1] if len(state_zip) > 1 else "48234"

    fieldnames = [
        "VIN","StockNumber","IsNew","Year","Make","Model","Trim","BodyStyle",
        "Mileage","Price","ExteriorColor","InteriorColor","Engine","Transmission",
        "Drivetrain","FuelType","Description","ImageURLs","VehicleURL",
        "DealerID","DealerName","DealerAddress","DealerCity","DealerState",
        "DealerZIP","DealerPhone","DealerCRMEmail","DealerWebsite",
    ]

    def _cg_text(s):
        """Strip all line breaks and collapse extra whitespace — required by CarGurus."""
        return _re.sub(r"\s+", " ", (s or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")).strip()

    buf = _io.StringIO()
    w   = _csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\r\n",
                          quoting=_csv.QUOTE_MINIMAL)
    w.writeheader()
    for v in vehicles:
        vin = _cg_text(v.get("vin", ""))
        if not vin:
            continue
        imgs     = v.get("images", [])
        img_urls = "|".join(BASE_URL + i for i in imgs)
        vid      = v.get("id", "")
        w.writerow({
            "VIN":           vin,
            "StockNumber":   _cg_text(v.get("stock", "")),
            "IsNew":         "N",
            "Year":          v.get("year", ""),
            "Make":          _cg_text(v.get("make", "")),
            "Model":         _cg_text(v.get("model", "")),
            "Trim":          _cg_text(v.get("trim", "")),
            "BodyStyle":     _cg_text(v.get("bodyStyle", "")),
            "Mileage":       v.get("mileage", ""),
            "Price":         v.get("price", ""),
            "ExteriorColor": _cg_text(v.get("exteriorColor", "")),
            "InteriorColor": _cg_text(v.get("interiorColor", "")),
            "Engine":        _cg_text(v.get("engine", "")),
            "Transmission":  _cg_text(v.get("transmission", "")),
            "Drivetrain":    _cg_text(v.get("drivetrain", "")),
            "FuelType":      _cg_text(v.get("fuel", "Gasoline")),
            "Description":   _cg_text(v.get("description", "")),
            "ImageURLs":     img_urls,
            "VehicleURL":    f"{BASE_URL}/vehicle/{vid}/",
            "DealerID":      "MSLDETROIT01",
            "DealerName":    "Matthew's Stop and Look Auto Sales",
            "DealerAddress": street,
            "DealerCity":    city,
            "DealerState":   state,
            "DealerZIP":     zipcode,
            "DealerPhone":   dealer.get("phone", "313-891-8000"),
            "DealerCRMEmail":"stopandlook2@yahoo.com",
            "DealerWebsite": BASE_URL,
        })
    return buf.getvalue()


@app.get("/feeds/cargurus.csv")
def cargurus_feed_csv():
    """
    Comma-delimited CSV inventory feed formatted for CarGurus.
    All required CarGurus fields: VIN, stock, year, make, model, trim,
    mileage, price, colors, transmission, drivetrain, images, dealer info.
    Pushed automatically to CarGurus SFTP on every inventory change.
    """
    data     = load_inventory()
    csv_text = _build_cargurus_csv(data)
    return app.response_class(
        csv_text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="matthews_stop_look_inventory.csv"',
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.post("/api/admin/push-cargurus")
def push_cargurus_manual():
    """Manually trigger an SFTP push to CarGurus."""
    require_admin()
    if not CG_FTP_PASS:
        return jsonify(ok=False, error="CG_FTP_PASSWORD secret not configured."), 500
    try:
        import paramiko, io as _io
        inv = load_inventory()
        csv_text = _build_cargurus_csv(inv)
        csv_bytes = csv_text.encode("utf-8")
        transport = paramiko.Transport((CG_FTP_HOST, CG_FTP_PORT))
        transport.connect(username=CG_FTP_USER, password=CG_FTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.putfo(_io.BytesIO(csv_bytes), "matthews_stop_look_inventory.csv")
        sftp.close()
        transport.close()
        vehicles = [v for v in inv.get("vehicles", []) if not v.get("sold")]
        return jsonify(ok=True, vehicles_sent=len(vehicles), bytes_sent=len(csv_bytes))
    except Exception as exc:
        app.logger.error("Manual CarGurus SFTP push failed: %s", exc)
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/api/admin/push-add")
def push_add_manual():
    """Manually trigger an FTP push to AutoDealersDigital."""
    require_admin()
    if not ADD_FTP_PASS:
        return jsonify(ok=False, error="ADD_FTP_PASSWORD secret not configured."), 500
    try:
        inv = load_inventory()
        csv_bytes = build_add_csv(inv).encode("utf-8")
        with ftplib.FTP(ADD_FTP_HOST, ADD_FTP_USER, ADD_FTP_PASS, timeout=30) as ftp:
            ftp.storbinary("STOR inventory.csv", io.BytesIO(csv_bytes))
        vehicles = [v for v in inv.get("vehicles", []) if not v.get("sold")]
        return jsonify(ok=True, vehicles_sent=len(vehicles), bytes_sent=len(csv_bytes))
    except Exception as exc:
        app.logger.error("Manual ADD push failed: %s", exc)
        return jsonify(ok=False, error=str(exc)), 500


# --------------------------------------------------------------- static site

@app.get("/healthz")
def healthz():
    """Lightweight health check for deployment infrastructure."""
    return jsonify(ok=True), 200


@app.after_request
def track_page_view_hook(response):
    """Log a page view for every successful GET of an HTML page on the public site."""
    if (request.method == "GET" and response.status_code == 200 and
            not request.path.startswith(("/admin", "/api", "/assets", "/data", "/scraper"))):
        last_seg = request.path.rstrip("/").split("/")[-1]
        if "." not in last_seg:          # skip .js .css .jpg .png .ico etc.
            log_page_view(request.path)
    return response


@app.after_request
def set_cache_headers(response):
    """
    HTML pages: never cache — browsers must always revalidate so users
    immediately get the latest content after a deploy.

    Versioned assets (CSS/JS with ?v=): long-lived cache is fine because
    the version string changes whenever the file changes.

    Everything else (images, video, fonts): default Flask caching applies.
    """
    path = request.path
    ct = response.content_type or ""
    if "text/html" in ct:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
def home():
    return send_from_directory(ROOT, "index.html")


@app.get("/data/inventory.js")
def live_inventory_javascript():
    """Serve inventory from PostgreSQL so every Autoscale instance is current."""
    data = load_inventory()
    body = (
        "/** Live inventory for Matthew's Stop and Look Auto Sales. */\n"
        "window.INVENTORY = " + json.dumps(data, separators=(",", ":")) + ";\n"
    )
    response = Response(body, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/vehicle/<vehicle_id>/")
def live_vehicle_page(vehicle_id):
    """Keep newly added vehicle pages working on every Autoscale instance."""
    vehicle = next(
        (v for v in load_inventory().get("vehicles", []) if v.get("id") == vehicle_id),
        None,
    )
    if not vehicle:
        abort(404)
    generated = ROOT / "vehicle" / vehicle_id / "index.html"
    if generated.is_file():
        return send_from_directory(generated.parent, generated.name)
    return send_from_directory(ROOT / "vehicle", "index.html")


@app.get("/<path:path>")
def static_site(path):
    full = (ROOT / path).resolve()
    try:
        full.relative_to(ROOT)
    except ValueError:
        abort(404)
    # hide server internals
    if path.split("/")[0] in ("main.py", "scraper", "pyproject.toml", "uv.lock", ".git", "attached_assets"):
        abort(404)
    if full.is_dir():
        return send_from_directory(full, "index.html")
    if full.is_file():
        return send_from_directory(ROOT, path)
    # /admin -> /admin/ style
    if (ROOT / path / "index.html").is_file():
        return send_from_directory(ROOT / path, "index.html")
    # Case-insensitive fallback — redirect /CREDITAPP → /creditapp/ etc.
    lower = path.lower()
    if lower != path:
        from flask import redirect
        qs = ("?" + request.query_string.decode()) if request.query_string else ""
        if (ROOT / lower).is_dir() or (ROOT / lower / "index.html").is_file():
            return redirect("/" + lower.rstrip("/") + "/" + qs, 301)
        if (ROOT / lower).is_file():
            return redirect("/" + lower + qs, 301)
    abort(404)


@app.errorhandler(401)
def unauthorized(_):
    return jsonify(ok=False, error="Not logged in"), 401


def _cargurus_scheduler():
    """Push inventory to CarGurus every 6 hours regardless of inventory changes."""
    import time
    while True:
        time.sleep(6 * 60 * 60)  # 6 hours
        try:
            app.logger.info("Scheduled CarGurus SFTP push starting")
            push_cargurus_sftp()
        except Exception as exc:
            app.logger.error("Scheduled CarGurus push error: %s", exc)

threading.Thread(target=_cargurus_scheduler, daemon=True, name="cargurus-scheduler").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
