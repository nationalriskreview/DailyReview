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
    "bank_robbery": (
        "an actual robbery of a financial institution (bank, credit union, "
        "ATM, or branch) that took place at a specific named location"
    ),
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
}

CATEGORY_QUESTIONS = {
    "bank_robbery": (
        'Does this article report an actual bank robbery that occurred in or '
        'directly affecting {county_name}, {state}, within the past week?'
    ),
    'protest': (
        'Does this article describe a real protest, demonstration, march, '
        'rally, or civil-unrest event in or near {county_name}, {state} that '
        'is either: (a) currently happening, (b) just happened in the past '
        '24 hours, or (c) announced/scheduled for today or the next 2 days? '
        'Answer YES for any of those. Answer NO only for opinion/analysis '
        'pieces, historical retrospectives older than 24 hours, or articles '
        'about protests in other locations.'
    ),
    "utility_outage": (
        'Does this article report an actual, significant power outage, blackout, '
        'or severe water disruption (like a boil water advisory) affecting '
        '{county_name}, {state}?'
    ),
    "transit_disruption": (
        'Does this article report an actual major disruption to public-transit '
        'service (transit strike, train derailment, line/system shutdown, mass '
        'service halt, or evacuation) that is currently active and affecting '
        '{county_name}, {state}? Answer YES only if a real transit service '
        'outage is happening right now — not for a planned future event, '
        'a resolved/averted event, or routine delays.'
    ),
}


def _build_prompt(category: str, article: dict, county_name: str, state: str) -> str:
    definition = CATEGORY_DEFINITIONS.get(category, category)
    question_tpl = CATEGORY_QUESTIONS.get(
        category,
        'Does this article report an actual "{category}" event in {county_name}, {state}?'
    )
    question = question_tpl.format(
        category=category, county_name=county_name, state=state,
    )
    title = article.get("title") or "(no title)"
    domain = article.get("domain") or ""
    url = article.get("url") or ""
    return (
        f'You are evaluating news articles to identify real-world events.\n'
        f'Consider the domain\'s reputation. Prioritize local news, .gov, or .edu. '
        f'Be highly skeptical of generic content aggregator sites.\n\n'
        f'Category "{category}" means: {definition}.\n\n'
        f"Article:\n"
        f"  Title: {title}\n"
        f"  Domain: {domain}\n"
        f"  URL: {url}\n"
        f"  Geographic context: tagged to {county_name}, {state}\n\n"
        f"{question}\n\n"
        f"Reply with EXACTLY one word: YES or NO. No explanation."
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


def filter_gdelt_results(
    gdelt_by_fips: dict[str, dict[str, list[dict]]],
    counties_by_fips: dict[str, dict],
) -> dict[str, dict[str, list[dict]]]:
    if not API_KEY:
        log.info("LLM filter: GEMINI_API_KEY not set, skipping precision pass")
        return gdelt_by_fips
    if not gdelt_by_fips:
        return gdelt_by_fips

    log.info("LLM filter: starting precision pass (model=%s)", MODEL)
    filtered: dict[str, dict[str, list[dict]]] = {}
    total_in = 0
    total_kept = 0
    total_calls = 0

    for fips, buckets in gdelt_by_fips.items():
        county = counties_by_fips.get(fips)
        if not county:
            filtered[fips] = buckets
            continue
        county_name = county["name"]
        state = county["state"]

        new_buckets: dict[str, list[dict]] = {}
        for category, articles in buckets.items():
            if not articles:
                new_buckets[category] = articles
                continue
            kept: list[dict] = []
            for art in articles:
                total_in += 1
                if total_calls >= MAX_CALLS_PER_RUN:
                    kept.append(art)
                    continue
                prompt = _build_prompt(category, art, county_name, state)
                answer = _call_gemini(prompt)
                total_calls += 1
                if answer is None:
                    kept.append(art)
                elif answer.startswith("YES"):
                    kept.append(art)
                time.sleep(DELAY_BETWEEN_CALLS)
            total_kept += len(kept)
            new_buckets[category] = kept
        if any(new_buckets.values()):
            filtered[fips] = new_buckets

    pass_rate = (100 * total_kept / total_in) if total_in else 0
    log.info(
        "LLM filter: %d articles in -> %d kept (%.0f%% pass rate, %d calls used)",
        total_in, total_kept, pass_rate, total_calls,
    )
    if total_calls >= MAX_CALLS_PER_RUN:
        log.warning(
            "LLM filter: hit GEMINI_MAX_CALLS=%d; remaining articles passed through unfiltered",
            MAX_CALLS_PER_RUN,
        )
    return filtered
