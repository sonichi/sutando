#!/usr/bin/env python3
"""voice-lock.py — the single implementation of the voice-agent PID-lock
transaction (design 1b; impl plan WS1 Step 3, amendments R3/S4/U1/Z1).

Serialization is an advisory ``fcntl.flock(LOCK_EX)`` on a *guard* file held
across each whole transaction; the JSON lock file
(``<workspace>/state/locks/voice-agent.pid``; root path pre-#2722) is owner *metadata* only. Every creator AND
replacer of the lock must hold the guard for the whole
stale-owner-resolution + acquisition sequence — delete-then-create without it
is racy (two contenders can both validate the stale lock; one creates a fresh
live lock, the other deletes it, violating "a live lock is never removed").

Structured lock schema v1 (created with exclusive ``wx`` open semantics —
never temp+rename, which could clobber a live lock)::

    {"v": 1, "lockId": "vl1-<uuid4>", "pid": <int>, "startTimeMs": <int>,
     "entry": "<abs path>", "workspace": "<abs path>"}

``lockId`` is random per acquisition (amendment R3); post-kill unlink re-reads
the lock and compares ``lockId`` before removing it. Legacy bare-pid locks are
readable for one release (``read``/``acquire`` fallback). Unparseable/partial
content normalizes to ``kind:"unknown"`` — readers must treat that as "unknown
lock", never as stale.

Subcommands (all print exactly one JSON object on stdout):

  acquire     — create the lock, replacing a validated-stale owner. Exit 0 on
                success, 7 when a live owner holds it (``{"code":"held"}``).
  read        — normalize lock content to
                ``{"kind":"structured"|"legacy"|"unknown"|"absent", ...}``.
  release     — unlink only if the lock matches ``--pid`` (idempotent, exit 0).
  steal       — re-validate the recorded owner is dead (or PID-reused), then
                unlink. Refuses with exit 3 (``owner-alive``) otherwise. The
                only path that ever removes another process's lock.
  takeover    — the FULL guarded kill-and-replace transaction (amendment S4):
                validate → TERM → wait → KILL → revalidate → unlink, all under
                the held guard. Two modes (amendment Z1):
                  --mode adopted  (default) — U1's full identity check: the
                    lock pid must equal the ``--port`` LISTEN pid (lsof
                    cross-check), realpath-normalized ``lock.entry`` must match
                    an expected ``--entry`` AND appear in the live argv, and a
                    structured lock's startTimeMs must match ``ps -o lstart=``
                    within ±2 s. Any mismatch → ``takeover-blocked`` (exit 3),
                    no signal sent.
                  --mode owned — validate the supervisor-recorded ``--pid`` +
                    ``--start-time-ms`` + entry (plus the descendant lock
                    holder when present) and terminate the whole owned process
                    group. No listener requirement (a hung-before-bind or
                    listener-lost owned child is the watchdog-repair case, and
                    dev's tsx parent holds no listener itself).
  guard-hold  — take LOCK_EX|LOCK_NB on the guard, print ``{"ok":true}``, then
                block reading stdin until EOF; the kernel releases the flock
                when the holder dies with its parent.

Exit codes: 0 ok · 3 refused (owner-alive / takeover-blocked / guard held) ·
4 invalid usage or validation impossible · 5 kill failed · 7 lock held by a
live owner. Stdlib-only; runs on the engine's relocatable python (resolve it
via ``scripts/sutando-config.sh python-bin``) and any system python3.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid

SCHEMA_V = 1
START_TIME_TOLERANCE_MS = 2000
DEFAULT_TERM_WAIT_MS = 3000
DEFAULT_KILL_WAIT_MS = 2000
POLL_INTERVAL_S = 0.1


def _emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def _tool(name, fallback):
    return shutil.which(name) or fallback


def _run(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def _ps(args):
    return _run([_tool("ps", "/bin/ps")] + args)


def pid_alive(pid):
    """True when a RUNNING process exists with this pid. A zombie (exited,
    unreaped — `ps` state Z) counts as dead: it runs nothing, holds no port,
    and `os.kill(pid, 0)` alone would wedge every wait loop that polls it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    stat = _ps(["-p", str(pid), "-o", "stat="]).strip()
    if stat.startswith("Z"):
        return False
    return True


def pid_start_time_ms(pid):
    """Start time of `pid` in epoch ms via `ps -o lstart=` — the single
    algorithm every caller shares (comparisons use ±2 s tolerance)."""
    out = _ps(["-p", str(pid), "-o", "lstart="]).strip()
    if not out:
        return None
    try:
        # lstart is the C-locale asctime form: 'Tue Aug  5 10:00:00 2026'
        return int(time.mktime(time.strptime(out)) * 1000)
    except ValueError:
        return None


def pid_argv(pid):
    return _ps(["-p", str(pid), "-o", "args="]).strip()


def pid_pgid(pid):
    try:
        return os.getpgid(pid)
    except OSError:
        out = _ps(["-p", str(pid), "-o", "pgid="]).strip()
        try:
            return int(out)
        except ValueError:
            return None


def pgid_member_pids(pgid):
    """Live (non-zombie) members of a process group."""
    out = _ps(["-axo", "pid=,pgid=,stat="])
    members = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                if int(parts[1]) == pgid and not parts[2].startswith("Z"):
                    members.append(int(parts[0]))
            except ValueError:
                continue
    return members


def listener_pids(port):
    lsof = _tool("lsof", "/usr/sbin/lsof")
    out = _run([lsof, "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"])
    pids = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return pids


def start_times_match(a_ms, b_ms):
    if a_ms is None or b_ms is None:
        return False
    return abs(a_ms - b_ms) <= START_TIME_TOLERANCE_MS


def _realpath(p):
    try:
        return os.path.realpath(p)
    except OSError:
        return p


class Guard:
    """Advisory kernel lock on the guard file, held across the whole
    transaction. Blocking LOCK_EX — transactions are short and bounded."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


def read_lock(pidfile):
    """Normalize lock content. Unparseable/partial = "unknown" — never treated
    as stale by any caller while live evidence exists."""
    try:
        with open(pidfile, "r") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError:
        return {"kind": "unknown"}
    s = raw.strip()
    if not s:
        return {"kind": "unknown"}
    # Legacy bare-pid form first (readable for one release): a bare integer is
    # ALSO valid JSON, so json.loads cannot be the discriminator.
    try:
        pid = int(s.splitlines()[0].strip())
        return {"kind": "legacy", "pid": pid}
    except ValueError:
        pass
    try:
        data = json.loads(s)
    except ValueError:
        return {"kind": "unknown"}
    if not isinstance(data, dict):
        return {"kind": "unknown"}
    structured = (
        data.get("v") == SCHEMA_V
        and isinstance(data.get("pid"), int)
        and isinstance(data.get("startTimeMs"), int)
        and isinstance(data.get("entry"), str)
        and isinstance(data.get("workspace"), str)
        and isinstance(data.get("lockId"), str)
    )
    if structured:
        out = {"kind": "structured"}
        out.update(
            {
                k: data[k]
                for k in ("v", "lockId", "pid", "startTimeMs", "entry", "workspace")
            }
        )
        return out
    # Partial JSON: surface a pid when one is recoverable so acquire can
    # refuse to clobber while live evidence exists, but the kind stays unknown.
    out = {"kind": "unknown"}
    if isinstance(data.get("pid"), int):
        out["pid"] = data["pid"]
    return out


def _owner_liveness(lock):
    """Classify the recorded owner: 'live', 'stale', or 'no-evidence'.

    live  — a process with the recorded pid exists AND (for structured locks)
            its start time matches within tolerance.
    stale — the pid is gone, or a structured lock's start time mismatches
            (PID reuse).
    no-evidence — nothing checkable was recorded (unknown lock without a pid).
    """
    pid = lock.get("pid")
    if not isinstance(pid, int):
        return "no-evidence"
    if not pid_alive(pid):
        return "stale"
    if lock["kind"] == "structured":
        actual = pid_start_time_ms(pid)
        if actual is not None and not start_times_match(actual, lock["startTimeMs"]):
            return "stale"  # PID reuse
    return "live"


def _create_lock(pidfile, pid, entry, workspace):
    my_start = pid_start_time_ms(pid)
    if my_start is None:
        _emit(
            {
                "ok": False,
                "code": "start-time-unavailable",
                "detail": "cannot compute startTimeMs for pid %d via ps -o lstart=" % pid,
            },
            4,
        )
    record = {
        "v": SCHEMA_V,
        "lockId": "vl1-%s" % uuid.uuid4(),
        "pid": pid,
        "startTimeMs": my_start,
        "entry": _realpath(entry),
        "workspace": _realpath(workspace),
    }
    # Exclusive wx creation — the current mutual-exclusion guarantee. Under the
    # guard EEXIST means a concurrent creator won between our stale-unlink and
    # here, which cannot happen while we hold the guard; treat it as held.
    fd = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)
    return record


def cmd_acquire(args):
    with Guard(args.guard):
        lock = read_lock(args.pidfile)
        state = "absent" if lock["kind"] == "absent" else _owner_liveness(lock)
        if lock["kind"] != "absent":
            if state == "live":
                _emit({"code": "held", "holder": lock}, 7)
            if state == "no-evidence":
                # Unparseable with no live evidence → safe to replace (design
                # 1b: "stale/absent/unparseable-with-no-live-evidence").
                pass
            try:
                os.unlink(args.pidfile)
            except FileNotFoundError:
                pass
        # A live legacy record (#2722 pre-move owner) holds this acquisition;
        # stale-retire and the held-verdict both stay inside THIS transaction.
        legacy_path = getattr(args, "legacy_pidfile", None)
        if legacy_path:
            legacy = read_lock(legacy_path)
            if legacy["kind"] != "absent":
                if _owner_liveness(legacy) == "live":
                    _emit({"code": "held", "holder": legacy, "at": "legacy"}, 7)
                try:
                    os.unlink(legacy_path)
                except FileNotFoundError:
                    pass
        try:
            record = _create_lock(args.pidfile, args.pid, args.entry, args.workspace)
        except OSError as e:
            if e.errno == errno.EEXIST:
                _emit({"code": "held", "holder": read_lock(args.pidfile)}, 7)
            raise
        _emit({"ok": True, "lock": record})


def cmd_read(args):
    _emit(read_lock(args.pidfile))


def cmd_release(args):
    with Guard(args.guard):
        lock = read_lock(args.pidfile)
        if lock["kind"] not in ("structured", "legacy") or lock.get("pid") != args.pid:
            _emit({"ok": True, "released": False})
        if lock["kind"] == "structured" and pid_alive(args.pid):
            actual = pid_start_time_ms(args.pid)
            if actual is not None and not start_times_match(actual, lock["startTimeMs"]):
                # A different (reused-PID) process — not the recorded owner.
                _emit({"ok": True, "released": False})
        try:
            os.unlink(args.pidfile)
        except FileNotFoundError:
            pass
        _emit({"ok": True, "released": True})


def cmd_steal(args):
    with Guard(args.guard):
        lock = read_lock(args.pidfile)
        if lock["kind"] == "absent":
            _emit({"ok": True, "stolen": False, "code": "absent"})
        if lock["kind"] == "unknown":
            _emit({"ok": False, "code": "unknown-lock"}, 4)
        if lock["pid"] != args.expect_pid:
            _emit({"ok": False, "code": "owner-mismatch", "holder": lock}, 4)
        if pid_alive(lock["pid"]):
            actual = pid_start_time_ms(lock["pid"])
            expected = None
            if args.expect_start_time_ms is not None:
                expected = args.expect_start_time_ms
            elif lock["kind"] == "structured":
                expected = lock["startTimeMs"]
            if expected is None or actual is None or start_times_match(actual, expected):
                # Alive with no disproving start-time evidence → a live lock is
                # never removed.
                _emit({"ok": False, "code": "owner-alive"}, 3)
            # Alive but start-time mismatch = PID reuse → stale.
        try:
            os.unlink(args.pidfile)
        except FileNotFoundError:
            pass
        _emit({"ok": True, "stolen": True})


def cmd_guard_hold(args):
    fd = os.open(args.guard, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _emit({"ok": False, "code": "held"}, 3)
    print(json.dumps({"ok": True, "pid": os.getpid()}))
    sys.stdout.flush()
    # Hold until stdin EOF; the kernel releases the flock when we die with our
    # parent (long-lived hold, auto-release on crash).
    try:
        while sys.stdin.read(4096):
            pass
    except (OSError, KeyboardInterrupt):
        pass
    sys.exit(0)


def _argv_entry_matches(argv, entry):
    """True when the live argv names `entry`. Matches against the FULL args
    string (`ps -o args=`), never bare whitespace tokens: entry paths can
    contain spaces (e.g. under 'Application Support'), which tokenization
    would split into fragments that can never match (amendment U1). A
    candidate is any substring running from one whitespace boundary to
    another; absolute candidates match on realpath equality with `entry`,
    relative candidates when they are a path suffix of it."""
    if not argv:
        return False
    entry = _realpath(entry)
    n = len(argv)
    starts = [
        i
        for i in range(n)
        if not argv[i].isspace() and (i == 0 or argv[i - 1].isspace())
    ]
    ends = [
        i
        for i in range(1, n + 1)
        if (i == n or argv[i].isspace()) and not argv[i - 1].isspace()
    ]
    for s in starts:
        for e in ends:
            if e <= s:
                continue
            cand = argv[s:e]
            if cand.startswith("/"):
                if cand == entry or _realpath(cand) == entry:
                    return True
            elif entry.endswith("/" + cand):
                return True
    return False


def _validate_entry(lock_entry, expected_entries, argv):
    entry = _realpath(lock_entry)
    expected = [_realpath(e) for e in expected_entries]
    if expected and entry not in expected:
        return False, "lock.entry %r not among expected entry points" % entry
    if not _argv_entry_matches(argv, entry):
        return False, "lock.entry %r not present in live argv" % entry
    return True, None


def _terminate_and_wait(kill_fn, is_dead_fn, term_wait_ms, kill_wait_ms):
    """SIGTERM → bounded wait → SIGKILL → bounded wait. Returns
    (dead, used_kill)."""
    used_kill = False
    try:
        kill_fn(signal.SIGTERM)
    except ProcessLookupError:
        return True, used_kill
    deadline = time.monotonic() + term_wait_ms / 1000.0
    while time.monotonic() < deadline:
        if is_dead_fn():
            return True, used_kill
        time.sleep(POLL_INTERVAL_S)
    used_kill = True
    try:
        kill_fn(signal.SIGKILL)
    except ProcessLookupError:
        return True, used_kill
    except OSError:
        # e.g. EPERM from killpg against a group reduced to zombies — let the
        # bounded is_dead poll below decide.
        pass
    deadline = time.monotonic() + kill_wait_ms / 1000.0
    while time.monotonic() < deadline:
        if is_dead_fn():
            return True, used_kill
        time.sleep(POLL_INTERVAL_S)
    return is_dead_fn(), used_kill


def _unlink_after_revalidate(pidfile, expect_lock_id):
    """Post-kill unlink: re-read and compare lockId (structured) before
    removing — amendment R3 / design L264."""
    current = read_lock(pidfile)
    if current["kind"] == "absent":
        return True
    if expect_lock_id is not None and current.get("lockId") not in (None, expect_lock_id):
        return False
    try:
        os.unlink(pidfile)
    except FileNotFoundError:
        pass
    return True


def _takeover_adopted(args):
    lock = read_lock(args.pidfile)
    if lock["kind"] == "absent":
        _emit({"ok": True, "code": "no-lock"})
    if lock["kind"] == "unknown":
        _emit(
            {
                "ok": False,
                "code": "takeover-blocked",
                "detail": "lock content is unknown/unparseable — a live lock is never removed",
            },
            3,
        )
    pid = lock["pid"]
    lock_id = lock.get("lockId")
    if not pid_alive(pid):
        # Dead owner (or structured PID-reuse handled below): clear the stale
        # lock under the guard.
        if _unlink_after_revalidate(args.pidfile, lock_id):
            _emit({"ok": True, "code": "stale-cleared", "stalePid": pid})
        _emit({"ok": False, "code": "takeover-blocked", "detail": "lock replaced concurrently"}, 3)
    if lock["kind"] == "structured":
        actual = pid_start_time_ms(pid)
        if actual is not None and not start_times_match(actual, lock["startTimeMs"]):
            # PID reuse: recorded owner is dead; the live pid is someone else.
            if _unlink_after_revalidate(args.pidfile, lock_id):
                _emit({"ok": True, "code": "stale-cleared", "stalePid": pid})
            _emit({"ok": False, "code": "takeover-blocked", "detail": "lock replaced concurrently"}, 3)
    # U1: the lock pid must equal the :<port> LISTEN pid — a plausible-looking
    # lock naming a non-listener must block, not signal.
    listeners = listener_pids(args.port)
    if pid not in listeners:
        _emit(
            {
                "ok": False,
                "code": "takeover-blocked",
                "detail": "lock pid %d is not the :%d listener (listeners: %s)"
                % (pid, args.port, listeners or "none"),
            },
            3,
        )
    argv = pid_argv(pid)
    if lock["kind"] == "structured":
        ok, why = _validate_entry(lock["entry"], args.entry or [], argv)
        if not ok:
            _emit({"ok": False, "code": "takeover-blocked", "detail": why}, 3)
        if args.workspace and _realpath(lock["workspace"]) != _realpath(args.workspace):
            _emit(
                {
                    "ok": False,
                    "code": "takeover-blocked",
                    "detail": "lock workspace %r does not match %r"
                    % (lock["workspace"], args.workspace),
                },
                3,
            )
    else:
        # Legacy lock: weaker evidence — require the live argv to match one of
        # the expected entry shapes.
        if not any(_argv_entry_matches(argv, _realpath(e)) for e in (args.entry or [])):
            _emit(
                {
                    "ok": False,
                    "code": "takeover-blocked",
                    "detail": "legacy lock pid %d argv does not match an expected voice-agent entry" % pid,
                },
                3,
            )
    dead, used_kill = _terminate_and_wait(
        lambda sig: os.kill(pid, sig),
        lambda: not pid_alive(pid),
        args.term_wait_ms,
        args.kill_wait_ms,
    )
    if not dead:
        _emit({"ok": False, "code": "kill-failed", "pid": pid}, 5)
    if not _unlink_after_revalidate(args.pidfile, lock_id):
        _emit({"ok": False, "code": "takeover-blocked", "detail": "lock replaced concurrently"}, 3)
    _emit({"ok": True, "code": "replaced", "terminatedPid": pid, "usedKill": used_kill})


def _takeover_owned(args):
    # Z1 owned mode: validate the supervisor-recorded pid/pgid + start time +
    # entry, then terminate the whole owned process group. No listener
    # requirement.
    if args.pid is None or args.start_time_ms is None:
        _emit({"ok": False, "code": "usage", "detail": "--mode owned requires --pid and --start-time-ms"}, 4)
    lock = read_lock(args.pidfile)
    lock_id = lock.get("lockId") if lock["kind"] == "structured" else None
    root = args.pid
    if not pid_alive(root):
        # Owned root already gone — clear a lock left by it or a descendant.
        if lock["kind"] in ("structured", "legacy") and not pid_alive(lock.get("pid", -1)):
            _unlink_after_revalidate(args.pidfile, lock_id)
            _emit({"ok": True, "code": "stale-cleared", "stalePid": lock.get("pid")})
        if lock["kind"] == "absent":
            _emit({"ok": True, "code": "no-lock"})
        _emit(
            {
                "ok": False,
                "code": "takeover-blocked",
                "detail": "owned pid %d is gone but the lock holder is alive — refusing" % root,
            },
            3,
        )
    actual = pid_start_time_ms(root)
    if not start_times_match(actual, args.start_time_ms):
        _emit(
            {
                "ok": False,
                "code": "takeover-blocked",
                "detail": "pid %d start time %s does not match supervisor-recorded %d"
                % (root, actual, args.start_time_ms),
            },
            3,
        )
    argv = pid_argv(root)
    if args.entry and not any(_argv_entry_matches(argv, _realpath(e)) for e in args.entry):
        _emit(
            {
                "ok": False,
                "code": "takeover-blocked",
                "detail": "owned pid %d argv does not match an expected voice-agent entry" % root,
            },
            3,
        )
    pgid = args.pgid if args.pgid is not None else pid_pgid(root)
    if pgid is None or pgid <= 0:
        _emit({"ok": False, "code": "takeover-blocked", "detail": "cannot establish pgid for pid %d" % root}, 3)
    # Descendant lock holder (dev tsx parent/worker topology): the lock's pid
    # must belong to the same owned process group.
    if lock["kind"] in ("structured", "legacy") and isinstance(lock.get("pid"), int):
        lpid = lock["pid"]
        if lpid != root and pid_alive(lpid) and pid_pgid(lpid) != pgid:
            _emit(
                {
                    "ok": False,
                    "code": "takeover-blocked",
                    "detail": "lock holder pid %d is outside the owned process group %d" % (lpid, pgid),
                },
                3,
            )
    dead, used_kill = _terminate_and_wait(
        lambda sig: os.killpg(pgid, sig),
        lambda: not pgid_member_pids(pgid),
        args.term_wait_ms,
        args.kill_wait_ms,
    )
    if not dead:
        _emit({"ok": False, "code": "kill-failed", "pgid": pgid}, 5)
    if not _unlink_after_revalidate(args.pidfile, lock_id):
        _emit({"ok": False, "code": "takeover-blocked", "detail": "lock replaced concurrently"}, 3)
    _emit({"ok": True, "code": "replaced", "terminatedPgid": pgid, "usedKill": used_kill})


def cmd_takeover(args):
    with Guard(args.guard):
        if args.mode == "owned":
            _takeover_owned(args)
        else:
            _takeover_adopted(args)


def main(argv=None):
    p = argparse.ArgumentParser(prog="voice-lock.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, guard=True):
        sp.add_argument("--pidfile", required=True)
        if guard:
            sp.add_argument("--guard", required=True)

    sp = sub.add_parser("acquire")
    common(sp)
    sp.add_argument("--pid", type=int, required=True)
    sp.add_argument("--entry", required=True)
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--legacy-pidfile", default=None)
    sp.set_defaults(fn=cmd_acquire)

    sp = sub.add_parser("read")
    common(sp, guard=False)
    sp.set_defaults(fn=cmd_read)

    sp = sub.add_parser("release")
    common(sp)
    sp.add_argument("--pid", type=int, required=True)
    sp.set_defaults(fn=cmd_release)

    sp = sub.add_parser("steal")
    common(sp)
    sp.add_argument("--expect-pid", type=int, required=True)
    sp.add_argument("--expect-start-time-ms", type=int, default=None)
    sp.set_defaults(fn=cmd_steal)

    sp = sub.add_parser("guard-hold")
    sp.add_argument("--guard", required=True)
    sp.set_defaults(fn=cmd_guard_hold)

    sp = sub.add_parser("takeover")
    common(sp)
    sp.add_argument("--mode", choices=("adopted", "owned"), default="adopted")
    sp.add_argument("--workspace", default=None)
    sp.add_argument("--port", type=int, default=9900)
    sp.add_argument("--entry", action="append", default=[])
    sp.add_argument("--pid", type=int, default=None)
    sp.add_argument("--start-time-ms", type=int, default=None)
    sp.add_argument("--pgid", type=int, default=None)
    sp.add_argument("--term-wait-ms", type=int, default=DEFAULT_TERM_WAIT_MS)
    sp.add_argument("--kill-wait-ms", type=int, default=DEFAULT_KILL_WAIT_MS)
    sp.set_defaults(fn=cmd_takeover)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
