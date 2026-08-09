#!/usr/bin/env python3
"""Slice 2: the activity + results push streams reach a WSS subscriber.

Boots the REAL daemon with the WSS transport enabled, connects a REAL websocket
client, sends task.subscribe {results, activity}, then triggers the two real
server-side sources and asserts each notification arrives over WSS:
  - a core-status step change  → an `activity` notification;
  - a new results/ file        → a `task.result` notification.

This is the live feed the mobile app + chat TUI need — proven over the network,
through the same subscriber machinery the Unix socket uses.

Run: python3 tests/runtime-api-wss-push.test.py   (needs aiohttp)
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


TMP = tempfile.mkdtemp(prefix="wss-push-")
PORT = free_port()
TOKEN = "wss-push-token"
STATE = Path(TMP) / "state"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "wss-push-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(STATE),
       "SUTANDO_AGENT_ID": "@wss-push:example.org",
       "SUTANDO_HOST_LABEL": "wss-push-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/wss-push-tmux.sock",
       "SUTANDO_TMUX_SESSION": "wss-push-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "SUTANDO_RESULT_POLL_S": "0.1",
       "SUTANDO_SCP_WSS_ENABLE": "1",
       "SUTANDO_SCP_WSS_TOKEN": TOKEN,
       "SUTANDO_SCP_WSS_PORT": str(PORT),
       "SUTANDO_SCP_WSS_HOST": "127.0.0.1"}


async def collect_until(ws, method, timeout=6, match=None):
    """Return the first notification with the given method (and, if `match` is
    given, satisfying match(params)), draining intermediate frames. The
    activity watcher legitimately emits the seed step before the one under
    test, so a step assertion must skip past it rather than grab the first."""
    dl = time.monotonic() + timeout
    while time.monotonic() < dl:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if not msg.data:
            continue
        obj = json.loads(msg.data)
        if obj.get("method") == method and (
                match is None or match(obj.get("params", {}))):
            return obj
    return None


async def probe():
    import aiohttp
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "core-status.json").write_text(
        json.dumps({"status": "running", "step": "initial", "ts": 1}))
    url = f"http://127.0.0.1:{PORT}/scp"
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(
                url, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send_str(json.dumps({
                "jsonrpc": "2.0", "id": "sub", "method": "task.subscribe",
                "params": {"results": True, "activity": True}}))
            sub = json.loads((await ws.receive()).data)
            check(sub.get("result", {}).get("subscribed") is True
                  and set(sub["result"]["streams"]) == {"results", "activity"},
                  "task.subscribe over WSS acknowledges results+activity streams")

            # activity push: change the core-status step
            await asyncio.sleep(0.4)  # let the watcher seed past 'initial'
            (STATE / "core-status.json").write_text(json.dumps(
                {"status": "running", "step": "PUSHED-OVER-WSS", "ts": 2}))
            act = await collect_until(
                ws, "activity", match=lambda p: p.get("step") == "PUSHED-OVER-WSS")
            check(act is not None,
                  "core-status step change pushes an `activity` frame over WSS")

            # results push: drop a new result file
            (Path(TMP) / "results").mkdir(exist_ok=True)
            (Path(TMP) / "results" / "task-rtapi-wsspush.txt").write_text(
                "streamed result body")
            res = await collect_until(ws, "task.result")
            check(res is not None
                  and res.get("params", {}).get("taskId") == "task-rtapi-wsspush"
                  and res["params"].get("result") == "streamed result body",
                  "a new result file pushes a `task.result` frame over WSS")


def main() -> int:
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        if not wait_port(PORT):
            print(proc.stdout.read())
            raise AssertionError("WSS port never came up")
        asyncio.run(probe())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{'PASS — WSS push streams green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
