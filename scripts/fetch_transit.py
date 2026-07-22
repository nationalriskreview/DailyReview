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
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
HTTP_TIMEOUT = 30
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)

EFFECT_NO_SERVICE = 1
GTFS_RT_MAX_AGE_DAYS = int(os.environ.get("GTFS_RT_MAX_AGE_DAYS", "30"))

PLANNED_KEYWORDS = (
    "weekend", "this sunday", "this saturday",
    "scheduled maintenance", "planned maintenance", "track work",
    "construction",
    "advance notice", "future change", "upcoming",
    "infrastructure upgrade", "infrastructure improvement",
    "will not stop at", "trains will not stop",
    "remain closed", "remains closed",
    "service changes", "service change",
    "modified service", "modified schedule",
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

# MTA tags every alert as UNKNOWN_EFFECT in GTFS-RT, so the generic
# effect==NO_SERVICE filter rejects them all. Apply a text-based severity
# heuristic instead, matching the pattern used for WMATA/CTA/PATH.
MTA_SEVERE_KEYWORDS = (
    "strike",
    "service is suspended",
    "service has been suspended",
    "all service suspended",
    "no service",
    "system-wide", "systemwide",
    "shutdown", "shut down",
    "evacuat",
)
MTA_PLANNED_KEYWORDS = (
    "late night", "late nights", "overnight", "overnights",
    "this weekend", "this saturday", "this sunday",
    "modified schedule", "modified service",
    "track work", "construction",
    "renovat", "infrastructure improvement",
    "facility upgrade", "facility upgrades",
    "bridge work",
    "buses replace trains",
    "temporary platform",
    "scheduled",
)
MTA_CONDITIONAL_KEYWORDS = (
    "possible strike", "possible suspension", "possible shutdown",
    "potential strike", "potential suspension",
    "may be suspended", "may be impacted", "may be delayed",
    "could be suspended", "could be impacted",
    "as early as",
    "is expected to",
    "if some", "if the unions",
    "no expected impact",
)

# NJ Transit publishes a human-readable RSS of rail advisories alongside its
# GTFS-RT feed. The RSS is dominated by elevator/escalator outages and
# planned weekend track work; we want only the rare severe events.
NJT_RSS_SEVERE_KEYWORDS = (
    "strike",
    "suspended",
    "no service",
    "no rail service",
    "service halted",
    "system-wide", "systemwide",
    "shutdown", "shut down",
    "derail", "derailed", "derailment",
    "evacuat",
)
NJT_RSS_PLANNED_KEYWORDS = (
    "elevator", "escalator",
    "staircase", "stairs", "stairwell",
    "platform repair", "platform closure", "platform closed",
    "track work",
    "construction", "renovat",
    "bus bridge",
    "schedule changes", "schedule change",
    "boarding changes",
    "potential delays", "possible delays",
    "waiting area", "waiting room",
    "ticket booth",
    "ada improvement",
    "station enhancement",
    "infrastructure improvement",
    "long-term", "long term",
    "weekend",
    "facility upgrade",
    "station house",
    "ticket vending",
    "scheduled",
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


def _is_stale_permanent(active_periods, now_ts: int) -> bool:
    """Alert started >N days ago with no end date — treat as permanent fixture, not a severe outage."""
    if not active_periods:
        return False
    p = active_periods[0]
    start = p.start if p.HasField("start") else 0
    has_end = p.HasField("end") and p.end > 0
    if has_end:
        return False
    if start <= 0:
        return False
    return (now_ts - start) > GTFS_RT_MAX_AGE_DAYS * 86400


def _has_route_scope(informed_entities) -> bool:
    """True when at least one entity is route-level (route_id set, no
    stop_id, no trip). Entities with both route_id and stop_id are
    stop-level — those are tracked stops on a route, not route-wide
    outages, and shouldn't trip the severe-outage filter (MBTA emits
    a lot of single-stop construction closures this way)."""
    for e in informed_entities:
        if e.route_id and not e.stop_id and not e.HasField("trip"):
            return True
    return False


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
        if _is_stale_permanent(alert.active_period, now_ts):
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


def _njt_rss_is_severe(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    if any(k in text for k in NJT_RSS_PLANNED_KEYWORDS):
        return False
    return any(k in text for k in NJT_RSS_SEVERE_KEYWORDS)


def _parse_njt_rss_date(s: str) -> datetime | None:
    if not s:
        return None
    # Per-item pubDate format: "May 16, 2026 01:00:24 PM" (no timezone; NJT is ET).
    try:
        from datetime import timezone as _tz, timedelta as _td
        et = _tz(_td(hours=-4))  # ET is UTC-4 or -5; -4 covers DST. Close enough for staleness checks.
        return datetime.strptime(s.strip(), "%b %d, %Y %I:%M:%S %p").replace(tzinfo=et)
    except ValueError:
        pass
    # Fall back: RFC 822 channel-level format.
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _parse_njt_rss_advisories(content: bytes, agency: dict, now_ts: int) -> list[dict]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.warning("Transit %s: RSS parse failed: %s", agency["name"], e)
        return []

    now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    max_age = timedelta(days=GTFS_RT_MAX_AGE_DAYS)
    out: list[dict] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()

        if not _njt_rss_is_severe(title, desc):
            continue

        pub_dt = _parse_njt_rss_date(pub_raw)
        if pub_dt and (now - pub_dt) > max_age:
            continue

        out.append({
            "agency": agency["name"],
            "agency_id": agency["id"],
            "route": "ALL",
            "system_outage": True,
            "effect": "NO_SERVICE",
            "header": desc[:200] if desc else title[:200],
            "description": desc[:1000],
            "start": pub_dt.isoformat() if pub_dt else "",
            "end": "",
            "source": link,
        })
    return out


def _mta_is_severe(header: str, description: str) -> bool:
    text = f"{header} {description}".lower()
    if any(k in text for k in MTA_CONDITIONAL_KEYWORDS):
        return False
    if any(k in text for k in MTA_PLANNED_KEYWORDS):
        return False
    return any(k in text for k in MTA_SEVERE_KEYWORDS)


def _parse_mta_alerts(content: bytes, agency: dict, now_ts: int) -> list[dict]:
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
        if not _is_active(alert.active_period, now_ts):
            continue
        if _is_stale_permanent(alert.active_period, now_ts):
            continue
        if not _has_route_scope(alert.informed_entity):
            continue

        header = _first_text(alert.header_text)
        description = _first_text(alert.description_text)
        if not _mta_is_severe(header, description):
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

        route_count = sum(1 for ie in alert.informed_entity if ie.route_id)
        text = f"{header} {description}".lower()
        system_outage = (
            "strike" in text
            or "all service" in text
            or "system-wide" in text
            or "systemwide" in text
            or route_count >= 6
        )

        out.append({
            "agency": agency["name"],
            "agency_id": agency["id"],
            "route": route_id or "ALL",
            "system_outage": system_outage,
            "effect": "NO_SERVICE",
            "header": header,
            "description": description[:1000],
            "start": start_iso,
            "end": end_iso,
            "source": _first_text(alert.url),
        })

    return out


def _fetch_agency(agency: dict) -> tuple[list[dict], str]:
    """Returns (alerts, status). Status is one of:
    'ok', 'skipped_no_auth', 'config_error', 'fetch_failed'."""
    auth = agency.get("auth")
    headers: dict[str, str] = {}
    url = agency["alerts_url"]
    if auth:
        env_var = auth.get("env")
        if not env_var:
            log.warning("Transit %s: auth.env missing, skipping", agency["name"])
            return [], "config_error"
        key = os.environ.get(env_var, "")
        if not key:
            log.info("Transit %s: env %s not set, skipping", agency["name"], env_var)
            return [], "skipped_no_auth"
        style = auth.get("style", "header")
        if style == "query":
            param = auth.get("param", "api_key")
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{param}={key}"
        else:
            prefix = auth.get("prefix", "")
            headers[auth.get("header", "x-api-key")] = f"{prefix}{key}"

    content = _http_get(url, headers=headers)
    if not content:
        return [], "fetch_failed"

    fmt = agency.get("format", "gtfs-rt")
    now_ts = int(time.time())
    if fmt == "wmata-incidents":
        return _parse_wmata_incidents(content, agency, now_ts), "ok"
    if fmt == "cta-alerts":
        return _parse_cta_alerts(content, agency, now_ts), "ok"
    if fmt == "path-alerts":
        return _parse_path_alerts(content, agency, now_ts), "ok"
    if fmt == "mta-alerts":
        return _parse_mta_alerts(content, agency, now_ts), "ok"
    if fmt == "njt-rss-advisories":
        return _parse_njt_rss_advisories(content, agency, now_ts), "ok"
    return _parse_feed(content, agency, now_ts), "ok"


def fetch_transit_by_county() -> tuple[dict[str, list[dict]], list[dict]]:
    """Returns (alerts_by_fips, per_agency_stats).

    per_agency_stats is a list of {id, name, status, items} suitable for
    inclusion in the run's data_sources health summary.
    """
    agencies = load_agencies()
    log.info("Transit: fetching %d agencies", len(agencies))

    by_county: dict[str, list[dict]] = {}
    total = 0
    stats: list[dict] = []

    for agency in agencies:
        agency_id = agency["id"]
        agency_name = agency["name"]
        try:
            alerts, status = _fetch_agency(agency)
        except Exception as e:
            log.error("Transit %s: unhandled error: %s", agency_name, e)
            stats.append({
                "id": agency_id, "name": agency_name,
                "status": "error", "items": 0, "error": str(e)[:200],
            })
            continue
        stats.append({
            "id": agency_id, "name": agency_name,
            "status": status, "items": len(alerts),
        })
        if not alerts:
            continue
        log.info("Transit %s: %d severe outage(s)", agency_name, len(alerts))
        total += len(alerts)
        for fips in agency.get("counties", []):
            by_county.setdefault(fips, []).extend(alerts)

    log.info("Transit: %d total severe alerts across %d counties", total, len(by_county))
    return by_county, stats
