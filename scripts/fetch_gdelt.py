"""GDELT via BigQuery — single SQL query against the public GKG dataset.

Replaces the rate-limited GDELT DOC 2.0 HTTP API. One query returns all US-tagged
articles in the last 24 hours matching protest/robbery/transport themes. Python
parses the V2Locations field to bucket articles by county FIPS.

Auth: reads service-account JSON from the GCP_SA_KEY_JSON env var (workflow
injects from the GCP_SA_KEY secret).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from typing import Iterable

log = logging.getLogger(__name__)

SERVICE_ACCOUNT_ENV = "GCP_SA_KEY_JSON"

STATE_POSTAL_TO_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
    "PR": "72", "VI": "78", "GU": "66", "AS": "60", "MP": "69",
}

THEME_CATEGORIES = {
    "bank_robbery": ("BANK_ROBBERY", "CRIME_ROBBERY", "ROBBERY"),
    "protest": ("PROTEST",),
    "transportation": (
        "ROAD_CLOSURE", "INFRASTRUCTURE_BAD_ROADS", "TRANSPORT_BLOCKED",
        "BRIDGE_CLOSED", "HIGHWAY_CLOSED", "TRANSIT_SUSPENDED",
    ),
}

QUERY = """
SELECT
  DocumentIdentifier AS url,
  SourceCommonName   AS domain,
  V2Themes           AS themes,
  V2Locations        AS locations,
  Extras             AS extras,
  DATE               AS date_int
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND _PARTITIONTIME <  CURRENT_TIMESTAMP()
  AND V2Locations LIKE '%#US#%'
  AND (
       V2Themes LIKE '%PROTEST%'
    OR V2Themes LIKE '%ROBBERY%'
    OR V2Themes LIKE '%ROAD_CLOSURE%'
    OR V2Themes LIKE '%TRANSPORT_BLOCKED%'
    OR V2Themes LIKE '%BRIDGE_CLOSED%'
    OR V2Themes LIKE '%HIGHWAY_CLOSED%'
    OR V2Themes LIKE '%TRANSIT_SUSPENDED%'
  )
LIMIT 100000
"""

US_ADM2_PATTERN = re.compile(r"3#[^#]*#US#US[A-Z]{2}#([A-Z]{2})(\d{3})#")
PAGE_TITLE_PATTERN = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.DOTALL)


def _build_client():
    from google.cloud import bigquery
    from google.oauth2 import service_account

    raw = os.environ.get(SERVICE_ACCOUNT_ENV)
    if not raw:
        raise RuntimeError(
            f"{SERVICE_ACCOUNT_ENV} env var is empty — workflow must inject the "
            f"service-account JSON from the GCP_SA_KEY secret."
        )
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(credentials=creds, project=info["project_id"])


def _county_fips(state_postal: str, county_3digit: str) -> str | None:
    state_fips = STATE_POSTAL_TO_FIPS.get(state_postal)
    if not state_fips:
        return None
    return f"{state_fips}{county_3digit}"


def _extract_counties(locations: str) -> set[str]:
    found: set[str] = set()
    if not locations:
        return found
    for m in US_ADM2_PATTERN.finditer(locations):
        fips = _county_fips(m.group(1), m.group(2))
        if fips:
            found.add(fips)
    return found


def _classify_categories(themes: str) -> set[str]:
    if not themes:
        return set()
    upper = themes.upper()
    return {
        cat for cat, patterns in THEME_CATEGORIES.items()
        if any(p in upper for p in patterns)
    }


def _extract_title(extras: str) -> str:
    if not extras:
        return ""
    m = PAGE_TITLE_PATTERN.search(extras)
    if not m:
        return ""
    return m.group(1).strip()[:300]


def _shape_article(row) -> dict:
    return {
        "title": _extract_title(row.extras or ""),
        "url": row.url or "",
        "domain": row.domain or "",
        "seendate": str(row.date_int) if row.date_int else "",
        "language": "",
    }


def collect_gdelt_by_county() -> dict[str, dict[str, list[dict]]]:
    """Single BigQuery call → FIPS → {bank_robbery, protest, transportation}."""
    client = _build_client()
    log.info("GDELT BigQuery: running query against gdelt-bq.gdeltv2.gkg_partitioned")
    job = client.query(QUERY)
    rows = list(job)
    log.info("GDELT BigQuery: %d rows returned, scanned %.2f GB",
             len(rows), (job.total_bytes_processed or 0) / 1e9)

    by_county: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"bank_robbery": [], "protest": [], "transportation": []}
    )
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in rows:
        cats = _classify_categories(row.themes)
        if not cats:
            continue
        counties = _extract_counties(row.locations)
        if not counties:
            continue
        shaped = _shape_article(row)
        url = shaped["url"]
        for fips in counties:
            for cat in cats:
                if url and url in seen[(fips, cat)]:
                    continue
                seen[(fips, cat)].add(url)
                by_county[fips][cat].append(shaped)

    nonzero = {k: v for k, v in by_county.items()
               if any(len(arts) for arts in v.values())}
    return nonzero


def merge_borough_into_county(
    county_results: dict[str, dict[str, list[dict]]],
    boroughs: Iterable[dict],
) -> dict[str, dict[str, list[dict]]]:
    """No-op for the BigQuery path — GKG location tagging already attributes
    Manhattan articles to FIPS 36061, etc. Kept for API compatibility.
    """
    return county_results
