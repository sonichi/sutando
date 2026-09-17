#!/usr/bin/env python3
"""The watcher's start diagnostics are fail-open and never block.

Two helpers the watcher calls before it publishes its record:

  src/diagnostic_append.py — appends one line to logs/watcher-starts.log. A
    shell `>>` blocks forever on a FIFO nobody reads, and `|| true` cannot help
    an open that never returns; this opens O_NONBLOCK and writes only a REGULAR
    file. Every other path (FIFO, directory, missing parent) is skipped, rc 0.
  src/git_binary.py short-head — the version the record carries, through the
    resolver that never spawns the Xcode-CLT stub; "unknown" without git.

The production watcher itself, started against a reader-less FIFO, is driven by
tests/watch-tasks-stream-sentinel-record.test.py (test_start_log_never_blocks).
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPEND = REPO / "src" / "diagnostic_append.py"
GITBIN = REPO / "src" / "git_binary.py"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_append(path: Path, line: str, timeout: float = 5.0) -> "subprocess.CompletedProcess | None":
    """The CLI the watcher calls, bounded: a hang is a failure, not a wait."""
    try:
        return subprocess.run([sys.executable, str(APPEND), str(path), line],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def test_append(box: Path) -> None:
    da = _load("diagnostic_append", APPEND)

    fifo = box / "fifo.log"
    os.mkfifo(fifo)
    # The instrument: a plain shell append onto this FIFO does block.
    try:
        subprocess.run(["bash", "-c", 'printf x >> "$1"', "_", str(fifo)], timeout=2)
        check("CONTROL: a shell >> onto a reader-less FIFO blocks", False,
              "it returned, so nothing below can show the helper differs")
    except subprocess.TimeoutExpired:
        check("CONTROL: a shell >> onto a reader-less FIFO blocks", True)
    r = run_append(fifo, "line")
    check("FIFO without a reader: the helper returns (bounded 5s)", r is not None, "it hung")
    check("  ...with rc 0 — the caller must not fail on it", r is not None and r.returncode == 0,
          f"rc {r.returncode if r else None}")
    check("  ...and the path is still the FIFO, untouched",
          stat.S_ISFIFO(os.lstat(fifo).st_mode), "replaced")

    # A FIFO WITH a reader opens fine and is still not a log: nothing is written.
    got: list[bytes] = []
    def drain():
        with open(fifo, "rb") as fh:
            got.append(fh.read())
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    check("FIFO with a reader: the helper refuses to write into it",
          da.append_line(str(fifo), "line") is False, "it reported a write")
    # Release the reader: open+close for write gives it EOF.
    fd = os.open(fifo, os.O_WRONLY)
    os.close(fd)
    t.join(timeout=5)
    check("  ...and the reader received nothing", got == [b""], f"reader got {got!r}")

    log = box / "regular.log"
    r = run_append(log, "first line")
    check("regular file: created and appended, rc 0", r is not None and r.returncode == 0
          and log.read_text() == "first line\n", f"content {log.read_text()!r}" if log.exists() else "missing")
    r = run_append(log, "second line\n")
    check("  ...a second append adds exactly one more line, newline normalised",
          log.read_text() == "first line\nsecond line\n", f"content {log.read_text()!r}")
    check("  ...mode is a plain 0644-ish regular file", stat.S_ISREG(log.stat().st_mode))

    r = run_append(box / "no-such-dir" / "x.log", "line")
    check("missing parent: rc 0, nothing created", r is not None and r.returncode == 0
          and not (box / "no-such-dir").exists(), f"rc {r.returncode if r else None}")
    d = box / "a-directory"
    d.mkdir()
    r = run_append(d, "line")
    check("a directory at the path: rc 0, left alone", r is not None and r.returncode == 0 and d.is_dir())
    r = subprocess.run([sys.executable, str(APPEND)], capture_output=True, text=True)
    check("usage error is the ONLY non-zero exit", r.returncode == 2, f"rc {r.returncode}")


def test_short_head(box: Path) -> None:
    gb = _load("git_binary", GITBIN)
    here = gb.short_head(str(REPO))
    want = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    check("short_head(REPO) is this checkout's abbreviated HEAD", here == want and len(want) >= 7,
          f"got {here!r}, git says {want!r}")
    r = subprocess.run([sys.executable, str(GITBIN), "short-head", str(REPO)],
                       capture_output=True, text=True)
    check("the CLI prints the same, rc 0", r.returncode == 0 and r.stdout.strip() == want,
          f"rc {r.returncode} out {r.stdout!r}")
    check("a directory that is not a checkout reads \"unknown\"",
          gb.short_head(str(box)) == "unknown", f"got {gb.short_head(str(box))!r}")

    # No runnable git at all: the resolver answers None and provenance degrades
    # to "unknown" instead of raising or spawning the stub.
    real = gb.resolve_git
    gb.resolve_git = lambda: None
    try:
        check("no runnable git: short_head is \"unknown\", no exception",
              gb.short_head(str(REPO)) == "unknown")
    finally:
        gb.resolve_git = real
    r = subprocess.run([sys.executable, str(GITBIN)], capture_output=True, text=True)
    check("CLI usage error exits 2", r.returncode == 2, f"rc {r.returncode}")


def main() -> int:
    print("watch-tasks-stream start diagnostics:")
    box = Path(tempfile.mkdtemp(prefix="start-log-"))
    try:
        test_append(box)
        test_short_head(box)
    finally:
        import shutil
        shutil.rmtree(box, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All start-diagnostics checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
