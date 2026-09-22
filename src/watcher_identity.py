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
import shutil
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

    `ready`: a matching watcher counts only once a sentinel under `state_dir`
    names its pid, which the watcher stamps after a real event round-trip; a
    process that exists but has not stamped is decidably "no", never "unknown".
    The caller names `state_dir`: this module resolves no workspace.
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
        if ready and not sentinel_names_pid(as_pid(pid), state_dir):
            continue
        return True
    return None if saw_undecidable else False


class InboxHolders(NamedTuple):
    """`observed` False: the process table itself could not be read, so `holders`
    says nothing. `undecided` counts watcher-shaped lines that could be serving
    this inbox but cannot be proven either way — a process that exits between the
    snapshot and the argv read is the ordinary case, so this is never empty for
    long on a busy host and must not be read as "a holder exists"."""
    observed: bool
    holders: List[tuple]
    undecided: int


def inbox_holders(inbox: str, exclude_pid=None, ps_output: Optional[str] = None,
                  argv_vector: Optional[Callable] = None,
                  run: Callable = subprocess.run) -> InboxHolders:
    """Every watcher-shaped process PROVEN to serve `inbox`, tagged or not, ready
    or not: `[(pid, role), ...]` with role `session`, `standby` or `untagged`.
    Presence only: readiness is the supervisor's question.

    Undecidable lines are counted, never merged into the answer: a caller that
    starts a watcher must distinguish "something holds this inbox" from "something
    could not be read", because refusing to start leaves the inbox with no
    announcer at all, which is worse than the duplicate the check prevents."""
    if ps_output is None:
        try:
            result = run(["ps", "-Ao", "pid,ppid,args"],
                         capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001
            return InboxHolders(False, [], 0)
        if getattr(result, "returncode", None) != 0:
            return InboxHolders(False, [], 0)
        ps_output = result.stdout
    want = canonical_inbox(inbox)
    skip = {str(os.getpid()), str(exclude_pid) if exclude_pid else ""}
    holders: List[tuple] = []
    undecided = 0
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        # A forked child of the excluded caller (its own command substitution)
        # carries the caller's argv and would read as a second watcher.
        if len(parts) < 3 or parts[0] in skip or parts[1] in skip:
            continue
        pid, argv = parts[0], parts[2]
        verdict = classify_argv(argv, as_pid(pid), argv_vector)
        if verdict.watcher is False:
            continue
        if verdict.watcher is None:
            flat = flat_inbox(argv)
            if flat is None or canonical_inbox(flat) == want:
                undecided += 1
            continue
        tagged = watcher_inbox(verdict.operands)
        theirs = canonical_inbox(tagged if tagged else positional_inbox(verdict.operands))
        if theirs is None:
            undecided += 1
            continue
        if theirs != want:
            continue
        role = watcher_role(verdict.operands)
        holders.append((int(pid), role if role in ("session", "standby") else "untagged"))
    return InboxHolders(True, holders, undecided)


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


class OutputSink(NamedTuple):
    """Where a watcher's announcements go, and whether anything reads them.

    `read` is None when the question is not worth asking rather than when it
    failed: see `output_sink`. `observed` False means `lsof` could not be
    consulted, so nothing here is evidence."""
    observed: bool
    kind: str
    target: str
    read: Optional[bool]


def _lsof(args: List[str], run: Callable) -> Optional[str]:
    """`lsof` output, or None when it could not be consulted. Exit 1 is lsof's
    "nothing matched", which is an answer; only a missing or broken lsof is not."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        r = run([lsof, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in (0, 1):
        return None
    return r.stdout or ""


def _fields(text: str):
    """lsof -F records as dicts, one per open file, carrying the owning pid."""
    pid, cur = None, {}
    for line in text.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            if cur:
                yield cur
            pid, cur = val, {}
        elif tag == "f":
            if cur:
                yield cur
            cur = {"p": pid}
        else:
            cur[tag] = val
    if cur:
        yield cur


def output_sink(pid, run: Callable = subprocess.run) -> OutputSink:
    """What pid's stdout is, and whether another process is reading it.

    This is how a stray watcher is told from a working one: a watcher holds its
    inbox whether or not anything consumes what it announces, and the only
    difference visible from outside is the reader.

    Decided for a regular file (are there other openers with read access) and
    for /dev/null (nothing can read it). Left at None for a pipe, fifo or
    socket, which is not evasion: the watcher exits on its first failed write,
    so those clear themselves at the next announcement. A tty means an operator
    is attached, which counts as read."""
    text = _lsof(["-p", str(pid), "-a", "-d", "1", "-F", "ftn"], run)
    if text is None:
        return OutputSink(False, "unknown", "", None)
    rec = next((r for r in _fields(text) if "t" in r), None)
    if rec is None:
        return OutputSink(True, "unknown", "", None)
    kind_raw, target = rec.get("t", ""), rec.get("n", "")
    if kind_raw == "REG":
        readers = _lsof(["-F", "pan", "--", target], run)
        if readers is None:
            return OutputSink(False, "file", target, None)
        me = str(pid)
        found = any(r.get("p") != me and "r" in (r.get("a") or "")
                    for r in _fields(readers) if r.get("n") == target)
        return OutputSink(True, "file", target, found)
    if kind_raw == "CHR":
        if target.endswith("/null"):
            return OutputSink(True, "discarded", target, False)
        return OutputSink(True, "tty", target, True)
    if kind_raw in ("FIFO", "PIPE", "unix", "IPv4", "IPv6", "sock"):
        return OutputSink(True, "stream", target, None)
    return OutputSink(True, kind_raw or "unknown", target, None)


def main(argv=None) -> int:
    """`watcher_identity.py <pid>` -> `watcher`, `not-watcher`, `dead` or
    `unknown` on stdout, with `why=` beneath. Exit 0 when decided, 2 when not,
    so a shell adapter cannot read an unobservable `ps` as a proven answer.

    `watcher_identity.py role-present <role> [--inbox VALUE] [--ready STATE_DIR]`
    -> `yes`, `no` or `unknown` on stdout; `--ready` counts a session watcher
    only once a sentinel under STATE_DIR names it. `standby-present --inbox VALUE` asks the same of a
    watcher that is NOT session-role for that inbox. Exit 0 for yes/no (both decided), 2 only when the
    `ps` snapshot itself failed -- `unknown` must never read as `no` to a
    caller deciding whether to start a duplicate watcher."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "output-sink":
        if len(args) != 2 or as_pid(args[1]) is None:
            print("usage: watcher_identity.py output-sink <pid>", file=sys.stderr)
            return 64
        sink = output_sink(args[1])
        print(f"{sink.kind} {sink.target}".strip())
        print("read=" + ("unknown" if sink.read is None else ("yes" if sink.read else "no")))
        if not sink.observed:
            print("why=lsof could not be consulted", file=sys.stderr)
            return 2
        return 0
    if args and args[0] == "inbox-holders":
        rest = args[1:]
        inbox = None
        exclude = None
        i = 0
        while i < len(rest):
            if rest[i] == "--inbox" and i + 1 < len(rest):
                inbox = rest[i + 1]
                i += 2
            elif rest[i].startswith("--inbox="):
                inbox = rest[i].split("=", 1)[1] or None
                i += 1
            elif rest[i] == "--exclude" and i + 1 < len(rest):
                exclude = as_pid(rest[i + 1])
                i += 2
            else:
                print("usage: watcher_identity.py inbox-holders --inbox VALUE [--exclude PID]", file=sys.stderr)
                return 64
        if not inbox:
            print("usage: watcher_identity.py inbox-holders --inbox VALUE [--exclude PID]", file=sys.stderr)
            return 64
        seen = inbox_holders(inbox, exclude_pid=exclude)
        if not seen.observed:
            print("unobserved")
            print("why=ps snapshot unavailable", file=sys.stderr)
            return 2
        for pid, role in seen.holders:
            print(f"{pid} {role}")
        if not seen.holders:
            print("none")
        if seen.undecided:
            print(f"undecided={seen.undecided}", file=sys.stderr)
        return 0
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
            print("usage: watcher_identity.py role-present <role> [--inbox VALUE] [--ready STATE_DIR]", file=sys.stderr)
            return 64
        role = rest[0]
        inbox = None
        state_dir = None
        i = 1
        while i < len(rest):
            if rest[i] == "--inbox" and i + 1 < len(rest):
                inbox = rest[i + 1]
                i += 2
            elif rest[i].startswith("--inbox="):
                inbox = rest[i].split("=", 1)[1] or None
                i += 1
            elif rest[i] == "--ready" and i + 1 < len(rest):
                state_dir = rest[i + 1]
                i += 2
            elif rest[i].startswith("--ready="):
                state_dir = rest[i].split("=", 1)[1] or None
                i += 1
            else:
                print("usage: watcher_identity.py role-present <role> [--inbox VALUE] [--ready STATE_DIR]", file=sys.stderr)
                return 64
        verdict = role_present(role, inbox, ready=state_dir is not None, state_dir=state_dir)
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
