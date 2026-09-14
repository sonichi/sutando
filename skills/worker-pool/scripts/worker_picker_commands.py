#!/usr/bin/env python3
"""The worker picker's buttons arrive as ordinary tasks; this reads their intent.

The broker turns each button into a normal task addressed to this instance —
`source: worker-picker`, an id prefixed `worker-add-` or `worker-pin-`, and a
sentence of English. So the intent has to be recovered, and two rules keep that
honest:

  * the ROOM comes from the `channel_id` header, never from the sentence —
    a room-scoped button with no stamped room is REFUSED, not guessed;
  * `source` must be the header the gateway stamped — prose that merely says
    "worker picker" grants nothing, or anyone who can send a message could
    create workers.

Both rules rest on one mechanism: the parser is chosen by WRITER, not by any
value in the file. A task-last file is read STRICTLY — parsing stops at `task:`,
so a body cannot supply a header. The remote-gateway bridge is the one writer
that puts fields below `task:`; it newline-strips every value, which is what
makes its last-wins scan safe, and it stamps `receiving_instance` above `task:`,
which is how it is RECOGNISED rather than inferred from a header it also writes.
Sniffing the writer from a value is the forgery these rules exist to prevent:
a body line `access_tier: owner` under a last-wins scan is an authorization
bypass, not a typo. The producer side of the bargain: a task-last writer must
put `source` and `channel_id` ABOVE `task:`, or this reader refuses.

It returns intent. Acting on one is the caller's, so a misparse cannot spawn.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import sys
from pathlib import Path

# Sibling skill scripts resolve from this directory; core helpers (the task
# protocol) from the repo root (parents[3] of skills/<name>/scripts/<file>.py).
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_SCRIPTS), str(_SCRIPTS.parents[2] / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import local_task_protocol as ltp  # noqa: E402

import pool_advertise as pa  # noqa: E402
import pool_roster as pr  # noqa: E402

SOURCE = "worker-picker"

# A header the broker may stamp instead of relying on the sentence. In
# KNOWN_HEADER_KEYS, so the strict parser reads it ABOVE `task:` only.
COMMAND_HEADER = "picker_command"
COMMAND_ARGS_HEADER = "picker_args"
COMMANDS = ("add", "pin", "unpin")

# The complete admitted sentences, anchored end to end: a suffix that changes
# the meaning ("... — do not") must not parse as the original intent.
_ADD = re.compile(
    r"^Add a new worker to the pool \(worker picker '\+' button\)"
    r"(?:: grow the installed core pool by one via scripts/install-core-pool\.sh, "
    r"then confirm the new worker's id back to the owner\.)?"
    r"(?: Preferred label for the new worker: (?P<label>[^.]+?)\.?)?$", re.I)
_UNPIN = re.compile(r"^Unpin room (\S+) \(worker picker: back to auto routing\)$", re.I)
_DEDICATE = re.compile(r"^Dedicate room (\S+) to (\S+) — exclusive worker \(worker picker\)$", re.I)
_PIN_SET = re.compile(
    r"^Pin room (\S+) to workers (\S+(?: \S+)*) — bound set, pool-restriction routing "
    r"\(worker picker\)$", re.I)
_PIN_ONE = re.compile(r"^Pin room (\S+) to (\S+) \(worker picker\)$", re.I)


def _names(blob: str) -> list:
    return [w for w in blob.split() if w]


def _refuse_no_room(action: str) -> None:
    """Say why a room-scoped button was dropped: silence here reads as 'the
    sentence did not match', which is a different and much less alarming fault."""
    print(f"worker-picker: refusing {action} — no channel_id header",
          file=sys.stderr)


def _bad(name: str, reason: str) -> dict:
    """Refuse a stamped command and say which rule it broke — an unnamed
    refusal is indistinguishable from 'the sentence did not match'."""
    print(f"worker-picker: refusing {name!r} — {reason}", file=sys.stderr)
    return {"action": "malformed", "command": name, "reason": reason}


def _worker_names(args: dict) -> "list | None":
    """The pin target as a list of names, or None if it is not one.

    A bare string is REJECTED, not accepted as a one-element list: iterating
    one yields its characters, which is how `"w1"` became six workers.
    """
    if "workers" in args:
        got = args["workers"]
        if not isinstance(got, list):
            return None
    elif "worker" in args:
        got = [args["worker"]]
    else:
        return None
    if not got or not all(isinstance(w, str) and w.strip() for w in got):
        return None
    return [w.strip() for w in got]


def _structured(headers: dict, room: str) -> "dict | None":
    """The intent from a stamped command, or None when none was stamped.

    Only an ABSENT `picker_command` returns None and lets the sentence decide.
    Present-but-unusable — empty, an unknown verb, args that are not an object
    or do not fit the verb's schema — is an explicit refusal: a broker that
    stamped something we cannot honour must not be answered by guessing from a
    sentence written for a different command.
    """
    if COMMAND_HEADER not in headers:
        return None
    name = (headers.get(COMMAND_HEADER) or "").strip()
    if not name:
        return _bad(name, "empty picker_command")
    if name not in COMMANDS:
        return {"action": "unsupported", "command": name}

    args: dict = {}
    raw = (headers.get(COMMAND_ARGS_HEADER) or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return _bad(name, "picker_args is not JSON")
        if not isinstance(parsed, dict):
            return _bad(name, "picker_args is not a JSON object")
        args = parsed

    if name == "add":
        label = args.get("label")
        if label is not None and not isinstance(label, str):
            return _bad(name, "label must be a string")
        return {"action": "add", "label": label.strip() if label and label.strip() else None}

    # Every remaining command is room-scoped, and the room is the header's
    # alone — `picker_args` may name one, and it is never read.
    if not room:
        return _bad(name, "no channel_id header")
    if name == "unpin":
        return {"action": "unpin", "room": room}
    workers = _worker_names(args)
    if workers is None:
        return _bad(name, "workers must be a non-empty list of names")
    dedicated = args.get("dedicated", False)
    if not isinstance(dedicated, bool):
        return _bad(name, "dedicated must be a boolean")
    return {"action": "pin", "room": room,
            "workers": workers, "dedicated": dedicated}


# Exactly the fields the gateway writer emits below task: (its _TASK_FIELDS
# after "task" plus the tier lines); any other `key:` line is a second sentence.
_WRITER_BELOW_TASK = frozenset((
    "room_name", "sender_name", "reply_to_event", "reply_to_me", "reply_to_sender",
    "addressed_to", "thread_root", "source_room_id", "room_members", "room_member_count",
    "source_message_id", "user_id", "interaction_type", "platform_card", "collaborator",
    "sensitive_data_filter", "access_tier", "session_scope", "requested_worker", "priority",
    "hitl_click",
))
_BELOW_TASK_FIELD = re.compile(r"^([a-z_]+): ")


def _trailing_is_writer_context(rest: list) -> bool:
    """True when every line after the sentence is one the gateway writer appends
    itself (a field written below task:, or the system-instructions block)."""
    for ln in rest:
        ln = ln.strip()
        if ln.startswith("==="):
            # The writer's instruction blocks (system, skill) are appended last
            # and run to the end of the body.
            return True
        m = _BELOW_TASK_FIELD.match(ln)
        if m and m.group(1) in _WRITER_BELOW_TASK:
            continue
        return False
    return True


def parse(headers: dict, body: str) -> "dict | None":
    """The intent behind one task, or None when it is not the picker's.

    `headers` must come from the strict parser (see module docstring): every
    key read below is an authorization field, and a lenient scan would let the
    body supply one. A stamped command wins over the sentence; the sentence is
    the fallback for a broker that stamps none. An unrecognised sentence
    returns None rather than a guess, and so does a room-scoped sentence with
    no stamped room: a wrong intent creates or re-routes a worker.
    """
    # The writer either passes the broker's source through or, on the lane-
    # authority lineage, keeps source: as the lane and moves it to wire_source:.
    if SOURCE not in ((headers.get("source") or "").strip(), (headers.get("wire_source") or "").strip()):
        return None
    # The sentence is the body's first line. Only the writer's own trailing
    # lines may follow it; any other line is a second sentence and refuses.
    room = (headers.get("channel_id") or "").strip()

    structured = _structured(headers, room)
    if structured is not None:
        return structured

    lines = [ln for ln in (body or "").splitlines() if ln.strip()]
    if not lines or not _trailing_is_writer_context(lines[1:]):
        return None
    text = " ".join(lines[0].split())

    m = _ADD.fullmatch(text)
    if m:
        label = m.group("label")
        return {"action": "add", "label": label.strip() if label else None}

    if _UNPIN.fullmatch(text):
        if not room:
            _refuse_no_room("unpin")
            return None
        return {"action": "unpin", "room": room}
    m = _DEDICATE.fullmatch(text)
    if m:
        if not room:
            _refuse_no_room("dedicate")
            return None
        return {"action": "pin", "room": room,
                "workers": _names(m.group(2)), "dedicated": True}
    m = _PIN_SET.fullmatch(text)
    if m:
        if not room:
            _refuse_no_room("pin")
            return None
        return {"action": "pin", "room": room,
                "workers": _names(m.group(2)), "dedicated": False}
    m = _PIN_ONE.fullmatch(text)
    if m:
        if not room:
            _refuse_no_room("pin")
            return None
        return {"action": "pin", "room": room,
                "workers": [m.group(2)], "dedicated": False}
    return None


def parse_task_file(path) -> "dict | None":
    """Read one task file. The STRICT parser is the whole trust boundary here:
    it stops at `task:`, so no line of body text reaches `parse` as a header."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    parsed = ltp.parse_task_headers(text)
    return parse(parsed.headers, parsed.body or "")


def authorized_command(path) -> "dict | None":
    """The picker command in one task file, or None unless the OWNER sent it.

    Two writer shapes exist, and which one this is decides the parser. A
    task-last writer puts every header above `task:`; everything below is the
    sender's text and may promote nothing. The remote-gateway bridge writes the
    picker mark and the tier below `task:` and newline-strips every value, which
    is what makes its last-wins parse safe -- and it stamps `receiving_instance`
    above `task:`, which is how that writer is recognised rather than guessed.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    strict = ltp.parse_task_headers(text)
    above = strict.headers
    if above.get("source") is not None and (above.get("source") or "").strip() != SOURCE:
        return None
    # Chosen by WRITER, never by a value: the gateway stamps `receiving_instance`
    # above `task:`, the one slot a task body cannot reach.
    parsed = ltp.parse_task_headers_trusted(text) if above.get("receiving_instance") else strict
    if (parsed.headers.get("access_tier") or "").strip() != "owner":
        return None
    sentence = (parsed.body or "").split("\n", 1)[0]
    return parse(parsed.headers, sentence)


def applied_path(workspace) -> Path:
    return pr.roster_path(workspace).parent / "picker-applied.json"


@contextlib.contextmanager
def _applied_locked(workspace):
    # Its OWN lock file: taking pool_roster's here and then calling bind_room,
    # which takes the same one, deadlocks this process against itself.
    p = pr.roster_path(workspace).parent / ".picker-applied.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_applied(workspace) -> dict:
    try:
        got = json.loads(applied_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _results_dir(workspace, results_dir=None) -> Path:
    return Path(results_dir) if results_dir else pr.roster_path(workspace).parent.parent / "results"


def replay_reason(workspace, cmd: dict, task_id, results_dir=None) -> "str | None":
    """Why this picker command must NOT be applied, or None to go ahead.

    The startup sweep re-probes every RETAINED task, so a command whose task
    outlived its result would re-bind a room the owner has since unpinned.
    """
    if not task_id:
        # No id means this call cannot be replay-gated at all. An optional gate
        # is not a gate: refuse rather than silently apply ungated.
        return "no task id, so this command cannot be replay-gated"
    log = _read_applied(workspace)
    rec = (log.get("applied") or {}).get(str(task_id))
    if rec:
        return f"already applied as seq {rec.get('seq')}"
    rd = _results_dir(workspace, results_dir)
    # Live AND archive: a finished command routinely survives as a live task
    # with no live result, so a live-only check reads it as never having run.
    if (rd / f"{task_id}.txt").is_file():
        return "a delivered result already exists for this task"
    if ltp.find_archived_result(rd, str(task_id)) is not None:
        return "an archived result already exists for this task"
    room = (cmd or {}).get("room")
    room_rec = (log.get("rooms") or {}).get(room) if room else None
    if room_rec and int(room_rec.get("seq") or 0) > int(log.get("seq") or 0):
        return f"a newer command (seq {room_rec.get('seq')}) already holds {room}"
    return None


def _record_applied(workspace, cmd: dict, task_id) -> int:
    """Record BEFORE mutating. A crash between the two must LOSE a command —
    which the owner can see and re-issue — never resurrect an older one."""
    with _applied_locked(workspace):
        log = _read_applied(workspace)
        seq = int(log.get("seq") or 0) + 1
        log["seq"] = seq
        log.setdefault("applied", {})[str(task_id)] = {
            "room": cmd.get("room"), "action": cmd.get("action"), "seq": seq}
        if cmd.get("room"):
            log.setdefault("rooms", {})[cmd["room"]] = {
                "seq": seq, "task_id": str(task_id), "action": cmd.get("action")}
        pr._write_atomic(applied_path(workspace), log)
    return seq


def apply(workspace, cmd: dict, *, task_id=None, results_dir=None) -> "dict | None":
    """Apply a parsed pin or unpin AND publish it: binding, roster and the
    advertisement in one call, so the new binding is on the wire without
    waiting for another task. `add` is create_worker's and returns None.

    `task_id` is required for a pin or unpin: without it the call cannot be
    replay-gated, and an ungated door is how this defect returns.
    """
    action = (cmd or {}).get("action")
    if action in ("pin", "unpin"):
        reason = replay_reason(workspace, cmd, task_id, results_dir)
        if reason:
            return {"action": "skipped", "room": cmd.get("room"), "reason": reason}
        _record_applied(workspace, cmd, task_id)
    if action == "pin":
        workers = list(cmd.get("workers") or [])
        if len(workers) != 1:
            raise pr.RosterError(f"pin names {len(workers)} workers; a room takes one")
        roster = pr.bind_room(workspace, cmd["room"], workers[0])
    elif action == "unpin":
        roster = pr.unbind_room(workspace, cmd["room"])
    else:
        return None
    path = pa.write_advertisement(workspace)
    return {"action": action, "room": cmd["room"], "roster_version": roster.get("version"),
            "advertisement": str(path)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="read a worker-picker task's intent")
    ap.add_argument("--task-file", required=True)
    a = ap.parse_args(argv)
    intent = parse_task_file(a.task_file)
    if intent is None:
        print("worker-picker: not a picker command", file=sys.stderr)
        return 3
    print(json.dumps(intent, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
