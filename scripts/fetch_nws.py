"""NWS Alerts (bulk) and per-county forecast threshold checks."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

import aiohttp

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)

WATCH_EVENTS = {
    "Hurricane Watch", "Tropical Storm Watch", "Winter Storm Watch",
    "Hurricane Local Statement",
}
EXTRA_KEYWORDS = ("Hurricane", "Tropical")

PRECIP_THRESHOLD_INCHES = 1.0
SNOW_THRESHOLD_INCHES = 6.0
HEAT_THRESHOLD_F = 105.0
COLD_THRESHOLD_F = 0.0
MM_TO_IN = 0.0393701
C_TO_F = lambda c: (c * 9/5) + 32

log = logging.getLogger(__name__)


def is_relevant_event(event: str) -> bool:
    if not event:
        return False
    if "Warning" in event:
        return True
    if event in WATCH_EVENTS:
        return True
    return any(k in event for k in EXTRA_KEYWORDS)


async def fetch_active_alerts(session: aiohttp.ClientSession) -> list[dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    async with session.get(NWS_ALERTS_URL, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        r.raise_for_status()
        payload = await r.json()
    features = payload.get("features", [])
    log.info("NWS active alerts fetched: %d", len(features))
    return features


def bucket_alerts_by_county(features: list[dict]) -> dict[str, list[dict]]:
    """FIPS → list of relevant alerts. SAME codes are 6-digit (leading 0 + FIPS)."""
    by_county: dict[str, list[dict]] = {}
    seen: dict[str, set[str]] = {}
    for feat in features:
        props = feat.get("properties", {})
        event = props.get("event", "")
        if not is_relevant_event(event):
            continue
        alert_id = props.get("id") or feat.get("id", "")
        same = props.get("geocode", {}).get("SAME", []) or []
        for code in same:
            if len(code) < 5:
                continue
            fips = code[-5:]
            by_county.setdefault(fips, [])
            seen.setdefault(fips, set())
            if alert_id in seen[fips]:
                continue
            seen[fips].add(alert_id)
            by_county[fips].append({
                "event": event,
                "headline": props.get("headline", ""),
                "severity": props.get("severity", ""),
                "urgency": props.get("urgency", ""),
                "effective": props.get("effective", ""),
                "expires": props.get("expires", ""),
                "areaDesc": props.get("areaDesc", ""),
                "id": alert_id,
                "source": "nws_alert",
            })
    return by_county


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def fetch_county_forecast_threshold(
    session: aiohttp.ClientSession, county: dict
) -> list[dict]:
    """Return synthetic forecast alerts if 24h precip/snow/temps exceed thresholds.

    Uses NWS gridpoint forecast for quantitative precipitation, snowfall, and apparent temperature.
    """
    grid_points = county.get("grid", [{"lat": county["lat"], "lon": county["lon"]}])

    max_precip_in = 0.0
    max_snow_in = 0.0
    highest_temp_f = None
    lowest_temp_f = None

    for pt in grid_points:
        point = await _fetch_json(
            session, NWS_POINTS_URL.format(lat=pt["lat"], lon=pt["lon"])
        )
        if not point:
            continue
        grid_url = point.get("properties", {}).get("forecastGridData")
        if not grid_url:
            continue
        grid = await _fetch_json(session, grid_url)
        if not grid:
            continue

        grid_props = grid.get("properties", {})
        precip_mm = _sum_first_24h(grid_props.get("quantitativePrecipitation"))
        snow_mm = _sum_first_24h(grid_props.get("snowfallAmount"))
        max_app_temp_c = _max_first_24h(grid_props.get("apparentTemperature"))
        min_app_temp_c = _min_first_24h(grid_props.get("apparentTemperature"))

        precip_in = precip_mm * MM_TO_IN
        snow_in = snow_mm * MM_TO_IN
        max_app_temp_f = C_TO_F(max_app_temp_c) if max_app_temp_c is not None else None
        min_app_temp_f = C_TO_F(min_app_temp_c) if min_app_temp_c is not None else None

        if precip_in > max_precip_in:
            max_precip_in = precip_in
        if snow_in > max_snow_in:
            max_snow_in = snow_in
        if max_app_temp_f is not None:
            if highest_temp_f is None or max_app_temp_f > highest_temp_f:
                highest_temp_f = max_app_temp_f
        if min_app_temp_f is not None:
            if lowest_temp_f is None or min_app_temp_f < lowest_temp_f:
                lowest_temp_f = min_app_temp_f

    alerts = []
    if max_precip_in > PRECIP_THRESHOLD_INCHES:
        alerts.append({
            "event": "Heavy Precipitation Forecast",
            "headline": f"~{max_precip_in:.1f}\" rain expected in next 24h",
            "severity": "Moderate",
            "source": "nws_forecast",
        })
    if max_snow_in > SNOW_THRESHOLD_INCHES:
        alerts.append({
            "event": "Heavy Snow Forecast",
            "headline": f"~{max_snow_in:.1f}\" snow expected in next 24h",
            "severity": "Moderate",
            "source": "nws_forecast",
        })
    if highest_temp_f is not None and highest_temp_f > HEAT_THRESHOLD_F:
        alerts.append({
            "event": "Extreme Heat Forecast",
            "headline": f"Apparent temperature expected to reach {highest_temp_f:.1f}°F in next 24h",
            "severity": "Severe",
            "source": "nws_forecast",
        })
    if lowest_temp_f is not None and lowest_temp_f < COLD_THRESHOLD_F:
        alerts.append({
            "event": "Extreme Cold Forecast",
            "headline": f"Apparent temperature expected to drop to {lowest_temp_f:.1f}°F in next 24h",
            "severity": "Severe",
            "source": "nws_forecast",
        })
    return alerts


def _sum_first_24h(field: dict | None) -> float:
    """Sum values over the next 24h from an NWS gridpoint time-series field.

    Each entry has validTime like '2026-05-13T09:00:00+00:00/PT6H'. We sum all
    entries whose start is within 24h of the first entry.
    """
    if not field:
        return 0.0
    values = field.get("values", []) or []
    if not values:
        return 0.0
    total = 0.0
    hours_covered = 0.0
    for v in values:
        if hours_covered >= 24:
            break
        valid = v.get("validTime", "")
        if "/PT" not in valid:
            continue
        try:
            duration_str = valid.split("/PT")[1]
            if duration_str.endswith("H"):
                hours = float(duration_str[:-1])
            elif "H" in duration_str:
                hours = float(duration_str.split("H")[0])
            else:
                continue
        except (ValueError, IndexError):
            continue
        value = v.get("value")
        if value is None:
            continue
        portion = min(hours, 24 - hours_covered) / hours
        total += float(value) * portion
        hours_covered += hours
    return total

def _max_first_24h(field: dict | None) -> float | None:
    if not field:
        return None
    values = field.get("values", []) or []
    if not values:
        return None
    max_val = None
    hours_covered = 0.0
    for v in values:
        if hours_covered >= 24:
            break
        valid = v.get("validTime", "")
        if "/PT" not in valid:
            continue
        try:
            duration_str = valid.split("/PT")[1]
            if duration_str.endswith("H"):
                hours = float(duration_str[:-1])
            elif "H" in duration_str:
                hours = float(duration_str.split("H")[0])
            else:
                continue
        except (ValueError, IndexError):
            continue
        value = v.get("value")
        if value is not None:
            val_f = float(value)
            if max_val is None or val_f > max_val:
                max_val = val_f
        hours_covered += hours
    return max_val

def _min_first_24h(field: dict | None) -> float | None:
    if not field:
        return None
    values = field.get("values", []) or []
    if not values:
        return None
    min_val = None
    hours_covered = 0.0
    for v in values:
        if hours_covered >= 24:
            break
        valid = v.get("validTime", "")
        if "/PT" not in valid:
            continue
        try:
            duration_str = valid.split("/PT")[1]
            if duration_str.endswith("H"):
                hours = float(duration_str[:-1])
            elif "H" in duration_str:
                hours = float(duration_str.split("H")[0])
            else:
                continue
        except (ValueError, IndexError):
            continue
        value = v.get("value")
        if value is not None:
            val_f = float(value)
            if min_val is None or val_f < min_val:
                min_val = val_f
        hours_covered += hours
    return min_val


async def fetch_forecasts_for_counties(
    counties: Iterable[dict],
    concurrency: int = 20,
) -> dict[str, list[dict]]:
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, list[dict]] = {}

    async with aiohttp.ClientSession() as session:
        async def worker(county):
            async with sem:
                alerts = await fetch_county_forecast_threshold(session, county)
                if alerts:
                    results[county["fips"]] = alerts

        await asyncio.gather(*(worker(c) for c in counties))
    return results
