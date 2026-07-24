"""GDELT via BigQuery — precision-focused collection.

Each category: a BigQuery theme/organization prefilter over the last 24h of
GKG, then a strict Python title filter, then an LLM precision pass.

- Protests: GKG PROTEST theme + protest-event title filter (upcoming/ongoing/
  past-24h).
- Utility outages: POWER_OUTAGE / WATER_SHORTAGE themes + utility title filter.
- Transit disruptions: STRIKE / TRANSPORT / INFRASTRUCTURE themes + disruption-
  verb + transit-noun title filter (major disruptions only).
- Service-provider outages: major cloud/telecom/SaaS org mentions + outage-word
  title filter (Microsoft, Google, AWS, Oracle, Cloudflare, Verizon, ...).
- Hazmat / industrial accidents: MANMADE_DISASTER theme + hazmat title filter
  (chemical spill, plant explosion, gas leak, evacuation, ...).
- Road/highway closures: TRANSPORT / INFRASTRUCTURE themes + highway-noun +
  closure-verb title filter (major closures, planned/resolved rejected).

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

QUERY_GKG_SERVICE_OUTAGE = """
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
    LOWER(V2Organizations) LIKE '%microsoft%'
    OR LOWER(V2Organizations) LIKE '%google%'
    OR LOWER(V2Organizations) LIKE '%amazon%'
    OR LOWER(V2Organizations) LIKE '%oracle%'
    OR LOWER(V2Organizations) LIKE '%cloudflare%'
    OR LOWER(V2Organizations) LIKE '%salesforce%'
    OR LOWER(V2Organizations) LIKE '%verizon%'
    OR LOWER(V2Organizations) LIKE '%at&t%'
    OR LOWER(V2Organizations) LIKE '%t-mobile%'
    OR LOWER(V2Organizations) LIKE '%comcast%'
    OR LOWER(V2Organizations) LIKE '%akamai%'
    OR LOWER(V2Organizations) LIKE '%fastly%'
    OR LOWER(V2Organizations) LIKE '%cisco%'
    OR LOWER(V2Organizations) LIKE '%okta%'
  )
LIMIT 50000
"""

QUERY_GKG_HAZMAT = """
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
    V2Themes LIKE '%MANMADE_DISASTER%'
    OR V2Themes LIKE '%ENV_OIL%'
    OR V2Themes LIKE '%HAZMAT%'
  )
LIMIT 30000
"""

QUERY_GKG_ROAD_CLOSURE = """
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
    V2Themes LIKE '%TRANSPORT%'
    OR V2Themes LIKE '%INFRASTRUCTURE%'
    OR V2Themes LIKE '%MANMADE_DISASTER%'
  )
LIMIT 30000
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

# --- Service-provider outage (cloud / telecom / SaaS) ---
SERVICE_PROVIDER_NAMES = (
    "microsoft", "azure", "office 365", "microsoft 365", "outlook", "teams",
    "google", "gmail", "google cloud", "youtube", "amazon", "aws",
    "amazon web services", "oracle", "cloudflare", "salesforce", "verizon",
    "at&t", "at t ", "t-mobile", "comcast", "xfinity", "cisco", "akamai",
    "fastly", "okta", "zoom", "slack", "github", "datadog",
)
SERVICE_OUTAGE_WORDS = (
    "outage", "outages", " down ", " down.", " down,", "goes down", "went down",
    "is down", "was down", "offline", "disruption", "disruptions", "not working",
    "unavailable", "service interruption", "widespread", "users report",
    "reports of issues", "major outage", "nationwide outage", "crashes", "crashed",
)

# --- Hazmat / industrial accidents ---
HAZMAT_KEYWORDS = (
    "chemical spill", "chemical leak", "chemical fire", "hazmat",
    "toxic spill", "toxic leak", "toxic release", "industrial accident",
    "plant explosion", "explosion at", "refinery fire", "refinery explosion",
    "chemical plant", "gas leak", "ammonia leak", "chlorine leak",
    "chlorine gas", "oil spill", "shelter in place", "shelter-in-place",
    "evacuated after", "evacuation ordered", "pipeline explosion",
    "pipeline rupture", "train carrying chemicals", "derailment spill",
)

# --- Road / highway closures ---
ROAD_NOUNS = (
    "highway", "interstate", "freeway", "expressway", "turnpike", "thruway",
    "parkway", "beltway", "motorway", " i-", "u.s. route", "us route",
    " route ", "bridge", "tunnel", "overpass", "on-ramp", "off-ramp",
)
ROAD_CLOSURE_WORDS = (
    "closed", "closure", "shut down", "shutdown", "shuts down", "blocked",
    "all lanes", "both directions", "impassable", "washed out",
)
ROAD_CLOSURE_REJECT = (
    "could", "may ", "might", "scheduled", "planned", "will close",
    "construction", "reopen", "reopens", "reopened", "back open", "cleared",
    "lane closure", "single lane", "ramp closure",  # minor/partial, not major
)

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

# Present/past protest-event indicators. Combined with PROTEST_FORWARD_KEYWORDS,
# the title gate now admits protests that are upcoming, ongoing, OR just
# happened (past 24h). The LLM pass confirms timing, reality, and location.
PROTEST_EVENT_KEYWORDS = (
    "protest", "protester", "protesters", "protestor", "protestors",
    "demonstration", "demonstrations", "demonstrator", "demonstrators",
    "rally", "rallies", "rallied",
    "march ", "marches", "marched", "marching",
    "sit-in", "walkout", "walk-out",
    "picket", "picketing", "picket line",
    "vigil",
    "riot", "rioting", "rioters",
    "unrest", "civil unrest",
    "clash", "clashes", "clashed",
    "took to the streets", "take to the streets",
    "staged a", "stage a protest",
    "uprising",
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


def _title_passes_service_outage(title: str) -> bool:
    """Title names a major service provider AND an outage/disruption word."""
    if not title:
        return False
    t = " " + title.lower() + " "
    return (any(p in t for p in SERVICE_PROVIDER_NAMES)
            and any(w in t for w in SERVICE_OUTAGE_WORDS))


def _title_passes_hazmat(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(k in t for k in HAZMAT_KEYWORDS)


def _title_passes_road_closure(title: str) -> bool:
    """Title has a highway/road noun AND a closure word, with planned/partial/
    resolved language rejected so only major active closures pass."""
    if not title:
        return False
    t = " " + title.lower() + " "
    if any(k in t for k in ROAD_CLOSURE_REJECT):
        return False
    return (any(n in t for n in ROAD_NOUNS)
            and any(w in t for w in ROAD_CLOSURE_WORDS))


def _title_is_protest_event(title: str) -> bool:
    """Title indicates a protest/demonstration event — upcoming, ongoing, or
    just-occurred (past 24h). The LLM pass downstream confirms timing, reality,
    and location. Strictly broader than the old forward-looking-only gate."""
    if not title:
        return False
    t = title.lower()
    return (any(k in t for k in PROTEST_EVENT_KEYWORDS)
            or any(k in t for k in PROTEST_FORWARD_KEYWORDS))

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


def _collect_by_title_filter(client, query: str, title_filter, label: str) -> dict[str, list[dict]]:
    """Generic GKG collector: run query, keep rows whose title passes the
    filter, fan out to counties. Used by the title-driven categories."""
    job = client.query(query)
    rows = list(job)
    log.info("GDELT GKG %s: %d rows, scanned %.2f GB",
             label, len(rows), (job.total_bytes_processed or 0) / 1e9)
    if not rows:
        log.warning("GDELT GKG %s: 0 rows — possible partition lag or quiet day.", label)

    by_county: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    passed = 0
    for row in rows:
        title = _extract_title(row.extras or "")
        if not title_filter(title):
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
    log.info("GDELT %s: %d titles passed filter, %d counties had matches",
             label, passed, sum(1 for v in by_county.values() if v))
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
    passed = 0
    for row in rows:
        title = _extract_title(row.extras or "")
        if not _title_is_protest_event(title):
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
        "GDELT protests: %d articles passed protest-event filter, "
        "%d counties had matches",
        passed, sum(1 for v in by_county.values() if v),
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


GDELT_CATEGORIES = (
    "protest", "utility_outage", "transit_disruption",
    "service_provider_outage", "hazmat_incident", "road_closure",
)


def collect_gdelt_by_county() -> dict[str, dict[str, list[dict]]]:
    """Run all GKG queries; return FIPS → {category: [articles]}.

    utility_outage / transit_disruption / service_provider_outage /
    hazmat_incident / road_closure are retrospective (past 24h). protest covers
    demonstrations that are upcoming, ongoing, or occurred within the past 24h.
    """
    client = _build_client()
    per_category = {
        "protest": _collect_protests(client),
        "utility_outage": _collect_utilities(client),
        "transit_disruption": _collect_transit_disruptions(client),
        "service_provider_outage": _collect_by_title_filter(
            client, QUERY_GKG_SERVICE_OUTAGE, _title_passes_service_outage,
            "service outage"),
        "hazmat_incident": _collect_by_title_filter(
            client, QUERY_GKG_HAZMAT, _title_passes_hazmat, "hazmat"),
        "road_closure": _collect_by_title_filter(
            client, QUERY_GKG_ROAD_CLOSURE, _title_passes_road_closure,
            "road closure"),
    }

    by_county: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {cat: [] for cat in GDELT_CATEGORIES}
    )
    for category, results in per_category.items():
        for fips, arts in results.items():
            by_county[fips][category] = arts

    return {k: v for k, v in by_county.items() if any(v.values())}


def merge_borough_into_county(
    county_results: dict[str, dict[str, list[dict]]],
    boroughs: Iterable[dict],
) -> dict[str, dict[str, list[dict]]]:
    """No-op — GKG location tagging already attributes events to NYC county FIPS."""
    return county_results
