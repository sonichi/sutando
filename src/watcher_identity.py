#!/usr/bin/env python3
"""Watcher ownership — the ONE policy every signaller and reporter asks.

WHY THIS MODULE EXISTS. `kill -0` proves a pid EXISTS; it never proves the pid
is ours. The OS reissues the numbers of exited processes, and `watch-tasks-stream.sh`
appearing anywhere in a flattened argv is satisfied by a stranger carrying that
path as ORDINARY DATA (`python3 -c pass /scratch/watch-tasks-stream.sh` matched
a containment check and authorised a kill). Ownership is therefore two questions,
and both are answered here rather than once per caller:

  1. Does the sentinel carry a COMPLETE identity record naming this install,
     this instance and this incarnation? A missing field is a refusal — an
     absent `instance` must never read as "matches the empty default".
  2. Is the process wearing that pid actually EXECUTING our watcher? Only the
     executed slot of argv answers that (argv[0], or argv[1] after an
     interpreter); membership anywhere later is data passed TO the program.
     A substring test also counts the OBSERVER — a `ps | grep watch-tasks-stream`,
     or the wrapper running the health check — as another watcher; measured
     2026-07-21, it reported 3 watcher trees where 2 were real.

Everything here is pure: process and file I/O belong to the caller (restart.sh
reaches the process table only through `src/process-ops.sh`, so a test can
replace that layer wholesale), and this module decides. Refusals carry a reason
string because "I could not prove that watcher is mine" is a reportable
outcome, never a silent pass — the owner's rule is refuse-and-report.

Consumers: `src/restart.sh` (through the CLI below, via its resolved python),
and `src/health-check.py` (imported).

CLI, for the shell bridge:
  watcher_identity.py owner-pid --sentinel P --instance I --workspace W
                                --incarnation-file F   -> "<pid>\t<code_path>"
  watcher_identity.py runs-watcher --pid N --argv A --code-path C
Both exit 0 on confirmation, or print the reason and exit 1.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from util_paths import read_sentinel_record  # noqa: E402

WATCHER_STEM = "watch-tasks-stream"
WATCHER_SCRIPT_NAME = WATCHER_STEM + ".sh"
# A shell in the executed slot, and the name as a whole final path component, so
# `x-watch-tasks-stream.sh` and a mention inside a longer word cannot match.
WATCHER_SHELLS = ("sh", "bash", "zsh", "ksh")
WATCHER_SCRIPT_RE = re.compile(r"(?:^|[\s/])" + re.escape(WATCHER_SCRIPT_NAME) + r"(?=\s|$)")

# Every claim a signaller must find before it may target the pid on line 1.
# `version`/`started_at` are recorded but not required: neither narrows ownership.
REQUIRED_FIELDS = ("instance", "incarnation", "code_path", "workspace")


def is_watcher_argv(argv: str, vector: "list[str] | None" = None) -> "bool | None":
    """True/False from the EXECUTED script; None when nothing can prove it.

    Callers disagree on what None should mean, which is why this is tri-state:
    over-counting a watcher costs delayed tasks, publishing (or signalling) a
    wrong pid costs a killed stranger. `vector` is the real argv list when the
    caller could read one — a flattened string cannot separate an operand
    containing a space from two operands.
    """
    if vector is not None and len(vector) >= 2:
        if vector[0].rsplit("/", 1)[-1] not in WATCHER_SHELLS:
            return False
        if vector[1].startswith("-"):
            return False
        return os.path.basename(vector[1]) == WATCHER_SCRIPT_NAME
    parts = argv.split()
    if len(parts) < 2:
        return False
    if parts[0].rsplit("/", 1)[-1] not in WATCHER_SHELLS:
        return False
    if parts[1].startswith("-"):
        return False
    # Authoritative only when argv ends at parts[1]: with more tokens the real
    # pathname may continue past a space and end in a different name.
    if WATCHER_SCRIPT_RE.search(parts[1]) is not None:
        return True if len(parts) == 2 else None
    if len(parts) == 2:
        return False
    # Only a later token matches, and a spaced script path is the same string as
    # a script plus arguments -- nothing here can decide between them.
    return None if WATCHER_SCRIPT_RE.search(argv) else False


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


class Refused(Exception):
    """Ownership could not be confirmed. `str(exc)` is the reportable reason."""


def confirm_record(sentinel, want_instance: str, want_workspace: str,
                   incarnation_file) -> "tuple[int, str]":
    """(pid, code_path) from a sentinel that names THIS install's watcher, or
    raise Refused with the check that failed. Reads the record through the one
    shared reader, so a consumer never re-spells the on-disk grammar."""
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


def confirm_process(pid: int, argv: str, code_path: str,
                    vector: "list[str] | None" = None) -> None:
    """Raise Refused unless the live process is EXECUTING our watcher script."""
    verdict = is_watcher_argv(argv, vector)
    if verdict is False:
        raise Refused(f"argv: pid {pid} is not a live {WATCHER_STEM}")
    if verdict is None:
        raise Refused(f'argv: cannot prove pid {pid} executes {WATCHER_SCRIPT_NAME} '
                      f'from "{argv[:80]}" — an unprovable identity is a refusal')
    if not runs_code_path(argv, code_path, vector):
        raise Refused(f"code_path: pid {pid} does not run {code_path}")


def main(argv_in: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("owner-pid")
    o.add_argument("--sentinel", required=True)
    o.add_argument("--instance", default="")
    o.add_argument("--workspace", default="")
    o.add_argument("--incarnation-file", required=True)
    r = sub.add_parser("runs-watcher")
    r.add_argument("--pid", type=int, required=True)
    r.add_argument("--argv", default="")
    r.add_argument("--code-path", required=True)
    args = ap.parse_args(argv_in)
    try:
        if args.cmd == "owner-pid":
            pid, code_path = confirm_record(args.sentinel, args.instance,
                                            args.workspace, args.incarnation_file)
            print(f"{pid}\t{code_path}")
        else:
            confirm_process(args.pid, args.argv, args.code_path)
    except Refused as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
