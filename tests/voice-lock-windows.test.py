#!/usr/bin/env python3
"""Windows contract for scripts/voice-lock.py (msvcrt guard + PID liveness)."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    print("SKIP: Windows-only voice lock contract")
    raise SystemExit(0)

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "voice-lock.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(HELPER), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def main():
    with tempfile.TemporaryDirectory(prefix="voice-lock-windows-") as raw:
        root = Path(raw)
        pidfile = root / ".voice-agent.pid"
        guard = root / ".voice-agent.lock.guard"
        entry = REPO / "src" / "voice-agent.ts"

        first = run(
            "acquire", "--pidfile", pidfile, "--guard", guard,
            "--pid", os.getpid(), "--entry", entry, "--workspace", root,
        )
        assert first.returncode == 0, first.stderr or first.stdout
        lock = json.loads(pidfile.read_text())
        assert lock["pid"] == os.getpid()
        assert lock["lockId"].startswith("vl1-")

        other = sleeper()
        try:
            held = run(
                "acquire", "--pidfile", pidfile, "--guard", guard,
                "--pid", other.pid, "--entry", entry, "--workspace", root,
            )
            assert held.returncode == 7, held.stderr or held.stdout
            assert json.loads(held.stdout)["holder"]["pid"] == os.getpid()
        finally:
            other.kill()
            other.wait()

        released = run(
            "release", "--pidfile", pidfile, "--guard", guard,
            "--pid", os.getpid(),
        )
        assert released.returncode == 0, released.stderr or released.stdout
        assert not pidfile.exists()

        contenders = [sleeper(), sleeper()]
        try:
            procs = [
                subprocess.Popen(
                    [
                        sys.executable, str(HELPER), "acquire",
                        "--pidfile", str(pidfile), "--guard", str(guard),
                        "--pid", str(proc.pid), "--entry", str(entry),
                        "--workspace", str(root),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for proc in contenders
            ]
            codes = sorted(proc.wait(timeout=30) for proc in procs)
            assert codes == [0, 7], codes
        finally:
            for proc in contenders:
                proc.kill()
                proc.wait()

    print("PASS: Windows voice lock excludes live duplicates and releases")


if __name__ == "__main__":
    main()
