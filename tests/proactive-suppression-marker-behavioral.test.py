#!/usr/bin/env python3
"""BEHAVIORAL mutation-resistance proof for the suppression-marker skip check
inside `poll_proactive` and `poll_dm_fallback` (src/discord-bridge.py).

Sibling to `proactive-suppression-marker-honored.test.py`, which pins the
decision (`has_skip_action()`) behaviorally but only pins each call SITE
structurally (source-text: "does the function contain this substring, near
this other substring"). qingyun-wu's second review round showed that is not
enough: `if False and has_skip_action(_pp.actions):` leaves every guarded
substring — `parse_markers`, `kind == "skip"`, `.drop(`, `continue`, their
relative order — intact, so the structural test stays green while a
suppression-marked file falls through to a live send attempt.

This file closes that gap by actually EXECUTING `poll_proactive` and
`poll_dm_fallback` (one real pass each, driven the same way
`proactive-dm-failure-keeps-file-behaviour.test.py` already does — a
sleep-sentinel that raises after one iteration) against a real `[no-send]`
file and a real ordinary file side by side, and observing the one signal
that actually matters: was a send attempted. `check_mutation_makes_it_fail`
is the permanent guard — it applies qingyun's exact mutation to a COPY of
the source, executes THAT, and asserts the observable behavior flips. A
regex over the mutated text would prove nothing; running the mutated code
is the only thing that can.

`proactive_routing` is captured as a real module reference below, before any
per-pass stub replaces `sys.modules["proactive_routing"]`, so
`poll_dm_fallback`'s own runtime `from proactive_routing import ...` still
sees the real module after a `poll_proactive` pass has stubbed it out.

Run: python3 tests/proactive-suppression-marker-behavioral.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import asyncio
import importlib.util
import itertools
import os
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
sys.path.insert(0, str(REPO / "src"))

import proactive_routing as _real_proactive_routing_module  # noqa: E402
from proactive_routing import redirect_target_is_foreign as _real_redirect_target_is_foreign  # noqa: E402

_CFG = tempfile.mkdtemp(prefix="suppression-behavioral-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": ["4242"]}')

try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                      "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

_FAILURES: list[str] = []
_counter = itertools.count()


def fail(msg: str) -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)


class _Sentinel(Exception):
    """Breaks the poll loop after exactly one pass."""


def _load(source_path: Path):
    """Load a fresh discord-bridge module instance from `source_path`."""
    name = f"dbridge_suppression_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, source_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mutated_bridge_source() -> str:
    """discord-bridge.py source with BOTH skip-check sites disabled — the
    exact mutation from qingyun-wu's review, applied to a copy, never the
    real file: `if has_skip_action(...)` -> `if False and has_skip_action(...)`."""
    src = BRIDGE.read_text()
    targets = [
        "if has_skip_action(_pp.actions):",
        "if has_skip_action(_parsed_fb.actions):",
    ]
    for t in targets:
        assert src.count(t) == 1, f"expected exactly one occurrence of {t!r}"
        src = src.replace(t, t.replace("if has_skip_action(", "if False and has_skip_action("))
    return src


def _load_mutated():
    """Write the mutated source to a temp file and load it as a fresh module
    — this EXECUTES the mutation rather than matching it as text."""
    tmp = Path(tempfile.mkdtemp(prefix="suppression-mutated-")) / "discord-bridge.py"
    tmp.write_text(_mutated_bridge_source())
    return _load(tmp)


class _FenceProxy:
    """Wraps the real ProactiveClaimFence and records which terminal call the
    production code reached — `.drop()` (suppressed) vs `.confirm()` (treated
    as sent) — for the ONE file under test.

    `ProactiveClaimFence.drop()` internally calls `self.confirm()` on itself,
    not on this wrapper, so that internal call is invisible here: only calls
    poll_proactive itself makes through `_proactive_fence()` are recorded.
    This is the direct, executable signal for "does the suppression
    predicate control the terminal drop" (qingyun-wu's own wording) —
    independent of whether the body also happens to be empty.
    """

    def __init__(self, real, calls: list[str]):
        self._real = real
        self._calls = calls

    def claim(self, path):
        return self._real.claim(path)

    def confirm(self, claim):
        self._calls.append("confirm")
        return self._real.confirm(claim)

    def drop(self, claim, reason):
        self._calls.append("drop")
        return self._real.drop(claim, reason)

    def release(self, claim):
        return self._real.release(claim)

    def attempts(self, claim):
        return self._real.attempts(claim)

    def fail(self, claim, exc, progressed, undelivered_dir=None):
        return self._real.fail(claim, exc, progressed, undelivered_dir=undelivered_dir)

    def recover(self):
        return self._real.recover()


def _run_proactive_pass(db, results_dir: Path) -> tuple[list[str], list[str]]:
    """One real poll_proactive pass over `results_dir` (a [no-send] file, an
    ordinary file, or both). Returns (delivered_item_ids, fence_terminal_calls)
    — whether the DeliveryProvider was invoked and which fence call landed."""
    db.RESULTS_DIR = results_dir
    db.ACCESS_FILE = Path(_CFG) / "channels" / "discord" / "access.json"
    db.presenter_mode_active = lambda *_a, **_k: False
    db._PROACTIVE_FENCE = None
    fence_calls: list[str] = []
    real_fence = db._proactive_fence()  # constructs, bound to results_dir
    db._PROACTIVE_FENCE = _FenceProxy(real_fence, fence_calls)

    routing = types.ModuleType("proactive_routing")
    routing.should_claim_proactive_file = lambda *_a, **_k: True
    routing.redirect_target_is_foreign = _real_redirect_target_is_foreign
    # The poll path re-checks routing on the body it SENDS, so the stub must
    # carry the real guard or that runtime import fails under the stub.
    routing.proactive_body_guard = _real_proactive_routing_module.proactive_body_guard
    routing.proactive_destination = _real_proactive_routing_module.proactive_destination
    sys.modules["proactive_routing"] = routing

    delivered: list[str] = []

    from ag2_sparrow.delivery_core.contract import (
        DeliveryOutcome, DeliveryReceipt, ProviderCapabilities)

    class _Provider:
        capabilities = ProviderCapabilities()

        def deliver(self, item_id, payload, key):
            delivered.append(item_id)
            return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED, provider_ref="m1")

        def reconcile(self, attempt):
            return None

    db._PROACTIVE_PROVIDER = _Provider()

    class _DM:
        id = 4242

        async def send(self, *a, **kw):
            pass

    class _User:
        bot = False
        name = "owner"

        async def create_dm(self):
            return _DM()

    class _Client:
        async def fetch_user(self, _uid):
            return _User()

        def get_channel(self, _cid):
            return None

        async def fetch_channel(self, _cid):
            raise RuntimeError("no channel resolvable in this harness")

    db.client = _Client()

    async def _sleep(_secs):
        raise _Sentinel()

    orig_sleep = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    try:
        asyncio.run(db.poll_proactive())
    except _Sentinel:
        pass
    finally:
        db.asyncio.sleep = orig_sleep

    return delivered, fence_calls


def _run_dm_fallback_pass(db, results_dir: Path, tasks_dir: Path) -> list[list[str]]:
    """One real poll_dm_fallback pass over `results_dir`/`tasks_dir`. Returns
    the argv of every dm-result.py subprocess invocation — whichever files
    were dispatched to it. Restores the real proactive_routing module in case
    a prior _run_proactive_pass() stubbed it out."""
    db.RESULTS_DIR = results_dir
    db.TASKS_DIR = tasks_dir
    db.ARCHIVE_TASKS_DIR = tasks_dir / "archive"
    db.ARCHIVE_RESULTS_DIR = results_dir / "archive"
    db.pending_replies = {}
    sys.modules["proactive_routing"] = _real_proactive_routing_module

    calls: list[list[str]] = []

    def _fake_run(argv, **_kw):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="sent", stderr="")

    orig_run = db.subprocess.run
    db.subprocess.run = _fake_run

    async def _sleep(_secs):
        raise _Sentinel()

    orig_sleep = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    try:
        asyncio.run(db.poll_dm_fallback())
    except _Sentinel:
        pass
    finally:
        db.asyncio.sleep = orig_sleep
        db.subprocess.run = orig_run

    return calls


def _age_past_grace(*paths: Path) -> None:
    old = time.time() - 100  # > GRACE_SECONDS (90s), < MAX_RETRY_AGE_SECONDS
    for p in paths:
        os.utime(p, (old, old))


# --------------------------------------------------------------------------

def check_real_code_honors_the_marker() -> None:
    """The real, unmutated code: [no-send] must never be attempted, and the
    ordinary sibling MUST be attempted (positive control). Each file runs in
    its own pass so the fence-call signal is unambiguous per file."""
    box1 = Path(tempfile.mkdtemp(prefix="proactive-behavioral-nosend-"))
    nosend = box1 / "proactive-behavioral-nosend.txt"
    nosend.write_text("[no-send]\nInternal, must never reach a send attempt.")
    delivered, calls = _run_proactive_pass(_load(BRIDGE), box1)
    if nosend.stem in delivered:
        fail(
            f"poll_proactive: [no-send] file {nosend.name} reached a DM-send "
            f"attempt (delivered={delivered!r})"
        )
    if calls != ["drop"]:
        fail(
            f"poll_proactive: [no-send] file {nosend.name} did not take the "
            f"fence .drop() (suppressed) path — fence calls were {calls!r}, "
            "expected ['drop']"
        )

    box2 = Path(tempfile.mkdtemp(prefix="proactive-behavioral-ordinary-"))
    ordinary = box2 / "proactive-behavioral-ordinary.txt"
    ordinary.write_text("Good morning! Here is your briefing.")
    delivered2, calls2 = _run_proactive_pass(_load(BRIDGE), box2)
    if ordinary.stem not in delivered2:
        fail(
            f"poll_proactive: ordinary file {ordinary.name} was NOT delivered — "
            f"positive control failed (delivered={delivered2!r})"
        )
    if calls2 != ["confirm"]:
        fail(
            f"poll_proactive: ordinary file {ordinary.name} did not take the "
            f"fence .confirm() (sent) path — fence calls were {calls2!r}, "
            "expected ['confirm']"
        )

    db2 = _load(BRIDGE)
    results = Path(tempfile.mkdtemp(prefix="dmfallback-behavioral-results-"))
    tasks = Path(tempfile.mkdtemp(prefix="dmfallback-behavioral-tasks-"))
    fb_nosend = results / "question-behavioral-nosend.txt"
    fb_ordinary = results / "question-behavioral-ordinary.txt"
    fb_nosend.write_text("[no-send]\nInternal, must never reach dm-result.py.")
    fb_ordinary.write_text("Here is your answer.")
    _age_past_grace(fb_nosend, fb_ordinary)
    calls = _run_dm_fallback_pass(db2, results, tasks)
    argvs = [" ".join(a) for a in calls]
    if any(str(fb_nosend) in a for a in argvs):
        fail(
            f"poll_dm_fallback: [no-send] file {fb_nosend.name} reached the "
            f"dm-result.py subprocess (calls={calls!r})"
        )
    if not any(str(fb_ordinary) in a for a in argvs):
        fail(
            f"poll_dm_fallback: ordinary file {fb_ordinary.name} was NOT "
            f"dispatched to dm-result.py — positive control failed (calls={calls!r})"
        )


def check_mutation_makes_it_fail() -> None:
    """Permanent guard: qingyun-wu's exact review mutation, applied to a real
    executed copy, must flip the [no-send] case from held-back to attempted.

    parse_markers() blanks the body for ANY skip-matched marker regardless of
    has_skip_action, so delivered-content alone can't see this mutation for
    poll_proactive — the fence call it reaches can: disabling the predicate
    flips .drop() -> .confirm(), the terminal-drop control under review."""
    mdb = _load_mutated()
    box = Path(tempfile.mkdtemp(prefix="proactive-mutated-"))
    nosend = box / "proactive-mutated-nosend.txt"
    nosend.write_text("[no-send]\nInternal, must never reach a send attempt.")
    _delivered, calls = _run_proactive_pass(mdb, box)
    if calls == ["drop"]:
        fail(
            "mutation-resistance: disabling poll_proactive's skip check "
            "(if False and has_skip_action(...)) did NOT change the "
            "[no-send] file's fence outcome away from .drop() — this test "
            f"cannot detect that mutation (fence calls: {calls!r})"
        )

    mdb2 = _load_mutated()
    results = Path(tempfile.mkdtemp(prefix="dmfallback-mutated-results-"))
    tasks = Path(tempfile.mkdtemp(prefix="dmfallback-mutated-tasks-"))
    fb_nosend = results / "question-mutated-nosend.txt"
    fb_nosend.write_text("[no-send]\nInternal, must never reach dm-result.py.")
    _age_past_grace(fb_nosend)
    calls = _run_dm_fallback_pass(mdb2, results, tasks)
    argvs = [" ".join(a) for a in calls]
    if not any(str(fb_nosend) in a for a in argvs):
        fail(
            "mutation-resistance: disabling poll_dm_fallback's skip check "
            "(if False and has_skip_action(...)) did NOT cause a [no-send] "
            "file to reach the dm-result.py subprocess — this test cannot "
            "detect that mutation"
        )


def main() -> int:
    check_real_code_honors_the_marker()
    check_mutation_makes_it_fail()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s)", file=sys.stderr)
        return 1
    print(
        "PASS: poll_proactive/poll_dm_fallback behaviorally honor suppression "
        "markers, and disabling the check (mutation) makes this test fail"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
