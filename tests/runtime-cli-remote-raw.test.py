#!/usr/bin/env python3
"""Remote routing of the persistent surfaces: the REAL CLI refuses the local
tmux attach (--raw) and the local Unix-socket chat under a remote URL, and
leaves both untouched without one.

The raw view is inherently LOCAL (it attaches this host's tmux). With
SUTANDO_SCP_WSS_URL configured, `task chat --raw` / `task watch --raw` must
refuse (rc 2) WITHOUT invoking tmux — attaching the local console under a
remote profile shows the WRONG agent's output, which can include secrets and
private task content. Without the URL, the attach must be byte-identical to
what shipped: same tmux argv, exit code passed through.

Ordinary `task chat` (no --raw) is a separate decision: it multiplexes over
the local Unix socket only, so under a remote URL it must refuse (rc 2) with
the explicit refusal and never open that socket — otherwise private or
mutating work typed for the remote agent lands on the local one.

Every polarity runs production dispatch via the real CLI process. A `tmux`
shim first on PATH records its argv to a file and exits a sentinel, and a
real listener bound at SUTANDO_RUNTIME_SOCKET records each accepted
connection, so "tmux was not invoked" and "the local socket was not opened"
are filesystem facts, not a mock's claim.

Run: python3 tests/runtime-cli-remote-raw.test.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run_cli(args, env_extra, shim_dir):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SUTANDO_SCP_WSS_URL",)}
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    env.update(env_extra)
    return subprocess.run([sys.executable, str(CLI), *args],
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, env=env, timeout=30)


def uds_listener(path, rec):
    """Real Unix-socket server: appends one line to `rec` per accepted
    connection, drains the peer until it closes (bounded), never replies."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with open(rec, "a") as f:
                f.write("connect\n")
            conn.settimeout(3)
            try:
                while conn.recv(65536):
                    pass
            except OSError:
                pass
            conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return stop


def last_json(stdout):
    try:
        return json.loads(stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        shim_dir = Path(td) / "bin"
        shim_dir.mkdir()
        rec = Path(td) / "tmux-argv.txt"
        shim = shim_dir / "tmux"
        shim.write_text("#!/bin/sh\n"
                        f"printf '%s\\n' \"$@\" > {rec}\n"
                        "exit 7\n")
        shim.chmod(0o755)
        sock = str(Path(td) / "fake-tmux.sock")
        base_env = {"SUTANDO_TMUX_SOCKET": sock,
                    "SUTANDO_TMUX_SESSION": "raw-test-session"}

        for cmd in (["task", "chat", "--raw"], ["task", "watch", "--raw"]):
            name = " ".join(cmd)

            # Remote URL configured: refuse, and tmux must never run.
            rec.unlink(missing_ok=True)
            r = run_cli(cmd, {**base_env,
                              "SUTANDO_SCP_WSS_URL": "ws://127.0.0.1:9/scp"},
                        shim_dir)
            check(r.returncode == 2, f"{name}: remote URL -> rc 2 "
                  f"(got {r.returncode})")
            check(not rec.exists(), f"{name}: remote URL -> tmux NOT invoked")
            err = last_json(r.stdout)
            check("error" in err and "raw" in err["error"],
                  f"{name}: refusal is a machine-readable error naming raw")

            # No remote URL: the attach reaches tmux with the shipped argv and
            # the exit code passes through.
            rec.unlink(missing_ok=True)
            r = run_cli(cmd, base_env, shim_dir)
            check(r.returncode == 7,
                  f"{name}: local -> tmux rc passes through (got {r.returncode})")
            argv = rec.read_text().split() if rec.exists() else []
            check(argv == ["-S", sock, "attach-session", "-t",
                           "raw-test-session", "-r"],
                  f"{name}: local -> shipped tmux argv (got {argv})")

        # Ordinary `task chat`: remote URL -> refuse, and the local Unix
        # socket must never be opened. A real listener records every accept.
        uds = str(Path(td) / "rt.sock")
        conn_rec = Path(td) / "uds-connects.txt"
        stop = uds_listener(uds, conn_rec)
        chat_env = {**base_env, "SUTANDO_RUNTIME_SOCKET": uds}
        rec.unlink(missing_ok=True)
        try:
            r = run_cli(["task", "chat"],
                        {**chat_env, "SUTANDO_SCP_WSS_URL": "ws://127.0.0.1:9/scp"},
                        shim_dir)
            check(r.returncode == 2,
                  f"task chat: remote URL -> rc 2 (got {r.returncode})")
            err = last_json(r.stdout)
            check("not served over the remote WebSocket transport"
                  in err.get("error", ""),
                  "task chat: remote URL -> explicit refusal JSON")
            check(not conn_rec.exists(),
                  "task chat: remote URL -> local Unix socket NOT opened")
            check(not rec.exists(),
                  "task chat: remote URL -> tmux NOT invoked")

            # Positive control: without the URL the same CLI opens the
            # local socket, so the absence above measures the guard.
            r = run_cli(["task", "chat"], chat_env, shim_dir)
            n = conn_rec.read_text().count("connect") if conn_rec.exists() else 0
            check(n == 1,
                  f"task chat: local -> local Unix socket opened once (got {n})")
            check(r.returncode != 2,
                  f"task chat: local -> not the refusal rc (got {r.returncode})")
        finally:
            stop.set()

    print(("FAIL: " + "; ".join(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
