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
                               "!room:hs", log=lambda *_: None)
        n1 = poster.sweep()
        n2 = poster.sweep()
    finally:
        ha.urllib.request.urlopen = orig
    check(n1 == 1 and n2 == 0, "poster: one card per action, never re-posted")
    check(calls[0]["op"] == "message" and calls[0]["room_id"] == "!room:hs"
          and "Ship v1" in calls[0]["text"],
          "poster: card carries the options via the gateway message op")
    got = json.loads(Path(store.dir, rec["action_id"] + ".json").read_text())
    check(got["card_event_id"] == "$newcard",
          "poster: card_event_id recorded (correlation anchor)")
    check(got["audit"][-1]["event"] == "card_posted", "poster: post is audited")


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
