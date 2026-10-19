"""`ag2-sparrow-claude-local inbox <verb>`: the Mod's only path to the task lifecycle.

The Mod keeps no shadow state machine and writes no claim or progress file: every claim, authorize,
confirm, stage, publish, park and progress mutation is one CLI call here, serialized per work root
under `state/claims.lock` (the lifecycle call) and `state/progress.lock` (the plugin record, through
the packaged writer). Local claims bind to the consumer generation named by the holder record; the
dead-generation recovery `hold` runs under the root flock lives here too. This module is the
adapter's one importer of task_lifecycle (the boundary test names it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

from ... import task_io, task_lifecycle as lifecycle
from ... import claude_local_records as records

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_PROGRESS = 0, 1, 2, 3
INTERRUPTED_BODY = ("[AG2 Space] This attempt was interrupted before the session answered; send the request "
                    "again if it is still wanted.")
PROTOCOL_FAILURE = "[AG2 Space] The session's turn ended without a reply (%s); send the request again if still wanted."
ROOT_VAR = "AG2_SPARROW_ROOT"


class Refused(Exception):
    pass


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare_root(root) -> str:
    """'' once the work dir is prepared and fenced to claude-local/1, else the refusal line."""
    problem = lifecycle.prepare_work_dir(root)
    if problem:
        return problem
    try:
        lifecycle.enable_local_format(root)
    except lifecycle.StateError as e:
        return str(e)
    return ""


def _lifecycle(root):
    return lifecycle.Lifecycle(root, identity=task_io.process_identity(), lineage=task_io.verify_lineage,
                               log=lambda *a: print("inbox:", *a, file=sys.stderr))


def _generation(root) -> dict:
    """The live consumer {pid, start} every local claim binds to; Refused without one."""
    consumer = task_io.holder_consumer(root)
    if not consumer or not consumer.get("pid") or consumer.get("live") is not True:
        raise Refused("no live consumer holds %s; the session is not the root's consumer" % root)
    return {"pid": consumer["pid"], "start": consumer.get("start")}


def recover_dead_generation(root) -> int:
    """Under claims.lock: every local claim whose generation verifies dead is recovered through the
    lifecycle's generation path; the count of generations handled. Never touches a live one."""
    lc = _lifecycle(root)
    with records.flocked(records.claims_lock_path(root)):
        dead = {}
        for task_id in lc.claims.ids():
            try:
                rec = lc.claims.load(task_id)
            except lifecycle.ClaimUnreadable:
                continue
            gen = (rec or {}).get("generation") if rec and rec.get("claim_mode") == lifecycle.LOCAL else None
            if gen and gen.get("pid") and rec["phase"] != lifecycle.SETTLED:
                key = (gen["pid"], gen.get("start"))
                if key not in dead and task_io.verify_lineage(gen["pid"], gen.get("start")) is False:
                    dead[key] = {"pid": gen["pid"], "start": gen.get("start")}
        for gen in dead.values():
            lc.recover(generation=gen, proof=False)
    return len(dead)


def _mirror(root, task_id, phase, out: dict, extra=None) -> dict:
    """The plugin record mirrors each claim's phase after the lifecycle step; a failed write marks the
    answer `hard` (exit 3: the step stands, intake closes) instead of hiding it."""
    def mutate(rec):
        inflight = dict(rec.get("inflight") or {})
        if phase is None:
            inflight.pop(task_id, None)
        else:
            inflight[task_id] = dict(extra or {}, phase=phase)
        rec["inflight"] = inflight
        return rec
    try:
        records.write_progress(root, mutate)
    except records.RecordError as e:
        out.update(hard=True, error="progress write failed: %s" % e)
    return out


def claim(root, task_id) -> dict:
    lc = _lifecycle(root)
    with records.flocked(records.claims_lock_path(root)):
        generation = _generation(root)
        verdict, state = lc.decide(task_id)
        if verdict != "call":
            raise Refused("%s is %s; only ABSENT may be submitted" % (task_id, state))
        try:
            attempt = lc.prepare(task_id, INTERRUPTED_BODY, mode=lifecycle.LOCAL, generation=generation)
        except lifecycle.AttemptInProgress as e:
            raise Refused(str(e))
    return _mirror(root, task_id, lifecycle.PREPARED, {"task_id": task_id, "attempt": attempt, "generation": generation},
                   {"attempt": attempt})


def authorize(root, task_id, attempt, hash_) -> dict:
    lc = _lifecycle(root)
    with records.flocked(records.claims_lock_path(root)):
        token = lc.authorize_call(task_id, int(attempt), text_hash=hash_)
    if token is None:
        raise Refused("%s attempt %s is not PREPARED: no submit authorization" % (task_id, attempt))
    return _mirror(root, task_id, lifecycle.CALLING, {"task_id": task_id, "token": token}, {"attempt": int(attempt)})


def confirm(root, task_id, turn_id, nonce, hash_) -> dict:
    lc = _lifecycle(root)
    with records.flocked(records.claims_lock_path(root)):
        try:
            lc.confirm_turn(task_id, turn_id, nonce, hash_)
        except lifecycle.TurnRefused as e:
            raise Refused(str(e))
    return _mirror(root, task_id, lifecycle.TURN_CONFIRMED, {"task_id": task_id, "turn_id": turn_id}, {"turn_id": turn_id})


def stage(root, task_id, turn_id, body: bytes) -> dict:
    lc = _lifecycle(root)
    with records.flocked(records.claims_lock_path(root)):
        outcome = lc.stage_result(task_id, body, turn_id)
    if outcome != "staged":
        raise Refused("%s: staging %s" % (task_id, outcome))
    return _mirror(root, task_id, lifecycle.RESULT_STAGED, {"task_id": task_id, "outcome": outcome}, {"turn_id": turn_id})


def publish(root, task_id) -> dict:
    lc = _lifecycle(root)
    with records.flocked(records.claims_lock_path(root)):
        outcome = lc.publish_and_settle(task_id)
    if outcome != "settled":
        raise Refused("%s: publication %s" % (task_id, outcome))
    return _mirror(root, task_id, None, {"task_id": task_id, "outcome": outcome})


def park(root, task_id, turn_id, detail) -> dict:
    """A completed or aborted turn with nothing staged: the explicit protocol failure, published once."""
    body = (PROTOCOL_FAILURE % detail).encode("utf-8")
    staged = stage(root, task_id, turn_id, body)
    out = publish(root, task_id)
    out.update({k: v for k, v in staged.items() if k in ("hard", "error")})
    return dict(out, parked=detail)


def progress(root, session_id=None, nonce=None, intake=None, reason=None, generation=None) -> dict:
    def mutate(rec):
        for key, value in (("session_id", session_id), ("nonce", nonce), ("intake", intake), ("reason", reason)):
            if value is not None:
                rec[key] = value
        if generation is not None:
            rec["generation"] = generation
        return rec
    return records.write_progress(root, mutate)


def check_inode(directory, expect) -> dict:
    st = os.stat(directory)
    found = "%d:%d" % (st.st_dev, st.st_ino)
    if found != expect:
        raise Refused("%s is %s, not the held inbox %s" % (directory, found, expect))
    return {"inode": found}


def _root(args) -> str:
    root = args.root or os.environ.get(ROOT_VAR) or os.path.dirname(os.getcwd())
    if not os.path.isabs(root):
        raise Refused("the work root must be absolute (%s or --root)" % ROOT_VAR)
    return root


def main(args) -> int:
    try:
        root = _root(args)
        verb = args.verb
        if verb == "claim":
            out = claim(root, args.task_id)
        elif verb == "authorize":
            out = authorize(root, args.task_id, args.attempt, args.text_hash)
        elif verb == "confirm":
            out = confirm(root, args.task_id, args.turn_id, args.nonce, args.text_hash)
        elif verb == "stage":
            out = stage(root, args.task_id, args.turn_id, sys.stdin.buffer.read())
        elif verb == "publish":
            out = publish(root, args.task_id)
        elif verb == "park":
            out = park(root, args.task_id, args.turn_id, args.detail)
        elif verb == "progress":
            gen = task_io.holder_consumer(root) if args.bind_generation else None
            out = progress(root, args.session_id, args.nonce, args.intake, args.reason,
                           {"pid": gen["pid"], "start": gen["start"]} if gen and gen.get("pid") else None)
        elif verb == "check-inode":
            out = check_inode(args.dir or os.getcwd(), args.expect)
        else:
            raise Refused("unknown verb %s" % verb)
    except records.RecordError as e:
        print(json.dumps({"error": "progress write failed: %s" % e, "hard": True}))
        return EXIT_PROGRESS
    except (Refused, lifecycle.StateError, lifecycle.ClaimUnreadable, OSError, ValueError) as e:
        print(json.dumps({"refused": str(e)}))
        return EXIT_REFUSED
    print(json.dumps(out, sort_keys=True))
    return EXIT_PROGRESS if out.get("hard") else EXIT_OK


def add_parser(sub) -> None:
    p = sub.add_parser("inbox", help="the Mod's lifecycle verbs (claim, authorize, confirm, stage, publish, park, "
                       "progress, check-inode); never by hand")
    p.add_argument("--root", help="the work root (default: AG2_SPARROW_ROOT, else the cwd's parent)")
    v = p.add_subparsers(dest="verb", required=True)
    c = v.add_parser("claim"); c.add_argument("task_id")
    a = v.add_parser("authorize"); a.add_argument("task_id"); a.add_argument("attempt", type=int)
    a.add_argument("--text-hash", required=True)
    f = v.add_parser("confirm"); f.add_argument("task_id"); f.add_argument("turn_id"); f.add_argument("nonce")
    f.add_argument("--text-hash", required=True)
    s = v.add_parser("stage"); s.add_argument("task_id"); s.add_argument("turn_id")
    u = v.add_parser("publish"); u.add_argument("task_id")
    k = v.add_parser("park"); k.add_argument("task_id"); k.add_argument("turn_id"); k.add_argument("--detail", default="no result")
    g = v.add_parser("progress")
    g.add_argument("--session-id"); g.add_argument("--nonce"); g.add_argument("--intake", choices=("open", "stopped"))
    g.add_argument("--reason"); g.add_argument("--bind-generation", action="store_true")
    i = v.add_parser("check-inode"); i.add_argument("--dir"); i.add_argument("--expect", required=True)
    p.set_defaults(func=main)
