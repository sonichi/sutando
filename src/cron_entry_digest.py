"""Stable per-entry digests for `crons.json`, so config drift is DETECTABLE.

A session cron is a snapshot of its prompt taken at registration time. Editing
`crons.json` afterwards does not reach the running job (#2653 makes
/schedule-crons re-register rather than skip, which makes the drift
self-healing) — but nothing yet makes an unhealed drift *visible*. A drifted
cron stays invisible until it fires or until /schedule-crons runs again.

This module is the shared half of that: /schedule-crons stamps the digest map
it registered, and `health-check.py`'s `session-crons` probe recomputes the map
from the current `crons.json` and reports the entries whose digest moved.

Deliberately dependency-light (stdlib only) and deliberately NOT the place for
the "is this entry session-owned?" policy: that filter lives in the probe, which
applies it to the names in the stamp. Keeping it there is what lets this ship
without relocating `_cron_can_never_fire` / `_entry_marked_parked` out of
health-check.py — the probe already owns that judgement and still does.

What the digest covers, and why exactly this:

  cron    — a schedule change is a real behavioural change to a running job
  prompt  — the payload; this is the field that actually drifted in practice

and nothing else. `disabled`, `launchd`, `execution` and friends change WHETHER
an entry is registered, which the existing expected/registered count already
covers; folding them in here would report "drift" for an entry that correctly
stopped being registered at all.
"""

from __future__ import annotations

import hashlib
import json

DIGEST_LEN = 12


def entry_digest(entry: dict) -> str:
    """Digest the registration-relevant fields of one crons.json entry.

    Uses a canonical JSON encoding rather than string concatenation so a prompt
    ending in the separator cannot collide with the next field. `prompt_skill`
    rides along because an entry may carry it INSTEAD of `prompt` (step 3 turns
    it into `/skill-name`), and swapping between the two changes what fires.
    """
    payload = [
        entry.get("cron"),
        entry.get("prompt"),
        entry.get("prompt_skill"),
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:DIGEST_LEN]


def digest_map(entries) -> dict:
    """{entry name -> digest} for every NAMED entry.

    Unnamed entries are skipped: the name is the only stable key between a stamp
    and a later read, and a positional index would report drift for every entry
    after an insertion. Duplicate names keep the LAST occurrence, matching how a
    name-keyed registration would resolve them.
    """
    out = {}
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            out[name] = entry_digest(entry)
    return out


def drifted(stamped: dict, current: dict, names=None) -> list:
    """Names present in BOTH maps whose digest changed, sorted.

    Restricted to `names` when given — the probe passes the entries its own
    session-owned filter accepts, so an edit to a launchd-owned or codex-owned
    entry never warns about session crons.

    A name in one map only is NOT drift: it appeared or disappeared from the
    config, which the expected/registered count already speaks to, and reporting
    it here would double-warn on a legitimate add or removal.
    """
    if not isinstance(stamped, dict) or not isinstance(current, dict):
        return []
    shared = set(stamped) & set(current)
    if names is not None:
        shared &= set(names)
    return sorted(n for n in shared if stamped[n] != current[n])


def main() -> int:  # pragma: no cover — thin CLI glue over the pure functions above
    """Print the digest map for a crons.json, for /schedule-crons to stamp."""
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("crons_file", help="path to hosts/<host>/crons.json")
    args = ap.parse_args()
    with open(args.crons_file, encoding="utf-8") as fh:
        entries = json.load(fh)
    print(json.dumps(digest_map(entries), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
