#!/usr/bin/env python3
"""A proactive Slack body led by [channel: <id>] is delivered to <id>, stripped,
and guarded at <id>'s audience tier (#4971).

Drives the real `result_watcher` loop and `_send_reply`: the redirect and attach
markers come from `parse_markers`, the delivery log names the real destination,
and the guard is the shared `guard_proactive_for_destination`.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# MODULE level, before any exec_module: the bridge resolves ACCESS_FILE during
# import and falls back to the real ~/.claude when CLAUDE_CONFIG_DIR is unset.
_CCD = tempfile.mkdtemp(prefix="sutando-slack-redirect-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CCD
_cfg_slack = Path(_CCD) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


class _RecordingClient:
    def __init__(self):
        self.calls = []
        self.opened_for = None

    def conversations_open(self, **kwargs):
        self.opened_for = kwargs["users"]
        return {"channel": {"id": "D-OWNER"}}

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}

    def conversations_info(self, **kwargs):
        # D0OWNERALT is a second DM whose peer is the owner; every other D is a non-owner's DM.
        peer = "owner-1" if kwargs["channel"] == "D0OWNERALT" else "U-STRANGER"
        return {"channel": {"id": kwargs["channel"], "is_im": True, "user": peer}}


class _FakeApp:
    def __init__(self, token=None):
        self.client = _RecordingClient()

    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


def _load_bridge():
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket.SocketModeHandler = type("SocketModeHandler", (), {})
    sys.modules["slack_bolt.adapter.socket_mode"] = socket

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="sutando-slack-redirect-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location(
        "slackbridge_proactive_redirect_test", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_either(*paths):
    """Return the body from whichever name currently exists.
    exists()-then-read races the watcher's rename between the two calls."""
    for _ in range(100):
        for p in paths:
            try:
                return p.read_text()
            except FileNotFoundError:
                continue
        time.sleep(0.02)
    return None


def _settle(predicate, timeout=4.0, interval=0.1):
    """Poll until predicate holds; the watcher runs on its own cadence."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()



class _InfoClient:
    """conversations_info answers from a table; a missing entry raises like a Slack API error."""

    def __init__(self, table):
        self.table, self.asked = table, []

    def conversations_info(self, **kwargs):
        self.asked.append(kwargs["channel"])
        answer = self.table[kwargs["channel"]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def destination_tier_units(bridge) -> None:
    """Every branch of `_slack_destination_tier`, called directly, before the watcher starts."""
    def im(user, is_im=True):
        return {"channel": {"is_im": is_im, **({"user": user} if user else {})}}

    owner_im = im("owner-1")
    client = _InfoClient({
        "C0SHARED": owner_im, "G0LEGACY": owner_im,
        "D0OWNERPEER": owner_im, "D0TEAMPEER": im("U-TEAM"), "D0STRANGER": im("U-STRANGER"),
        "D0UNREADABLE": RuntimeError("missing_scope"), "D0NOTIM": im("owner-1", is_im=False),
        "D0NOPEER": im(None), "D0NOCHANNEL": {"ok": True},
    })
    real = bridge.app.client
    bridge.app.client = client
    try:
        tier = bridge._slack_destination_tier
        check("tier: a shared channel is other, without asking Slack",
              tier("C0SHARED") == "other" and tier("G0LEGACY") == "other"
              and not {"C0SHARED", "G0LEGACY"} & set(client.asked), repr(client.asked))
        check("tier: a DM whose peer is owner in tierMap is owner", tier("D0OWNERPEER") == "owner")
        check("tier: a DM whose peer is team in tierMap is team", tier("D0TEAMPEER") == "team")
        check("tier: a DM whose peer is not in tierMap is other", tier("D0STRANGER") == "other")
        check("tier: a DM conversations_info cannot read is other (fail closed)",
              tier("D0UNREADABLE") == "other")
        check("tier: a non-IM answer is other, even naming an owner", tier("D0NOTIM") == "other")
        check("tier: an IM with no peer is other", tier("D0NOPEER") == "other")
        check("tier: an answer with no channel is other", tier("D0NOCHANNEL") == "other")
    finally:
        bridge.app.client = real


def main() -> int:
    bridge = _load_bridge()
    results = Path(bridge.RESULTS_DIR)
    access = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    access.write_text(json.dumps({"allowFrom": ["owner-1"],
                                  "tierMap": {"owner-1": "owner", "U-TEAM": "team"},
                                  "tofuOwner": "owner-1"}))
    bridge.ACCESS_FILE = access

    logged: list[str] = []
    real_print = print

    def _capture(*args, **kwargs):
        logged.append(" ".join(str(a) for a in args))
        real_print(*args, **kwargs)

    bridge.print = _capture

    destination_tier_units(bridge)

    missing = "/nonexistent-4971/report.png"
    name = "proactive-redirect-4971.txt"
    (results / name).write_text(f"[channel: D0OTHERDM1]\nhello other DM\n[attach: {missing}]\n")

    threading.Thread(target=bridge.result_watcher, daemon=True).start()
    calls = bridge.app.client.calls
    check("the proactive file was delivered",
          _settle(lambda: len(calls) >= 2 and not (results / name).exists()),
          f"{len(calls)} post(s)")

    channels = {c.get("channel") for c in calls}
    texts = [c.get("text", "") for c in calls]
    check("every post goes to the redirect target, none to the owner's DM",
          channels == {"D0OTHERDM1"}, f"posted to {sorted(map(str, channels))}")
    check("the body arrives without the redirect marker",
          "hello other DM" in texts and not any("[channel:" in t for t in texts), repr(texts))
    check("the attach marker is acted on, not shown",
          not any("[attach:" in t for t in texts), repr(texts))

    sent = [line for line in logged if "[proactive] sent to" in line]
    check("the delivery log names the redirect target",
          len(sent) == 1 and "sent to D0OTHERDM1" in sent[0], repr(sent))
    check("the delivery log shows the parsed body, not the raw markers",
          len(sent) == 1 and "[channel:" not in sent[0], repr(sent))

    from policy.egress.result import TEAM_LEAK_RESULT as NOTICE

    def deliver(name, body):
        start = len(calls)
        (results / name).write_text(body)
        _settle(lambda: len(calls) > start and not (results / name).exists())
        time.sleep(0.2)
        return calls[start:]

    leak = "copy failed: [Errno 2] No such file: /Users/someone/private/plan.md"
    posts = deliver("proactive-leak-other.txt", f"[channel: D0STRANGER]\n{leak}\n")
    check("a redirected internal error reaches the non-owner DM only as the standard notice",
          [p.get("channel") for p in posts] == ["D0STRANGER"]
          and posts[0].get("text") == NOTICE, repr(posts))
    check("the guard ran with the destination's tier, not owner",
          any("withheld proactive" in line and "tier=other" in line for line in logged), "")

    posts = deliver("proactive-leak-owner.txt", f"{leak}\n")
    check("the same body to the owner DM is delivered unchanged",
          [(p.get("channel"), p.get("text")) for p in posts] == [("D-OWNER", leak)], repr(posts))

    posts = deliver("proactive-leak-owner-alt.txt", f"[channel: D0OWNERALT]\n{leak}\n")
    check("a redirect to a DM whose peer is owner-tier is delivered unchanged",
          [(p.get("channel"), p.get("text")) for p in posts] == [("D0OWNERALT", leak)], repr(posts))

    posts = deliver("proactive-leak-room.txt", f"[channel: C0SHARED]\n{leak}\n")
    check("a redirect to a shared channel is guarded too",
          [(p.get("channel"), p.get("text")) for p in posts] == [("C0SHARED", NOTICE)],
          repr(posts))

    # Attachment notices are generated after the guard, by the sender itself.
    private = Path(tempfile.mkdtemp(prefix="slack-redirect-private-")) / "report.png"
    private.write_bytes(b"png")
    missing = "/Users/someone/private/missing.png"
    uploads: list = []
    real_sendable, real_send_file = bridge._is_path_sendable, bridge._send_file
    bridge._is_path_sendable = lambda p: p.endswith("public.png")
    bridge._send_file = lambda ch, ts, p: uploads.append((ch, p)) or True
    try:
        posts = deliver("proactive-notice-other.txt",
                        f"[channel: D0STRANGER]\nweekly summary\n[attach: {private}]\n[attach: {missing}]\n")
        texts = [p.get("text", "") for p in posts]
        check("a redirected body's denied / missing notices name no local path",
              "(file access denied: report.png)" in texts and "(file not found: missing.png)" in texts
              and not any(str(private.parent) in t or "/Users/" in t for t in texts), repr(texts))
        posts = deliver("proactive-notice-owner.txt", f"weekly summary\n[attach: {missing}]\n")
        check("the owner DM still gets the full path in its notice",
              f"(file not found: {missing})" in [p.get("text") for p in posts], repr(posts))
        public = str(private.parent / "public.png")
        deliver("proactive-notice-public.txt", f"[channel: D0STRANGER]\nchart\n[attach: {public}]\n")
        check("an allowlisted attachment to a non-owner destination is still uploaded",
              ("D0STRANGER", public) in uploads, repr(uploads))
    finally:
        bridge._is_path_sendable, bridge._send_file = real_sendable, real_send_file

    src = (REPO / "src" / "slack-bridge.py").read_text()
    check("the bridge delegates to the shared guard, not a copy",
          "guard_proactive_for_destination(" in src and "_INTERNAL_ERROR_RE" not in src)

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PASS: a [channel:] proactive Slack body goes to its target, markers stripped")
    return 0


if __name__ == "__main__":
    rc = main()
    # result_watcher is a daemon thread with no stop condition; exit before finalization races it.
    sys.stdout.flush()
    sys.stderr.flush()
    try:  # os._exit skips coverage's atexit save
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    os._exit(rc)
