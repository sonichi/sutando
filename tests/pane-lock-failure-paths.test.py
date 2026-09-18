#!/usr/bin/env python3
"""The pane lock's failure and cleanup paths, which decide whether a writer SENDS.

`tests/pane-lock-collision.test.py` covers contention — an owner holds the lock and
every real writer defers. This file covers what happens when the lock machinery
itself fails, because those branches decide the same question and a wrong answer
there sends into a pane the caller never owned.

Each case asserts the REFUSAL or the swallow, not merely that the line executed.
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSION = "sutando-core"
SOCK = "/tmp/sutando-pane-lock-failure.sock"

sys.path.insert(0, str(REPO / "src"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _repo_whose_lock_path_is(td: Path, emitted: str) -> Path:
    """A fake repo whose tmux-pane-lock.sh emits `emitted` as the lock path.

    The real script owns path derivation; stubbing it is the only way to present
    a path the process cannot open without tampering with the filesystem.
    """
    scripts = td / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    s = scripts / "tmux-pane-lock.sh"
    s.write_text("#!/bin/sh\n" f'printf "%s\\n" "{emitted}"\n' "exit 0\n")
    s.chmod(0o755)
    return td


def case_unopenable_lock_refuses() -> list[str]:
    """A lock path that cannot be opened must yield False, not raise and not send.

    Covers src/tmux_pane_lock.py 45-47. A directory standing where the lock file
    belongs makes os.open raise EISDIR; the contract is refuse-and-defer.
    """
    fails: list[str] = []
    mod = _load("tmux_pane_lock", "src/tmux_pane_lock.py")
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        a_directory = td / "not-a-file"
        a_directory.mkdir()
        repo = _repo_whose_lock_path_is(td, str(a_directory))

        # Control: the stub really does hand back that path.
        got = mod.lock_path(SOCK, SESSION, repo=repo)
        if got != str(a_directory):
            fails.append(f"control: lock_path returned {got!r}, expected the directory")
            return fails

        try:
            with mod.pane_lock(SOCK, SESSION, repo=repo) as held:
                if held is not False:
                    fails.append(f"unopenable lock yielded {held!r}; must be False so the caller defers")
        except OSError as exc:
            fails.append(f"unopenable lock raised {exc!r}; must refuse quietly, not propagate")
    return fails


def case_undecidable_lock_refuses() -> list[str]:
    """A lock path that cannot be DERIVED must also yield False.

    The neighbouring guard to 45-47: lock_path returns None and pane_lock yields
    False rather than guessing a path.
    """
    fails: list[str] = []
    mod = _load("tmux_pane_lock", "src/tmux_pane_lock.py")
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        scripts = td / "scripts"
        scripts.mkdir(parents=True)
        s = scripts / "tmux-pane-lock.sh"
        s.write_text("#!/bin/sh\nexit 3\n")  # derivation fails
        s.chmod(0o755)
        if mod.lock_path(SOCK, SESSION, repo=td) is not None:
            fails.append("control: a failing derivation should give None")
            return fails
        with mod.pane_lock(SOCK, SESSION, repo=td) as held:
            if held is not False:
                fails.append(f"underivable lock yielded {held!r}; must be False")
    return fails


class _Shim:
    """Proxy a stdlib module, overriding ONE attribute for one module's global name.

    Assigning to `mod.os.close` would rebind the real `os` module process-wide —
    measured: it broke the subprocess that derives the lock path, so the case
    failed its own control instead of exercising the branch. Rebinding the
    module's global NAME (`mod.os = shim`) confines the override to this module.
    """

    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


def case_unlock_failure_is_swallowed() -> list[str]:
    """A failing flock(LOCK_UN) during cleanup must not escape the context manager.

    Covers src/tmux_pane_lock.py 65-66. Cleanup is best effort: raising here would
    turn a SUCCESSFUL send into an exception at the call site.
    """
    fails: list[str] = []
    mod = _load("tmux_pane_lock", "src/tmux_pane_lock.py")
    real_fcntl = mod.fcntl
    calls: list[int] = []

    def flaky(fd, op):
        calls.append(op)
        if op == fcntl.LOCK_UN:
            raise OSError(9, "Bad file descriptor")
        return real_fcntl.flock(fd, op)

    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        repo = _repo_whose_lock_path_is(td, str(td / "pane.lock"))
        mod.fcntl = _Shim(real_fcntl, flock=flaky)
        try:
            with mod.pane_lock(SOCK, SESSION, repo=repo) as held:
                if held is not True:
                    fails.append(f"control: expected to hold an uncontended lock, got {held!r}")
        except OSError as exc:
            fails.append(f"cleanup unlock failure escaped as {exc!r}; must be swallowed")
        finally:
            mod.fcntl = real_fcntl
        if fcntl.LOCK_UN not in calls:
            fails.append("control: LOCK_UN was never attempted, so the branch was not exercised")
    return fails


def case_close_failure_is_swallowed() -> list[str]:
    """A failing os.close during cleanup must not escape either.

    Covers src/tmux_pane_lock.py 69-70, the same best-effort contract one line down.
    """
    fails: list[str] = []
    mod = _load("tmux_pane_lock", "src/tmux_pane_lock.py")
    real_os = mod.os
    closed: list[int] = []

    def flaky_close(fd):
        closed.append(fd)
        real_os.close(fd)
        raise OSError(9, "Bad file descriptor")

    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        repo = _repo_whose_lock_path_is(td, str(td / "pane.lock"))
        # Derive BEFORE shimming: lock_path shells out, and subprocess uses os.close.
        derived = mod.lock_path(SOCK, SESSION, repo=repo)
        if not derived:
            fails.append("control: lock path did not derive")
            return fails
        mod.os = _Shim(real_os, close=flaky_close)
        try:
            with mod.pane_lock(SOCK, SESSION, repo=repo) as held:
                if held is not True:
                    fails.append(f"control: expected to hold an uncontended lock, got {held!r}")
        except OSError as exc:
            fails.append(f"cleanup close failure escaped as {exc!r}; must be swallowed")
        finally:
            mod.os = real_os
        if not closed:
            fails.append("control: os.close was never called, so the branch was not exercised")
    return fails


def case_send_keys_reports_false_when_tmux_raises() -> list[str]:
    """send_keys must return False when the tmux call raises, never True.

    Covers src/core-input-watch.py 459-460. The return value is what the caller
    treats as "the key landed"; an exception path that reported success would
    claim a keystroke was delivered when nothing was sent.
    """
    fails: list[str] = []
    ciw = _load("core_input_watch", "src/core-input-watch.py")
    real_sub = ciw.subprocess
    ran: list[tuple] = []

    def raising_run(*a, **k):
        ran.append(a)
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=8)

    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        repo = _repo_whose_lock_path_is(td, str(td / "pane.lock"))
        # The lock must be HELD or this measures the not-held refusal instead.
        # Shim the module's global name: `ciw.subprocess.run = ...` rebinds stdlib.
        real_lock = ciw.pane_lock
        lock_mod = _load("tmux_pane_lock", "src/tmux_pane_lock.py")

        def held_lock(sock, session, *a, **k):
            return lock_mod.pane_lock(sock, session, repo=repo)

        ciw.pane_lock = held_lock
        ciw.subprocess = _Shim(real_sub, run=raising_run)
        try:
            got = ciw.send_keys(SOCK, SESSION, "watcher")
            if got is not False:
                fails.append(f"send_keys returned {got!r} when tmux raised; must be False")
        except Exception as exc:  # noqa: BLE001 - the point is that it must not escape
            fails.append(f"send_keys let {exc!r} escape; must report False instead")
        finally:
            ciw.subprocess = real_sub
            ciw.pane_lock = real_lock
        if not ran:
            fails.append("control: the tmux call never happened, so the except branch was not reached")
    return fails


CASES = [
    ("an unopenable lock path refuses rather than sending", case_unopenable_lock_refuses),
    ("an underivable lock path refuses rather than guessing", case_undecidable_lock_refuses),
    ("a failing unlock is swallowed by cleanup", case_unlock_failure_is_swallowed),
    ("a failing close is swallowed by cleanup", case_close_failure_is_swallowed),
    ("send_keys reports False when tmux raises", case_send_keys_reports_false_when_tmux_raises),
]


def main() -> int:
    failures = []
    for name, fn in CASES:
        try:
            fs = fn()
        except Exception as exc:  # noqa: BLE001
            fs = [f"case raised: {exc!r}"]
        if fs:
            failures.append(name)
            print(f"  ✗ {name}")
            for f in fs:
                print(f"      {f}")
        else:
            print(f"  ✓ {name}")
    if failures:
        print(f"\npane-lock failure paths: {len(failures)} FAILED")
        return 1
    print("\nEvery lock failure path refuses, and cleanup never escapes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
