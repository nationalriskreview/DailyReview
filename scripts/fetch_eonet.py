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
import urllib.request
from typing import Iterable

log = logging.getLogger(__name__)

EONET_URL = os.environ.get(
    "EONET_URL",
    "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&category=wildfires&days=14",
)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
DEFAULT_RADIUS_MILES = float(os.environ.get("WILDFIRE_RADIUS_MILES", "50"))
HTTP_TIMEOUT = 30


def _http_get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("EONET fetch failed %s: %s", url, e)
        return None


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _latest_point(geometry: list[dict]) -> tuple[float, float, str] | None:
    if not geometry:
        return None
    pt = geometry[-1]
    coords = pt.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    lon, lat = float(coords[0]), float(coords[1])
    return lat, lon, pt.get("date", "")


def fetch_wildfires_by_county(
    counties: Iterable[dict],
    radius_miles: float = DEFAULT_RADIUS_MILES,
) -> dict[str, list[dict]]:
    data = _http_get_json(EONET_URL)
    if not data:
        return {}
    events = data.get("events", []) or []
    log.info("EONET open wildfires: %d events", len(events))

    counties_list = list(counties)
    by_county: dict[str, list[dict]] = {}

    for ev in events:
        latest = _latest_point(ev.get("geometry", []))
        if not latest:
            continue
        lat, lon, date = latest

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
            "sources": sources,
        }
        
        magnitude = ev.get("magnitudeValue")
        mag_unit = ev.get("magnitudeUnit", "").lower()
        if magnitude is not None and "acre" in mag_unit:
            record_base["acreage"] = magnitude

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
