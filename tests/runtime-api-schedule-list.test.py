#!/usr/bin/env python3
"""schedule.list — the read-only schedules surface, end to end over WSS.

Boots the REAL daemon with the WSS transport enabled and exercises the new
method with the shared read-only bearer:
  1. no crons.json yet → {"schedules": []} (not an error);
  2. a mixed-owner crons.json (session cron, launchd, codex-task, dynamic
     loop) → EVERY entry comes back (no owner filtering), tagged with its
     owner, with kind/prompt_or_skill and a computed next_run + next_run_ts;
  3. an unreadable crons.json → {"schedules": []} again (fail-soft read);
  4. the method is in the paired-device default grants (desktop/companion).

The parse/next-run policy itself lives in src/dashboard_schedules.py (the
domain module the dashboard delegates to); this proves the SCP binding reads
the same hosts/<label>/crons.json through that module.

Run: python3 tests/runtime-api-schedule-list.test.py   (needs aiohttp)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
from device_store import DEFAULT_DEVICE_GRANTS  # noqa: E402
from ws_transport import READ_ONLY_METHODS  # noqa: E402

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=10) -> bool:
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


TMP = tempfile.mkdtemp(prefix="wss-sched-")
PORT = free_port()
STATE = Path(TMP) / "state"          # workspace = STATE.parent = TMP
HOST_LABEL = "sched-host"
CRONS = Path(TMP) / "hosts" / HOST_LABEL / "crons.json"
URL = f"http://127.0.0.1:{PORT}/scp"
TOKEN = "shared-ro-token"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "wss-sched-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(STATE),
       "SUTANDO_AGENT_ID": "@wss-sched:example.org",
       "SUTANDO_HOST_LABEL": HOST_LABEL,
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/wss-sched-tmux.sock",
       "SUTANDO_TMUX_SESSION": "wss-sched-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "SUTANDO_SCP_WSS_ENABLE": "1",
       "SUTANDO_SCP_WSS_TOKEN": TOKEN,
       "SUTANDO_SCP_WSS_PORT": str(PORT),
       "SUTANDO_SCP_WSS_HOST": "127.0.0.1"}

# Every owner class schedule-crons documents: a session cron (skill), a
# launchd-owned daily, an OS-backed codex entry, and a self-pacing dynamic
# loop with NO cron field. schedule.list must return ALL of them.
FIXTURE = [
    {"name": "loop", "cron": "* * * * *", "prompt_skill": "proactive-loop"},
    {"name": "digest", "cron": "0 6 * * *", "prompt": "Run: send the digest",
     "launchd": True},
    {"name": "codex-daily", "cron": "30 7 * * *", "prompt": "daily codex run",
     "execution": "codex-task", "timezone": "America/Los_Angeles"},
    {"name": "inbox-score", "prompt_skill": "inbox-score", "loop": "dynamic",
     "loop_hint": "~10 min when owner active"},
]


async def rpc(sess, method, params=None):
    async with sess.ws_connect(
            URL, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params or {}}))
        return json.loads((await ws.receive()).data)


async def probe():
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        # 1. missing crons.json → empty list, not an error
        r = await rpc(sess, "schedule.list")
        check(r.get("result") == {"schedules": []},
              f"missing crons.json → empty schedules, no error ({r})")

        # 2. mixed-owner fixture → every entry, owner-tagged
        CRONS.parent.mkdir(parents=True, exist_ok=True)
        CRONS.write_text(json.dumps(FIXTURE))
        r = await rpc(sess, "schedule.list")
        scheds = r.get("result", {}).get("schedules", [])
        by = {s.get("name"): s for s in scheds}
        check(len(scheds) == 4,
              f"ALL entries returned — no owner filtering ({len(scheds)}/4)")
        check({s.get("owner") for s in scheds}
              == {"session", "launchd", "codex", "dynamic-loop"},
              "each entry is tagged with the scheduler that owns it")
        check(by.get("digest", {}).get("owner") == "launchd"
              and by.get("codex-daily", {}).get("owner") == "codex"
              and by.get("inbox-score", {}).get("owner") == "dynamic-loop",
              "owner tags land on the right entries")
        loop = by.get("loop", {})
        check(loop.get("kind") == "skill"
              and loop.get("prompt_or_skill") == "proactive-loop",
              "skill entries carry kind=skill + the skill name")
        check(by.get("digest", {}).get("kind") == "prompt"
              and "digest" in by.get("digest", {}).get("prompt_or_skill", ""),
              "prompt entries carry kind=prompt + the prompt text")
        ts = loop.get("next_run_ts")
        check(isinstance(ts, int) and 0 < ts - time.time() <= 120,
              f"an every-minute cron gets a computed near-future next_run_ts ({ts})")
        check("(" in loop.get("next_run", ""),
              f'next_run is the display string ({loop.get("next_run")!r})')
        dyn = by.get("inbox-score", {})
        check(dyn.get("cron") == "" and dyn.get("next_run_ts") is None,
              "a dynamic loop (no cron field) has no computed next run")

        # 3. unreadable crons.json → fail-soft empty list
        CRONS.write_text("{not valid json")
        r = await rpc(sess, "schedule.list")
        check(r.get("result") == {"schedules": []},
              "bad-JSON crons.json → empty schedules, no error")


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        if not wait_port(PORT):
            print(proc.stdout.read())
            raise AssertionError("WSS port never came up")
        asyncio.run(probe())

        # 4. surface membership: shared read-only edge + paired-device defaults
        check("schedule.list" in READ_ONLY_METHODS,
              "schedule.list is on the shared-bearer read-only surface")
        check("schedule.list" in DEFAULT_DEVICE_GRANTS,
              "schedule.list is in the paired-device default grants")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{'PASS — schedule.list e2e green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
