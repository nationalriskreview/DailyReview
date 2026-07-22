# Daily Review

Daily national risk feed covering all ~3,143 US counties. Static JSON, updated once per day via GitHub Actions.

NYC's five boroughs are also exposed as first-class units alongside their county roll-ups.

## Endpoints

Base URL: `https://nationalriskreview.github.io/DailyReview/data/`
CDN alternative: `https://cdn.jsdelivr.net/gh/nationalriskreview/DailyReview@main/data/`

| Endpoint | Description |
|---|---|
| `today-summary.json` | Only counties with active alerts. Small, fast. The default choice for most consumers. |
| `today.json` | Full national snapshot — all 3,143 counties, including those with no alerts. |
| `national.json` | CDC HAN + WHO outbreak news only. |
| `counties/{fips}.json` | Single county detail (5-digit FIPS, e.g. `06037` for Los Angeles County, CA). |
| `states/{abbr}.json` | State-level roll-up (e.g. `CA`, `NY`, `TX`). |
| `nyc/index.json` | All five NYC boroughs in one file. |
| `nyc/{borough}.json` | Single borough: `manhattan`, `brooklyn`, `queens`, `bronx`, `staten-island`. |
| `archive/{YYYY-MM-DD}.json` | Historical daily snapshot. Written only on days with active alerts. Last 365 days retained. |

## Categories

Each county object exposes alerts in these buckets:

- **`weather`** — NWS Warnings, Hurricane/Tropical/Winter Storm Watches, and 24h gridpoint forecast exceeding 1" rain, 6" snow, >105°F heat, or <0°F cold.
- **`bank_robbery`** — News reports of bank robberies (GDELT GKG, strict title filter + LLM confirmation). Includes `is_new` flag for reports within the last 12 hours.
- **`protest`** — Protests / demonstrations (GDELT GKG, prospective filter + LLM confirmation). Includes `is_new` flag.
- **`utility_outage`** — Significant disruptions to power or water (GDELT GKG, keyword filter + LLM confirmation). Includes `is_new` flag.
- **`transit_disruption`** — Major transit disruptions reported in past-24h news (GDELT GKG, strict disruption-verb + transit-noun title filter, conditional/resolution language rejected, LLM precision pass). Covers strikes, derailments, system shutdowns, mass cancellations, evacuations. Complements `transit` by surfacing events before they are encoded in structured agency feeds.
- **`wildfires`** — Active wildfires within 50 miles (NASA EONET). Categorized by `threat_level` (`Immediate` <15mi, `Vicinity` <50mi) and includes `acreage` when available.
- **`transit`** — Severe mass-transit outages from structured agency feeds: GTFS-Realtime alerts with `effect=NO_SERVICE` (active now, route-level scope, non-planned). Covers ~12 major US transit agency feeds (configured in `reference/transit_agencies.json`) — MTA NYC Subway, LIRR, and Metro-North; MBTA; NJ Transit Rail (public Rail Advisories RSS); PATH; WMATA; BART; Caltrain (via 511 SF Bay); CTA; Metra. MTA feeds tag every alert as `UNKNOWN_EFFECT`, so a text-based severity heuristic is used in their place (matches strike/system-wide/full-suspension language, rejects planned work and conditional notices). NJ Transit's RSS is filtered to strike/derailment/system-wide severity only; elevator/escalator/track-work entries are dropped. `system_outage: true` flags agency-wide shutdowns.
- **`amtrak`** — Active Amtrak service-stoppage and full-station-closure advisories scraped from `amtrak.com/service-alerts-and-notices`, filtered to severity-only and fanned out per-county via Amtrak's static GTFS (passenger advisories → route stops → nearest county centroid; station advisories → station code → county). Surfaces only items where (a) the title indicates a service stoppage (strike/derailment/suspension/cancellation/shutdown) or full station closure, (b) the effective date range includes today, and (c) the route or station code can be matched in the GTFS map. Routine schedule adjustments, boarding changes, modified schedules, accessibility/equipment outages, baggage policy changes, and construction/renovation entries are dropped. Each entry has a `kind` field of `service_stoppage` or `station_closure`. The same severity-filtered list is exposed at `national.amtrak_advisories`.
- **`aviation`** — FAA airport closures and ground stops at major commercial hubs from `nasstatus.faa.gov/api/airport-status-information`. Allowlisted to FAA Large Hubs (~30 airports configured in `reference/airports.json`). Closure NOTAMs that only restrict general-aviation or non-scheduled traffic (`CLSD TO NON SKED`, `CLSD TO ... GA ACFT`, `CLSD TO TRANSIENT`) are dropped — only airport-wide closures affecting commercial service are surfaced. Ground Delay Programs and routine arrival/departure delays are skipped entirely. Each advisory fans out to the airport's metro service-area counties, not just the physical county — so a JFK closure tags Manhattan even though the airport is in Queens. Each entry has a `kind` field of `airport_closure` or `ground_stop`. Same list is exposed at `national.faa_advisories`.
- **`fema`** — Active FEMA disaster declarations (DR / EM / FM) in the last 30 days. Includes `is_new_today` flag for declarations issued in the past 24 hours.
- **`disease`** — Positive pathogen detections (e.g., Measles) at the county level within the last 14 days, sourced from CDC National Wastewater Surveillance System.

## Conditions (every county)

Separate from `alerts`, each county carries an always-on `conditions` object with ambient readings — present whether or not the county has any active alert. Exposed in `today.json`, `counties/{fips}.json`, and the NYC borough files; **omitted** from the lean `today-summary.json` and state roll-ups.

- **`conditions.forecast`** — NWS gridpoint 24h summary: `precip_in_24h`, `snow_in_24h`, `high_apparent_temp_f`, `low_apparent_temp_f`. The same values still generate `alerts.weather` entries when they cross a threshold (>1" rain, >6" snow, >105°F, <0°F), but the raw numbers are now reported for every county. `null` where NWS has no grid coverage.
- **`conditions.air_quality`** — Current US EPA AQI and pollutant concentrations from the Open-Meteo Air Quality API (keyless): `us_aqi`, `category`, next-24h peak (`aqi_24h_max` / `aqi_24h_max_category`), plus `pm2_5`, `pm10`, `ozone`, `nitrogen_dioxide` (µg/m³) and `observed_at`.

**Note on Geographic Precision:** For massive counties (>4,000 sq miles), weather forecasts and wildfire distances are evaluated against a 5-point bounding grid rather than a single centroid to ensure large jurisdictions do not miss border events.

National alerts in `national.json`:

- **CDC HAN** — Health Alert Network notices at Alert/Advisory level (collector stub; currently inactive pending CDC URL restructure).
- **WHO outbreak news** (`who_don`) — Items from WHO's official Disease Outbreak News REST API (`/api/news/diseaseoutbreaknews`), last 30 days. Every item in that feed is a confirmed outbreak report, so no keyword filtering is applied. (Previously this filtered WHO's *general* news feed by outbreak keywords, which leaked policy/governance/guidance items — pandemic-treaty negotiations, risk-reduction guidelines, awards — that are not outbreaks.)
- **`amtrak_advisories`** — Active Amtrak service-stoppage and station-closure advisories, scraped daily from amtrak.com/service-alerts-and-notices. Severity-filtered; routine schedule changes and equipment-level station issues are excluded.
- **`faa_advisories`** — FAA Large-Hub airport closures and ground stops, severity-filtered to exclude GA-only NOTAMs. From nasstatus.faa.gov/api/airport-status-information.

## Run health (`data_sources`)

Each top-level output (`today.json`, `today-summary.json`, `national.json`) includes a `data_sources` object reporting per-collector status for the current run. Use it to see at a glance which fetches worked and which silently returned empty.

Per source: `status` is one of `ok`, `failed`, `skipped`, `partial`. `ok` means the fetch and parse succeeded (zero items is still `ok`). `failed` includes a truncated `error` string. `partial` is used for transit when some agencies succeeded and others failed.

Transit additionally exposes a per-agency `agencies` array — each with `id`, `name`, `status`, and `items`. Per-agency status values:

- `ok` — fetched and parsed
- `skipped_no_auth` — agency requires an API key env var that wasn't set
- `fetch_failed` — HTTP fetch returned an error or timed out
- `config_error` — agency config is malformed (`auth.env` missing)
- `error` — unhandled exception during parse (also has truncated `error` text)

Examples:

```json
"transit": {
  "status": "ok",
  "agencies_total": 11,
  "agencies_ok": 8,
  "items_total": 1,
  "counties_with_alerts": 6,
  "agencies": [
    {"id": "mta-lirr", "name": "MTA Long Island Rail Road", "status": "ok", "items": 1},
    {"id": "caltrain", "name": "Caltrain", "status": "skipped_no_auth", "items": 0},
    ...
  ]
}
```

```json
"gdelt": {
  "status": "ok",
  "counties_with_matches": 12,
  "items_pre_llm": {"bank_robbery": 3, "protest": 6, "utility_outage": 4, "transit_disruption": 5}
}
```

## Schedule

Workflow runs daily at **09:00 UTC** (~5 AM ET / 2 AM PT). Output `generated_at` timestamp reflects the actual run time.

## Data Sources

- [NWS Alerts API](https://api.weather.gov/alerts/active)
- [NWS Forecast API](https://www.weather.gov/documentation/services-web-api)
- [GDELT GKG via BigQuery](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [NASA EONET](https://eonet.gsfc.nasa.gov/)
- [CDC NWSS Wastewater API](https://data.cdc.gov/resource/akvg-8vrb.json)
- [OpenFEMA API](https://www.fema.gov/about/openfema/data-sets)
- [CDC HAN](https://emergency.cdc.gov/han/) (Placeholder)
- [WHO Disease Outbreak News](https://www.who.int/feeds/entity/csr/don/en/rss.xml)
- [Amtrak Service Alerts & Notices](https://www.amtrak.com/service-alerts-and-notices) — scraped HTML; route→county mapping derived from Amtrak's [static GTFS](https://content.amtrak.com/content/gtfs/GTFS.zip)
- [FAA NAS Airport Status](https://nasstatus.faa.gov/api/airport-status-information) — XML feed; airport→service-area mapping in `reference/airports.json`

## License

Aggregated data; original source licensing applies per item. Repository code: MIT.
