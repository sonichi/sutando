#!/usr/bin/env python3
"""Slice 3: pairing + per-device credential + per-device authz, end to end.

Boots the REAL daemon with the WSS transport enabled, then exercises the full
opaque-bearer pairing flow against the real device store:
  1. owner mints a one-time pairing token (DeviceStore, as the local CLI would);
  2. a device connects with the pairing token — it may ONLY pair.redeem;
  3. pair.redeem returns a long-term per-device credential + its grants;
  4. the device reconnects with its credential and task.submit SUCCEEDS
     (a real task file lands) — the read-only edge no longer applies to it;
  5. a method NOT in the device's grants is refused per-credential;
  6. the pairing token is single-use (a second redeem fails);
  7. no token → 401.

This is the M5-class client's connect+status+submit path proven end to end,
minus the firmware.

Run: python3 tests/runtime-api-wss-pairing.test.py   (needs aiohttp)
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
from device_store import DeviceStore  # noqa: E402

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


TMP = tempfile.mkdtemp(prefix="wss-pair-")
PORT = free_port()
STATE = Path(TMP) / "state"
URL = f"http://127.0.0.1:{PORT}/scp"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "wss-pair-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(STATE),
       "SUTANDO_AGENT_ID": "@wss-pair:example.org",
       "SUTANDO_HOST_LABEL": "wss-pair-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/wss-pair-tmux.sock",
       "SUTANDO_TMUX_SESSION": "wss-pair-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "SUTANDO_SCP_WSS_ENABLE": "1",
       "SUTANDO_SCP_WSS_TOKEN": "shared-ro-token",
       "SUTANDO_SCP_WSS_PORT": str(PORT),
       "SUTANDO_SCP_WSS_HOST": "127.0.0.1"}


async def rpc(sess, token, method, params=None):
    import aiohttp
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with sess.ws_connect(URL, headers=headers) as ws:
        await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params or {}}))
        return json.loads((await ws.receive()).data)


async def probe():
    import aiohttp
    # 1. owner mints a pairing token (what the local `sutando pair new` does)
    store = DeviceStore(STATE / "auth")
    pairing = store.mint_pairing("m5-watch")

    async with aiohttp.ClientSession() as sess:
        # 2+3. device connects with pairing token → may only pair.redeem
        bad = await rpc(sess, pairing, "sutando.info")
        check(bad.get("error", {}).get("code") == -32601,
              "a pairing-token connection cannot call ordinary methods")
        red = await rpc(sess, pairing, "pair.redeem",
                        {"label": "m5-watch", "capabilities": ["mic", "speaker", "haptic"]})
        cred = red.get("result", {}).get("credential")
        check(cred and "task.submit" in red["result"].get("granted_methods", []),
              "pair.redeem issues a device credential granting task.submit")

        # 4. the paired device SUBMITS a task over WSS (edge no longer read-only)
        sub = await rpc(sess, cred, "task.submit", {"task": "from the watch", "priority": "low"})
        tid = sub.get("result", {}).get("taskId")
        check(sub.get("result", {}).get("state") == "pending" and tid,
              "paired device submits a task over WSS (per-device grant honored)")
        tf = Path(TMP) / "tasks" / f"{tid}.txt"
        check(tf.is_file() and "from the watch" in tf.read_text(),
              "the submitted task landed as a real task file")

        # 5. a method NOT granted to the device is refused per-credential
        ref = await rpc(sess, cred, "capability.execute", {"action": "x"})
        check(ref.get("error", {}).get("code") == -32601
              and "credential" in ref["error"]["message"],
              "an ungranted method is refused for this credential")

        # 5b. the device CAN read status (M5 status-glance path)
        st = await rpc(sess, cred, "sutando.status")
        check("error" not in st, "paired device can read sutando.status (status glance)")

        # 5c. client.hello: the device advertises its live capabilities, which
        # the server records on the device record (descriptive, not authz).
        hello = await rpc(sess, cred, "client.hello",
                          {"device_type": "watch",
                           "capabilities": ["display", "microphone", "speaker",
                                            "vibration", "imu"]})
        rec = hello.get("result", {}).get("recorded") or {}
        check(rec.get("device_type") == "watch"
              and "vibration" in rec.get("capabilities", []),
              "client.hello records the device's advertised type + capabilities")

        # 6. pairing token is single-use — once redeemed it no longer authorizes
        #    ANY connection, so reuse is rejected at connect (stronger than a
        #    redeem-time error: a burned token can't even open a session).
        try:
            await rpc(sess, pairing, "pair.redeem", {})
            check(False, "reused pairing token should be rejected")
        except aiohttp.WSServerHandshakeError as e:
            check(e.status == 401,
                  "the pairing token is single-use (reuse → 401 at connect)")

        # 7. an unknown bearer is rejected at connect
        try:
            await rpc(sess, "garbage-token", "sutando.info")
            check(False, "unknown bearer should 401")
        except aiohttp.WSServerHandshakeError as e:
            check(e.status == 401, "unknown bearer → 401")


CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"


def cli(*args, env, expect_rc=0):
    p = subprocess.run([sys.executable, str(CLI), *args],
                       capture_output=True, text=True, env=env, timeout=20)
    if p.returncode != expect_rc:
        raise AssertionError(f"cli {args} rc={p.returncode} err={p.stderr}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def cli_probe():
    """The owner-facing flow through the REAL CLI: mint locally, redeem over
    WSS on the 'device', then submit with the issued credential."""
    wss_env = {**ENV, "SUTANDO_SCP_WSS_URL": f"ws://127.0.0.1:{PORT}/scp"}
    newp = cli("pair", "new", "--label", "cli-phone", env=ENV)  # local mint
    check(newp and newp.get("pairing_token"),
          "`pair new` mints a pairing token locally (owner)")
    red = cli("pair", "redeem", newp["pairing_token"], "--label", "cli-phone",
              env=wss_env)  # device redeems over WSS
    cred = red.get("credential") if red else None
    check(cred and "task.submit" in red.get("granted_methods", []),
          "`pair redeem` exchanges the token for a device credential over WSS")
    sub_env = {**wss_env, "SUTANDO_SCP_WSS_TOKEN": cred}
    sub = cli("task", "submit", "from cli-paired-device", env=sub_env)
    check(sub and sub.get("state") == "pending",
          "the paired device submits a task via the CLI over WSS")
    devs = cli("pair", "list", env=ENV)
    check(devs and any(d.get("label") == "cli-phone"
                       for d in devs.get("devices", [])),
          "`pair list` shows the paired device")


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "core-status.json").write_text(
        json.dumps({"status": "running", "step": "pairing e2e", "ts": 1}))
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_port(PORT):
            print(proc.stdout.read())
            raise AssertionError("WSS port never came up")
        asyncio.run(probe())
        cli_probe()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{'PASS — SCP pairing E2E green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
