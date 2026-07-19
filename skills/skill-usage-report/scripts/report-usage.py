#!/usr/bin/env python3
"""Drain the local skill-usage log and report it to AG2 Cloud (AU#93).

Reads <workspace>/state/skill-usage-log.jsonl (written by hooks/log-usage.py),
aggregates per slug, and POSTs one batched report to
POST {cloud}/api/skills/usage as {agentId, events:[{slug, count, lastUsedAt}]}.

Graceful degrade (exit 0, log left in place) when: log empty/missing, no
AG2_CLOUD_TOKEN in the vault, no agent identity, network/HTTP failure. The
log is renamed to a .reporting file BEFORE the POST so events arriving during
the report are never lost; on failure the .reporting content is folded back.

The endpoint only accepts usage for skills currently equipped to this agent
(skill_assignments rows) — everything else comes back in `skipped`. That is
expected until the owner equips skills to this agent's mxid.

Identity: agentId = $AGENT_MXID (the ag2space identity, same key the
EquipPanel / assignments API use). Cloud origin override: $AG2_CLOUD_ORIGIN.
"""

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLOUD = os.environ.get("AG2_CLOUD_ORIGIN", "https://sutando.ag2.ai")
MAX_EVENTS = 100  # server cap per report


def repo_root() -> Path:
    return Path(os.path.realpath(__file__)).parents[3]


def main() -> int:
    root = repo_root()
    sys.path.insert(0, str(root / "src"))
    try:
        from workspace_default import resolve_workspace  # type: ignore

        ws = Path(resolve_workspace())
    except Exception:
        ws = root / "workspace"

    log = ws / "state" / "skill-usage-log.jsonl"
    pending = log.with_suffix(".jsonl.reporting")

    # Recover a previously claimed log (failed or crashed mid-report), then
    # claim the current one. A crash between rename and fold-back leaves ONLY
    # the .reporting file — handle pending-with-no-active by plain rename.
    if pending.exists():
        if log.exists():
            with pending.open("a", encoding="utf-8") as out, log.open("r", encoding="utf-8") as cur:
                out.write(cur.read())
            log.unlink()
        pending.rename(log)
    if not log.exists() or log.stat().st_size == 0:
        print("usage-report: nothing to report")
        return 0

    agent = os.environ.get("AGENT_MXID", "").strip()
    if not agent:
        print("usage-report: AGENT_MXID not set — skipping (log kept)")
        return 0
    try:
        from vault_intercept import get_vault_key  # type: ignore

        token = get_vault_key("AG2_CLOUD_TOKEN")
    except Exception:
        print("usage-report: no AG2_CLOUD_TOKEN in vault — skipping (log kept)")
        return 0

    log.rename(pending)
    counts: dict[str, int] = defaultdict(int)
    last: dict[str, int] = defaultdict(int)
    with pending.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                slug, ts = rec["slug"], int(rec["ts"])
            except Exception:
                continue
            counts[slug] += max(1, int(rec.get("count", 1)))
            last[slug] = max(last[slug], ts)

    events = [
        {
            "slug": slug,
            "count": counts[slug],
            "lastUsedAt": datetime.fromtimestamp(last[slug], tz=timezone.utc).isoformat(),
        }
        for slug in counts
    ]
    if not events:
        pending.unlink()
        print("usage-report: log had no parseable events")
        return 0

    def fold_back(remaining: list) -> None:
        # Persist unsent slugs back into the ACTIVE log as count-carrying
        # records (the aggregator honors "count"), then release the claim.
        # Appending keeps any events that arrived mid-report; order is
        # irrelevant — aggregation is commutative.
        with log.open("a", encoding="utf-8") as f:
            for ev in remaining:
                f.write(
                    json.dumps(
                        {
                            "slug": ev["slug"],
                            "ts": int(datetime.fromisoformat(ev["lastUsedAt"]).timestamp()),
                            "count": ev["count"],
                        }
                    )
                    + "\n"
                )
        pending.unlink()

    accepted = skipped = sent = 0
    for i in range(0, len(events), MAX_EVENTS):
        chunk = events[i : i + MAX_EVENTS]
        req = urllib.request.Request(
            f"{CLOUD}/api/skills/usage",
            data=json.dumps({"agentId": agent, "events": chunk}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.load(resp)
        except Exception as e:
            fold_back(events[i:])
            print(
                f"usage-report: POST failed ({e}) after {sent} slug(s) — "
                f"{len(events) - i} unsent slug(s) folded back for retry"
            )
            return 0
        accepted += int(body.get("accepted") or 0)
        skipped += int(body.get("skipped") or 0)
        sent += len(chunk)

    pending.unlink()
    print(f"usage-report: {sent} slug(s) reported — accepted={accepted} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
