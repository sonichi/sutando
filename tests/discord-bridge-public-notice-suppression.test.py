#!/usr/bin/env python3
"""
Tests for the public-channel internal-notice suppression in discord-bridge.py.

2026-07-29 AG2-community incident: two classes of bridge-internal plumbing
posted into a PUBLIC community thread —
  1. the "🌱 Auto-seeded … access.json" owner-visibility notice (posted into
     the very thread it seeded), and
  2. the Stage-2 sandbox fallback body "Sandbox unavailable; refusing
     non-owner task." (written to results/ by the tier instructions, then
     delivered to the originating channel by poll_results with no check).

The fix routes the seed notice to the owner's DM (no public fallback) and
makes poll_results swallow the sandbox sentinel for non-DM destinations
(no-send archive + best-effort owner DM).

Under test:
  - is_sandbox_fallback_sentinel(): single source of truth — the tier-instruction
    templates must render the exact same literal the guard checks.
  - _is_sandbox_fallback_result(body, is_dm): pure suppression predicate
  - _send_seed_notice_to_owner(owner_id, notice): DM-only delivery
  - _notify_owner_sandbox_suppressed(channel, task_id): best-effort, never raises
  - _format_seed_notice: still carries owner mention / author / parent / undo,
    plus the <#thread> self-location a DM needs
  - source-grep: the thread-engage block must NOT send the seed notice to
    message.channel anymore

Run: python3 tests/discord-bridge-public-notice-suppression.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolate CLAUDE_CONFIG_DIR BEFORE the bridge is exec'd so this test never
# reads or writes the host's real channel config (per the #2428/#2429 rule).
_CFG = Path(tempfile.mkdtemp(prefix="dbps-cfg-"))
os.environ["CLAUDE_CONFIG_DIR"] = str(_CFG)
_env_dir = _CFG / "channels" / "discord"
_env_dir.mkdir(parents=True, exist_ok=True)
(_env_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")

# Stub minimal discord module BEFORE the bridge is exec'd.
_discord_stub = types.ModuleType("discord")


class _Intents:
    @classmethod
    def default(cls):
        i = cls()
        i.message_content = False
        i.members = False
        return i


class _Client:
    def __init__(self, *args, **kwargs):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)

    def event(self, fn):
        return fn

    def get_channel(self, _id):
        return None


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None
_discord_stub.DMChannel = type("_DMChannel", (), {})
_discord_stub.Thread = type("_Thread", (), {})
sys.modules["discord"] = _discord_stub


def load_bridge():
    """Exec the bridge module without running its main()."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(REPO / "src" / "discord-bridge.py")
    exec(src, bridge.__dict__)
    return bridge


bridge = load_bridge()
SENTINEL = "Sandbox unavailable; refusing non-owner task."


# ---------------------------------------------------------------------------
# Fakes for the async DM paths
# ---------------------------------------------------------------------------

class _FakeDM:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class _FakeUser:
    def __init__(self, dm):
        self._dm = dm
        self.bot = False

    async def create_dm(self):
        return self._dm


class _FakeClient:
    def __init__(self, dm, fail=False):
        self._dm = dm
        self._fail = fail
        self.fetched = []

    async def fetch_user(self, uid):
        self.fetched.append(uid)
        if self._fail:
            raise RuntimeError("fetch_user down")
        return _FakeUser(self._dm)


# ---------------------------------------------------------------------------
# is_sandbox_fallback_sentinel — single source of truth
# ---------------------------------------------------------------------------

def case_sentinel_constant() -> list[str]:
    fails = []
    # The recogniser is the contract now, not one constant: main replaced the
    # single sentinel with a legacy literal plus two generated forms.
    if not bridge.is_sandbox_fallback_sentinel(SENTINEL):
        fails.append("a) legacy sentinel must still be recognised")
    if not bridge.is_sandbox_fallback_sentinel(
            "Sandbox unavailable (codex exit 3) — no reply generated."):
        fails.append("a) nonzero-exit sentinel must be recognised")
    if not bridge.is_sandbox_fallback_sentinel(
            "Sandbox unavailable (codex exited 0 with no output) — no reply generated."):
        fails.append("a) no-output sentinel must be recognised")
    # Prefix-only prose is ordinary content and must NOT be suppressed.
    if bridge.is_sandbox_fallback_sentinel(
            "Sandbox unavailable after upgrading — can you diagnose it?"):
        fails.append("a) prefix-matching prose must not be treated as a sentinel")
    return fails


# ---------------------------------------------------------------------------
# _is_sandbox_fallback_result
# ---------------------------------------------------------------------------

def case_predicate() -> list[str]:
    fails = []
    if not bridge._is_sandbox_fallback_result(SENTINEL, False):
        fails.append("b) sentinel to a guild channel must suppress")
    if not bridge._is_sandbox_fallback_result(f"  {SENTINEL}\n", False):
        fails.append("b) surrounding whitespace must not defeat the guard")
    # Exact match, per main's is_sandbox_fallback_sentinel: a prefix rule would
    # archive ordinary prose that merely opens with the same words.
    if bridge._is_sandbox_fallback_result(SENTINEL + " (extra)", False):
        fails.append("b) exact-match: wrapped text is prose, not a sentinel")
    if bridge._is_sandbox_fallback_result(
            "Sandbox unavailable after upgrading — can you diagnose it?", False):
        fails.append("b) prefix-matching prose must deliver, not be suppressed")
    if bridge._is_sandbox_fallback_result(SENTINEL, True):
        fails.append("b) DM destination keeps current behavior (deliver)")
    if bridge._is_sandbox_fallback_result("a normal answer", False):
        fails.append("b) normal bodies must deliver")
    if bridge._is_sandbox_fallback_result("", False):
        fails.append("b) empty body is not the sentinel")
    if bridge._is_sandbox_fallback_result(None, False):
        fails.append("b) None body is not the sentinel")
    return fails


# ---------------------------------------------------------------------------
# _send_seed_notice_to_owner
# ---------------------------------------------------------------------------

def case_seed_notice_dm() -> list[str]:
    fails = []
    dm = _FakeDM()
    orig = bridge.client
    bridge.client = _FakeClient(dm)
    try:
        asyncio.run(bridge._send_seed_notice_to_owner("111", "notice-body"))
    finally:
        bridge.client = orig
    if dm.sent != ["notice-body"]:
        fails.append("c) seed notice must be DM'd to the owner verbatim")
    return fails


def case_seed_notice_dm_failure_propagates() -> list[str]:
    # The call site wraps this in try/except and logs; the helper itself must
    # NOT swallow — and must never fall back to a public post (nothing else
    # is sent anywhere on failure).
    fails = []
    dm = _FakeDM()
    orig = bridge.client
    bridge.client = _FakeClient(dm, fail=True)
    try:
        try:
            asyncio.run(bridge._send_seed_notice_to_owner("111", "notice-body"))
            fails.append("d) DM failure should propagate to the logging call site")
        except RuntimeError:
            pass
    finally:
        bridge.client = orig
    if dm.sent:
        fails.append("d) nothing may be sent anywhere when the owner DM fails")
    return fails


# ---------------------------------------------------------------------------
# _notify_owner_sandbox_suppressed
# ---------------------------------------------------------------------------

def _with_access(tmp_access: dict | None):
    """Point the bridge at a temp ACCESS_FILE (or a missing one)."""
    import json
    d = Path(tempfile.mkdtemp(prefix="dbps-acc-"))
    p = d / "access.json"
    if tmp_access is not None:
        p.write_text(json.dumps(tmp_access))
    return p


def case_suppress_notice_dm() -> list[str]:
    fails = []
    dm = _FakeDM()
    chan = types.SimpleNamespace(id=424242)
    orig_client, orig_access = bridge.client, bridge.ACCESS_FILE
    bridge.client = _FakeClient(dm)
    bridge.ACCESS_FILE = _with_access({"allowFrom": ["111"]})
    # SUTANDO_DM_OWNER_ID is resolve_owner_id's step-1 override — deterministic
    # regardless of any host discord-config.json this test must not depend on.
    os.environ["SUTANDO_DM_OWNER_ID"] = "111"
    try:
        asyncio.run(bridge._notify_owner_sandbox_suppressed(chan, "task-x1"))
    finally:
        os.environ.pop("SUTANDO_DM_OWNER_ID", None)
        bridge.client, bridge.ACCESS_FILE = orig_client, orig_access
    if len(dm.sent) != 1:
        fails.append("e) owner must get exactly one suppression DM")
    else:
        body = dm.sent[0]
        if "424242" not in body:
            fails.append("e) DM must name the public channel")
        if "task-x1" not in body:
            fails.append("e) DM must name the task id")
        if SENTINEL not in body:
            fails.append("e) DM should say what was suppressed")
    return fails


def case_suppress_notice_never_raises() -> list[str]:
    fails = []
    chan = types.SimpleNamespace(id=1)
    orig_client, orig_access = bridge.client, bridge.ACCESS_FILE
    # 1) no access file → no owner → returns quietly
    bridge.ACCESS_FILE = _with_access(None)
    bridge.client = _FakeClient(_FakeDM())
    try:
        asyncio.run(bridge._notify_owner_sandbox_suppressed(chan, "task-x2"))
    except Exception as e:
        fails.append(f"f) unresolvable owner must not raise: {e}")
    # 2) fetch_user blows up → still swallowed
    bridge.ACCESS_FILE = _with_access({"allowFrom": ["111"]})
    bridge.client = _FakeClient(_FakeDM(), fail=True)
    os.environ["SUTANDO_DM_OWNER_ID"] = "111"
    try:
        asyncio.run(bridge._notify_owner_sandbox_suppressed(chan, "task-x3"))
    except Exception as e:
        fails.append(f"f) DM failure must not raise (suppression must complete): {e}")
    finally:
        os.environ.pop("SUTANDO_DM_OWNER_ID", None)
        bridge.client, bridge.ACCESS_FILE = orig_client, orig_access
    return fails


# ---------------------------------------------------------------------------
# _format_seed_notice — DM-ready body
# ---------------------------------------------------------------------------

def case_seed_notice_body() -> list[str]:
    fails = []
    body = bridge._format_seed_notice("111", "<@999>", "#general", "555")
    if "<@111>" not in body:
        fails.append("g) notice must mention the owner")
    if "<@999>" not in body:
        fails.append("g) notice should name the seeding author")
    if "#general" not in body:
        fails.append("g) notice should name the parent channel")
    if "<#555>" not in body:
        fails.append("g) DM'd notice must self-locate via a thread mention")
    if "group rm 555" not in body:
        fails.append("g) notice should keep the group-rm undo affordance")
    return fails


# ---------------------------------------------------------------------------
# Source-grep: no public post of the seed notice; guard wired into poll_results
# ---------------------------------------------------------------------------

def case_source_wiring() -> list[str]:
    fails = []
    src = (REPO / "src" / "discord-bridge.py").read_text()
    # The thread-engage block must route through the DM helper, and the old
    # in-thread post must be gone.
    if "await _send_seed_notice_to_owner(" not in src:
        fails.append("h) seed notice must go through _send_seed_notice_to_owner")
    if "message.channel.send(\n                                _format_seed_notice" in src \
            or "message.channel.send(_format_seed_notice" in src:
        fails.append("h) seed notice must NOT be posted to the seeded thread")
    # poll_results must consult the predicate before delivering.
    if "_is_sandbox_fallback_result(reply_text" not in src:
        fails.append("h) poll_results must gate delivery on _is_sandbox_fallback_result")
    return fails


def main() -> int:
    cases = [
        ("a-sentinel-constant", case_sentinel_constant),
        ("b-predicate", case_predicate),
        ("c-seed-dm", case_seed_notice_dm),
        ("d-seed-dm-failure", case_seed_notice_dm_failure_propagates),
        ("e-suppress-dm", case_suppress_notice_dm),
        ("f-suppress-never-raises", case_suppress_notice_never_raises),
        ("g-notice-body", case_seed_notice_body),
        ("h-source-wiring", case_source_wiring),
    ]
    failures: list[str] = []
    for name, fn in cases:
        try:
            fails = fn()
        except Exception as e:  # a crashed case is a failure, not a skip
            fails = [f"{name} crashed: {type(e).__name__}: {e}"]
        for f in fails:
            failures.append(f"[{name}] {f}")
    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print(f"OK — {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
