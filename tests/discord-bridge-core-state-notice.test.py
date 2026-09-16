#!/usr/bin/env python3
"""discord-bridge core-state notices (silent-core fix) — the async binder.

The shared decision logic (dedup, cooldown, recovery, per-surface ledger) is
covered in packages/ag2-sparrow/tests/test_core_state_notice.py. This covers
only what the Discord binder owns:

  * intake sends a degraded notice to the message's own channel and commits to
    the DISCORD ledger (not the gateway's);
  * intake is a no-op when the core is healthy;
  * the recovery pass resolves ledger channel ids to channels and sends "back
    online", clearing the ledger; an unresolvable channel is dropped, never
    wedged;
  * a send failure at intake burns nothing (retryable next message).

Run: python3 tests/discord-bridge-core-state-notice.test.py
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

_FAILURES: list[str] = []


def fail(msg: str) -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)


def expect(cond, label: str) -> None:
    if not cond:
        fail(label)


def _install_discord_stub():
    stub = types.ModuleType("discord")

    class _Intents:
        def __init__(self, *a, **k):
            pass

        @classmethod
        def default(cls):
            return cls()

        def __setattr__(self, k, v):
            object.__setattr__(self, k, v)

    class _Client:
        def __init__(self, *a, **k):
            self.user = None
            self.loop = types.SimpleNamespace(create_task=lambda *a, **k: None)

        def event(self, fn):
            return fn

        def get_channel(self, _id):
            return None

    stub.Intents = _Intents
    stub.Client = _Client
    stub.MessageType = types.SimpleNamespace(default=0, reply=1)
    stub.File = lambda *a, **k: None
    stub.DMChannel = type("_DMChannel", (), {})
    sys.modules["discord"] = stub


def load_bridge(config_root: Path):
    _install_discord_stub()
    env_dir = Path(config_root) / "channels" / "discord"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
    src = BRIDGE.read_text()
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(BRIDGE)
    code = compile(src, str(BRIDGE), "exec")
    exec(code, bridge.__dict__)
    return bridge


class _Channel:
    def __init__(self, cid, ok=True):
        self.id = cid
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, body, *a, **k):
        if not self.ok:
            raise RuntimeError("discord send failed")
        self.sent.append(body)


def _write_state(state_dir: Path, state, kind=None):
    (state_dir / "core-supervisor.json").write_text(json.dumps(
        {"state": state, "detail": "t", "prompt": None, "kind": kind}))


def main():
    tmp_home = Path(tempfile.mkdtemp(prefix="dbcsn-home-"))
    prior_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp_home)
    state_dir = Path(tempfile.mkdtemp(prefix="dbcsn-state-"))
    try:
        bridge = load_bridge(tmp_home)
        bridge.STATE_DIR = state_dir  # redirect ledger + supervisor reads
        bridge._CORE_NOTICE_DEBOUNCE_S = 0
        bridge.RESULTS_DIR = state_dir  # pending-rooms result check reads here
        ledger = state_dir / bridge._CORE_NOTICE_LEDGER

        # A cache/fetch registry the sweep resolves ids through.
        registry: dict = {}
        bridge.client.get_channel = lambda cid: registry.get(cid)

        async def _fetch(cid):
            ch = registry.get(("fetch", cid))
            if ch is None:
                raise RuntimeError("NotFound")
            return ch
        bridge.client.fetch_channel = _fetch

        def sweep(rooms):
            asyncio.run(bridge._core_notice_sweep(rooms))

        def reset():
            ledger.unlink(missing_ok=True)  # isolate scenarios: fresh cooldown+active

        MAX = bridge._plan_core_notices.__globals__["MAX_RECOVERY_ATTEMPTS"]

        # 1. degraded intake → notice to this channel, committed to DISCORD ledger
        _write_state(state_dir, "logged-out")
        ch = _Channel(555000000000000001)
        registry[ch.id] = ch
        sweep({str(ch.id)})
        expect(len(ch.sent) == 1, "intake: exactly one notice sent")
        expect("logged out" in ch.sent[0], "intake: reason wording is 'logged out'")
        expect("_(automated notice)_" in ch.sent[0], "intake: discord suffix, not gateway's")
        expect("gateway" not in ch.sent[0], "intake: must not carry the gateway suffix")
        expect(ledger.exists(), "intake: wrote the discord-specific ledger")
        expect(not (state_dir / "core-state-notice.json").exists(),
               "intake: did NOT touch the gateway ledger")

        # 2. same channel/reason → cooldown suppresses
        ch.sent.clear()
        sweep({str(ch.id)})
        expect(ch.sent == [], "cooldown: duplicate suppressed")

        # 3. recovery via cache resolution → back online, ledger active cleared
        _write_state(state_dir, "idle-ready")
        ch.sent.clear()
        sweep(set())
        expect(len(ch.sent) == 1 and "back online" in ch.sent[0],
               "recovery: back-online delivered via cache")
        expect(json.loads(ledger.read_text()).get("active") == {},
               "recovery: ledger active cleared")

        # 4. healthy core → no degraded notice
        reset()
        _write_state(state_dir, "idle-ready")
        ch3 = _Channel(555000000000000002)
        registry[ch3.id] = ch3
        sweep({str(ch3.id)})
        expect(ch3.sent == [], "healthy core sends no degraded notice")

        # 5. #8 — a cache MISS is resolved via a bounded fetch, not dropped
        reset()
        _write_state(state_dir, "crashed")
        fch = _Channel(555000000000000007)
        registry[("fetch", fch.id)] = fch  # only resolvable via fetch, not cache
        sweep({str(fch.id)})              # degraded notice (cache miss → fetch)
        expect(len(fch.sent) == 1, "intake: cache-miss channel resolved via fetch")
        _write_state(state_dir, "idle-ready")
        fch.sent.clear()
        sweep(set())
        expect(len(fch.sent) == 1 and "back online" in fch.sent[0],
               "recovery: cache-miss channel resolved via fetch, not dropped")

        # 6. #7 — a forged non-ASCII-decimal active key is purged, not crashed
        reset()
        _write_state(state_dir, "crashed")
        good = _Channel(555000000000000010)
        registry[good.id] = good
        sweep({str(good.id)})
        led = json.loads(ledger.read_text())
        led["active"]["²"] = "crashed"   # "²".isdigit() is True; int() raises
        ledger.write_text(json.dumps(led))
        _write_state(state_dir, "idle-ready")
        good.sent.clear()
        sweep(set())
        expect(any("back online" in b for b in good.sent),
               "purge: valid room still recovered alongside the bad key")
        expect(json.loads(ledger.read_text()).get("active") == {},
               "purge: forged '²' key removed, not left wedging the ledger")

        # 7. #8/#6 — a permanently unresolvable room is retried a bounded number
        # of times then purged (never counted as delivered)
        reset()
        _write_state(state_dir, "crashed")
        lost = _Channel(555000000000000011)
        registry[lost.id] = lost
        sweep({str(lost.id)})
        del registry[lost.id]              # now neither cache nor fetch resolves it
        _write_state(state_dir, "idle-ready")
        for _ in range(MAX + 1):
            sweep(set())
        expect(all("back online" not in b for b in lost.sent),
               "unresolved room never received recovery")
        expect(json.loads(ledger.read_text()).get("active") == {},
               "unresolved room purged after bounded retries, not stuck forever")

        # 8. #6 — core crashes DURING the resolve await → no stale recovery
        # (fetch resolves the channel but flips the supervisor to crashed first).
        reset()
        _write_state(state_dir, "crashed")
        race = _Channel(555000000000000013)
        registry[race.id] = race
        sweep({str(race.id)})              # seed active (degraded notice)
        del registry[race.id]

        async def _fetch_then_crash(cid):
            _write_state(state_dir, "crashed")  # core dies during the await
            return registry.get(("fetch", cid))
        registry[("fetch", race.id)] = race
        bridge.client.fetch_channel = _fetch_then_crash
        _write_state(state_dir, "idle-ready")  # healthy at the pre-resolve check
        race.sent.clear()
        sweep(set())
        expect(all("back online" not in b for b in race.sent),
               "no stale recovery when the core crashed during channel resolution")
        # restore the plain fetch for any later use
        bridge.client.fetch_channel = _fetch

        # 9. #4 — pending unanswered tasks feed the periodic retry set
        bridge.pending_replies = {"task-x": _Channel(555000000000000012),
                                  "task-done": _Channel(999)}
        (state_dir / "task-done.txt").write_text("answered")
        rooms = bridge._core_notice_pending_rooms()
        expect(rooms == {"555000000000000012"},
               "pending rooms: only unanswered tasks (answered one excluded)")
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(tmp_home, ignore_errors=True)
        if prior_cfg is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prior_cfg

    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("PASS: discord-bridge core-state notices (intake + recovery + ledger isolation)")


if __name__ == "__main__":
    main()
