#!/usr/bin/env python3
"""The loader's claim gate keeps its own `proactive_destination` function.

The loader imports `proactive_destination` (a filename -> channel function) and then
executes the package bridge into the same globals, so a package import bound to that
name would replace the function and every gate call would raise.

  a) the loaded module's `proactive_destination` is still the routing function
  b) a verified room's tagged voice result is claimed
  c) a bare proactive with discord last-active is left for discord
  d) a `.to-discord` file is never claimed

Run: python3 tests/gateway-claim-gate-name-collision.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "remote-gateway-bridge.py"
AGENT = "@agent:example.org"
ROOM = "!ok:example.org"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load():
    ws = Path(tempfile.mkdtemp(prefix="gw-gate-name-ws-"))
    cfg = Path(tempfile.mkdtemp(prefix="gw-gate-name-cfg-"))
    for d in ("results", "state", "logs"):
        (ws / d).mkdir()
    (ws / "state" / "last-owner-activity.json").write_text(json.dumps(
        {"ts": int(time.time()), "channel": "discord", "summary": "t"}))
    (cfg / "channels" / "discord").mkdir(parents=True)
    (cfg / "channels" / "discord" / ".env").write_text("DISCORD_BOT_TOKEN=x\n")
    (ws / "state" / "discord-bridge.heartbeat").write_text("alive")
    os.environ.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
                       "CLAUDE_CONFIG_DIR": str(cfg), "AG2_DEVICE_ENV": "",
                       "REMOTE_TASK_URL": "http://127.0.0.1:9", "REMOTE_TASK_TOKEN": "t",
                       "REMOTE_PROACTIVE_ROOM": "", "AGENT_MXID": AGENT})
    os.environ.pop("GATEWAY_INSTANCE", None)
    name = f"rgb_gate_name_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(name, _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod._log = lambda *_a, **_k: None
    return mod, ws / "results"


def gate(mod, path: Path) -> "bool | str":
    try:
        return mod._ag2space_proactive_claim_gate(path)
    except Exception as e:  # noqa: BLE001 — the regression raised here
        return f"raised {type(e).__name__}: {e}"


def main() -> int:
    mod, results = load()
    check(callable(mod.proactive_destination)
          and mod.proactive_destination("proactive-1.to-discord.txt") == "discord",
          f"a) proactive_destination is the routing function, got {type(mod.proactive_destination)}")

    class Verified:
        def verified(self, room):
            return room == ROOM
    mod.VOICE_ROOM_VERIFIER = Verified()
    voice = results / "proactive-result-1.to-ag2space.txt"
    voice.write_text(f"[channel: {ROOM}]\nyour answer\n", encoding="utf-8")
    got = gate(mod, voice)
    check(got is True, f"b) verified-room voice result is claimed, got {got!r}")

    bare = results / "proactive-2.txt"
    bare.write_text("owner nudge\n", encoding="utf-8")
    got = gate(mod, bare)
    check(got is False, f"c) bare file with discord last-active is left for discord, got {got!r}")

    discord = results / "proactive-3.to-discord.txt"
    discord.write_text("for discord\n", encoding="utf-8")
    got = gate(mod, discord)
    check(got is False, f"d) a .to-discord file is never claimed, got {got!r}")
    check(discord.exists(), "d) and stays on disk")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
