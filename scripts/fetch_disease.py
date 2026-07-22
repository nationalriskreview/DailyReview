"""CDC HAN scraping + WHO Disease Outbreak News RSS.

NOTE: CDC restructured emergency.cdc.gov; the historical HAN landing-page URL
returns 404. CDC_HAN_URL is left as an env-overridable placeholder. When CDC
publishes a stable scraping target again, set CDC_HAN_URL to the new endpoint.
Until then, fetch_cdc_han() returns an empty list and logs a warning.

WHO retired their CSR/DON-specific RSS, but publishes an official Disease
Outbreak News REST API at /api/news/diseaseoutbreaknews. Every item there is
a confirmed outbreak report, so we consume it directly — no keyword filtering.
This replaces the old approach of substring-matching outbreak keywords against
WHO's *general* news feed, which leaked policy/governance/guidance items
(pandemic-treaty negotiations, dementia-risk guidelines, awards, etc.) that
are not outbreaks. We use urllib here because aiohttp's default header-size
limit (8KB) rejects who.int's oversized Content-Security-Policy header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

CDC_HAN_URL = os.environ.get("CDC_HAN_URL", "")
WHO_DON_API_URL = os.environ.get(
    "WHO_DON_API_URL",
    "https://www.who.int/api/news/diseaseoutbreaknews",
)
WHO_DON_ITEM_BASE = "https://www.who.int/emergencies/disease-outbreak-news/item/"
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
# DON is authoritative but low-volume (a handful of items per week), and an
# active outbreak from a few weeks ago is still a live concern — so the window
# is measured in days, not the tight 7d used for the old high-noise news feed.
WHO_DON_RECENT_DAYS = int(os.environ.get("WHO_DON_RECENT_DAYS", "30"))
HTTP_TIMEOUT = 30

log = logging.getLogger(__name__)


def _http_get(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log.warning("Fetch error %s: %s", url, e)
        return None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _strip_html(s: str) -> str:
    if not s:
        return ""
    if "<" not in s:
        return s.strip()
    return BeautifulSoup(s, "html.parser").get_text(" ", strip=True)


def _fetch_cdc_han_sync() -> list[dict]:
    if not CDC_HAN_URL:
        log.warning("CDC_HAN_URL not set; skipping CDC HAN.")
        return []
    html = _http_get(CDC_HAN_URL)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if not re.search(r"/han/\d{4}/", href) or not text:
            continue
        level = ""
        for cls in ("alert", "advisory", "update"):
            if cls in text.lower():
                level = cls.title()
                break
        if level == "Update":
            continue
        date_match = re.search(r"(20\d{2})", href)
        items.append({
            "title": text,
            "url": href if href.startswith("http") else f"https://www.cdc.gov{href}",
            "level": level or "Unknown",
            "year": date_match.group(1) if date_match else "",
            "source": "CDC HAN",
        })
    seen = set()
    deduped = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        deduped.append(it)
    return deduped[:20]


def _fetch_who_don_sync() -> list[dict]:
    """WHO Disease Outbreak News via the official DON REST API.

    Every item in this feed is a confirmed outbreak report, so there is no
    keyword filter — we simply take the most recent items within the window.
    """
    query = urllib.parse.urlencode({
        "$orderby": "PublicationDateAndTime desc",
        "$top": "40",
    })
    text = _http_get(f"{WHO_DON_API_URL}?{query}")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("WHO DON API returned non-JSON payload")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=WHO_DON_RECENT_DAYS)
    items: list[dict] = []
    for entry in data.get("value", []):
        pub_raw = (entry.get("PublicationDateAndTime")
                   or entry.get("PublicationDate") or "")
        pub_dt = _parse_iso(pub_raw)
        if pub_dt and pub_dt < cutoff:
            continue
        title = (entry.get("Title") or "").strip()
        if not title:
            continue
        url_name = (entry.get("UrlName")
                    or (entry.get("ItemDefaultUrl") or "").lstrip("/"))
        summary = _strip_html(entry.get("Summary") or entry.get("Overview") or "")
        items.append({
            "title": title,
            "url": f"{WHO_DON_ITEM_BASE}{url_name}" if url_name else "",
            "published": pub_raw,
            "summary": summary[:500],
            "source": "WHO Disease Outbreak News",
        })
    return items


async def fetch_national() -> dict[str, list[dict]]:
    loop = asyncio.get_event_loop()
    han, who = await asyncio.gather(
        loop.run_in_executor(None, _fetch_cdc_han_sync),
        loop.run_in_executor(None, _fetch_who_don_sync),
    )
    return {"cdc_han": han, "who_don": who}


async def fetch_county_disease() -> dict[str, list[dict]]:
    """Fetch CDC NWSS wastewater measles detections."""
    url = "https://data.cdc.gov/resource/akvg-8vrb.json"
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _http_get, url)
    if not text:
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}

    by_county: dict[str, list[dict]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    for row in data:
        # Columns: county_fips, pcr_target_detect, sample_collect_date
        fips = row.get("county_fips")
        detection = row.get("pcr_target_detect", "").lower()
        if not fips or detection != "yes":
            continue

        date_str = row.get("sample_collect_date", "")
        if date_str:
            try:
                # Socrata usually YYYY-MM-DD
                dt = datetime.fromisoformat(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except ValueError:
                pass

        by_county.setdefault(fips, []).append({
            "event": "Measles Detected (Wastewater)",
            "headline": f"Measles virus detected in wastewater sample on {date_str}",
            "detection": "Positive",
            "sampling_date": date_str,
            "source": "CDC NWSS Wastewater",
            "url": "https://www.cdc.gov/nwss/index.html",
        })

    return by_county
