"""Tests for human_action (bridge v1 steps 2+3) — CardPoster + DecisionHandler
+ HandlerChain. Covers: owner-only resolution, reaction/reply/answer-command
decision forms, terminal-state immutability, one-card-per-action, chain routing
(decision events never become taskify material). Self-contained; exit 0/1."""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import human_action as ha                     # noqa: E402
from ag2_sparrow.event_consumer import EventConsumer, TaskifyHandler  # noqa: E402
from ag2_sparrow.event_inbox import EventInbox                 # noqa: E402

FAILS: list = []
OWNER = "@qingyun:ag2.space"


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _store_with_action(card_event_id="$card1"):
    d = tempfile.mkdtemp()
    store = ha.ActionStore(d)
    rec = {
        "action_id": "ha_abc123def456", "kind": "clarification",
        "status": "pending", "card_event_id": card_event_id,
        "questions": [{"question": "Ship v1 or wait?", "options": [
            {"label": "Ship v1"}, {"label": "Wait"}]}],
        "decision": None, "created_at": time.time(),
        "expires_at": time.time() + 300, "audit": [],
    }
    Path(d, rec["action_id"] + ".json").write_text(json.dumps(rec))
    return store, rec


def _reaction(key, relates="$card1", actor=OWNER, eid="$r1"):
    return {"event_id": eid, "cursor": 1, "type": "reaction.added",
            "actor_id": actor,
            "content": {"m.relates_to": {"event_id": relates, "key": key}}}


def _message(body, relates=None, actor=OWNER, eid="$m1"):
    content = {"body": body}
    if relates:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": relates}}
    return {"event_id": eid, "cursor": 2, "type": "message.created",
            "actor_id": actor, "content": content}


def test_owner_reaction_resolves():
    store, rec = _store_with_action()
    h = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    settled = h.offer(_reaction("1️⃣"))
    check(settled == ["$r1"], "reaction event settles")
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "resolved"
          and got["decision"]["answers"] == {"Ship v1 or wait?": "Ship v1"},
          "owner's 1️⃣ reaction resolves to option 1")
    check(got["resolved_by"] == OWNER, "resolution records the resolver")


def test_non_owner_is_ignored():
    store, rec = _store_with_action()
    h = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    h.offer(_reaction("2️⃣", actor="@mallory:ag2.space"))
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "pending",
          "AUTHORIZATION — a non-owner reaction NEVER resolves an action")
    check(h.claims(_reaction("2️⃣", actor="@mallory:ag2.space")) is True,
          "…but the attempt IS claimed (never becomes taskify material)")


def test_no_owner_configured_is_inert():
    store, rec = _store_with_action()
    h = ha.DecisionHandler(store, None, log=lambda *_: None)
    check(h.claims(_reaction("1️⃣")) is False,
          "no owner configured → handler inert (fail-closed)")
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "pending", "…and nothing is resolved")


def test_answer_command_and_reply_forms():
    store, rec = _store_with_action()
    h = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    h.offer(_message("answer ha_abc123def456 2"))
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["decision"]["answers"] == {"Ship v1 or wait?": "Wait"},
          "`answer ha_x 2` command form resolves to option 2")

    store2, rec2 = _store_with_action()
    h2 = ha.DecisionHandler(store2, OWNER, log=lambda *_: None)
    h2.offer(_message("1", relates="$card1"))
    got2 = json.loads(Path(store2.dir, rec2["action_id"] + ".json").read_text())
    check(got2["decision"]["answers"] == {"Ship v1 or wait?": "Ship v1"},
          "bare option number replying to the card resolves")

    # Keyword-optional grammar (live-acceptance finding 2026-07-26): a real
    # owner naturally types the bare `ha_<hex> N` without `answer`.
    store3, rec3 = _store_with_action()
    h3 = ha.DecisionHandler(store3, OWNER, log=lambda *_: None)
    h3.offer(_message("ha_abc123def456 2"))
    got3 = json.loads(Path(store3.dir, rec3["action_id"] + ".json").read_text())
    check(got3["decision"]["answers"] == {"Ship v1 or wait?": "Wait"},
          "bare `ha_x 2` (no answer keyword) resolves")
    # a NON-pending id mentioned with a number stays inert (terminal immutable)
    h3.offer(_message("ha_abc123def456 1"))
    got3b = json.loads(Path(store3.dir, rec3["action_id"] + ".json").read_text())
    check(got3b["decision"]["answers"] == {"Ship v1 or wait?": "Wait"},
          "late bare-id answer cannot overwrite the resolution")

    # Intent boundary (review blocker): the BARE form must be the ENTIRE
    # trimmed message. Prose that merely QUOTES a pending token must not
    # resolve; the explicit `answer` keyword remains usable inside prose.
    store4, rec4 = _store_with_action()
    h4 = ha.DecisionHandler(store4, OWNER, log=lambda *_: None)
    h4.offer(_message("fyi the pending card ha_abc123def456 2 is still open"))
    got4 = json.loads(Path(store4.dir, rec4["action_id"] + ".json").read_text())
    check(got4.get("decision") is None and got4["status"] == "pending",
          "prose quoting `ha_x 2` does NOT resolve (bare form must fullmatch)")
    h4.offer(_message("  ha_abc123def456 2  "))
    got4b = json.loads(Path(store4.dir, rec4["action_id"] + ".json").read_text())
    check(got4b["decision"]["answers"] == {"Ship v1 or wait?": "Wait"},
          "whitespace-padded bare form still resolves (trimmed fullmatch)")
    store5, rec5 = _store_with_action()
    h5 = ha.DecisionHandler(store5, OWNER, log=lambda *_: None)
    h5.offer(_message("ok let me answer ha_abc123def456 1 then"))
    got5 = json.loads(Path(store5.dir, rec5["action_id"] + ".json").read_text())
    check(got5["decision"]["answers"] == {"Ship v1 or wait?": "Ship v1"},
          "explicit `answer ha_x N` inside prose still resolves")


def test_a2ui_button_click_resolves():
    # A2UI-CONTRACT.md: a button click arrives as a NORMAL m.room.message with a
    # human body (e.g. "▸ ack") + structured content["space.ag2.a2ui.action"].
    # The action string is our own grammar, so the click resolves like a reply.
    store, rec = _store_with_action()
    h = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    click = {"event_id": "$click", "cursor": 9, "type": "message.created",
             "actor_id": OWNER,
             "content": {"body": "▸ ack",
                         "space.ag2.a2ui.action": {
                             "name": "buttons", "component_id": "c1",
                             "value": "answer ha_abc123def456 2",
                             "in_reply_to": "$card1"}}}
    h.offer(click)
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "resolved"
          and got["decision"]["answers"] == {"Ship v1 or wait?": "Wait"},
          "a structured A2UI button click resolves via the SAME answer grammar")


def test_terminal_states_immutable():
    store, rec = _store_with_action()
    store.resolve(rec["action_id"], {"Ship v1 or wait?": "Ship v1"}, OWNER)
    ok = store.resolve(rec["action_id"], {"Ship v1 or wait?": "Wait"}, OWNER)
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(ok is False and got["decision"]["answers"]["Ship v1 or wait?"] == "Ship v1",
          "a second answer NEVER overwrites a resolution (immutable terminal state)")
    # expired action rejects late answers too
    store3, rec3 = _store_with_action()
    raw = json.loads(Path(store3.dir, rec3["action_id"] + ".json").read_text())
    raw["status"] = "expired"
    Path(store3.dir, rec3["action_id"] + ".json").write_text(json.dumps(raw))
    check(store3.resolve(rec3["action_id"], {"q": "a"}, OWNER) is False,
          "late answer on an EXPIRED action is ignored (hook already denied)")


def test_chain_routes_decisions_away_from_taskify():
    store, rec = _store_with_action()
    inbox = EventInbox(os.path.join(tempfile.mkdtemp(), "e.db"))
    inbox.insert(_reaction("1️⃣", eid="$dec"))
    for i in range(2, 5):
        inbox.insert({"event_id": f"$m{i}", "cursor": i, "type": "message.created",
                      "actor_id": "@peer:hs", "content": {"body": f"chat {i}"}})
    tdir = tempfile.mkdtemp()
    decisions = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    taskify = TaskifyHandler(tdir, agent_mxid="@me:hs", threshold=3,
                             log=lambda *_: None)
    chain = ha.HandlerChain([decisions, taskify])
    r = EventConsumer(inbox, chain).drain()
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "resolved", "chain: decision event resolves the action")
    check(len(r["promoted"]) == 1, "chain: the 3 chat events still flush a taskify batch")
    body = open(r["promoted"][0]).read()
    check("$dec" not in body,
          "chain: the decision event is NOT taskify material (claimed by decisions)")
    check(inbox.unconsumed() == [], "chain: all events settled")


def _two_q_store():
    d = tempfile.mkdtemp()
    store = ha.ActionStore(d)
    rec = {"action_id": "ha_aaa111bbb222", "kind": "clarification",
           "status": "pending", "card_event_id": "$card2",
           "questions": [
               {"question": "Q1?", "options": [{"label": "A1"}, {"label": "B1"}]},
               {"question": "Q2?", "options": [{"label": "A2"}, {"label": "B2"}]}],
           "decision": None, "created_at": time.time(),
           "expires_at": time.time() + 300, "audit": []}
    Path(d, rec["action_id"] + ".json").write_text(json.dumps(rec))
    return store, rec


def test_multi_question_partial_never_resolves():
    # Review blocker: a single reaction/bare number must not terminally resolve
    # a multi-question action with only Q1 answered.
    store, rec = _two_q_store()
    h = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    settled = h.offer(_reaction("1️⃣", relates="$card2", eid="$pr"))
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "pending" and settled == ["$pr"],
          "multi-q: a reaction NEVER partially resolves (claimed, action stays pending)")
    h.offer(_message("answer ha_aaa111bbb222 1", eid="$pm"))
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "pending",
          "multi-q: an under-count answer vector does not resolve")
    h.offer(_message("answer ha_aaa111bbb222 9,1", eid="$pi"))
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "pending",
          "multi-q: an out-of-range option never resolves")
    h.offer(_message("answer ha_aaa111bbb222 2,1", eid="$pf"))
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["status"] == "resolved"
          and got["decision"]["answers"] == {"Q1?": "B1", "Q2?": "A2"},
          "multi-q: the FULL ordered vector resolves every question")


def test_multiselect_single_question_joins_labels():
    d = tempfile.mkdtemp()
    store = ha.ActionStore(d)
    rec = {"action_id": "ha_ccc333ddd444", "kind": "clarification",
           "status": "pending", "card_event_id": "$card3",
           "questions": [{"question": "Pick several", "multiSelect": True,
                          "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}],
           "decision": None, "created_at": time.time(),
           "expires_at": time.time() + 300, "audit": []}
    Path(d, rec["action_id"] + ".json").write_text(json.dumps(rec))
    h = ha.DecisionHandler(store, OWNER, log=lambda *_: None)
    h.offer(_message("answer ha_ccc333ddd444 1,3"))
    got = json.loads(Path(d, rec["action_id"] + ".json").read_text())
    check(got["status"] == "resolved"
          and got["decision"]["answers"] == {"Pick several": "A, C"},
          "multiSelect single-q: every selected option is preserved (review fix)")


def test_update_is_durable_and_uniquely_named():
    # Review blocker: decision writes must be fsync-durable (file + dir) with a
    # unique temp name (the hook's timeout writer may run concurrently).
    store, rec = _store_with_action()
    fsynced, replaced = [], []
    orig_fsync, orig_replace = os.fsync, os.replace
    os.fsync = lambda fd: fsynced.append(fd)
    os.replace = lambda a, b: (replaced.append(a), orig_replace(a, b))[1]
    try:
        store.resolve(rec["action_id"], {"Ship v1 or wait?": "Ship v1"}, OWNER)
    finally:
        os.fsync, os.replace = orig_fsync, orig_replace
    check(len(fsynced) >= 2,
          "store: decision write fsyncs the file AND the directory entry")
    check(replaced and str(os.getpid()) in replaced[0] and not replaced[0].endswith("json.tmp"),
          "store: temp name is unique per writer (no fixed .tmp collision)")


def test_card_poster_posts_once():
    store, rec = _store_with_action(card_event_id=None)
    calls = []

    def fake_open(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        return io.BytesIO(json.dumps({"event_id": "$newcard"}).encode())

    orig = ha.urllib.request.urlopen
    ha.urllib.request.urlopen = fake_open
    try:
        poster = ha.CardPoster(store, "https://gw", {"Authorization": "Bearer x"},
                               "!room:hs", log=lambda *_: None, include_a2ui=True)
        n1 = poster.sweep()
        n2 = poster.sweep()
    finally:
        ha.urllib.request.urlopen = orig
    check(n1 == 1 and n2 == 0, "poster: one card per action, never re-posted")
    check(calls[0]["op"] == "message" and calls[0]["room_id"] == "!room:hs"
          and "Ship v1" in calls[0]["body"],
          "poster: card carries the options via the gateway message op")
    # A2UI contract: one fenced ```a2ui block; options carry OUR decision
    # grammar as the action, so a button click round-trips through the same
    # regex as a typed reply.
    a2ui_json = calls[0]["body"].split("```a2ui")[1].split("```")[0]
    card_obj = json.loads(a2ui_json)
    check(card_obj["type"] == "buttons"
          and card_obj["options"][0]["action"] == "answer ha_abc123def456 1",
          "poster: fenced a2ui block present; option actions use the answer grammar")
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["card_event_id"] == "$newcard",
          "poster: card_event_id recorded (correlation anchor)")
    check(got["audit"][-1]["event"] == "card_posted", "poster: post is audited")


def test_card_poster_default_omits_a2ui_and_sends_ua():
    # Deployed client shows an unclickable "Room App" for a2ui content and
    # hides the fallback text — so the block is OPT-IN (default off), and the
    # request must carry the gateway client UA (Cloudflare 403s urllib's
    # default — review blocker).
    store, rec = _store_with_action(card_event_id=None)
    seen = {}

    def fake_open(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())["body"]
        seen["ua"] = req.headers.get("User-agent")
        return io.BytesIO(json.dumps({"event_id": "$c"}).encode())

    orig = ha.urllib.request.urlopen
    ha.urllib.request.urlopen = fake_open
    try:
        ha.CardPoster(store, "https://gw", {"Authorization": "Bearer x"},
                      "!room:hs", log=lambda *_: None).sweep()
    finally:
        ha.urllib.request.urlopen = orig
    check("```a2ui" not in seen["body"] and "Ship v1" in seen["body"],
          "poster default: plain text card, NO a2ui block (client can't render it yet)")
    check(seen["ua"] == "sutando-gateway-client/1.0",
          "poster: explicit gateway UA header present (review fix)")


def test_card_poster_never_resurrects_terminal_action():
    # Review P1 (verified twice): sweep() read a pending rec, did the blocking
    # POST, then wrote the STALE rec back — resurrecting an action that
    # resolved/expired mid-POST. The stamp now happens on the re-read CURRENT
    # record under the shared transition lock, and only while still pending.
    store, rec = _store_with_action(card_event_id=None)

    def expire_during_post(req, timeout=None):
        cur = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
        cur["status"] = "expired"
        cur.setdefault("audit", []).append({"event": "expired"})
        Path(store.dir, rec["action_id"] + ".json").write_text(json.dumps(cur))
        return io.BytesIO(json.dumps({"event_id": "$card-race"}).encode())

    orig = ha.urllib.request.urlopen
    ha.urllib.request.urlopen = expire_during_post
    try:
        n = ha.CardPoster(store, "https://gw", {}, "!room:hs",
                          log=lambda *_: None).sweep()
    finally:
        ha.urllib.request.urlopen = orig
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(n == 0 and got["status"] == "expired",
          "TERMINAL IMMUTABILITY — a mid-POST expiry is never overwritten back to pending")
    check(got.get("card_event_id") is None,
          "no stale card id is stamped onto a terminal action")


def test_card_poster_failure_retries():
    store, rec = _store_with_action(card_event_id=None)

    def boom(req, timeout=None):
        raise OSError("gateway down")

    orig = ha.urllib.request.urlopen
    ha.urllib.request.urlopen = boom
    try:
        poster = ha.CardPoster(store, "https://gw", {}, "!room:hs",
                               log=lambda *_: None)
        n = poster.sweep()
    finally:
        ha.urllib.request.urlopen = orig
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(n == 0 and not got.get("card_event_id"),
          "poster: failed post leaves the action card-less (retried next sweep)")


def test_card_instructions_match_answer_grammar():
    # Review blocker (re-check on 8bcf13a4): the card's instructions must match
    # what _complete_answers accepts for THIS action's shape — a card telling
    # the owner to react on a multi-question action is silently
    # claimed-but-pending until timeout.
    store, rec = _store_with_action(card_event_id=None)
    poster = ha.CardPoster(store, "https://gw", {}, "!room:hs",
                           log=lambda *_: None)
    single = poster._render(rec)
    check("React with the option number" in single and "<n>`" in single,
          "single-question single-select: reaction or `answer <n>`")
    ms = dict(rec, questions=[dict(rec["questions"][0], multiSelect=True)])
    ms_card = poster._render(ms)
    check("<n1>,<n2>" in ms_card and "React with the option number" not in ms_card,
          "single-question multiSelect: comma-separated form, no react-only prompt")
    multi = dict(rec, questions=[rec["questions"][0],
                                 {"question": "Q2?", "options": [{"label": "A"}]}])
    multi_card = poster._render(multi)
    check("one number per question" in multi_card
          and "React with the option number" not in multi_card,
          "multi-question: full ordered vector; reactions ruled out")
    mixed = dict(rec, questions=[dict(rec["questions"][0], multiSelect=True),
                                 {"question": "Q2?", "options": [{"label": "A"}]}])
    check("decides autonomously at timeout" in poster._render(mixed),
          "multi-question + multiSelect: honest can't-answer-by-numbers notice")
    a2 = ha.CardPoster(store, "https://gw", {}, "!room:hs",
                       log=lambda *_: None, include_a2ui=True)
    check("```a2ui" in a2._render(rec),
          "a2ui: single-question single-select still gets buttons")
    check("```a2ui" not in a2._render(multi) and "```a2ui" not in a2._render(ms),
          "a2ui: shapes one click can't resolve stay text-only (no eaten clicks)")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — human_action (CardPoster + DecisionHandler + chain)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
