"""Per-county air quality (US AQI + pollutants) via the Open-Meteo Air Quality API.

Keyless for non-commercial use, global lat/lon coverage, returns the US EPA
AQI plus PM2.5 / PM10 / ozone / NO2 concentrations and an hourly forecast.
One request per county centroid, fanned out concurrently like the NWS
gridpoint forecast. Unlike the weather forecast (which only surfaces when a
threshold is crossed), air quality is reported for *every* county as ambient
"conditions", not as an alert.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

import aiohttp

AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)

# Open-Meteo's free tier rate-limits by minute (~600/min); a naive burst of
# 3,200 requests trips HTTP 429 and silently loses most counties. Pace request
# starts to stay comfortably under that and retry the 429/5xx that still slip
# through. ~8 starts/sec ≈ 480/min → full national sweep in ~7 minutes.
AQ_REQUESTS_PER_SEC = float(os.environ.get("AQ_REQUESTS_PER_SEC", "8"))
AQ_MAX_RETRIES = int(os.environ.get("AQ_MAX_RETRIES", "4"))

# US EPA AQI breakpoints → category label.
AQI_CATEGORIES = (
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (10_000, "Hazardous"),
)

log = logging.getLogger(__name__)


def aqi_category(aqi: float | None) -> str:
    if aqi is None:
        return "Unknown"
    for upper, label in AQI_CATEGORIES:
        if aqi <= upper:
            return label
    return "Hazardous"


class _RateLimiter:
    """Spaces request *starts* to a fixed rate across all concurrent workers."""

    def __init__(self, rate_per_sec: float):
        self._interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_event_loop()
        async with self._lock:
            target = max(self._next, loop.time()) + self._interval
            self._next = target
        delay = target - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


async def _fetch_json(
    session: aiohttp.ClientSession, url: str, params: dict,
    limiter: _RateLimiter,
) -> dict | None:
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(AQ_MAX_RETRIES):
        await limiter.acquire()
        try:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 429 or r.status >= 500:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status != 200:
                    return None
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def fetch_county_air_quality(
    session: aiohttp.ClientSession, county: dict, limiter: _RateLimiter
) -> dict | None:
    """Current US AQI + pollutants for a county, plus next-24h peak AQI."""
    params = {
        "latitude": county["lat"],
        "longitude": county["lon"],
        "current": "us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide",
        "hourly": "us_aqi",
        "forecast_days": 1,
        "timezone": "UTC",
    }
    data = await _fetch_json(session, AQ_URL, params, limiter)
    if not data:
        return None

    cur = data.get("current") or {}
    aqi = cur.get("us_aqi")
    if aqi is None:
        return None

    hourly_aqi = [v for v in (data.get("hourly", {}).get("us_aqi") or [])
                  if v is not None]
    aqi_24h_max = max(hourly_aqi) if hourly_aqi else aqi

    return {
        "us_aqi": round(aqi),
        "category": aqi_category(aqi),
        "aqi_24h_max": round(aqi_24h_max),
        "aqi_24h_max_category": aqi_category(aqi_24h_max),
        "pm2_5": cur.get("pm2_5"),
        "pm10": cur.get("pm10"),
        "ozone": cur.get("ozone"),
        "nitrogen_dioxide": cur.get("nitrogen_dioxide"),
        "observed_at": cur.get("time"),
        "source": "Open-Meteo Air Quality (US EPA AQI)",
    }


async def fetch_air_quality_for_counties(
    counties: Iterable[dict],
    concurrency: int = 12,
) -> dict[str, dict]:
    sem = asyncio.Semaphore(concurrency)
    limiter = _RateLimiter(AQ_REQUESTS_PER_SEC)
    results: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        async def worker(county):
            async with sem:
                aq = await fetch_county_air_quality(session, county, limiter)
                if aq:
                    results[county["fips"]] = aq

        await asyncio.gather(*(worker(c) for c in counties))
    return results
