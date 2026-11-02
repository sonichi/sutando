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
import time
from contextlib import contextmanager
from pathlib import Path

from ... import connect_common as cm, task_io, task_lifecycle as lifecycle
from ... import claude_local_records as records
from . import hmac_policy
from .shape import LOCAL

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_PROGRESS = 0, 1, 2, 3
INTERRUPTED_BODY = ("[s.space] This attempt was interrupted before the session answered; send the request "
                    "again if it is still wanted.")
PROTOCOL_FAILURE = "[s.space] The session's turn ended without a reply (%s); send the request again if still wanted."
ROOT_VAR = "AG2_SPARROW_ROOT"
CONFIG_DIR_VAR = "AG2_SPARROW_CONFIG_DIR"
ALLOW_NON_OWNER = "ALLOW_NON_OWNER"


# The verbs whose success is a durable progress write: one clears the launcher's alarm.
PROGRESS_VERBS = frozenset(("claim", "interrupt", "authorize", "confirm", "stage", "publish", "park", "progress"))


class Refused(Exception):
    pass


class Parked(Refused):
    """A step that may succeed on a later call (a parked publication): the caller retries."""


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


def root_problem(root) -> str:
    """prepare_root's layout verdict without writing anything: what install checks before it starts."""
    return lifecycle.prepare_work_dir(root, check_only=True) or ""


def retryable(problem: str) -> bool:
    """Whether a prepare_root refusal may clear by itself (the dir was unreadable), not a layout one."""
    return problem.startswith(lifecycle.UNCHECKED) or not problem.startswith("refusing to start:")

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
            # A SETTLED claim whose settlement is not complete, or due for compaction, brings its generation in too.
            owed = rec and rec["phase"] == lifecycle.SETTLED and (
                not lc.settlement_complete(task_id) or lc.clock() - rec.get("settled_at", lc.clock()) > lifecycle.COMPACT_AFTER)
            if gen and gen.get("pid") and (rec["phase"] != lifecycle.SETTLED or owed):
                key = (gen["pid"], gen.get("start"))
                if key not in dead and task_io.verify_lineage(gen["pid"], gen.get("start")) is False:
                    dead[key] = {"pid": gen["pid"], "start": gen.get("start")}
        for gen in dead.values():
            lc.recover(generation=gen, proof=False)
        lc.reconcile_unbound()
    return len(dead)


def _installed_config(environ=None) -> Path:
    """The config the pane names (its config dir), else the CLI's own."""
    config_dir = (os.environ if environ is None else environ).get(CONFIG_DIR_VAR)
    return Path(config_dir) / cm.config_path(LOCAL).name if config_dir else cm.config_path(LOCAL)


def allow_non_owner(environ=None) -> bool:
    """The installed --allow-non-owner, read from the config the pane names; unreadable reads as off."""
    try:
        return cm.read_config(_installed_config(environ)).get(ALLOW_NON_OWNER) == "1"
    except (OSError, ValueError):
        return False


def envelope(verdict: str, environ=None, key_seen=None) -> dict:
    """The Mod's question before a claim: is a task with this envelope verdict submitted now? Answered
    by hmac_policy from the installed config and the key file as it is at this call; a config that is
    missing, unreadable, unparsable or short of a required key parks (fail closed) and the Mod retries.
    `key_seen` is the digest of the key the verifier read; a different key here holds the task."""
    cfg, why = cm.load_config_at(LOCAL, _installed_config(environ))
    if why:
        raise Parked("the installed config is not usable (%s); retried" % why)
    submit, reason, retry = hmac_policy.decide(cfg, verdict, key_seen)
    return {"verdict": verdict, "mode": hmac_policy.mode(cfg), "submit": submit, "reason": reason, "retry": retry}


def admit(root, task_id, allow: bool) -> "tuple[str | None, dict, str]":
    """(admission, headers, why refused) for tasks/<task_id>.txt through the shared task_io.admission. A
    file with no access_tier header is malformed (Sparrow's bridge always writes one); a collaborator is
    served only under --allow-non-owner, like every other non-owner."""
    path = os.path.join(root, "tasks", task_id + ".txt")
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = task_io.attested_headers(f.read()).headers
    if "access_tier" not in raw:
        return None, {}, "malformed: no access_tier header"
    headers, _, _ = task_io.parse_task(path)
    kind = task_io.admission(headers, allow)
    if kind == "collaborator" and not allow:
        kind = None
    why = "" if kind else "sender tier %s (collaborator %s) and --allow-non-owner is off" % (
        headers["access_tier"], headers["collaborator"])
    return kind, headers, why


# The lifecycle's answers about the claim itself: final, whatever raised them. An unreadable claim is not
# one: it resolved nothing, so it retries like any other failure.
DECISIONS = (Refused, lifecycle.TurnRefused, lifecycle.AttemptInProgress)


class Committed(Exception):
    """The step committed, then something failed: hard, never a retry; `data` is read from the claim
    as it now stands, never from what the step meant to do."""

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = dict(data or {})


def _token_for(rec, attempt, text_hash):
    """The stored one-use token, only for the request it was issued to (same attempt and text hash)."""
    call = (rec or {}).get("call") or {}
    if rec and rec["phase"] == lifecycle.CALLING and rec["attempt"] == attempt and call.get("nonce") \
            and call.get("text_hash") == text_hash:
        return call["nonce"]
    return None


# What a hard reply carries, per verb, from the re-read claim: only what that claim proves for this request.
COMMITTED_FIELDS = {
    "claim": lambda rec, req: {"attempt": rec["attempt"]} if rec["phase"] == lifecycle.PREPARED else {},
    "authorize": lambda rec, req: {"token": _token_for(rec, req["attempt"], req["text_hash"]), "attempt": rec["attempt"]}
    if _token_for(rec, req["attempt"], req["text_hash"]) else {},
    "confirm": lambda rec, req: {"turn_id": rec.get("turn_id")} if rec["phase"] == lifecycle.TURN_CONFIRMED else {},
    "stage": lambda rec, req: {"outcome": "staged"} if rec["phase"] == lifecycle.RESULT_STAGED else {},
    "publish": lambda rec, req: {"outcome": "settled"} if rec["phase"] == lifecycle.SETTLED and req.get("complete") else {},
}


def _committed(lc, task_id, verb, request=None) -> "dict | None":
    """The hard reply's data, from the claim re-read now: its phase always, the verb's fields when proven;
    None when it cannot be re-read, so the caller answers retry and the next call reconciles."""
    try:
        rec = lc.claims.load(task_id)
    except Exception:
        return None
    if not rec:
        return {"task_id": task_id, "phase": "absent"}
    try:
        # A hard reply is final only over a complete settlement; otherwise the caller's retry completes it.
        request = dict(request or {}, complete=lc.settlement_complete(task_id))
    except Exception:
        request = dict(request or {}, complete=False)
    return dict(COMMITTED_FIELDS.get(verb, lambda r, q: {})(rec, request), task_id=task_id, phase=rec["phase"])


def _committed_reply(lc, task_id, verb, request, message):
    data = _committed(lc, task_id, verb, request)
    if data is None:
        return Parked("%s; the claim could not be re-read; retried" % message)
    return Committed(message, data)


@contextmanager
def _claims_step(root, lc, task_id, verb, request=None):
    """claims.lock around one lifecycle step: anything raised before the commit point is a retry (unless
    a decision); from it on (a rename that landed, only the release left) hard, as `_committed` reads it."""
    done = False
    try:
        with records.flocked(records.claims_lock_path(root)):
            try:
                yield
            except lifecycle.StateCommitted as e:
                raise _committed_reply(lc, task_id, verb, request, "the step's write landed, then failed (%s)" % e)
            done = True
    except (Committed, Parked) + DECISIONS:
        raise
    except Exception as e:
        if done:
            raise _committed_reply(lc, task_id, verb, request, "the step committed, then %s: %s" % (
                type(e).__name__, getattr(e, "strerror", None) or e))
        if isinstance(e, lifecycle.StateError) and not isinstance(e, lifecycle.StateIOError):
            raise
        raise Parked("the step did not commit (%s: %s); retried" % (type(e).__name__, getattr(e, "strerror", None) or e))


def _config_dir(environ=None) -> Path:
    config_dir = (os.environ if environ is None else environ).get(CONFIG_DIR_VAR)
    return Path(config_dir) if config_dir else cm.config_path(LOCAL).parent


def _clear_stuck(root, task_id) -> bool:
    """A resolved task's stuck mark and in-flight mirror dropped in one write: True once neither is left
    (none to clear writes nothing), False when the record could not be read or written, so the caller
    keeps the task and retries."""
    def mutate(rec):
        for key in ("stuck", "inflight"):
            if isinstance(rec.get(key), dict) and task_id in rec[key]:
                rec = dict(rec, **{key: {k: v for k, v in rec[key].items() if k != task_id}})
        return rec
    try:
        plugin = records.load_plugin(root)
        if task_id not in (plugin.get("stuck") or {}) and task_id not in (plugin.get("inflight") or {}):
            return True
        records.write_progress(root, mutate)
        return True
    except (records.RecordError, OSError, TypeError):
        return False


def _settle_terminal(lc, task_id) -> None:
    """A local or call-less claim at RESULT_STAGED or PUBLISHED whose result already exists is settled on that result
    (the publisher records it and adds none); Parked when it does not settle now. The caller holds
    claims.lock."""
    rec = lc.claims.load(task_id)
    if not rec or rec.get("claim_mode") not in (None, lifecycle.LOCAL):
        return
    if rec["phase"] not in (lifecycle.RESULT_STAGED, lifecycle.PUBLISHED):
        return
    if lc.publish_and_settle(task_id) != "settled":
        raise Parked("%s: answered, but its claim did not settle now (%s); retried" % (task_id, lc.claims.load(task_id)["phase"]))


def _mirror(root, task_id, phase, out: dict, extra=None, unstuck=False) -> dict:
    """The plugin record mirrors each claim's phase after the lifecycle step; a failed write marks the
    answer `hard` (exit 3: the step stands, intake closes) instead of hiding it. `unstuck` also drops
    the task's stuck mark, in the same write."""
    def mutate(rec):
        if unstuck and isinstance(rec.get("stuck"), dict) and task_id in rec["stuck"]:
            rec["stuck"] = {k: v for k, v in rec["stuck"].items() if k != task_id}
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


def _repair_settled(lc, task_id) -> None:
    """Before any reply that can be final: a SETTLED claim's settlement completed (lifecycle.reconcile_settled);
    a repair that fails raises, the caller's retry. The caller holds claims.lock."""
    rec = lc.claims.load(task_id)
    if rec and rec["phase"] == lifecycle.SETTLED:
        lc.reconcile_settled(task_id)


def claim(root, task_id) -> dict:
    lc = _lifecycle(root)
    terminal, lost = False, ""
    with _claims_step(root, lc, task_id, "claim"):
        _repair_settled(lc, task_id)
        generation = _generation(root)
        verdict, state = lc.decide(task_id)
        if verdict == "call":
            kind, headers, why = admit(root, task_id, allow_non_owner())
            if kind is None:
                outcome = lc.complete_without_call(task_id, task_io.NON_OWNER_REFUSED, why)
                raise Refused("%s not admitted (%s); the owner-only refusal is %s" % (task_id, why, outcome))
            try:
                attempt = lc.prepare(task_id, INTERRUPTED_BODY, mode=lifecycle.LOCAL, generation=generation)
            except lifecycle.AttemptInProgress as e:
                lost = lost_turn_answered(lc, task_id, generation)
                if not lost:
                    raise Refused(str(e))
        else:
            if state in lifecycle.TERMINAL:
                _settle_terminal(lc, task_id)
            terminal = resolved(lc, task_id)[0]
    if lost:
        # After claims.lock, as every mirror write; a mirror that fails is the hard answer, the Mod retries.
        out = _mirror(root, task_id, None, {"task_id": task_id, "phase": lifecycle.SETTLED, "answered": lost}, unstuck=True)
        if out.get("hard"):
            return out
        raise Refused("%s: %s; only ABSENT may be submitted" % (task_id, lost))
    if verdict != "call":
        if not terminal:
            raise Parked("%s is %s, not claimable yet; retried" % (task_id, state))
        # After claims.lock (the progress lock is never taken under it); a mark not yet cleared keeps it.
        if not _clear_stuck(root, task_id):
            raise Parked("%s is %s, but its stuck mark could not be cleared: cleanup pending; retried" % (task_id, state))
        raise Refused("%s is %s; only ABSENT may be submitted" % (task_id, state))
    return _mirror(root, task_id, lifecycle.PREPARED,
                   {"task_id": task_id, "attempt": attempt, "generation": generation, "admission": kind,
                    "access_tier": headers["access_tier"], "collaborator": headers["collaborator"] == "true"},
                   {"attempt": attempt}, unstuck=True)


def resolved(lc, task_id) -> "tuple[bool, str]":
    """Whether the task is resolved, read from its disposition alone: the claim SETTLED, or a result
    already terminal in results/ (ready, archived, quarantined). Anything unreadable is unresolved."""
    try:
        rec = lc.claims.load(task_id)
        if rec and rec.get("phase") == lifecycle.SETTLED:
            # Final only once the settlement is whole (ledger entry, no staged body); else a retry.
            return lc.settlement_complete(task_id), lifecycle.SETTLED
        state = lc.classify(task_id)
        return state in lifecycle.TERMINAL, state
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def lost_turn_answered(lc, task_id, generation) -> str:
    """A local claim past its call, claimed afresh: a Mod never re-claims a task it still holds, so the text
    that could finish its stage is gone. An accepted answer is delivered as recorded (its verifying blob,
    or the staged body), whichever generation's claim it is; a claim of the live `generation` with no such
    answer is answered as interrupted now, not at the next generation. What was answered; '' when it is
    not such a claim; Parked when its publication did not land, so the next claim retries it. The caller
    holds claims.lock."""
    rec = lc.claims.load(task_id)
    if not rec or rec.get("claim_mode") != lifecycle.LOCAL:
        return ""
    live = rec.get("generation") == generation
    phase = rec["phase"]
    if phase == lifecycle.TURN_CONFIRMED and rec.get("staged") and not live:
        # Another generation's turn: only its verifying answer is ours to finish; an interruption is recovery's.
        if lc.finish_verified(task_id) != "finished":
            return ""
        answered = "its accepted answer, staged and never published, is published now"
    elif phase == lifecycle.TURN_CONFIRMED and rec.get("staged"):
        # The lifecycle decides what the recorded descriptor is worth, exactly as recovery lets it.
        closed = lc.close_turn(task_id, "turn confirmed, the accepted answer's staging was lost")
        if closed == "parked":
            raise Parked("%s: the accepted answer could not be closed now; retried" % task_id)
        answered = "its accepted answer, staged and never published, is published now" if closed == "finished" \
            else "its accepted answer could not be finished; answered as interrupted now"
    elif phase == lifecycle.OUTCOME_UNKNOWN:
        # A committed intermediate state (the rename landed, its directory fsync failed): resumed, never retired.
        if lc.resume_outcome(task_id) != "staged":
            raise Parked("%s: its recorded outcome could not be resumed now; retried" % task_id)
        answered = "its recorded outcome, interrupted, is published now"
    elif phase in (lifecycle.RESULT_STAGED, lifecycle.PUBLISHED):
        answered = "its staged answer is published now"
    else:
        return ""
    try:
        published = lc.publish_and_settle(task_id, io_raises=True)
    except lifecycle.PublishedNotDurable as e:
        raise Parked("%s: the result landed, its durability unknown (%s); retried" % (task_id, e))
    if published != "settled":
        raise Parked("%s: %s, its answer is not published yet; retried" % (task_id, lc.claims.load(task_id)["phase"]))
    return answered


def unresolved_stuck(root, marks) -> dict:
    """The stuck marks whose task is still unresolved: a mark never outlives its answer, whoever
    published it and whether or not the Mod ever sees the task file again."""
    if not isinstance(marks, dict) or not marks:
        return {}
    try:
        lc = _lifecycle(root)
    except Exception:
        return dict(marks)
    return {k: v for k, v in marks.items() if not resolved(lc, k)[0]}


def interrupt(root, task_id, detail) -> dict:
    """A task this session can no longer take (intake stopped for good): answered once without a call,
    with the interrupted answer or, for a sender the policy does not admit, the owner-only refusal.
    Whatever happens on the way, the reply is decided by the disposition afterwards: resolved, or
    Parked (retry) with the reason; no failure class is named."""
    why, answered, kind, lc = "", False, None, None
    try:
        lc = _lifecycle(root)
        with _claims_step(root, lc, task_id, "interrupt"):
            _repair_settled(lc, task_id)
            rec = lc.claims.load(task_id)
            phase = rec.get("phase") if rec else None
            if phase in (lifecycle.RESULT_STAGED, lifecycle.PUBLISHED):
                # An earlier interrupt staged its answer but could not publish it: publish that one.
                kind = str((rec.get("outcome") or {}).get("detail") or "").startswith("intake stopped")
                answered = lc.publish_and_settle(task_id) == "settled"
            elif phase != lifecycle.SETTLED:
                verdict, state = lc.decide(task_id)
                if verdict == "call":
                    kind, _headers, refusal = admit(root, task_id, allow_non_owner())
                    body, reason = (INTERRUPTED_BODY, "intake stopped: %s" % detail) if kind else (
                        task_io.NON_OWNER_REFUSED, refusal)
                    answered = lc.complete_without_call(task_id, body, reason) == "settled"
                else:
                    why = "the result is %s" % state
    except Exception as e:
        why = "%s: %s" % (type(e).__name__, e)
    done, state = resolved(lc, task_id) if lc is not None else (False, "")
    if not done:
        raise Parked("%s: not answered yet (%s); retried" % (task_id, why or "disposition %s" % (state or "unknown")))
    return _mirror(root, task_id, None, {"task_id": task_id, "resolved": True, "answered": answered,
                                         "interrupted": bool(answered and kind), "state": state}, unstuck=True)


def authorize(root, task_id, attempt, hash_) -> dict:
    """The one-use token for this attempt and text: issued once, and the same one again to the same request
    (a lost reply), never to another text."""
    lc = _lifecycle(root)
    request = {"attempt": int(attempt), "text_hash": hash_}
    try:
        with _claims_step(root, lc, task_id, "authorize", request):
            token = lc.authorize_call(task_id, int(attempt), text_hash=hash_) or \
                _token_for(lc.claims.load(task_id), int(attempt), hash_)
    except Committed as e:
        if e.data.get("token"):
            raise
        if e.data.get("phase") == lifecycle.PREPARED:
            raise Parked("%s attempt %s: not authorized yet (%s); retried" % (task_id, attempt, e))
        raise Refused("%s attempt %s: no submit authorization for this text (%s)" % (task_id, attempt, e.data.get("phase")))
    if token is None:
        raise Refused("%s attempt %s is not PREPARED: no submit authorization" % (task_id, attempt))
    return _mirror(root, task_id, lifecycle.CALLING, {"task_id": task_id, "token": token}, {"attempt": int(attempt)})


def confirm(root, task_id, turn_id, nonce, hash_) -> dict:
    lc = _lifecycle(root)
    with _claims_step(root, lc, task_id, "confirm"):
        try:
            lc.confirm_turn(task_id, turn_id, nonce, hash_)
        except lifecycle.TurnRefused as e:
            rec = lc.claims.load(task_id)
            same = rec and rec["phase"] == lifecycle.TURN_CONFIRMED and rec.get("turn_id") == turn_id and \
                (rec.get("call") or {}).get("nonce") == nonce
            if not same:        # the same confirmation again (a lost reply) is its own answer
                raise Refused(str(e))
    return _mirror(root, task_id, lifecycle.TURN_CONFIRMED, {"task_id": task_id, "turn_id": turn_id}, {"turn_id": turn_id})


def stage(root, task_id, turn_id, body: bytes) -> dict:
    lc = _lifecycle(root)
    with _claims_step(root, lc, task_id, "stage"):
        outcome = lc.stage_result(task_id, body, turn_id)
        rec = lc.claims.load(task_id) if outcome != "staged" else None
    if outcome != "staged":
        staged = (rec or {}).get("staged") or {}
        if rec and rec["phase"] == lifecycle.TURN_CONFIRMED and staged.get("length") == len(body) \
                and staged.get("sha256") == hashlib.sha256(body).hexdigest():
            # This body's descriptor is recorded but its blob is not in place: the identical retry finishes.
            raise Parked("%s: this answer is accepted but its staging did not finish; retried" % task_id)
        raise Refused("%s: staging %s" % (task_id, outcome))
    return _mirror(root, task_id, lifecycle.RESULT_STAGED, {"task_id": task_id, "outcome": outcome}, {"turn_id": turn_id})


def publish(root, task_id) -> dict:
    lc = _lifecycle(root)
    with _claims_step(root, lc, task_id, "publish"):
        rec = lc.claims.load(task_id)
        if rec and rec["phase"] == lifecycle.TURN_CONFIRMED and rec.get("staged") and \
                lc.finish_staged(task_id) != "staged":
            # An accepted answer whose blob is not verified yet: only the identical stage retry finishes it.
            raise Parked("%s: the accepted answer's staging has not finished; retried" % task_id)
        # A publication that fails on I/O (an unwritable results dir) is the pre-commit retry, not a soft refusal.
        try:
            outcome = lc.publish_and_settle(task_id, io_raises=True)
        except lifecycle.PublishedNotDurable as e:
            raise Parked("%s: the result landed, its durability unknown (%s); retried" % (task_id, e))
    if outcome != "settled":
        raise Refused("%s: publication %s" % (task_id, outcome))
    return _mirror(root, task_id, None, {"task_id": task_id, "outcome": outcome})


def park(root, task_id, turn_id, detail) -> dict:
    """A completed or aborted turn with nothing staged: the explicit protocol failure, published once.
    A turn whose answer was already accepted (its descriptor recorded) publishes that answer instead.
    The identical park again after one that settled but whose reply was lost answers settled, naming
    the protocol failure only when that is what it delivered; nothing is published twice."""
    rec = _lifecycle(root).claims.load(task_id)
    body = (PROTOCOL_FAILURE % detail).encode("utf-8")
    if rec and rec.get("staged") and rec["phase"] in (lifecycle.TURN_CONFIRMED, lifecycle.RESULT_STAGED, lifecycle.SETTLED):
        # `parked` names what the recorded descriptor delivers: this park's failure line, or an accepted answer.
        failure = rec["staged"].get("sha256") == hashlib.sha256(body).hexdigest()
        return dict(publish(root, task_id), parked=detail if failure else False)
    if rec and rec["phase"] == lifecycle.SETTLED:
        return dict(publish(root, task_id), parked=False)
    staged = stage(root, task_id, turn_id, body)
    out = publish(root, task_id)
    out.update({k: v for k, v in staged.items() if k in ("hard", "error")})
    return dict(out, parked=detail)


def progress(root, session_id=None, nonce=None, intake=None, reason=None, generation=None, stuck=None,
             stuck_detail="", unstuck=None) -> dict:
    """`stuck` marks an interrupted answer that has not been published for a while (verify and the card
    name it); `unstuck` clears it once the answer lands or the claim is final."""
    def mutate(rec):
        for key, value in (("session_id", session_id), ("nonce", nonce), ("intake", intake), ("reason", reason)):
            if value is not None:
                rec[key] = value
        if generation is not None:
            rec["generation"] = generation
        marks = dict(rec["stuck"]) if isinstance(rec.get("stuck"), dict) else {}
        if stuck:
            marks[stuck] = dict(marks.get(stuck) or {"since": time.time()}, detail=stuck_detail)
        if unstuck:
            marks.pop(unstuck, None)
        if stuck or unstuck or marks:
            rec["stuck"] = unresolved_stuck(root, marks)
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
        elif verb == "interrupt":
            out = interrupt(root, args.task_id, args.detail)
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
                           {"pid": gen["pid"], "start": gen["start"]} if gen and gen.get("pid") else None,
                           args.stuck, args.stuck_detail, args.unstuck)
        elif verb == "check-inode":
            out = check_inode(args.dir or os.getcwd(), args.expect)
        elif verb == "envelope":
            out = envelope(args.verdict, key_seen=args.key_seen)
        else:
            raise Refused("unknown verb %s" % verb)
    except records.RecordError as e:
        records.raise_progress_alarm(_config_dir(), str(e))
        print(json.dumps({"error": "progress write failed: %s" % e, "hard": True}))
        return EXIT_PROGRESS
    except Committed as e:
        print(json.dumps(dict(e.data, error=str(e), hard=True), sort_keys=True))
        return EXIT_PROGRESS
    except Parked as e:
        print(json.dumps({"refused": str(e), "retry": True}))
        return EXIT_REFUSED
    except (Refused, lifecycle.StateError, lifecycle.ClaimUnreadable, OSError, ValueError) as e:
        print(json.dumps({"refused": str(e)}))
        return EXIT_REFUSED
    # The launcher's own view of the record's writability, in the config dir, so a broken state/ cannot hide it.
    if out.get("hard"):
        records.raise_progress_alarm(_config_dir(), str(out.get("error") or "progress write failed"))
    elif verb in PROGRESS_VERBS:
        records.clear_progress_alarm(_config_dir())
    print(json.dumps(out, sort_keys=True))
    return EXIT_PROGRESS if out.get("hard") else EXIT_OK


def add_parser(sub) -> None:
    p = sub.add_parser("inbox", help="the Mod's lifecycle verbs (claim, interrupt, authorize, confirm, stage, publish, park, "
                       "progress, check-inode, envelope); never by hand")
    p.add_argument("--root", help="the work root (default: AG2_SPARROW_ROOT, else the cwd's parent)")
    v = p.add_subparsers(dest="verb", required=True)
    c = v.add_parser("claim"); c.add_argument("task_id")
    n = v.add_parser("interrupt"); n.add_argument("task_id"); n.add_argument("--detail", default="")
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
    g.add_argument("--stuck"); g.add_argument("--stuck-detail", default=""); g.add_argument("--unstuck")
    i = v.add_parser("check-inode"); i.add_argument("--dir"); i.add_argument("--expect", required=True)
    e = v.add_parser("envelope"); e.add_argument("--verdict", required=True); e.add_argument("--key-seen")
    p.set_defaults(func=main)
