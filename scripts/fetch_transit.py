"""Mass-transit Service Alerts via GTFS-Realtime.

Filters aggressively for severe outages only:
  - effect == NO_SERVICE                            (full stoppage, not delays)
  - active right now                                (start <= now <= end)
  - scope is route-level or higher                  (no single-stop noise)
  - description doesn't match planned-maintenance   (weekend/track work/etc.)

Flags `system_outage=True` when the alert references the agency as a whole
rather than a specific route — these are the rare high-signal events
(weather closures, strikes, infrastructure failures).

Agencies configured in reference/transit_agencies.json. Per-agency failures
are isolated; one agency's outage doesn't poison the whole run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
HTTP_TIMEOUT = 30
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)

EFFECT_NO_SERVICE = 1

PLANNED_KEYWORDS = (
    "weekend", "this sunday", "this saturday", "scheduled maintenance",
    "planned maintenance", "track work", "construction project",
    "advance notice", "future change", "upcoming",
)

WMATA_SEVERE_KEYWORDS = (
    "suspend", "no service", "no trains", "service halted",
    "shut down", "shutdown", "evacuat",
)

CTA_RAIL_SERVICE_TYPES = {"T", "R"}
CTA_RAIL_LINE_NAMES = {
    "red", "blue", "brown", "green", "orange", "purple", "pink", "yellow",
}
CTA_SEVERE_SCORE = 70

PATH_SEVERE_KEYWORDS = (
    "suspend", "no service", "no trains", "service halted",
    "shut down", "shutdown", "evacuat", "no train service",
)
PATH_PLANNED_KEYWORDS = (
    "planned", "scheduled", "overnight", "weekend",
    "single-tracking", "single tracking",
    "construction", "track work", "advisory",
    "may leave up to", "delay of up to", "delays of up to",
)


def load_agencies() -> list[dict]:
    return json.loads((REFERENCE_DIR / "transit_agencies.json").read_text())


def _http_get(url: str, headers: dict | None = None) -> bytes | None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("Transit fetch %s -> HTTP %d", url, resp.status)
                return None
            return resp.read()
    except Exception as e:
        log.warning("Transit fetch %s: %s", url, e)
        return None


def _is_active(active_periods, now_ts: int) -> bool:
    if not active_periods:
        return True
    for period in active_periods:
        start = period.start if period.HasField("start") else 0
        end = period.end if period.HasField("end") else 2_000_000_000
        if start <= now_ts <= end:
            return True
    return False


def _has_route_scope(informed_entities) -> bool:
    return any(e.route_id for e in informed_entities)


def _is_system_outage(informed_entities) -> bool:
    for e in informed_entities:
        if not e.route_id and not e.stop_id and not e.HasField("trip"):
            if e.agency_id:
                return True
    return False


def _first_text(translated_string) -> str:
    if not translated_string or not translated_string.translation:
        return ""
    return translated_string.translation[0].text or ""


def _is_planned(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(k in lower for k in PLANNED_KEYWORDS)


def _ts_to_iso(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return ""


def _parse_feed(content: bytes, agency: dict, now_ts: int) -> list[dict]:
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except Exception as e:
        log.warning("Transit %s: protobuf parse failed: %s", agency["name"], e)
        return []

    out: list[dict] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        if alert.effect != EFFECT_NO_SERVICE:
            continue
        if not _is_active(alert.active_period, now_ts):
            continue
        if not _has_route_scope(alert.informed_entity):
            continue

        header = _first_text(alert.header_text)
        description = _first_text(alert.description_text)
        if _is_planned(header) or _is_planned(description):
            continue

        route_id = ""
        for ie in alert.informed_entity:
            if ie.route_id:
                route_id = ie.route_id
                break

        start_iso = ""
        end_iso = ""
        if alert.active_period:
            p = alert.active_period[0]
            start_iso = _ts_to_iso(p.start) if p.HasField("start") else ""
            end_iso = _ts_to_iso(p.end) if p.HasField("end") else ""

        out.append({
            "agency": agency["name"],
            "agency_id": agency["id"],
            "route": route_id or "ALL",
            "system_outage": _is_system_outage(alert.informed_entity),
            "effect": "NO_SERVICE",
            "header": header,
            "description": description[:1000],
            "start": start_iso,
            "end": end_iso,
            "source": _first_text(alert.url),
        })

    return out


def _is_wmata_severe(description: str) -> bool:
    if not description:
        return False
    lower = description.lower()
    return any(k in lower for k in WMATA_SEVERE_KEYWORDS)


def _parse_wmata_incidents(content: bytes, agency: dict, now_ts: int) -> list[dict]:
    try:
        payload = json.loads(content)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("Transit %s: JSON parse failed: %s", agency["name"], e)
        return []

    incidents = payload.get("Incidents", []) or []
    out: list[dict] = []
    for inc in incidents:
        desc = inc.get("Description") or ""
        if not _is_wmata_severe(desc):
            continue
        lines_raw = (inc.get("LinesAffected") or "").rstrip(";")
        lines = [l.strip() for l in lines_raw.split(";") if l.strip()]
        route = "/".join(lines) if lines else "ALL"

        out.append({
            "agency": agency["name"],
            "agency_id": agency["id"],
            "route": route,
            "system_outage": len(lines) >= 5,
            "effect": "NO_SERVICE",
            "header": desc[:200],
            "description": desc[:1000],
            "start": inc.get("DateUpdated", ""),
            "end": "",
            "source": agency["alerts_url"],
            "incident_id": inc.get("IncidentID", ""),
            "incident_type": inc.get("IncidentType", ""),
        })
    return out


def _cta_affects_rail(alert: dict) -> tuple[bool, list[str]]:
    imp = alert.get("ImpactedService") or {}
    services = imp.get("Service") or []
    if isinstance(services, dict):
        services = [services]
    rail_lines: list[str] = []
    for s in services:
        stype = (s.get("ServiceType") or "").strip().upper()
        sname = (s.get("ServiceName") or "").strip()
        if stype in CTA_RAIL_SERVICE_TYPES or sname.lower() in CTA_RAIL_LINE_NAMES:
            rail_lines.append(sname or s.get("ServiceId") or "")
    return (len(rail_lines) > 0, rail_lines)


def _cta_is_severe(alert: dict) -> bool:
    impact = (alert.get("Impact") or "").lower()
    if "planned" in impact or "minor" in impact:
        return False
    if str(alert.get("MajorAlert", "0")) == "1":
        return True
    try:
        score = int(alert.get("SeverityScore", "0") or 0)
    except (TypeError, ValueError):
        score = 0
    if score >= CTA_SEVERE_SCORE:
        return True
    text = f"{alert.get('Headline','')} {alert.get('ShortDescription','')}".lower()
    return any(k in text for k in WMATA_SEVERE_KEYWORDS)


def _parse_cta_alerts(content: bytes, agency: dict, now_ts: int) -> list[dict]:
    try:
        payload = json.loads(content)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("Transit %s: JSON parse failed: %s", agency["name"], e)
        return []

    root = payload.get("CTAAlerts") or {}
    alerts = root.get("Alert") or []
    if isinstance(alerts, dict):
        alerts = [alerts]

    out: list[dict] = []
    for a in alerts:
        is_rail, rail_lines = _cta_affects_rail(a)
        if not is_rail:
            continue
        if not _cta_is_severe(a):
            continue
        route = "/".join(rail_lines) if rail_lines else "ALL"
        out.append({
            "agency": agency["name"],
            "agency_id": agency["id"],
            "route": route,
            "system_outage": len(rail_lines) >= 6,
            "effect": "NO_SERVICE",
            "header": (a.get("Headline") or "")[:200],
            "description": (a.get("ShortDescription") or a.get("FullDescription") or "")[:1000],
            "start": a.get("EventStart") or "",
            "end": a.get("EventEnd") or "",
            "source": a.get("AlertURL") or agency["alerts_url"],
            "alert_id": a.get("AlertId") or a.get("GUID") or "",
            "severity_score": a.get("SeverityScore") or "",
        })
    return out


_PATH_MS_DATE = re.compile(r"/Date\((\d+)\)/")


def _path_parse_ms_date(s: str) -> str:
    if not s:
        return ""
    m = _PATH_MS_DATE.search(s)
    if not m:
        return ""
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return ""


def _path_is_severe(subject: str, message: str) -> bool:
    text = f"{subject} {message}".lower()
    if any(p in text for p in PATH_PLANNED_KEYWORDS):
        return False
    return any(k in text for k in PATH_SEVERE_KEYWORDS)


def _parse_path_alerts(content: bytes, agency: dict, now_ts: int) -> list[dict]:
    try:
        payload = json.loads(content)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("Transit %s: JSON parse failed: %s", agency["name"], e)
        return []

    if not isinstance(payload, list):
        return []

    out: list[dict] = []
    seen_ids: set[str] = set()
    for msg in payload:
        subject = msg.get("Subject") or ""
        body = msg.get("SentMessage") or ""
        if not _path_is_severe(subject, body):
            continue
        mid = str(msg.get("messageid") or "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        out.append({
            "agency": agency["name"],
            "agency_id": agency["id"],
            "route": "ALL",
            "system_outage": True,
            "effect": "NO_SERVICE",
            "header": subject[:200],
            "description": body[:1000],
            "start": _path_parse_ms_date(msg.get("sentdate2", "")),
            "end": "",
            "source": agency["alerts_url"],
            "message_id": mid,
            "template": msg.get("TemplateName", ""),
        })
    return out


def _fetch_agency(agency: dict) -> list[dict]:
    auth = agency.get("auth")
    headers: dict[str, str] = {}
    if auth:
        env_var = auth.get("env")
        if not env_var:
            log.warning("Transit %s: auth.env missing, skipping", agency["name"])
            return []
        key = os.environ.get(env_var, "")
        if not key:
            log.info("Transit %s: env %s not set, skipping", agency["name"], env_var)
            return []
        headers[auth.get("header", "x-api-key")] = key

    content = _http_get(agency["alerts_url"], headers=headers)
    if not content:
        return []

    fmt = agency.get("format", "gtfs-rt")
    now_ts = int(time.time())
    if fmt == "wmata-incidents":
        return _parse_wmata_incidents(content, agency, now_ts)
    if fmt == "cta-alerts":
        return _parse_cta_alerts(content, agency, now_ts)
    if fmt == "path-alerts":
        return _parse_path_alerts(content, agency, now_ts)
    return _parse_feed(content, agency, now_ts)


def fetch_transit_by_county() -> dict[str, list[dict]]:
    agencies = load_agencies()
    log.info("Transit: fetching %d agencies", len(agencies))

    by_county: dict[str, list[dict]] = {}
    total = 0

    for agency in agencies:
        try:
            alerts = _fetch_agency(agency)
        except Exception as e:
            log.error("Transit %s: unhandled error: %s", agency["name"], e)
            continue
        if not alerts:
            continue
        log.info("Transit %s: %d severe outage(s)", agency["name"], len(alerts))
        total += len(alerts)
        for fips in agency.get("counties", []):
            by_county.setdefault(fips, []).extend(alerts)

    log.info("Transit: %d total severe alerts across %d counties", total, len(by_county))
    return by_county
