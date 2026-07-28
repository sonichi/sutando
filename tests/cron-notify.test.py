#!/usr/bin/env python3
"""Unit tests for skills/schedule-crons/cron-notify.py — the pure decision/format
half of the cron-room → owner-active-channel ping (Track 13a) plus the CLI's
non-network paths (dry-run, suppression exits). Network delivery (_post_to_room)
is deliberately NOT exercised here — it rides the same gateway op:message path
every cron uses and is covered by live use."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cron_notify", REPO / "skills" / "schedule-crons" / "cron-notify.py")
cn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cn)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {extra}")
        FAILURES.append(name)


def main() -> int:
    # ── is_attention_worthy ──────────────────────────────────────────────
    check("routine is silent", not cn.is_attention_worthy("routine", "big news"))
    check("unknown kind is silent", not cn.is_attention_worthy("banana", "big news"))
    check("non-str kind is silent", not cn.is_attention_worthy(None, "x"))
    check("owner_action pings", cn.is_attention_worthy("owner_action", "need a decision"))
    check("digest with news pings", cn.is_attention_worthy("digest", "3 PRs merged"))
    check("digest with no news is downgraded",
          not cn.is_attention_worthy("digest", "Nothing new this pass"))
    check("error always pings — even a 'nothing new' error",
          cn.is_attention_worthy("error", "nothing new but the cron crashed"))
    check("empty summary digest still pings (no empty-signal match)",
          cn.is_attention_worthy("digest", ""))

    # ── deep_link ────────────────────────────────────────────────────────
    check("room-only link",
          cn.deep_link("!r:ag2.space") == "https://matrix.to/#/!r:ag2.space?via=ag2.space")
    check("room+event link",
          cn.deep_link("!r:ag2.space", "$e") == "https://matrix.to/#/!r:ag2.space/$e?via=ag2.space")
    check("empty via drops query",
          cn.deep_link("!r:ag2.space", via="") == "https://matrix.to/#/!r:ag2.space")

    # ── format_ping ──────────────────────────────────────────────────────
    p = cn.format_ping("pr-shepherd", "  two\n lines\t here ", "!r:ag2.space", "$e")
    check("whitespace collapsed", "two lines here" in p, p)
    check("ping carries cron name + link",
          p.startswith("⏰ pr-shepherd: ") and "matrix.to/#/!r:ag2.space/$e" in p, p)
    long = cn.format_ping("c", "x" * 300, "!r:ag2.space")
    head = long.split(" → ")[0]
    check("long summary truncated with ellipsis",
          head.endswith("…") and len(head) < 160, f"len={len(head)}")

    # ── should_ping_now / record_ping ────────────────────────────────────
    check("never-pinged cron passes", cn.should_ping_now({}, "c", now=10_000))
    check("recent ping is rate-limited",
          not cn.should_ping_now({"c": 9_000}, "c", now=10_000, min_interval_s=1800))
    check("old ping passes",
          cn.should_ping_now({"c": 1_000}, "c", now=10_000, min_interval_s=1800))
    check("exactly-at-interval passes",
          cn.should_ping_now({"c": 8_200}, "c", now=10_000, min_interval_s=1800))
    check("non-numeric stored value fails open", cn.should_ping_now({"c": "bogus"}, "c", 10_000))
    check("negative stored value fails open", cn.should_ping_now({"c": -5}, "c", 10_000))
    check("non-dict state fails open", cn.should_ping_now(None, "c", 10_000))
    st = cn.record_ping({}, "c", 123.9)
    check("record_ping stamps int", st == {"c": 123})
    check("record_ping tolerates non-dict", cn.record_ping(None, "c", 5) == {"c": 5})

    # ── CLI non-network paths ────────────────────────────────────────────
    rc = cn.main(["--cron", "c", "--summary", "s", "--kind", "routine", "--room", "!r:x"])
    check("CLI: routine kind suppressed exit 3", rc == 3)

    with tempfile.TemporaryDirectory() as d:
        sf = Path(d) / "state.json"
        sf.write_text(json.dumps({"c": 9_000}))
        rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "10000"])
        check("CLI: rate-limited exit 3", rc == 3)
        rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                      "--room", "!r:x", "--state-file", str(sf), "--now", "20000",
                      "--dry-run"])
        check("CLI: dry-run posts nothing, exit 0", rc == 0)
        check("CLI: dry-run does not stamp state",
              json.loads(sf.read_text()) == {"c": 9_000})

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
