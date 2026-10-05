"""Download the Washington State EV population dataset and load it into SQLite (PRD 000 §7)."""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

SOURCE_URL = "https://data.wa.gov/api/views/f6w7-q2d2/rows.csv?accessType=DOWNLOAD"
DATASET_PAGE = "https://data.wa.gov/Transportation/Electric-Vehicle-Population-Data/f6w7-q2d2"
DEFAULT_CSV = Path("data/raw/ev_population.csv")
DEFAULT_DB = Path("data/ev.db")

# (csv header, column, sqlite type, description)
COLUMNS = [
    ("DOL Vehicle ID", "dol_vehicle_id", "INTEGER PRIMARY KEY", "Unique vehicle ID assigned by the Department of Licensing"),
    ("VIN (1-10)", "vin_prefix", "TEXT", "First 10 characters of the VIN"),
    ("County", "county", "TEXT", "County of registration (no 'County' suffix, e.g. 'King')"),
    ("City", "city", "TEXT", "City of registration"),
    ("State", "state", "TEXT", "State of the registered address; a small number of rows are outside WA"),
    ("Postal Code", "postal_code", "TEXT", "ZIP code"),
    ("Model Year", "model_year", "INTEGER", "Vehicle model year"),
    ("Make", "make", "TEXT", "Manufacturer, upper case (e.g. 'TESLA')"),
    ("Model", "model", "TEXT", "Model name, upper case (e.g. 'MODEL Y')"),
    ("Electric Vehicle Type", "ev_type", "TEXT", "'BEV' (battery electric) or 'PHEV' (plug-in hybrid)"),
    ("Clean Alternative Fuel Vehicle (CAFV) Eligibility", "cafv_eligibility", "TEXT",
     "Clean Alternative Fuel Vehicle eligibility, based on battery range"),
    ("Electric Range", "electric_range", "INTEGER",
     "All-electric range in miles. 0 means NOT RESEARCHED (unknown), not zero miles; exclude 0 when averaging"),
    ("Legislative District", "legislative_district", "INTEGER", "WA legislative district (NULL if outside WA)"),
    ("Vehicle Location", None, None, None),  # parsed into longitude/latitude below
    ("Electric Utility", "electric_utility", "TEXT", "Electric utility (or utilities, separated by '|') serving the address"),
    ("2020 GEOID", "census_tract_2020", "TEXT", "2020 census tract GEOID"),
]
EXTRA_COLUMNS = [
    ("longitude", "REAL", "Longitude of the registration address (approximate)"),
    ("latitude", "REAL", "Latitude of the registration address (approximate)"),
]
_POINT = re.compile(r"POINT \((-?[\d.]+) (-?[\d.]+)\)")
_EV_TYPE = {"Battery Electric Vehicle (BEV)": "BEV", "Plug-in Hybrid Electric Vehicle (PHEV)": "PHEV"}


def download(dest: Path = DEFAULT_CSV) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {SOURCE_URL}", file=sys.stderr)
    with httpx.stream("GET", SOURCE_URL, follow_redirects=True, timeout=120.0) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    return dest


def load(csv_path: Path = DEFAULT_CSV, db_path: Path = DEFAULT_DB) -> int:
    """(Re)build db_path from csv_path. Returns the number of rows loaded."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA journal_mode = DELETE")  # read-only opens must not need -wal/-shm files

    cols = [(c, t) for _, c, t, _ in COLUMNS if c] + [(c, t) for c, t, _ in EXTRA_COLUMNS]
    conn.execute(f"CREATE TABLE vehicles ({', '.join(f'{c} {t}' for c, t in cols)})")
    names = [c for c, _ in cols]
    insert = f"INSERT OR IGNORE INTO vehicles ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = (_convert(r) for r in csv.DictReader(fh))
        conn.executemany(insert, (tuple(r[n] for n in names) for r in rows))

    for col in ("county", "city", "make", "model_year", "ev_type", "electric_utility"):
        conn.execute(f"CREATE INDEX idx_vehicles_{col} ON vehicles ({col})")

    conn.execute("CREATE TABLE _column_docs (table_name TEXT, column_name TEXT, description TEXT)")
    docs = [("vehicles", c, d) for _, c, _, d in COLUMNS if c] + [("vehicles", c, d) for c, _, d in EXTRA_COLUMNS]
    conn.executemany("INSERT INTO _column_docs VALUES (?, ?, ?)", docs)
    conn.execute("CREATE TABLE _dataset_info (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO _dataset_info VALUES (?, ?)", [
        ("title", "Electric Vehicle Population Data (Washington State Department of Licensing)"),
        ("source_url", DATASET_PAGE),
        ("loaded_at", datetime.now(timezone.utc).date().isoformat()),
    ])
    conn.commit()
    (count,) = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()
    conn.execute("ANALYZE")
    conn.close()
    tmp.replace(db_path)
    return count


def _convert(r: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for header, col, ctype, _ in COLUMNS:
        if not col:
            continue
        value = (r.get(header) or "").strip()
        if col == "ev_type":
            out[col] = _EV_TYPE.get(value, value or None)
        elif ctype.startswith("INTEGER"):
            out[col] = int(value) if value.lstrip("-").isdigit() else None
        else:
            out[col] = value or None
    match = _POINT.match(r.get("Vehicle Location") or "")
    out["longitude"], out["latitude"] = (float(match[1]), float(match[2])) if match else (None, None)
    return out
