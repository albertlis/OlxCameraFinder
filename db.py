import sqlite3
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "cameras.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    title       TEXT,
    price_pln   REAL,
    location    TEXT,
    date_posted TEXT,
    image_urls  TEXT,
    scraped_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS listing_details (
    listing_id      INTEGER PRIMARY KEY REFERENCES listings(id),
    full_description TEXT,
    all_image_urls  TEXT,
    fetched_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS text_extractions (
    listing_id          INTEGER PRIMARY KEY REFERENCES listings(id),
    brand               TEXT,
    model               TEXT,
    canonical_name      TEXT,
    condition           TEXT,
    condition_confidence TEXT,
    model_confidence    TEXT,
    extracted_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_specs (
    canonical_name  TEXT PRIMARY KEY,
    brand           TEXT,
    focal_mm        INTEGER,
    aperture_max    REAL,
    metering_type   TEXT,
    focus_type      TEXT,
    is_slr          INTEGER,
    specs_source    TEXT,
    fetched_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vision_obs_raw (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          INTEGER REFERENCES listings(id),
    image_url           TEXT,
    visible_model_text  TEXT,
    condition           TEXT,
    defects             TEXT,
    lens_visible        TEXT,
    needs_more_images   INTEGER,
    analyzed_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vision_obs (
    listing_id          INTEGER PRIMARY KEY REFERENCES listings(id),
    visible_model_text  TEXT,
    condition           TEXT,
    defects             TEXT,
    images_used         INTEGER,
    analyzed_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    listing_id          INTEGER PRIMARY KEY REFERENCES listings(id),
    canonical_name      TEXT,
    overall_score       REAL,
    metering_upgrade    INTEGER,
    optics_upgrade      INTEGER,
    is_point_and_shoot  INTEGER,
    vibe_90s            INTEGER,
    condition_ok        INTEGER,
    recommended         INTEGER,
    reasoning           TEXT,
    skip_reason         TEXT,
    model_used          TEXT,
    scored_at           TEXT DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    _ = conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ = conn.execute("PRAGMA journal_mode=WAL")
    return conn
