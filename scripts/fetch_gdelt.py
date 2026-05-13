"""GDELT DOC 2.0 — combined query per county, post-classify locally."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
from collections import defaultdict
from typing import Iterable

import aiohttp

from geography import state_full

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)

COMBINED_KEYWORDS = (
    '"bank robbery" OR "protest" OR "demonstration" '
    'OR "road closure" OR "bridge closed" OR "freeway closed" '
    'OR "transit suspended" OR "highway shutdown"'
)

BANK_KW = ("bank robbery", "bank")
PROTEST_KW = ("protest", "demonstration", "march", "rally")
PROTEST_EXCLUDE_KW = ("super bowl", "concert", "game", "stadium", "playoff",
                       "halftime", "festival")
TRANSPORT_KW = ("road closure", "bridge closed", "freeway closed",
                 "transit suspended", "highway shutdown", "highway closed",
                 "interstate closed")
TRANSPORT_REQUIRED = ("clos", "shut", "suspend", "block")

log = logging.getLogger(__name__)


def _build_query(place: str, state_postal: str) -> str:
    state = state_full(state_postal)
    return f'({COMBINED_KEYWORDS}) "{place}" "{state}"'


async def _fetch_articles(
    session: aiohttp.ClientSession, query: str
) -> list[dict]:
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": "25",
        "timespan": "1d",
        "format": "json",
        "sort": "datedesc",
    }
    url = f"{GDELT_URL}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": USER_AGENT}
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                return []
            try:
                payload = await r.json(content_type=None)
            except Exception:
                return []
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []
    return payload.get("articles", []) or []


def _classify(article: dict) -> list[str]:
    title = (article.get("title") or "").lower()
    buckets: list[str] = []

    if any(k in title for k in BANK_KW):
        if "bank" in title:
            buckets.append("bank_robbery")

    if any(k in title for k in PROTEST_KW):
        if not any(ex in title for ex in PROTEST_EXCLUDE_KW):
            buckets.append("protest")

    if any(k in title for k in TRANSPORT_KW) or (
        any(req in title for req in TRANSPORT_REQUIRED)
        and any(t in title for t in ("road", "highway", "bridge", "freeway",
                                       "transit", "interstate"))
    ):
        buckets.append("transportation")

    return buckets


def _normalize_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", "", (title or "").lower())
    words = [w for w in t.split() if len(w) > 3]
    return " ".join(words[:6])


def _dedup_by_domain(articles: list[dict]) -> list[dict]:
    """Group near-identical titles; keep only events with ≥2 distinct domains."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        groups[_normalize_title(a.get("title", ""))].append(a)
    kept: list[dict] = []
    for group in groups.values():
        domains = {a.get("domain", "") for a in group if a.get("domain")}
        if len(domains) >= 2:
            kept.append(group[0])
    return kept


def _shape(article: dict) -> dict:
    return {
        "title": article.get("title", ""),
        "url": article.get("url", ""),
        "domain": article.get("domain", ""),
        "seendate": article.get("seendate", ""),
        "language": article.get("language", ""),
    }


async def fetch_for_place(
    session: aiohttp.ClientSession,
    place: str,
    state_postal: str,
) -> dict[str, list[dict]]:
    """Return {bank_robbery, protest, transportation} → list of articles."""
    articles = await _fetch_articles(session, _build_query(place, state_postal))
    buckets: dict[str, list[dict]] = {
        "bank_robbery": [],
        "protest": [],
        "transportation": [],
    }
    for a in articles:
        for cat in _classify(a):
            buckets[cat].append(a)
    return {cat: _dedup_by_domain(arts) for cat, arts in buckets.items()}


def _merge_buckets(
    a: dict[str, list[dict]], b: dict[str, list[dict]]
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for cat in ("bank_robbery", "protest", "transportation"):
        seen_urls = set()
        merged = []
        for art in (a.get(cat, []) + b.get(cat, [])):
            url = art.get("url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(art)
        out[cat] = merged
    return out


async def fetch_for_counties(
    counties: Iterable[dict],
    concurrency: int = 20,
    delay: float = 0.05,
) -> dict[str, dict[str, list[dict]]]:
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict[str, list[dict]]] = {}

    async with aiohttp.ClientSession() as session:
        async def worker(county: dict):
            async with sem:
                buckets = await fetch_for_place(session, county["name"], county["state"])
                shaped = {k: [_shape(a) for a in v] for k, v in buckets.items()}
                if any(shaped.values()):
                    results[county["fips"]] = shaped
                await asyncio.sleep(delay)

        await asyncio.gather(*(worker(c) for c in counties))
    return results


async def fetch_for_boroughs(
    boroughs: Iterable[dict],
    concurrency: int = 5,
    delay: float = 0.1,
) -> dict[str, dict[str, list[dict]]]:
    """Returns {fips: borough_buckets}. Each borough is queried by its name."""
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict[str, list[dict]]] = {}

    async with aiohttp.ClientSession() as session:
        async def worker(b: dict):
            async with sem:
                buckets = await fetch_for_place(session, b["borough"], "NY")
                shaped = {k: [_shape(a) for a in v] for k, v in buckets.items()}
                results[b["fips"]] = shaped
                await asyncio.sleep(delay)

        await asyncio.gather(*(worker(b) for b in boroughs))
    return results


def merge_borough_into_county(
    county_results: dict[str, dict[str, list[dict]]],
    borough_results: dict[str, dict[str, list[dict]]],
) -> dict[str, dict[str, list[dict]]]:
    for fips, borough_buckets in borough_results.items():
        existing = county_results.get(fips, {"bank_robbery": [], "protest": [], "transportation": []})
        county_results[fips] = _merge_buckets(existing, borough_buckets)
    return county_results
