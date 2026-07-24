"""Gemini Flash final-pass filter for GDELT article classification.

Takes the GDELT bucketed output and applies a per-article LLM yes/no judgment:
"Does this article report an actual {category} event affecting {county}?"
Drops the ones the model answers NO.

Sized for Gemini Flash free tier:
  - 15 RPM   (we sleep ~4s between calls)
  - 1500 RPD (we cap at MAX_CALLS_PER_RUN)
  - 1M TPD   (our typical prompt+response is ~600 tokens)

Gracefully degrades: if GEMINI_API_KEY is unset or any call fails, the
input is passed through unchanged with a log warning. The pipeline never
fails because of this filter.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
)
DELAY_BETWEEN_CALLS = float(os.environ.get("GEMINI_DELAY_SEC", "4.5"))
MAX_CALLS_PER_RUN = int(os.environ.get("GEMINI_MAX_CALLS", "300"))
HTTP_TIMEOUT = 30

CATEGORY_DEFINITIONS = {
    "protest": (
        "a public protest, demonstration, march, rally, or civil-unrest event "
        "that is either currently happening, just happened in the past 24h, "
        "or is announced/scheduled for today or the next 2 days at a specific "
        "named location"
    ),
    "utility_outage": (
        "a significant, currently ongoing or very recent (past 24h) disruption "
        "to public utilities such as a power grid failure, major blackout, "
        "water main break causing a boil water advisory, or severe water shortage"
    ),
    "transit_disruption": (
        "a major, currently active disruption to mass-transit service such as "
        "a transit-worker strike, train derailment, full line or system "
        "shutdown, mass cancellation of trains, or evacuation of a transit "
        "facility. Routine delays, single-train mechanical issues, planned "
        "weekend track work, and station-level accessibility outages do NOT "
        "qualify."
    ),
    "service_provider_outage": (
        "a significant operational outage or service disruption at a major "
        "technology, cloud, or telecom provider (e.g. Microsoft/Azure, Google "
        "Cloud, AWS, Oracle, Cloudflare, Salesforce, Verizon, AT&T, T-Mobile, "
        "Comcast) that is currently affecting users — not a product launch, "
        "earnings, security-patch, or business-deal story"
    ),
    "hazmat_incident": (
        "a hazardous-materials or industrial accident such as a chemical "
        "spill/leak, plant or refinery explosion, gas or toxic release, or "
        "pipeline rupture, that is causing evacuations, shelter-in-place, or "
        "area closures at a specific named location"
    ),
    "road_closure": (
        "a major roadway closure — an interstate, highway, freeway, key bridge "
        "or tunnel closed or blocked in both directions or across all lanes due "
        "to an incident, crash, flooding, or damage. Routine lane closures, "
        "single-lane or ramp closures, and planned construction do NOT qualify."
    ),
}

def _build_prompt(category: str, article: dict) -> str:
    """Location-agnostic event-type classification. We ask only whether the
    article reports a real event of this category — geographic attribution to
    counties is GDELT's job (V2Locations), so the same article is classified
    once and the verdict is applied to every county it was tagged to."""
    definition = CATEGORY_DEFINITIONS.get(category, category)
    readable = category.replace("_", " ")
    title = article.get("title") or "(no title)"
    domain = article.get("domain") or ""
    url = article.get("url") or ""
    return (
        'You are evaluating news articles to identify real-world events.\n'
        "Consider the domain's reputation. Prioritize local news, .gov, or .edu. "
        'Be highly skeptical of generic content aggregator sites.\n\n'
        f'Category "{category}" means: {definition}.\n\n'
        'Article:\n'
        f'  Title: {title}\n'
        f'  Domain: {domain}\n'
        f'  URL: {url}\n\n'
        f'Does this article report a real, current {readable} event matching '
        'that definition, at a specific location in the United States? Answer NO '
        'for opinion/analysis, historical retrospectives, planned-then-cancelled '
        'or resolved events, or unrelated topics.\n\n'
        'Reply with EXACTLY one word: YES or NO. No explanation.'
    )


def _call_gemini(prompt: str) -> str | None:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 10},
    }
    url = f"{ENDPOINT}?key={API_KEY}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        log.warning("Gemini HTTPError %d: %s", e.code, body_text)
        return None
    except Exception as e:
        log.warning("Gemini fetch error: %s", e)
        return None

    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    if not parts:
        return None
    return ((parts[0].get("text") or "").strip().upper())


def _article_key(category: str, art: dict) -> tuple[str, str]:
    """Dedup key: an article is the same classification everywhere it is tagged.
    Keyed by (category, url); falls back to title so url-less articles are still
    classified individually rather than collapsing together."""
    return (category, art.get("url") or art.get("title") or "")


def filter_gdelt_results(
    gdelt_by_fips: dict[str, dict[str, list[dict]]],
    counties_by_fips: dict[str, dict],
) -> tuple[dict[str, dict[str, list[dict]]], dict]:
    """Return (filtered, stats). stats carries unique_articles / calls_used /
    cap_hit so the caller can surface incomplete filtering in run health."""
    if not API_KEY:
        log.info("LLM filter: GEMINI_API_KEY not set, skipping precision pass")
        return gdelt_by_fips, {"skipped_reason": "no_api_key",
                               "unique_articles": 0, "calls_used": 0, "cap_hit": False}
    if not gdelt_by_fips:
        return gdelt_by_fips, {"unique_articles": 0, "calls_used": 0, "cap_hit": False}

    # 1. Collect each unique (category, article) once — an article tagged to N
    #    counties would otherwise be classified N times with the same answer.
    unique: dict[tuple[str, str], dict] = {}
    for buckets in gdelt_by_fips.values():
        for category, articles in buckets.items():
            for art in articles:
                unique.setdefault(_article_key(category, art), art | {"_category": category})

    log.info("LLM filter: %d unique articles to classify (model=%s)", len(unique), MODEL)

    # 2. Classify each unique article once, up to the per-run cap (fail-open).
    verdicts: dict[tuple[str, str], bool] = {}
    calls_used = 0
    cap_hit = False
    for key, art in unique.items():
        category = art["_category"]
        if calls_used >= MAX_CALLS_PER_RUN:
            cap_hit = True
            verdicts[key] = True  # cap reached: keep unclassified (fail-open)
            continue
        answer = _call_gemini(_build_prompt(category, art))
        calls_used += 1
        verdicts[key] = True if answer is None else answer.startswith("YES")
        time.sleep(DELAY_BETWEEN_CALLS)

    # 3. Rebuild the per-county structure, applying each article's verdict.
    filtered: dict[str, dict[str, list[dict]]] = {}
    total_in = 0
    total_kept = 0
    for fips, buckets in gdelt_by_fips.items():
        new_buckets: dict[str, list[dict]] = {}
        for category, articles in buckets.items():
            kept = []
            for art in articles:
                total_in += 1
                if verdicts.get(_article_key(category, art), True):
                    kept.append(art)
            new_buckets[category] = kept
            total_kept += len(kept)
        if any(new_buckets.values()):
            filtered[fips] = new_buckets

    pass_rate = (100 * total_kept / total_in) if total_in else 0
    log.info(
        "LLM filter: %d unique classified (%d calls), %d county-items in -> %d kept (%.0f%%)",
        len(unique), calls_used, total_in, total_kept, pass_rate,
    )
    if cap_hit:
        log.warning(
            "LLM filter: hit GEMINI_MAX_CALLS=%d; %d unique articles passed through unfiltered",
            MAX_CALLS_PER_RUN, len(unique) - calls_used,
        )
    return filtered, {
        "unique_articles": len(unique),
        "calls_used": calls_used,
        "cap": MAX_CALLS_PER_RUN,
        "cap_hit": cap_hit,
    }
