#!/usr/bin/env python3
"""Agent panel cards (src/agent_card.py): the builders, the client's checks, the sender and card replies.

- `build` returns a one-line body and a card the checks accept; a card a client would not draw raises.
- `problem` matches the web client: unknown fields allowed, required keys, http(s) links without
  credentials (avatars https or mxc), menus only in row actions, and the client's size limits.
- `send_card` posts through the injected Action call (`room.message.send` with `extra_content`) and returns
  the event id; a private card goes only to the owner's checked DM. `update_card` posts a new card as a
  reply to the old one, since no Action carries a card in an edit.
- A card reply is `card:<kind>:<target>:<verb>[:confirmed]`, read from a gateway task's own words, and only
  the owner, in their DM, may act on one.

Run: python3 tests/agent-card.test.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agent_card as ac  # noqa: E402

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

    def test_an_avatar_link_is_https_or_mxc_and_an_app_is_a_slug(self):
        for url, ok in (("https://a.b/x.png", True), ("mxc://s.space/abc", True), ("http://a.b/x.png", False)):
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

    def test_groups_rows_and_avatars(self):
        good = ac.group("Chat apps", [ac.row("Discord", state="off", avatar_=ac.avatar(app="discord", shape="square"),
                                             actions=[ac.button("⋯", "menu", items=[
                                                 ac.button("Disconnect", "reply", reply="card:a:b:c:confirmed",
                                                           confirm="Disconnect Discord?")])])])
        self.assertIsNone(ac.problem({**self.card, "groups": [good]}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": [{"label": "x", "rows": [{"state": "on"}]}]}))
        self.assertIsNotNone(ac.problem({**self.card, "groups": [{"label": "x", "rows": [
            {"title": "t", "avatar": {"shape": "round"}}]}]}))


class Send(unittest.TestCase):
    def test_send_posts_the_card_as_extra_content_and_returns_the_event(self):
        body, card = a_card()
        fake = FakeActions()
        eid = ac.send_card(fake.execute, DM, body, card, operation_id="op-1", reply_to="$ask")
        self.assertEqual(eid, "$ev1")
        action, args, op, room = fake.calls[0]
        self.assertEqual((action, op, room), ("room.message.send", "op-1", DM))
        self.assertEqual(args, {"body": body, "extra_content": {"space.ag2.card": card}, "reply_to": "$ask"})

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
    def test_the_payload_is_read_from_the_sender_s_own_words(self):
        headers, said = ac.reply_payload(task("card:worker-approval:w1:resend"))
        self.assertEqual(said, ac.Reply("worker-approval", "w1", "resend", False))
        self.assertEqual(headers["user_id"], OWNER)

    def test_a_reply_quote_in_front_is_stripped(self):
        text = task('[s.space reply context;] {"sender": "@a:s.space", "body": "Card"} [End s.space reply context] '
                    "card:worker-approval:w1:resend")
        self.assertEqual(ac.reply_payload(text)[1], ac.Reply("worker-approval", "w1", "resend", False))

    def test_ordinary_words_are_no_payload(self):
        self.assertIsNone(ac.reply_payload(task("Add pylon to a room"))[1])
        self.assertIsNone(ac.reply_payload(task("please card:worker-approval:w1:resend"))[1])

    def test_only_the_owner_in_their_dm_may_act(self):
        ok = {"access_tier": "owner", "user_id": OWNER, "channel_kind": "dm"}
        self.assertIsNone(ac.owner_may_act(ok, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "access_tier": "team"}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "access_tier": ""}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "collaborator": "true"}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "user_id": "@other:s.space"}, OWNER))
        self.assertIsNotNone(ac.owner_may_act({**ok, "channel_kind": "room"}, OWNER))
        self.assertIsNone(ac.owner_may_act({**ok, "channel_kind": "room"}, OWNER, dm_only=False))


if __name__ == "__main__":
    unittest.main()
