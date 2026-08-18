#!/usr/bin/env python3
"""Explicit proactive destinations (owner design 2026-08-18): a destined
filename is claimed ONLY by its target bridge; undestined names keep the
last-activity routing. The grammar must survive every existing discovery
glob and the claim-rename cycle, or a destined file silently re-enters
the race this feature exists to end.

Run: python3 tests/proactive-destination.test.py"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import fnmatch
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Hermetic (module level, before any bridge import): the slack bridge resolves
# channel config at import — point it at a seeded temp dir, never the real one.
import os

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="proactive-dest-ccd-")
_SLACK_CFG = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_SLACK_CFG.mkdir(parents=True, exist_ok=True)
(_SLACK_CFG / "access.json").write_text('{"allowFrom": ["U000TEST"]}')
os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="proactive-dest-ws-")
os.environ.setdefault("SUTANDO_TEST_MODE", "1")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")

from proactive_routing import (PROACTIVE_DESTINATIONS, fallback_claims_name,
                               proactive_destination,
                               proactive_filename,
                               should_claim_proactive_file)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def _state(tmp, channel):
    p = Path(tmp) / "last-owner-activity.json"
    p.write_text(json.dumps({"channel": channel, "ts": 1}))
    return p


def main() -> int:
    # Grammar round-trip + constructor is the only legal spelling.
    n = proactive_filename(1234, "discord")
    check("round-trip", proactive_destination(n) == "discord", n)
    check("undestined round-trips to None",
          proactive_destination(proactive_filename(1234)) is None)
    try:
        proactive_filename(1, "smoke-signal")
        check("unknown destination refused at construction", False)
    except ValueError:
        check("unknown destination refused at construction", True)
    check("every bridge channel is a legal destination",
          all(proactive_destination(proactive_filename(1, c)) == c
              for c in PROACTIVE_DESTINATIONS))

    # Discovery compatibility: every existing poller's filter still sees it.
    check("gateway glob still discovers destined files",
          fnmatch.fnmatch(n, "proactive-*.txt"))
    check("telegram prefix+suffix filter still discovers destined files",
          n.startswith("proactive-") and Path(n).suffix == ".txt")

    # Claim-rename cycle preserves the tag (with_suffix touches only .txt).
    claimed = Path(n).with_suffix(".sending.4242")
    check("claim rename keeps the destination tag",
          ".to-discord." in claimed.name)
    # Model the PRODUCTION recovery expression (both recovery sites use
    # name.split(".sending")[0] + ".txt"), not with_suffix.
    recovered = claimed.name.split(".sending")[0] + ".txt"
    check("recovery rename restores a claimable destined name",
          recovered == n and proactive_destination(recovered) == "discord")

    # Per-file decision: destination outranks activity routing BOTH ways.
    with tempfile.TemporaryDirectory() as td:
        st = _state(td, "ag2space")
        check("destined file claimed by target even when activity is elsewhere",
              should_claim_proactive_file(n, st, "discord") is True)
        check("destined file refused to the activity-preferred bridge",
              should_claim_proactive_file(n, st, "ag2space") is False)
        legacy = proactive_filename(1234)
        check("undestined file follows activity routing (preferred claims)",
              should_claim_proactive_file(legacy, st, "ag2space") is True)
        check("undestined file follows activity routing (other declines)",
              should_claim_proactive_file(legacy, st, "discord") is False)
        # A tag this install can't parse to a known channel still blocks all:
        # strand visibly, never leak into the race.
        alien = "proactive-1.to-futurechan.txt"
        check("unrecognized tag reads as a destination (blocks every bridge)",
              all(should_claim_proactive_file(alien, st, c) is False
                  for c in ("discord", "telegram", "ag2space")))

    # slack's filename-level claim predicate (module-loadable without bolt).
    import importlib.util as _ilu
    import types as _types
    for _m in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode"):
        mod = _types.ModuleType(_m)
        if _m.endswith("socket_mode"):
            mod.SocketModeHandler = type("SocketModeHandler", (), {})
        if _m == "slack_bolt":
            mod.App = type("App", (), {"__init__": lambda self, **k: None,
                                       "event": lambda self, *_a, **_k: (lambda f: f)})
        sys.modules[_m] = mod
    _spec = _ilu.spec_from_file_location("slackbridge_dest_test",
                                         REPO / "src" / "slack-bridge.py")
    _sb = _ilu.module_from_spec(_spec)
    try:
        _spec.loader.exec_module(_sb)
        check("slack claims undestined", _sb._slack_claims_name("proactive-1.txt"))
        check("slack claims its own destination",
              _sb._slack_claims_name(proactive_filename(1, "slack")))
        check("slack skips a foreign destination",
              _sb._slack_claims_name(proactive_filename(1, "discord")) is False)
    except Exception as e:  # pragma: no cover — loader env drift, not the predicate
        check("slack bridge loadable for predicate checks", False, str(e))

    # Catch-all fallback gate (discord's poll_dm_fallback wiring): undestined
    # and own-destination names are sweepable; foreign/unknown tags never are.
    check("fallback sweeps undestined cron artifacts",
          fallback_claims_name("briefing-2026-08-18.txt", "discord"))
    check("fallback sweeps its own destination",
          fallback_claims_name("briefing-1.to-discord.txt", "discord"))
    check("fallback never sweeps a foreign destination",
          fallback_claims_name("briefing-1.to-telegram.txt", "discord") is False)
    check("fallback never sweeps an unknown tag",
          fallback_claims_name("insight-1.to-futurechan.txt", "discord") is False)

    # Production writers with declared channel intent emit destined names.
    import importlib.util as _wilu
    _dspec = _wilu.spec_from_file_location(
        "dealfinder_scan_dest_test", REPO / "skills" / "deal-finder" / "scripts" / "scan.py")
    _dmod = _wilu.module_from_spec(_dspec)
    _dspec.loader.exec_module(_dmod)
    # Un-overridden: the writer must target the RESOLVED workspace results/
    # (the dir bridges poll), not the repo-root guess it used to make.
    from workspace_default import resolve_workspace as _rw
    check("deal-finder writes where bridges poll (resolved workspace)",
          _dmod.RESULTS_DIR == _rw() / "results", str(_dmod.RESULTS_DIR))
    with tempfile.TemporaryDirectory() as td:
        _dmod.RESULTS_DIR = Path(td)
        check("deal-finder writes a telegram-destined proactive file",
              _dmod.send_telegram("test body") and any(
                  proactive_destination(f.name) == "telegram"
                  for f in Path(td).iterdir()))
    _rspec = _wilu.spec_from_file_location(
        "harness_report_dest_test",
        REPO / "skills" / "voice-agent-test-harness" / "scripts" / "report.py")
    _rmod = _wilu.module_from_spec(_rspec)
    _rspec.loader.exec_module(_rmod)
    with tempfile.TemporaryDirectory() as td:
        out = Path(_rmod.deliver("report body", workspace=td))
        check("test-harness report is telegram-destined",
              proactive_destination(out.name) == "telegram" and out.exists())

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: proactive destinations — grammar, glob/claim survival, "
          "destination-outranks-activity, visible stranding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
