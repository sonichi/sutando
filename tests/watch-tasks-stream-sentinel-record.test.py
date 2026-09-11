#!/usr/bin/env python3
"""The sentinel is a RECORD, and only its own writer may release it.

WHAT THIS PINS (owner directive 2026-09-11, watcher side of the lifecycle split).
#4168 taught the SIGNALLERS to confirm ownership from an identity record and an
incarnation marker before stopping anything. Nothing wrote either, so every
signaller refused every watcher: correct, and permanently inert. This file drives
the production writer -- real watcher processes, not a re-implementation -- and
asserts the record a signaller reads back.

THE RACE, and why the incarnation is load-bearing.
Two watchers of ONE instance share a sentinel path during a handover: B starts,
stamps over A's record, and A exits afterwards. A's release already compares the
PID on line 1, so B's record survives that. The MARKER does not carry a pid --
it is one opaque id in a file -- so an unguarded `rm -f` in A's cleanup deletes
the proof that B is stoppable, and restart.sh then refuses to stop B forever
("the live process exposes no marker"). The incarnation is what makes A's
cleanup skip a file it did not write, and the controls below show the marker
disappearing without it.

WHAT THE CONTROLS REMOVE.
The pid on line 1 already protects the RECORD -- that is #4168's guarantee, and
the sibling suite pins it -- so a control that deletes only the incarnation
comparison changes nothing about the record, and would be a control that removes
no variable. The variable this change actually introduces is incarnation
awareness in cleanup AT ALL. C1 is therefore the cleanup a change that shipped
the marker WITHOUT it would have written: release the record by pid exactly as
before, and rm the new marker outright. C2 is the pre-#4168 baseline shape, an
unguarded rm of both.

HARNESS RULES inherited from watch-tasks-stream-sentinel-ownership.test.py, for
the same reasons documented there: every watcher runs in its OWN session because
cleanup() ends in `kill 0`; the workspace is a private temp tree and the
resolution is ASSERTED before any child is spawned; `fswatch` is a stub on the
child's PATH so Linux runs the same interaction as macOS. No `pkill -f` anywhere.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SENTINEL_SH = REPO / "src" / "watcher_sentinel.sh"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def sh(script: str, **kw) -> subprocess.CompletedProcess:
    """Run a snippet with the PRODUCTION helper sourced — never a copy of it."""
    return subprocess.run(["bash", "-c", f'. "{SENTINEL_SH}"\n{script}'],
                          capture_output=True, text=True, **kw)


def make_stub_fswatch(bin_dir: Path) -> None:
    """A pollable `fswatch`: it must actually EMIT, or "B keeps working" is untested.

    The blocking stub the sibling suite uses proves the EOF path and nothing
    else. This one prints every new .txt in the watched dir exactly as fswatch
    does (absolute physical path, one per line), so a task dropped after the
    race shows whether B's event loop is still live.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "fswatch"
    stub.write_text(
        "#!/bin/bash\n"
        "# The watched dir is fswatch's last argument, and fswatch emits PHYSICAL\n"
        "# paths — the watcher's parent-dir filter compares against `pwd -P`.\n"
        "for a in \"$@\"; do d=\"$a\"; done\n"
        "d=\"$(cd \"$d\" 2>/dev/null && pwd -P)\" || exit 1\n"
        "# A membership string, not `declare -A`: /bin/bash on macOS is 3.2 and\n"
        "# rejects associative arrays, which silently kills the stub and the test.\n"
        "seen=\"\"\n"
        "while :; do\n"
        "  for f in \"$d\"/*.txt; do\n"
        "    [ -f \"$f\" ] || continue\n"
        "    case \"$seen\" in *\"|$f|\"*) continue ;; esac\n"
        "    seen=\"$seen|$f|\"\n"
        "    printf '%s\\n' \"$f\"\n"
        "  done\n"
        "  sleep 0.2\n"
        "done\n"
    )
    stub.chmod(0o755)


class Watcher:
    """One watcher in its OWN session, so its `kill 0` cannot reach us."""

    def __init__(self, workspace: Path, bin_dir: Path, out: Path):
        self.env = dict(os.environ,
                        SUTANDO_WORKSPACE=str(workspace), SUTANDO_TEST_MODE="1",
                        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH','')}")
        # ASSERT THE RESOLVED PATH BEFORE SPAWNING: the state dir comes from
        # `sutando-config.sh`, not from this box, so a drift writes the real one.
        got = subprocess.run(["bash", "scripts/sutando-config.sh", "workspace"],
                             cwd=str(REPO), env=self.env,
                             capture_output=True, text=True).stdout.strip()
        if not got or Path(got).resolve() != workspace.resolve():
            raise SystemExit(
                "REFUSING TO SPAWN: the watcher would resolve\n"
                f"    {got}\n  but this test declared\n    {workspace}\n"
                "  Fix the isolation before running this test."
            )
        self.out = out
        self.fh = open(out, "ab")
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh"], cwd=str(REPO), env=self.env,
            stdout=self.fh, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def emitted(self) -> str:
        return self.out.read_text(errors="replace")

    def retire_via_eof(self) -> None:
        """Exit through the EXIT trap — the only path that runs cleanup()."""
        kids = subprocess.run(["pgrep", "-P", str(self.pid)],
                              capture_output=True, text=True).stdout.split()
        for k in kids:
            try:
                os.kill(int(k), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    def hard_stop(self) -> None:
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.fh.close()
        except OSError:
            pass


def wait_for(pred, timeout: float = 10.0, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(step)
    return False


def fields(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines()[1:]:
        if "=" in line:
            k, _, v = line.partition("=")
            out.setdefault(k, v)
    return out


def pid_line(path: Path) -> str:
    return path.read_text(errors="replace").splitlines()[0].strip()


def test_record_and_marker(ws: Path, bin_dir: Path, box: Path) -> None:
    sentinel = ws / "state" / "watch-tasks-stream.pid"
    marker = ws / "state" / "watch-tasks-stream.incarnation"
    w = Watcher(ws, bin_dir, box / "w0.out")
    try:
        if not wait_for(lambda: sentinel.exists() and "code_path=" in sentinel.read_text()):
            check("the watcher publishes a record", False,
                  "no record appeared; every assertion below would be vacuous")
            return
        check("the watcher STAYS UP after startup", w.alive(),
              "it exited during startup, so the record below is a corpse's")
        f = fields(sentinel)
        check("line 1 is still a bare pid (health-check.py int()s it)",
              pid_line(sentinel) == str(w.pid), f"read {pid_line(sentinel)!r}")
        for key in ("instance", "incarnation", "code_path", "version",
                    "started_at", "workspace"):
            check(f"the record carries {key}", key in f, f"record: {f}")
        check("instance is the key encoded in the resolved sentinel path",
              f.get("instance") == "", f"got {f.get('instance')!r} for watch-tasks-stream.pid")
        check("code_path is the ABSOLUTE path of the running script",
              f.get("code_path") == str(REPO / "src" / "watch-tasks-stream.sh"),
              f"got {f.get('code_path')!r}")
        check("workspace is the resolved workspace, not the repo",
              Path(f.get("workspace", "")).resolve() == ws.resolve(),
              f"got {f.get('workspace')!r}")
        check("version is a short sha or the honest \"unknown\"",
              bool(re.fullmatch(r"[0-9a-f]{7,40}|unknown", f.get("version", ""))),
              f"got {f.get('version')!r}")
        check("started_at is an epoch", f.get("started_at", "").isdigit(),
              f"got {f.get('started_at')!r}")
        check("the incarnation marker exists beside the sentinel", marker.exists(),
              "restart.sh refuses to stop a watcher that exposes none")
        check("  ...and carries exactly the incarnation the record claims",
              marker.exists() and marker.read_text().strip() == f.get("incarnation"),
              f"marker={marker.read_text().strip() if marker.exists() else None!r} "
              f"record={f.get('incarnation')!r}")
        check("the incarnation is not just the pid",
              f.get("incarnation", "") != str(w.pid) and str(w.pid) in f.get("incarnation", ""),
              f"got {f.get('incarnation')!r}")

        # B3: one line per start, so "which code is this pid running" is a read.
        log = ws / "logs" / "watcher-starts.log"
        check("a start line is appended to logs/watcher-starts.log", log.exists(),
              "no start log written")
        if log.exists():
            cols = log.read_text().strip().splitlines()[-1].split(" ")
            check("  ...with ts instance incarnation pid code_path version sentinel",
                  len(cols) == 7, f"got {len(cols)} columns: {cols}")
            if len(cols) == 7:
                check("  ...naming this pid, incarnation and sentinel",
                      cols[3] == str(w.pid) and cols[2] == f.get("incarnation")
                      and Path(cols[6]).resolve() == sentinel.resolve(),
                      f"line: {cols}")
                check("  ...and the version the record claims",
                      cols[5] == f.get("version"), f"line: {cols}")
        check("no publish temp is left in the state dir",
              not list((ws / "state").glob("*.new.*")),
              f"residue: {[p.name for p in (ws / 'state').glob('*.new.*')]}")
    finally:
        w.hard_stop()
        wait_for(lambda: not w.alive(), timeout=5)


def test_atomic_publish(box: Path) -> None:
    """A reader must never catch a half-written record.

    Republish in a tight loop from one process while this one reads: under a
    non-atomic writer the reader eventually sees a record whose code_path has
    not landed yet, which is exactly the state that makes a signaller refuse a
    watcher that IS ours.
    """
    d = box / "atomic"
    d.mkdir(parents=True, exist_ok=True)
    pf = d / "watch-tasks-stream.pid"
    writer = subprocess.Popen(
        ["bash", "-c",
         f'. "{SENTINEL_SH}"\n'
         f'for i in $(seq 1 400); do\n'
         f'  sentinel_write_record "{pf}" "$$" "" "inc-$i" "/x/watch-tasks-stream.sh" "v" "{d}"\n'
         f'done\n'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    torn = 0
    reads = 0
    while writer.poll() is None or reads < 50:
        try:
            body = pf.read_text(errors="replace")
        except OSError:
            continue
        reads += 1
        if body:
            head = body.splitlines()[0].strip()
            if not head.isdigit() or "code_path=" not in body or "workspace=" not in body:
                torn += 1
        if reads > 5000:
            break
    writer.wait()
    check("the publish is atomic — no reader ever saw a partial record",
          torn == 0, f"{torn} torn reads out of {reads}")
    check("  ...and the loop actually read the file (else the check is vacuous)",
          reads >= 50, f"only {reads} reads")
    check("no temp survives 400 republishes",
          not list(d.glob("*.new.*")), f"residue: {[p.name for p in d.glob('*.new.*')]}")


def test_handover_race(ws: Path, bin_dir: Path, box: Path) -> None:
    """A starts, B stamps over it, A exits LATE. B's record and marker survive."""
    sentinel = ws / "state" / "watch-tasks-stream.pid"
    marker = ws / "state" / "watch-tasks-stream.incarnation"
    a = b = None
    try:
        a = Watcher(ws, bin_dir, box / "a.out")
        if not wait_for(lambda: sentinel.exists() and "incarnation=" in sentinel.read_text()):
            check("watcher A publishes a record", False, "none appeared")
            return
        inc_a = fields(sentinel)["incarnation"]

        b = Watcher(ws, bin_dir, box / "b.out")
        if not wait_for(lambda: sentinel.exists()
                        and fields(sentinel).get("incarnation") not in ("", inc_a)):
            check("watcher B takes the sentinel over", False, f"still {inc_a!r}")
            return
        inc_b = fields(sentinel)["incarnation"]
        check("B's start replaces the record AND the marker",
              marker.read_text().strip() == inc_b,
              f"marker={marker.read_text().strip()!r} record={inc_b!r}")

        a.retire_via_eof()
        exited = wait_for(lambda: not a.alive(), timeout=15)
        check("A exits through its EXIT trap (so cleanup RUNS)", exited,
              "it never exited — the assertions below would pass vacuously")
        if not exited:
            return

        check("A's late cleanup leaves B's RECORD in place",
              sentinel.exists() and fields(sentinel).get("incarnation") == inc_b,
              f"record={fields(sentinel).get('incarnation') if sentinel.exists() else '<DELETED>'} "
              f"expected={inc_b}")
        check("A's late cleanup leaves B's MARKER in place",
              marker.exists() and marker.read_text().strip() == inc_b,
              f"marker={marker.read_text().strip() if marker.exists() else '<DELETED>'} "
              f"expected={inc_b} — without it restart.sh can never stop B")
        check("  ...and line 1 still names B",
              sentinel.exists() and pid_line(sentinel) == str(b.pid),
              f"read {pid_line(sentinel) if sentinel.exists() else '<DELETED>'}")

        # "B survived" must mean B still WORKS, not merely that its pid exists.
        (ws / "tasks" / "task-after-race.txt").write_text("task: probe\n")
        check("B keeps working after A's cleanup — its fswatch still emits",
              wait_for(lambda: "TASK_FILE: task-after-race.txt" in b.emitted(), timeout=15),
              f"B emitted: {b.emitted()!r}")
        check("  ...and B is still running", b.alive(), "B died, so the check above proved nothing")

        # Controls, on a COPY so the live watcher is never disturbed; the
        # docstring's WHAT THE CONTROLS REMOVE says why C1 is shaped this way.
        ctl = box / "ctl-state"
        shutil.rmtree(ctl, ignore_errors=True)
        shutil.copytree(ws / "state", ctl)
        ctl_pf = ctl / "watch-tasks-stream.pid"
        ctl_marker = ctl / "watch-tasks-stream.incarnation"
        check("the control fixture starts from B's live record and marker",
              ctl_pf.exists() and ctl_marker.read_text().strip() == inc_b,
              "the copy did not capture B's files, so the controls measure nothing")
        sh(f'sentinel_release_if_owner "{ctl_pf}" "{a.pid}"; rm -f "{ctl_marker}"')
        check("CONTROL: a cleanup with no incarnation awareness takes B's MARKER",
              not ctl_marker.exists(), "the control removed nothing")
        check("  ...while the pid on line 1 still saves B's record",
              ctl_pf.exists(),
              "the record went too — then the marker loss above is not the "
              "variable this guard removes")

        # C2: the pre-record baseline — an unguarded rm of both.
        shutil.rmtree(ctl, ignore_errors=True)
        shutil.copytree(ws / "state", ctl)
        ctl_pf.unlink(missing_ok=True)
        ctl_marker.unlink(missing_ok=True)
        check("CONTROL: the unguarded cleanup loses B's record AND its marker",
              not ctl_pf.exists() and not ctl_marker.exists(), "fixture did not apply")
    finally:
        for w in (b, a):
            if w is not None:
                w.hard_stop()


def main() -> int:
    print("watch-tasks-stream sentinel record:")
    box = Path(tempfile.mkdtemp(prefix="sentinel-record-"))
    bin_dir = box / "bin"
    make_stub_fswatch(bin_dir)
    try:
        for name in ("a", "b"):
            ws = box / f"ws-{name}"
            for d in ("tasks", "results", "state", "logs"):
                (ws / d).mkdir(parents=True, exist_ok=True)
        test_record_and_marker(box / "ws-a", bin_dir, box)
        test_atomic_publish(box)
        test_handover_race(box / "ws-b", bin_dir, box)
    finally:
        shutil.rmtree(box, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All sentinel-record checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
