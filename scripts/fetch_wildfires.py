"""Active US wildfires from NIFC WFIGS — authoritative, national.

Primary source is NIFC's WFIGS "current incident locations" feature service:
national coverage with size, containment %, cause, coordinates, and a stable
incident id. We surface actively-burning incidents — type Wildfire (WF) or
Complex (CX), at least MIN_ACRES, and not yet fully contained — and fan each
out to every county within a radius (Immediate <15mi, Vicinity <50mi).

Excluded: prescribed/controlled burns (RX) — planned, not a threat. NIFC (like
EONET before it, which this replaces) carries containment/size but NOT
evacuation orders; there is no clean national machine-readable evacuation feed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable

log = logging.getLogger(__name__)

NIFC_URL = os.environ.get(
    "NIFC_URL",
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query",
)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
DEFAULT_RADIUS_MILES = float(os.environ.get("WILDFIRE_RADIUS_MILES", "50"))
MIN_ACRES = float(os.environ.get("WILDFIRE_MIN_ACRES", "100"))
IRWIN_OBSERVER = "https://irwin.doi.gov/observer/incidents/"
HTTP_TIMEOUT = 30
_PAGE = 1000


def _http_get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("NIFC fetch failed %s: %s", url, e)
        return None


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _epoch_ms_to_iso(v) -> str:
    try:
        return datetime.fromtimestamp(float(v) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _fetch_nifc_incidents() -> list[dict]:
    where = (
        "IncidentTypeCategory IN ('WF','CX') "
        f"AND IncidentSize >= {MIN_ACRES:g} AND PercentContained < 100"
    )
    out_fields = ",".join((
        "IncidentName", "IncidentTypeCategory", "IncidentSize", "PercentContained",
        "FireCause", "FireDiscoveryDateTime", "POOState", "UniqueFireIdentifier",
        "InitialLatitude", "InitialLongitude",
    ))
    incidents: list[dict] = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": _PAGE,
            "f": "json",
        })
        data = _http_get_json(f"{NIFC_URL}?{params}")
        if not data:
            break
        feats = data.get("features", []) or []
        incidents.extend(feats)
        if len(feats) < _PAGE or not data.get("exceededTransferLimit"):
            break
        offset += _PAGE
    return incidents


def fetch_wildfires_by_county(
    counties: Iterable[dict],
    radius_miles: float = DEFAULT_RADIUS_MILES,
) -> dict[str, list[dict]]:
    feats = _fetch_nifc_incidents()
    log.info("NIFC: %d active wildfire incident(s) (>= %d ac, not fully contained)",
             len(feats), int(MIN_ACRES))

    counties_list = list(counties)
    by_county: dict[str, list[dict]] = {}

    for ft in feats:
        a = ft.get("attributes", {}) or {}
        g = ft.get("geometry") or {}
        lat = g.get("y", a.get("InitialLatitude"))
        lon = g.get("x", a.get("InitialLongitude"))
        if lat is None or lon is None:
            continue
        lat, lon = float(lat), float(lon)

        fid = a.get("UniqueFireIdentifier") or ""
        name = a.get("IncidentName") or "Wildfire"
        is_complex = a.get("IncidentTypeCategory") == "CX"
        record_base = {
            "title": name,
            "incident_name": name,
            "id": fid,
            "category": "wildfire",
            "incident_type": "Complex" if is_complex else "Wildfire",
            "date": _epoch_ms_to_iso(a.get("FireDiscoveryDateTime")),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "acreage": a.get("IncidentSize"),
            "containment_pct": a.get("PercentContained"),
            "cause": a.get("FireCause"),
            "state": (a.get("POOState") or "").replace("US-", ""),
            "source_url": f"{IRWIN_OBSERVER}{fid}" if fid else "",
            "source": "NIFC WFIGS",
        }

        for c in counties_list:
            grid_points = c.get("grid", [{"lat": c["lat"], "lon": c["lon"]}])
            min_d = min(_haversine_miles(lat, lon, pt["lat"], pt["lon"])
                        for pt in grid_points)
            if min_d > radius_miles:
                continue
            record = dict(record_base)
            record["distance_miles"] = round(min_d, 1)
            record["threat_level"] = "Immediate" if min_d < 15 else "Vicinity"
            by_county.setdefault(c["fips"], []).append(record)

    log.info("NIFC: %d counties within %d mi of an active wildfire",
             len(by_county), int(radius_miles))
    return by_county
