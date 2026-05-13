#!/usr/bin/env python3
"""Generate reference/counties.json and reference/nyc_boroughs.json.

Run once (or whenever census data updates). Output is committed to the repo
and read by the daily collection workflow.

Usage:
    python scripts/build_reference.py
"""

import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

CENSUS_GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2023_Gazetteer/2023_Gaz_counties_national.zip"
)
GAZETTEER_FILE_IN_ZIP = "2023_Gaz_counties_national.txt"

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = REPO_ROOT / "reference"

NYC_BOROUGHS = [
    {"borough": "Manhattan", "slug": "manhattan", "fips": "36061",
     "county_name": "New York County", "aliases": ["Manhattan", "New York County"]},
    {"borough": "Brooklyn", "slug": "brooklyn", "fips": "36047",
     "county_name": "Kings County", "aliases": ["Brooklyn", "Kings County"]},
    {"borough": "Queens", "slug": "queens", "fips": "36081",
     "county_name": "Queens County", "aliases": ["Queens", "Queens County"]},
    {"borough": "Bronx", "slug": "bronx", "fips": "36005",
     "county_name": "Bronx County", "aliases": ["Bronx", "The Bronx", "Bronx County"]},
    {"borough": "Staten Island", "slug": "staten-island", "fips": "36085",
     "county_name": "Richmond County", "aliases": ["Staten Island", "Richmond County"]},
]


def fetch_gazetteer() -> list[dict]:
    print(f"Fetching {CENSUS_GAZETTEER_URL}")
    req = urllib.request.Request(
        CENSUS_GAZETTEER_URL,
        headers={"User-Agent": "DailyReview/1.0 (reference-build)"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    print(f"  downloaded {len(data):,} bytes")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with zf.open(GAZETTEER_FILE_IN_ZIP) as f:
            content = f.read().decode("latin-1")

    lines = content.splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    idx = {name: i for i, name in enumerate(header)}

    counties = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = [f.strip() for f in line.split("\t")]
        if len(fields) < len(header):
            continue
        counties.append({
            "fips": fields[idx["GEOID"]],
            "name": fields[idx["NAME"]],
            "state": fields[idx["USPS"]],
            "lat": float(fields[idx["INTPTLAT"]]),
            "lon": float(fields[idx["INTPTLONG"]]),
        })
    return counties


def main() -> int:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    counties = fetch_gazetteer()
    counties.sort(key=lambda c: c["fips"])
    counties_path = REFERENCE_DIR / "counties.json"
    counties_path.write_text(json.dumps(counties, indent=2) + "\n")
    print(f"Wrote {counties_path} — {len(counties):,} counties")

    boroughs_path = REFERENCE_DIR / "nyc_boroughs.json"
    boroughs_path.write_text(json.dumps(NYC_BOROUGHS, indent=2) + "\n")
    print(f"Wrote {boroughs_path} — {len(NYC_BOROUGHS)} boroughs")

    return 0


if __name__ == "__main__":
    sys.exit(main())
