#!/usr/bin/env python3
"""Watcher identity and ownership — the ONE policy every signaller and reporter asks.

WHY THIS MODULE EXISTS. `kill -0` proves a pid EXISTS; it never proves the pid
is ours. The OS reissues the numbers of exited processes, and `watch-tasks-stream.sh`
appearing anywhere in a flattened argv is satisfied by a stranger carrying that
path as ORDINARY DATA (`python3 -c pass /scratch/watch-tasks-stream.sh` matched
a containment check and authorised a kill). Ownership is therefore three
questions, and all are answered here rather than once per caller:

  1. Does the sentinel carry a COMPLETE identity record naming this install,
     this instance and this incarnation? A missing field is a refusal — an
     absent `instance` must never read as "matches the empty default".
  2. Is the process wearing that pid actually EXECUTING our watcher? Only the
     executed slot of argv answers that (argv[0], or argv[1] after an
     interpreter); membership anywhere later is data passed TO the program.
     A substring test also counts the OBSERVER — a `ps | grep watch-tasks-stream`,
     or the wrapper running the health check — as another watcher; measured
     2026-07-21, it reported 3 watcher trees where 2 were real.
  3. Is the script the record names THIS checkout's watcher? A record and an
     argv that agree on `/foreign/checkout/src/watch-tasks-stream.sh` are
     self-consistent and still not ours: checkouts may share one workspace.

Every identity answer is tri-state. A `ps` that failed, timed out or answered
non-zero proves nothing; a caller that treats its empty answer as "not a
watcher" starts a duplicate watcher, or publishes a stranger's pid. So the
answer is True / False / None, and None is never a default — callers disagree
on what it should mean (over-counting costs delayed tasks, a wrong pid costs a
killed stranger), so it is carried to them undecided.

The decisions here are pure: process and file I/O belong to the caller
(restart.sh reaches the process table only through `src/process-ops.sh`, so a
test can replace that layer wholesale; the readers below take injectable
`run`/`argv_vector` seams), and this module decides. Refusals carry a reason
string because "I could not prove that watcher is mine" is a reportable
outcome, never a silent pass — the owner's rule is refuse-and-report.

Consumers: `src/watcher_identity.sh` — the one shell sequence restart.sh and
the startup reaper both run (through the CLI below, via the resolved python) —
`src/health-check.py` (imported) and the pool's per-worker bootstrap gate.

CLI, for the shell bridges:
  watcher_identity.py <pid>          -> `watcher`, `not-watcher`, `dead` or
                                        `unknown`, with `why=` beneath; exit 0
                                        when decided, 2 when not.
  watcher_identity.py owner-pid --sentinel P --instance I --workspace W
                                --incarnation-file F --code-path C
                                                        -> "<pid>\t<code_path>"
  watcher_identity.py runs-watcher --pid N --argv A [--argv-vector J] --code-path C
The last two exit 0 on confirmation, or print the reason and exit 1. J is the
JSON argv LIST src/process-ops.sh read for N (`pops_argv_vector`); when the seam
could not read one, only the flattened A is judged, and an operand after the
script then stays unprovable — the adapter never splits the text itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from util_paths import read_sentinel_record  # noqa: E402

WATCHER_STEM = "watch-tasks-stream"
WATCHER_SCRIPT_NAME = WATCHER_STEM + ".sh"
# A shell in the executed slot, and the name as a whole final path component, so
# `x-watch-tasks-stream.sh` and a mention inside a longer word cannot match.
WATCHER_SHELLS = ("sh", "bash", "zsh", "ksh")
WATCHER_SCRIPT = re.compile(r"(?:^|[\s/])watch-tasks-stream\.sh(?=\s|$)")
WATCHER_SCRIPT_RE = WATCHER_SCRIPT

# Every claim a signaller must find before it may target the pid on line 1.
# `version`/`started_at` are recorded but not required: neither narrows ownership.
REQUIRED_FIELDS = ("instance", "incarnation", "code_path", "workspace")


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


def classify_argv(argv: str, pid=None, argv_vector: Optional[Callable] = None,
                  vector: Optional[List[str]] = None) -> Verdict:
    """Decide from the EXECUTED script; the flattened `argv` only when no
    authoritative vector exists, and only where it cannot mislead. `vector` is
    the real argv list when the caller already read one; otherwise it is read
    for `pid` through `argv_vector` (the module's reader by default)."""
    vec = vector
    if vec is None and pid is not None:
        read = argv_vector if argv_vector is not None else proc_argv_vector
        vec = read(pid)
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


def is_watcher_argv(argv: str, pid=None, argv_vector: Optional[Callable] = None,
                    vector: Optional[List[str]] = None) -> Optional[bool]:
    """True/False from the EXECUTED script; None when nothing can prove it."""
    return classify_argv(argv, pid, argv_vector, vector).watcher


def executed_script(argv: str, vector: "list[str] | None" = None) -> "str | None":
    """The script the process is EXECUTING, or None when the slot is unreadable.
    An interpreter in argv[0] runs its next argument; a flag there means the
    executed slot cannot be determined from a flattened argv at all."""
    parts = list(vector) if vector else argv.split()
    if len(parts) < 2 or parts[0].rsplit("/", 1)[-1] not in WATCHER_SHELLS:
        return None
    if parts[1].startswith("-"):
        return None
    if vector is None and len(parts) > 2:
        return None
    return parts[1]


def runs_code_path(argv: str, code_path: str, vector: "list[str] | None" = None) -> bool:
    """Is the EXECUTED script `code_path`? Absolute → realpath equality; relative
    → a path-component suffix, since the documented start is `bash
    src/watch-tasks-stream.sh` and the cwd that resolved it is not ours to read."""
    executed = executed_script(argv, vector)
    if not executed or not code_path:
        return False
    if os.path.isabs(executed):
        return os.path.realpath(executed) == os.path.realpath(code_path)
    want = [p for p in os.path.normpath(executed).split(os.sep) if p not in ("", ".")]
    have = os.path.normpath(code_path).split(os.sep)
    return bool(want) and len(have) >= len(want) and have[-len(want):] == want


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


class Refused(Exception):
    """Ownership could not be confirmed. `str(exc)` is the reportable reason."""


def confirm_record(sentinel, want_instance: str, want_workspace: str,
                   incarnation_file, expect_code_path: str) -> "tuple[int, str]":
    """(pid, code_path) from a sentinel that names THIS install's watcher, or
    raise Refused with the check that failed. Reads the record through the one
    shared reader, so a consumer never re-spells the on-disk grammar.
    `expect_code_path` is the watcher script of the checkout asking."""
    rec = read_sentinel_record(sentinel)
    pid = rec.get("pid")
    if pid is None:
        raise Refused(f'line 1 of {sentinel} is not a pid (read "{rec.get("pid_line", "")}")')
    if not any(k in rec for k in REQUIRED_FIELDS):
        raise Refused(f"{sentinel} records a pid only — no instance, incarnation or "
                      f"code_path to check pid {pid} against")
    for field in REQUIRED_FIELDS:
        if field not in rec:
            raise Refused(f"{field}: {sentinel} records none — an absent claim is not a "
                          f"match, and pid {pid} stays unproven")
    # Unconditional: the default instance's key IS the empty string, so a
    # "compare only when non-empty" gate never checks the core's own sentinel.
    if rec["instance"] != want_instance:
        raise Refused(f'instance: {sentinel} says "{rec["instance"]}", this scope '
                      f'resolves "{want_instance}"')
    if not want_workspace:
        raise Refused(f"workspace: this scope resolved none, so {sentinel}'s claim "
                      f'"{rec["workspace"]}" cannot be checked')
    if rec["workspace"] != want_workspace:
        raise Refused(f'workspace: {sentinel} says "{rec["workspace"]}", this install '
                      f'is "{want_workspace}"')
    if not rec["code_path"]:
        raise Refused(f"code_path: {sentinel} records none, so pid {pid}'s argv cannot "
                      f"be matched to our checkout")
    if not expect_code_path:
        raise Refused(f"code_path: this checkout resolved no watcher script, so {sentinel}'s "
                      f'claim "{rec["code_path"]}" cannot be checked')
    if os.path.realpath(rec["code_path"]) != os.path.realpath(expect_code_path):
        raise Refused(f'code_path: {sentinel} records "{rec["code_path"]}", this checkout runs '
                      f'"{expect_code_path}" — another checkout\'s watcher is not ours to signal')
    if not rec["incarnation"]:
        raise Refused(f"incarnation: {sentinel} records an empty one, so nothing "
                      f"distinguishes this watcher from the one it replaced")
    if not os.path.isfile(incarnation_file):
        raise Refused(f'incarnation: {sentinel} claims "{rec["incarnation"]}" but the '
                      f"live process exposes no marker at {incarnation_file}")
    try:
        live = open(incarnation_file, errors="replace").readline().strip()
    except OSError as exc:
        raise Refused(f"incarnation: {incarnation_file} is unreadable ({exc})") from None
    if live != rec["incarnation"]:
        raise Refused(f'incarnation: {sentinel} claims "{rec["incarnation"]}", the live '
                      f'marker says "{live}"')
    return pid, rec["code_path"]


def parse_vector(pid: int, encoded: str) -> "list[str]":
    """The JSON argv list the seam printed for `pid`, or Refused: a vector the
    adapter could not hand over intact proves nothing, and is not split from text."""
    try:
        vector = json.loads(encoded)
    except ValueError as exc:
        raise Refused(f"argv: pid {pid}'s argv vector is unreadable ({exc}) — an "
                      f"unprovable identity is a refusal") from None
    if not isinstance(vector, list) or not all(isinstance(a, str) for a in vector):
        raise Refused(f"argv: pid {pid}'s argv vector is not a list of strings — an "
                      f"unprovable identity is a refusal")
    return vector


def confirm_process(pid: int, argv: str, code_path: str,
                    vector: "list[str] | None" = None) -> None:
    """Raise Refused unless the live process is EXECUTING our watcher script."""
    verdict = is_watcher_argv(argv, vector=vector)
    if verdict is False:
        raise Refused(f"argv: pid {pid} is not a live {WATCHER_STEM}")
    if verdict is None:
        raise Refused(f'argv: cannot prove pid {pid} executes {WATCHER_SCRIPT_NAME} '
                      f'from "{argv[:80]}" — an unprovable identity is a refusal')
    if not runs_code_path(argv, code_path, vector):
        raise Refused(f"code_path: pid {pid} does not run {code_path}")


def _main_ownership(args: "list[str]") -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("owner-pid")
    o.add_argument("--sentinel", required=True)
    o.add_argument("--instance", default="")
    o.add_argument("--workspace", default="")
    o.add_argument("--incarnation-file", required=True)
    o.add_argument("--code-path", required=True)
    r = sub.add_parser("runs-watcher")
    r.add_argument("--pid", type=int, required=True)
    r.add_argument("--argv", default="")
    r.add_argument("--argv-vector", default=None, help="JSON list, the authoritative argv")
    r.add_argument("--code-path", required=True)
    ns = ap.parse_args(args)
    try:
        if ns.cmd == "owner-pid":
            pid, code_path = confirm_record(ns.sentinel, ns.instance, ns.workspace,
                                            ns.incarnation_file, ns.code_path)
            print(f"{pid}\t{code_path}")
        else:
            vector = None
            if ns.argv_vector is not None:
                vector = parse_vector(ns.pid, ns.argv_vector)
            confirm_process(ns.pid, ns.argv, ns.code_path, vector)
    except Refused as exc:
        print(str(exc))
        return 1
    return 0


def main(argv=None) -> int:
    """`watcher_identity.py <pid>` -> `watcher`, `not-watcher`, `dead` or
    `unknown` on stdout, with `why=` beneath. Exit 0 when decided, 2 when not,
    so a shell adapter cannot read an unobservable `ps` as a proven answer.
    `owner-pid` / `runs-watcher` are the ownership subcommands (see the module doc)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("owner-pid", "runs-watcher"):
        return _main_ownership(args)
    if len(args) != 1:
        print("usage: watcher_identity.py <pid> | owner-pid ... | runs-watcher ...",
              file=sys.stderr)
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
    sys.exit(main())
