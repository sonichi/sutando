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
makes its last-wins scan safe, and a verified envelope HMAC is what admits it,
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
from delivery.readiness import read_ready_result  # noqa: E402

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
    "hitl_click", "channel_kind",
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


def sender_command(path, workspace=None) -> "tuple[str, dict, dict] | None":
    """(sender, command, headers) for one picker task, or None when it is not one.

    `sender` is owner, collaborator, other (an explicit non-owner tier) or
    unknown (no tier). A task-last writer puts every header above `task:`;
    everything below is the sender's text and may promote nothing. The gateway
    instead writes the picker mark, the tier and `collaborator` BELOW `task:`,
    so reading them needs the last-wins parse -- which is only safe on a file
    whose writer declared the one-line task-mid layout above `task:` AND whose
    envelope HMAC verifies; either missing falls back to the strict parse
    (task_envelope.attested_task_headers).
    """
    import task_envelope as te
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    above = ltp.parse_task_headers(text).headers
    if above.get("source") is not None and (above.get("source") or "").strip() != SOURCE:
        return None
    parsed = te.attested_task_headers(text, workspace)
    headers = parsed.headers
    tier = (headers.get("access_tier") or "").strip()
    sentence = (parsed.body or "").split("\n", 1)[0]
    cmd = parse(headers, sentence)
    if cmd is None:
        return None
    if tier == "owner":
        sender = "owner"
    elif tier == "team" and (headers.get("collaborator") or "").strip() == "true":
        sender = "collaborator"
    else:
        sender = "other" if tier else "unknown"
    return sender, cmd, dict(headers)


def authorized_command(path, workspace=None) -> "dict | None":
    """The picker command in one task file, or None unless the OWNER sent it."""
    got = sender_command(path, workspace)
    return got[1] if got and got[0] == "owner" else None


# A collaborator's add/pin waits for the owner: parked by the edge, answered by
# `approve`/`decline`; the parked command is the only thing an approval applies.

REFUSAL = ("Only this agent's owner can add or pin workers, so this request was "
           "not applied.")


def requests_path(workspace) -> Path:
    return pr.roster_path(workspace).parent / "picker-requests.json"


@contextlib.contextmanager
def _requests_locked(workspace):
    # Taken before the applied-ledger lock, never after it.
    p = pr.roster_path(workspace).parent / ".picker-requests.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_requests(workspace) -> dict:
    try:
        got = json.loads(requests_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def describe(cmd: dict) -> str:
    action = cmd.get("action")
    if action == "add":
        label = cmd.get("label")
        return "add a new worker to the pool" + (f" labelled '{label}'" if label else "")
    if action == "unpin":
        return "unpin this room from its worker (back to auto routing)"
    workers = " ".join(cmd.get("workers") or [])
    return (f"dedicate this room to worker {workers}" if cmd.get("dedicated")
            else f"pin this room to worker {workers}")


def _request_id(log: dict, task_id: str) -> str:
    import hashlib
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    for n in range(8, len(digest) + 1):
        rid = digest[:n]
        held = (log.get("requests") or {}).get(rid)
        if held is None or held.get("task_id") == task_id:
            return rid
    raise ValueError(f"no free request id for {task_id}")


def _ask_owner(workspace, question: str, context: str) -> str:
    """Ask through scripts/ask-owner.py, detached: the edge runs inside the
    watcher's dispatch loop, and the owner store may be slow or remote."""
    import subprocess
    script = _SCRIPTS.parents[2] / "scripts" / "ask-owner.py"
    log = pr.roster_path(workspace).parent / "picker-requests-ask.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, str(script), question, "--context", context,
                          "--workspace", str(pr._root(workspace))],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return f"ask-owner.py started (output: {log})"


def _post_notice(results_dir: Path, task_id: str, text: str) -> bool:
    """The task's own result, which the bridge threads to the asking message.
    Never over a result that already exists: that one is the answer."""
    from local_record import write_text_whole
    if ltp.find_result(results_dir, task_id) is not None:
        return False
    write_text_whole(results_dir / f"{task_id}.txt", text.rstrip("\n") + "\n")
    return True


def request_approval(workspace, path, *, results_dir=None, asker=None) -> "dict | None":
    """Park a collaborator's add/pin/unpin and ask the owner; refuse any other
    non-owner. None when this task is not the edge's to answer (owner, unknown)."""
    got = sender_command(path, workspace)
    if not got:
        return None
    sender, cmd, headers = got
    if sender in ("owner", "unknown") or cmd.get("action") not in COMMANDS:
        return None
    task_id = Path(path).stem
    rd = _results_dir(workspace, results_dir)
    if sender != "collaborator":
        _post_notice(rd, task_id, REFUSAL)
        return {"action": "refused", "task_id": task_id}

    with _requests_locked(workspace):
        log = _read_requests(workspace)
        rid = _request_id(log, task_id)
        rec = (log.get("requests") or {}).get(rid)
        new = rec is None
        if new:
            thread = (headers.get("thread_root") or headers.get("source_message_id") or "").strip()
            lane = (headers.get("source") or "").strip()
            seq = int(log.get("seq") or 0) + 1
            log["seq"] = seq
            room = (headers.get("channel_id") or "").strip() or None
            rec = {"id": rid, "task_id": task_id, "command": cmd, "status": "pending",
                   "seq": seq, "room_seq": _room_seq(workspace, room), "room": room,
                   "room_name": (headers.get("room_name") or "").strip() or None,
                   "thread_root": thread if thread.startswith("$") else None,
                   "lane": lane if lane and lane != SOURCE else "ag2space",
                   "requester": (headers.get("sender_name") or headers.get("user_id")
                                 or "a collaborator").strip()}
            log.setdefault("requests", {})[rid] = rec
            pr._write_atomic(requests_path(workspace), log)
    if new:
        where = rec["room_name"] or rec["room"] or "a room"
        question = (f"{rec['requester']} (a collaborator in {where}) asks to "
                    f"{describe(cmd)}. Approve?")
        context = (f"Request {rid}. Reply 'approve {rid}' or 'decline {rid}'; the core then runs "
                   f"skills/worker-pool/scripts/worker_picker_commands.py approve|decline {rid} "
                   "--task-file <your reply's task file>.")
        try:
            asked = (asker or _ask_owner)(workspace, question, context)
        except Exception as e:  # noqa: BLE001 — the collaborator must hear either way
            asked = None
            print(f"worker-picker: owner not asked for {rid}: {e!r}", file=sys.stderr)
        notice = (f"I've asked my owner for permission to {describe(cmd)}. "
                  "I'll post here once they decide." if asked else
                  f"Your request to {describe(cmd)} needs my owner's approval, and I "
                  "could not reach them to ask. Please ask them directly.")
        _post_notice(rd, task_id, notice)
    return {"action": "awaiting-owner", "id": rid, "task_id": task_id, "new": new,
            "status": rec.get("status")}


def _notify_room(rec: dict, text: str) -> str:
    """Post `text` in the requester's room through the task-progress skill when
    it is installed; otherwise say it was not posted, never that it was."""
    import subprocess
    notify = _SCRIPTS.parents[1] / "task-progress" / "scripts" / "notify.py"
    if not rec.get("room"):
        return "NOT posted: the request carries no room"
    if not notify.is_file():
        return "NOT posted: the task-progress skill is not installed"
    argv = [sys.executable, str(notify), "--source", rec.get("lane") or "ag2space",
            "--channel-id", rec["room"], "--message", text]
    if rec.get("thread_root"):
        argv += ["--thread-root", rec["thread_root"]]
    rc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode
    return "posted" if rc == 0 else f"NOT posted: notify.py exited {rc}"


def _room_seq(workspace, room) -> "int | None":
    return ((_read_applied(workspace).get("rooms") or {}).get(room) or {}).get("seq") if room else None


def owner_authority_error(path, workspace, rid: str) -> "str | None":
    """Why the task at `path` cannot decide request `rid`, or None when it can:
    its ATTESTED tier is owner and its text names the request."""
    import task_envelope as te
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"owner task unreadable: {e}"
    parsed = te.attested_task_headers(text, workspace)
    if (parsed.headers.get("access_tier") or "").strip() != "owner":
        return "the deciding task is not the owner's"
    if rid not in (parsed.body or ""):
        return f"the owner's task does not name request {rid}"
    return None


def _superseded(workspace, log: dict, rec: dict) -> "str | None":
    """Why approving this room-scoped request now would undo a later change."""
    for other in (log.get("requests") or {}).values():
        if (other is not rec and other.get("room") == rec.get("room")
                and (other.get("command") or {}).get("action") in ("pin", "unpin")
                and int(other.get("seq") or 0) > int(rec.get("seq") or 0)
                and other.get("status") in ("pending", "approved")):
            return f"a newer request for this room ({other['id']}) is {other['status']}"
    if _room_seq(workspace, rec.get("room")) != rec.get("room_seq"):
        return "this room's worker binding changed after the request was made"
    return None


def decide(workspace, rid: str, approve: bool, *, authority, notifier=None) -> dict:
    """Apply (or drop) exactly the parked command, then tell the collaborator.
    `authority` is the owner's own task answering it; a request decides once."""
    refused = owner_authority_error(authority, workspace, rid)
    if refused:
        return {"id": rid, "error": refused}
    with _requests_locked(workspace):
        log = _read_requests(workspace)
        rec = (log.get("requests") or {}).get(rid)
        if rec is None:
            return {"id": rid, "error": "no such request"}
        if rec.get("status") != "pending":
            return {"id": rid, "status": rec.get("status"), "error": "already decided"}
        cmd = rec["command"]
        out: dict = {"id": rid, "command": cmd}
        why = _superseded(workspace, log, rec) if (
            approve and cmd.get("action") in ("pin", "unpin")) else None
        if why:
            out["reason"] = why
        elif approve and cmd.get("action") in ("pin", "unpin"):
            try:
                applied = apply(workspace, cmd, task_id=f"{rec['task_id']}.approved-{rid}")
            except (pr.RosterError, OSError, ValueError) as e:
                applied = {"action": "skipped", "reason": str(e)}
            if applied.get("action") == "skipped":
                return {**out, "status": "pending", "error": f"not applied: {applied.get('reason')}"}
            out["applied"] = applied
        elif approve:
            out["next"] = ("run the owner's add: grow the installed core pool by one via "
                           "scripts/install-core-pool.sh" +
                           (f", label '{cmd['label']}'" if cmd.get("label") else ""))
        rec["status"] = "superseded" if why else ("approved" if approve else "declined")
        pr._write_atomic(requests_path(workspace), log)
    out["status"] = rec["status"]
    if why:
        text = (f"Your request to {describe(cmd)} was not applied: {why}, so applying it "
                "now would undo that. Nothing was changed.")
    elif out.get("applied"):
        text = f"My owner approved your request to {describe(cmd)}, and it is applied."
    elif approve:
        text = (f"My owner approved your request to {describe(cmd)}. The worker is added "
                "when my owner's Sutando runs the add.")
    else:
        text = f"My owner declined your request to {describe(cmd)}."
    out["notice"] = text
    try:
        out["posted"] = (notifier or _notify_room)(rec, text)
    except Exception as e:  # noqa: BLE001 — the decision stands; say the post failed
        out["posted"] = f"NOT posted: {e!r}"
    return out


def pending(workspace) -> list:
    return [r for r in (_read_requests(workspace).get("requests") or {}).values()
            if r.get("status") == "pending"]


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
    # The shared contract, never a second implementation: find_result is the
    # live-then-archive lookup and read_ready_result rejects a placeholder body.
    found = ltp.find_result(rd, str(task_id))
    if found is not None and read_ready_result(found) is not None:
        return f"a completed result already exists for this task ({found.name})"
    return None


def _record_applied(workspace, cmd: dict, task_id) -> int:
    """Record BEFORE mutating. A crash between the two must LOSE a command —
    which the owner can see and re-issue — never resurrect an older one.

    The caller already holds the ledger lock: flock blocks on a second
    acquisition of the same file, so taking it here would deadlock the writer
    against itself.
    """
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
    """Apply a parsed pin or unpin: binding, roster and (through the roster's
    own writer) the advertisement, so the new binding is on the wire without
    waiting for another task. `add` is create_worker's and returns None.

    `task_id` is required for a pin or unpin: without it the call cannot be
    replay-gated, and an ungated door is how this defect returns.
    """
    action = (cmd or {}).get("action")
    if action not in ("pin", "unpin"):
        return None
    # ONE critical section for gate, record and mutation: split, two probes can
    # commit sequence 1/2 and then mutate in the opposite order.
    with _applied_locked(workspace):
        reason = replay_reason(workspace, cmd, task_id, results_dir)
        if reason:
            return {"action": "skipped", "room": cmd.get("room"), "reason": reason}
        # Refuse a roster this would fail on part-way: bind writes, then the
        # advertisement raises, leaving a binding nothing published.
        pr.validate_current_roster(workspace)
        if action == "pin":
            workers = list(cmd.get("workers") or [])
            if len(workers) != 1:
                raise pr.RosterError(f"pin names {len(workers)} workers; a room takes one")
            roster = pr.bind_room(workspace, cmd["room"], workers[0])
        else:
            roster = pr.unbind_room(workspace, cmd["room"])
        # bind/unbind end in a compile, and the compile publishes; a publish
        # failure raises out of them, so nothing below records a half-applied pin.
        path = pa.advertisement_path(workspace)
        _record_applied(workspace, cmd, task_id)
    return {"action": action, "room": cmd["room"], "roster_version": roster.get("version"),
            "advertisement": str(path)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="read a worker-picker task's intent, or "
                                 "answer a collaborator's parked request")
    ap.add_argument("verb", nargs="?", default="parse",
                    choices=("parse", "request", "pending", "approve", "decline"))
    ap.add_argument("request_id", nargs="?", help="for approve/decline")
    ap.add_argument("--task-file", help="the picker task; for approve/decline, the "
                    "owner's own task that answers the request")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--results-dir", default=None)
    a = ap.parse_args(argv)
    if not a.task_file and a.verb != "pending":
        ap.error(f"{a.verb} needs --task-file")
    if a.verb in ("approve", "decline") and not a.request_id:
        ap.error(f"{a.verb} needs a request id")
    if a.verb == "parse":
        intent = parse_task_file(a.task_file)
        if intent is None:
            print("worker-picker: not a picker command", file=sys.stderr)
            return 3
        print(json.dumps(intent, indent=2))
        return 0
    ws = Path(a.workspace) if a.workspace else pr._root(None)
    if a.verb == "request":
        out = request_approval(ws, a.task_file, results_dir=a.results_dir)
        if out is None:
            print("worker-picker: not a collaborator's or non-owner's picker command",
                  file=sys.stderr)
            return 3
    elif a.verb == "pending":
        out = pending(ws)
    else:
        out = decide(ws, a.request_id, a.verb == "approve", authority=a.task_file)
    print(json.dumps(out, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
