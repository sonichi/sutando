#!/usr/bin/env python3
"""Guards for the heartbeat-starvation fix (gateway flapping / presence offline).

The bridge's async handlers must never run long sync subprocesses on the event
loop: a 15-25s block starves Discord's ~41s heartbeat, the socket drops, and
the bot's presence flaps offline (owner report 2026-07-17 — 49 gateway
sessions in one log window).

Behavioral guard 1: _transcribe_via_skill executed through asyncio.to_thread
keeps the loop responsive — a concurrent heartbeat-like task keeps ticking
while a slow (stubbed) transcription runs off-loop.

Structural guards 2-4: the two known blocking call sites are to_thread-wrapped
in source, and on_ready sets an explicit presence.

Run: python3 tests/discord-bridge-heartbeat-offload.test.py  (exit 0/1)
"""
from __future__ import annotations

import asyncio
import atexit
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Isolate host config BEFORE touching the bridge. This file only reads the
# source text today, but `channel_access_path()` falls back to the real
# `~/.claude/channels/<ch>/access.json`, so the isolation has to be in place
# for any future edit that imports the module rather than reading it.
_CFG = tempfile.mkdtemp(prefix="ccd-heartbeat-offload-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_CHAN = Path(_CFG) / "channels" / "discord"
_CHAN.mkdir(parents=True, exist_ok=True)
(_CHAN / "access.json").write_text('{"allowFrom": []}')

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "discord-bridge.py").read_text()

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. Behavioral: to_thread keeps the loop responsive under a slow call ──────

def _slow_transcribe(_path: str) -> str:
    time.sleep(1.5)  # stands in for the up-to-25s transcription subprocess
    return "transcript"


async def _probe() -> tuple[int, str]:
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.1)
            ticks += 1

    hb = asyncio.ensure_future(heartbeat())
    # The pattern under test — identical shape to the bridge's call site.
    result = await asyncio.to_thread(_slow_transcribe, "/tmp/fake.m4a")
    hb.cancel()
    return ticks, result


ticks, result = asyncio.run(_probe())
check(
    "event loop keeps ticking while the slow call runs off-loop",
    ticks >= 10,
    f"only {ticks} heartbeat ticks during a 1.5s to_thread call",
)
check("off-loop call still returns its result", result == "transcript")

# ── 1b. BEFORE/AFTER behavioral contrast (CR #2159) ───────────────────────────
# The bug: a long SYNC subprocess ran directly on the event loop, starving
# Discord's ~41s heartbeat until the socket dropped and presence flapped
# offline (49 gateway sessions in one window). Run the SAME 1.5s work + the
# SAME 0.1s heartbeat two ways and contrast the tick counts — this is the
# behavior-level, not source-grep, evidence that the flap cause is removed.
async def _probe_blocking() -> int:
    ticks_b = 0

    async def heartbeat():
        nonlocal ticks_b
        while True:
            await asyncio.sleep(0.1)
            ticks_b += 1

    hb = asyncio.ensure_future(heartbeat())
    await asyncio.sleep(0.05)          # let the heartbeat register (may tick once)
    _slow_transcribe("/tmp/fake.m4a")  # BLOCKING on the loop — the pre-fix shape
    hb.cancel()
    try:
        await hb
    except asyncio.CancelledError:
        pass
    return ticks_b


before_ticks = asyncio.run(_probe_blocking())
check(
    "BEFORE-fix: a blocking on-loop call starves the heartbeat (the flap cause)",
    before_ticks <= 1,
    f"expected ~0 ticks during a 1.5s on-loop block, got {before_ticks}",
)
check(
    "AFTER-fix: the same work via to_thread keeps the heartbeat ticking",
    ticks >= 10 and (ticks - before_ticks) >= 8,
    f"blocking={before_ticks} ticks vs to_thread={ticks} ticks over the same 1.5s",
)

# ── 2-4. Structural: the real call sites are wrapped; presence is set ─────────
# (Secondary to the behavioral contrast above — these confirm the fix is applied
# at the two specific blocking call sites, which a stand-in probe can't verify.)

check(
    "transcribe call site is to_thread-wrapped",
    re.search(r"await asyncio\.to_thread\(_transcribe_via_skill,", SRC) is not None,
)
check(
    "no remaining bare _transcribe_via_skill call in async flow",
    re.search(r"(?<!to_thread\()\btranscript = _transcribe_via_skill\(", SRC) is None,
)
check(
    "dm-fallback subprocess.run is to_thread-wrapped",
    re.search(r"await asyncio\.to_thread\([^)]*?subprocess\.run,", SRC) is not None,
)
check(
    "on_ready sets an explicit online presence",
    "change_presence(status=discord.Status.online)" in SRC,
)
check(
    "ready log carries a gateway-session counter (flap visibility)",
    "gateway session #" in SRC,
)

# ── 5. Execution: on_ready runs its presence block (counter + try/except) ──────
# The structural checks above read source text; this one actually executes
# on_ready so the counter bump + the change_presence try/except are covered.
# Two runs: presence succeeds, then presence raises (the except/pass path).
try:
    import discord  # noqa: F401
except ImportError:
    print("  skip on_ready exec-coverage — discord.py not importable")
else:
    import importlib.util
    import os
    import tempfile
    from unittest.mock import patch, AsyncMock, MagicMock

    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="sutando-hb-test-")
    os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken-for-tests")
    _spec = importlib.util.spec_from_file_location("discordbridge_hb", REPO / "src" / "discord-bridge.py")
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    class _Loop:
        def create_task(self, coro):  # swallow scheduled coros without running them
            try:
                coro.close()
            except Exception:
                pass

    class _ReadyClient:
        user = "BotUser#1"
        loop = _Loop()

        def __init__(self, raise_presence):
            self._raise = raise_presence

        async def change_presence(self, **kw):
            if self._raise:
                raise RuntimeError("presence flap")

    _mod._poll_loops_started = True  # skip the one-time poll-loop spawn block
    for _raise in (False, True):     # success path, then except/pass path
        with patch.object(_mod, "client", _ReadyClient(_raise)), \
             patch.object(_mod, "_recover_orphan_sending_files", lambda *a, **k: None), \
             patch.object(_mod, "discord_config", MagicMock()):
            _before = _mod._ready_count
            asyncio.run(_mod.on_ready())  # must never raise, even when presence does
            check(f"on_ready executed + bumped session counter (presence_raises={_raise})",
                  _mod._ready_count == _before + 1)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — heartbeat-offload guards")
