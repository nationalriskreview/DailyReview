"""Amtrak service advisories — scrape the official curated advisories page.

Amtrak has no public alerts API. The page at
https://www.amtrak.com/service-alerts-and-notices is server-rendered HTML
with a stable class structure (Adobe AEM).

Filtering policy — county-risk-feed relevance only:

  - Passenger Advisories: surface only **service stoppages** (suspensions,
    cancellations, derailments, strikes, full route shutdowns). The bulk
    of Amtrak's posted advisories are routine "Service Adjustments" or
    "Schedule Changes" which we drop.
  - Station Advisories: surface only **full station closures**. The bulk
    are equipment-level (elevator/escalator/baggage/waiting-room/access),
    which we drop.

Route → county mapping is built from Amtrak's static GTFS (downloaded
fresh each run, ~500KB). Station codes likewise map to counties via the
GTFS stops table.

Active-today filter: an advisory must have a parsed effective date range
that includes today. Unparseable dates default to "include" — combined
with the strict severity filter, this stays safe (severe items are rare
and we'd rather miss the active-date filter than drop a real stoppage).
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
import urllib.request
import zipfile
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

ADVISORIES_URL = "https://www.amtrak.com/service-alerts-and-notices"
GTFS_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"
ALERT_BASE = "https://www.amtrak.com"
HTTP_TIMEOUT = 30
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = (
    "(january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
)

# Passenger Advisory severity filter — service stoppages only.
_PASSENGER_SEVERE_KEYWORDS = (
    "suspended", "suspension",
    "no service", "no train service",
    "service canceled", "service cancelled",
    "service cancellation",
    "trains canceled", "trains cancelled", "trains halted", "trains stopped",
    "all trains canceled", "all trains cancelled",
    "strike",
    "derail", "derailment", "derailed",
    "shutdown", "shut down",
    "evacuat",
    "route closed", "line closed", "service halted",
)
_PASSENGER_REJECT_KEYWORDS = (
    "adjustment", "adjustments",
    "modification", "modifications",
    "schedule change", "schedule changes", "modified schedule",
    "boarding changes",
    "construction", "renovation",
    "weekend",
    "bus substitution", "bus replacement", "buses replace",
    "potential", "possible",
    "averted", "resolved", "resumed", "restored",
)

# Station Advisory severity filter — full station closures only.
_STATION_SEVERE_KEYWORDS = (
    "station closed",
    "station closure",
    "station temporarily closed",
    "station shutdown", "station shut down",
    "closed indefinitely",
    "closed until further notice",
    "evacuat",
)
_STATION_REJECT_KEYWORDS = (
    "elevator", "escalator",
    "stair", "stairs", "stairwell",
    "platform repair", "platform closure", "platform closed",
    "waiting room", "waiting area",
    "ticket booth", "ticket vending", "ticket office",
    "ada", "accessibility",
    "baggage",
    "checked baggage", "no longer accept",
    "limited", "limits", "limit",
    "access impacted", "impacts access",
    "renovation", "construction", "repair work",
    "window replacement",
    "street closure",
    "boarding changes",
    "long-term", "long term",
)


def _passenger_advisory_is_severe(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    if any(k in t for k in _PASSENGER_REJECT_KEYWORDS):
        return False
    return any(k in t for k in _PASSENGER_SEVERE_KEYWORDS)


def _station_advisory_is_severe(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    if any(k in t for k in _STATION_REJECT_KEYWORDS):
        return False
    return any(k in t for k in _STATION_SEVERE_KEYWORDS)


_STATION_CODE_RE = re.compile(r"\(([A-Z]{2,4})\)\s*$")


def _http_get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("Amtrak fetch %s -> HTTP %d", url, resp.status)
                return None
            return resp.read()
    except Exception as e:
        log.warning("Amtrak fetch %s: %s", url, e)
        return None


def _parse_date_ranges(date_str: str, ref_year: int) -> list[tuple[date, date]]:
    """Extract one or more (start, end) date tuples from messy Amtrak prose.

    Handles formats observed on the live page:
      "Effective May 15 - 17, 2026"
      "Effective May 23 - 24 and May 30 - 31, 2026"
      "Effective Monday - Friday, April 21 - October 30, 2026"
      "Effective April 20, 2026"
      "Effective June 11 to July 9, 2026"
    """
    if not date_str:
        return []
    text = date_str.strip().lower()
    text = re.sub(r"\beffective\b", "", text)
    text = re.sub(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b\s*-?\s*",
        "",
        text,
    )
    text = text.replace(",", " ").replace(" to ", " - ").replace("–", "-")
    text = re.sub(r"\s+", " ", text).strip()

    m_year = re.search(r"\b(20\d{2})\b", text)
    year = int(m_year.group(1)) if m_year else ref_year
    text_no_year = re.sub(r"\b20\d{2}\b", "", text).strip()

    segments = re.split(r"\s+and\s+", text_no_year)

    ranges: list[tuple[date, date]] = []
    last_month: int | None = None
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # Pattern A: "<month1> <day1> [- [<month2>] <day2>]"
        m = re.match(
            rf"^{_MONTH_RE}\s+(\d{{1,2}})(?:\s*-\s*(?:{_MONTH_RE}\s+)?(\d{{1,2}}))?$",
            seg,
        )
        if m:
            mo1 = MONTHS[m.group(1)]
            day1 = int(m.group(2))
            mo2 = MONTHS[m.group(3)] if m.group(3) else mo1
            day2 = int(m.group(4)) if m.group(4) else day1
            year2 = year if mo2 >= mo1 else year + 1
            try:
                start = date(year, mo1, day1)
                end = date(year2, mo2, day2)
                if end >= start:
                    ranges.append((start, end))
                last_month = mo2
            except ValueError:
                pass
            continue
        # Pattern B: "<day1> [- <day2>]" — reuses last_month
        m2 = re.match(r"^(\d{1,2})(?:\s*-\s*(\d{1,2}))?$", seg)
        if m2 and last_month is not None:
            day1 = int(m2.group(1))
            day2 = int(m2.group(2)) if m2.group(2) else day1
            try:
                start = date(year, last_month, day1)
                end = date(year, last_month, day2)
                if end >= start:
                    ranges.append((start, end))
            except ValueError:
                pass
    return ranges


def _is_active_today(ranges: list[tuple[date, date]], today: date) -> bool:
    if not ranges:
        return True  # default to surfacing if we couldn't parse the date prose
    return any(s <= today <= e for s, e in ranges)


def _parse_passenger_advisories(html_soup) -> list[dict]:
    """Route-level advisories. No severity filter applied here — caller filters."""
    container = html_soup.find(
        "div", class_="na-advisories-section__tab_content_passengerAdvisories"
    )
    if not container:
        log.warning(
            "Amtrak: passenger advisories container not found — "
            "page structure may have changed"
        )
        return []

    out: list[dict] = []
    for opt in container.find_all("div", class_="na-service-alert__option"):
        h3 = opt.find("h3")
        title_tag = opt.find("a", class_="na-service-alert__option_title")
        date_tag = opt.find("span", class_="na-service-alert__option_date")
        if not (title_tag and h3):
            continue
        title = title_tag.get_text(strip=True)
        date_str = date_tag.get_text(strip=True) if date_tag else ""
        href = title_tag.get("data-href") or title_tag.get("href") or ""
        url = ALERT_BASE + href if href.startswith("/") else href

        primary_route = h3.get_text(strip=True)
        if primary_route.lower().startswith("multiple"):
            tooltip = opt.find("div", class_="tooltip__text")
            routes: list[str] = []
            if tooltip:
                for p in tooltip.find_all("p", class_="tooltip__text_content"):
                    rn = p.get_text(strip=True)
                    if rn:
                        routes.append(rn)
            if not routes:
                routes = [primary_route]
        else:
            routes = [primary_route]

        out.append({
            "kind": "passenger",
            "title": title,
            "routes": routes,
            "date_text": date_str,
            "url": url,
        })
    return out


def _parse_station_advisories(html_soup) -> list[dict]:
    """Station-level advisories. No severity filter applied here — caller filters.

    Returns entries with station code + city/state header so the caller can
    map station → county via GTFS.
    """
    container = html_soup.find(
        "div", class_="na-advisories-section__tab_content_stationAdvisories"
    )
    if not container:
        log.warning(
            "Amtrak: station advisories container not found — "
            "page structure may have changed"
        )
        return []

    out: list[dict] = []
    for li in container.find_all("li", class_="na-service-alert__stations_ul_li"):
        header_tag = li.find("span", class_="na-service-alert__stations_ul_li_header")
        if not header_tag:
            continue
        header_text = header_tag.get_text(strip=True)
        m = _STATION_CODE_RE.search(header_text)
        if not m:
            # Header without a parenthesized code — skip; we won't know how to map it.
            continue
        station_code = m.group(1)
        station_label = _STATION_CODE_RE.sub("", header_text).strip().rstrip(",")

        for alert_block in li.find_all(
            "div", class_="na-service-alert__stations_ul_li_details_alert"
        ):
            a = alert_block.find(
                "a", class_="na-service-alert__stations_ul_li_details_alert_link"
            )
            d = alert_block.find(
                "span", class_="na-service-alert__stations_ul_li_details_alert_date"
            )
            if not a:
                continue
            title = a.get_text(strip=True)
            date_str = d.get_text(strip=True) if d else ""
            href = a.get("data-href") or a.get("href") or ""
            url = ALERT_BASE + href if href.startswith("/") else href
            out.append({
                "kind": "station",
                "title": title,
                "station_code": station_code,
                "station_label": station_label,
                "date_text": date_str,
                "url": url,
            })
    return out


def _nearest_fips(lat: float, lon: float, cc: list[tuple[str, float, float]]) -> str | None:
    best_fips = None
    best_d = float("inf")
    for fips, clat, clon in cc:
        d = (clat - lat) ** 2 + (clon - lon) ** 2
        if d < best_d:
            best_d = d
            best_fips = fips
    return best_fips


def _build_gtfs_maps(
    gtfs_bytes: bytes, counties: list[dict]
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse static GTFS once and return:

      route_to_counties: {lowercase route_long_name → sorted [fips]}
      station_to_county: {uppercase station code → fips}

    Station codes come from stop_id (Amtrak's GTFS uses 3-4 letter codes
    like ALX, BOS, NYP as stop_id). stop_code is also checked as fallback.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(gtfs_bytes))
    except zipfile.BadZipFile as e:
        log.warning("Amtrak GTFS zip parse failed: %s", e)
        return {}, {}

    def _read_csv(name: str) -> list[dict]:
        try:
            with zf.open(name) as f:
                text = io.TextIOWrapper(f, encoding="utf-8-sig")
                return list(csv.DictReader(text))
        except KeyError:
            return []

    stops_rows = _read_csv("stops.txt")
    stops = {r["stop_id"]: r for r in stops_rows}
    trips = _read_csv("trips.txt")
    stop_times = _read_csv("stop_times.txt")
    routes = {r["route_id"]: r for r in _read_csv("routes.txt")}

    cc = [(c["fips"], float(c["lat"]), float(c["lon"])) for c in counties]

    station_to_county: dict[str, str] = {}
    stop_to_county: dict[str, str] = {}
    for r in stops_rows:
        try:
            lat = float(r["stop_lat"])
            lon = float(r["stop_lon"])
        except (KeyError, ValueError):
            continue
        f = _nearest_fips(lat, lon, cc)
        if not f:
            continue
        stop_to_county[r["stop_id"]] = f
        # Index by stop_id (Amtrak's canonical 3-4 letter code) and stop_code if set
        sid = (r.get("stop_id") or "").strip().upper()
        scode = (r.get("stop_code") or "").strip().upper()
        if sid and 2 <= len(sid) <= 4 and sid.isalpha():
            station_to_county[sid] = f
        if scode and 2 <= len(scode) <= 4 and scode.isalpha():
            station_to_county[scode] = f

    trip_to_route = {t["trip_id"]: t["route_id"] for t in trips}
    route_stops: dict[str, set[str]] = {}
    for st in stop_times:
        rid = trip_to_route.get(st["trip_id"])
        if rid:
            route_stops.setdefault(rid, set()).add(st["stop_id"])

    route_to_counties: dict[str, list[str]] = {}
    for rid, stop_ids in route_stops.items():
        route_info = routes.get(rid, {})
        route_name = (route_info.get("route_long_name") or "").strip()
        if not route_name:
            continue
        fips_set: set[str] = set()
        for sid in stop_ids:
            f = stop_to_county.get(sid)
            if f:
                fips_set.add(f)
        if fips_set:
            route_to_counties[route_name.lower()] = sorted(fips_set)

    return route_to_counties, station_to_county


def _match_route(advisory_route: str, route_map: dict[str, list[str]]) -> list[str]:
    if not advisory_route:
        return []
    q = advisory_route.lower().strip()
    q_clean = re.sub(r"^amtrak\s+", "", q)
    if q in route_map:
        return route_map[q]
    if q_clean in route_map:
        return route_map[q_clean]
    matches: set[str] = set()
    for name, fips_list in route_map.items():
        if name in q_clean or q_clean in name:
            matches.update(fips_list)
    return sorted(matches)


def fetch_amtrak_advisories(
    counties: list[dict],
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Return (national_advisories, by_fips_advisories).

    Strict filter: only **service stoppages** (passenger advisories) and
    **full station closures** (station advisories) effective today.
    """
    from bs4 import BeautifulSoup
    html_bytes = _http_get(ADVISORIES_URL)
    if not html_bytes:
        log.warning("Amtrak: advisories page fetch failed")
        return [], {}

    soup = BeautifulSoup(html_bytes, "html.parser")
    passenger = _parse_passenger_advisories(soup)
    station = _parse_station_advisories(soup)
    log.info(
        "Amtrak: parsed %d passenger + %d station advisor(y/ies) from page",
        len(passenger), len(station),
    )

    today = datetime.now(timezone.utc).date()

    # Apply severity + active-date filter.
    severe_passenger: list[dict] = []
    for adv in passenger:
        if not _passenger_advisory_is_severe(adv["title"]):
            continue
        ranges = _parse_date_ranges(adv["date_text"], ref_year=today.year)
        if not _is_active_today(ranges, today):
            continue
        severe_passenger.append(adv)

    severe_station: list[dict] = []
    for adv in station:
        if not _station_advisory_is_severe(adv["title"]):
            continue
        ranges = _parse_date_ranges(adv["date_text"], ref_year=today.year)
        if not _is_active_today(ranges, today):
            continue
        severe_station.append(adv)

    log.info(
        "Amtrak: post-filter %d service-stoppage + %d station-closure advisor(y/ies) active today",
        len(severe_passenger), len(severe_station),
    )

    national: list[dict] = []
    for adv in severe_passenger:
        national.append({
            "kind": "service_stoppage",
            "title": adv["title"],
            "routes": adv["routes"],
            "effective": adv["date_text"],
            "url": adv["url"],
            "source": "Amtrak service alerts page",
        })
    for adv in severe_station:
        national.append({
            "kind": "station_closure",
            "title": adv["title"],
            "station_code": adv["station_code"],
            "station": adv["station_label"],
            "effective": adv["date_text"],
            "url": adv["url"],
            "source": "Amtrak service alerts page",
        })

    if not national:
        return [], {}

    gtfs_bytes = _http_get(GTFS_URL)
    by_fips: dict[str, list[dict]] = {}
    if not gtfs_bytes:
        log.warning("Amtrak: GTFS fetch failed — county fan-out skipped")
        return national, {}

    route_map, station_map = _build_gtfs_maps(gtfs_bytes, counties)
    log.info(
        "Amtrak: built route map (%d routes) + station map (%d codes) from static GTFS",
        len(route_map), len(station_map),
    )

    for adv in severe_passenger:
        unique_fips: set[str] = set()
        unmatched: list[str] = []
        for route_name in adv["routes"]:
            fips = _match_route(route_name, route_map)
            if fips:
                unique_fips.update(fips)
            else:
                unmatched.append(route_name)
        if unmatched:
            log.info("Amtrak: route(s) %s not matched in GTFS map", unmatched)
        for fips in unique_fips:
            by_fips.setdefault(fips, []).append({
                "agency": "Amtrak",
                "kind": "service_stoppage",
                "title": adv["title"],
                "route": ", ".join(adv["routes"]),
                "effective": adv["date_text"],
                "url": adv["url"],
                "source": "amtrak_advisory",
            })

    for adv in severe_station:
        code = adv["station_code"]
        fips = station_map.get(code)
        if not fips:
            log.info("Amtrak: station code %s not matched in GTFS map", code)
            continue
        by_fips.setdefault(fips, []).append({
            "agency": "Amtrak",
            "kind": "station_closure",
            "title": adv["title"],
            "station": adv["station_label"],
            "station_code": code,
            "effective": adv["date_text"],
            "url": adv["url"],
            "source": "amtrak_advisory",
        })

    log.info(
        "Amtrak: %d total advisor(y/ies); fanned out to %d counties",
        len(national), len(by_fips),
    )
    return national, by_fips
