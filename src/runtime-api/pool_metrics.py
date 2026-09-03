#!/usr/bin/env python3
"""Lead-side pool metrics (slice L4 — the owner's 2026-05-19 quality bar).

The bar demanded a CONTINUOUS benchmark, not one-off bursts: claim
distribution, head-of-line incidents, duplicate-reply risk, per-channel
latency, tracked over time. The lead's global queue view makes this one
append-only JSONL per day plus a summarizer — no cross-worker aggregation.

Recording is fail-open (a metrics error must never break scheduling) and
append-only (multi-writer-safe under O_APPEND, same contract as build_log).
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


class PoolMetrics:
    def __init__(self, state_dir, now_fn=time.time):
        self.dir = Path(state_dir) / "pool" / "metrics"
        self.now = now_fn

    def _path(self) -> Path:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        return self.dir / f"pool-{day}.jsonl"

    def record(self, event: str, **fields) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            row = {"ts": round(self.now(), 3), "event": event, **fields}
            with open(self._path(), "a") as f:
                f.write(json.dumps(row) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass  # fail-open: scheduling must not depend on metrics I/O

    # Convenience hooks the lead/follower call sites use.
    def assigned(self, task: str, instance: str, channel, wait_s: float):
        self.record("assigned", task=task, instance=instance,
                    channel=channel, wait_s=round(wait_s, 3))

    def claimed(self, task: str, instance: str, fallback: bool):
        self.record("claimed", task=task, instance=instance,
                    fallback=fallback)

    def reclaimed(self, task: str, instance: str, reason: str = "dead"):
        # reason: dead (assignment), stuck (never claimed), claim-dead (repooled claim)
        self.record("reclaimed", task=task, instance=instance, reason=reason)

    # ── the summary the benchmark reads ─────────────────────────────────────
    def summarize(self, day: "str | None" = None,
                  head_of_line_s: float = 120.0,
                  continuity_window_s: float = 1800.0) -> dict:
        """Distribution + incident counts for one day's log. head-of-line
        incident = a task that waited longer than `head_of_line_s` before
        assignment (queue starvation, the #884 symptom the bar names).
        Continuity break = two same-channel assignments within
        `continuity_window_s` landing on different cores — the measured cost
        of every affinity yield, paired against wait time to tune the
        busy threshold from data instead of feel."""
        if day:
            path = self.dir / f"pool-{day}.jsonl"
        else:
            path = self._path()
        dist: Counter = Counter()
        per_channel: dict = defaultdict(list)
        chan_seq: dict = defaultdict(list)  # channel -> [(ts, instance)]
        incidents = fallback_claims = reclaims = rows = bad = 0
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return {"rows": 0, "note": f"no metrics at {path.name}"}
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            rows += 1
            ev = r.get("event")
            if ev == "assigned":
                dist[r.get("instance", "?")] += 1
                w = float(r.get("wait_s") or 0)
                if r.get("channel"):
                    per_channel[r["channel"]].append(w)
                    chan_seq[r["channel"]].append(
                        (float(r.get("ts") or 0), r.get("instance", "?")))
                if w > head_of_line_s:
                    incidents += 1
            elif ev == "claimed" and r.get("fallback"):
                fallback_claims += 1
            elif ev == "reclaimed":
                reclaims += 1
        chan_latency = {c: round(sum(v) / len(v), 3)
                        for c, v in per_channel.items() if v}
        breaks_by_channel: Counter = Counter()
        pairs = 0
        for c, seq in chan_seq.items():
            # by TIME only, stable: sorting the (ts, instance) tuple would
            # reorder equal-timestamp rows by worker name and invent switches
            seq.sort(key=lambda r: r[0])
            for (t0, i0), (t1, i1) in zip(seq, seq[1:]):
                if t1 - t0 > continuity_window_s:
                    continue
                pairs += 1
                if i0 != i1:
                    breaks_by_channel[c] += 1
        return {"rows": rows, "bad_lines": bad,
                "assignment_distribution": dict(dist),
                "head_of_line_incidents": incidents,
                "fallback_claims": fallback_claims,
                "reclaims": reclaims,
                "mean_wait_by_channel": chan_latency,
                "continuity_breaks": sum(breaks_by_channel.values()),
                "continuity_pairs": pairs,
                "continuity_breaks_by_channel": dict(breaks_by_channel)}


def summarize_cli(argv: "list[str]") -> int:
    """`pool_metrics.py <state_dir> [YYYY-MM-DD]` — the summary as JSON."""
    if len(argv) not in (1, 2):
        print("usage: pool_metrics.py <state_dir> [YYYY-MM-DD]", file=sys.stderr)
        return 2
    print(json.dumps(PoolMetrics(argv[0]).summarize(*argv[1:]),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(summarize_cli(sys.argv[1:]))
