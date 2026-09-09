#!/usr/bin/env python3
"""End-to-end: the REAL CLI talks to the REAL daemon over the LAN-WSS transport.

Proves the client half of slice 1 (SUTANDO_SCP_WSS_URL routes _rpc through the
websocket) against a real server.py with the WSS listener enabled — the same
`sutando` commands, one SCP, a different transport. Read commands round-trip;
a mutating command is refused at the network edge (read-only allowlist).

Run: python3 tests/runtime-api-wss-client.test.py   (needs aiohttp)
"""
from __future__ import annotations

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
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"

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


TMP = tempfile.mkdtemp(prefix="wss-client-")
PORT = free_port()
TOKEN = "wss-client-token"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "wss-client-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(Path(TMP) / "state"),
       "SUTANDO_AGENT_ID": "@wss-client:example.org",
       "SUTANDO_HOST_LABEL": "wss-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/wss-tmux.sock",
       "SUTANDO_TMUX_SESSION": "wss-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "SUTANDO_SCP_WSS_ENABLE": "1",
       "SUTANDO_SCP_WSS_TOKEN": TOKEN,
       "SUTANDO_SCP_WSS_PORT": str(PORT),
       "SUTANDO_SCP_WSS_HOST": "127.0.0.1"}

# The CLI env additionally carries the WSS target so _rpc routes over the socket.
CLI_ENV = {**ENV, "SUTANDO_SCP_WSS_URL": f"ws://127.0.0.1:{PORT}/scp"}


def cli(*args, env=CLI_ENV, expect_rc=0, timeout=20):
    p = subprocess.run([sys.executable, str(CLI), *args],
                       capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != expect_rc:
        raise AssertionError(f"cli {args} rc={p.returncode} err={p.stderr}")
    return (json.loads(p.stdout) if p.stdout.strip() else None), p


def main() -> int:
    Path(TMP, "state").mkdir(parents=True, exist_ok=True)
    Path(TMP, "state", "core-status.json").write_text(
        json.dumps({"status": "running", "step": "wss client e2e", "ts": 1}))
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        if not wait_port(PORT):
            print(proc.stdout.read())
            raise AssertionError("WSS port never came up")

        # read commands round-trip through the REAL CLI over WSS
        info, _ = cli("sutando", "info")
        check(info.get("agentId") == "@wss-client:example.org",
              "sutando info over WSS returns the daemon-resolved identity")
        st, _ = cli("sutando", "status")
        check(st.get("step") == "wss client e2e",
              "sutando status over WSS reflects the real core-status")
        h, _ = cli("runtime", "health")
        check(h.get("state") in ("online", "offline", "stale_or_crashed"),
              f"runtime health over WSS answers (state={h.get('state')})")

        # a mutating command is refused at the network edge (read-only set)
        _, p = cli("task", "submit", "should be refused",
                   expect_rc=1)
        check("not permitted" in p.stderr,
              "task submit over WSS is refused at the edge (read-only allowlist)")

        # streaming: `sutando watch --activity` over WSS receives a live push
        import queue
        import threading
        watch = subprocess.Popen(
            [sys.executable, str(CLI), "task", "watch", "--activity"],
            env=CLI_ENV, stdout=subprocess.PIPE, text=True, bufsize=1)
        lines: queue.Queue = queue.Queue()

        def _pump():
            for ln in watch.stdout:
                lines.put(ln)
        threading.Thread(target=_pump, daemon=True).start()

        def _await_line(pred, timeout):
            dl = time.time() + timeout
            while time.time() < dl:
                try:
                    ln = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if pred(ln):
                    return ln
            return None
        try:
            started = _await_line(lambda l: '"watching": true' in l.lower(), 5)
            # label reflects the ACTUAL scheme: this test connects ws://
            check(started is not None and '"transport": "ws"' in started,
                  "sutando watch labels its true (cleartext) scheme")
            time.sleep(0.4)  # let the watcher seed past the initial step
            Path(TMP, "state", "core-status.json").write_text(json.dumps(
                {"status": "running", "step": "WATCH-OVER-WSS", "ts": 9}))
            got = _await_line(lambda l: "WATCH-OVER-WSS" in l, 6)
            check(got is not None,
                  "watch over WSS streams a live activity frame from the daemon")
        finally:
            watch.terminate()
            try:
                watch.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watch.kill()

        # control: the SAME CLI without the WSS env still uses the Unix socket
        info_uds, _ = cli("sutando", "info", env=ENV)
        check(info_uds.get("agentId") == "@wss-client:example.org",
              "without SUTANDO_SCP_WSS_URL the CLI uses the Unix socket (unchanged)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{'PASS — CLI-over-WSS e2e green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
