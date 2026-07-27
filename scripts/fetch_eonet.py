"""NASA EONET — active natural-event tracker.

Fetches open wildfire events and buckets them to all US counties within a
configurable radius of the event's most recent geometry. Wildfires often span
or threaten multiple counties; per-radius bucketing better reflects business-
disruption risk than nearest-centroid attribution.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.parse
import urllib.request
from typing import Iterable

log = logging.getLogger(__name__)

EONET_URL = os.environ.get(
    "EONET_URL",
    "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&category=wildfires&days=14",
)
# NIFC WFIGS current incident locations — authoritative US containment/size,
# joinable to EONET via the IRWIN incident id (UniqueFireIdentifier).
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
HTTP_TIMEOUT = 30
_IRWIN_ID_RE = re.compile(r"incidents/([0-9A-Za-z-]+)")


def _http_get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("EONET fetch failed %s: %s", url, e)
        return None


def _fetch_nifc_by_irwin(irwin_ids: list[str]) -> dict[str, dict]:
    """Map IRWIN UniqueFireIdentifier -> {containment_pct, incident_size, name}
    from NIFC WFIGS. Chunked IN-queries; failures degrade to an empty map."""
    index: dict[str, dict] = {}
    ids = [i for i in dict.fromkeys(irwin_ids) if i]
    for start in range(0, len(ids), 40):
        chunk = ids[start:start + 40]
        in_list = ",".join("'%s'" % i.replace("'", "") for i in chunk)
        params = urllib.parse.urlencode({
            "where": f"UniqueFireIdentifier IN ({in_list})",
            "outFields": "UniqueFireIdentifier,IncidentName,PercentContained,IncidentSize",
            "returnGeometry": "false",
            "f": "json",
        })
        data = _http_get_json(f"{NIFC_URL}?{params}")
        if not data:
            continue
        for feat in data.get("features", []):
            a = feat.get("attributes", {})
            fid = a.get("UniqueFireIdentifier")
            if fid:
                index[fid] = {
                    "containment_pct": a.get("PercentContained"),
                    "incident_size": a.get("IncidentSize"),
                    "incident_name": a.get("IncidentName"),
                }
    return index


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _latest_point(geometry: list[dict]) -> dict | None:
    """Most recent geometry point. In EONET v3 the fire's size (magnitude)
    lives on each geometry point and updates over time alongside location, so
    we read it from here — not the event root, where it does not exist."""
    if not geometry:
        return None
    pt = geometry[-1]
    coords = pt.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    return {
        "lat": float(coords[1]),
        "lon": float(coords[0]),
        "date": pt.get("date", ""),
        "magnitude_value": pt.get("magnitudeValue"),
        "magnitude_unit": pt.get("magnitudeUnit", ""),
    }


def fetch_wildfires_by_county(
    counties: Iterable[dict],
    radius_miles: float = DEFAULT_RADIUS_MILES,
) -> dict[str, list[dict]]:
    data = _http_get_json(EONET_URL)
    if not data:
        return {}
    events = data.get("events", []) or []
    log.info("EONET open wildfires: %d events", len(events))

    # Collect IRWIN ids across events and bulk-fetch NIFC containment/size.
    def _irwin_id(ev: dict) -> str | None:
        for s in ev.get("sources") or []:
            if s.get("id") == "IRWIN":
                m = _IRWIN_ID_RE.search(s.get("url", "") or "")
                if m:
                    return m.group(1)
        return None

    irwin_ids = [i for i in (_irwin_id(ev) for ev in events) if i]
    try:
        nifc = _fetch_nifc_by_irwin(irwin_ids)
        log.info("NIFC: matched containment/size for %d of %d IRWIN incident(s)",
                 len(nifc), len(irwin_ids))
    except Exception as e:
        log.warning("NIFC enrichment failed (continuing without): %s", e)
        nifc = {}

    counties_list = list(counties)
    by_county: dict[str, list[dict]] = {}

    for ev in events:
        latest = _latest_point(ev.get("geometry", []))
        if not latest:
            continue
        lat, lon, date = latest["lat"], latest["lon"], latest["date"]

        sources = []
        for s in (ev.get("sources") or [])[:3]:
            u = s.get("url") or ""
            if u:
                sources.append(u)

        record_base = {
            "title": ev.get("title", ""),
            "id": ev.get("id", ""),
            "category": "wildfire",
            "date": date,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "source_url": sources[0] if sources else ev.get("link", ""),
            "sources": sources,
        }

        magnitude = latest.get("magnitude_value")
        mag_unit = (latest.get("magnitude_unit") or "").lower()
        if magnitude is not None and "acre" in mag_unit:
            record_base["acreage"] = magnitude

        # NIFC enrichment: authoritative containment %, and size as a fallback.
        info = nifc.get(_irwin_id(ev) or "")
        if info:
            record_base["containment_pct"] = info.get("containment_pct")
            if "acreage" not in record_base and info.get("incident_size") is not None:
                record_base["acreage"] = info["incident_size"]
        else:
            record_base["containment_pct"] = None

        for c in counties_list:
            grid_points = c.get("grid", [{"lat": c["lat"], "lon": c["lon"]}])
            min_d = float('inf')
            for pt in grid_points:
                d = _haversine_miles(lat, lon, pt["lat"], pt["lon"])
                if d < min_d:
                    min_d = d
            
            if min_d > radius_miles:
                continue
            record = dict(record_base)
            record["distance_miles"] = round(min_d, 1)
            record["threat_level"] = "Immediate" if min_d < 15 else "Vicinity"
            by_county.setdefault(c["fips"], []).append(record)

    log.info("EONET: %d counties within %d mi of an active wildfire",
             len(by_county), int(radius_miles))
    return by_county
