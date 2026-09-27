#!/usr/bin/env python3
"""`current-track-write.py append` must SAY when it rotated.

A rotation decides which entries a later pass can still read, so a silent one is a
decision taken on the caller's behalf without telling them.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRITER = REPO / "scripts" / "current-track-write.py"
sys.path.insert(0, str(REPO / "src"))
from current_track import DEFAULT_KEEP  # noqa: E402

failures = 0


def check(name: str, ok: bool, got: str = "") -> None:
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — got {got[:200]}" if not ok else ""))
    if not ok:
        failures += 1


def write(target: Path, text: str):
    return subprocess.run([sys.executable, str(WRITER), "append", str(target)],
                          input=text, capture_output=True, text=True)


with tempfile.TemporaryDirectory() as d:
    host = Path(d) / "hosts" / "TESTHOST"
    host.mkdir(parents=True)
    track = host / "current-track.md"

    # 1. A small append does NOT rotate, so it must stay quiet.
    track.write_text("## seed, 2026-01-01T00:00Z\nbody\n", encoding="utf-8")
    r = write(track, "\n## quiet, 2026-01-02T00:00Z\nbody\n")
    # `== ""` not `"rotated" not in ...`: the looser form would also accept "still over budget".
    check("a non-rotating append says nothing at all", r.returncode == 0
          and r.stderr == "", f"rc={r.returncode} stderr={r.stderr!r}")

    # 2. An append that crosses the budget rotates, and must SAY so.
    entries = "".join(f"\n## e{i}, 2026-02-{(i % 27) + 1:02d}T00:00Z\n{'x' * 900}\n" for i in range(45))
    track.write_text(entries, encoding="utf-8")
    assert track.stat().st_size > DEFAULT_KEEP, track.stat().st_size
    r = write(track, "\n## crosses, 2026-03-01T00:00Z\nbody\n")
    check("a rotating append reports it on stderr", r.returncode == 0
          and "current-track-write:" in r.stderr and "rotated" in r.stderr,
          f"rc={r.returncode} stderr={r.stderr!r}")
    check("and names what moved and what is left", "archived" in r.stderr and "head now" in r.stderr,
          r.stderr)
    check("and the head really did shrink", track.stat().st_size <= DEFAULT_KEEP,
          str(track.stat().st_size))

    # condense() keeps only pin-matching LINES, so only those lines can cause `oversized`;
    # a large body cannot.
    pinned = "".join(f"\n## p{i}, 2026-04-{(i % 27) + 1:02d}T00:00Z\n"
                     + f"in force until the owner says stop: {'y' * 700}\n" * 3
                     for i in range(30))
    track.write_text(pinned, encoding="utf-8")
    r = write(track, "\n## crosses-pinned, 2026-05-01T00:00Z\nin force until the owner says stop\nbody\n")
    check("the pin-only oversized case is reported, not silent", r.returncode == 0
          and "still over budget" in r.stderr, f"rc={r.returncode} stderr={r.stderr!r}")
    check("and it names the pinned bytes that caused it", "pinned entr" in r.stderr, r.stderr)

    # 4. `oversized` without pins: the pinned clause would name nothing, so it must be absent.
    track.write_text("## seed, 2026-01-01T00:00Z\nb\n", encoding="utf-8")
    r = write(track, f"\n## huge-newest, 2026-06-02T00:00Z\n{'z' * (DEFAULT_KEEP + 7000)}\n")
    check("an entry bigger than the budget reports still-over", r.returncode == 0
          and "still over budget" in r.stderr, f"rc={r.returncode} stderr={r.stderr!r}")
    check("and says nothing about pins when there are none", "pinned entr" not in r.stderr, r.stderr)
    check("every report names the budget", f"of a {DEFAULT_KEEP} B budget" in r.stderr, r.stderr)

    # 5. Multibyte: the printed head size must be BYTES, matching the file on disk.
    track.write_text("## seed, 2026-01-01T00:00Z\nb\n", encoding="utf-8")
    r = write(track, "\n## em, 2026-06-02T00:00Z\n" + ("\u2014" * 12000) + "\n")
    on_disk = track.stat().st_size
    m = re.search(r"head now (\d+) B", r.stderr)
    check("the printed head size is bytes, not characters",
          m is not None and int(m.group(1)) == on_disk,
          f"printed={m.group(1) if m else None} on_disk={on_disk} stderr={r.stderr!r}")

    # In-process too: the checks above run the writer as a subprocess, so coverage of the
    # pinned branch depends on subprocess instrumentation being wired up.
    import contextlib
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("ctw", WRITER)
    ctw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctw)
    track.write_text(pinned, encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        stdin, sys.stdin = sys.stdin, io.StringIO(
            "\n## in-proc, 2026-07-01T00:00Z\nin force until the owner says stop\nb\n")
        try:
            rc = ctw.main(["append", str(track)])
        finally:
            sys.stdin = stdin
    check("the pinned clause is reached in-process, not only via a subprocess",
          rc == 0 and "pinned entr" in err.getvalue(), f"rc={rc} stderr={err.getvalue()!r}")

print(f"\n{'PASS' if failures == 0 else f'FAIL — {failures} check(s) failed'}")
sys.exit(1 if failures else 0)
