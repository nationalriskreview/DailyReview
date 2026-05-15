"""FEMA OpenFEMA — Disaster Declarations Summaries.

Federal Emergency Management Agency declarations, county-level. Three types:
  - DR  Major Disaster   (post-event federal recognition)
  - EM  Emergency        (pre/during-event federal mobilization)
  - FM  Fire Management  (wildfire-specific federal funding)

All three are operational signals: a federal declaration means local resources
are overwhelmed and the incident has been formally recognized.

API: https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries
No key required. OData query syntax.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

FEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
LOOKBACK_DAYS = int(os.environ.get("FEMA_LOOKBACK_DAYS", "30"))
HTTP_TIMEOUT = 30


def _http_get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("FEMA fetch %s -> HTTP %d", url, resp.status)
                return None
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("FEMA HTTPError %d: %s", e.code, url)
        return None
    except Exception as e:
        log.warning("FEMA fetch %s: %s", url, e)
        return None


def fetch_fema_by_county() -> dict[str, list[dict]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT00:00:00.000z"
    )
    params = {
        "$filter": f"declarationDate ge '{cutoff}'",
        "$top": "1000",
        "$orderby": "declarationDate desc",
    }
    url = f"{FEMA_URL}?{urllib.parse.urlencode(params)}"
    log.info("FEMA: querying declarations since %s", cutoff)
    payload = _http_get_json(url)
    if not payload:
        return {}

    declarations = payload.get("DisasterDeclarationsSummaries", []) or []
    log.info("FEMA: %d records returned", len(declarations))

    now = datetime.now(timezone.utc)
    by_county: dict[str, list[dict]] = {}
    seen: dict[str, set[str]] = {}

    for d in declarations:
        state_fips = (d.get("fipsStateCode") or "").zfill(2)
        county_fips = (d.get("fipsCountyCode") or "").zfill(3)
        if not state_fips or county_fips == "000":
            continue
        fips = f"{state_fips}{county_fips}"

        disaster_num = str(d.get("disasterNumber") or "")
        decl_type = d.get("declarationType") or ""
        dedupe_key = f"{decl_type}-{disaster_num}"
        seen.setdefault(fips, set())
        if dedupe_key in seen[fips]:
            continue
        seen[fips].add(dedupe_key)

        declared_at_str = d.get("declarationDate", "")
        is_new = False
        if declared_at_str:
            try:
                # OpenFEMA dates are 2026-05-15T...
                dt = datetime.fromisoformat(declared_at_str.replace("z", "+00:00").replace("Z", "+00:00"))
                is_new = (now - dt) < timedelta(hours=24)
            except ValueError:
                pass

        record = {
            "disaster_number": disaster_num,
            "declaration_type": decl_type,
            "declaration_title": d.get("declarationTitle", ""),
            "incident_type": d.get("incidentType", ""),
            "declared_at": declared_at_str,
            "is_new_today": is_new,
            "incident_begin": d.get("incidentBeginDate", ""),
            "incident_end": d.get("incidentEndDate"),
            "designated_area": d.get("designatedArea", ""),
            "fema_declaration_string": d.get("femaDeclarationString", ""),
            "source": f"https://www.fema.gov/disaster/{disaster_num}" if disaster_num else "",
        }
        by_county.setdefault(fips, []).append(record)

    log.info("FEMA: %d counties have declarations in last %d days",
             len(by_county), LOOKBACK_DAYS)
    return by_county
