#!/usr/bin/env python3
"""The turn ledger — a record that the agent's turn produced an outbound message.

THE GAP THIS FILLS. `src/check-pending-tasks.sh` is the Stop hook, and it can
only see turns that ANSWER A QUEUED TASK: it walks `<workspace>/tasks/` and
requires a ready `results/<same name>`. A turn with no task in the queue — a
proactive turn, or one that replies by calling `skills/agent-room-ops/room_ops.py
say` — is invisible to it. `say` posts to the room over HTTP and writes nothing
to disk, so no surface anywhere records that the agent spoke. The rule the owner
asked for ("the end of a turn must be a msg or no-send") was therefore
unenforceable: nothing knew whether a message had happened.

WHAT THIS RECORDS. An append-only JSONL log under `<workspace>/state/`, one
object per line: `{ts, kind, target}` for a send, `{ts, kind: "no-send",
reason}` for a deliberate decision not to speak. Writers append with O_APPEND
and a single `write()` so concurrent workers cannot interleave a line; nothing
here read-modify-writes the log.

WHAT COUNTS AS "THE TURN SPOKE". Two surfaces, because there are two ways a
reply leaves this agent, and only one of them is instrumented:

  • a ledger entry newer than the previous Stop — `room_ops say`, or an explicit
    `record_no_send`;
  • a READY file at the top level of `<workspace>/results/` newer than the
    previous Stop — the result-file protocol, delivered by whichever bridge owns
    the task. Readiness is `delivery.readiness`, the same policy the bridges
    apply, so this agrees with what will actually be sent rather than counting a
    half-written file as a reply.

The results surface is deliberately NOT recursive. A delivered result is
archived into `results/archive/YYYY-MM/` within seconds, so an archived result
belongs to an earlier turn boundary — and a turn that replied long ago and then
went silent is precisely the case the rule is meant to catch.

ARMING. `stop_gate` is inert on an install whose ledger file does not exist yet,
and this is load-bearing rather than incidental: without it every Stop on a
fresh workspace would block, including the three that the existing hook tests
pin as quiet. The gate arms at the first send or no-send ever recorded — which
`room_ops say` writes on the agent's first room message — and the cost is a
bootstrap window, on a host that has never once spoken, in which the check
cannot fire.

SESSION SCOPING. The reminder flag, the stop boundary, and the ledger's
no-send/send evidence were originally one workspace-wide record apiece, with no
session identity anywhere in the hooks that call this module. Two sessions in
one workspace could therefore reset or satisfy each other's gate: session B's
`turn-start` reset session A's already-spent reminder mid-retry (refusing A's
turn a second time, past the documented one-reminder ceiling), and A's
`no-send` silently passed B's own silent turn (PR #4028 review,
qingyun-wu, 2026-09-09). Every public function here now takes an optional
`session`, defaulting (via `_resolve_session`) to `$CLAUDE_CODE_SESSION_ID` —
the same established idiom as `scripts/skill-read-receipt.py`'s `_session_id()`.
Claude Code sets that variable on every subprocess it spawns, hooks AND
ordinary Bash-tool calls alike, so this reaches `room_ops.py say` and a manual
`turn_ledger.py no-send` from the agent too, not only the two hook wrappers —
neither of which needs to parse its own stdin payload as a result. When a
session is known, the reminder and boundary live in a per-session file
(`turn-reminder.<session>.json`, `turn-stop.<session>.json`) instead of the
shared name, and a ledger entry tagged with a *different* session no longer
counts as this session's own no-send or send. No session anywhere (the env var
unset — outside Claude Code entirely, e.g. a standalone script or most test
harnesses) falls back to exactly the original shared-file, unfiltered behavior
— this must never make an unscoped caller MORE strict than before the fix,
only session-aware callers gain isolation. An entry with no `session` key
(written before this change, or by an unscoped caller) still counts for
everyone, so the fix does not require a flag day.

NOT SCOPED: the result-file evidence surface (`_result_after`). A delivered
`results/` file carries no session identity anywhere in the result-file
protocol — it is written by whichever process handles the task and delivered
by a bridge independent of any session — so there is nothing to filter it by.
Scoping it would need a broader change to that protocol; out of scope here,
where the review's finding was specifically the reminder/boundary/ledger.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from delivery.readiness import read_ready_result  # noqa: E402
from workspace_default import resolve_workspace, status_read_path, write_status  # noqa: E402

__all__ = [
    "LEDGER_NAME", "STOP_NAME", "ledger_path", "record_send", "record_no_send",
    "last_action_after", "delivery_after", "last_stop_ts", "mark_stop", "stop_gate",
]

LEDGER_NAME = "turn-ledger.jsonl"
STOP_NAME = "turn-stop.json"
TURN_NAME = "turn-reminder.json"

# An entry is ~90 bytes and the only question ever asked of this file is "since
# the last Stop", so the cap is about unbounded growth, not retention depth.
MAX_BYTES = 256 * 1024
TRIM_TO_BYTES = 128 * 1024


def _workspace(workspace: Path | str | None) -> Path:
    """The caller's workspace, else the canonical one.

    Callers that already resolved it (the Stop hook has) pass it in: re-resolving
    would answer about a different directory than the one the hook is guarding.
    `migrate=False` keeps a path lookup free of the resolver's migration notices.
    """
    if workspace is not None:
        return Path(workspace)
    return resolve_workspace(migrate=False)


def ledger_path(workspace: Path | str | None = None) -> Path:
    """Absolute path of the ledger. `state/` per the workspace contract."""
    return _workspace(workspace) / "state" / LEDGER_NAME


def _scoped_name(base: str, session: str | None) -> str:
    """`base` unchanged when `session` is falsy; else a per-session sibling name.

    A caller with no session id (an unscoped writer, a hook that could not
    parse its stdin payload) must land on exactly the pre-fix shared name —
    isolation is only ever ADDED for a caller that supplies one, never removed
    from one that does not.
    """
    if not session:
        return base
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session))[:128]
    stem, dot, ext = base.partition(".")
    return f"{stem}.{safe}{dot}{ext}"


def _session_id() -> str:
    """This process's own Claude Code session id, or "" — never raises.

    Same idiom as `scripts/skill-read-receipt.py`'s `_session_id()`: Claude
    Code sets `CLAUDE_CODE_SESSION_ID` on every subprocess it spawns.
    """
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()


def _resolve_session(session: str | None) -> str | None:
    """`session` if the caller supplied one, else this process's own session id.

    Every public function below runs its incoming `session` through this, so a
    caller that says nothing still gets scoped for free under Claude Code, and
    a test or standalone script (no such env var) still gets exactly the
    original shared-file behavior — never a regression, only a missed
    isolation opportunity outside Claude Code.
    """
    if session:
        return str(session)
    return _session_id() or None


def _entry_matches_session(entry: dict, session: str | None) -> bool:
    """Whether `entry` counts as evidence for `session`.

    `session=None` (the caller could not determine one) never filters — the
    original, unscoped behavior. Given a session, an entry tagged with a
    DIFFERENT one is excluded; an untagged entry (written before this change,
    or by an unscoped caller) still counts, so old data and mixed-version
    callers do not need a flag day.
    """
    if not session:
        return True
    tag = entry.get("session")
    return tag is None or str(tag) == str(session)


@contextlib.contextmanager
def _writer_lock(path: Path):
    """One writer at a time across append and compaction, via a sidecar lock.

    The lock is its own file so compaction's `os.replace` never swaps the inode
    a holder is waiting on.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _append(entry: dict, workspace: Path | str | None = None) -> None:
    """Append one JSON line, then bound the file. Never raises.

    O_APPEND + one `write()` of a single line: two workers recording at once each
    land a whole line, and a reader never sees a partial one. Recording is
    bookkeeping around a message that already went out, so a failure here must
    never propagate into the caller's send path.
    """
    path = ledger_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        # Append and compaction share one lock: `_trim` reads a tail then swaps
        # the file, so an append landing in that window would be discarded.
        with _writer_lock(path):
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            _trim(path)
    except OSError:
        pass


def _trim(path: Path) -> None:
    """Drop the oldest entries once the file passes `MAX_BYTES`.

    Rewrites to `TRIM_TO_BYTES` rather than to the cap so the rewrite is
    amortized over many appends instead of firing on every one past it. The swap
    is temp-file + `os.replace`, so a concurrent reader sees the whole old file
    or the whole new one — never a truncated one.
    """
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        with open(path, "rb") as fh:
            fh.seek(-TRIM_TO_BYTES, os.SEEK_END)
            tail = fh.read()
        # The seek lands mid-line; drop that fragment so every kept line parses.
        cut = tail.find(b"\n")
        tail = tail[cut + 1:] if cut >= 0 else b""
        tmp = path.with_name(f".{path.name}.trim.tmp")
        with open(tmp, "wb") as fh:
            fh.write(tail)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass


def record_send(kind: str, target: str, workspace: Path | str | None = None,
                 session: str | None = None) -> None:
    """Record that a message went out — `kind` is the surface, `target` its address."""
    session = _resolve_session(session)
    entry = {"ts": time.time(), "kind": str(kind), "target": str(target)}
    if session:
        entry["session"] = session
    _append(entry, workspace)


def record_no_send(reason: str, workspace: Path | str | None = None,
                    session: str | None = None) -> None:
    """Record a deliberate decision that this turn ends without a message."""
    session = _resolve_session(session)
    entry = {"ts": time.time(), "kind": "no-send", "reason": str(reason)}
    if session:
        entry["session"] = session
    _append(entry, workspace)


def read_entries(workspace: Path | str | None = None) -> list[dict]:
    """Every parseable entry, oldest first. A damaged line is skipped, not fatal."""
    path = ledger_path(workspace)
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("ts"), (int, float)):
            out.append(entry)
    return out


def last_action_after(ts: float, workspace: Path | str | None = None,
                       session: str | None = None) -> dict | None:
    """The most recent ledger entry newer than `ts` that counts for `session`, or None."""
    session = _resolve_session(session)
    newer = [e for e in read_entries(workspace)
             if float(e["ts"]) > ts and _entry_matches_session(e, session)]
    return max(newer, key=lambda e: float(e["ts"])) if newer else None


def _result_dirs(results: Path, ts: float) -> list:
    """`results/` plus the archive partitions a turn starting at `ts` can reach.

    Bounded to the months spanned by the boundary so a large archive is never
    walked whole; a turn cannot predate its own boundary.
    """
    archive = results / "archive"
    # The archive holds month partitions AND a flat top level; a freshly
    # delivered result lands flat, so scanning only the partitions misses it.
    dirs = [results, archive]
    # Same calendar as the writer, by its own helper: computing months here in
    # UTC missed the local-month partition either side of a boundary.
    from task_archive import archive_month
    months = {archive_month(t) for t in (ts, time.time())}
    dirs.extend(archive / m for m in sorted(months))
    return dirs


def _result_after(ts: float, workspace: Path | str | None = None) -> dict | None:
    """A ready result file at the top of `results/` newer than `ts`, or None.

    The other way a reply leaves this agent. Readiness is `delivery.readiness` —
    an empty or half-written result delivers nothing and must not read as a
    message, which is the same distinction the Stop hook's task check makes.
    """
    results = _workspace(workspace) / "results"
    # The bridge archives a delivered result immediately, independently of the
    # turn ending, so the top level alone loses this turn's own evidence.
    entries = []
    for directory in _result_dirs(results, ts):
        try:
            entries.extend(os.scandir(directory))
        except OSError:
            continue
    if not entries:
        return None
    best = None
    for item in entries:
        try:
            if not item.is_file(follow_symlinks=False):
                continue
            mtime = item.stat().st_mtime
        except OSError:
            continue
        if mtime <= ts or (best and mtime <= best["ts"]):
            continue
        if read_ready_result(item.path) is None:
            continue
        best = {"ts": mtime, "kind": "result", "target": item.name}
    return best


def delivery_after(ts: float, workspace: Path | str | None = None,
                    session: str | None = None) -> dict | None:
    """The turn's newest outbound message since `ts`, over either surface.

    Only the ledger side is session-filtered; `_result_after` carries no
    session identity to filter by (see the module docstring's NOT SCOPED note).
    """
    session = _resolve_session(session)
    candidates = [c for c in (last_action_after(ts, workspace, session),
                              _result_after(ts, workspace)) if c]
    return max(candidates, key=lambda e: float(e["ts"])) if candidates else None


def last_stop_ts(workspace: Path | str | None = None, session: str | None = None) -> float | None:
    """When the previous turn was allowed to end, or None if none ever was.

    The boundary lives in its own `state/turn-stop.json` (or, given a session,
    its per-session sibling) rather than as a ledger entry for two reasons: it
    is a single mutable value against an append-only log, and `_trim` could
    otherwise evict the very boundary the gate measures from. `write_status`
    already makes that one-value write atomic.
    """
    session = _resolve_session(session)
    try:
        raw = json.loads(
            status_read_path(_scoped_name(STOP_NAME, session), _workspace(workspace)).read_text())
    except (OSError, ValueError):
        return None
    ts = raw.get("ts") if isinstance(raw, dict) else None
    return float(ts) if isinstance(ts, (int, float)) else None


def mark_stop(workspace: Path | str | None = None, session: str | None = None) -> float:
    """Record that a turn ended here; the next turn is measured from it."""
    session = _resolve_session(session)
    now = time.time()
    try:
        write_status(_scoped_name(STOP_NAME, session), {"ts": now}, _workspace(workspace))
    except OSError:
        pass
    return now


def begin_turn(workspace: Path | str | None = None, session: str | None = None) -> None:
    """A turn is starting: the reminder is unspent again.

    Reset at turn start rather than when a reminder is sent, so one refusal per
    turn is the ceiling and a turn can never be refused twice — a guarantee that
    only holds per-session now: session B's turn-start must not unspend
    session A's reminder, which is why this writes the per-session file when a
    session id is known.
    """
    session = _resolve_session(session)
    write_status(_scoped_name(TURN_NAME, session), {"reminded": False, "ts": time.time()},
                 _workspace(workspace))


def reminder_spent(workspace: Path | str | None = None, session: str | None = None) -> bool:
    session = _resolve_session(session)
    try:
        return bool(json.loads(
            status_read_path(_scoped_name(TURN_NAME, session), _workspace(workspace)).read_text())
                    .get("reminded"))
    except (OSError, ValueError):
        return False


def spend_reminder(workspace: Path | str | None = None, session: str | None = None) -> None:
    session = _resolve_session(session)
    write_status(_scoped_name(TURN_NAME, session), {"reminded": True, "ts": time.time()},
                 _workspace(workspace))


# Below this, the message was effectively the last thing the turn did.
ENDED_ON_A_MESSAGE_S = 20.0


def stop_gate(workspace: Path | str | None = None, session: str | None = None) -> str | None:
    """None when the turn may end; otherwise the reason it must not.

    Allowing a stop RECORDS it, so the decision and the next turn's starting
    boundary cannot disagree. A refusal deliberately leaves the boundary alone:
    the turn has not ended, and the message the agent is about to send must still
    count against the boundary it began from.

    `session`, when known (explicitly, or via `$CLAUDE_CODE_SESSION_ID`),
    scopes the boundary/reminder to a per-session file and the ledger checks to
    entries tagged for this session (or untagged) — see the module docstring's
    SESSION SCOPING note. No session anywhere reproduces the original
    shared-file, unfiltered behavior exactly.
    """
    session = _resolve_session(session)
    ws = _workspace(workspace)
    # An absent ledger means nothing was ever sent, which is what this gate
    # catches. Only a missing boundary below is genuinely unjudgeable.
    since = last_stop_ts(ws, session)
    if since is None:
        mark_stop(ws, session)
        return None
    # An explicit no-send is a decision ABOUT this turn, so its age cannot make it
    # stale; only a message is judged on whether the turn ended on it.
    if any(e.get("kind") == "no-send" and _entry_matches_session(e, session)
           for e in read_entries(ws) if float(e["ts"]) > since):
        mark_stop(ws, session)
        return None
    last = delivery_after(since, ws, session)
    if last is not None and (time.time() - float(last["ts"])) <= ENDED_ON_A_MESSAGE_S:
        mark_stop(ws, session)
        return None
    if reminder_spent(ws, session):
        # One nudge per turn. A turn that was already reminded ends regardless:
        # refusing twice is how a gate that is wrong becomes a loop.
        mark_stop(ws, session)
        return None
    spend_reminder(ws, session)
    return ("This turn is ending without a message and without an explicit "
            "no-send. Reply — post to the room (`room_ops.py say`) or write the "
            "result file the task expects — or, if silence is right, record it: "
            "`python3 src/turn_ledger.py no-send \"<why>\"`. "
            "This is the only reminder for this turn.")


def main(argv: list[str]) -> int:
    """`stop-gate` (exit 1 + reason on stdout when the turn must not end),
    `send KIND TARGET`, `no-send REASON`. `--workspace` pins the directory for a
    caller that already resolved it. `--session ID` overrides the session used
    to scope the reminder/boundary/ledger checks — mainly for tests simulating
    more than one session; ordinary callers need not pass it, since every
    function here already defaults to `$CLAUDE_CODE_SESSION_ID`."""
    args = list(argv)
    ws = None
    session = None
    if "--workspace" in args:
        i = args.index("--workspace")
        ws = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    if "--session" in args:
        i = args.index("--session")
        session = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    cmd = args[0] if args else ""
    if cmd == "turn-start":
        begin_turn(ws, session)
        return 0
    if cmd == "stop-gate":
        reason = stop_gate(ws, session)
        if reason:
            print(reason)
            return 1
        return 0
    if cmd == "send" and len(args) >= 3:
        record_send(args[1], args[2], ws, session)
        return 0
    if cmd == "no-send" and len(args) >= 2:
        record_no_send(" ".join(args[1:]), ws, session)
        return 0
    print(f"usage: {Path(__file__).name} [--workspace DIR] [--session ID] "
          "turn-start | stop-gate | send KIND TARGET | no-send REASON",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
