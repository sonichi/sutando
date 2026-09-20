#!/usr/bin/env python3
"""Two writers move the SAME card to DIFFERENT columns.

Different cards converge trivially and prove nothing; one card is where Yjs's
own rule and the panel's rule disagree. Yjs resolves two writers of one key by
CLIENT ID, which knows nothing about `updated` — so the peer with the higher
client id can win while holding the OLDER move, and the card lands in the wrong
column for everyone.

The question this answers is not "does it converge" but WHO HAS TO RE-ASSERT.
An agent that writes once and walks away is the realistic case, and if only the
panel reconciles then that agent's newer move is silently lost. Both cases are
below, with the client ids forced so the race is decided rather than lucky.

Pure: two documents synced by hand, no server and no panel.
Run: python3 tests/room-doc-kanban-convergence.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

try:
    from pycrdt import Doc, Map
except ImportError as exc:  # pragma: no cover
    print(f"room-doc kanban convergence: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_kanban import (CARDS_KEY, changed, delete_card, in_column,  # noqa: E402
                         is_card, is_newer, live_cards)

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def card(column, updated, by, ident="c1"):
    return {"id": ident, "column": column, "order": 0, "text": "the card",
            "assignee": None, "updated": updated, "by": by}


def sync(a: Doc, b: Doc) -> None:
    """Exchange updates both ways until both documents have everything."""
    for _ in range(3):
        b.apply_update(a.get_update(b.get_state()))
        a.apply_update(b.get_update(a.get_state()))


def cards_of(doc: Doc) -> dict:
    return {k: dict(v) for k, v in doc.get(CARDS_KEY, type=Map).items()}


def write(doc: Doc, value: dict) -> None:
    doc.get(CARDS_KEY, type=Map)[value["id"]] = dict(value)


def reassert(doc: Doc, mine: dict) -> None:
    """Re-apply what this writer knows, the way the panel does after a remote
    change: only if it is still newer than what the merge kept."""
    stored = cards_of(doc).get(mine["id"])
    if is_newer(mine, stored if is_card(stored, mine["id"]) else None):
        write(doc, mine)


# Fixed client ids so the Yjs race is decided, not lucky: LOW is the panel,
# HIGH is the agent, and HIGH wins any client-id tiebreak.
LOW, HIGH = 1, 10_000_000


def test_the_rule_itself_prefers_the_later_move():
    assert is_newer(card("doing", 200, "@a"), card("todo", 100, "@b"))
    assert not is_newer(card("todo", 100, "@b"), card("doing", 200, "@a"))


def test_an_exact_tie_breaks_on_author_identically_everywhere():
    a, b = card("doing", 100, "@zoe"), card("todo", 100, "@amy")
    assert is_newer(a, b) and not is_newer(b, a), "the tie-break must be total"


def _no_reconcile_round(agent_id, panel_id):
    """The realistic agent: write once, walk away. Nobody re-asserts."""
    panel, agent = Doc(client_id=panel_id), Doc(client_id=agent_id)
    write(panel, card("todo", 100, "@panel"))
    sync(panel, agent)
    write(panel, card("todo", 150, "@panel"))      # older move
    write(agent, card("doing", 200, "@agent"))     # NEWER move
    sync(panel, agent)
    kept = cards_of(panel)["c1"]
    assert cards_of(agent)["c1"] == kept, "the two sides disagree"
    return kept["column"]


def test_without_re_assertion_the_winner_is_decided_by_client_id_not_by_time():
    """The answer to phase 1, and it is not "it works".

    Run both ways round: the agent's move is NEWER in both, and it survives only
    when the agent happens to hold the higher client id. One round alone would
    have reported whichever luck it drew as the behaviour.
    """
    agent_high = _no_reconcile_round(agent_id=HIGH, panel_id=LOW)
    agent_low = _no_reconcile_round(agent_id=LOW, panel_id=HIGH)
    globals()["_NO_RECONCILE"] = (agent_high, agent_low)
    assert agent_high != agent_low, (
        "the two rounds agreed, so client id is not deciding and this test "
        "no longer measures what it claims")
    assert agent_low == "todo", "the newer move lost purely on client id"


def test_when_both_sides_re_assert_the_later_move_wins():
    """With the panel's rule applied on both sides, client id stops mattering."""
    panel, agent = Doc(client_id=LOW), Doc(client_id=HIGH)
    write(panel, card("todo", 100, "@panel"))
    sync(panel, agent)

    panel_move = card("todo", 150, "@panel")
    agent_move = card("doing", 200, "@agent")
    write(panel, panel_move)
    write(agent, agent_move)
    sync(panel, agent)

    reassert(panel, panel_move)
    reassert(agent, agent_move)
    sync(panel, agent)

    assert cards_of(panel)["c1"]["column"] == "doing", cards_of(panel)["c1"]
    assert cards_of(agent)["c1"]["column"] == "doing", cards_of(agent)["c1"]


def test_the_loser_sees_the_winners_column_not_its_own():
    """Convergence means the loser MOVES, not that each side keeps its own."""
    panel, agent = Doc(client_id=HIGH), Doc(client_id=LOW)   # ids swapped
    write(panel, card("todo", 100, "@panel"))
    sync(panel, agent)
    panel_move, agent_move = card("review", 300, "@panel"), card("doing", 200, "@agent")
    write(panel, panel_move)
    write(agent, agent_move)
    sync(panel, agent)
    reassert(panel, panel_move)
    reassert(agent, agent_move)
    sync(panel, agent)
    assert cards_of(agent)["c1"]["column"] == "review", "the loser did not move"


def test_re_asserting_an_older_move_writes_nothing():
    """Re-assertion must not mean always winning, or two agents would fight."""
    panel, agent = Doc(client_id=LOW), Doc(client_id=HIGH)
    write(panel, card("doing", 300, "@panel"))
    sync(panel, agent)
    mine = card("todo", 100, "@agent")
    reassert(agent, mine)
    assert cards_of(agent)["c1"]["column"] == "doing"


def test_a_card_read_back_from_the_document_is_still_a_card():
    """pycrdt hands a stored `300` back as `300.0`. Refusing floats outright
    made is_card() reject every card that had been through the document —
    including ones this client wrote — so nothing would ever be re-asserted."""
    doc = Doc()
    write(doc, card("todo", 300, "@a"))
    stored = cards_of(doc)["c1"]
    assert isinstance(stored["updated"], float), "the premise: the CRDT floats it"
    assert is_card(stored, "c1"), f"a round-tripped card was refused: {stored}"
    assert is_newer(card("doing", 400, "@b"), stored), "and it still compares"


def test_a_non_integral_updated_is_still_refused():
    """Accepting 300.0 must not become accepting 300.5: the schema says ms."""
    assert not is_card({**card("todo", 1, "@a"), "updated": 300.5})
    assert not is_card({**card("todo", 1, "@a"), "updated": "300"})
    assert not is_card({**card("todo", 1, "@a"), "updated": True})


def test_changed_refuses_a_malformed_card_rather_than_writing_it():
    stored = {}
    assert changed([{"id": "x"}], stored.get) == [], "a card with no column is not a card"
    assert len(changed([card("todo", 1, "@a")], stored.get)) == 1


def test_a_tombstone_is_still_a_card_but_not_a_live_one():
    """The panel keeps deleted cards in the map — removing the key loses to a
    concurrent write. So an agent must read the flag, not the key's absence."""
    gone = delete_card(card("todo", 100, "@a"), 200, "@b")
    assert is_card(gone, "c1"), "a tombstone is still a well-formed card"
    assert gone["deleted"] is True and gone["updated"] == 200
    items = [("c1", gone), ("c2", card("todo", 100, "@a", ident="c2"))]
    assert [c["id"] for c in live_cards(items)] == ["c2"]
    assert [c["id"] for c in in_column(items, "todo")] == ["c2"], \
        "a deleted card must not be offered back as work"


def test_a_junk_deleted_flag_is_not_a_card():
    assert not is_card({**card("todo", 1, "@a"), "deleted": "yes"})
    assert not is_card({**card("todo", 1, "@a"), "deleted": 1})
    assert is_card({**card("todo", 1, "@a"), "deleted": False})


def test_deleting_wins_over_an_older_concurrent_edit():
    """The reason it is a write: it has to be able to win a race."""
    edit = card("doing", 150, "@someone")
    gone = delete_card(card("todo", 100, "@a"), 200, "@b")
    assert is_newer(gone, edit), "a later deletion must beat an earlier move"
    later_edit = card("doing", 300, "@someone")
    assert is_newer(later_edit, gone), "and a later move must beat the deletion"


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc kanban convergence: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc kanban convergence: ok")
_h, _l = globals().get("_NO_RECONCILE", ("?", "?"))
print("  finding — agent's move is NEWER in both rounds, and with NO re-assertion:")
print(f"    agent holds the higher client id -> kept {_h!r}  (agent won)")
print(f"    agent holds the lower  client id -> kept {_l!r}  (agent LOST its newer move)")
print("  so an agent that does not re-assert converges by luck, not by the rule.")
