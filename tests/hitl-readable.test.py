"""What a person reads on a card, and in what order.

A card's human-facing strings carry no link, machine name or digest; the event keeps every
field the producer set. Options are numbered exactly as the terminal prints them.
"""

# ruff: noqa: E402 — imports follow the sys.path insert below

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl import readable, tui_gate
from hitl.events import SCHEMA, _requirement_for
from hitl.manager import HitlManager, HitlStore
from hitl.projector import fallback_body, plain_text, project
from hitl.schema import (
    Action, ActionReply, HumanRequirement, WIRE_FIELD, validate_action)

ROOM = "!room:ag2.space"
HOST = "Chis-MacBook-Pro"
LINK = "https://docs.ag2.ai/guide?id=9f2a1b3c4d5e6f70"
DIGEST = "4f2b9c1d8e7a6b50"
SEAT = "sutando-worker-pro-dev"
# Every machine word, link and digest this file plants: none of them is a person's business.
LEAKS = (LINK, "docs.ag2.ai", DIGEST, "sha256:" + DIGEST, SEAT, HOST, "Qingyun's Air")

# A real permission dialog: the caret sits on 2, so the live block is the numbered one.
PROMPT = "\n".join([
    "Claude needs your permission to continue",
    f"Fetch {LINK} for session {SEAT}",
    "",
    "   1. Yes",
    " ❯ 2. Yes, and don't ask again",
    "   3. No, tell Claude what to do",
    "  Esc to cancel",
])


def leaky_req(**kw):
    """A card whose every human-facing string carries a link, a machine name and a digest."""
    defaults = dict(
        kind="choice",
        runtime="claude",
        message=f"Approve the deploy at {LINK} on host {HOST} (build {DIGEST}abcd)",
        title=f"{SEAT} · waiting",
        guard="sha256:" + DIGEST,
        device={"id": SEAT, "name": "Qingyun's Air"},
        actions=[Action(id="go", kind="confirm", label=f"Deploy {DIGEST}abcd"),
                 Action(id="open_terminal", kind="open_terminal", label=f"Open terminal ({SEAT})")],
        subject={"session": SEAT},
    )
    defaults.update(kw)
    return HumanRequirement(**defaults)


def tui_req():
    return tui_gate.requirement_for("blocked-human", "selection", PROMPT, SEAT,
                                    "detail", "fallback", runtime="claude")


def event_req(**extra):
    ev = {"schema": SCHEMA, "session": "core-2", "socket": "/tmp/s.sock", "runtime": "claude",
          "kind": "choice", "prompt": f"Pick a branch at {LINK}", "guard": "g1",
          "options": [{"id": "1", "label": "Yes"}, {"id": "4", "label": "No"}]}
    ev.update(extra)
    return _requirement_for(ev)


def human_strings(content):
    return [str(content.get("message") or ""), str(content.get("title") or "")] + \
           [str(a.get("label") or "") for a in content.get("actions") or []]


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, payload):
        self.sent.append(payload)
        return {"ok": True, "event_id": f"$ev{len(self.sent)}"}


class HumanFacingStringsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        self.send = Sender()
        patch = mock.patch("hitl.schema.device_host", return_value=HOST)
        patch.start()
        self.addCleanup(patch.stop)

    def projected(self, req):
        self.mgr.create(req)
        project(self.mgr, self.send, ROOM)
        return self.send.sent[-1]

    def test_a_link_a_machine_name_and_a_digest_never_reach_what_a_person_reads(self):
        payload = self.projected(leaky_req())
        content = payload["extra_content"][WIRE_FIELD]
        for text in human_strings(content) + [payload["body"]]:
            for leak in LEAKS:
                self.assertNotIn(leak, text, text)
        self.assertEqual(content["message"], "Approve the deploy (build)")
        self.assertEqual(content["title"], "waiting")
        self.assertEqual([a["label"] for a in content["actions"]], ["Deploy", "Open terminal"])

    def test_what_the_card_stops_saying_is_still_on_the_wire(self):
        req = leaky_req()
        content = self.projected(req)["extra_content"][WIRE_FIELD]
        self.assertEqual(content["guard"], "sha256:" + DIGEST, "CAS state, not prose")
        self.assertEqual(content["device"], {"id": SEAT, "name": "Qingyun's Air", "host": HOST})
        self.assertEqual(content["device"], req.to_wire()["device"])

    def test_the_wire_shape_is_unchanged(self):
        """The regression guard: only the three human-facing strings may differ from
        `to_wire()`. A key added, dropped or re-typed here is a protocol change."""
        for req in (leaky_req(), tui_req(), event_req(), leaky_req(title="", device=None)):
            with self.subTest(kind=req.kind, title=req.title):
                wire, content = req.to_wire(), readable.card(req)
                self.assertEqual(list(content), list(wire), "same keys, same order")
                rest = lambda w: {k: v for k, v in w.items()  # noqa: E731
                                  if k not in ("message", "title", "actions")}
                self.assertEqual(rest(content), rest(wire))
                self.assertEqual([list(a) for a in content.get("actions") or []],
                                 [list(a) for a in wire.get("actions") or []])
                for key in ("message", "title"):
                    if key in wire:
                        self.assertIsInstance(content[key], str)

    def test_a_title_that_was_only_a_machine_name_keeps_its_key(self):
        content = readable.card(leaky_req(title=SEAT))
        self.assertEqual(content["title"], "", "emptied, not removed")
        self.assertIn("title", content)

    def test_the_plain_text_is_built_from_the_cards_own_strings(self):
        """The body cannot drift from the card: it is composed of the card's own
        message plus the fixed header, so it has no second source to drift from."""
        content = readable.card(leaky_req())
        self.assertEqual(plain_text(content),
                         f"Sutando needs a decision — {content['message']}")
        self.assertEqual(plain_text(dict(content, message="")), "Sutando needs a decision",
                         "a message that redacts away leaves the header alone")
        self.assertEqual(fallback_body(leaky_req()), plain_text(content))

    def test_the_body_that_is_sent_is_rendered_from_the_content_that_is_sent(self):
        payload = self.projected(leaky_req())
        self.assertEqual(payload["body"], plain_text(payload["extra_content"][WIRE_FIELD]))

    def test_an_ordinary_sentence_is_left_alone(self):
        for text in ("Claude Code needs to sign in again",
                     "Run the tests before you merge - they are quick",
                     "Delete 12 files in ~/Documents?"):
            self.assertEqual(readable.clean(text, leaky_req(device=None, subject={})), text)

    def test_a_one_or_two_letter_device_name_is_not_scrubbed_out_of_prose(self):
        req = leaky_req(device={"id": "d1", "name": "n"}, subject={})
        self.assertEqual(readable.clean("n files in d1 are new", req), "n files in d1 are new")


class CanonicalActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        self.send = Sender()

    def projected_actions(self, req):
        if self.mgr.get(req.id) is None:
            self.mgr.create(req)
        project(self.mgr, self.send, ROOM)
        return self.send.sent[-1]["extra_content"][WIRE_FIELD]["actions"]

    def test_the_options_are_numbered_and_ordered_as_the_terminal_prints_them(self):
        """The terminal prints `1. Yes` … `3. No`; the card says the same words against
        the same numbers, in the same order, so a person reading both sees one list."""
        req = tui_req()
        self.assertEqual(tui_gate.parse_options(PROMPT)[0],
                         ["Yes", "Yes, and don't ask again", "No, tell Claude what to do"])
        self.assertEqual([(a["id"], a["label"]) for a in self.projected_actions(req)],
                         [("allow", "1. Yes"), ("deny", "3. No"), ("open_terminal", "Open terminal")])

    def test_a_terminal_key_is_the_number_it_is_printed_against(self):
        self.assertEqual([a["label"] for a in self.projected_actions(event_req())],
                         ["1. Yes", "4. No", "Open terminal"])

    def test_nothing_is_appended_padded_or_dropped(self):
        for req in (tui_req(), event_req(), leaky_req()):
            with self.subTest(kind=req.kind):
                got = readable.card(req)["actions"]
                self.assertEqual(len(got), len(req.actions))
                self.assertEqual({a["id"] for a in got}, {a.id for a in req.actions})
                self.assertEqual({a["kind"] for a in got}, {a.kind for a in req.actions})
                for label in (a["label"] for a in got):
                    self.assertNotIn("Other", label)
                    self.assertNotIn("reply", label.lower())

    def test_an_action_the_terminal_does_not_number_keeps_its_place_last(self):
        got = readable.card(event_req())["actions"]
        self.assertEqual(got[-1]["id"], "open_terminal")

    def test_a_label_that_already_carries_its_number_is_not_numbered_twice(self):
        req = event_req(options=[{"id": "a", "label": "2. No"}, {"id": "b", "label": "1. Yes"}])
        self.assertEqual([a["label"] for a in readable.card(req)["actions"]][:2],
                         ["1. Yes", "2. No"])

    def test_a_card_the_terminal_never_printed_is_not_numbered_at_all(self):
        req = HumanRequirement(
            kind="auth", runtime="claude", message="Your Claude session has expired.", guard="g",
            actions=[Action(id="authenticate", kind="authenticate", label="Sign in to Claude"),
                     Action(id="open_terminal", kind="open_terminal", label="Open terminal")])
        self.assertEqual([a["label"] for a in readable.card(req)["actions"]],
                         ["Sign in to Claude", "Open terminal"])

    def test_the_number_on_a_label_never_becomes_the_id_that_is_clicked(self):
        """Renumbering is presentation; the click still carries the producer's action id,
        which is what the stale gate and the key-sender match on."""
        req = self.mgr.create(tui_req())
        first = self.projected_actions(req)[0]
        self.assertEqual(first["label"], "1. Yes")
        matched = validate_action(req, ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                                   action_id=first["id"], guard=req.guard))
        self.assertEqual((matched.id, matched.label), ("allow", "Yes"))

    def test_the_order_survives_the_round_trip_to_the_sender(self):
        req = tui_req()
        wire_ids = [a["id"] for a in self.projected_actions(req)]
        self.assertEqual(wire_ids, ["allow", "deny", "open_terminal"])
        self.assertEqual([a.id for a in self.mgr.get(req.id).actions],
                         ["deny", "allow", "open_terminal"], "the store keeps the producer's own")


class NoticeHeadlineTests(unittest.TestCase):
    def test_a_notice_drops_links_and_digests_and_keeps_the_seat_it_is_about(self):
        """A notice stands alone, one per seat: the seat's name is how the owner knows
        which machine is still waiting, so only the link and the digest come out."""
        from hitl import renotify
        req = HumanRequirement(kind="core-blocked", runtime="claude", guard="g",
                               message=f"worker pro-dev is stuck at {LINK}",
                               title=f"worker pro-dev · blocked at {DIGEST}abcd",
                               device={"id": SEAT, "name": "worker pro-dev"},
                               created_at=1000.0, updated_at=1000.0)
        body = renotify.body(renotify.REMINDER, req, 1000.0 + 3600)
        self.assertIn("worker pro-dev · blocked at", body)
        for leak in (LINK, DIGEST):
            self.assertNotIn(leak, body)


if __name__ == "__main__":
    unittest.main()
