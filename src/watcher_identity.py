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


def watcher_role(operands: Optional[List[str]]) -> Optional[str]:
    """The `--role VALUE` (or `--role=VALUE`) operand, or None if absent."""
    if not operands:
        return None
    for i, tok in enumerate(operands):
        if tok == "--role" and i + 1 < len(operands):
            return operands[i + 1]
        if tok.startswith("--role="):
            return tok.split("=", 1)[1] or None
    return None


def watcher_inbox(operands: Optional[List[str]]) -> Optional[str]:
    """The `--inbox VALUE` (or `--inbox=VALUE`) operand, or None if absent.

    Distinct from the positional tasks-dir operand: an instance whose inbox
    comes from $SUTANDO_TASKS_DIR (env, not argv) leaves no positional trace,
    so only an explicit tag is a reliable cross-process inbox identity.
    """
    if not operands:
        return None
    for i, tok in enumerate(operands):
        if tok == "--inbox" and i + 1 < len(operands):
            return operands[i + 1]
        if tok.startswith("--inbox="):
            return tok.split("=", 1)[1] or None
    return None


INBOX_TAG_FLAT = re.compile(r"--inbox(?:=|\s+)(.+?)(?=\s+--[a-z]|\s*$)")


def positional_inbox(operands: Optional[List[str]]) -> Optional[str]:
    """The first bare operand: an untagged watcher names its inbox only there."""
    if not operands:
        return None
    skip = False
    for tok in operands:
        if skip:
            skip = False
            continue
        if tok in ("--role", "--inbox"):
            skip = True
            continue
        if tok.startswith("-"):
            continue
        return tok
    return None


def state_dir_for_inbox(inbox: Optional[str]) -> Optional[str]:
    """The state dir whose sentinels cover `inbox`: the workspace the launcher
    names, else the inbox's parent, the same fallback the watcher itself uses."""
    ws = os.environ.get("SUTANDO_WORKSPACE_DIR")
    if ws:
        return os.path.join(ws, "state")
    want = canonical_inbox(inbox)
    return None if want is None else os.path.join(os.path.dirname(want), "state")


def sentinel_names_pid(pid: Optional[int], state_dir: Optional[str]) -> bool:
    """True only when a readable watcher sentinel under `state_dir` holds `pid`.
    Anything unreadable is not a stamp, so it never counts as ready."""
    if pid is None or not state_dir:
        return False
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from util_paths import watcher_sentinel_paths
        for sentinel in watcher_sentinel_paths(state_dir):
            try:
                if int(sentinel.read_text().strip().split()[0]) == pid:
                    return True
            except (OSError, ValueError, IndexError):
                continue
    except Exception:  # noqa: BLE001 -- the helper itself is unavailable: not a stamp
        return False
    return False


def canonical_inbox(path: Optional[str]) -> Optional[str]:
    """One spelling per inbox, so a trailing slash, a doubled slash or a
    /private symlink prefix cannot make one directory look like two."""
    if not path:
        return None
    return os.path.realpath(os.path.expanduser(path.strip()))


def flat_inbox(argv: str) -> Optional[str]:
    """The `--inbox` value of a FLATTENED argv: runs to the next `--flag` or the
    end, so a value containing a space (the default desktop root has one) is kept whole."""
    m = INBOX_TAG_FLAT.search(argv)
    return m.group(1).strip() if m else None


def role_present(role: str, inbox: Optional[str] = None, ps_output: Optional[str] = None,
                 is_watcher: Optional[Callable] = None,
                 argv_vector: Optional[Callable] = None,
                 run: Callable = subprocess.run,
                 ready: bool = False, state_dir: Optional[str] = None) -> Optional[bool]:
    """Is a watcher with `--role role` (and, when `inbox` is given, an `--inbox`
    naming the same directory) present anywhere on the host?

    Host-wide `ps`, not tmux/session-scoped: two instances (core and a worker,
    or two workers) can each run a same-`role` watcher for DIFFERENT inboxes at
    once, so a role match alone cannot tell a caller whether ITS OWN inbox is
    covered. Passing `inbox` closes that; omitting it keeps the host-wide answer
    for a caller that genuinely wants "is ANY session-role watcher running".

    Every process is classified from the ONE snapshot (plus the authoritative
    argv vector where one exists); no second per-pid `ps`, which would disagree
    with the snapshot about anything that exited in between, and which a host
    with no such pid answers with a failure indistinguishable from "unknown".

    True/False/None: None means the snapshot itself was unobservable, or a
    watcher-shaped line could not be decided AND could be for this inbox. A
    line whose tag names a different inbox is decidably not ours, so it never
    turns a clean answer into "unknown".

    `ready`: a matching watcher counts only once the inbox's sentinel names its
    pid, which the watcher stamps after a real event round-trip; a process that
    exists but has not stamped is decidably "no", never "unknown".
    """
    if ps_output is None:
        try:
            result = run(["ps", "-Ao", "pid,ppid,args"],
                         capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001
            return None
        # run() doesn't raise on a non-zero exit -- checked explicitly, or a
        # failed ps reads as a clean empty scan instead of unknown.
        if getattr(result, "returncode", None) != 0:
            return None
        ps_output = result.stdout
    want = canonical_inbox(inbox)
    me = str(os.getpid())
    saw_undecidable = False
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0] == me:
            continue
        pid, argv = parts[0], parts[2]
        if is_watcher is not None and is_watcher(argv, as_pid(pid)) is False:
            continue
        verdict = classify_argv(argv, as_pid(pid), argv_vector)
        if verdict.watcher is False:
            continue
        if verdict.watcher is None:
            tagged = canonical_inbox(flat_inbox(argv))
            if want is not None and tagged is not None and tagged != want:
                continue
            saw_undecidable = True
            continue
        if watcher_role(verdict.operands) != role:
            continue
        if want is not None and canonical_inbox(watcher_inbox(verdict.operands)) != want:
            continue
        if ready and not sentinel_names_pid(as_pid(pid), state_dir or state_dir_for_inbox(inbox)):
            continue
        return True
    return None if saw_undecidable else False


def standby_present(inbox: str, ps_output: Optional[str] = None,
                    argv_vector: Optional[Callable] = None,
                    run: Callable = subprocess.run) -> Optional[bool]:
    """Is a watcher that is NOT session-role serving `inbox` (tagged or by its
    positional operand)? The external standby is untagged, so the tag alone
    cannot find it. None when the snapshot is unobservable or undecidable."""
    if ps_output is None:
        try:
            result = run(["ps", "-Ao", "pid,ppid,args"],
                         capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001
            return None
        if getattr(result, "returncode", None) != 0:
            return None
        ps_output = result.stdout
    want = canonical_inbox(inbox)
    me = str(os.getpid())
    saw_undecidable = False
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0] == me:
            continue
        pid, argv = parts[0], parts[2]
        verdict = classify_argv(argv, as_pid(pid), argv_vector)
        if verdict.watcher is False:
            continue
        if verdict.watcher is None:
            tagged = canonical_inbox(flat_inbox(argv))
            if tagged is not None and tagged != want:
                continue
            saw_undecidable = True
            continue
        if watcher_role(verdict.operands) == "session":
            continue
        served = watcher_inbox(verdict.operands) or positional_inbox(verdict.operands)
        if canonical_inbox(served) != want:
            continue
        return True
    return None if saw_undecidable else False


def main(argv=None) -> int:
    """`watcher_identity.py <pid>` -> `watcher`, `not-watcher`, `dead` or
    `unknown` on stdout, with `why=` beneath. Exit 0 when decided, 2 when not,
    so a shell adapter cannot read an unobservable `ps` as a proven answer.

    `watcher_identity.py role-present <role> [--inbox VALUE] [--ready]` -> `yes`,
    `no` or `unknown` on stdout; `--ready` counts a session watcher only once
    its sentinel names it. `standby-present --inbox VALUE` asks the same of a
    watcher that is NOT session-role for that inbox. Exit 0 for yes/no (both decided), 2 only when the
    `ps` snapshot itself failed -- `unknown` must never read as `no` to a
    caller deciding whether to start a duplicate watcher."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "standby-present":
        rest = args[1:]
        inbox = None
        if len(rest) == 2 and rest[0] == "--inbox":
            inbox = rest[1]
        elif len(rest) == 1 and rest[0].startswith("--inbox="):
            inbox = rest[0].split("=", 1)[1] or None
        if not inbox:
            print("usage: watcher_identity.py standby-present --inbox VALUE", file=sys.stderr)
            return 64
        verdict = standby_present(inbox)
        if verdict is None:
            print("unknown")
            print("why=ps snapshot unavailable or undecidable")
            return 2
        print("yes" if verdict else "no")
        return 0
    if args and args[0] == "role-present":
        rest = args[1:]
        if not rest or rest[0].startswith("-"):
            print("usage: watcher_identity.py role-present <role> [--inbox VALUE] [--ready]", file=sys.stderr)
            return 64
        role = rest[0]
        inbox = None
        ready = False
        i = 1
        while i < len(rest):
            if rest[i] == "--inbox" and i + 1 < len(rest):
                inbox = rest[i + 1]
                i += 2
            elif rest[i].startswith("--inbox="):
                inbox = rest[i].split("=", 1)[1] or None
                i += 1
            elif rest[i] == "--ready":
                ready = True
                i += 1
            else:
                print("usage: watcher_identity.py role-present <role> [--inbox VALUE] [--ready]", file=sys.stderr)
                return 64
        verdict = role_present(role, inbox, ready=ready)
        if verdict is None:
            print("unknown")
            print("why=ps snapshot unavailable")
            return 2
        print("yes" if verdict else "no")
        return 0
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
