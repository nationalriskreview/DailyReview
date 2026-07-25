"""Major service-provider outages from official status pages.

Direct, structured, near-real-time — unlike the GDELT news signal, which only
catches outages that make the news within 24h. Polls each provider's official
status feed and surfaces *active* incidents as a national list.

Provider handlers (configured in reference/service_providers.json):
  - statuspage : Atlassian Statuspage `/api/v2/summary.json` (indicator +
                 unresolved incidents). Covers Cloudflare, GitHub, Datadog,
                 Zoom, OpenAI, Anthropic, Oracle OCI.
  - gcp        : Google Cloud `incidents.json` (ongoing = no end time).
  - rss        : AWS / Azure status RSS (items updated in the last 48h).
  - slack      : Slack `api/v2.0.0/current` (active_incidents).
  - unsupported: providers with no clean public feed (Salesforce, M365, X) —
                 reported as skipped so the gap is visible, not silent.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_FILE = REPO_ROOT / "reference" / "service_providers.json"
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
HTTP_TIMEOUT = 20
RSS_RECENT_HOURS = int(os.environ.get("SERVICE_RSS_RECENT_HOURS", "48"))


def _http_get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Service status fetch failed %s: %s", url, e)
        return None


def _http_get_json(url: str):
    raw = _http_get(url)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Service status non-JSON payload: %s", url)
        return None


def _incident(provider: str, key: str, *, title: str, impact: str,
              status: str = "", started: str = "", updated: str = "",
              url: str = "") -> dict:
    return {
        "provider": provider, "key": key, "title": title, "impact": impact,
        "status": status, "started": started, "updated": updated, "url": url,
        "source": "provider status page",
    }


def _check_statuspage(p: dict) -> list[dict]:
    base = p["url"].rstrip("/")
    data = _http_get_json(f"{base}/api/v2/summary.json")
    if not data:
        # Some Statuspage instances (e.g. Oracle OCI) expose status.json but not
        # summary.json — fall back to the indicator-only endpoint.
        sdata = _http_get_json(f"{base}/api/v2/status.json")
        if not sdata:
            raise RuntimeError("fetch/parse failed")
        s = sdata.get("status") or {}
        ind = s.get("indicator", "none")
        if ind not in ("none", "", None):
            return [_incident(p["name"], p["key"],
                              title=s.get("description", "") or f"{ind} degradation",
                              impact=ind, status="active", url=base)]
        return []
    indicator = (data.get("status") or {}).get("indicator", "none")
    description = (data.get("status") or {}).get("description", "")
    incidents = data.get("incidents") or []  # summary.json lists UNRESOLVED only
    out = []
    for inc in incidents:
        out.append(_incident(
            p["name"], p["key"],
            title=inc.get("name", description or "Service incident"),
            impact=inc.get("impact", indicator),
            status=inc.get("status", ""),
            started=inc.get("started_at", "") or inc.get("created_at", ""),
            updated=inc.get("updated_at", ""),
            url=inc.get("shortlink", "") or p["url"],
        ))
    # Component-level degradation with no formal incident (e.g. Cloudflare "minor").
    if not out and indicator not in ("none", "", None):
        out.append(_incident(
            p["name"], p["key"], title=description or f"{indicator} degradation",
            impact=indicator, status="active", url=p["url"],
        ))
    return out


def _check_gcp(p: dict) -> list[dict]:
    data = _http_get_json(p["url"])
    if data is None:
        raise RuntimeError("fetch/parse failed")
    out = []
    for inc in data:
        if inc.get("end"):  # resolved incidents carry an end time
            continue
        out.append(_incident(
            p["name"], p["key"],
            title=inc.get("external_desc", "Service incident"),
            impact=inc.get("severity", inc.get("status_impact", "")),
            status=inc.get("status_impact", ""),
            started=inc.get("begin", ""),
            updated=(inc.get("most_recent_update") or {}).get("modified", ""),
            url="https://status.cloud.google.com/" + inc.get("uri", "").lstrip("/"),
        ))
    return out


def _check_slack(p: dict) -> list[dict]:
    data = _http_get_json(p["url"])
    if data is None:
        raise RuntimeError("fetch/parse failed")
    out = []
    for inc in data.get("active_incidents", []) or []:
        out.append(_incident(
            p["name"], p["key"],
            title=inc.get("title", "Service incident"),
            impact=inc.get("type", ""),
            status=inc.get("status", ""),
            started=inc.get("date_created", ""),
            updated=inc.get("date_updated", ""),
            url=inc.get("url", "") or "https://status.slack.com",
        ))
    return out


def _check_rss(p: dict) -> list[dict]:
    raw = _http_get(p["url"])
    if raw is None:
        raise RuntimeError("fetch failed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise RuntimeError("unparseable RSS")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RSS_RECENT_HOURS)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        pub_raw = (item.findtext("pubDate") or "").strip()
        pub_dt = None
        if pub_raw:
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pub_dt = None
        if pub_dt and pub_dt < cutoff:
            continue  # stale
        out.append(_incident(
            p["name"], p["key"], title=title, impact="",
            started=pub_raw, url=(item.findtext("link") or "").strip(),
        ))
    return out


_HANDLERS = {
    "statuspage": _check_statuspage,
    "gcp": _check_gcp,
    "slack": _check_slack,
    "rss": _check_rss,
}


def fetch_service_outages() -> tuple[list[dict], list[dict]]:
    """Return (outages, provider_stats). outages is the flat national list of
    active incidents; provider_stats is per-provider {key, name, status, items}.
    """
    try:
        providers = json.loads(PROVIDERS_FILE.read_text())
    except Exception as e:
        log.error("Cannot load service_providers.json: %s", e)
        return [], []

    outages: list[dict] = []
    stats: list[dict] = []
    for p in providers:
        ptype = p.get("type")
        if ptype == "unsupported":
            stats.append({"key": p["key"], "name": p["name"],
                          "status": "unsupported", "reason": p.get("reason", ""), "items": 0})
            continue
        handler = _HANDLERS.get(ptype)
        if not handler:
            stats.append({"key": p["key"], "name": p["name"],
                          "status": "config_error", "items": 0})
            continue
        try:
            incidents = handler(p)
            outages.extend(incidents)
            stats.append({"key": p["key"], "name": p["name"],
                          "status": "ok", "items": len(incidents)})
        except Exception as e:
            log.warning("Service status %s failed: %s", p["name"], e)
            stats.append({"key": p["key"], "name": p["name"],
                          "status": "fetch_failed", "items": 0})

    log.info("Service status: %d active incident(s) across %d provider(s)",
             len(outages), sum(1 for s in stats if s["status"] == "ok"))
    return outages, stats
