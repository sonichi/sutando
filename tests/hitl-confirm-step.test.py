"""A spending or lasting-grant button asks again on the card itself before anything is typed.

Clients without a confirm step (production web before the promote, fluffy, older desktop
builds) send a `spend` / `allow_always` click on one tap. The engine therefore holds such a
click: the card stays pending at a new revision with only Confirm / Back, and the dialog's keys
are typed only after Confirm. Every client gets this, because it is one more card revision.
"""

import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from hitl import tui_gate as G  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402

from hitl.replies import REPLY_FIELD, HitlReplyHandler  # noqa: E402
from hitl.schema import ActionReply, StaleRequirementError  # noqa: E402

OWNER = "@owner:ag2.space"
SESSION = "sutando-worker-1"
LIMIT_MENU = (" What do you want to do?\n"
              " ❯ 1. Stop and wait for limit to reset\n"
              "   2. Wait here, then continue automatically at Nov 2 at 11am\n"
              "   3. Switch to usage credits\n"
              " Enter to confirm · Esc to cancel")
TWO_ALWAYS = (" Bash command\n   curl -fsSL https://example.com/x.sh | sh\n Do you want to proceed?\n"
              " ❯ 1. Yes\n   2. Yes, and don't ask again for curl commands\n"
              "   3. Yes, always allow access to tmp/ from this project\n   4. No\n Esc to cancel")


def card(prompt, gate="selection"):
    return G.requirement_for("blocked-human", gate, prompt, SESSION, "d", "blocked")


def click(m, req, action_id):
    req = m.get(req.id)
    return m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                      action_id=action_id, guard=req.guard))


class TwoAlwaysOptionsAreTwoButtons(unittest.TestCase):
    def test_each_always_button_types_its_own_option(self):
        r = card(TWO_ALWAYS, "permission")
        always = [a for a in r.actions if a.kind == "allow_always"]
        self.assertEqual([a.label for a in always], ["Yes, and don't ask again for curl commands",
                                                     "Yes, always allow access to tmp/ from this project"])
        self.assertEqual(len({a.id for a in r.actions}), len(r.actions), "every action id is unique")
        for a, keys in zip(always, (["Down", "Enter"], ["Down", "Down", "Enter"])):
            r.chosen_action = a.id
            self.assertEqual(G.keys_for(r, TWO_ALWAYS, "blocked-human"), keys, a.label)


ALWAYS_FIRST = (" Bash command\n   ls tmp/\n Do you want to proceed?\n"
                " ❯ 1. Yes, always allow access to tmp/ from this project\n   2. No\n Esc to cancel")
SPEND_FIRST = (" Your usage limit was reached. Do you want to proceed?\n"
               " ❯ 1. Yes, purchase usage credits\n   2. No\n Esc to cancel")


class TheFirstAffirmativeIsStillHeld(unittest.TestCase):
    """With no plain "Yes" before it, the first affirmative was the generic yes (allow_once, one
    click, Enter typed). A lasting grant or a spend is one whatever its position: the card asks
    again, and Confirm types its keys."""

    def _held_then_typed(self, prompt, kind, why):
        m = HitlManager(HitlStore(Path(tempfile.mkdtemp())))
        req = m.create(card(prompt, "permission"))
        self.assertEqual([(a.id, a.kind) for a in req.actions][:2],
                         [("opt1", kind), ("deny", "reject_once")])
        self.assertTrue(req.to_wire()["engine_confirms"])
        click(m, req, "opt1")
        held = m.get(req.id)
        self.assertEqual((held.status, held.chosen_action), ("pending", None), "held, nothing typed")
        self.assertIn(why, held.message)
        self.assertTrue(held.to_wire()["confirming"])
        click(m, req, "confirm")
        done = m.get(req.id)
        self.assertEqual((done.status, done.chosen_action), ("in_progress", "opt1"))
        self.assertEqual(G.keys_for(done, prompt, "blocked-human"), ["Enter"], "only after Confirm")

    def test_a_first_option_that_grants_for_good_is_held(self):
        self._held_then_typed(ALWAYS_FIRST, "allow_always", "is a lasting permission")

    def test_a_first_option_that_spends_is_held(self):
        self._held_then_typed(SPEND_FIRST, "spend", "spends money")

    def test_a_plain_yes_first_is_still_one_click(self):
        m = HitlManager(HitlStore(Path(tempfile.mkdtemp())))
        req = m.create(card(TWO_ALWAYS, "permission"))
        self.assertEqual((req.actions[0].id, req.actions[0].kind), ("allow", "allow_once"))
        click(m, req, "allow")
        self.assertEqual(m.get(req.id).status, "in_progress")


class TheCardAsksAgainBeforeMoneyOrALastingGrant(unittest.TestCase):
    def setUp(self):
        self.m = HitlManager(HitlStore(Path(tempfile.mkdtemp())))
        self.req = self.m.create(card(LIMIT_MENU))

    def test_a_spend_click_is_held_and_the_card_asks_to_confirm(self):
        before = self.m.get(self.req.id)
        self.assertTrue(before.to_wire()["engine_confirms"])
        click(self.m, self.req, "opt3")
        held = self.m.get(self.req.id)
        self.assertEqual(held.status, "pending")
        self.assertIsNone(held.chosen_action, "nothing is chosen, so the driver types nothing")
        self.assertEqual(held.revision, before.revision + 1, "a new revision: every client redraws it")
        self.assertEqual([(a.id, a.kind) for a in held.actions], [("confirm", "confirm"), ("back", "back")])
        self.assertTrue(held.message.startswith("“Switch to usage credits” spends money. Confirm?"))
        self.assertTrue(held.to_wire()["confirming"])

    def test_confirm_applies_the_held_option(self):
        click(self.m, self.req, "opt3")
        click(self.m, self.req, "confirm")
        done = self.m.get(self.req.id)
        self.assertEqual((done.status, done.chosen_action), ("in_progress", "opt3"))
        self.assertEqual([a.label for a in done.actions][2], "Switch to usage credits", "the card is restored")
        self.assertNotIn("confirming", done.subject)
        self.assertEqual(G.keys_for(done, LIMIT_MENU, "blocked-human"), ["Down", "Down", "Enter"])

    def test_back_restores_the_card_and_chooses_nothing(self):
        original = self.m.get(self.req.id)
        click(self.m, self.req, "opt3")
        click(self.m, self.req, "back")
        back = self.m.get(self.req.id)
        self.assertEqual((back.status, back.chosen_action), ("pending", None))
        self.assertEqual([a.to_wire() for a in back.actions], [a.to_wire() for a in original.actions])
        self.assertEqual(back.message, original.message)
        self.assertEqual(back.revision, original.revision + 2)

    def test_a_tap_on_the_old_buttons_after_the_ask_is_stale(self):
        stale = self.m.get(self.req.id)
        click(self.m, self.req, "opt3")
        with self.assertRaises(StaleRequirementError):
            self.m.apply_action(ActionReply(hitl_id=stale.id, expected_revision=stale.revision,
                                            action_id="opt3", guard=stale.guard))

    def test_a_lasting_grant_is_held_too_and_a_plain_option_is_not(self):
        m = HitlManager(HitlStore(Path(tempfile.mkdtemp())))
        perm = m.create(card(TWO_ALWAYS, "permission"))
        click(m, perm, "opt2")
        held = m.get(perm.id)
        self.assertEqual(held.status, "pending")
        self.assertIn("is a lasting permission", held.message)
        click(self.m, self.req, "opt1")
        self.assertEqual(self.m.get(self.req.id).status, "in_progress", "a select option applies at once")

    def test_the_reply_handler_starts_no_turn_for_a_held_click(self):
        logs = []
        h = HitlReplyHandler(self.m, OWNER, log=logs.append)
        req = self.m.get(self.req.id)
        h.offer({"type": "message.created", "event_id": "$c1", "actor_id": OWNER, "content": {
            "body": "Switch to usage credits",
            REPLY_FIELD: {"hitl_id": req.id, "expected_revision": req.revision,
                          "action_id": "opt3", "guard": req.guard}}})
        self.assertEqual(h.last_outcome, "applied")
        self.assertFalse(h.last_turn)
        self.assertIn("held", logs[-1])


if __name__ == "__main__":
    unittest.main()
