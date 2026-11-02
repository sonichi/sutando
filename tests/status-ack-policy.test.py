#!/usr/bin/env python3
"""Contract test for src/status_ack_policy.py: one initial status ack per task, from observed facts.

Each requirement is a predicate over a `decide` function. It must hold for the real policy and,
as its CONTROL, fail for a deliberately broken variant, so a requirement cannot pass vacuously.
Run: python3 tests/status-ack-policy.test.py
"""
import dataclasses
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
import status_ack_policy as P  # noqa: E402

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def holds(name, case, broken):
    """`case(decide)` must be True for the policy and False for `broken`."""
    check(case(P.decide), name)
    check(not case(broken), f"CONTROL: {name} fails without the mechanism")


T0 = 1_792_000_000.0
ROOM, MSG, ROOT = "!room:ag2.space", "$ask", "$root"
NORMAL = P.DEFAULT_FIRST_ACK_DEADLINE_S["normal"]


def headers(**kw):
    h = {"source": "ag2space", "channel_id": ROOM, "source_message_id": MSG, "access_tier": "owner"}
    h.update({k: v for k, v in kw.items() if v is not None})
    for k in [k for k, v in kw.items() if v is None]:
        h.pop(k, None)
    return h


def obs(**kw):
    base = dict(task_id="task-1792000000000", headers=headers(), intake_at=T0, phase=None,
                inbox="session")
    base.update(kw)
    return P.TaskObservation(**base)


def broken_with(**overrides):
    """decide() with one of the policy's own observation fields rewritten before deciding."""
    def run(o, now, deadlines=None):
        return P.decide(dataclasses.replace(o, **{k: f(o) for k, f in overrides.items()}), now, deadlines)
    return run


# --- Immutable intake deadline -------------------------------------------------------------------
def every_minute_work(decide):
    # A work event every minute: last_activity_at always fresh. The ack still fires at the deadline.
    return decide(obs(last_activity_at=T0 + NORMAL - 1, started_at=T0 + 1), T0 + NORMAL).action == P.SPEAK


def ten_minute_call(decide):
    # One uninterrupted call that began 5 s after intake and is still running at the deadline.
    return decide(obs(started_at=T0 + 5, last_activity_at=T0 + 5), T0 + NORMAL).action == P.SPEAK


anchor_on_activity = broken_with(intake_at=lambda o: max(o.intake_at, o.last_activity_at or o.intake_at))
anchor_on_start = broken_with(intake_at=lambda o: o.started_at or o.intake_at)
holds("deadline: a work event every minute still acks at the deadline", every_minute_work, anchor_on_activity)
holds("deadline: one uninterrupted call acks at the deadline", ten_minute_call, anchor_on_start)


def intake_never_later(intake_fn):
    tid = "task-1792000000000"
    first = intake_fn(tid, {})
    later = intake_fn(tid, {}, T0 + 400)  # a first-seen recorded after intake
    forged = intake_fn(tid, {"timestamp": "2026-12-31T00:00:00Z"})
    return first == later == forged == T0


def intake_latest(tid, headers_, first_seen=None):
    stamps = [s for s in (P._header_epoch(headers_.get("timestamp")), P._id_epoch(tid), first_seen) if s]
    return max(stamps)


def intake_from_mtime(tid, headers_, first_seen=None, mtime=T0 + 900):
    return P.intake_at(tid, headers_, first_seen) or mtime


def slug_without_anchor_is_unknown(intake_fn):
    return intake_fn("task-plain", {}) is None


def slug_with_first_seen(intake_fn):
    return intake_fn("task-plain", {}, T0 + 5) == T0 + 5


check(intake_never_later(P.intake_at), "intake: a later read or a later header never moves intake later")
check(not intake_never_later(intake_latest), "CONTROL: intake: a newest-stamp anchor would move it")
check(P.intake_at("task-chat-x", {"timestamp": "2026-10-24T10:00:00Z"}) == 1792836000.0,
      "intake: the UTC `timestamp` header is read when the id carries no epoch")
check(P.intake_at("task-chat-x", {}) is None, "intake: no stamp at all is None, never 'now'")
check(slug_without_anchor_is_unknown(P.intake_at), "intake: a slug id with no timestamp and no first-seen is unknown")
check(not slug_without_anchor_is_unknown(intake_from_mtime),
      "CONTROL: intake: an mtime fallback would report a rewritten slug task as fresh")
check(P.intake_at("task-dd3a13e1514613581a", {}) is None and P.intake_at("task-2084056842a242eec2", {}) is None,
      "intake: a digit run inside a hex id is not an epoch")
check(P._id_epoch("task-1792000000000") == 1792000000.0 and P._id_epoch("task-chat-1792000000000") == 1792000000.0
      and P._id_epoch("task-newsradar-1792000000") == 1792000000.0
      and P._id_epoch("task-1792000000000-abc123def456") == 1792000000.0
      and P._id_epoch("task-1792000000000.claimed-core-1") == 1792000000.0,
      "intake: genuine minted epoch ids still parse")
check(slug_with_first_seen(P.intake_at), "intake: a persisted first-seen anchors a slug id")
check(P.decide(obs(intake_at=None), T0 + 10_000).action == P.SUPPRESS,
      "intake: an unknown intake suppresses instead of guessing")

# --- Task-aware timers ---------------------------------------------------------------------------
d = P.DEFAULT_FIRST_ACK_DEADLINE_S
check(d["urgent"] < d["normal"] < d["low"], "timers: urgent < normal < low")
check(P.deadline_for(headers(source="voice")) == d["urgent"], "timers: a voice task gets the urgent budget")
check(P.deadline_for(headers(priority="low")) == d["low"], "timers: an explicit priority header wins")
check(P.deadline_for(headers(source="discord", access_tier="team")) == d["low"],
      "timers: a non-owner channel task gets the low budget")
check(P.deadline_for(headers(), {"normal": 45}) == 45, "timers: a configured deadline overrides the default")


def not_before_deadline(decide):
    return decide(obs(), T0 + NORMAL - 0.001).action == P.WAIT


def by_deadline_plus_one_poll(decide, poll=15.0):
    # A runner polling every `poll` s from any phase offset speaks within [deadline, deadline + poll).
    for offset in (0.0, 3.7, 14.9):
        t = T0 + offset
        while decide(obs(), t).action != P.SPEAK:
            t += poll
            if t > T0 + 10 * NORMAL:
                return False
        if not (T0 + NORMAL <= t < T0 + NORMAL + poll):
            return False
    return True


early = broken_with(intake_at=lambda o: o.intake_at - NORMAL)
late = broken_with(intake_at=lambda o: o.intake_at + 60)
holds("timers: no ack before the deadline", not_before_deadline, early)
holds("timers: the ack is due by deadline + one poll interval", by_deadline_plus_one_poll, late)
check(P.decide(obs(), T0 + 1).due_at == T0 + NORMAL, "timers: a WAIT names when it becomes due")

# --- Scope: unpicked tasks only; a card or progress notice already speaks -------------------------
def picked_up_or_shown_is_silent(decide):
    cases = [obs(phase="RUNNING", started_at=T0 + 1, last_activity_at=T0 + 900), obs(worker_holder="worker-2"),
             obs(card=True), obs(phase="QUEUED", card=True), obs(progress_notice=True)]
    return all(decide(o, T0 + 10 * NORMAL).action == P.SUPPRESS for o in cases)


def speaks_regardless_of_pickup(o, now, deadlines=None):
    return P.decide(dataclasses.replace(o, phase=None, worker_holder=None, card=False, progress_notice=False),
                    now, deadlines)


holds("scope: a picked-up task, or one with a card or progress notice, gets no line",
      picked_up_or_shown_is_silent, speaks_regardless_of_pickup)
check(P.decide(obs(), T0 + NORMAL).action == P.SPEAK, "scope: an unpicked task with no card still gets its line")
check("handed_off" not in P.TEMPLATES, "scope: no template reports a picked-up task")

# --- Thread routing: the task's own verified room and thread, never a fallback -------------------
def in_thread_keeps_root(decide):
    r = decide(obs(headers=headers(thread_root=ROOT)), T0 + NORMAL)
    return r.destination == P.Destination(ROOM, ROOT, MSG)


def top_level_rooted_at_source(decide):
    r = decide(obs(), T0 + NORMAL)
    return r.destination == P.Destination(ROOM, MSG, MSG)


def bad_root_no_fallback(decide):
    outs = [decide(obs(headers=headers(thread_root=v)), T0 + NORMAL) for v in ("not-an-event", "  ")]
    return all(r.action == P.SUPPRESS and r.destination is None for r in outs)


def fallback_destination(h):
    try:
        return P.Destination(h["channel_id"], h["source_message_id"], h["source_message_id"]), ""
    except KeyError:
        return None, "missing"


def unthreaded_top_level(h):
    dest, why = REAL_DESTINATION(h)
    if dest is not None and "thread_root" not in h:
        dest = P.Destination(dest.room, "", dest.reply_to)
    return dest, why


REAL_DESTINATION = P.destination


def with_destination(fn):
    def run(o, now, deadlines=None):
        P.destination = fn
        try:
            return P.decide(o, now, deadlines)
        finally:
            P.destination = REAL_DESTINATION
    return run


with_fallback = with_destination(fallback_destination)


def unverifiable_destinations(decide):
    cases = [headers(channel_id=None), headers(channel_id="not-a-room"),
             headers(source_room_id="!other:ag2.space"), headers(source_message_id=None),
             headers(source_message_id="1234567890")]
    return all(decide(obs(headers=h), T0 + NORMAL).action == P.SUPPRESS for h in cases)


holds("routing: an in-thread request keeps its own thread root", in_thread_keeps_root,
      broken_with(headers=lambda o: {k: v for k, v in o.headers.items() if k != "thread_root"}))
holds("routing: a top-level request is rooted at its source message", top_level_rooted_at_source,
      with_destination(unthreaded_top_level))
holds("routing: an unverifiable thread root sends nothing, with no top-level fallback",
      bad_root_no_fallback, with_fallback)
holds("routing: no room, a malformed room, disagreeing rooms or no source message send nothing",
      unverifiable_destinations, with_fallback)
check(P.destination(headers(channel_id=None, source_room_id=ROOM))[0] == P.Destination(ROOM, MSG, MSG),
      "routing: source_room_id alone is the task's room")

# --- Suppression ---------------------------------------------------------------------------------
SUPPRESSING = [
    ("an answer", dict(result="answered")), ("a no-send result", dict(result="no-send")),
    ("a result mid-delivery", dict(result="sending")), ("an archived task", dict(result="archived")),
    ("COMPLETED", dict(phase="COMPLETED")), ("FAILED", dict(phase="FAILED")),
    ("CANCELLED", dict(phase="CANCELLED")), ("WAITING on a person", dict(phase="WAITING")),
    ("a cancel instruction", dict(cancel_instruction=True)), ("a cancel request", dict(cancel_requested=True)),
    ("an unrecognised result", dict(result="weird")), ("an unrecognised phase", dict(phase="LIMBO")),
    ("an unrecognised ack state", dict(ack="maybe")),
]


def ignores_outcome(o, now, deadlines=None):
    return P.decide(dataclasses.replace(o, result="none", phase=None, cancel_instruction=False,
                                        cancel_requested=False, ack="none"), now, deadlines)


for label, kw in SUPPRESSING:
    holds(f"suppression: {label} is never acked",
          lambda dec, kw=kw: dec(obs(**kw), T0 + 10 * NORMAL).action == P.SUPPRESS, ignores_outcome)


def result_lands_after_wait(decide):
    # Decided WAIT, then the answer lands before the deadline: the next decision is SUPPRESS.
    first = decide(obs(), T0 + 1)
    second = decide(obs(result="answered"), T0 + NORMAL)
    return first.action == P.WAIT and second.action == P.SUPPRESS


holds("suppression: an answer landing after a WAIT suppresses the ack", result_lands_after_wait, ignores_outcome)

# --- Initial ack only, at most once --------------------------------------------------------------
check(P.ack_key("task-9") == "ack:task-9:initial", "ack key is ack:<task>:initial")


def once_then_silent(decide):
    # Simulate a runner: speak, record the claim, then poll for an hour. Exactly one speak.
    ack, spoken, t = "none", 0, T0
    while t < T0 + 3600:
        if decide(obs(ack=ack), t).action == P.SPEAK:
            spoken += 1
            ack = "claimed"
        t += 15
    return spoken == 1


def no_five_minute_timer(decide):
    return all(decide(obs(ack=a), T0 + k * 300).action == P.SUPPRESS
               for a in ("claimed", "confirmed", "unknown") for k in range(1, 13))


holds("initial only: one ack per task over an hour of polling", once_then_silent, ignores_outcome)
holds("initial only: a claimed, confirmed or parked ack never repeats (no 5-minute timer)",
      no_five_minute_timer, ignores_outcome)

# --- Factual text only, from a fixed template set ------------------------------------------------
shapes = [obs(), obs(phase="QUEUED"), obs(phase=None, inbox="standby_only"),
          obs(phase=None, inbox="standby_only", owner_alerted=True), obs(phase=None, inbox="unknown"),
          obs(phase="RECEIVED", inbox="empty")]
texts = [P.decide(o, T0 + NORMAL) for o in shapes]
check(all(r.action == P.SPEAK and r.text == P.TEMPLATES[r.template] for r in texts),
      "text: every spoken line is one of the fixed templates, unmodified")
check({r.template for r in texts} == set(P.TEMPLATES), "text: every template is reachable")
check(not any(ch.isdigit() for t in P.TEMPLATES.values() for ch in t)
      and not any(w in t.lower() for t in P.TEMPLATES.values() for w in ("expected", "eta", "minute", "soon")),
      "text: no template carries an estimate, a time or a promise")

# --- Watcher down ---------------------------------------------------------------------------------
r = P.decide(obs(phase=None, inbox="standby_only"), T0 + NORMAL)
check(r.watcher_down and r.template == "inbox_down", "watcher down: only a standby holds the inbox -> says so")
r = P.decide(obs(phase="RUNNING", inbox="standby_only"), T0 + NORMAL)
check(r.action == P.SUPPRESS and not r.watcher_down, "watcher down: a task already claimed gets no room line")
check(P.inbox_state(True, ["standby"]) == "standby_only", "inbox: a tagged standby alone is standby_only")
check(P.inbox_state(True, ["untagged"]) == "standby_only", "inbox: the untagged external standby counts too")
check(P.inbox_state(True, ["standby", "session"]) == "session", "inbox: any session holder means session")
check(P.inbox_state(True, []) == "empty", "inbox: no holder at all is empty")
check(P.inbox_state(False, []) == "unknown", "inbox: an unread process table is unknown, never empty")
check(P.inbox_state(True, ["standby"], undecided=1) == "unknown",
      "inbox: an undecided line could be the session watcher, so it is unknown")

print(f"\nstatus-ack-policy: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
