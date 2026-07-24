"""Assemble per-county / per-state / NYC / national JSON outputs."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SCHEMA_VERSION = "1.7"

OUTPUT_NOTES = {
    "transit": (
        "Transit alerts cover severe outages only (GTFS-RT effect=NO_SERVICE "
        "or, for MTA/NJT, a text-based severity heuristic — strike, "
        "system-wide suspension, derailment, etc.), active now, route-level "
        "scope, non-planned. Coverage spans the ~10 major US transit agencies "
        "configured in reference/transit_agencies.json (MTA Subway / LIRR / "
        "Metro-North, MBTA, NJ Transit Rail, PATH, WMATA, BART, CTA, Metra)."
    ),
    "transit_disruption": (
        "GDELT-news transit disruption signal — articles reporting major "
        "transit disruptions (strikes, derailments, system shutdowns, mass "
        "cancellations) in the past 24h. Strict title filter (disruption verb "
        "+ transit noun) plus LLM precision pass. Complements `transit` "
        "(GTFS-RT) by surfacing events that haven't yet been encoded in "
        "structured agency feeds."
    ),
    "amtrak": (
        "Amtrak service-stoppage and full-station-closure advisories scraped "
        "from amtrak.com/service-alerts-and-notices. Severity-filtered: only "
        "suspensions, cancellations, derailments, strikes, full route "
        "shutdowns, and full station closures are surfaced. Routine schedule "
        "adjustments, accessibility/equipment outages, baggage and waiting-"
        "room issues are dropped. County fan-out via Amtrak's static GTFS "
        "(route stops → nearest county centroid; station code → county). "
        "Each entry has a `kind` field of `service_stoppage` or "
        "`station_closure`. Same list is exposed at `national.amtrak_advisories`."
    ),
    "aviation": (
        "FAA major-airport closures and ground stops from "
        "nasstatus.faa.gov/api/airport-status-information. Allowlisted to "
        "FAA Large Hubs (~30 commercial airports, configured in "
        "reference/airports.json). Closure NOTAMs that restrict only "
        "general-aviation or non-scheduled traffic are filtered out — only "
        "airport-wide closures affecting commercial service are surfaced. "
        "Routine Ground Delay Programs and arrival/departure delays are "
        "skipped entirely. Each entry fans out to the airport's metro "
        "service-area counties — e.g. a JFK closure tags Manhattan even "
        "though JFK is in Queens. `kind` is `airport_closure` or "
        "`ground_stop`. Same list is exposed at `national.faa_advisories`."
    ),
}

DATA_WINDOWS = {
    "weather": "live",
    "bank_robbery": "24h",
    "protest": "24h",
    "utility_outage": "24h",
    "transit_disruption": "24h",
    "wildfires": "active (EONET open events, last 14d)",
    "transit": "live",
    "amtrak": "active (effective today per Amtrak page)",
    "aviation": "live (FAA NAS status)",
    "fema": "30d",
    "disease": "14d (wastewater)",
    "conditions.forecast": "next 24h (NWS gridpoint)",
    "conditions.air_quality": "current + next 24h peak (Open-Meteo US AQI)",
    "national.cdc_han": "7d",
    "national.cdc_outbreaks": "active (CDC US-based outbreaks, workplace-relevant)",
    "national.notifiable_disease_alerts": "current MMWR week (CDC NNDSS elevation)",
    "national.amtrak_advisories": "active (effective today)",
    "national.faa_advisories": "live",
}


def _county_record(
    county: dict,
    weather: list[dict],
    forecast: list[dict],
    gdelt: dict[str, list[dict]] | None,
    wildfires: list[dict] | None,
    transit: list[dict] | None,
    amtrak: list[dict] | None,
    aviation: list[dict] | None,
    fema: list[dict] | None,
    disease: list[dict] | None,
    forecast_conditions: dict | None,
    air_quality: dict | None,
) -> dict:
    alerts = {
        "weather": (weather or []) + (forecast or []),
        "bank_robbery": (gdelt or {}).get("bank_robbery", []),
        "protest": (gdelt or {}).get("protest", []),
        "utility_outage": (gdelt or {}).get("utility_outage", []),
        "transit_disruption": (gdelt or {}).get("transit_disruption", []),
        "wildfires": wildfires or [],
        "transit": transit or [],
        "amtrak": amtrak or [],
        "aviation": aviation or [],
        "fema": fema or [],
        "disease": disease or [],
    }
    alert_count = sum(len(v) for v in alerts.values())
    # Ambient conditions — reported for every county regardless of alert state.
    conditions = {
        "forecast": forecast_conditions,
        "air_quality": air_quality,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "fips": county["fips"],
        "name": county["name"],
        "state": county["state"],
        "centroid": {"lat": county["lat"], "lon": county["lon"]},
        "alerts": alerts,
        "alert_count": alert_count,
        "conditions": conditions,
    }


def _reset_dir(path: Path) -> None:
    if path.exists():
        for entry in path.iterdir():
            if entry.name == ".gitkeep":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        path.mkdir(parents=True)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def write_all(
    counties: list[dict],
    boroughs: list[dict],
    weather_by_fips: dict[str, list[dict]],
    forecast_by_fips: dict[str, list[dict]],
    forecast_conditions_by_fips: dict[str, dict],
    air_quality_by_fips: dict[str, dict],
    gdelt_by_fips: dict[str, dict[str, list[dict]]],
    wildfires_by_fips: dict[str, list[dict]],
    transit_by_fips: dict[str, list[dict]],
    amtrak_by_fips: dict[str, list[dict]],
    faa_by_fips: dict[str, list[dict]],
    fema_by_fips: dict[str, list[dict]],
    disease_by_fips: dict[str, list[dict]],
    national: dict[str, list[dict]],
    data_sources: dict[str, dict] | None = None,
) -> None:
    data_sources = data_sources or {}
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    records: list[dict] = []
    by_fips: dict[str, dict] = {}
    for c in counties:
        rec = _county_record(
            c,
            weather_by_fips.get(c["fips"], []),
            forecast_by_fips.get(c["fips"], []),
            gdelt_by_fips.get(c["fips"]),
            wildfires_by_fips.get(c["fips"], []),
            transit_by_fips.get(c["fips"], []),
            amtrak_by_fips.get(c["fips"], []),
            faa_by_fips.get(c["fips"], []),
            fema_by_fips.get(c["fips"], []),
            disease_by_fips.get(c["fips"], []),
            forecast_conditions_by_fips.get(c["fips"]),
            air_quality_by_fips.get(c["fips"]),
        )
        rec["date"] = today
        rec["generated_at"] = now.isoformat()
        records.append(rec)
        by_fips[c["fips"]] = rec

    flagged = [r for r in records if r["alert_count"] > 0]

    full = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "date": today,
        "data_windows": DATA_WINDOWS,
        "notes": OUTPUT_NOTES,
        "data_sources": data_sources,
        "counties_total": len(records),
        "counties_with_alerts": len(flagged),
        "national": national,
        "counties": {
            r["fips"]: _strip_for_full(r, include_conditions=True) for r in records
        },
    }
    _write_json(DATA_DIR / "today.json", full)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "date": today,
        "data_windows": DATA_WINDOWS,
        "notes": OUTPUT_NOTES,
        "data_sources": data_sources,
        "counties_total": len(records),
        "counties_with_alerts": len(flagged),
        "national": national,
        "counties": {r["fips"]: _strip_for_full(r) for r in flagged},
    }
    _write_json(DATA_DIR / "today-summary.json", summary)

    _write_json(DATA_DIR / "national.json", {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "date": today,
        "data_windows": {
            "national.cdc_han": DATA_WINDOWS["national.cdc_han"],
            "national.cdc_outbreaks": DATA_WINDOWS["national.cdc_outbreaks"],
            "national.notifiable_disease_alerts": DATA_WINDOWS["national.notifiable_disease_alerts"],
            "national.amtrak_advisories": DATA_WINDOWS["national.amtrak_advisories"],
            "national.faa_advisories": DATA_WINDOWS["national.faa_advisories"],
        },
        "data_sources": data_sources,
        **national,
    })

    counties_dir = DATA_DIR / "counties"
    _reset_dir(counties_dir)
    for r in records:
        _write_json(counties_dir / f"{r['fips']}.json", r)

    states_dir = DATA_DIR / "states"
    _reset_dir(states_dir)
    by_state: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_state[r["state"]].append(r)
    for state, rs in by_state.items():
        flagged_in_state = [x for x in rs if x["alert_count"] > 0]
        _write_json(states_dir / f"{state}.json", {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now.isoformat(),
            "date": today,
            "state": state,
            "counties_total": len(rs),
            "counties_with_alerts": len(flagged_in_state),
            "counties": {x["fips"]: _strip_for_full(x) for x in rs},
        })

    nyc_dir = DATA_DIR / "nyc"
    _reset_dir(nyc_dir)
    borough_records: list[dict] = []
    for b in boroughs:
        county_rec = by_fips.get(b["fips"])
        if not county_rec:
            continue
        b_rec = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now.isoformat(),
            "date": today,
            "borough": b["borough"],
            "slug": b["slug"],
            "fips": b["fips"],
            "county_name": b["county_name"],
            "alerts": county_rec["alerts"],
            "alert_count": county_rec["alert_count"],
            "conditions": county_rec.get("conditions"),
            "note": f"Includes events tagged '{b['borough']}' or '{b['county_name']}'",
        }
        _write_json(nyc_dir / f"{b['slug']}.json", b_rec)
        borough_records.append(b_rec)
    _write_json(nyc_dir / "index.json", {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "date": today,
        "boroughs": borough_records,
    })

    if flagged:
        archive_path = DATA_DIR / "archive" / f"{today}.json"
        _write_json(archive_path, summary)


def _strip_for_full(rec: dict, include_conditions: bool = False) -> dict:
    out = {
        "fips": rec["fips"],
        "name": rec["name"],
        "state": rec["state"],
        "alerts": rec["alerts"],
        "alert_count": rec["alert_count"],
    }
    if include_conditions:
        out["conditions"] = rec.get("conditions")
    return out
