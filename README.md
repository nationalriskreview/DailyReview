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

- **`weather`** — NWS Warnings, Hurricane/Tropical/Winter Storm Watches, and 24h forecast exceeding 1" rain or 6" snow.
- **`bank_robbery`** — News reports of bank robberies in the county (GDELT, ≥2 source domains).
- **`protest`** — Protests/demonstrations (GDELT, sports/entertainment context excluded).
- **`transportation`** — Major road/bridge/transit closures (GDELT, keyword-filtered).

National alerts in `national.json`:

- **CDC HAN** — Health Alert Network notices at Alert/Advisory level, last 48h.
- **WHO Disease Outbreak News** — last 48h.

## Schedule

Workflow runs daily at **09:00 UTC** (~5 AM ET / 2 AM PT). Output `generated_at` timestamp reflects the actual run time.

## Data Sources

- [NWS Alerts API](https://api.weather.gov/alerts/active)
- [NWS Forecast API](https://www.weather.gov/documentation/services-web-api)
- [GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [CDC HAN](https://emergency.cdc.gov/han/)
- [WHO Disease Outbreak News](https://www.who.int/feeds/entity/csr/don/en/rss.xml)

## License

Aggregated data; original source licensing applies per item. Repository code: MIT.
