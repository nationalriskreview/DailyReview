"""CDC NNDSS — notifiable-disease elevation signal (state-level).

Surfaces a state+disease pair ONLY when activity breaks the state's own
baseline, so routine weekly case counts are skipped and only states that are
"heating up" appear:

  * spike        — current week (m1) exceeds the state's previous-52-week
                   maximum (m2): a fresh high, CDC's own "exceeds historical
                   limits" indicator.
  * elevated YTD — year-to-date (m3) is at least NNDSS_YOY_RATIO x last year's
                   YTD (m4) and at or above NNDSS_YTD_FLOOR: a slow-building
                   resurgence that no single week spikes on.

Dataset: data.cdc.gov/resource/x9gk-5huc (NNDSS Weekly Data), updated weekly.
NNDSS columns: m1=current week, m2=previous-52-week max, m3=cumulative YTD this
year, m4=cumulative YTD last year.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

NNDSS_URL = "https://data.cdc.gov/resource/x9gk-5huc.json"
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "DailyReview/1.0 (https://github.com/nationalriskreview/DailyReview)",
)
HTTP_TIMEOUT = 30
YOY_RATIO = float(os.environ.get("NNDSS_YOY_RATIO", "2.0"))
YTD_FLOOR = float(os.environ.get("NNDSS_YTD_FLOOR", "10"))

# The exact NNDSS label(s) that compose each surfaced disease. Measles is split
# into Imported+Indigenous (summed). Meningococcal is reported both by serogroup
# and as an "All serogroups" total — we use ONLY the total to avoid
# double-counting the serogroup rows.
DISEASE_LABELS = {
    "Measles": ["Measles, Imported", "Measles, Indigenous"],
    "Tuberculosis": ["Tuberculosis"],
    "Meningococcal disease": ["Meningococcal disease, All serogroups"],
    "Legionellosis": ["Legionellosis"],
}
_LABEL_TO_DISEASE = {
    lbl: dis for dis, lbls in DISEASE_LABELS.items() for lbl in lbls
}

NAME_TO_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "New York City": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI",
    "Wyoming": "WY", "Puerto Rico": "PR", "U.S. Virgin Islands": "VI",
    "Guam": "GU", "American Samoa": "AS", "Northern Mariana Islands": "MP",
}


def _http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("NNDSS fetch failed %s: %s", url, e)
        return None


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _latest_week() -> tuple[str, str] | None:
    params = urllib.parse.urlencode({
        "$select": "year,week",
        "$order": "year DESC, week DESC",
        "$limit": "1",
    })
    rows = _http_get_json(f"{NNDSS_URL}?{params}")
    if not rows:
        return None
    return rows[0].get("year"), rows[0].get("week")


def fetch_notifiable_disease_alerts() -> list[dict]:
    latest = _latest_week()
    if not latest or not latest[0]:
        return []
    year, week = latest

    labels_sql = ",".join(
        "'%s'" % lbl.replace("'", "''") for lbl in _LABEL_TO_DISEASE
    )
    where = (f"year='{year}' AND week='{week}' AND label in ({labels_sql})")
    params = urllib.parse.urlencode({"$where": where, "$limit": "5000"})
    rows = _http_get_json(f"{NNDSS_URL}?{params}")
    if rows is None:
        return []

    # Aggregate m1..m4 per (state, disease), summing measles sub-labels.
    agg: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        state = r.get("states") or ""
        if state not in NAME_TO_ABBR:  # keep real states/territories, drop regions/national
            continue
        disease = _LABEL_TO_DISEASE.get(r.get("label"))
        if not disease:
            continue
        acc = agg.setdefault((state, disease), {"m1": 0.0, "m2": 0.0, "m3": 0.0, "m4": 0.0})
        acc["m1"] += _num(r.get("m1"))
        acc["m2"] += _num(r.get("m2"))
        acc["m3"] += _num(r.get("m3"))
        acc["m4"] += _num(r.get("m4"))

    alerts: list[dict] = []
    for (state, disease), v in agg.items():
        this_week, max_52, ytd, ytd_prev = v["m1"], v["m2"], v["m3"], v["m4"]
        reasons = []
        if this_week > 0 and this_week > max_52:
            reasons.append("spike_above_52wk_max")
        if ytd >= YTD_FLOOR and ytd >= YOY_RATIO * ytd_prev:
            reasons.append("elevated_vs_last_year")
        if not reasons:
            continue
        alerts.append({
            "state": state,
            "state_abbr": NAME_TO_ABBR[state],
            "disease": disease,
            "reasons": reasons,
            "this_week": int(this_week),
            "prev_52wk_max": int(max_52),
            "ytd": int(ytd),
            "ytd_last_year": int(ytd_prev),
            "mmwr_week": f"{year}-W{week}",
            "source": "CDC NNDSS Weekly Data",
        })

    alerts.sort(key=lambda a: (a["disease"], -a["ytd"]))
    log.info("NNDSS: %d elevated state+disease alert(s) at %s-W%s",
             len(alerts), year, week)
    return alerts
