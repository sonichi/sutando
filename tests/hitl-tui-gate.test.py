#!/usr/bin/env python3
"""A dialog becomes a card with the buttons a human expects; a click becomes the keys that
answer THAT dialog: never a guessed key, never a key into a dialog the human did not see."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from hitl import tui_gate as G  # noqa: E402

PERM = ("  Dangerous rm operation on a possibly-empty path\n"
        "  Do you want to proceed?\n"
        "  ❯ 1. Yes\n"
        "    2. Yes, and don't ask again for rm commands\n"
        "    3. No\n"
        "  Esc to cancel")
PERM_CARET_ON_NO = PERM.replace("  ❯ 1. Yes", "    1. Yes").replace("    3. No", "  ❯ 3. No")
SELECT = ("  Select a tsconfig\n"
          "  ❯ 1. ./tsconfig.json\n"
          "    2. ./tsconfig.base.json\n"
          "  Enter to confirm · Esc to cancel")
LOGIN = "  Login\n  Select login method:\n  ❯ 1. Claude account with subscription\n    2. Anthropic Console account"
PRESS = "  Login\n  Logged in as x@example.com\n  Login successful. Press Enter to continue…"
FABLE = "  You've reached your Fable limit\n    Switch to Opus 5 and continue\n  ❯ Continue with Fable 5.1"
FALLBACK = "I am blocked and cannot continue without you."


def req(gate, prompt, state="blocked-human", session="s"):
    return G.requirement_for(state, gate, prompt, session, f"awaiting user: {gate}", FALLBACK)


def clicked(r, action_id):
    r.chosen_action = action_id
    return r


class TestParsing(unittest.TestCase):
    def test_options_and_caret(self):
        self.assertEqual(G.parse_options(PERM),
                         (["Yes", "Yes, and don't ask again for rm commands", "No"], 0))
        self.assertEqual(G.parse_options(PERM_CARET_ON_NO)[1], 2)

    def test_the_question_is_the_prose_above_the_options_without_frames_or_hints(self):
        self.assertEqual(G.question(PERM, "d"),
                         "Dangerous rm operation on a possibly-empty path Do you want to proceed?")
        self.assertEqual(G.question(SELECT, "d"), "Select a tsconfig")
        self.assertEqual(G.question("  ────────\n  Esc to cancel", "detail wins"), "detail wins")


class TestRequirement(unittest.TestCase):
    def test_a_permission_dialog_is_a_no_yes_card_in_the_owners_words(self):
        r = req("permission", PERM)
        self.assertEqual(r.kind, "permission")
        self.assertEqual(r.title, "Agent needs your confirmation")
        self.assertEqual([a.label for a in r.actions], ["No", "Yes", "Open terminal"])
        self.assertEqual([a.kind for a in r.actions], ["reject_once", "allow_once", "open_terminal"])
        self.assertEqual(r.message, "Dangerous rm operation on a possibly-empty path Do you want to proceed?")
        self.assertEqual(r.subject["option_for_action"], {"deny": 2, "allow": 0})
        self.assertEqual(r.subject["source"], G.SOURCE)
        self.assertEqual(r.device, {"id": "s", "name": "s"})

    def test_a_selection_is_a_choice_with_one_button_per_option(self):
        r = req("selection", SELECT)
        self.assertEqual(r.kind, "choice")
        self.assertEqual([a.id for a in r.actions], ["opt1", "opt2", "open_terminal"])
        self.assertEqual([a.label for a in r.actions][:2], ["./tsconfig.json", "./tsconfig.base.json"])
        self.assertEqual(r.subject["option_for_action"], {"opt1": 0, "opt2": 1})

    def test_login_is_an_auth_card_with_a_local_sign_in_and_no_keys(self):
        for gate, state in (("login", "blocked-human"), (None, "logged-out")):
            r = req(gate, LOGIN if gate else None, state=state)
            self.assertEqual(r.kind, "auth")
            self.assertEqual(r.title, "Claude needs to be reconnected")
            self.assertEqual([a.kind for a in r.actions], ["authenticate", "open_terminal"])
            self.assertIsNone(G.keys_for(clicked(r, "authenticate"), LOGIN if gate else None, state))

    def test_press_enter_is_a_confirmation_that_continues(self):
        r = req("press-enter", PRESS)
        self.assertEqual(r.kind, "confirmation")
        self.assertEqual([a.id for a in r.actions], ["continue", "open_terminal"])
        self.assertEqual(G.keys_for(clicked(r, "continue"), PRESS, "blocked-human"), ["Enter"])

    def test_an_unparseable_or_paying_dialog_keeps_the_blocked_card_with_no_guessed_button(self):
        for gate, prompt in (("fable-limit-unfocused", FABLE), ("unknown", "  something odd\n  >")):
            r = req(gate, prompt)
            self.assertEqual(r.kind, G.FALLBACK_KIND, gate)
            self.assertEqual([a.id for a in r.actions], ["open_terminal"], gate)
            self.assertEqual(r.message, FALLBACK, gate)

    def test_kind_follows_the_options_not_the_gate_label(self):
        """classify() calls a real permission dialog `selection` (its caret row is
        nearest the bottom); the buttons must still be No/Yes. And a gate labelled
        permission whose options are not yes/no is a choice, one button per option."""
        self.assertEqual(req("selection", PERM).kind, "permission")
        self.assertEqual([a.label for a in req("selection", PERM).actions], ["No", "Yes", "Open terminal"])
        r = req("permission", "  Proceed?\n  ❯ 1. Continue\n    2. Abort")
        self.assertEqual(r.kind, "choice")
        self.assertEqual([a.label for a in r.actions], ["Continue", "Abort", "Open terminal"])

    def test_the_guard_is_session_plus_dialog(self):
        self.assertEqual(req("permission", PERM).guard, req("permission", PERM).guard)
        self.assertNotEqual(req("permission", PERM).guard, req("permission", PERM, session="t").guard)
        self.assertNotEqual(req("permission", PERM).guard, req("permission", SELECT).guard)


class TestKeys(unittest.TestCase):
    def test_yes_with_the_caret_already_on_yes_is_just_enter(self):
        self.assertEqual(G.keys_for(clicked(req("permission", PERM), "allow"), PERM, "blocked-human"),
                         ["Enter"])

    def test_no_walks_the_caret_down_to_no(self):
        self.assertEqual(G.keys_for(clicked(req("permission", PERM), "deny"), PERM, "blocked-human"),
                         ["Down", "Down", "Enter"])

    def test_yes_walks_the_caret_up_when_it_sits_on_no(self):
        r = clicked(req("permission", PERM_CARET_ON_NO), "allow")
        self.assertEqual(G.keys_for(r, PERM_CARET_ON_NO, "blocked-human"), ["Up", "Up", "Enter"])

    def test_a_click_against_a_dialog_that_changed_sends_nothing(self):
        """The most important check (owner): the card showed prompt A; by the click
        the terminal shows prompt B. Enter into B is a decision nobody made."""
        r = clicked(req("permission", PERM), "allow")
        self.assertIsNone(G.keys_for(r, SELECT, "blocked-human"))
        self.assertIsNone(G.keys_for(r, PERM.replace("possibly-empty", "non-empty"), "blocked-human"))
        self.assertIsNone(G.keys_for(r, None, "idle-ready"))

    def test_no_caret_on_screen_means_no_navigation_is_attempted(self):
        flat = PERM.replace("  ❯ 1. Yes", "    1. Yes")
        self.assertIsNone(G.keys_for(clicked(req("permission", flat), "deny"), flat, "blocked-human"))

    def test_an_unknown_action_sends_nothing(self):
        self.assertIsNone(G.keys_for(clicked(req("permission", PERM), "open_terminal"), PERM, "blocked-human"))


TWO_DIALOGS = ("  Do you want to proceed?\n"
               "    1. Yes\n"
               "    2. No\n"
               "  ✓ Yes\n"
               "  Select a deployment\n"
               "  ❯ 1. Production\n"
               "    2. Staging\n"
               "  Esc to cancel")
TRUST = "  Do you trust the files in this folder?\n  ❯ 1. Yes, proceed\n    2. No, exit"
BYPASS = "  in Bypass Permissions mode.\n  ❯ 1. No, exit\n    2. Yes, I accept\n  Enter to confirm"


class TestLiveDialogOnly(unittest.TestCase):
    """Reviewer P1: a resolved dialog still visible above the live one must not
    feed the buttons, even though the prompt fingerprint is identical."""

    def test_only_the_block_with_the_caret_is_parsed(self):
        self.assertEqual(G.parse_options(TWO_DIALOGS), (["Production", "Staging"], 0))

    def test_the_card_is_built_from_the_live_dialog(self):
        r = req("selection", TWO_DIALOGS)
        self.assertEqual(r.kind, "choice")
        self.assertEqual([a.label for a in r.actions], ["Production", "Staging", "Open terminal"])
        self.assertEqual(r.message, "Select a deployment")
        self.assertEqual(G.keys_for(clicked(r, "opt2"), TWO_DIALOGS, "blocked-human"), ["Down", "Enter"])

    def test_no_caret_anywhere_falls_back_to_the_last_block(self):
        flat = TWO_DIALOGS.replace("  ❯ 1. Production", "    1. Production")
        self.assertEqual(G.parse_options(flat), (["Production", "Staging"], None))


class TestNeverAOneClickOnATrustOrSpendGate(unittest.TestCase):
    """Reviewer blocker: a numbered rendering of a trust / bypass / credit dialog
    must still keep the blocked card, whatever its options say."""

    def test_trust_bypass_fable_and_session_limit_keep_the_blocked_card(self):
        for gate, prompt in (("folder-trust", TRUST), ("bypass-permissions", BYPASS),
                             ("fable-limit-unfocused", FABLE), ("fable-limit", FABLE),
                             ("session-limit", "  You've hit your usage limit\n  ❯ 1. Upgrade\n    2. Wait")):
            r = req(gate, prompt)
            self.assertEqual(r.kind, G.FALLBACK_KIND, gate)
            self.assertEqual([a.id for a in r.actions], ["open_terminal"], gate)
            self.assertIsNone(G.keys_for(clicked(r, "opt1"), prompt, "blocked-human"), gate)

    def test_the_allowlist_is_the_only_way_in(self):
        self.assertEqual(G.SEMANTIC_GATES, {"permission", "selection", "press-enter", "login"})


if __name__ == "__main__":
    unittest.main()
