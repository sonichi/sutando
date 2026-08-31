#!/usr/bin/env python3
"""Remote --raw routing: the REAL CLI refuses the local tmux attach under a
remote URL, and leaves the local attach untouched without one.

The raw view is inherently LOCAL (it attaches this host's tmux). With
SUTANDO_SCP_WSS_URL configured, `task chat --raw` / `task watch --raw` must
refuse (rc 2) WITHOUT invoking tmux — attaching the local console under a
remote profile shows the WRONG agent's output, which can include secrets and
private task content. Without the URL, the attach must be byte-identical to
what shipped: same tmux argv, exit code passed through.

Both polarities run production dispatch via the real CLI process. A `tmux`
shim first on PATH records its argv to a file and exits a sentinel, so "tmux
was not invoked" is a filesystem fact, not a mock's claim.

Run: python3 tests/runtime-cli-remote-raw.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
                          capture_output=True, text=True, env=env, timeout=30)


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
            try:
                err = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                err = {}
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

    print(("FAIL: " + "; ".join(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
