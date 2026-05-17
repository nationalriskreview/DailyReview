"""FAA airport-status — closures and ground stops at major US airports.

Source: https://nasstatus.faa.gov/api/airport-status-information (XML, ~2KB).

Why we don't surface every "closure" in the feed:
  The FAA feed's Airport_Closure_List mixes true operational closures with
  long-running NOTAMs that restrict only general-aviation aircraft (e.g.
  "AD AP CLSD TO NON SKED TRANSIENT GA ACFT EXC PPR" — commercial scheduled
  service is unaffected). We parse the NOTAM text and drop GA-only / non-
  scheduled-only entries.

Why we don't surface every delay:
  Ground Delay Programs and Arrival/Departure Delays are routine — surfacing
  them would create constant noise. We skip those sections entirely. Ground
  Stops (rare and high-impact) are surfaced.

Why we don't surface every airport:
  Limited to FAA Large Hubs (reference/airports.json). Small-airport
  closures aren't risk-relevant for a county feed.

Why we fan out to metro service areas:
  Each airport entry has a `service_counties` list — the metro region it
  primarily serves. A JFK closure flags Manhattan even though JFK is in
  Queens.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
HTTP_TIMEOUT = 30
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"

# NOTAM reason patterns that indicate the closure restricts only a subset of
# aircraft — commercial scheduled service is unaffected. Reject these.
_NOTAM_REJECT_PATTERNS = (
    re.compile(r"\bCLSD\s+TO\s+NON\s+SKED\b", re.IGNORECASE),
    re.compile(r"\bCLSD\s+TO\s+NON\s+SCHEDULED\b", re.IGNORECASE),
    re.compile(r"\bCLSD\s+TO\b[^.]*\bGA\s+ACFT\b", re.IGNORECASE),
    re.compile(r"\bCLSD\s+TO\b[^.]*\bTRANSIENT\b", re.IGNORECASE),
)


def _load_airports() -> dict[str, dict]:
    raw = json.loads((REFERENCE_DIR / "airports.json").read_text())
    return {a["code"]: a for a in raw}


def _http_get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("FAA fetch %s -> HTTP %d", url, resp.status)
                return None
            return resp.read()
    except Exception as e:
        log.warning("FAA fetch %s: %s", url, e)
        return None


def _closure_is_severe(reason: str) -> bool:
    """True if NOTAM language indicates an airport-wide closure (commercial
    service affected). False if it's a GA-only / non-scheduled-only NOTAM."""
    if not reason:
        return True  # missing reason → don't second-guess, surface it
    return not any(p.search(reason) for p in _NOTAM_REJECT_PATTERNS)


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_closures(root) -> list[dict]:
    out: list[dict] = []
    for delay_type in root.findall("Delay_type"):
        name = _text(delay_type, "Name").lower()
        if "closure" not in name:
            continue
        for ap in delay_type.findall(".//Airport"):
            out.append({
                "code": _text(ap, "ARPT").upper(),
                "reason": _text(ap, "Reason"),
                "start": _text(ap, "Start"),
                "reopen": _text(ap, "Reopen"),
            })
    return out


def _parse_ground_stops(root) -> list[dict]:
    out: list[dict] = []
    for delay_type in root.findall("Delay_type"):
        name = _text(delay_type, "Name").lower()
        if "ground stop" not in name:
            continue
        # The FAA DTD nests ground stops under Ground_Stop_List/Ground_Stop;
        # accept any element with an ARPT child as a defensive fallback.
        for gs in delay_type.findall(".//Ground_Stop"):
            out.append({
                "code": _text(gs, "ARPT").upper(),
                "reason": _text(gs, "Reason"),
                "end": _text(gs, "End_Time") or _text(gs, "End"),
            })
        if not out:
            for el in delay_type.iter():
                code = _text(el, "ARPT")
                if code:
                    out.append({
                        "code": code.upper(),
                        "reason": _text(el, "Reason"),
                        "end": _text(el, "End_Time") or _text(el, "End"),
                    })
    return out


def fetch_faa_advisories() -> tuple[list[dict], dict[str, list[dict]]]:
    """Return (national_advisories, by_fips_advisories).

    Filters: major airports only (reference/airports.json) + NOTAM severity
    check for closures. Fans each emission out to the airport's metro
    service-area counties.
    """
    airports = _load_airports()
    log.info("FAA: airport allowlist has %d entries", len(airports))

    xml_bytes = _http_get(FAA_URL)
    if not xml_bytes:
        log.warning("FAA: feed fetch failed")
        return [], {}

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log.warning("FAA: XML parse failed: %s", e)
        return [], {}

    closures = _parse_closures(root)
    ground_stops = _parse_ground_stops(root)
    log.info(
        "FAA: feed has %d closure entry/entries + %d ground stop(s) (pre-filter)",
        len(closures), len(ground_stops),
    )

    national: list[dict] = []
    by_fips: dict[str, list[dict]] = {}

    for c in closures:
        airport = airports.get(c["code"])
        if not airport:
            continue  # not a Large Hub — drop
        if not _closure_is_severe(c["reason"]):
            log.info(
                "FAA: closure at %s skipped (GA/non-scheduled only): %s",
                c["code"], c["reason"][:80],
            )
            continue
        entry = {
            "kind": "airport_closure",
            "code": c["code"],
            "name": airport["name"],
            "city": airport["city"],
            "state": airport["state"],
            "reason": c["reason"],
            "start": c["start"],
            "reopen": c["reopen"],
            "source": "FAA NAS Status",
        }
        national.append(entry)
        for fips in airport["service_counties"]:
            by_fips.setdefault(fips, []).append(entry)

    for gs in ground_stops:
        airport = airports.get(gs["code"])
        if not airport:
            continue
        entry = {
            "kind": "ground_stop",
            "code": gs["code"],
            "name": airport["name"],
            "city": airport["city"],
            "state": airport["state"],
            "reason": gs["reason"],
            "end": gs.get("end", ""),
            "source": "FAA NAS Status",
        }
        national.append(entry)
        for fips in airport["service_counties"]:
            by_fips.setdefault(fips, []).append(entry)

    log.info(
        "FAA: %d major-hub advisor(y/ies) post-filter, fanned out to %d counties",
        len(national), len(by_fips),
    )
    return national, by_fips
