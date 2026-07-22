#!/usr/bin/env python3
"""Daily collection orchestrator — fetches all sources, writes data/*.json.

Each fetch is wrapped to populate a `data_sources` health dict that flows
into the output, so consumers can see at a glance which collectors worked
on the current run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from geography import load_counties, load_boroughs, index_by_fips
from fetch_nws import (
    fetch_active_alerts, bucket_alerts_by_county, fetch_forecasts_for_counties,
)
from fetch_airquality import fetch_air_quality_for_counties
from fetch_gdelt import collect_gdelt_by_county
from fetch_eonet import fetch_wildfires_by_county
from fetch_transit import fetch_transit_by_county
from fetch_fema import fetch_fema_by_county
from fetch_disease import fetch_national, fetch_county_disease
from fetch_amtrak import fetch_amtrak_advisories
from fetch_faa import fetch_faa_advisories
from llm_filter import filter_gdelt_results
from build_outputs import write_all


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("collect_digest")


def _truncate_error(e: Exception, n: int = 200) -> str:
    return f"{type(e).__name__}: {e}"[:n]


async def run(limit: int | None = None, skip_gdelt: bool = False) -> int:
    t0 = time.time()
    counties = load_counties()
    boroughs = load_boroughs()
    if limit:
        counties = counties[:limit]
    log.info("Loaded %d counties, %d boroughs", len(counties), len(boroughs))

    data_sources: dict[str, dict] = {}

    # --- Weather (NWS active alerts) ---
    try:
        async with aiohttp.ClientSession() as session:
            alerts_features = await fetch_active_alerts(session)
        weather_by_fips = bucket_alerts_by_county(alerts_features)
        log.info("Weather: %d counties with active warnings/watches",
                 len(weather_by_fips))
        data_sources["weather_nws_alerts"] = {
            "status": "ok",
            "counties_with_alerts": len(weather_by_fips),
        }
    except Exception as e:
        log.error("Weather alerts fetch failed (continuing with empty): %s", e)
        weather_by_fips = {}
        data_sources["weather_nws_alerts"] = {
            "status": "failed", "error": _truncate_error(e),
        }

    # --- Forecast (NWS gridpoint) ---
    # Queried for ALL counties so every county carries forecast conditions.
    # Synthetic threshold *alerts* are still suppressed for counties that
    # already have an active NWS warning, to avoid double-surfacing.
    log.info("Forecast: querying %d counties for conditions + thresholds",
             len(counties))
    try:
        forecast_results = await fetch_forecasts_for_counties(
            counties, concurrency=20
        )
        forecast_conditions_by_fips = {
            f: r["forecast"] for f, r in forecast_results.items()
            if r.get("forecast")
        }
        forecast_by_fips = {
            f: r["alerts"] for f, r in forecast_results.items()
            if r.get("alerts") and f not in weather_by_fips
        }
        log.info("Forecast: %d counties with conditions, %d over threshold",
                 len(forecast_conditions_by_fips), len(forecast_by_fips))
        data_sources["weather_nws_forecast"] = {
            "status": "ok",
            "counties_queried": len(counties),
            "counties_with_conditions": len(forecast_conditions_by_fips),
            "counties_over_threshold": len(forecast_by_fips),
        }
    except Exception as e:
        log.error("Forecast fetch failed (continuing with empty): %s", e)
        forecast_by_fips = {}
        forecast_conditions_by_fips = {}
        data_sources["weather_nws_forecast"] = {
            "status": "failed", "error": _truncate_error(e),
        }

    # --- Air quality (Open-Meteo, US AQI + pollutants), all counties ---
    log.info("Air quality: querying %d counties", len(counties))
    try:
        air_quality_by_fips = await fetch_air_quality_for_counties(
            counties, concurrency=20
        )
        log.info("Air quality: %d counties with readings", len(air_quality_by_fips))
        data_sources["air_quality_open_meteo"] = {
            "status": "ok",
            "counties_queried": len(counties),
            "counties_with_readings": len(air_quality_by_fips),
        }
    except Exception as e:
        log.error("Air quality fetch failed (continuing with empty): %s", e)
        air_quality_by_fips = {}
        data_sources["air_quality_open_meteo"] = {
            "status": "failed", "error": _truncate_error(e),
        }

    # --- GDELT (bank robbery, protest, utility outage, transit disruption) ---
    gdelt_by_fips: dict = {}
    if skip_gdelt:
        log.info("GDELT: skipped (--skip-gdelt)")
        data_sources["gdelt"] = {"status": "skipped", "reason": "--skip-gdelt flag"}
        data_sources["llm_filter_gemini"] = {"status": "skipped", "reason": "gdelt skipped"}
    else:
        log.info("GDELT: querying BigQuery (single SQL, gkg_partitioned 24h)")
        try:
            gdelt_by_fips = await asyncio.get_event_loop().run_in_executor(
                None, collect_gdelt_by_county
            )
            log.info("GDELT: %d counties had matches", len(gdelt_by_fips))
            cat_totals: dict[str, int] = {
                "bank_robbery": 0, "protest": 0,
                "utility_outage": 0, "transit_disruption": 0,
            }
            for buckets in gdelt_by_fips.values():
                for k in cat_totals:
                    cat_totals[k] += len(buckets.get(k, []))
            data_sources["gdelt"] = {
                "status": "ok",
                "counties_with_matches": len(gdelt_by_fips),
                "items_pre_llm": cat_totals,
            }
        except Exception as e:
            log.error("GDELT BigQuery failed (continuing with empty): %s", e)
            gdelt_by_fips = {}
            data_sources["gdelt"] = {
                "status": "failed", "error": _truncate_error(e),
            }

        if gdelt_by_fips:
            try:
                gdelt_by_fips = await asyncio.get_event_loop().run_in_executor(
                    None, filter_gdelt_results, gdelt_by_fips, index_by_fips(counties)
                )
                log.info("GDELT (post-LLM): %d counties remain", len(gdelt_by_fips))
                cat_totals_post: dict[str, int] = {
                    "bank_robbery": 0, "protest": 0,
                    "utility_outage": 0, "transit_disruption": 0,
                }
                for buckets in gdelt_by_fips.values():
                    for k in cat_totals_post:
                        cat_totals_post[k] += len(buckets.get(k, []))
                data_sources["llm_filter_gemini"] = {
                    "status": "ok",
                    "counties_with_matches": len(gdelt_by_fips),
                    "items_post_llm": cat_totals_post,
                }
            except Exception as e:
                log.error("LLM filter failed (continuing with unfiltered): %s", e)
                data_sources["llm_filter_gemini"] = {
                    "status": "failed", "error": _truncate_error(e),
                }
        else:
            data_sources["llm_filter_gemini"] = {
                "status": "skipped", "reason": "no gdelt rows to filter",
            }

    # --- EONET wildfires ---
    log.info("EONET: fetching active wildfires")
    try:
        wildfires_by_fips = await asyncio.get_event_loop().run_in_executor(
            None, fetch_wildfires_by_county, counties
        )
        log.info("EONET: %d counties within wildfire radius", len(wildfires_by_fips))
        data_sources["wildfires_eonet"] = {
            "status": "ok",
            "counties_with_alerts": len(wildfires_by_fips),
        }
    except Exception as e:
        log.error("EONET fetch failed (continuing with empty): %s", e)
        wildfires_by_fips = {}
        data_sources["wildfires_eonet"] = {
            "status": "failed", "error": _truncate_error(e),
        }

    # --- Transit (per-agency stats baked in) ---
    log.info("Transit: fetching severe-outage alerts")
    try:
        transit_by_fips, transit_agency_stats = await asyncio.get_event_loop().run_in_executor(
            None, fetch_transit_by_county
        )
        log.info("Transit: %d counties had severe outages", len(transit_by_fips))
        ok_count = sum(1 for s in transit_agency_stats if s["status"] == "ok")
        items_total = sum(s["items"] for s in transit_agency_stats)
        # Derive overall status: ok if all agencies returned ok/skipped_no_auth;
        # partial if some hit fetch_failed / error; failed if all did.
        non_ok = [s for s in transit_agency_stats
                  if s["status"] not in ("ok", "skipped_no_auth")]
        if not non_ok:
            overall = "ok"
        elif ok_count == 0:
            overall = "failed"
        else:
            overall = "partial"
        data_sources["transit"] = {
            "status": overall,
            "agencies_total": len(transit_agency_stats),
            "agencies_ok": ok_count,
            "items_total": items_total,
            "counties_with_alerts": len(transit_by_fips),
            "agencies": transit_agency_stats,
        }
    except Exception as e:
        log.error("Transit fetch failed (continuing with empty): %s", e)
        transit_by_fips = {}
        data_sources["transit"] = {
            "status": "failed", "error": _truncate_error(e),
        }

    # --- FEMA ---
    log.info("FEMA: fetching disaster declarations")
    try:
        fema_by_fips = await asyncio.get_event_loop().run_in_executor(
            None, fetch_fema_by_county
        )
        log.info("FEMA: %d counties have active declarations", len(fema_by_fips))
        data_sources["fema"] = {
            "status": "ok",
            "counties_with_alerts": len(fema_by_fips),
        }
    except Exception as e:
        log.error("FEMA fetch failed (continuing with empty): %s", e)
        fema_by_fips = {}
        data_sources["fema"] = {
            "status": "failed", "error": _truncate_error(e),
        }

    # --- Disease (CDC HAN + WHO DON + Wastewater) ---
    log.info("Disease: fetching CDC HAN + WHO DON + Wastewater")
    try:
        national, disease_by_fips = await asyncio.gather(
            fetch_national(),
            fetch_county_disease(),
        )
        log.info("Disease: %d HAN, %d WHO, %d counties with detections",
                 len(national.get("cdc_han", [])), len(national.get("who_don", [])),
                 len(disease_by_fips))
        data_sources["disease_cdc_han"] = {
            "status": "ok", "items": len(national.get("cdc_han", [])),
        }
        data_sources["disease_who_don"] = {
            "status": "ok", "items": len(national.get("who_don", [])),
        }
        data_sources["disease_wastewater"] = {
            "status": "ok", "counties_with_detections": len(disease_by_fips),
        }
    except Exception as e:
        log.error("Disease fetch failed (continuing with empty): %s", e)
        national, disease_by_fips = {"cdc_han": [], "who_don": []}, {}
        err = _truncate_error(e)
        data_sources["disease_cdc_han"] = {"status": "failed", "error": err}
        data_sources["disease_who_don"] = {"status": "failed", "error": err}
        data_sources["disease_wastewater"] = {"status": "failed", "error": err}

    # --- Amtrak ---
    log.info("Amtrak: scraping passenger advisories + building route map")
    try:
        amtrak_national, amtrak_by_fips = await asyncio.get_event_loop().run_in_executor(
            None, fetch_amtrak_advisories, counties
        )
        log.info("Amtrak: %d active advisor(y/ies), %d counties tagged",
                 len(amtrak_national), len(amtrak_by_fips))
        data_sources["amtrak"] = {
            "status": "ok",
            "national_items": len(amtrak_national),
            "counties_with_alerts": len(amtrak_by_fips),
        }
    except Exception as e:
        log.error("Amtrak fetch failed (continuing with empty): %s", e)
        amtrak_national, amtrak_by_fips = [], {}
        data_sources["amtrak"] = {
            "status": "failed", "error": _truncate_error(e),
        }
    national["amtrak_advisories"] = amtrak_national

    # --- FAA ---
    log.info("FAA: fetching major-hub closures and ground stops")
    try:
        faa_national, faa_by_fips = await asyncio.get_event_loop().run_in_executor(
            None, fetch_faa_advisories
        )
        log.info("FAA: %d advisor(y/ies), %d counties tagged",
                 len(faa_national), len(faa_by_fips))
        data_sources["aviation_faa"] = {
            "status": "ok",
            "national_items": len(faa_national),
            "counties_with_alerts": len(faa_by_fips),
        }
    except Exception as e:
        log.error("FAA fetch failed (continuing with empty): %s", e)
        faa_national, faa_by_fips = [], {}
        data_sources["aviation_faa"] = {
            "status": "failed", "error": _truncate_error(e),
        }
    national["faa_advisories"] = faa_national

    log.info("Writing outputs")
    write_all(
        counties=counties,
        boroughs=boroughs,
        weather_by_fips=weather_by_fips,
        forecast_by_fips=forecast_by_fips,
        forecast_conditions_by_fips=forecast_conditions_by_fips,
        air_quality_by_fips=air_quality_by_fips,
        gdelt_by_fips=gdelt_by_fips,
        wildfires_by_fips=wildfires_by_fips,
        transit_by_fips=transit_by_fips,
        amtrak_by_fips=amtrak_by_fips,
        faa_by_fips=faa_by_fips,
        fema_by_fips=fema_by_fips,
        disease_by_fips=disease_by_fips,
        national=national,
        data_sources=data_sources,
    )

    elapsed = time.time() - t0
    log.info("Done in %.1fs", elapsed)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit to N counties (for local testing)")
    parser.add_argument("--skip-gdelt", action="store_true",
                        help="Skip GDELT queries (for fast local testing)")
    args = parser.parse_args()
    return asyncio.run(run(limit=args.limit, skip_gdelt=args.skip_gdelt))


if __name__ == "__main__":
    sys.exit(main())
