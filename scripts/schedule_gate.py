#!/usr/bin/env python3
"""Decide whether this workflow trigger should run the nightly collection.

GitHub's `schedule` triggers are best-effort: they routinely start hours late
(4-12h at times) and are occasionally dropped. So the workflow fires every two
hours and this gate runs the collection once per Pacific day, on the first
trigger that lands after midnight Pacific:

  - workflow_dispatch          -> always run (manual runs bypass the gate)
  - digest already generated   -> skip (one run per Pacific day)
    today (Pacific)
  - collect already attempted  -> skip; a persistently failing run must not
    MAX_ATTEMPTS_PER_DAY times    burn BigQuery/Gemini quota every two hours
  - otherwise                  -> run; "in_window" before WINDOW_END_HOUR,
                                  "catch_up" after (late data beats no data)

"Already generated today" is read from data/national.json `generated_at` at
the *current* tip of the branch (the gate checks out the branch, not the
trigger's SHA), so a run queued behind one that just pushed sees its digest.

Writes `run=true|false` and `reason=<code>` to $GITHUB_OUTPUT. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WINDOW_TZ = ZoneInfo("America/Los_Angeles")
WINDOW_END_HOUR = 3            # target window is 00:00-02:59 Pacific
MAX_ATTEMPTS_PER_DAY = 3
COLLECT_JOB_NAME = "collect"

NATIONAL_JSON = Path(__file__).resolve().parent.parent / "data" / "national.json"


def last_generated_at(path: Path = NATIONAL_JSON) -> datetime | None:
    """`generated_at` of the latest published digest, or None if unreadable."""
    try:
        ts = json.loads(path.read_text())["generated_at"]
        dt = datetime.fromisoformat(ts)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"::warning::schedule gate: can't read last digest time from "
              f"{path} ({e!r}); treating today as not yet run")
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pacific_midnight(now_utc: datetime) -> datetime:
    """Start of the current Pacific day, as an aware datetime."""
    local = now_utc.astimezone(WINDOW_TZ)
    return datetime(local.year, local.month, local.day, tzinfo=WINDOW_TZ)


def decide(
    event: str,
    now_utc: datetime,
    generated_at: datetime | None,
    attempts_today: int,
) -> tuple[bool, str]:
    if event == "workflow_dispatch":
        return True, "manual"
    today = now_utc.astimezone(WINDOW_TZ).date()
    if generated_at is not None and generated_at.astimezone(WINDOW_TZ).date() == today:
        return False, "already_ran_today"
    if attempts_today >= MAX_ATTEMPTS_PER_DAY:
        return False, "attempt_cap_reached"
    if now_utc.astimezone(WINDOW_TZ).hour < WINDOW_END_HOUR:
        return True, "in_window"
    return True, "catch_up"


def _gh_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def count_attempts_today(now_utc: datetime) -> int | None:
    """How many runs of this workflow have run the collect job today (Pacific).

    Returns None if it can't tell (no token / API error); the caller then
    fails open, since a missed digest is worse than an extra run.
    """
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    this_run = os.environ.get("GITHUB_RUN_ID")
    # owner/repo/.github/workflows/daily-digest.yml@refs/heads/main
    workflow_file = workflow_ref.split("@")[0].rsplit("/", 1)[-1]
    if not (token and repo and workflow_file):
        return None

    day_start = pacific_midnight(now_utc)
    # Runs can be created a little before midnight yet execute after it, so
    # list from a margin earlier and filter on when the collect job started.
    since = (day_start - timedelta(hours=6)).astimezone(timezone.utc)
    since_q = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        runs = _gh_get(
            f"{api}/repos/{repo}/actions/workflows/{workflow_file}/runs"
            f"?created=%3E%3D{since_q}&per_page=100", token,
        ).get("workflow_runs", [])
        attempts = 0
        for run in runs:
            if str(run.get("id")) == this_run:
                continue
            jobs = _gh_get(f"{run['jobs_url']}?per_page=100", token).get("jobs", [])
            for job in jobs:
                if job.get("name") != COLLECT_JOB_NAME or not job.get("started_at"):
                    continue
                if job.get("conclusion") == "skipped":
                    continue
                started = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
                if started >= day_start:
                    attempts += 1
        return attempts
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"::warning::schedule gate: could not count today's attempts ({e}); "
              "failing open")
        return None


def main() -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    now = datetime.now(timezone.utc)
    generated = last_generated_at()

    attempts = 0
    if event != "workflow_dispatch" and not (
        generated and generated.astimezone(WINDOW_TZ).date()
        == now.astimezone(WINDOW_TZ).date()
    ):
        attempts = count_attempts_today(now) or 0

    run, reason = decide(event, now, generated, attempts)

    now_pt = now.astimezone(WINDOW_TZ)
    gen_pt = generated.astimezone(WINDOW_TZ).isoformat(timespec="minutes") if generated else "none"
    summary = (f"event={event} now={now_pt.isoformat(timespec='minutes')} "
               f"last_digest={gen_pt} attempts_today={attempts} "
               f"-> run={str(run).lower()} ({reason})")
    print(summary)
    if reason == "catch_up":
        print("::notice::schedule gate: running outside the 00:00-03:00 Pacific "
              "window (catch-up; scheduled triggers arrived late)")
    elif reason == "attempt_cap_reached":
        print(f"::warning::schedule gate: {attempts} collect attempts today without a "
              "published digest; not retrying again until tomorrow")

    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"run={str(run).lower()}\nreason={reason}\n")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(f"Schedule gate: `{summary}`\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
