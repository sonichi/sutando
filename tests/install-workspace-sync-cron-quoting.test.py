#!/usr/bin/env python3
"""Behavioral test for install-workspace-sync.sh's cron_line quoting.

Asserts the emitted crontab line WORKS when the repo / sync-script / log paths
contain spaces — not that it contains particular characters. An assertion on the
text would pass for any quoting scheme that looks plausible; this one runs the
command cron would run and checks where the bytes actually landed.

The bundled desktop install lives under `~/Library/Application Support/...`, so
the spaced case is the normal case there, not an exotic one.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-workspace-sync.sh"


def extract_cron_line(script_text: str) -> str:
    """Pull just the cron_line function out of the installer.

    The installer runs its command dispatch at load time (no `BASH_SOURCE`
    guard), so sourcing it would attempt a real install. Extracting the one
    function under test keeps this hermetic.
    """
    m = re.search(r"^cron_line\(\)\s*\{.*?^\}", script_text, re.S | re.M)
    assert m, "cron_line() not found in install-workspace-sync.sh"
    return m.group(0)


def emit_line(fn_src: str, repo: Path, sync: Path, log: Path, interval_s: int = 1800) -> str:
    """Run cron_line with spaced paths and return the crontab line it prints.

    cron_line reads INTERVAL (seconds) from the enclosing script rather than
    taking an argument, so the harness supplies it the same way the installer
    does.
    """
    prog = f'''
set -u
REPO={shell_quote(str(repo))}
SYNC={shell_quote(str(sync))}
LOG={shell_quote(str(log))}
INTERVAL={interval_s}
{fn_src}
cron_line
'''
    out = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
    assert out.returncode == 0, f"cron_line failed: {out.stderr}"
    return out.stdout.strip()


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def run_as_cron_would(line: str) -> subprocess.CompletedProcess:
    """Strip the 5-field schedule and execute the command, as cron does via sh."""
    command = line.split(" ", 5)[5]
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


def main() -> None:
    fn_src = extract_cron_line(SCRIPT.read_text())

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "Application Support" / "my repo"
        logdir = base / "log dir"
        repo.mkdir(parents=True)
        logdir.mkdir(parents=True)
        log = logdir / "workspace-sync.log"

        # Stand in for sync-workspace.sh: prints a token so we can prove the
        # command reached it, and prints its own cwd so we can prove the `cd`
        # landed in the spaced repo rather than a truncated prefix.
        sync = repo / "scripts" / "sync-workspace.sh"
        sync.parent.mkdir(parents=True)
        sync.write_text('#!/bin/bash\necho "SYNC_RAN cwd=$PWD"\n')
        sync.chmod(0o755)

        line = emit_line(fn_src, repo, sync, log)

        # 1. It must run at all. Unquoted, `cd <path with spaces>` gets extra
        #    args and bash exits non-zero before reaching the sync script.
        proc = run_as_cron_would(line)
        assert proc.returncode == 0, (
            f"cron command failed on spaced paths (rc={proc.returncode})\n"
            f"  line: {line}\n  stderr: {proc.stderr.strip()}"
        )

        # 2. The output must land in the spaced log path — the redirect is the
        #    part most likely to silently write to the wrong file.
        assert log.is_file(), f"log not written to the spaced path: {log}"
        body = log.read_text()
        assert "SYNC_RAN" in body, f"sync script never ran; log held: {body!r}"

        # 3. The `cd` must reach the full spaced repo path, not a prefix.
        assert f"cwd={repo}" in body, (
            f"cd landed in the wrong directory\n  expected cwd={repo}\n  log: {body.strip()}"
        )

        # 4. Nothing may leak into a truncated sibling path — the classic
        #    unquoted-space symptom is a stray file named after the first word.
        strays = [p for p in logdir.iterdir() if p != log]
        assert not strays, f"unexpected files created alongside the log: {strays}"

    print("ok - cron_line emits a line that works with spaced repo/sync/log paths")


if __name__ == "__main__":
    main()
