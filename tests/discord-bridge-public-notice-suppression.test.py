#!/usr/bin/env python3
"""The auto-seed notice and the sandbox-fallback sentinel must never reach a
public channel: DM-only with no public fallback, swallowed for non-DM."""

from __future__ import annotations

import asyncio
import pathlib
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolate CLAUDE_CONFIG_DIR BEFORE the bridge is exec'd so this test never
# reads or writes the host's real channel config (per the #2428/#2429 rule).
_CFG_DIR = tempfile.mkdtemp(prefix="dbps-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG_DIR
_CFG = Path(_CFG_DIR)
_env_dir = _CFG / "channels" / "discord"
_env_dir.mkdir(parents=True, exist_ok=True)
(_env_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
# access.json too: channel_access_path() falls back to the real ~/.claude copy
# when the canonical file is absent, so a token stub alone is not isolation.
(_env_dir / "access.json").write_text(
    '{"allowFrom": ["111"], "tierMap": {"111": "owner"}}\n')

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
    exec(compile(src, bridge.__file__, "exec"), bridge.__dict__)
    return bridge


bridge = load_bridge()
SENTINEL = "Sandbox unavailable; refusing non-owner task."


# --- Fakes for the async DM paths

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


# --- is_sandbox_fallback_sentinel — single source of truth

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


# --- _is_sandbox_fallback_result

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


# --- _send_seed_notice_to_owner

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
    # The helper must not swallow (its call site logs) and must never fall
    # back to a public post.
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


# --- _notify_owner_sandbox_suppressed

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


# --- _format_seed_notice — DM-ready body

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


# --- BEHAVIOURAL: one real pass of poll_results over the guard's call site

class _Stop(Exception):
    """Breaks the poll loop after exactly one pass."""


class _GuildChan:
    id = 4242

    def __init__(self):
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)


def _run_one_poll_pass(body: str, chan):
    """Drive poll_results once with `body` queued for `chan` -> (results, id).
    The loop's own sleep raises, which is how the repo bounds its `while True`."""
    td = tempfile.mkdtemp(prefix="dbps-poll-")
    results, tasks = Path(td) / "results", Path(td) / "tasks"
    (results / "archive").mkdir(parents=True)
    (tasks / "archive").mkdir(parents=True)
    task_id = "task-1786000000000"
    (tasks / f"{task_id}.txt").write_text(
        f"id: {task_id}\nsource: discord\naccess_tier: other\ntask: hi\n")
    (results / f"{task_id}.txt").write_text(body)

    bridge.RESULTS_DIR, bridge.TASKS_DIR = results, tasks
    bridge.ARCHIVE_RESULTS_DIR = results / "archive"
    bridge.ARCHIVE_TASKS_DIR = tasks / "archive"

    class _PollClient:
        def is_ready(self):
            return False

        async def fetch_user(self, uid):
            raise RuntimeError("no owner DM in this driver")

    bridge.client = _PollClient()
    bridge._recovered_replies = {}
    bridge.pending_replies.clear()
    bridge.pending_replies[task_id] = chan
    bridge.save_pending_replies = lambda *a, **k: None

    async def _sleep(_s):
        raise _Stop()

    orig_sleep = bridge.asyncio.sleep
    bridge.asyncio.sleep = _sleep
    try:
        asyncio.run(bridge.poll_results())
    except _Stop:
        pass
    finally:
        bridge.asyncio.sleep = orig_sleep
    return results, task_id


def case_poll_loop_swallows_sentinel() -> list[str]:
    """The guard's call site: a sentinel body bound for a guild channel must be
    archived without a send, which is the branch the extracted helper cannot reach."""
    fails = []
    chan = _GuildChan()
    results, task_id = _run_one_poll_pass(SENTINEL, chan)
    if chan.sent:
        fails.append(f"i) sentinel was posted to the guild channel: {chan.sent!r}")
    if (results / f"{task_id}.txt").exists():
        fails.append("i) swallowed result must be archived out of results/")
    # archive_path() partitions by year-month, so match the name, not the path.
    if not list((results / "archive").rglob(f"{task_id}.txt")):
        fails.append("i) swallowed result must land under results/archive/")
    return fails


def case_poll_loop_delivers_normal_prose() -> list[str]:
    """The same call site must not swallow an ordinary answer — otherwise the
    guard would pass by suppressing everything."""
    fails = []
    chan = _GuildChan()
    _run_one_poll_pass("here is a normal answer", chan)
    if not any("normal answer" in s for s in chan.sent):
        fails.append(f"j) ordinary reply must still be delivered, got {chan.sent!r}")
    return fails


# --- Source-grep: no public post of the seed notice; guard wired into poll_results

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
    # The delivery loop must consult the guard. After the extraction the
    # predicate lives in the helper, so assert the CALL SITE, not the predicate.
    if "await _swallow_sandbox_fallback(" not in src:
        fails.append("h) delivery loop must gate on _swallow_sandbox_fallback")
    return fails



# --- _swallow_sandbox_fallback / _maybe_notify_owner_of_seed — the extracted blocks

def case_swallow_guard() -> list[str]:
    fails = []
    archived = []
    notified = []

    async def _fake_notify(channel, task_id):
        notified.append(task_id)

    class _Chan:
        id = 424242

    # Restore the real functions: these are module globals, so leaving stubs
    # behind silently disarms any later test that drives the real loop.
    _orig = (bridge.archive_file, bridge._record_skip_audit,
             bridge._notify_owner_sandbox_suppressed)
    bridge.archive_file = lambda p, kind, tid: archived.append((kind, tid))
    bridge._record_skip_audit = lambda tid, why: archived.append(("audit", why))
    bridge._notify_owner_sandbox_suppressed = _fake_notify
    try:
        # Guild destination + sentinel -> swallowed, archived, owner notified.
        got = asyncio.run(bridge._swallow_sandbox_fallback(
            _Chan(), "task-s1", pathlib.Path("/tmp/nonexistent-result.txt"), SENTINEL, False))
        if got is not True:
            fails.append("i) guild sentinel must be swallowed")
        if "task-s1" not in notified:
            fails.append("i) owner must be notified when swallowing")
        if ("audit", "no-send") not in archived:
            fails.append("i) swallow must record a no-send skip audit")

        # DM destination -> delivered as before.
        if asyncio.run(bridge._swallow_sandbox_fallback(
                _Chan(), "task-s2", pathlib.Path("/tmp/x.txt"), SENTINEL, True)) is not False:
            fails.append("i) DM destination must NOT be swallowed")

        # Ordinary prose -> delivered.
        if asyncio.run(bridge._swallow_sandbox_fallback(
                _Chan(), "task-s3", pathlib.Path("/tmp/x.txt"), "a normal answer", False)) is not False:
            fails.append("i) ordinary body must NOT be swallowed")
    finally:
        (bridge.archive_file, bridge._record_skip_audit,
         bridge._notify_owner_sandbox_suppressed) = _orig
    return fails


def case_maybe_notify_seed() -> list[str]:
    fails = []
    sent = []

    async def _fake_send(owner_id, notice):
        sent.append((owner_id, notice))

    bridge._send_seed_notice_to_owner = _fake_send
    access = {"allowFrom": ["team-user", "owner-user"],
              "tierMap": {"team-user": "team", "owner-user": "owner"}}

    # Team-tier seeder -> owner IS notified, and the DM targets the canonical owner.
    if asyncio.run(bridge._maybe_notify_owner_of_seed(
            access, "team-user", "@team", "#parent", "9001")) is not True:
        fails.append("j) team-tier seeder must notify the owner")
    if not sent or sent[-1][0] != "owner-user":
        fails.append(f"j) DM must target the canonical owner, got {sent[-1][0] if sent else None}")

    # Owner seeding their own thread -> no ping.
    if asyncio.run(bridge._maybe_notify_owner_of_seed(
            access, "owner-user", "@owner", "#parent", "9002")) is not False:
        fails.append("j) owner seeder must NOT notify")

    # Delivery failure is log-only, never raises.
    async def _boom(owner_id, notice):
        raise RuntimeError("dm down")

    bridge._send_seed_notice_to_owner = _boom
    if asyncio.run(bridge._maybe_notify_owner_of_seed(
            access, "team-user", "@team", "#parent", "9003")) is not False:
        fails.append("j) a failed DM must report False, not raise")
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
        ("i-swallow-guard", case_swallow_guard),
        ("j-maybe-notify-seed", case_maybe_notify_seed),
        ("k-poll-loop-swallows", case_poll_loop_swallows_sentinel),
        ("l-poll-loop-delivers", case_poll_loop_delivers_normal_prose),
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
