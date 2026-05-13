# National Daily Risk Digest — Implementation Plan

## Context
Produce a **daily national risk feed** covering all ~3,143 US counties, served as static JSON from a public GitHub repository. NYC's five boroughs are treated as first-class units in addition to their county roll-ups.

Pipeline: **GitHub Actions** (collection + processing) → **commits results to repo** → **GitHub Pages** (serves static JSON over HTTPS). Total infrastructure cost: $0.

---

## Architecture Overview

```
┌─────────────────────┐    ┌──────────────────────┐    ┌─────────────────────┐
│ GitHub Actions cron │───▶│ collect_digest.py    │───▶│ Writes data/*.json  │
│ daily @ 09:00 UTC   │    │ (Python, async HTTP) │    │ to repo via commit  │
└─────────────────────┘    └──────────────────────┘    └──────────┬──────────┘
                                                                  │
                                                                  ▼
                                                        ┌─────────────────────┐
                                                        │ GitHub Pages serves │
                                                        │ data/ as public API │
                                                        └─────────────────────┘
```

Consumers fetch JSON from `https://{user}.github.io/{repo}/data/...` or `https://cdn.jsdelivr.net/gh/{user}/{repo}@main/data/...` (CDN-backed).

---

## Geography Model

### Counties (primary unit)
- Source of truth: US Census TIGER county FIPS list (~3,143 counties + DC + territories).
- Each county keyed by 5-digit FIPS (`SSCCC`), e.g. `06037` = Los Angeles County, CA.
- Each county has: name, state, FIPS, centroid (lat/lon), NWS UGC zone(s).

### NYC Boroughs (special-case first-class units)
Each borough IS a county. Treated as a normal county AND given a borough-level entry with NYC-aware queries.

| Borough | County | FIPS |
|---|---|---|
| Manhattan | New York County | 36061 |
| Brooklyn | Kings County | 36047 |
| Queens | Queens County | 36081 |
| Bronx | Bronx County | 36005 |
| Staten Island | Richmond County | 36085 |

For each borough, GDELT queries use **both** the borough name and the county name:
- e.g. Manhattan: `("Manhattan" OR "New York County") AND "New York"`
- Results from both queries are merged and deduplicated by URL.

Borough output appears under `data/nyc/{borough}.json` AND in the corresponding county's `data/counties/{fips}.json`.

---

## Data Sources (national-scale strategy)

### 1. NWS Alerts — Severe Weather
- **Endpoint:** `https://api.weather.gov/alerts/active` (single bulk call, no key)
- **Strategy:** Fetch **all active alerts nationally in one request**, then bucket by UGC zone code. Map zones → counties via the NWS zone-to-county lookup. Massively more efficient than per-county point queries.
- **Include if:**
  - `event` contains "Warning" (any type), OR
  - `event` in {"Hurricane Watch", "Tropical Storm Watch", "Winter Storm Watch", "Hurricane Local Statement"}, OR
  - `event` contains "Hurricane" or "Tropical" (early awareness)
- **Exclude:** Advisories, Statements, Special Weather Statements.
- **Cost:** 1 API call total.

### 2. NWS Point Forecast — Precipitation/Snow Thresholds
- **Endpoint:** `https://api.weather.gov/points/{lat},{lon}` → follow `forecastHourly`
- **Per county centroid**, only if no Warning already exists for that county.
- **Trigger if 24h forecast shows:** >1.0" precip OR >6.0" snow.
- **Cost:** Up to 3,143 calls, but skipped for counties with active warnings.
- **Concurrency:** `aiohttp` with semaphore=20, ~0.05s polite delay. Estimated ~5 minutes.

### 3. GDELT DOC 2.0 — Protests, Bank Robberies, Transportation
- **Endpoint:** `https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode=artlist&maxrecords=25&timespan=1d&format=json`
- **One combined query per county**, scoped by county + state to disambiguate name collisions ("Washington County" exists in ~30 states):
  ```
  ("bank robbery" OR "protest" OR "demonstration"
    OR "road closure" OR "bridge closed" OR "freeway closed"
    OR "transit suspended" OR "highway shutdown")
  AND "{county}" AND "{state_full}"
  ```
- **Post-classification:** for each returned article, locally classify into `bank_robbery`, `protest`, or `transportation` by which keyword(s) matched in title/snippet. Articles matching multiple categories appear in each (rare in practice).
- **`maxrecords=25`** (raised from 10) to leave headroom for the broader OR'd query.
- **NYC boroughs:** additional borough-name query merged into the county result.
- **Noise filters:**
  - ≥2 distinct source domains per event (dedup by title similarity).
  - Bank robbery bucket: snippet must contain "bank".
  - Protest bucket: exclude if snippet contains "Super Bowl", "concert", "game", "stadium".
  - Transportation bucket: snippet must contain "clos", "shut", "suspend", or "block".
- **Cost:** ~3,143 calls/day (+5 NYC borough calls). Concurrency=5, polite delay 0.2s. Estimated ~12 minutes.
- **Fallback:** if GDELT throttles (empty responses / 429s) at this volume, switch the GDELT collection to BigQuery against the public `gdelt-bq:gdeltv2.*` tables. Requires a Google Cloud account + service account key (free tier covers daily use). Script-level change only — output shapes unchanged.

### 4. CDC Health Alert Network
- **Feed:** scrape `https://emergency.cdc.gov/han/` or its RSS equivalent.
- **Filter:** HAN Health Alert + Health Advisory only (skip Health Updates). Published within last 48h.
- **Scope:** national section, not per-county.
- **Cost:** 1 call.

### 5. WHO Disease Outbreak News
- **Feed:** `https://www.who.int/feeds/entity/csr/don/en/rss.xml`
- **Filter:** items published within last 48h.
- **Scope:** national/global section.
- **Cost:** 1 call.

### Total daily API budget
| Source | Calls | Notes |
|---|---|---|
| NWS alerts (bulk) | 1 | |
| NWS forecast | ≤3,143 | skipped for counties w/ warnings |
| GDELT | ~3,143 | 1 OR'd query × 3,143 counties |
| GDELT (NYC extra) | 5 | 1 × 5 boroughs |
| CDC HAN | 1 | |
| WHO | 1 | |
| **Total** | **~6,300** | Workflow runs in ~20–25 min |

Public repo = unlimited GitHub Actions minutes. Even on the private-repo free tier (2,000 min/month), daily runs at 25 min ≈ 750 min/month — well within budget.

---

## Repo Layout

```
/
├── .github/workflows/
│   └── daily-digest.yml          # cron workflow
├── scripts/
│   ├── collect_digest.py         # main orchestrator
│   ├── fetch_nws.py              # NWS alerts (bulk) + forecast
│   ├── fetch_gdelt.py            # GDELT queries + dedup
│   ├── fetch_disease.py          # CDC HAN + WHO
│   ├── geography.py              # FIPS list, centroids, NYC borough map
│   └── build_outputs.py          # write all data/*.json files
├── reference/
│   ├── counties.json             # FIPS, name, state, centroid lat/lon, NWS zone(s)
│   └── nyc_boroughs.json         # borough → FIPS + query aliases
├── data/
│   ├── today.json                # full national snapshot (all counties)
│   ├── today-summary.json        # only counties with alerts
│   ├── national.json             # CDC HAN + WHO
│   ├── counties/
│   │   └── {fips}.json           # one file per county (3,143 files)
│   ├── states/
│   │   └── {state}.json          # one file per state (50 files + DC + territories)
│   ├── nyc/
│   │   ├── index.json            # roll-up of all 5 boroughs
│   │   ├── manhattan.json
│   │   ├── brooklyn.json
│   │   ├── queens.json
│   │   ├── bronx.json
│   │   └── staten-island.json
│   └── archive/
│       └── {YYYY-MM-DD}.json     # full daily snapshot, only kept if any alerts
└── README.md                     # endpoint documentation for consumers
```

---

## Output JSON Shapes

### `data/today.json` (full national)
```json
{
  "generated_at": "2026-05-13T09:14:22Z",
  "date": "2026-05-13",
  "counties_total": 3143,
  "counties_with_alerts": 47,
  "national": {
    "cdc_han": [...],
    "who_don": [...]
  },
  "counties": {
    "06037": {
      "fips": "06037",
      "name": "Los Angeles County",
      "state": "CA",
      "alerts": {
        "weather": [{"event": "High Wind Warning", "headline": "...", "expires": "..."}],
        "bank_robbery": [...],
        "protest": [...],
        "transportation": []
      },
      "alert_count": 3
    },
    "36061": { "fips": "36061", "name": "New York County", "state": "NY", ... },
    ...
  }
}
```

### `data/today-summary.json`
Same shape, but `counties` only includes those with `alert_count > 0`. Much smaller (~50–200 KB).

### `data/counties/{fips}.json`
```json
{
  "fips": "06037",
  "name": "Los Angeles County",
  "state": "CA",
  "date": "2026-05-13",
  "alerts": { "weather": [...], "bank_robbery": [...], ... },
  "alert_count": 3
}
```

### `data/nyc/manhattan.json`
```json
{
  "borough": "Manhattan",
  "fips": "36061",
  "county_name": "New York County",
  "date": "2026-05-13",
  "alerts": { "weather": [...], "bank_robbery": [...], "protest": [...], "transportation": [...] },
  "alert_count": 2,
  "note": "Includes events tagged 'Manhattan' or 'New York County'"
}
```

### `data/nyc/index.json`
Array of all 5 boroughs in one file for convenience.

---

## Public Endpoints (via GitHub Pages)

Base URL: `https://{user}.github.io/{repo}/data/`

| Endpoint | Use case |
|---|---|
| `today-summary.json` | 95% case — only counties with alerts. Small, fast. |
| `today.json` | Full national snapshot, all 3,143 counties. |
| `national.json` | CDC HAN + WHO only. |
| `counties/{fips}.json` | Single county detail. |
| `states/{abbr}.json` | State-level roll-up. |
| `nyc/index.json` | All 5 NYC boroughs. |
| `nyc/{borough}.json` | Single borough. |
| `archive/{YYYY-MM-DD}.json` | Historical snapshot (alert days only). |

CDN alternative (cached, global): swap `{user}.github.io/{repo}` for `cdn.jsdelivr.net/gh/{user}/{repo}@main`.

---

## GitHub Actions Workflow

`.github/workflows/daily-digest.yml`:

```yaml
name: Daily National Risk Digest

on:
  schedule:
    - cron: '0 9 * * *'      # 09:00 UTC daily (~5 AM ET / 2 AM PT)
  workflow_dispatch:          # manual trigger button

permissions:
  contents: write             # needed to commit results

jobs:
  collect:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install aiohttp feedparser beautifulsoup4
      - name: Run collection
        run: python scripts/collect_digest.py
      - name: Commit results
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/
          git diff --staged --quiet || git commit -m "Daily digest $(date -u +%Y-%m-%d)"
          git push
```

---

## Script Architecture

```
collect_digest.py:
  main()
  ├── load_counties()                       # reference/counties.json (3,143 entries)
  ├── load_nyc_boroughs()                   # reference/nyc_boroughs.json
  ├── fetch_nws_alerts_bulk()               # 1 call, returns all active US alerts
  │   └── bucket_by_county(alerts, counties)
  ├── for each county without weather warning (async, sem=20):
  │   └── fetch_nws_forecast(centroid)      # check precip/snow thresholds
  ├── for each county (async, sem=5):
  │   └── fetch_gdelt_combined(county, state)   # 1 OR'd query, returns up to 25 articles
  │       └── classify_articles(...)            # bucket into bank_robbery / protest / transportation
  ├── for each NYC borough:
  │   └── fetch_gdelt_combined with borough name; merge + dedup with county results
  ├── fetch_cdc_han()                       # 1 call
  ├── fetch_who_don()                       # 1 call
  ├── apply_noise_filters(all_results)
  ├── build_outputs.write_all(results)
  │   ├── today.json
  │   ├── today-summary.json
  │   ├── national.json
  │   ├── counties/*.json
  │   ├── states/*.json
  │   ├── nyc/*.json
  │   └── archive/{date}.json (only if any alerts)
  └── exit 0
```

---

## Noise Suppression

| Category | Include | Exclude |
|---|---|---|
| Weather | Warnings (all); Hurricane/Tropical/Winter Storm Watches; >1" rain or >6" snow forecast in 24h | Advisories, Statements, routine forecasts |
| Bank robbery | Mentions "bank" + robbery within county/state | Non-bank robberies; events > 24h old |
| Protests | ≥2 sources; organized/planned language | Sports/entertainment context; single-source mentions |
| Transportation | Major closures/shutdowns | Routine delays, minor congestion |
| Disease | CDC HAN Alert/Advisory; WHO outbreak news < 48h old | CDC Health Updates (routine); old items |

---

## Repo Size & Retention

**Per-day generated data:**
- `today.json` (full): ~3–5 MB uncompressed.
- `counties/*.json`: ~3,143 files × ~1–2 KB = ~5 MB.
- States, NYC, summary: < 1 MB combined.
- Daily file churn (git diff): typically <500 KB (most counties unchanged day-to-day).

**Archive policy:**
- `data/archive/{YYYY-MM-DD}.json` written **only on days where ≥1 county has an alert** (most days). ~1–2 MB per file.
- Retention: keep last 365 days, prune older in the same workflow.
- Expected steady-state repo size: ~500 MB–1 GB (within GitHub's recommended ceiling).

If repo size becomes a concern, switch archive storage to GitHub Releases (each release can hold large files and doesn't bloat clones).

---

## Verification Steps

1. **Reference data sanity:** confirm `reference/counties.json` has 3,143 entries with valid FIPS, centroids, and NWS zones.
2. **Dry-run locally:** `python scripts/collect_digest.py --dry-run` writes to `/tmp/data/` without committing. Verify output shapes.
3. **Manual workflow trigger:** run via `workflow_dispatch` in GitHub UI. Confirm:
   - Workflow completes in <60 min.
   - Commit appears with today's data.
   - `https://{user}.github.io/{repo}/data/today-summary.json` is fetchable.
4. **High-alert day test:** trigger on a known event day (recent hurricane landfall, etc.). Verify counties in the impact zone show weather alerts.
5. **NYC borough test:** confirm `data/nyc/manhattan.json` and `data/counties/36061.json` reflect the same county data, with the borough file additionally containing Manhattan-tagged GDELT items.
6. **Schedule verification:** confirm next scheduled run appears in the Actions tab.

---

## Open Design Decisions (resolve before/during build)

1. **County centroid source.** Census TIGER vs. a precomputed lookup file. Census is canonical but bulky; a pre-baked `counties.json` is simpler.
2. **NWS zone-to-county mapping.** Some zones span multiple counties (rural west). Need to decide whether to attribute a zone alert to all overlapping counties (recommended) or use a primary-county heuristic.
3. **GDELT rate-limiting.** ~3,143 calls/day at concurrency=5 should sit comfortably within GDELT's informal ~1–2 req/sec tolerance. If throttling shows up in practice (empty responses, 429s, repeated zero-result counties), switch to the BigQuery fallback noted in §"Data Sources" — single SQL query against `gdelt-bq:gdeltv2.*`, free under BigQuery's 1 TB/month tier.
4. **Repo name.** Suggested: `national-risk-digest` or similar. Affects all public URLs.
5. **GitHub Pages activation.** Must be enabled in repo settings (Pages → source = main branch, `/` root) after first push.
6. **Large county handling.** Counties like San Bernardino (CA) are >20,000 sq mi — centroid forecast won't represent corners. Acceptable for v1; flag as future refinement.
7. **Versioning.** Add `"schema_version": "1.0"` to all JSON outputs so consumers can detect changes.

---

## Implementation Notes (deltas from plan above)

Captured during initial build — describes how the working code differs from the plan as written.

- **County count.** Census 2023 gazetteer yields **3,222** counties (50 states + DC + Puerto Rico). VI / GU / AS / MP are not in the gazetteer file and are omitted from v1.
- **NWS alert bucketing.** Uses `properties.geocode.SAME` (6-digit, leading 0 + FIPS) directly. No separate UGC-zone-to-county lookup needed — the SAME field is the authoritative FIPS list per alert.
- **NWS forecast thresholds.** Uses gridpoint `quantitativePrecipitation` + `snowfallAmount` (mm → in conversion). Sums first 24h of values. Centroid-based — known limitation for very large counties.
- **GDELT concurrency.** Bumped to `concurrency=20`, `delay=0.05s` to keep the workflow under the timeout. Typical query time is 5–13s; full run lands in ~25–45 min.
- **Workflow timeout.** Set to **180 min** (not 60). Real GDELT query time is variable; large margin avoids spurious failures.
- **CDC HAN.** The original `emergency.cdc.gov/han/index.asp` URL returns 404 — CDC restructured. `CDC_HAN_URL` env var is left empty in v1; `fetch_cdc_han()` returns `[]` and logs a warning. Set the env var (and adjust the parser if needed) once a stable replacement page is identified.
- **WHO DON.** The retired CSR/DON-specific RSS is replaced with `https://www.who.int/rss-feeds/news-english.xml`, filtered by outbreak keywords. Fetched via `urllib` (not `aiohttp`) because WHO's oversized Content-Security-Policy header exceeds aiohttp's default 8 KB header-size limit.
- **Disease recency window.** Widened from 48h to **168h (7 days)** via `DISEASE_RECENT_HOURS` env var. The general-news feed is lower frequency than the retired DON feed, so 48h often returns zero items even when relevant news exists.

---

## Out of Scope (v1)

- Email or other push delivery (feed-only).
- Sub-county geography outside NYC.
- Historical analytics / trend detection.
- Authenticated endpoints / rate limiting (public read-only feed).
- Real-time push / webhooks (daily snapshot only).
