#!/usr/bin/env python3
"""Agent panel cards (src/agent_card.py): the builders, the client's checks, the sender and card replies.

- `build` returns a one-line body and a card the checks accept; a card a client would not draw raises.
- `problem` matches the web client: unknown fields allowed, required keys, http(s) links without
  credentials (avatar links mxc:// only), the client's size limits, and where each kind may sit: a menu
  only in row actions, `connect` in buttons and row actions, `connect_picker` in buttons and the footer.
- The status pill is `header.status.label` (40 characters at most); a done card carries no shuffle, and
  `shape` is never sent for an mxid or seed avatar.
- `send_card` posts through the injected Action call (`room.message.send` with `extra_content`) and returns
  the event id; a private card goes only to the owner's checked DM. `update_card` posts a new card as a
  reply to the old one, since no Action carries a card in an edit.
- A reply button sends a readable sentence and keeps its `card:<kind>:<target>:<verb>[:confirmed]` command in
  this agent's record of the card (issued_cards), with the room, the connection and the handler script.
- `click_verdict` is the one decision on a task: `command` or `owner_words` for the click of the owner of
  that connection, in that room and their DM, in reply to that card's event, with a text it offered;
  `forged_or_stale_click` for every other click, for a click header that is present but unusable (blank,
  whitespace, wrong type, missing key, bad JSON, oversize) and for a card command's text sent without a
  click, which nothing acts on (the handlers print one fixed refusal). Without a click, the owner's other
  words in reply to their card are `owner_words`; the rest is `not_a_card`, ordinary chat.
- A newer card with the same id in the same room replaces the older record; `settle` forgets one.

Run: python3 tests/agent-card.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agent_card as ac  # noqa: E402
import issued_cards  # noqa: E402

DM, ROOM, OWNER = "!dm:s.space", "!shared:s.space", "@owner:s.space"


def a_card(**kw):
    kw.setdefault("title", "Pylon")
    kw.setdefault("body", "Pylon needs you.")
    return ac.build("worker-approval:w1:s.space", kw.pop("state", "pending"), **kw)


class FakeActions:
    def __init__(self, answer=None):
        self.calls, self.answer = [], answer

    def execute(self, action, arguments, *, operation_id="", room_id=""):
        self.calls.append((action, arguments, operation_id, room_id))
        return self.answer if self.answer is not None else {"result": {"event_id": f"$ev{len(self.calls)}"}}


class Build(unittest.TestCase):
    def test_a_built_card_carries_the_contract_keys(self):
        body, card = a_card(subtitle="Worker", avatar_=ac.avatar(seed="pylon"), text="Approve it.",
                            buttons=[ac.button("Approve →", "url", style="primary", url="https://s.space/x")],
                            expires_at_ms=1794445920000, note="Works for 15 minutes.", note_tone="dim")
        self.assertEqual(body, "Pylon needs you.")
        self.assertEqual((card["kind"], card["v"], card["state"]), ("agent_panel", 1, "pending"))
        self.assertEqual(card["header"], {"title": "Pylon", "subtitle": "Worker", "avatar": {"seed": "pylon"}})
        self.assertEqual(card["buttons"][0]["kind"], "url")
        self.assertIsNone(ac.problem(card))

    def test_the_status_pill_is_a_short_label_and_a_done_card_has_no_shuffle(self):
        _, card = a_card(state="done", status="Approved", shuffle_reply="Try another avatar")
        self.assertEqual(card["header"], {"title": "Pylon", "status": {"label": "Approved"}})
        _, card = a_card(shuffle_reply="Try another avatar")
        self.assertEqual(card["header"]["shuffle"], {"reply": "Try another avatar"})
        self.assertNotIn("status", card["header"])
        with self.assertRaises(ac.CardError):
            a_card(status="x" * 41)

    def test_shape_is_sent_only_for_avatars_the_card_may_shape(self):
        self.assertEqual(ac.avatar(mxid="@a:s.space", shape="square"), {"mxid": "@a:s.space"})
        self.assertEqual(ac.avatar(seed="pylon", shape="square"), {"seed": "pylon"})
        for key in ("url", "initial", "app"):
            value = {"url": "mxc://s.space/abc", "initial": "P", "app": "gmail"}[key]
            self.assertEqual(ac.avatar(**{key: value}, shape="round"), {key: value, "shape": "round"})
        with self.assertRaises(ac.CardError):
            ac.avatar(shape="round")

    def test_connect_and_picker_buttons_carry_their_contract_keys(self):
        self.assertEqual(ac.button("Reconnect Gmail", "connect", toolkit="gmail", for_=OWNER),
                         {"label": "Reconnect Gmail", "kind": "connect", "toolkit": "gmail", "for": OWNER})
        self.assertEqual(ac.button("+ Connect an app", "connect_picker", suggest=["notion", "linear", "notion"]),
                         {"label": "+ Connect an app", "kind": "connect_picker", "suggest": ["notion", "linear"]})
        self.assertEqual(ac.button("+ Connect an app", "connect_picker"),
                         {"label": "+ Connect an app", "kind": "connect_picker"})

    def test_a_card_a_client_would_not_draw_is_refused_at_build(self):
        with self.assertRaises(ac.CardError):
            a_card(buttons=[ac.button("Open", "url", url="javascript:alert(1)")])
        with self.assertRaises(ac.CardError):
            a_card(state="maybe")
        with self.assertRaises(ac.CardError):
            a_card(body="two\nlines")

    def test_the_fallback_body_labels_a_link_and_drops_bare_urls(self):
        line = ac.body_line("Approve it at https://s.space/api/connect-page?code=U-1 now", link_label="Approve",
                            url="https://s.space/api/connect-page?code=U-1")
        self.assertEqual(line, "Approve it at now [Approve](https://s.space/api/connect-page?code=U-1)")

    def test_a_hostile_name_in_the_fallback_body_stays_literal(self):
        evil = "x](http://evil) [y*_`\\"
        line = ac.body_line(f"Approve agent {evil} for {evil}:", link_label=f"Approve {evil}",
                            url="https://s.space/api/connect-page?code=U-1")
        self.assertTrue(line.endswith("](https://s.space/api/connect-page?code=U-1)"))
        links = re.findall(r"\[((?:\\.|[^\]\\])*)\]\(([^)\s]*)\)", line)
        self.assertEqual([t for _, t in links], ["https://s.space/api/connect-page?code=U-1"])
        self.assertNotIn("code=", line[:line.rindex("](")])
        self.assertEqual(ac.md_escape("a]b\nc"), "a\\]b c")

    def test_a_reply_payload_round_trips(self):
        text = ac.reply("connections", "s_dspace", "disconnect", confirmed=True)
        self.assertEqual(text, "card:connections:s_dspace:disconnect:confirmed")
        self.assertEqual(ac.parse_reply(text), ac.Reply("connections", "s_dspace", "disconnect", True))
        self.assertEqual(ac.parse_reply(ac.reply("connections", "", "reconnect")).target, "")
        for bad in ("card:a:b", "card:a:b:c:yes", "card:a b:c:d", "hello", "card:a:b:c d"):
            self.assertIsNone(ac.parse_reply(bad), bad)
        with self.assertRaises(ac.CardError):
            ac.reply("a:b", "c", "d")


class Check(unittest.TestCase):
    def setUp(self):
        self.card = a_card()[1]

    def test_unknown_fields_are_allowed(self):
        self.assertIsNone(ac.problem({**self.card, "sparkle": True, "header": {**self.card["header"], "x": 1}}))

    def test_required_keys(self):
        for key in ("kind", "v", "id", "header"):
            broken = {k: v for k, v in self.card.items() if k != key}
            self.assertIsNotNone(ac.problem(broken), key)
        self.assertIsNotNone(ac.problem({**self.card, "header": {"subtitle": "no title"}}))
        self.assertIsNotNone(ac.problem({**self.card, "v": 2}))

    def test_only_http_links_without_credentials(self):
        for url in ("ftp://x", "file:///etc/passwd", "javascript:x", "//s.space", "https://u:p@s.space/x",
                    "https://u@s.space", "https://s.space/a b"):
            self.assertIsNotNone(ac.problem({**self.card, "buttons": [{"label": "x", "kind": "url", "url": url}]}))
            self.assertIsNotNone(ac.problem({**self.card, "header": {"title": "t", "avatar": {"url": url}}}))
        self.assertIsNone(ac.problem({**self.card, "buttons": [{"label": "x", "kind": "url", "url": "http://a.b"}]}))

    def test_an_avatar_link_is_mxc_only_and_an_app_is_a_slug(self):
        for url, ok in (("mxc://s.space/abc", True), ("https://a.b/x.png", False), ("http://a.b/x.png", False),
                        ("mxc:///abc", False)):
            self.assertEqual(ac.problem({**self.card, "header": {"title": "t", "avatar": {"url": url}}}) is None, ok)
        for app, ok in (("gmail", True), ("google_calendar", True), ("Gmail", False), ("g-mail", False)):
            self.assertEqual(ac.problem({**self.card, "header": {"title": "t", "avatar": {"app": app}}}) is None, ok)

    def test_state_and_note_tone_may_be_left_out(self):
        self.assertIsNone(ac.problem({k: v for k, v in self.card.items() if k != "state"}))

    def test_actions_need_their_target(self):
        for b in ({"label": "x", "kind": "reply"}, {"label": "x", "kind": "open_dm"},
                  {"label": "x", "kind": "open_room"}, {"kind": "reply", "reply": "r"},
                  {"label": "x", "kind": "teleport"}, {"label": "x", "kind": "reply", "reply": "r", "disabled": "no"}):
            self.assertIsNotNone(ac.problem({**self.card, "buttons": [b]}), b)

    def test_connect_sits_in_buttons_and_row_actions_only(self):
        good = ac.button("Reconnect Gmail", "connect", toolkit="gmail")
        rows = lambda a: [{"label": "g", "rows": [{"title": "Gmail", "actions": [a]}]}]
        menu = lambda a: {"label": "⋯", "kind": "menu", "items": [a]}
        for ok in ({**self.card, "buttons": [good]}, {**self.card, "groups": rows(good)},
                   {**self.card, "buttons": [{**good, "for": OWNER, "confirm": "ignored"}]}):
            self.assertIsNone(ac.problem(ok))
        self.assertIn("connect belongs only in buttons and row actions",
                      ac.problem({**self.card, "groups": rows(menu(good))}))
        self.assertIn("connect belongs only", ac.problem({**self.card, "footer_link": good}))
        for toolkit in (None, "", "Gmail", "g-mail", "x" * 65, 7):
            b = {"label": "Reconnect", "kind": "connect", **({"toolkit": toolkit} if toolkit is not None else {})}
            self.assertIn("toolkit is not an app slug", ac.problem({**self.card, "buttons": [b]}), toolkit)
        for who in ("owner", "@owner", 7, "@" + "o" * 255 + ":s.space"):
            self.assertIn("for is not a user id", ac.problem({**self.card, "buttons": [{**good, "for": who}]}))

    def test_a_picker_sits_in_buttons_and_the_footer_only(self):
        good = ac.button("+ Connect an app", "connect_picker", suggest=["notion", "linear"])
        bare = {"label": "+ Connect an app", "kind": "connect_picker"}
        rows = lambda a: [{"label": "g", "rows": [{"title": "t", "actions": [a]}]}]
        for ok in ({**self.card, "buttons": [good]}, {**self.card, "footer_link": good},
                   {**self.card, "buttons": [bare]}, {**self.card, "buttons": [{**good, "for": OWNER}]},
                   {**self.card, "buttons": [{**bare, "suggest": ["a"] * 8}]}):
            self.assertIsNone(ac.problem(ok))
        self.assertIn("connect_picker belongs only in buttons and the footer link",
                      ac.problem({**self.card, "groups": rows(good)}))
        self.assertIn("connect_picker belongs only", ac.problem(
            {**self.card, "groups": rows({"label": "⋯", "kind": "menu", "items": [good]})}))
        for suggest in ("notion", ["Notion"], [7], ["a"] * 9):
            self.assertIn("suggest must be at most 8 app slugs",
                          ac.problem({**self.card, "buttons": [{**bare, "suggest": suggest}]}), suggest)
        self.assertIn("for is not a user id", ac.problem({**self.card, "footer_link": {**bare, "for": "me"}}))

    def test_neither_connect_kind_is_a_reply_this_agent_must_remember(self):
        card = {**self.card, "buttons": [ac.button("Reconnect Gmail", "connect", toolkit="gmail"),
                                         ac.button("+ Connect an app", "connect_picker")]}
        self.assertEqual(issued_cards.offered(card), {})

    def test_a_menu_is_for_row_actions_only_and_never_nests(self):
        menu = {"label": "⋯", "kind": "menu", "items": [{"label": "y", "kind": "reply", "reply": "r"}]}
        self.assertIsNotNone(ac.problem({**self.card, "buttons": [menu]}))
        self.assertIsNotNone(ac.problem({**self.card, "footer_link": menu}))
        rows = lambda m: [{"label": "g", "rows": [{"title": "t", "actions": [m]}]}]
        self.assertIsNone(ac.problem({**self.card, "groups": rows(menu)}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": rows({**menu, "items": [menu]})}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": rows({**menu, "items": []})}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": rows({**menu, "items": menu["items"] * 9})}))

    def test_the_input_name_and_pattern(self):
        good = {"name": "agent_id", "value": "pylon", "pattern": "^[a-z]+$"}
        self.assertIsNone(ac.problem({**self.card, "input": good}))
        for bad in ({**good, "name": "agent-id"}, {**good, "name": "1x"}, {**good, "pattern": "(["},
                    {**good, "value": "x" * 201}, {**good, "pattern": "a" * 301}):
            self.assertIsNotNone(ac.problem({**self.card, "input": bad}), bad)

    def test_the_client_limits(self):
        c = self.card
        reply = {"label": "x", "kind": "reply", "reply": "r"}
        row = {"title": "t"}
        over = [{**c, "id": "x" * 201}, {**c, "header": {"title": "x" * 121}},
                {**c, "header": {"title": "t", "subtitle": "x" * 301}}, {**c, "text": "x" * 4001},
                {**c, "header": {"title": "t", "badges": [{"label": "b"}] * 9}},
                {**c, "fields": [{"label": "f", "value": "v"}] * 13}, {**c, "buttons": [reply] * 7},
                {**c, "groups": [{"label": "g", "rows": [row]}] * 11},
                {**c, "groups": [{"label": "g", "rows": [row] * 51}] * 2},
                {**c, "groups": [{"label": "g", "rows": [{"title": "t", "actions": [reply] * 5}]}]},
                {**c, "buttons": [{**reply, "reply": "x" * 1001}]}]
        for card in over:
            self.assertIsNotNone(ac.problem(card))
        at = [{**c, "id": "x" * 200}, {**c, "header": {"title": "x" * 120}}, {**c, "text": "x" * 4000},
              {**c, "buttons": [reply] * 6}, {**c, "groups": [{"label": "g", "rows": [row] * 50}] * 2},
              {**c, "buttons": [{**reply, "reply": "x" * 1000}]}]
        for card in at:
            self.assertIsNone(ac.problem(card))

    def test_the_client_s_text_bounds(self):
        c, h = self.card, self.card["header"]
        reply = {"label": "x", "kind": "reply", "reply": "r"}
        row = lambda **kw: {**c, "groups": [{"label": "g", "rows": [{"title": "t", **kw}]}]}
        cases = [
            ({**c, "header": {**h, "status": {"label": "x" * 41}}}, "status needs a label"),
            ({**c, "header": {**h, "status": {"label": " "}}}, "status needs a label"),
            ({**c, "header": {**h, "status": "Approved"}}, "status needs a label"),
            ({**c, "header": {**h, "subtitle": ""}}, "header.subtitle must be"),
            ({**c, "header": {**h, "badges": [{"label": "x" * 41}]}}, "badges[0] needs a label"),
            ({**c, "header": {**h, "avatar": {"mxid": "pylon"}}}, "mxid is not a user id"),
            ({**c, "header": {**h, "avatar": "pylon"}}, "avatar needs one of"),
            ({**c, "header": {**h, "avatar": {"seed": "x" * 129}}}, "seed must be"),
            ({**c, "header": {**h, "avatar": {"initial": "x" * 9}}}, "initial must be"),
            ({**c, "header": {**h, "avatar": {"url": "mxc://s.space/" + "x" * 512}}}, "must be an mxc:// link"),
            ({**c, "text": " "}, "text must be"),
            ({**c, "fields": [{"label": "f", "value": ""}]}, "fields[0] needs"),
            ({**c, "fields": [{"label": "x" * 61, "value": "v"}]}, "fields[0] needs"),
            ({**c, "fields": [{"label": "f", "value": "x" * 301}]}, "fields[0] needs"),
            ({**c, "groups": [{"label": "g", "rows": []}]}, "groups[0] needs rows"),
            ({**c, "groups": [{"label": "x" * 81, "rows": [{"title": "t"}]}]}, "groups[0].label must be"),
            (row(tag="x" * 41), "tag must be"), (row(subtitle="x" * 301), "subtitle must be"),
            (row(title="x" * 121), "needs a title"),
            ({**c, "buttons": [{**reply, "label": "x" * 81}]}, "needs a label"),
            ({**c, "buttons": [{**reply, "confirm": "x" * 301}]}, "confirm must be"),
            ({**c, "buttons": [{"label": "x", "kind": "url", "url": "https://s.space/x", "uses_input": "yes"}]},
             "uses_input must be true or false"),
            ({**c, "buttons": [{"label": "x", "kind": "url", "url": "https://s.space/" + "x" * 4096}]}, "not an http(s) link"),
            ({**c, "buttons": [{"label": "x", "kind": "open_dm", "user_id": "pylon"}]}, "needs a user_id"),
            ({**c, "buttons": [{"label": "x", "kind": "open_room", "room_id": "#room:s.space"}]}, "needs a room_id"),
            ({**c, "input": {"name": "a", "label": "x" * 201}}, "input.label must be"),
            ({**c, "input": {"name": "a", "prefix": "x" * 61}}, "input.prefix must be"),
            ({**c, "input": {"name": "a", "hint": ""}}, "input.hint must be"),
            ({**c, "input": {"name": "a", "value": 3}}, "input.value"),
            ({**c, "note": "x" * 601}, "note must be"),
            ({**c, "expires_at": 0}, "expires_at must be milliseconds"),
            ({**c, "expires_at": 2 ** 53}, "expires_at must be milliseconds"),
            ({**c, "v": True}, "kind must be"),
        ]
        for card, why in cases:
            self.assertIn(why, ac.problem(card) or "", card)
        fine = [{**c, "header": {**h, "status": {"label": "x" * 40}, "subtitle": None}}, {**c, "state": None},
                {**c, "note": "x" * 600, "note_tone": None, "text": None, "input": {"name": "a"}},
                {**c, "groups": [{"rows": [{"title": "t", "tag": "x" * 40, "state": None, "avatar": None}]}]},
                {**c, "buttons": [{**reply, "label": "x" * 80, "confirm": "x" * 300, "style": None}]},
                {**c, "header": {**h, "avatar": {"mxid": "@a:s.space", "shape": "square"}}}]
        for card in fine:
            self.assertIsNone(ac.problem(card), card)

    def test_groups_rows_and_avatars(self):
        good = ac.group("Chat apps", [ac.row("Discord", state="off", avatar_=ac.avatar(app="discord", shape="square"),
                                             actions=[ac.button("⋯", "menu", items=[
                                                 ac.button("Disconnect", "reply", reply="card:a:b:c:confirmed",
                                                           confirm="Disconnect Discord?")])])])
        self.assertIsNone(ac.problem({**self.card, "groups": [good]}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": [{"label": "x", "rows": [{"state": "on"}]}]}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": [{"label": "x", "rows": [
            {"title": "t", "avatar": {"shape": "round"}}]}]}))

    def test_each_other_shape_the_client_refuses_is_named(self):
        c, h = self.card, self.card["header"]
        reply = {"label": "x", "kind": "reply", "reply": "r"}
        cases = [
            ("not an object", "the card is not an object"),
            ({**c, "header": {**h, "avatar": {"seed": "s", "shape": "oval"}}}, "shape must be round or square"),
            ({**c, "header": {**h, "avatar": {"url": "mxc://[::1"}}}, "must be an mxc:// link"),
            ({**c, "buttons": [{**reply, "style": "loud"}]}, "style must be primary or secondary"),
            ({**c, "buttons": "x"}, "buttons must be a list"),
            ({**c, "header": {**h, "badges": [{"label": "b", "state": "maybe"}]}}, "badges[0] needs a label"),
            ({**c, "header": {**h, "shuffle": {"reply": ""}}}, "shuffle needs a reply"),
            ({**c, "fields": [{"label": "f", "value": 3}]}, "fields[0] needs a label and a value"),
            ({**c, "groups": [{"label": "g"}]}, "groups[0] needs rows"),
            ({**c, "groups": [{"label": "g", "rows": [{"title": "t", "state": "lit"}]}]}, "state must be one of"),
            ({**c, "note_tone": "loud"}, "note_tone must be dim, warn or ok"),
            ({**c, "expires_at": True}, "expires_at must be milliseconds"),
        ]
        for card, why in cases:
            self.assertIn(why, ac.problem(card) or "", card)

    def test_the_builders_refuse_an_empty_avatar_and_mark_an_input_button(self):
        with self.assertRaises(ac.CardError):
            ac.avatar()
        self.assertTrue(ac.button("Save", "reply", reply="card:a:b:c", uses_input=True)["uses_input"])
        self.assertIsNone(ac.parse_reply("card:a#b:c:d"))
        ok = {"access_tier": "owner", "user_id": "not-an-mxid", "channel_kind": "dm"}
        self.assertEqual(ac.owner_may_act(ok), "the reply names no sender")


class Send(unittest.TestCase):
    def test_send_posts_the_card_as_extra_content_and_returns_the_event(self):
        body, card = a_card()
        fake = FakeActions()
        eid = ac.send_card(fake.execute, DM, body, card, operation_id="op-1", reply_to="$ask")
        self.assertEqual(eid, "$ev1")
        action, args, op, room = fake.calls[0]
        self.assertEqual((action, op, room), ("room.message.send", "op-1", DM))
        self.assertEqual(args, {"body": body, "extra_content": {"space.ag2.card": card}, "reply_to": "$ask"})
        ac.send_card(fake.execute, DM, body, card, operation_id="op-2", thread_root="$root")
        self.assertEqual(fake.calls[1][1]["thread_root"], "$root")

    def test_a_private_card_goes_only_to_the_owner_dm(self):
        body, card = a_card()
        fake = FakeActions()
        for owner_dm in ("", DM):
            with self.assertRaises(ac.CardPrivacyError):
                ac.send_card(fake.execute, ROOM, body, card, operation_id="op", owner_dm=owner_dm)
        self.assertEqual(fake.calls, [])
        self.assertEqual(ac.send_card(fake.execute, DM, body, card, operation_id="op", owner_dm=DM), "$ev1")

    def test_a_refused_or_held_send_raises(self):
        body, card = a_card()
        for answer in ({"is_error": True, "code": "FORBIDDEN"}, {"status": "pending_approval", "approval_id": "a"},
                       {"result": {}}):
            with self.assertRaises(ac.CardError):
                ac.send_card(FakeActions(answer).execute, DM, body, card, operation_id="op")

    def test_a_bad_card_is_never_sent(self):
        fake = FakeActions()
        with self.assertRaises(ac.CardError):
            ac.send_card(fake.execute, DM, "b", {"kind": "agent_panel"}, operation_id="op")
        with self.assertRaises(ac.CardError):
            ac.send_card(fake.execute, DM, "b", a_card()[1], operation_id="")
        self.assertEqual(fake.calls, [])

    def test_an_update_posts_a_new_card_in_reply_to_the_old_one(self):
        self.assertFalse(ac.EDIT_CARRIES_CARD)
        body, card = a_card(state="done")
        fake = FakeActions()
        eid = ac.update_card(fake.execute, DM, "$old", body, card, operation_id="op-done", owner_dm=DM)
        self.assertEqual(eid, "$ev1")
        self.assertEqual(fake.calls[0][1]["reply_to"], "$old")
        self.assertEqual(fake.calls[0][1]["extra_content"]["space.ag2.card"]["state"], "done")


def task(said: str, **headers) -> str:
    h = {"source": "ag2space", "channel_id": DM, "channel_kind": "dm", "user_id": OWNER, "access_tier": "owner",
         **headers}
    lines = ["id: task-1", "task_layout: mid", f"task: {said}"] + [f"{k}: {v}" for k, v in h.items() if v is not None]
    return "\n".join(lines) + "\n"


class Replies(unittest.TestCase):
    def test_the_words_are_the_sender_s_own(self):
        headers, said = ac.reply_words(task("Get a new link for Pylon"))
        self.assertEqual((said, headers["user_id"]), ("Get a new link for Pylon", OWNER))

    def test_a_reply_quote_in_front_is_stripped(self):
        text = task('[s.space reply context;] {"sender": "@a:s.space", "body": "Card"} [End s.space reply context] '
                    "Get a new link for Pylon")
        self.assertEqual(ac.reply_words(text)[1], "Get a new link for Pylon")

    def test_a_command_parses_only_whole(self):
        self.assertEqual(ac.parse_reply("card:worker-approval:w1:resend"), ac.Reply("worker-approval", "w1", "resend", False))
        self.assertEqual(ac.parse_reply("card:connections:-:x:confirmed").payload, "card:connections:-:x:confirmed")
        for text in ("Add pylon to a room", "please card:worker-approval:w1:resend", "card:a:b", "card:a:b:c:d"):
            self.assertIsNone(ac.parse_reply(text), text)

    def test_a_command_rides_only_on_a_reply_button(self):
        with self.assertRaises(ac.CardError):
            ac.button("Go", "reply", reply="Go", command="not a command")
        card = a_card()[1]
        self.assertIsNotNone(ac.problem({**card, "buttons": [{"label": "x", "kind": "url", "url": "https://a.b",
                                                               "command": "card:a:b:c"}]}))
        self.assertIsNotNone(ac.problem({**card, "buttons": [{"label": "x", "kind": "reply", "reply": "x",
                                                               "command": "x"}]}))

    def test_the_click_header_is_read_only_whole(self):
        good = '{"card_id":"c1","action":"Go"}'
        self.assertEqual(ac.card_click({"card_click": good}), {"card_id": "c1", "action": "Go"})
        for bad in ("", "{broken", "[]", '{"card_id":"c1"}', '{"card_id":"","action":"Go"}',
                    json.dumps({"card_id": "c" * 201, "action": "Go"}), json.dumps({"card_id": "c", "action": "G" * 1001})):
            self.assertIsNone(ac.card_click({"card_click": bad}), bad)

    def test_only_the_owner_in_their_dm_may_act(self):
        ok = {"access_tier": "owner", "user_id": OWNER, "channel_kind": "dm"}
        self.assertIsNone(ac.owner_may_act(ok, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "access_tier": "team"}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "access_tier": ""}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "collaborator": "true"}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "user_id": "@other:s.space"}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "channel_kind": "room"}, OWNER))
        self.assertIsNone(ac.owner_may_act({**ok, "channel_kind": "room"}, OWNER, dm_only=False))


class Issued(unittest.TestCase):
    """Only the owner's click on a card this agent posted, with a text that card offered, acts."""
    TEXT, COMMAND = "Get a new link for Pylon", "card:worker-approval:w1:resend"
    HANDLER = "skills/worker-pool/scripts/pool_identity.py card-reply"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.card = a_card(state="expired", buttons=[
            ac.button(self.TEXT, "reply", reply=self.TEXT, command=self.COMMAND),
            ac.button("Add Pylon to a room", "reply", reply="Add Pylon to a room"),
            ac.button("+ Connect an app", "connect_picker")])[1]
        self.assertTrue(issued_cards.record(self.ws, "$mine", self.card, room=DM, lane="s_dspace",
                                            handler=self.HANDLER, now=1000.0))

    def ask(self, said=None, *, now=1001.0, click=None, owner=OWNER, **headers):
        said = self.TEXT if said is None else said
        click = {"card_id": self.card["id"], "action": said} if click is None else click
        h = {"access_tier": "owner", "user_id": OWNER, "channel_kind": "dm", "channel_id": DM,
             "reply_to_event": "$mine", "card_click": json.dumps(click) if click else "", **headers}
        return ac.click_verdict(h, said, self.ws, lambda lane: owner if lane == "s_dspace" else "", now=now)

    def forged(self, *a, **kw):
        got = self.ask(*a, **kw)
        self.assertEqual((got.verdict, got.command), (ac.FORGED, None), (a, kw))
        self.assertIn(ac.click_answer(got, "x")["say"], (ac.UNMATCHED_CLICK, ac.UNTAPPED_ACTION))
        return got.why

    def raw(self, header, said="Retire Pylon", **headers):
        h = {"access_tier": "owner", "user_id": OWNER, "channel_kind": "dm", "channel_id": DM,
             "reply_to_event": "$mine", "card_click": header, **headers}
        return ac.click_verdict(h, said, self.ws, lambda lane: OWNER, now=1001.0)

    def test_a_click_header_that_is_present_but_unusable_is_refused_never_read_as_chat(self):
        said = "Retire Pylon"
        shapes = {"blank id": {"card_id": "", "action": said}, "whitespace id": {"card_id": "  ", "action": said},
                  "blank action": {"card_id": "c1", "action": ""}, "whitespace action": {"card_id": "c1", "action": " \t"},
                  "non-string id": {"card_id": 7, "action": said}, "non-string action": {"card_id": "c1", "action": [said]},
                  "missing id": {"action": said}, "missing action": {"card_id": "c1"}, "a list": ["c1", said],
                  "a string": "c1", "null": None, "oversize id": {"card_id": "c" * 201, "action": said},
                  "oversize action": {"card_id": "c1", "action": "a" * 1001}}
        headers = {name: json.dumps(v) for name, v in shapes.items()}
        headers.update({"bad json": "{broken", "the bridge's stamp": "invalid"})
        for name, header in headers.items():
            for reply_to in ("$mine", "$forged", ""):
                got = self.raw(header, reply_to_event=reply_to)
                self.assertEqual((got.verdict, got.why), (ac.FORGED, "its click header cannot be read"), (name, reply_to))
                self.assertEqual(ac.click_answer(got, said),
                                 {"status": "forged_or_stale_click", "say": ac.UNMATCHED_CLICK, "why": got.why}, name)
            self.assertIsNone(ac.card_click({"card_click": header}), name)
            self.assertTrue(ac.click_tagged({"card_click": header}), name)
        for absent in ({}, {"card_click": ""}, {"card_click": "  "}):
            self.assertFalse(ac.click_tagged(absent))
        self.assertEqual(self.raw("", reply_to_event="").verdict, ac.NOT_A_CARD)

    def test_the_owner_s_click_on_this_agent_s_card_is_its_command(self):
        got = self.ask()
        self.assertEqual(got, ac.Click(ac.COMMAND, ac.Reply("worker-approval", "w1", "resend", False), "", self.HANDLER,
                                       "s_dspace"))
        self.assertIsNone(ac.click_answer(got, self.TEXT, ("worker-approval",)))
        self.assertEqual(ac.click_answer(got, self.TEXT, ("workers",)),
                         {"status": "other_card", "card": "worker-approval", "run": self.HANDLER})

    def test_a_click_on_plain_words_or_a_picker_s_ask_is_the_owner_s_own_words(self):
        for said in ("Add Pylon to a room", "Connect acme crm"):
            got = self.ask(said)
            self.assertEqual(got.verdict, ac.WORDS, said)
            self.assertEqual(ac.click_answer(got, said), {"status": "owner_words", "words": said})
        plain = {**self.card, "id": "no-picker", "buttons": self.card["buttons"][:2]}
        issued_cards.record(self.ws, "$plain", plain, room=DM, lane="s_dspace", now=1000.0)
        self.forged("Connect acme crm", reply_to_event="$plain", click={"card_id": "no-picker", "action": "Connect acme crm"})
        self.forged("Connect ")

    def test_words_with_no_click_and_no_card_action_are_ordinary_chat(self):
        for said, reply_to in (("Retire Pylon", ""), (self.TEXT, "$elsewhere"), ("", "")):
            got = self.ask(said, click={}, reply_to_event=reply_to)
            self.assertEqual(got, ac.Click(ac.NOT_A_CARD), (said, reply_to))
            self.assertEqual(ac.click_answer(got, said), {"status": "not_a_card"})

    def test_untagged_words_in_reply_to_an_issued_card(self):
        for said in ("what's up", "Retire Pylon", "Add Pylon to a room"):
            got = self.ask(said, click={})
            self.assertEqual(got, ac.Click(ac.WORDS), said)
            self.assertEqual(ac.click_answer(got, said), {"status": "owner_words", "words": said})
        got = self.ask(click={})
        self.assertEqual((got.verdict, got.why, got.say),
                         (ac.FORGED, "it is a button's text without a click", ac.UNTAPPED_ACTION))
        self.assertEqual(ac.click_answer(got, self.TEXT)["say"], ac.UNTAPPED_ACTION)
        for kw in ({"user_id": "@member:s.space"}, {"access_tier": "team"}, {"channel_id": ROOM},
                   {"channel_kind": "room"}, {"owner": ""}):
            for said in (self.TEXT, "what's up"):
                self.assertEqual(self.ask(said, click={}, **kw), ac.Click(ac.NOT_A_CARD),
                                 "someone else's words are chat under their own tier, never the owner's")

    def test_card_action_text_without_a_click_is_never_ordinary_chat(self):
        self.assertIn("without a click", self.forged(click={}))
        for reply_to in ("", "$mine", "$elsewhere"):
            got = self.ask(self.COMMAND, click={}, reply_to_event=reply_to)
            self.assertEqual((got.verdict, got.say), (ac.FORGED, ac.UNTAPPED_ACTION), reply_to)

    def test_a_click_that_does_not_match_an_issued_card_is_forged_or_stale(self):
        self.assertIn("another action", self.forged(click={"card_id": self.card["id"], "action": "something else"}))
        for kw in ({"reply_to_event": "$forged"}, {"reply_to_event": ""},
                   {"click": {"card_id": "another-card", "action": self.TEXT}}):
            self.assertIn("does not answer a card I posted", self.forged(**kw))
        self.assertIn("did not offer", self.forged("Retire Pylon"))
        self.forged(self.COMMAND)
        self.assertIn("not in the room", self.forged(channel_id=ROOM))
        self.forged(now=1000.0 + issued_cards.TTL_S + 1)

    def test_anyone_but_the_owner_of_the_card_s_connection_is_refused(self):
        self.forged(user_id="@member:s.space")
        self.forged(access_tier="team")
        self.forged(channel_kind="room")
        self.assertEqual(self.forged(owner=""), "the owner is not known")
        issued_cards.record(self.ws, "$other", {**self.card, "id": "c2"}, room=DM, lane="other", now=1000.0)
        self.forged(reply_to_event="$other", click={"card_id": "c2", "action": self.TEXT})

    def test_the_owner_is_the_one_of_the_connection_the_card_was_posted_on(self):
        rows = [{"instance": "s_dspace", "owner_mxid": "@second:s.space"},
                {"instance": "s_dspace", "owner_mxid": "@worker-owner:s.space", "worker": "w1"},
                {"instance": "primary", "owner_mxid": OWNER}]
        with mock.patch("connections.read_rows", lambda ws=None: rows):
            owner_of = ac.lane_owner(self.ws, "@fallback:s.space")
            self.assertEqual([owner_of(x) for x in ("s_dspace", "primary", "gone", "")],
                             ["@second:s.space", OWNER, "", "@fallback:s.space"])

    def test_a_newer_card_with_the_same_id_replaces_the_older_record(self):
        done = a_card(state="done")[1]
        self.assertFalse(issued_cards.record(self.ws, "$done", done, room=DM, now=1002.0))
        self.forged(now=1003.0)
        self.assertTrue(issued_cards.record(self.ws, "$a", self.card, room=DM, lane="s_dspace", now=1004.0))
        self.assertTrue(issued_cards.record(self.ws, "$b", self.card, room=ROOM, lane="s_dspace", now=1004.0))
        self.assertEqual(sorted(json.loads(issued_cards.ledger_path(self.ws).read_text())["cards"]), ["$a", "$b"])
        self.assertTrue(issued_cards.settle(self.ws, "$a"))
        self.assertFalse(issued_cards.settle(self.ws, "$a"))
        self.forged(reply_to_event="$a", now=1005.0)

    def test_a_record_expires_and_the_ledger_keeps_only_live_cards(self):
        self.assertLessEqual(issued_cards.TTL_S, 3 * 86400)
        issued_cards.record(self.ws, "$later", {**self.card, "id": "later"}, now=1000.0 + issued_cards.TTL_S + 1)
        kept = json.loads(issued_cards.ledger_path(self.ws).read_text())["cards"]
        self.assertEqual(list(kept), ["$later"])
        issued_cards.ledger_path(self.ws).unlink()
        with mock.patch.object(issued_cards, "MAX_CARDS", 2):
            for i in range(3):
                issued_cards.record(self.ws, f"$n{i}", {**self.card, "id": f"c{i}"}, now=2000.0 + i)
        self.assertEqual(sorted(json.loads(issued_cards.ledger_path(self.ws).read_text())["cards"]), ["$n1", "$n2"])

    def test_every_unambiguous_reply_is_recorded_with_its_command_or_as_plain_words(self):
        cmd = lambda text, c: ac.button(text, "reply", reply=text, command=c)
        card = a_card(shuffle_reply="Shuffle", buttons=[cmd("Pause Pylon", "card:workers:w1:pause"),
                      cmd("Pause Quill", "card:workers:w2:pause"), cmd("Pause Quill", "card:workers:w3:pause"),
                      ac.button("+ New worker", "reply", reply="create a worker"),
                      ac.button("Open", "url", url="https://s.space/x")],
                      footer_link=cmd("Restart Pylon", "card:workers:w1:restart"),
                      groups=[ac.group("g", [ac.row("t", actions=[ac.button("⋯", "menu", items=[
                          cmd("Retire Pylon", "card:workers:w1:retire")])])])])[1]
        self.assertEqual(issued_cards.offered(card), {"Pause Pylon": "card:workers:w1:pause",
                                                      "Restart Pylon": "card:workers:w1:restart",
                                                      "Retire Pylon": "card:workers:w1:retire",
                                                      "create a worker": "", "Shuffle": ""})
        self.assertFalse(issued_cards.record(self.ws, "$plain", a_card()[1]))
        self.assertFalse(issued_cards.record(self.ws, "not-an-event", card))
        self.assertFalse(issued_cards.has_picker(card))

    def test_an_unreadable_ledger_holds_no_card(self):
        issued_cards.ledger_path(self.ws).write_text("{broken")
        self.forged()
        issued_cards.ledger_path(self.ws).write_text(json.dumps({"cards": []}))
        self.assertIsNone(issued_cards.live(self.ws, "$mine"))
        issued_cards.ledger_path(self.ws).write_text(json.dumps({"cards": {"$mine": {
            "card_id": self.card["id"], "offers": [self.TEXT], "expires_at": 1e18}}}))
        self.assertIsNone(issued_cards.live(self.ws, "$mine"))
        issued_cards.ledger_path(self.ws).write_text(json.dumps({"cards": {"$mine": {
            "card_id": self.card["id"], "offers": {self.TEXT: "not a command"}, "room": DM, "lane": "s_dspace",
            "expires_at": 1e18}}}))
        self.assertEqual(self.forged(), "its command is unreadable")

    def test_the_cli_records_a_posted_message_payload(self):
        def run(stdin, *argv):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
                rc = issued_cards.main(["record", "--workspace", str(self.ws), *argv])
            return rc, json.loads(out.getvalue())
        message = json.dumps({"body": "b", "extra_content": {"space.ag2.card": {**self.card, "id": "cli"}}})
        self.assertEqual(run(message, "--event-id", "$cli", "--room", DM, "--lane", "s_dspace", "--handler", "h"),
                         (0, {"recorded": True}))
        entry = issued_cards.live(self.ws, "$cli")
        self.assertEqual((entry["room"], entry["lane"], entry["handler"], entry["offers"][self.TEXT]),
                         (DM, "s_dspace", "h", self.COMMAND))
        self.assertEqual(run("{}", "--event-id", "$x"), (1, {"recorded": False}))
        self.assertEqual(run("nope", "--event-id", "$x")[0], 2)


class Dispatch(Issued):
    """`agent_card.py click`: the one place a task is matched to a card, for every card."""

    def run_click(self, said, *, click=None, **headers):
        click = {"card_id": self.card["id"], "action": said} if click is None else click
        h = {"access_tier": "owner", "user_id": OWNER, "channel_kind": "dm", "channel_id": DM,
             "reply_to_event": "$mine", **headers}
        path = self.ws / "task.txt"
        path.write_text("\n".join(["id: task-1", "task_layout: mid"] + [f"{k}: {v}" for k, v in h.items()]
                                  + ([f"card_click: {json.dumps(click)}"] if click else [])
                                  + [f"task: {said}", "source: ag2space"]) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch("connections.read_rows", lambda ws=None: [{"instance": "s_dspace", "owner_mxid": OWNER}]), \
                mock.patch.object(issued_cards.time, "time", lambda: 1001.0):
            rc = ac.main(["click", "--task-file", str(path), "--workspace", str(self.ws)])
        return rc, json.loads(out.getvalue())

    def test_each_verdict_is_one_json_line(self):
        self.assertEqual(self.run_click(self.TEXT),
                         (0, {"status": "command", "card": "worker-approval", "run": self.HANDLER}))
        self.assertEqual(self.run_click("Add Pylon to a room"),
                         (0, {"status": "owner_words", "words": "Add Pylon to a room"}))
        self.assertEqual(self.run_click("good morning", click={}), (0, {"status": "owner_words", "words": "good morning"}))
        self.assertEqual(self.run_click("good morning", click={}, reply_to_event=""), (0, {"status": "not_a_card"}))
        rc, out = self.run_click(self.TEXT, click={})
        self.assertEqual((rc, out["status"], out["say"]), (1, "forged_or_stale_click", ac.UNTAPPED_ACTION))
        rc, out = self.run_click("Retire Pylon", click={"card_id": "  ", "action": "Retire Pylon"})
        self.assertEqual((rc, out["status"], out["say"]), (1, "forged_or_stale_click", ac.UNMATCHED_CLICK))
        rc, out = self.run_click("Retire Pylon")
        self.assertEqual((rc, out["status"], out["say"]), (1, "forged_or_stale_click", ac.UNMATCHED_CLICK))
        rc, out = self.run_click(self.TEXT, user_id="@member:s.space")
        self.assertEqual((rc, out["status"]), (1, "forged_or_stale_click"))

    def test_a_handler_that_is_not_a_skill_script_is_not_printed(self):
        issued_cards.record(self.ws, "$odd", {**self.card, "id": "odd"}, room=DM, lane="s_dspace",
                            handler="rm -rf / ; skills/x/scripts/y.py", now=1000.0)
        rc, out = self.run_click(self.TEXT, reply_to_event="$odd", click={"card_id": "odd", "action": self.TEXT})
        self.assertEqual((rc, out), (0, {"status": "command", "card": "worker-approval", "run": None}))

    def test_an_unreadable_task_and_the_default_workspace(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("workspace_default.resolve_workspace", lambda: str(self.ws)):
            rc = ac.main(["click", "--task-file", str(self.ws / "absent.txt")])
        self.assertEqual((rc, json.loads(out.getvalue())["status"]), (2, "refused"))


if __name__ == "__main__":
    unittest.main()
