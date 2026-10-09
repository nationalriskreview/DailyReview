"""CDC HAN scraping + CDC US-based outbreaks (workplace-relevant filter).

NOTE: CDC restructured emergency.cdc.gov; the historical HAN landing-page URL
returns 404. CDC_HAN_URL is left as an env-overridable placeholder. When CDC
publishes a stable scraping target again, set CDC_HAN_URL to the new endpoint.
Until then, fetch_cdc_han() returns an empty list and logs a warning.

National outbreak signal comes from CDC's "US-Based Outbreaks" RSS feed,
filtered to the person-to-person communicable diseases that actually disrupt a
workplace (measles, TB, meningococcal, Legionellosis). The feed is dominated by
foodborne/enteric outbreaks (Listeria, Salmonella, E. coli, Cyclospora, ...),
which are dropped. This is the authoritative "CDC has named a US outbreak"
layer; the quantitative state-level "heating up" signal lives in fetch_nndss.py.
An earlier iteration used WHO Disease Outbreak News (all foreign) and then CDC
Travel Health Notices (foreign destinations) — both were the wrong altitude for
US workplace risk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

CDC_HAN_URL = os.environ.get("CDC_HAN_URL", "")
NWSS_MEASLES_URL = "https://data.cdc.gov/resource/akvg-8vrb.json"
# Dataset landing page — fallback link for a row without a record_id.
NWSS_MEASLES_DATASET_PAGE = "https://data.cdc.gov/d/akvg-8vrb"
NWSS_WINDOW_DAYS = 14
NWSS_PAGE_SIZE = 5000
CDC_OUTBREAKS_RSS_URL = os.environ.get(
    "CDC_OUTBREAKS_RSS_URL",
    "https://tools.cdc.gov/api/v2/resources/media/285676.rss",
)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
# Keep only outbreaks of the workplace-disruptive communicable diseases we
# track; the feed is otherwise mostly foodborne. Matched against the title.
CDC_OUTBREAK_KEYWORDS = (
    "measles", "tuberculosis", " tb ", "meningococcal", "meningitis",
    "legionel", "legionnaires",
)
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


def _fetch_cdc_outbreaks_sync() -> list[dict]:
    """CDC US-Based Outbreaks RSS, filtered to workplace-relevant communicable
    diseases (foodborne/enteric outbreaks are dropped). Parsed with stdlib XML,
    so there is no feedparser/lxml dependency."""
    text = _http_get(CDC_OUTBREAKS_RSS_URL)
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        log.warning("CDC outbreaks feed returned unparseable XML")
        return []

    items: list[dict] = []
    for item in root.iter("item"):
        title = _strip_html(item.findtext("title") or "")
        if not title:
            continue
        t = f" {title.lower()} "
        if not any(k in t for k in CDC_OUTBREAK_KEYWORDS):
            continue
        items.append({
            "title": title,
            "url": (item.findtext("link") or "").strip() or CDC_OUTBREAKS_RSS_URL,
            "published": (item.findtext("pubDate") or "").strip(),
            "summary": _strip_html(item.findtext("description") or "")[:500],
            "source": "CDC Outbreaks (US-based)",
        })
    return items


async def fetch_national() -> dict[str, list[dict]]:
    loop = asyncio.get_event_loop()
    han, outbreaks = await asyncio.gather(
        loop.run_in_executor(None, _fetch_cdc_han_sync),
        loop.run_in_executor(None, _fetch_cdc_outbreaks_sync),
    )
    return {"cdc_han": han, "cdc_outbreaks": outbreaks}


def _fetch_wastewater_rows(since: str) -> list[dict]:
    """Every positive measles sample collected on/after `since` (YYYY-MM-DD).

    Filtered server-side: the dataset holds ~76k rows, and an unfiltered
    request returns only Socrata's default page of 1,000 (in no useful order),
    which silently missed almost every recent detection. Raises on fetch or
    parse failure so the run reports the source as failed, not empty.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "$select": "record_id,county_fips,sample_collect_date",
            "$where": f"pcr_target_detect='yes' AND sample_collect_date >= '{since}'",
            "$order": "record_id",
            "$limit": str(NWSS_PAGE_SIZE),
            "$offset": str(offset),
        })
        text = _http_get(f"{NWSS_MEASLES_URL}?{params}")
        if text is None:
            raise RuntimeError("CDC NWSS wastewater fetch failed")
        try:
            page = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"CDC NWSS wastewater returned non-JSON: {e}") from e
        if not isinstance(page, list):
            raise RuntimeError(f"CDC NWSS wastewater error: {str(page)[:150]}")
        rows.extend(page)
        if len(page) < NWSS_PAGE_SIZE:
            return rows
        offset += NWSS_PAGE_SIZE


async def fetch_county_disease(stats: dict | None = None) -> dict[str, list[dict]]:
    """CDC NWSS wastewater measles detections from the last 14 days.

    One entry per county: a sewershed can serve several counties (the dataset
    lists them comma-separated), and a county can have several positive
    samples (multiple sites or days). Mirroring the weather consolidation, the
    entry reports the most recent sample, `count` positive samples, and every
    sample's record link in `urls`. If `stats` is given, it is filled with
    `positive_samples`.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=NWSS_WINDOW_DAYS)).date().isoformat()
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, _fetch_wastewater_rows, since)

    samples: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        date_str = (row.get("sample_collect_date") or "")[:10]
        record_id = row.get("record_id") or ""
        # Link the specific sample record behind this detection (CDC open data).
        record_url = (f"{NWSS_MEASLES_URL}?record_id={urllib.parse.quote(record_id)}"
                      if record_id else NWSS_MEASLES_DATASET_PAGE)
        for fips in (row.get("county_fips") or "").split(","):
            fips = fips.strip()
            if len(fips) == 5 and fips.isdigit():
                samples.setdefault(fips, []).append((date_str, record_url))

    by_county: dict[str, list[dict]] = {}
    for fips, found in samples.items():
        found.sort(reverse=True)  # newest sample first
        latest_date, latest_url = found[0]
        n = len(found)
        by_county[fips] = [{
            "event": "Measles Detected (Wastewater)",
            "headline": (f"Measles virus detected in wastewater sample on {latest_date}"
                         if n == 1 else
                         f"Measles virus detected in {n} wastewater samples, "
                         f"most recently on {latest_date}"),
            "detection": "Positive",
            "sampling_date": latest_date,
            "count": n,
            "source": "CDC NWSS Wastewater",
            "url": latest_url,
            "urls": [u for _, u in found],
        }]

    if stats is not None:
        stats["positive_samples"] = len(rows)
    return by_county
