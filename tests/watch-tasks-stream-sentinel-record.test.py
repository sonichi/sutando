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

import importlib.util
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


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
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
              Path(f.get("code_path", "")).resolve()
              == (REPO / "src" / "watch-tasks-stream.sh").resolve(),
              f"got {f.get('code_path')!r}")
        check("workspace is the resolved workspace, not the repo",
              Path(f.get("workspace", "")).resolve() == ws.resolve(),
              f"got {f.get('workspace')!r}")
        check("version is a short sha or the honest \"unknown\"",
              bool(re.fullmatch(r"[0-9a-f]{7,40}|unknown", f.get("version", ""))),
              f"got {f.get('version')!r}")
        # Provenance comes through src/git_binary.py, the resolver that never
        # spawns the Xcode-CLT stub; the record must carry exactly its answer.
        resolver_says = _load("git_binary", "src/git_binary.py").short_head(str(REPO))
        check("  ...and it is the resolver's answer (src/git_binary.py short_head)",
              f.get("version") == resolver_says,
              f"record {f.get('version')!r}, resolver {resolver_says!r}")
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


# The pre-fix steal, verbatim in shape: probe, `rm -rf`, `mkdir`. Two ops, so
# two stealers interleave and both return 0 — the control for the arm below.
UNSAFE_ACQUIRE = """
acquire() {
  local lk dl
  lk="$(sentinel_lock_path "$1")"
  dl=$(( $(date +%s) + 0 ))
  while ! mkdir "$lk" 2>/dev/null; do
    if find "$lk" -maxdepth 0 -mmin +1 2>/dev/null | grep -q .; then
      rm -rf "$lk"
      continue
    fi
    [ "$(date +%s)" -lt "$dl" ] || return 1
    sleep 0.05
  done
  return 0
}
"""

SHIPPED_ACQUIRE = """
acquire() { sentinel_lock_acquire "$1" 0; }
"""

# The pre-fix steal, probing through the shared predicate so the stale-observer
# hook below can interleave it: observe, then `rm -rf` whatever is there now.
UNSAFE_PROBE_ACQUIRE = """
acquire() {
  local lk
  lk="$(sentinel_lock_path "$1")"
  while ! mkdir "$lk" 2>/dev/null; do
    if sentinel_lock_abandoned "$lk"; then
      rm -rf "$lk"
      continue
    fi
    return 1
  done
  return 0
}
"""


def lock_race(box: Path, tag: str, impl: str, trials: int, n: int,
              abandoned: bool = True) -> "tuple[int, int, int, list[str]]":
    """N real PROCESSES race for one lock, `trials` times. -> (multi, one, zero).

    Separate processes, not `( … ) &` subshells: a subshell inherits its
    parent's `$$`, which no two real acquirers ever share.

    A 0s acquire timeout keeps a loser from spinning the default 10s — the
    winner never releases here, so every loser is destined to time out anyway.
    """
    root = box / "lockrace" / tag
    shutil.rmtree(root, ignore_errors=True)
    for i in range(trials):
        d = root / f"t{i}"
        lk = d / "watch-tasks-stream.lock"
        lk.mkdir(parents=True)
        (lk / "held.fixture").write_text("")     # a held lock is a STAMPED one
        if abandoned:
            old = time.time() - 600
            os.utime(lk / "held.fixture", (old, old))
            os.utime(lk, (old, old))
    acq = root / "acquire.sh"
    acq.write_text(f'. "{SENTINEL_SH}"\n{impl}\n'
                   'acquire "$1/watch-tasks-stream.pid" && echo WON >> "$1/out"\n')
    driver = (f'for d in "{root}"/t*; do\n'
              f'  : > "$d/out"\n'
              f'  for i in $(seq 1 {n}); do bash "{acq}" "$d" & done\n'
              f'  wait\n'
              f'done\n')
    subprocess.run(["bash", "-c", driver], capture_output=True, text=True)
    won = [(root / f"t{i}" / "out").read_text().count("WON") for i in range(trials)]
    multi = sum(1 for w in won if w > 1)
    one = sum(1 for w in won if w == 1)
    residue = [q.name for q in root.rglob("*.lock.acq.*")] + [q.name for q in root.rglob("*.lock.ste*")]
    return multi, one, trials - multi - one, residue


def test_lock_is_mutually_exclusive(box: Path) -> None:
    """`sentinel_lock_acquire` is what serialises the record against the marker.

    Two holders inside that section land a record from one writer beside a
    marker from the other — the mismatched pair the incarnation exists to make
    impossible — so the lock reintroduces B2 through the mechanism that closed it.
    """
    # 50x4, not more: CI caps a python suite at 120s. The vacuity control is
    # test_two_acquirers_both_abandoned's forced schedule, not a one-in-six race.
    trials, n = 50, 4
    multi, one, zero, residue = lock_race(box, "shipped", SHIPPED_ACQUIRE, trials, n)
    check(f"an ABANDONED lock has exactly ONE stealer ({trials} trials x {n} acquirers)",
          multi == 0 and one == trials, f"{multi} trials with >1 winner, {one} with 1, {zero} with 0")
    check("  ...and no acquire temp or steal lock is left behind", not residue, f"residue: {residue[:4]}")

    f_multi, f_one, f_zero, _ = lock_race(box, "held", SHIPPED_ACQUIRE, 25, n,
                                          abandoned=False)
    check("CONTROL: a FRESH held lock is never stolen — mkdir excludes all 25",
          f_multi == 0 and f_one == 0 and f_zero == 25,
          f"{f_multi} with >1 winner, {f_one} with 1, {f_zero} with 0")


# The interleaving FIXED: the first abandonment probe answers truthfully, then
# runs a second acquirer to completion, so the observation is stale by one acquisition.
STALE_OBSERVER = """
_real_abandoned="$(declare -f sentinel_lock_abandoned)"
eval "${_real_abandoned/sentinel_lock_abandoned/_probe_abandoned}"
sentinel_lock_abandoned() {
  local rc=0
  _probe_abandoned "$1" || rc=$?
  if [ ! -e "$RACE_DIR/hooked" ]; then
    : > "$RACE_DIR/hooked"
    bash "$RACE_B" "$RACE_DIR"
  fi
  return $rc
}
"""


def stale_observer_race(box: Path, tag: str, impl: str, both: bool) -> "tuple[int, str]":
    """A (hooked, `impl`) observes an abandoned lock; B (shipped) then acquires
    fresh; A resumes. -> (winners, residue listing). `both` also abandons a
    legacy secondary `.steal` directory beside the primary."""
    d = box / "stale-observer" / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    old = time.time() - 600
    lock = d / "watch-tasks-stream.lock"
    lock.mkdir()
    (lock / "held.dead.0.0").write_text("")
    os.utime(lock / "held.dead.0.0", (old, old))
    os.utime(lock, (old, old))
    if both:
        (d / "watch-tasks-stream.lock.steal").mkdir()
        os.utime(d / "watch-tasks-stream.lock.steal", (old, old))
    # B gets a real deadline: the acquisition may need more than one pass
    # (a recovery, then the take), and a B that merely timed out is not a loser.
    b = d / "b.sh"
    b.write_text(f'. "{SENTINEL_SH}"\n'
                 'sentinel_lock_acquire "$1/watch-tasks-stream.pid" 5 && echo B-WON >> "$1/out"\n')
    a = d / "a.sh"
    a.write_text(f'. "{SENTINEL_SH}"\n{impl}\n{STALE_OBSERVER}\n'
                 'acquire "$1/watch-tasks-stream.pid" && echo A-WON >> "$1/out"\n')
    (d / "out").write_text("")
    subprocess.run(["bash", str(a), str(d)], env=dict(os.environ, RACE_DIR=str(d), RACE_B=str(b)),
                   capture_output=True, text=True, timeout=60)
    won = (d / "out").read_text().split()
    return len(won), " ".join(won) or "<nobody>"


def test_lock_steal_is_keyed(box: Path) -> None:
    """A stealer removes only the lock object it observed abandoned.

    The review finding: recovery that is check-then-`rm -rf` lets a delayed
    observer delete a lock somebody acquired since the check, and two acquirers
    then both hold it. The shipped steal unlinks only a stamp that is itself
    abandoned and `rmdir`s (empty only), so a fresh holder's lock survives an
    observer however stale — with and without a legacy `.steal` beside it.
    """
    for both, label in ((False, "the primary lock abandoned"),
                        (True, "BOTH the primary and the legacy .steal abandoned")):
        n, who = stale_observer_race(box, f"shipped-{int(both)}", SHIPPED_ACQUIRE, both)
        check(f"{label}: a stale observer + a fresh acquirer -> exactly ONE winner",
              n == 1 and who == "B-WON", f"winners: {who}")
        n2, who2 = stale_observer_race(box, f"unsafe-{int(both)}", UNSAFE_PROBE_ACQUIRE, both)
        check("  CONTROL: the check-then-rm steal lets BOTH win under the same interleaving",
              n2 == 2, f"winners: {who2} — the hook does not produce the interleaving, "
                       f"so the arm above is vacuous")

    # The primitives, on the states a stealer or taker can meet.
    d = box / "steal-prim"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    lk = d / "watch-tasks-stream.lock"
    pf = d / "watch-tasks-stream.pid"
    old = time.time() - 600
    lk.mkdir(); (lk / "held.dead").write_text("")
    os.utime(lk / "held.dead", (old, old)); os.utime(lk, (old, old))
    sh(f'sentinel_lock_steal_abandoned "{lk}"')
    check("an abandoned stamp is unlinked; the directory stays, EMPTY, for a rename to take",
          lk.is_dir() and not list(lk.iterdir()), f"{list(lk.iterdir()) if lk.exists() else 'gone'}")
    r = sh(f'sentinel_lock_acquire "{pf}" 0 && echo TOOK || echo HELD')
    check("  ...and the next acquire takes it at once", r.stdout.strip() == "TOOK", r.stdout)
    shutil.rmtree(lk)
    lk.mkdir(); (lk / "held.fresh").write_text("")
    sh(f'sentinel_lock_steal_abandoned "{lk}"')
    check("a FRESH stamped lock survives a stealer (the stamp is younger than abandonment)",
          lk.exists() and (lk / "held.fresh").exists(), "removed — the delayed-observer hole")
    r = sh(f'sentinel_lock_acquire "{pf}" 0 && echo TOOK || echo HELD')
    check("  ...and no rename can replace it while it is stamped", r.stdout.strip() == "HELD", r.stdout)
    shutil.rmtree(lk)
    lk.mkdir()                                   # a legacy mkdir lock, or a taker that died mid-steal
    r = sh(f'sentinel_lock_acquire "{pf}" 0 && echo TOOK || echo HELD')
    check("an EMPTY directory at the path is not held: a rename takes it, fresh or not",
          r.stdout.strip() == "TOOK" and any(q.name.startswith("held.") for q in lk.iterdir()), r.stdout)
    shutil.rmtree(lk)
    check("a lock is never observable unstamped: the stamp is renamed in with the directory",
          not list(d.glob("*.lock.acq.*")), f"residue {[q.name for q in d.glob('*.lock.acq.*')]}")
    r = sh(f'sentinel_lock_acquire "{d}/watch-tasks-stream.pid" 1 && ls "{lk}" '
           f'&& sentinel_lock_release "{d}/watch-tasks-stream.pid"; ls -d "{lk}" 2>/dev/null || echo GONE')
    lines = r.stdout.split()
    check("a holder's stamp names it: held.<pid>.<epoch>.<random>",
          len(lines) == 2 and bool(re.fullmatch(r"held\.\d+\.\d+\.\d+", lines[0])), f"got {r.stdout!r}")
    check("  ...and release removes the stamp and the directory",
          len(lines) == 2 and lines[1] == "GONE", f"got {r.stdout!r}")
    # A holder that exited without releasing left a FRESH stamped lock: nobody
    # else's release may remove it, and it is not stealable until abandoned.
    sh(f'sentinel_lock_acquire "{d}/watch-tasks-stream.pid" 1')
    r = sh(f'sentinel_lock_release "{d}/watch-tasks-stream.pid"; sentinel_lock_acquire "{d}/watch-tasks-stream.pid" 0 && echo TOOK || echo HELD')
    check("another process's release removes nothing, and the fresh lock stays held",
          r.stdout.strip() == "HELD" and lk.exists(), f"got {r.stdout.strip()!r}")


# The pre-fix two-lock steal, verbatim: the secondary `.steal` lock's OWN stale
# recovery (its last two lines) is check-then-`rm -rf` — the review's finding.
OLD_TWO_LOCK_ACQUIRE = """
acquire() {
  local lock steal deadline
  lock="$(sentinel_lock_path "$1")"
  steal="${lock}.steal"
  deadline=$(( $(date +%s) + ${2:-10} ))
  while ! mkdir "$lock" 2>/dev/null; do
    if sentinel_lock_abandoned "$lock"; then
      if mkdir "$steal" 2>/dev/null; then
        if sentinel_lock_abandoned "$lock"; then
          rm -rf "$lock"
        fi
        rmdir "$steal" 2>/dev/null || true
        continue
      fi
      if sentinel_lock_abandoned "$steal"; then
        rm -rf "$steal"
      fi
    fi
    [ "$(date +%s)" -lt "$deadline" ] || return 1
    sleep 0.05
  done
  return 0
}
"""

# Pause a process after its PAUSE_AT-th abandonment probe — the answer is the
# real one, only the scheduling is forced — until the orchestrator says go.
PAUSE_HOOK = """
_real_abandoned="$(declare -f sentinel_lock_abandoned)"
eval "${_real_abandoned/sentinel_lock_abandoned/_probe_abandoned}"
_probe_n=0
sentinel_lock_abandoned() {
  local rc=0
  _probe_abandoned "$1" || rc=$?
  _probe_n=$((_probe_n + 1))
  if [ "$_probe_n" = "$PAUSE_AT" ]; then
    : > "$RACE_DIR/$TAG.paused"
    while [ ! -e "$RACE_DIR/$TAG.go" ]; do sleep 0.02; done
  fi
  return $rc
}
"""


def two_acquirers_both_abandoned(box: Path, tag: str, impl: str) -> str:
    """The review's interleaving, forced. Both the primary lock and a secondary
    `.steal` are abandoned. A pauses right after observing the SECONDARY object
    abandoned (its 2nd probe); B then runs as far as it can — under the old
    protocol into the serialized section (paused at its 4th probe, holding a
    fresh `.steal`); A resumes and finishes; B resumes and finishes.
    -> the winners, in order."""
    d = box / "two-acquirers" / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    old = time.time() - 600
    lock = d / "watch-tasks-stream.lock"
    lock.mkdir()
    (lock / "held.dead.0.0").write_text("")
    os.utime(lock / "held.dead.0.0", (old, old))
    os.utime(lock, (old, old))
    (d / "watch-tasks-stream.lock.steal").mkdir()
    os.utime(d / "watch-tasks-stream.lock.steal", (old, old))
    script = d / "acq.sh"
    script.write_text(f'. "{SENTINEL_SH}"\n{impl}\n{PAUSE_HOOK}\n'
                      'acquire "$1/watch-tasks-stream.pid" 5 && echo "$TAG-WON" >> "$1/out"\n')
    (d / "out").write_text("")

    def start(name: str, pause_at: int) -> subprocess.Popen:
        return subprocess.Popen(["bash", str(script), str(d)],
                                env=dict(os.environ, RACE_DIR=str(d), TAG=name, PAUSE_AT=str(pause_at)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def settled(p: subprocess.Popen, name: str) -> None:   # paused, or already done
        wait_for(lambda: (d / f"{name}.paused").exists() or p.poll() is not None, timeout=20)

    a = start("A", 2)
    settled(a, "A")
    b = start("B", 4)
    settled(b, "B")
    (d / "A.go").write_text("")
    wait_for(lambda: a.poll() is not None, timeout=20)
    (d / "B.go").write_text("")
    wait_for(lambda: b.poll() is not None, timeout=20)
    for p in (a, b):
        if p.poll() is None:
            p.kill()
    return " ".join((d / "out").read_text().split()) or "<nobody>"


def test_two_acquirers_both_abandoned(box: Path) -> None:
    """Deterministic: both lock directories abandoned, two acquirers, the
    review's delays forced — exactly one winner. The same forced schedule on the
    pre-fix two-lock protocol produces two, so the schedule is the one that
    matters and the arm is not vacuous."""
    who = two_acquirers_both_abandoned(box, "shipped",
                                       'acquire() { sentinel_lock_acquire "$1" "$2"; }')
    check("both directories abandoned, two acquirers, forced delays -> exactly ONE winner",
          len(who.split()) == 1 and who != "<nobody>", f"winners: {who}")
    who_old = two_acquirers_both_abandoned(box, "old-two-lock", OLD_TWO_LOCK_ACQUIRE)
    check("  CONTROL: the pre-fix two-lock steal lets BOTH win under the same forced delays",
          who_old == "A-WON B-WON", f"winners: {who_old}")


def test_start_log_never_blocks(box: Path, bin_dir: Path) -> None:
    """The optional start log cannot stall the watcher: a FIFO with no reader at
    logs/watcher-starts.log, and the production watcher still publishes its
    record and still emits tasks. The plain `>>` control shows the FIFO blocks."""
    ws = box / "ws-fifo"
    for d in ("tasks", "results", "state", "logs"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    log = ws / "logs" / "watcher-starts.log"
    os.mkfifo(log)
    try:
        subprocess.run(["bash", "-c", 'printf x >> "$1"', "_", str(log)], timeout=2)
        check("CONTROL: a plain >> onto the reader-less FIFO blocks", False,
              "it returned — the FIFO does not block here, so the arm below proves nothing")
    except subprocess.TimeoutExpired:
        check("CONTROL: a plain >> onto the reader-less FIFO blocks", True)

    sentinel = ws / "state" / "watch-tasks-stream.pid"
    w = Watcher(ws, bin_dir, box / "fifo.out")
    try:
        published = wait_for(lambda: sentinel.exists() and "code_path=" in sentinel.read_text(),
                             timeout=15)
        check("FIFO without a reader: the watcher still publishes its record (bounded 15s)",
              published, "no record — the start log blocked startup")
        check("  ...and is still alive", w.alive(), "it died")
        (ws / "tasks" / "task-behind-fifo.txt").write_text("task: probe\n")
        check("  ...and still emits a task",
              wait_for(lambda: "TASK_FILE: task-behind-fifo.txt" in w.emitted(), timeout=15),
              f"emitted: {w.emitted()!r}")
        check("  ...and wrote nothing into the FIFO (only a regular file is a log)",
              log.exists() and __import__("stat").S_ISFIFO(log.stat().st_mode),
              "the FIFO was replaced")
    finally:
        w.hard_stop()
        wait_for(lambda: not w.alive(), timeout=5)


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
        test_lock_is_mutually_exclusive(box)
        test_lock_steal_is_keyed(box)
        test_two_acquirers_both_abandoned(box)
        test_start_log_never_blocks(box, bin_dir)
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
