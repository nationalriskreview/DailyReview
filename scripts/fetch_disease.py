"""CDC HAN scraping + WHO Disease Outbreak News RSS.

NOTE: CDC restructured emergency.cdc.gov; the historical HAN landing-page URL
returns 404. CDC_HAN_URL is left as an env-overridable placeholder. When CDC
publishes a stable scraping target again, set CDC_HAN_URL to the new endpoint.
Until then, fetch_cdc_han() returns an empty list and logs a warning.

WHO retired their CSR/DON-specific RSS. The general news feed at
news-english.xml carries DON items as a subset; we filter by outbreak-related
title keywords. We use urllib here because aiohttp's default header-size
limit (8KB) rejects who.int's oversized Content-Security-Policy header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
from bs4 import BeautifulSoup

CDC_HAN_URL = os.environ.get("CDC_HAN_URL", "")
WHO_RSS_URL = os.environ.get(
    "WHO_RSS_URL",
    "https://www.who.int/rss-feeds/news-english.xml",
)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
RECENT_HOURS = int(os.environ.get("DISEASE_RECENT_HOURS", "168"))
HTTP_TIMEOUT = 30

WHO_OUTBREAK_KEYWORDS = (
    "outbreak", "epidemic", "pandemic", "disease",
    "cholera", "ebola", "mpox", "monkeypox", "marburg", "lassa",
    "influenza", "h5n1", "h1n1", "avian", "bird flu",
    "polio", "measles", "yellow fever", "dengue", "zika", "chikungunya",
    "rift valley", "nipah", "mers", "sars", "coronavirus", "covid",
    "diphtheria", "meningitis", "plague", "rabies", "tuberculosis",
    "typhoid", "salmonella", "listeria", "e. coli", "anthrax", "smallpox",
    "hantavirus", "hepatitis", "malaria", "pertussis", "rsv", "norovirus",
)

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


def _within_recent(dt: datetime | None) -> bool:
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= timedelta(hours=RECENT_HOURS)


def _is_outbreak_item(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(k in text for k in WHO_OUTBREAK_KEYWORDS)


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
    text = _http_get(WHO_RSS_URL)
    if not text:
        return []
    feed = feedparser.parse(text)
    items: list[dict] = []
    for entry in feed.entries:
        published = None
        if getattr(entry, "published", None):
            try:
                published = parsedate_to_datetime(entry.published)
            except (TypeError, ValueError):
                published = None
        if not _within_recent(published):
            continue
        title = entry.get("title", "")
        summary = entry.get("summary", "") or ""
        if not _is_outbreak_item(title, summary):
            continue
        items.append({
            "title": title,
            "url": entry.get("link", ""),
            "published": entry.get("published", ""),
            "summary": summary[:500],
            "source": "WHO News (outbreak-filtered)",
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
