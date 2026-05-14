"""Assemble per-county / per-state / NYC / national JSON outputs."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SCHEMA_VERSION = "1.2"

OUTPUT_NOTES = {
    "transit": (
        "Transit alerts cover severe outages only (GTFS-RT effect=NO_SERVICE, "
        "active now, route-level scope, non-planned). Coverage is limited to "
        "the ~8 major US transit agencies configured in reference/transit_agencies.json."
    ),
}


def _county_record(
    county: dict,
    weather: list[dict],
    forecast: list[dict],
    gdelt: dict[str, list[dict]] | None,
    wildfires: list[dict] | None,
    transit: list[dict] | None,
) -> dict:
    alerts = {
        "weather": (weather or []) + (forecast or []),
        "bank_robbery": (gdelt or {}).get("bank_robbery", []),
        "protest": (gdelt or {}).get("protest", []),
        "wildfires": wildfires or [],
        "transit": transit or [],
    }
    alert_count = sum(len(v) for v in alerts.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "fips": county["fips"],
        "name": county["name"],
        "state": county["state"],
        "centroid": {"lat": county["lat"], "lon": county["lon"]},
        "alerts": alerts,
        "alert_count": alert_count,
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
    gdelt_by_fips: dict[str, dict[str, list[dict]]],
    wildfires_by_fips: dict[str, list[dict]],
    transit_by_fips: dict[str, list[dict]],
    national: dict[str, list[dict]],
) -> None:
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
        )
        rec["date"] = today
        records.append(rec)
        by_fips[c["fips"]] = rec

    flagged = [r for r in records if r["alert_count"] > 0]

    full = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "date": today,
        "notes": OUTPUT_NOTES,
        "counties_total": len(records),
        "counties_with_alerts": len(flagged),
        "national": national,
        "counties": {r["fips"]: _strip_for_full(r) for r in records},
    }
    _write_json(DATA_DIR / "today.json", full)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "date": today,
        "notes": OUTPUT_NOTES,
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


def _strip_for_full(rec: dict) -> dict:
    return {
        "fips": rec["fips"],
        "name": rec["name"],
        "state": rec["state"],
        "alerts": rec["alerts"],
        "alert_count": rec["alert_count"],
    }
