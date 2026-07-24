"""CDC HAN scraping + CDC Travel Health Notices.

NOTE: CDC restructured emergency.cdc.gov; the historical HAN landing-page URL
returns 404. CDC_HAN_URL is left as an env-overridable placeholder. When CDC
publishes a stable scraping target again, set CDC_HAN_URL to the new endpoint.
Until then, fetch_cdc_han() returns an empty list and logs a warning.

National outbreak signal comes from CDC's Travel Health Notices RSS
(wwwnc.cdc.gov/travel/rss/notices.xml). This replaced the WHO Disease Outbreak
News feed, which surfaced global outbreaks with no US relevance (Ebola in DRC,
Nipah in India, etc.) and exposed no country/region field to filter on. CDC's
notices are US-government-curated and severity-graded (Level 1 Watch / Level 2
Alert / Level 3 Warning), which is a far better fit for a US risk feed.
Domestic per-county disease signal is handled separately by the CDC NWSS
wastewater collector below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

CDC_HAN_URL = os.environ.get("CDC_HAN_URL", "")
CDC_TRAVEL_NOTICES_URL = os.environ.get(
    "CDC_TRAVEL_NOTICES_URL",
    "https://wwwnc.cdc.gov/travel/rss/notices.xml",
)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
# Travel notices are curated and stay active for a while; cap the list rather
# than filtering hard by age so genuinely-active older notices aren't dropped.
CDC_TRAVEL_NOTICES_MAX = int(os.environ.get("CDC_TRAVEL_NOTICES_MAX", "30"))
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


def _parse_level(title: str) -> str:
    """CDC notice titles start with 'Level N - ...'; map to CDC's labels."""
    m = re.match(r"\s*Level\s*(\d)", title, re.IGNORECASE)
    if not m:
        return "Unknown"
    return {
        "1": "Level 1 (Watch)",
        "2": "Level 2 (Alert)",
        "3": "Level 3 (Warning)",
    }.get(m.group(1), f"Level {m.group(1)}")


def _fetch_cdc_travel_notices_sync() -> list[dict]:
    """CDC Travel Health Notices — US-government-curated, severity-graded
    outbreak/health-risk notices. Parsed from the RSS feed with stdlib XML so
    there is no feedparser/lxml dependency."""
    text = _http_get(CDC_TRAVEL_NOTICES_URL)
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        log.warning("CDC travel notices returned unparseable XML")
        return []

    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        pub_raw = (item.findtext("pubDate") or "").strip()
        pub_ts = 0.0
        if pub_raw:
            try:
                pub_ts = parsedate_to_datetime(pub_raw).timestamp()
            except (TypeError, ValueError):
                pub_ts = 0.0
        items.append({
            "title": title,
            "url": (item.findtext("link") or "").strip(),
            "level": _parse_level(title),
            "published": pub_raw,
            "summary": _strip_html(item.findtext("description") or "")[:500],
            "source": "CDC Travel Health Notices",
            "_sort": pub_ts,
        })

    items.sort(key=lambda x: x["_sort"], reverse=True)
    for it in items:
        del it["_sort"]
    return items[:CDC_TRAVEL_NOTICES_MAX]


async def fetch_national() -> dict[str, list[dict]]:
    loop = asyncio.get_event_loop()
    han, notices = await asyncio.gather(
        loop.run_in_executor(None, _fetch_cdc_han_sync),
        loop.run_in_executor(None, _fetch_cdc_travel_notices_sync),
    )
    return {"cdc_han": han, "cdc_travel_notices": notices}


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
