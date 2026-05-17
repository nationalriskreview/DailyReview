"""GDELT via BigQuery — precision-focused collection.

- Bank robberies: GKG themes (CRIME_ROBBERY) + strict title filter requiring
  "bank" AND a robbery verb. Retrospective ("did a robbery happen?").
- Protests: GKG themes (PROTEST) + forward-looking title filter. Prospective
  ("is a protest announced for today / next 1–2 days?"). The Events table
  was tried originally but it has no titles and is retrospective by design.
- Transit disruptions: GKG themes (STRIKE / TRANSPORT / INFRASTRUCTURE) +
  strict title filter requiring a disruption verb AND a transit noun. Major
  disruptions only (strikes, derailments, full service suspensions). LLM
  precision pass drops false positives.

Auth: service-account JSON in GCP_SA_KEY_JSON env var.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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

QUERY_GKG_ROBBERIES = """
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
  AND (V2Themes LIKE '%CRIME_ROBBERY%' OR V2Themes LIKE '%BANK_ROBBERY%')
LIMIT 50000
"""

QUERY_GKG_PROTESTS = """
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
  AND V2Themes LIKE '%PROTEST%'
LIMIT 20000
"""

QUERY_GKG_UTILITY = """
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
  AND (V2Themes LIKE '%POWER_OUTAGE%' OR V2Themes LIKE '%WATER_SHORTAGE%')
LIMIT 20000
"""

QUERY_GKG_TRANSIT_DISRUPTION = """
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
    V2Themes LIKE '%STRIKE%'
    OR V2Themes LIKE '%TRANSPORT%'
    OR V2Themes LIKE '%INFRASTRUCTURE%'
    OR V2Themes LIKE '%MANMADE_DISASTER%'
  )
LIMIT 20000
"""

US_ADM2_PATTERN = re.compile(r"3#[^#]*#US#US[A-Z]{2}#([A-Z]{2})(\d{3})#")
PAGE_TITLE_PATTERN = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.DOTALL)

BANK_ROBBERY_VERBS = ("robbery", "robbed", "robber", "robbers", "heist", "stick-up", "stickup")

PROTEST_FORWARD_KEYWORDS = (
    "planned", "planning", "plans to",
    "scheduled", "schedule for",
    "tomorrow", "tonight",
    "this weekend", "this saturday", "this sunday", "this friday",
    "this monday", "this tuesday", "this wednesday", "this thursday",
    "upcoming", "set for", "set to",
    "expected to", "ahead of",
    "announces", "announced",
    "will rally", "will gather", "will march", "will protest", "will demonstrate",
    "to rally", "to gather", "to march", "to protest", "to demonstrate",
    "to take place", "to begin",
    "later today", "this afternoon", "this evening", "this morning",
    "next week",
)

UTILITY_KEYWORDS = (
    "power outage", "power outages", "blackout", "blackouts", "grid failure",
    "without power", "lost power", "losing power",
    "boil water", "water main break", "water shortage", "no water"
)

# Transit disruption — title must contain both a disruption phrase AND a
# transit noun. LLM pass downstream rejects remaining false positives.
TRANSIT_DISRUPTION_VERBS = (
    " strike", "strikes ", "striking",  # leading/trailing space avoids "struck", "strike out"
    "shutdown", "shut down", "shuts down", "shutting down",
    "service suspended", "suspends service", "suspending service",
    "service halted", "service halt",
    "no service",
    "service shutdown",
    "derail", "derails", "derailed", "derailment",
    "evacuat",
    "all service",
    "halt service", "halts service",
    "trains halted", "trains canceled", "trains cancelled",
    "out of service",
)
TRANSIT_DISRUPTION_NOUNS = (
    "lirr", "long island rail", "long island railroad",
    " mta ", "metro-north", "metro north", " mnr ",
    " bart ", " mbta ", "njt ", "nj transit", "nj-transit",
    "amtrak", "caltrain", "septa", "marc train",
    "subway", "subways",
    "transit",
    "railway", "railroad", "rail line", "rail service", "commuter rail",
    "rail strike", "train strike",
    " train ", " trains ",
    "light rail",
    "streetcar", "trolley",
    "metro rail", "metrorail",
    "commuter train",
)

TRANSIT_DISRUPTION_CONDITIONAL = (
    "could be", "could cause", "could halt", "could suspend",
    "may be ", "may cause", "may halt", "may suspend", "may impact",
    "might be ", "might cause",
    "possibly",
    "threatens to", "threatening to",
    " if ", "would ",
    "averted", "avoided",
    "deal reached", "agreement reached", "tentative agreement",
    " ends", " ended", " resolved", "back on track",
    " is over", " was over",
)

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


def _extract_counties_from_locations(locations: str) -> set[str]:
    if not locations:
        return set()
    found: set[str] = set()
    for m in US_ADM2_PATTERN.finditer(locations):
        fips = _county_fips(m.group(1), m.group(2))
        if fips:
            found.add(fips)
    return found


def _extract_title(extras: str) -> str:
    if not extras:
        return ""
    m = PAGE_TITLE_PATTERN.search(extras)
    if not m:
        return ""
    return m.group(1).strip()[:300]


def _title_passes_bank_robbery(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return "bank" in t and any(v in t for v in BANK_ROBBERY_VERBS)


def _title_is_forward_looking_protest(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(k in t for k in PROTEST_FORWARD_KEYWORDS)

def _title_is_utility(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(k in t for k in UTILITY_KEYWORDS)


def _title_passes_transit_disruption(title: str) -> bool:
    """Strict filter: title needs disruption verb AND transit noun, no
    conditional/forward-looking/resolution language. LLM does precision pass."""
    if not title:
        return False
    t = " " + title.lower() + " "
    if any(k in t for k in TRANSIT_DISRUPTION_CONDITIONAL):
        return False
    if not any(v in t for v in TRANSIT_DISRUPTION_VERBS):
        return False
    if not any(n in t for n in TRANSIT_DISRUPTION_NOUNS):
        return False
    return True

def _shape_article(row, title: str) -> dict:
    # GDELT date_int is YYYYMMDDHHMMSS
    is_new = False
    if row.date_int:
        try:
            s = str(row.date_int)
            dt = datetime(
                int(s[0:4]), int(s[4:6]), int(s[6:8]),
                int(s[8:10]), int(s[10:12]), int(s[12:14]),
                tzinfo=timezone.utc
            )
            now = datetime.now(timezone.utc)
            is_new = (now - dt) < timedelta(hours=12)
        except (ValueError, IndexError):
            pass

    return {
        "title": title,
        "url": row.url or "",
        "domain": row.domain or "",
        "seendate": str(row.date_int) if row.date_int else "",
        "is_new": is_new,
    }


def _collect_robberies(client) -> dict[str, list[dict]]:
    job = client.query(QUERY_GKG_ROBBERIES)
    rows = list(job)
    log.info("GDELT GKG robberies: %d rows, scanned %.2f GB",
             len(rows), (job.total_bytes_processed or 0) / 1e9)
    if not rows:
        log.warning(
            "GDELT GKG robberies: 0 rows — possible partition lag or "
            "a quiet news day; investigate if it recurs."
        )

    by_county: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        title = _extract_title(row.extras or "")
        if not _title_passes_bank_robbery(title):
            continue
        counties = _extract_counties_from_locations(row.locations or "")
        if not counties:
            continue
        article = _shape_article(row, title)
        url = article["url"]
        for fips in counties:
            if url and url in seen[fips]:
                continue
            seen[fips].add(url)
            by_county[fips].append(article)
    log.info("GDELT robberies: %d counties had post-filter matches",
             sum(1 for v in by_county.values() if v))
    return dict(by_county)


def _collect_protests(client) -> dict[str, list[dict]]:
    job = client.query(QUERY_GKG_PROTESTS)
    rows = list(job)
    log.info("GDELT GKG protests: %d rows, scanned %.2f GB",
             len(rows), (job.total_bytes_processed or 0) / 1e9)
    if not rows:
        log.warning(
            "GDELT GKG protests: 0 rows — possible partition lag; "
            "investigate if it recurs."
        )

    by_county: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    forward_count = 0
    for row in rows:
        title = _extract_title(row.extras or "")
        if not _title_is_forward_looking_protest(title):
            continue
        forward_count += 1
        counties = _extract_counties_from_locations(row.locations or "")
        if not counties:
            continue
        article = _shape_article(row, title)
        url = article["url"]
        for fips in counties:
            if url and url in seen[fips]:
                continue
            seen[fips].add(url)
            by_county[fips].append(article)
    log.info(
        "GDELT protests: %d articles passed forward-looking filter, "
        "%d counties had matches",
        forward_count, sum(1 for v in by_county.values() if v),
    )
    return dict(by_county)

def _collect_utilities(client) -> dict[str, list[dict]]:
    job = client.query(QUERY_GKG_UTILITY)
    rows = list(job)
    log.info("GDELT GKG utilities: %d rows, scanned %.2f GB",
             len(rows), (job.total_bytes_processed or 0) / 1e9)
    if not rows:
        log.warning(
            "GDELT GKG utilities: 0 rows — possible partition lag; "
            "investigate if it recurs."
        )

    by_county: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        title = _extract_title(row.extras or "")
        if not _title_is_utility(title):
            continue
        counties = _extract_counties_from_locations(row.locations or "")
        if not counties:
            continue
        article = _shape_article(row, title)
        url = article["url"]
        for fips in counties:
            if url and url in seen[fips]:
                continue
            seen[fips].add(url)
            by_county[fips].append(article)
    log.info("GDELT utilities: %d counties had matches",
             sum(1 for v in by_county.values() if v))
    return dict(by_county)


def _collect_transit_disruptions(client) -> dict[str, list[dict]]:
    job = client.query(QUERY_GKG_TRANSIT_DISRUPTION)
    rows = list(job)
    log.info("GDELT GKG transit disruption: %d rows, scanned %.2f GB",
             len(rows), (job.total_bytes_processed or 0) / 1e9)
    if not rows:
        log.warning(
            "GDELT GKG transit disruption: 0 rows — possible partition lag; "
            "investigate if it recurs."
        )

    by_county: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    passed = 0
    for row in rows:
        title = _extract_title(row.extras or "")
        if not _title_passes_transit_disruption(title):
            continue
        passed += 1
        counties = _extract_counties_from_locations(row.locations or "")
        if not counties:
            continue
        article = _shape_article(row, title)
        url = article["url"]
        for fips in counties:
            if url and url in seen[fips]:
                continue
            seen[fips].add(url)
            by_county[fips].append(article)
    log.info(
        "GDELT transit disruption: %d articles passed strict filter, "
        "%d counties had matches",
        passed, sum(1 for v in by_county.values() if v),
    )
    return dict(by_county)


def collect_gdelt_by_county() -> dict[str, dict[str, list[dict]]]:
    """Run all GKG queries; return FIPS → {bank_robbery, protest,
    utility_outage, transit_disruption}.

    bank_robbery / utility_outage / transit_disruption are retrospective
    (past 24h). protest is prospective (upcoming events announced in
    past-24h news).
    """
    client = _build_client()
    robberies = _collect_robberies(client)
    protests = _collect_protests(client)
    utilities = _collect_utilities(client)
    transit_disruptions = _collect_transit_disruptions(client)

    by_county: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {
            "bank_robbery": [], "protest": [],
            "utility_outage": [], "transit_disruption": [],
        }
    )
    for fips, arts in robberies.items():
        by_county[fips]["bank_robbery"] = arts
    for fips, arts in protests.items():
        by_county[fips]["protest"] = arts
    for fips, arts in utilities.items():
        by_county[fips]["utility_outage"] = arts
    for fips, arts in transit_disruptions.items():
        by_county[fips]["transit_disruption"] = arts

    return {k: v for k, v in by_county.items() if any(v.values())}


def merge_borough_into_county(
    county_results: dict[str, dict[str, list[dict]]],
    boroughs: Iterable[dict],
) -> dict[str, dict[str, list[dict]]]:
    """No-op — GKG location tagging already attributes events to NYC county FIPS."""
    return county_results
