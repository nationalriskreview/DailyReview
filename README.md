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
- **`wildfires`** — Active wildfires within 50 miles (NASA EONET). Categorized by `threat_level` (`Immediate` <15mi, `Vicinity` <50mi) and includes `acreage` when available.
- **`transit`** — Severe mass-transit outages: GTFS-Realtime alerts with `effect=NO_SERVICE`, active now, route-level scope, non-planned. Covers ~8 major US transit agencies (configured in `reference/transit_agencies.json`). `system_outage: true` flags agency-wide shutdowns.
- **`fema`** — Active FEMA disaster declarations (DR / EM / FM) in the last 30 days. Includes `is_new_today` flag for declarations issued in the past 24 hours.
- **`disease`** — Positive pathogen detections (e.g., Measles) at the county level within the last 14 days, sourced from CDC National Wastewater Surveillance System.

**Note on Geographic Precision:** For massive counties (>4,000 sq miles), weather forecasts and wildfire distances are evaluated against a 5-point bounding grid rather than a single centroid to ensure large jurisdictions do not miss border events.

National alerts in `national.json`:

- **CDC HAN** — Health Alert Network notices at Alert/Advisory level (collector stub; currently inactive pending CDC URL restructure).
- **WHO outbreak news** — Outbreak-keyword-filtered items from the WHO news feed, last 7 days.

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

## License

Aggregated data; original source licensing applies per item. Repository code: MIT.
