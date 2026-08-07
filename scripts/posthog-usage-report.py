#!/usr/bin/env python3
"""Daily Sutando usage report from PostHog project 504955 (headless).

Reconstructed 2026-07-28: the original lived behind `workspace/scripts/`, which
is a SYMLINK into the Sutando.app bundle — every app update wiped it. Code
belongs in the repo (workspace contract), so this lives at `scripts/` and the
`posthog-usage-daily` cron invokes it repo-relative.

Reads the personal API key from the secret vault — tries POSTHOG_PERSONAL_APIKEY
then POSTHOG_PERSONAL_API_KEY (both names appear in operational records).
Prints a plaintext report, or a single line starting with `unavailable:` when
the read path is down (the cron DMs that line verbatim).

Metric definitions (verified against live event schema 2026-07-28; events are
`core_started` / `task_processed` / `feature_used` — the older `morning_briefing`
definition no longer exists in the stream):
- installs (all-time) = distinct person_id over all events. person_id churns
  across reinstalls/anonymous sessions, so absolute counts are UPPER BOUNDS;
  trends are the signal (memory: reference_posthog_dau_inflated_id_churn).
- DAU = per-day distinct persons, last 7 days; WAU = 7d distinct persons
- new installs/day = persons whose first-ever event landed that day
- returning 24h = persons active in the last day whose first event is older
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = "504955"
API = f"https://us.posthog.com/api/projects/{PROJECT}/query/"


def _repo_root() -> Path | None:
    for p in Path(__file__).resolve().parents:
        if (p / "src" / "vault_intercept.py").is_file():
            return p
    return None


def _vault_key() -> str | None:
    repo = _repo_root()
    for name in ("POSTHOG_PERSONAL_APIKEY", "POSTHOG_PERSONAL_API_KEY"):
        if repo is not None:
            try:
                out = subprocess.run(
                    [sys.executable, str(repo / "skills/secret-vault/secret-vault.py"), "get", name],
                    capture_output=True, text=True, timeout=15)
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.strip()
            except Exception:
                pass
    return None


def _q(key: str, query: str):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=45))["results"]


def main() -> int:
    key = _vault_key()
    if not key:
        print("unavailable: no POSTHOG_PERSONAL_APIKEY in vault")
        return 1
    try:
        installs = _q(key, "select count(distinct person_id) from events")[0][0]
        wau = _q(key, "select count(distinct person_id) from events where timestamp > now() - interval 7 day")[0][0]
        dau_rows = _q(key, (
            "select toDate(timestamp) d, count(distinct person_id) from events "
            "where timestamp > now() - interval 7 day group by d order by d"))
        new_rows = _q(key, (
            "select first_day, count() from ("
            "select person_id, toDate(min(timestamp)) first_day from events group by person_id) "
            "where first_day > today() - interval 7 day group by first_day order by first_day"))
        returning = _q(key, (
            "select count() from ("
            "select person_id, min(timestamp) first_ts, max(timestamp) last_ts from events group by person_id) "
            "where last_ts > now() - interval 1 day and first_ts < now() - interval 1 day"))[0][0]
        breakdown = _q(key, (
            "select event, count(), count(distinct person_id) from events "
            "where timestamp > now() - interval 7 day group by event order by count() desc limit 10"))
    except urllib.error.HTTPError as e:
        print(f"unavailable: PostHog API {e.code} — {e.read().decode()[:120]}")
        return 1
    except Exception as e:  # noqa: BLE001 — network/timeout paths all end in the same line
        print(f"unavailable: {e}")
        return 1

    print("Sutando usage (PostHog 504955, headless)")
    print(f"- installs all-time (distinct persons; churn-inflated upper bound): {installs}")
    print(f"- WAU (7d distinct persons): {wau}")
    print(f"- returning in last 24h (seen before): {returning}")
    print("- DAU last 7 days:")
    for d, n in dau_rows:
        print(f"    {d}: {n}")
    print("- new installs/day (first-ever event):")
    for d, n in new_rows:
        print(f"    {d}: {n}")
    print("- event breakdown 7d (event / count / persons):")
    for ev, n, p in breakdown:
        print(f"    {ev:32s} {n:<8} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
