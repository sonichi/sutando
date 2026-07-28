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

    # ── gateway config + delivery (urllib mocked — no network) ──────────
    import unittest.mock as um
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text("OTHER=1\nAG2_REMOTE_TOKEN='https://x.example/relay|sekret'\n")
        base, secret = cn._load_gateway(str(env))
        check("_load_gateway parses quoted url|secret",
              base == "https://x.example/relay" and secret == "sekret", (base, secret))
        env.write_text("AG2_REMOTE_TOKEN=nopipe\n")
        check("_load_gateway no-pipe → (None, None)", cn._load_gateway(str(env)) == (None, None))
        check("_load_gateway missing file → (None, None)",
              cn._load_gateway(str(Path(d) / "absent.env")) == (None, None))

        env.write_text("AG2_REMOTE_TOKEN=https://x.example/relay|sekret\n")
        resp = um.MagicMock()
        resp.read.return_value = json.dumps({"event_id": "$evt"}).encode()
        with um.patch.object(cn, "_load_gateway", return_value=("https://x.example/relay", "sekret")), \
             um.patch("urllib.request.urlopen", return_value=resp) as uo:
            eid = cn._post_to_room("!r:x", "hello", str(env))
            check("_post_to_room returns event_id on 200", eid == "$evt", eid)
            req = uo.call_args[0][0]
            check("_post_to_room targets <base>/v1/room",
                  req.full_url == "https://x.example/relay/v1/room", req.full_url)
        import urllib.error as ue
        with um.patch.object(cn, "_load_gateway", return_value=("https://x.example/relay", "s")), \
             um.patch("urllib.request.urlopen", side_effect=ue.URLError("down")):
            check("_post_to_room URLError → None", cn._post_to_room("!r:x", "hi", str(env)) is None)
        with um.patch.object(cn, "_load_gateway", return_value=(None, None)):
            check("_post_to_room no gateway → None", cn._post_to_room("!r:x", "hi") is None)

    check("_load_state missing file → {}", cn._load_state("/nonexistent/state.json") == {})

    # ── CLI post paths (delivery mocked) ─────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        sf = Path(d) / "state.json"
        with um.patch.object(cn, "_post_to_room", return_value="$evt"):
            rc = cn.main(["--cron", "c", "--summary", "news", "--kind", "digest",
                          "--room", "!r:x", "--state-file", str(sf), "--now", "50000"])
            check("CLI: successful post exit 0", rc == 0)
            check("CLI: post stamps rate-limit state",
                  json.loads(sf.read_text()) == {"c": 50000}, sf.read_text())
        with um.patch.object(cn, "_post_to_room", return_value=None):
            rc = cn.main(["--cron", "c2", "--summary", "news", "--kind", "error",
                          "--room", "!r:x"])
            check("CLI: failed post exit 2", rc == 2)

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
