"""Major service-provider outages from official status pages.

Direct, structured, near-real-time — unlike the GDELT news signal, which only
catches outages that make the news. Polls each provider's official status feed
and surfaces *active* incidents as a national list.

Recency: we only surface maintenance/alerts from the last SERVICE_RECENT_HOURS
(24h). Every timestamped handler (statuspage incidents, gcp, slack, rss, atom)
drops any incident whose start AND last update are older than the window, and
an item that can't be dated is treated as NOT recent (dropped). Handlers that
read a live current-state signal with no per-incident timestamp — a component
reading "degraded"/"unavailable" right now (statuspage indicator fallback,
html_alt_table, statuscast, sorryapp) — reflect the present moment and are
kept as-is.

Provider handlers (public list in reference/service_providers.json; a private
allowlist in the PRIVATE_PROVIDERS_JSON secret is polled the same way but
pseudonymized by 4-char code):
  - statuspage : Atlassian Statuspage `/api/v2/summary.json` (+ status.json
                 fallback) — indicator + unresolved incidents.
  - gcp        : Google Cloud `incidents.json` (ongoing = no end time).
  - rss        : AWS / Azure status RSS (pubDate within 24h).
  - slack      : Slack `api/v2.0.0/current` (active_incidents).
  - html_alt_table : service table with status-icon alt text (e.g. FedLine).
  - statuscast : StatusCast SSR summary cards.
  - atom_feed  : Atom status feed; non-resolution entry updated within 24h.
  - sorryapp   : SorryApp `/api/v1/components` (non-operational components).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_FILE = REPO_ROOT / "reference" / "service_providers.json"
PRIVATE_PROVIDERS_ENV = "PRIVATE_PROVIDERS_JSON"
# Browser-like UA — several status pages (e.g. SorryApp) 406 a non-browser agent.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 DailyReview/1.0")
HTTP_TIMEOUT = 20
# Recency window for all timestamped incidents/alerts. Anything whose start or
# last update is older than this is dropped — we only surface maintenance/alerts
# from the last 24 hours. (Live current-state signals with no timestamp — a
# component reading "degraded" right now — reflect the present moment and are
# kept regardless.)
SERVICE_RECENT_HOURS = int(os.environ.get("SERVICE_RECENT_HOURS", "24"))

# Set True while polling private providers so their URLs never reach the
# (public) Actions logs. Only counts/status types are logged when redacting.
_REDACT_URLS = False


def _http_get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as e:
        if _REDACT_URLS:
            log.warning("Service status fetch failed (private): %s", type(e).__name__)
        else:
            log.warning("Service status fetch failed %s: %s", url, e)
        return None


def _parse_iso(s: str):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_any(s: str):
    """Parse an ISO-8601 or RFC-822 (RSS) timestamp; None if unparseable."""
    if not s:
        return None
    dt = _parse_iso(s)
    if dt is not None:
        return dt
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _is_recent(*stamps: str) -> bool:
    """True if ANY provided timestamp is within the last SERVICE_RECENT_HOURS.

    Used to keep only recent maintenance/alerts. A blank/unparseable set of
    stamps returns False — callers that represent live current-state signals
    (no timestamp) must not route through here; they are kept explicitly.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SERVICE_RECENT_HOURS)
    for s in stamps:
        dt = _parse_any(s)
        if dt is not None and dt >= cutoff:
            return True
    return False


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
        started = inc.get("started_at", "") or inc.get("created_at", "")
        updated = inc.get("updated_at", "")
        if not _is_recent(started, updated):  # only last-24h maintenance/alerts
            continue
        out.append(_incident(
            p["name"], p["key"],
            title=inc.get("name", description or "Service incident"),
            impact=inc.get("impact", indicator),
            status=inc.get("status", ""),
            started=started,
            updated=updated,
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
        began = inc.get("begin", "")
        modified = (inc.get("most_recent_update") or {}).get("modified", "")
        if not _is_recent(began, modified):  # only last-24h activity
            continue
        out.append(_incident(
            p["name"], p["key"],
            title=inc.get("external_desc", "Service incident"),
            impact=inc.get("severity", inc.get("status_impact", "")),
            status=inc.get("status_impact", ""),
            started=began,
            updated=modified,
            url="https://status.cloud.google.com/" + inc.get("uri", "").lstrip("/"),
        ))
    return out


def _check_slack(p: dict) -> list[dict]:
    data = _http_get_json(p["url"])
    if data is None:
        raise RuntimeError("fetch/parse failed")
    out = []
    for inc in data.get("active_incidents", []) or []:
        created = inc.get("date_created", "")
        updated = inc.get("date_updated", "")
        if not _is_recent(created, updated):  # only last-24h activity
            continue
        out.append(_incident(
            p["name"], p["key"],
            title=inc.get("title", "Service incident"),
            impact=inc.get("type", ""),
            status=inc.get("status", ""),
            started=created,
            updated=updated,
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
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        pub_raw = (item.findtext("pubDate") or "").strip()
        # Precise: require a parseable pubDate within the last 24h. An item we
        # cannot date is NOT assumed recent — it's dropped.
        if not _is_recent(pub_raw):
            continue
        out.append(_incident(
            p["name"], p["key"], title=title, impact="",
            started=pub_raw, url=(item.findtext("link") or "").strip(),
        ))
    return out


_ALT_NORMAL = {"normal operations", "operational", "normal", "available", "up", "ok"}
# A table row whose service-name cell is itself a status label is the legend/key.
_ALT_LEGEND = _ALT_NORMAL | {
    "service issue/industry alert", "service disruption", "service issue",
    "degraded", "unavailable", "maintenance", "status", "services", "service",
}
_ALT_SEV = {
    "service disruption": "major", "service issue/industry alert": "minor",
    "service issue": "minor", "delay": "minor", "degraded": "minor",
    "unavailable": "major", "maintenance": "maintenance",
}


def _check_html_alt_table(p: dict) -> list[dict]:
    """Status shown as a table of [service name | status-icon], where the icon's
    `alt`/`title` is the status text. Rows whose name is itself a status label
    are the legend and are skipped."""
    raw = _http_get(p["url"])
    if raw is None:
        raise RuntimeError("fetch failed")
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        name = cells[0].get_text(strip=True)
        if not name or name.strip().lower() in _ALT_LEGEND:
            continue
        status = next((img.get("alt", "") or img.get("title", "")
                       for img in tr.find_all("img")
                       if (img.get("alt") or img.get("title"))), "")
        if not status or status.strip().lower() in _ALT_NORMAL:
            continue
        out.append(_incident(p["name"], p["key"], title=f"{name}: {status}",
                             impact=_ALT_SEV.get(status.strip().lower(), "minor"),
                             status="active", url=p["url"]))
    return out


def _check_statuscast(p: dict) -> list[dict]:
    """StatusCast page: the authoritative summary lives in `currentstatus-card-*`
    count cards (hidden per-component badges over-count, so we read the cards)."""
    raw = _http_get(p["url"])
    if raw is None:
        raise RuntimeError("fetch failed")
    soup = BeautifulSoup(raw, "html.parser")
    counts = {}
    for state in ("degraded", "unavailable", "maintenance"):
        card = soup.find(class_=f"currentstatus-card-{state}")
        if card:
            nums = re.findall(r"\d+", card.get_text(" ", strip=True))
            counts[state] = int(nums[0]) if nums else 0
    if sum(counts.values()) == 0:
        return []
    impact = "major" if counts.get("unavailable") else "minor"
    desc = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    return [_incident(p["name"], p["key"], title=f"{desc} component(s)",
                      impact=impact, status="active", url=p["url"])]


def _check_atom_feed(p: dict) -> list[dict]:
    """Atom feed of status updates; an entry within the window whose title does
    NOT start with a resolution word is treated as an active incident."""
    raw = _http_get(p["url"])
    if raw is None:
        raise RuntimeError("fetch failed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise RuntimeError("unparseable feed")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall(".//a:entry", ns):
        title = (e.findtext("a:title", namespaces=ns) or "").strip()
        if not title:
            continue
        upd = (e.findtext("a:updated", namespaces=ns) or "").strip()
        pub = (e.findtext("a:published", namespaces=ns) or "").strip()
        # Precise: require a parseable timestamp within the last 24h.
        if not _is_recent(upd, pub):
            continue
        if title.lower().startswith(("resolved", "completed", "closed")):
            continue
        link = e.find("a:link", ns)
        out.append(_incident(p["name"], p["key"], title=title, impact="minor",
                             status="active", started=upd,
                             url=(link.get("href") if link is not None else "") or p["url"]))
    return out


def _check_sorryapp(p: dict) -> list[dict]:
    """SorryApp status page — poll its JSON components API; flag any component
    whose state is not operational."""
    data = _http_get_json(f"{p['url'].rstrip('/')}/api/v1/components")
    if not data:
        raise RuntimeError("fetch/parse failed")
    out = []
    for comp in data.get("components", []):
        state = (comp.get("state") or "").lower()
        if not state or state == "operational":
            continue
        impact = "major" if any(k in state for k in ("major", "outage", "down")) else (
            "maintenance" if "mainten" in state else "minor")
        out.append(_incident(p["name"], p["key"],
                             title=f"{comp.get('name', 'Component')}: {state.replace('_', ' ')}",
                             impact=impact, status="active", url=p["url"]))
    return out


_HANDLERS = {
    "statuspage": _check_statuspage,
    "gcp": _check_gcp,
    "slack": _check_slack,
    "rss": _check_rss,
    "html_alt_table": _check_html_alt_table,
    "statuscast": _check_statuscast,
    "atom_feed": _check_atom_feed,
    "sorryapp": _check_sorryapp,
}


_IMPACT_RANK = {"critical": 4, "major": 3, "minor": 2, "maintenance": 1, "none": 0, "": 0}


def _worst_impact(incidents: list[dict]) -> str:
    return max((i.get("impact", "") for i in incidents),
               key=lambda x: _IMPACT_RANK.get(x, 0), default="minor")


def _poll_providers(providers: list[dict], coded: bool = False) -> tuple[list[dict], list[dict]]:
    """Poll a list of providers; return (outages, per-provider stats).

    When `coded`, each provider carries a random `code`; its output is
    pseudonymized — the incident collapses to `{provider: <code>, impact,
    active_incidents}` with the real name, title, service names, and URL all
    stripped, and logs never include names/URLs. This lets coded providers ride
    in the same public feed without revealing their identity.
    """
    global _REDACT_URLS
    _REDACT_URLS = coded
    label = "coded service status" if coded else "service status"
    outages: list[dict] = []
    stats: list[dict] = []
    try:
        for p in providers:
            display = p.get("code") if coded else p["name"]
            skey = p.get("code") if coded else p["key"]
            ptype = p.get("type")
            if ptype == "unsupported":
                entry = {"key": skey, "name": display, "status": "unsupported", "items": 0}
                if not coded:
                    entry["reason"] = p.get("reason", "")
                stats.append(entry)
                continue
            handler = _HANDLERS.get(ptype)
            if not handler:
                stats.append({"key": skey, "name": display, "status": "config_error", "items": 0})
                continue
            try:
                # For coded providers the handler's per-incident name/key are
                # discarded (we summarize), but give it code-based values so it
                # never dereferences a missing "key"/"name".
                hp = {**p, "key": p["code"], "name": p["code"]} if coded else p
                incidents = handler(hp)
                if coded:
                    if incidents:
                        outages.append({
                            "provider": p["code"], "impact": _worst_impact(incidents),
                            "active_incidents": len(incidents), "status": "active",
                            "coded": True, "source": "provider status page (coded)",
                        })
                else:
                    outages.extend(incidents)
                stats.append({"key": skey, "name": display, "status": "ok", "items": len(incidents)})
            except Exception as e:
                if coded:
                    log.warning("%s: a provider failed: %s", label, type(e).__name__)
                else:
                    log.warning("Service status %s failed: %s", p["name"], e)
                stats.append({"key": skey, "name": display, "status": "fetch_failed", "items": 0})
    finally:
        _REDACT_URLS = False
    log.info("%s: %d active incident(s) across %d provider(s)",
             label, len(outages), sum(1 for s in stats if s["status"] == "ok"))
    return outages, stats


def fetch_service_outages() -> tuple[list[dict], list[dict]]:
    """Public providers from reference/service_providers.json."""
    try:
        providers = json.loads(PROVIDERS_FILE.read_text())
    except Exception as e:
        log.error("Cannot load service_providers.json: %s", e)
        return [], []
    return _poll_providers(providers, coded=False)


def fetch_coded_service_outages() -> tuple[list[dict], list[dict]]:
    """Coded (pseudonymous) providers from the PRIVATE_PROVIDERS_JSON secret.

    Each entry must carry a random `code`; output is pseudonymized so real
    identities never reach the public feed or the (public) logs. Returns
    ([], []) when the secret is unset — safe no-op. Entries lacking a `code`
    are skipped (they would otherwise leak a real name into the public feed).
    """
    raw = os.environ.get(PRIVATE_PROVIDERS_ENV, "").strip()
    if not raw:
        return [], []
    try:
        providers = json.loads(raw)
    except json.JSONDecodeError:
        log.error("PRIVATE_PROVIDERS_JSON is not valid JSON; skipping coded providers")
        return [], []
    if not isinstance(providers, list):
        log.error("PRIVATE_PROVIDERS_JSON must be a JSON array; skipping")
        return [], []
    coded = [p for p in providers if p.get("code")]
    dropped = len(providers) - len(coded)
    if dropped:
        log.warning("Coded service status: %d entr(ies) missing 'code' were skipped", dropped)
    log.info("Coded service status: polling %d configured provider(s)", len(coded))
    return _poll_providers(coded, coded=True)
