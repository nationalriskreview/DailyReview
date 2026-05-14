#!/usr/bin/env python3
"""Daily collection orchestrator — fetches all sources, writes data/*.json."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from geography import load_counties, load_boroughs
from fetch_nws import (
    fetch_active_alerts, bucket_alerts_by_county, fetch_forecasts_for_counties,
)
from fetch_gdelt import collect_gdelt_by_county
from fetch_eonet import fetch_wildfires_by_county
from fetch_transit import fetch_transit_by_county
from fetch_disease import fetch_national
from build_outputs import write_all


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("collect_digest")


async def run(limit: int | None = None, skip_gdelt: bool = False) -> int:
    t0 = time.time()
    counties = load_counties()
    boroughs = load_boroughs()
    if limit:
        counties = counties[:limit]
    log.info("Loaded %d counties, %d boroughs", len(counties), len(boroughs))

    async with aiohttp.ClientSession() as session:
        alerts_features = await fetch_active_alerts(session)
    weather_by_fips = bucket_alerts_by_county(alerts_features)
    log.info("Weather: %d counties with active warnings/watches",
             len(weather_by_fips))

    forecast_targets = [c for c in counties if c["fips"] not in weather_by_fips]
    log.info("Forecast: querying %d counties (skipping those with warnings)",
             len(forecast_targets))
    forecast_by_fips = await fetch_forecasts_for_counties(
        forecast_targets, concurrency=20
    )
    log.info("Forecast: %d counties exceeded thresholds", len(forecast_by_fips))

    gdelt_by_fips: dict = {}
    if skip_gdelt:
        log.info("GDELT: skipped (--skip-gdelt)")
    else:
        log.info("GDELT: querying BigQuery (single SQL, gkg_partitioned 24h)")
        try:
            gdelt_by_fips = await asyncio.get_event_loop().run_in_executor(
                None, collect_gdelt_by_county
            )
            log.info("GDELT: %d counties had matches", len(gdelt_by_fips))
        except Exception as e:
            log.error("GDELT BigQuery failed (continuing with empty): %s", e)
            gdelt_by_fips = {}

    log.info("EONET: fetching active wildfires")
    try:
        wildfires_by_fips = await asyncio.get_event_loop().run_in_executor(
            None, fetch_wildfires_by_county, counties
        )
        log.info("EONET: %d counties within wildfire radius", len(wildfires_by_fips))
    except Exception as e:
        log.error("EONET fetch failed (continuing with empty): %s", e)
        wildfires_by_fips = {}

    log.info("Transit: fetching severe-outage alerts")
    try:
        transit_by_fips = await asyncio.get_event_loop().run_in_executor(
            None, fetch_transit_by_county
        )
        log.info("Transit: %d counties had severe outages", len(transit_by_fips))
    except Exception as e:
        log.error("Transit fetch failed (continuing with empty): %s", e)
        transit_by_fips = {}

    log.info("Disease: fetching CDC HAN + WHO DON")
    national = await fetch_national()
    log.info("Disease: %d HAN items, %d WHO items",
             len(national.get("cdc_han", [])), len(national.get("who_don", [])))

    log.info("Writing outputs")
    write_all(
        counties=counties,
        boroughs=boroughs,
        weather_by_fips=weather_by_fips,
        forecast_by_fips=forecast_by_fips,
        gdelt_by_fips=gdelt_by_fips,
        wildfires_by_fips=wildfires_by_fips,
        transit_by_fips=transit_by_fips,
        national=national,
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
