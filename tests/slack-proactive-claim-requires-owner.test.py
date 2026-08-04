#!/usr/bin/env python3
"""Slack must not claim a proactive file it has nobody to deliver to.

THE BUG. `result_watcher` renamed every `results/proactive-*.txt` to `.sending`
at `:1466` and only asked `resolve_proactive_owner_id` thirteen lines later at
`:1479`. On a host where Slack is unconfigured — no `access.json`, TOFU never
ran — that resolve always returns None, so the bridge claimed the file, logged
"no owner in allowFrom, skipping", and deleted it anyway.

Those files are routed to **Discord**. `should_claim_proactive` is never called
here (`grep -c proactive_routing src/slack-bridge.py` -> 0), so Slack was
deleting another bridge's mail, and Discord logged nothing because it never saw
the file. Measured on Chis-MacBook-Pro 2026-08-04: 52 distinct files, including
four morning briefings, each verified absent from the owner's DM history.

WHY "DON'T CLAIM" RATHER THAN "CLAIM AND RELEASE". The poller globs `*.txt`
(`:1443`), so a released `.sending` would sit unread until a restart sweep
(`_recover_orphan_sending_files`, startup only). Leaving the file untouched is
the only variant where the bridge that CAN deliver picks it up on its next tick.

The assertion is deliberately about the FILE, not the log line: a bridge that
logs "skipping" while renaming has still taken the message out of Discord's
glob. Only "the `.txt` is still exactly where it was" proves the message is
still deliverable.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


_CFG = tempfile.mkdtemp(prefix="slack-claim-gate-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")
(Path(_CFG) / "channels" / "slack").mkdir(parents=True, exist_ok=True)

for name in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode",
             "slack_sdk", "slack_sdk.errors"):
    if name not in sys.modules:
        m = types.ModuleType(name)
        if name == "slack_bolt":
            m.App = type("App", (), {"__init__": lambda self, **kw: None,
                                     "event": lambda self, *a, **k: (lambda fn: fn),
                                     "client": types.SimpleNamespace()})
        if name == "slack_bolt.adapter.socket_mode":
            m.SocketModeHandler = type("SocketModeHandler", (),
                                       {"__init__": lambda self, *a, **kw: None})
        if name == "slack_sdk.errors":
            m.SlackApiError = type("SlackApiError", (Exception,), {})
        sys.modules[name] = m


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("slack_bridge_claimgate", REPO / "src" / "slack-bridge.py")
_LIVE_RESULTS = Path(sb.RESULTS_DIR)
_live_before = sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None


class _Tick(Exception):
    """Ends the watcher after one pass.

    The tick sleep sits INSIDE the loop's broad `try`, so this sentinel is caught
    once and printed as `[Slack] result_watcher error:` (empty, because the
    exception carries no message) before the error-path `time.sleep(5)` raises it
    again and it escapes. That line in the output is this sentinel, not a real
    failure — verified by the fact that the per-file assertions below observe the
    post-loop state correctly in both the fixed and the broken run."""


def _one_pass(results: Path, access: dict | None):
    sb.RESULTS_DIR = results
    acc = results / "access.json"
    if access is None:
        acc.unlink(missing_ok=True)          # the unconfigured-Slack case
    else:
        acc.write_text(json.dumps(access))
    sb.ACCESS_FILE = acc
    if hasattr(sb, "TASKS_DIR"):
        sb.TASKS_DIR = results

    def _sleep(_s):
        raise _Tick()

    orig = sb.time.sleep
    sb.time.sleep = _sleep
    try:
        sb.result_watcher()
    except _Tick:
        pass
    except Exception as e:                     # noqa: BLE001 - surfaced, not hidden
        print(f"  (watcher raised {type(e).__name__}: {str(e)[:90]})")
    finally:
        sb.time.sleep = orig


def main() -> int:
    print("slack proactive claim requires an owner:")

    box = Path(tempfile.mkdtemp(prefix="slack-noowner-"))
    msg = box / "proactive-pending-q-keepme.txt"
    BODY = "a notification routed to Discord"
    msg.write_text(BODY)

    _one_pass(box, access=None)                # Slack unconfigured

    check("the .txt is STILL THERE — Discord can still claim it", msg.exists(),
          "file was claimed and/or deleted by a bridge that cannot deliver")
    if msg.exists():
        check("  ...body untouched", msg.read_text() == BODY, "content changed")
    check("  ...and it was never renamed to .sending",
          not list(box.glob("*.sending")),
          f"found {[p.name for p in box.glob('*.sending')]} — out of the poller's *.txt glob")

    check("HERMETIC: operator's real results/ untouched",
          (sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None)
          == _live_before, "the redirect leaked")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All slack claim-gate checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
