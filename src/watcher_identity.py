"""Watcher identity: is a process THE task watcher, and which inbox does it read?

One anchored, tri-state policy for every reader that classifies processes by
argv -- the health check, the pool's per-worker bootstrap gate. Two failure
classes it exists to stop:

* The observer counted as a watcher. A substring test matches any command line
  that MENTIONS the script -- a `ps | grep watch-tasks-stream`, the wrapper
  running the very check -- and each one reads as another watcher. The argv
  must BE the invocation: a shell, then the script as the whole final path
  component.
* Unobserved read as absent. A `ps` that failed, timed out or answered non-zero
  proves nothing; a caller that treats its empty answer as "not a watcher"
  starts a duplicate watcher, or publishes a stranger's pid. Every answer here
  is True / False / None, and None is never a default -- callers disagree on
  what it should mean (over-counting costs delayed tasks, a wrong pid costs a
  killed stranger), so it is carried to them undecided.

Stdlib only, so a gate that runs before the rest of src/ is importable can use it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

WATCHER_SHELLS = ("sh", "bash", "zsh", "ksh")

# The script named as a whole final path component, so `x-watch-tasks-stream.sh`
# and a mention inside a longer word cannot match.
WATCHER_SCRIPT_NAME = "watch-tasks-stream.sh"
WATCHER_SCRIPT = re.compile(r"(?:^|[\s/])watch-tasks-stream\.sh(?=\s|$)")


def as_pid(tok) -> Optional[int]:
    try:
        return int(tok)
    except (TypeError, ValueError):
        return None


def proc_argv_vector(pid) -> Optional[List[str]]:
    """Real argv of `pid` as a LIST, or None when no authoritative read exists.

    A flattened argv cannot separate an operand containing a space from two
    operands, so the executed script is not recoverable from it by any rule.
    """
    try:  # linux: NUL-delimited, authoritative
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return [a for a in raw.decode("utf8", "replace").split("\0") if a]
    except Exception:  # noqa: BLE001 -- not linux, or gone
        pass
    try:  # darwin: KERN_PROCARGS2 carries argc then the real argv strings
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, int(pid))  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(262144)
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        data = buf.raw[:size.value]
        argc = int.from_bytes(data[:4], sys.byteorder)
        parts = data[4:].split(b"\0")
        i = 0
        while i < len(parts) and parts[i] == b"":
            i += 1
        i += 1                                   # the exec path
        while i < len(parts) and parts[i] == b"":
            i += 1
        out = []
        while i < len(parts) and len(out) < argc:
            out.append(parts[i].decode("utf8", "replace"))
            i += 1
        return out or None
    except Exception:  # noqa: BLE001 -- probe failure must not fail the caller
        return None


class Verdict(NamedTuple):
    """`watcher` is True / False / None (undecidable); `operands` are the tokens
    after the script, present only when `watcher` is True."""
    watcher: Optional[bool]
    operands: Optional[List[str]]


def classify_argv(argv: str, pid=None, argv_vector: Optional[Callable] = None) -> Verdict:
    """Decide from the EXECUTED script; the flattened `argv` only when no
    authoritative vector for `pid` exists, and only where it cannot mislead."""
    read = argv_vector if argv_vector is not None else proc_argv_vector
    vec = read(pid) if pid is not None else None
    if vec is not None and len(vec) >= 2:
        if vec[0].rsplit("/", 1)[-1] not in WATCHER_SHELLS:
            return Verdict(False, None)
        if vec[1].startswith("-"):
            return Verdict(False, None)
        if os.path.basename(vec[1]) == WATCHER_SCRIPT_NAME:
            return Verdict(True, list(vec[2:]))
        return Verdict(False, None)
    parts = argv.split()
    if len(parts) < 2:
        return Verdict(False, None)
    if parts[0].rsplit("/", 1)[-1] not in WATCHER_SHELLS:
        return Verdict(False, None)
    if parts[1].startswith("-"):
        return Verdict(False, None)
    # Authoritative only when argv ends at parts[1]: with more tokens the real
    # pathname may continue past a space and end in a different name.
    if WATCHER_SCRIPT.search(parts[1]) is not None:
        return Verdict(True, []) if len(parts) == 2 else Verdict(None, None)
    if len(parts) == 2:
        return Verdict(False, None)
    # Only a later token matches, and a spaced script path is the same string as a
    # script plus arguments -- nothing here can decide between them.
    return Verdict(None, None) if WATCHER_SCRIPT.search(argv) else Verdict(False, None)


def is_watcher_argv(argv: str, pid=None, argv_vector: Optional[Callable] = None) -> Optional[bool]:
    """True/False from the EXECUTED script; None when nothing can prove it."""
    return classify_argv(argv, pid, argv_vector).watcher


def watcher_script_path(argv: str, pid=None, argv_vector: Optional[Callable] = None) -> Optional[str]:
    """The script path this watcher EXECUTED, or None when argv cannot prove it.

    The executed path is the ownership signal: the script derives its repo from
    `$0`, and callers launch it by absolute path from an unrelated cwd.
    """
    read = argv_vector if argv_vector is not None else proc_argv_vector
    vec = read(pid) if pid is not None else None
    if vec is not None and len(vec) >= 2:
        if (vec[0].rsplit("/", 1)[-1] in WATCHER_SHELLS
                and not vec[1].startswith("-")
                and os.path.basename(vec[1]) == WATCHER_SCRIPT_NAME):
            return vec[1]
        return None
    parts = argv.split()
    # Only the two-token form is authoritative: past it, a spaced pathname and a
    # script-plus-arguments are the same string.
    if len(parts) == 2 and parts[0].rsplit("/", 1)[-1] in WATCHER_SHELLS:
        if os.path.basename(parts[1]) == WATCHER_SCRIPT_NAME:
            return parts[1]
    return None


def watcher_repo(argv: str, pid=None, argv_vector: Optional[Callable] = None,
                 cwd: Optional[str] = None) -> Optional[str]:
    """The repo owning this watcher, from its executed script, or None if unprovable.

    `cwd` resolves a RELATIVE script operand and nothing else — an absolute path
    means cwd is irrelevant, which is where inferring ownership from cwd broke.
    """
    script = watcher_script_path(argv, pid, argv_vector)
    if not script:
        return None
    if not os.path.isabs(script):
        if not cwd:
            return None
        script = os.path.join(cwd, script)
    # <repo>/src/watch-tasks-stream.sh -> <repo>
    parent = os.path.dirname(os.path.normpath(script))
    return os.path.dirname(parent) or None


def owns_watcher(argv: str, repo: str, pid=None, argv_vector: Optional[Callable] = None,
                 cwd: Optional[str] = None) -> Optional[bool]:
    """True/False/None — None means argv could not prove ownership either way, and
    an unprovable owner must never be treated as a foreign one."""
    got = watcher_repo(argv, pid, argv_vector, cwd)
    if got is None or not repo:
        return None
    return os.path.normpath(got) == os.path.normpath(repo)


class Inspection(NamedTuple):
    """`observed` False: `ps` proved nothing (failed, timed out, non-zero, empty),
    and `watcher` is then None -- an unobserved process is not a proven non-watcher."""
    observed: bool
    watcher: Optional[bool]
    operands: Optional[List[str]]
    argv: str
    reason: str


def inspect_pid(pid, run: Callable = subprocess.run, argv_vector: Optional[Callable] = None,
                timeout: float = 10) -> Inspection:
    """Classify one live pid. The command column only: `ps eww` would print the
    process's environment, which carries credentials on a Sutando host."""
    try:
        out = run(["ps", "-o", "command=", "-p", str(pid)],
                  capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Inspection(False, None, None, "", f"ps timed out after {timeout}s for pid {pid}")
    except Exception as e:  # noqa: BLE001 -- ps missing, signalled, unrunnable
        return Inspection(False, None, None, "", f"ps could not run for pid {pid}: {e}")
    rc = getattr(out, "returncode", None)
    argv = (getattr(out, "stdout", "") or "").strip()
    if rc != 0 or not argv:
        return Inspection(False, None, None, "",
                          f"ps answered rc {rc} with {'no' if not argv else 'a'} command for pid {pid}")
    verdict = classify_argv(argv, pid, argv_vector)
    return Inspection(True, verdict.watcher, verdict.operands, argv,
                      "argv could not be decided" if verdict.watcher is None else "")


def ps_watcher_index(ps_output: str, is_watcher: Optional[Callable] = None) -> tuple:
    """(watcher pid -> ppid, every pid in the snapshot) from ONE `ps -Ao pid,ppid,args`
    parse. Both the tree walk and an ownership split need this; two parses could
    disagree about a process that exited between them."""
    decide = is_watcher if is_watcher is not None else is_watcher_argv
    me = str(os.getpid())
    parent: dict = {}
    live: set = set()
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        live.add(parts[0])
        if parts[0] == me:
            continue
        # None is UNKNOWN: count it, because a missed watcher starts a second
        # one and every task is then processed twice.
        if decide(parts[2], as_pid(parts[0])) is False:
            continue
        parent[parts[0]] = parts[1]
    return parent, live


def watcher_trees(ps_output: Optional[str] = None, is_watcher: Optional[Callable] = None) -> dict:
    """Map root PID -> set of PIDs for each distinct watcher TREE running.

    Each watcher is several processes (a shell wrapper, the script, a subshell),
    so counting matching lines overcounts. A "root" is a match whose parent is
    not itself a match -- one per independent watcher. Callers need the whole
    tree to tell which one owns a sentinel PID (the sentinel records the
    script's PID, not the wrapper's).

    `ps -Ao` + filtering rather than `pgrep -f watch-tasks-stream`: pgrep would
    match the caller. Our own argv is dropped explicitly anyway.
    """
    if ps_output is None:
        try:
            ps_output = subprocess.run(["ps", "-Ao", "pid,ppid,args"],
                                       capture_output=True, text=True,
                                       timeout=5).stdout
        except Exception:  # noqa: BLE001
            return {}
    parent, _live = ps_watcher_index(ps_output, is_watcher)
    trees: dict = {}
    for pid in parent:
        root, seen = pid, set()
        while parent.get(root) in parent and root not in seen:
            seen.add(root)
            root = parent[root]
        trees.setdefault(root, set()).add(pid)
    return trees


def main(argv=None) -> int:
    """`watcher_identity.py <pid>` -> `watcher`, `not-watcher`, `dead` or
    `unknown` on stdout, with `why=` beneath. Exit 0 when decided, 2 when not,
    so a shell adapter cannot read an unobservable `ps` as a proven answer."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: watcher_identity.py <pid>", file=sys.stderr)
        return 64
    seen = inspect_pid(args[0])
    if not seen.observed:
        # Unobserved is two facts: gone, or unobservable — only the first
        # licenses cleanup. An observed process is never `dead`, whatever its argv.
        pid = as_pid(args[0])
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                print("dead")
                print(f"why=no such process ({seen.reason})")
                return 0
            except Exception:  # noqa: BLE001 -- EPERM and friends: it exists, we cannot say more
                pass
        print("unknown")
        print(f"why={seen.reason}")
        return 2
    if seen.watcher is None:
        print("unknown")
        print(f"why={seen.reason or 'argv could not be decided'}")
        return 2
    print("watcher" if seen.watcher else "not-watcher")
    print(f"why={seen.argv}")
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())
