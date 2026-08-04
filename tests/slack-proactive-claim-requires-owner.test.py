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

WHY "DON'T CLAIM" RATHER THAN "CLAIM AND RELEASE". On `main` there is no release
path, so a claimed `.sending` is outside every poller's `*.txt` glob until the
startup-only `_recover_orphan_sending_files` sweep. #2627 adds `release_claim()`,
which renames it back and IS re-polled — so after that lands the reason changes
rather than vanishes: claiming what you cannot deliver buys a claim/release hot
race and ~a second of hiding the file from the bridge that can.

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
_slack_cfg = Path(_CFG) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
# Seed the canonical access.json BEFORE the import. Creating the directory is not
# enough: `channel_access_path()` falls back to the real home when the canonical
# file is absent, so an env override alone still reads the developer's live
# allowlist. `lint-hermetic-bridge-tests` requires both, and it caught exactly
# this omission on the first CI run of this PR — the anti-pollution lint catching
# an anti-pollution test, which is the system working.
(_slack_cfg / "access.json").write_text(json.dumps({"allowFrom": []}))

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


def _one_pass(results: Path, access: dict | None, owner: str | None = None,
              sent: list | None = None):
    sb.RESULTS_DIR = results
    acc = results / "access.json"
    if access is None:
        acc.unlink(missing_ok=True)          # the unconfigured-Slack case
    else:
        acc.write_text(json.dumps(access))
    sb.ACCESS_FILE = acc
    if hasattr(sb, "TASKS_DIR"):
        sb.TASKS_DIR = results

    # Positive-control wiring. @john-the-dev mutated `if owner_id is None:` to
    # `if True:` — disabling EVERY Slack proactive claim — and the suite still
    # exited 0, because every case asserted only the no-owner side. A gate that
    # cannot tell "correctly skipped" from "never runs at all" is not a gate.
    if owner is not None:
        sb.resolve_proactive_owner_id = lambda _d: owner
        sb.app.client.conversations_open = lambda **kw: {"channel": {"id": "D_TEST_DM"}}
        def _stub_send(ch, _ts, text, **_kw):
            # MUST return truthy. #2627 changes `_send_reply` from `-> None` to
            # `-> bool` (delivered?) and gates cleanup on it. `list.append()`
            # returns None, so the original stub read as "Slack refused" on the
            # COMBINED tree: the claim was released instead of consumed and this
            # suite failed while passing on either PR alone.
            # @Sutando-Mini found it by running both suites on the merged tree;
            # a clean `git merge` cannot see it, because the contract and the
            # fixture live in different files.
            if sent is not None:
                sent.append((ch, text))
            return True
        sb._send_reply = _stub_send
        sb.mark_proactive_delivered = lambda *a, **kw: None

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

    # --- POSITIVE CONTROL: a CONFIGURED Slack must still claim and deliver ----
    # Without this, an over-broad future gate (or a mutation to `if True:`)
    # silently disables Slack proactive delivery and every assertion above still
    # passes. This is the case that makes the gate discriminating rather than
    # merely permissive.
    box2 = Path(tempfile.mkdtemp(prefix="slack-owner-"))
    msg2 = box2 / "proactive-pending-q-deliverme.txt"
    msg2.write_text("a notification Slack CAN deliver")
    sent: list = []
    _one_pass(box2, access={"allowFrom": ["UOWNER"]}, owner="UOWNER", sent=sent)

    check("configured owner -> the send actually happened", bool(sent),
          "no send recorded — the gate is skipping when it should deliver")
    if sent:
        check("  ...to the opened DM channel with the body",
              sent[0][0] == "D_TEST_DM" and "CAN deliver" in sent[0][1], repr(sent[0]))
    check("  ...and the file was consumed after a successful send",
          not msg2.exists() and not list(box2.glob("*.sending")),
          f"left behind: {[p.name for p in box2.iterdir()]}")

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
