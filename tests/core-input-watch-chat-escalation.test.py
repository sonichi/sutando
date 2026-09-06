#!/usr/bin/env python3
"""A blocked core raises a HumanRequirement, so it reaches the owner as a card.

ESCALATE was a banner in the Runtime window, so learning the core was stuck
required already looking at that window (owner report 2026-09-03, after a
weekly model-limit prompt held the core all morning).

`src/hitl` already owns requirement dedup and card projection; the monitor is
the driver it never had. These tests pin what the monitor contributes: the
right states, an episode key the Manager can dedup on, resolution when the
core moves on, and never dying of its own escalation.
"""
import importlib.util as u
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.schema import STATUS_RESOLVED  # noqa: E402


def _mgr():
    return HitlManager(HitlStore(pathlib.Path(tempfile.mkdtemp())))


class TestMessage(unittest.TestCase):
    def test_it_names_the_gate_and_quotes_the_terminal(self):
        b = M.escalation_message("blocked-human", "awaiting user: selection",
                                 "selection", "Pick one:\n> a\n  b")
        self.assertIn("cannot continue without you", b)
        self.assertIn("selection", b)
        self.assertIn("Pick one:", b)

    def test_an_unknown_gate_is_not_rendered_as_a_gate_name(self):
        """`unknown` is the classifier saying it could not tell, so `Gate:
        unknown` reads as a fact about the prompt rather than about us."""
        b = M.escalation_message("blocked-human", "d", "unknown", "x")
        self.assertNotIn("Gate: unknown", b)

    def test_logged_out_says_signed_out_not_blocked(self):
        self.assertIn("signed out",
                      M.escalation_message("logged-out", "d", None, None))

    LOGIN_PANE = (
        "───────────────────────────────── sutando-core ─\n"
        "Browser didn't open? Use the url below to sign in:\n"
        "https://claude.ai/oauth/authorize?code=true&client_id=abc&scope=user%3Aprofile+user%3A\n"
        "s%3Aclaude_code+user%3Amcp_servers&code_challenge=ncifI5jOgzI138TpX&state=uvUrUTS4WTI2FL\n"
        "Paste code here if prompted > \n"
        "Esc to cancel\n"
        "  ⏵⏵ bypass permissions on · 1 monitor · esc to interrupt · ← for agents\n"
        "[sutando-c0:2.1.261* 2:monitor                       \"✳ sutando-core\" 00:55 06-Sep-26\n"
    )

    def test_the_excerpt_is_the_prompt_not_the_chrome_around_it(self):
        """The owner saw a card whose 'terminal' was the tail of an OAuth URL plus a rule line
        that ran past the card (2026-09-06): the pane's last six lines are not the prompt."""
        b = M.escalation_message("blocked-human", "awaiting user: login", "login", self.LOGIN_PANE)
        self.assertIn("Paste code here if prompted", b)
        self.assertIn("Esc to cancel", b)
        self.assertIn("Use the url below to sign in", b)
        for noise in ("code_challenge", "%3A", "https://", "───", "bypass permissions", "2:monitor"):
            self.assertNotIn(noise, b, noise)

    def test_the_excerpt_is_plain_lines_not_a_code_fence(self):
        """The room card renders text, so a markdown fence arrives as literal backticks."""
        b = M.escalation_message("blocked-human", "d", "selection", "Pick one:\n> a\n  b")
        self.assertNotIn("```", b)
        self.assertIn("  Pick one:", b)

    def test_an_all_chrome_pane_leaves_no_excerpt_section(self):
        b = M.escalation_message("blocked-human", "d", "unknown",
                                 "──────── x ────────\nhttps://example.test/a?b=1\n")
        self.assertNotIn("What the terminal is showing", b)


class TestEscalate(unittest.TestCase):
    def test_it_raises_one_requirement_with_an_actionable_card(self):
        m = _mgr()
        req = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "sutando-core")
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, M.HITL_KIND)
        self.assertTrue(req.actions, "a card with no action cannot be answered")
        self.assertIn("Pick one:", req.message)

    def test_the_same_prompt_held_for_many_ticks_raises_one_card(self):
        m = _mgr()
        ids = {M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s").id
               for _ in range(40)}
        self.assertEqual(len(ids), 1)

    def test_a_different_prompt_is_a_different_card(self):
        """Two things needing the owner are two cards, even back to back —
        the second is not a redraw of the first."""
        m = _mgr()
        a = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        b = M.escalate(m, "blocked-human", "d", "login", "Log in:", "s")
        self.assertNotEqual(a.id, b.id)

    def test_the_prompt_is_the_episode_key_not_the_state(self):
        """Guard is derived from the prompt: keying on state alone would
        collapse two unrelated blocks into one card."""
        m = _mgr()
        a = M.escalate(m, "blocked-human", "d", "selection", "A", "s")
        b = M.escalate(m, "blocked-human", "d", "selection", "B", "s")
        self.assertNotEqual(a.guard, b.guard)

    def test_no_manager_is_survivable(self):
        """The monitor must outlive its own escalation: if this raised, the
        layer that DETECTS the block would die with the block unreported."""
        self.assertIsNone(M.escalate(None, "blocked-human", "d", "k", "p", "s"))


class TestResolve(unittest.TestCase):
    def test_leaving_the_blocked_set_resolves_the_card(self):
        m = _mgr()
        req = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        self.assertEqual(M.resolve_escalations(m, "s"), [req.id])
        self.assertEqual(m.get(req.id).status, STATUS_RESOLVED)

    def test_it_resolves_only_its_own_kind(self):
        """A permission card belongs to the hook driver; clearing it because
        the CORE unblocked would dismiss a question nobody answered."""
        m = _mgr()
        from hitl.schema import Action, HumanRequirement
        other = m.create(HumanRequirement(kind="permission", runtime="claude",
                                          message="may I", guard="g1",
                                          actions=[Action("allow", "allow", "Allow")]))
        M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        self.assertNotIn(other.id, M.resolve_escalations(m, "s"))
        self.assertNotEqual(m.get(other.id).status, STATUS_RESOLVED)

    def test_it_resolves_only_its_own_session(self):
        """A pool shares one login, so a weekly limit blocks every worker on
        byte-identical prompt text. One worker recovering must not clear a
        sibling's card, and two blocked workers are two cards, not one."""
        m = _mgr()
        P = "You've reached your Fable limit"
        a = M.escalate(m, "blocked-human", "d", "fable-limit-unfocused", P, "worker-1")
        b = M.escalate(m, "blocked-human", "d", "fable-limit-unfocused", P, "worker-2")
        self.assertNotEqual(a.id, b.id, "two blocked workers collapsed to one card")
        self.assertEqual(len(m.active()), 2)
        self.assertEqual(M.resolve_escalations(m, "worker-1"), [a.id])
        self.assertEqual([r.id for r in m.active()], [b.id],
                         "worker-1 recovering cleared worker-2's card")

    def test_a_second_block_after_recovery_raises_again(self):
        m = _mgr()
        first = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        M.resolve_escalations(m, "s")
        again = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        self.assertNotEqual(first.id, again.id)


class TestScope(unittest.TestCase):
    def test_states_with_an_automatic_remedy_are_not_escalated(self):
        """`blocked-known` is auto-answered and `hung` is RECOVER's job; both
        resolve without the owner, so neither is theirs to be woken for."""
        for s in ("blocked-known", "hung", "crashed", "running", "idle-ready"):
            self.assertNotIn(s, M._CHAT_ESCALATE_STATES, s)


class TestDegradesWithoutDyingV(unittest.TestCase):
    """The monitor's contract is that it keeps writing its state file whatever
    the card layer does. Every branch below is a failure the card layer can
    have; none of them may reach the caller as an exception."""

    def test_it_puts_src_on_the_path_when_it_is_missing(self):
        """The monitor is executed as a script, so `src` is not importable
        unless it puts itself there."""
        removed = [p for p in sys.path if p == str(SRC)]
        for p in removed:
            sys.path.remove(p)
        try:
            self.assertNotIn(str(SRC), sys.path, "control: src really was removed")
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "state", "core-input.json")
                self.assertIsNotNone(M._hitl_manager(out))
            self.assertIn(str(SRC), sys.path, "it must re-insert src to import hitl")
        finally:
            for p in removed:
                if p not in sys.path:
                    sys.path.insert(0, p)

    def test_no_hitl_package_yields_no_manager_and_no_exception(self):
        """`src/hitl` is an optional tier: a standalone checkout without it
        must still run the monitor, minus the card."""
        saved = sys.modules.get("hitl.manager", "absent")
        sys.modules["hitl.manager"] = None  # makes `from hitl.manager import ...` raise
        try:
            with tempfile.TemporaryDirectory() as d:
                self.assertIsNone(M._hitl_manager(os.path.join(d, "state", "s.json")))
        finally:
            if saved == "absent":
                del sys.modules["hitl.manager"]
            else:
                sys.modules["hitl.manager"] = saved

    def test_a_raising_store_does_not_take_down_the_escalation(self):
        class Boom:
            def create(self, req):
                raise RuntimeError("store on fire")

        self.assertIsNone(
            M.escalate(Boom(), "blocked-human", "d", "selection", "Pick one:", "s1"))

    def test_resolution_without_a_manager_is_empty_not_an_error(self):
        self.assertEqual(M.resolve_escalations(None, "s1"), [])

    def test_a_raising_store_does_not_take_down_resolution(self):
        class Boom:
            def active(self):
                raise RuntimeError("store on fire")

        self.assertEqual(M.resolve_escalations(Boom(), "s1"), [])

    def test_escalate_without_a_manager_is_none_not_an_error(self):
        self.assertIsNone(
            M.escalate(None, "blocked-human", "d", None, None, "s1"))

    def test_a_store_that_cannot_be_CONSTRUCTED_is_as_optional_as_a_missing_import(self):
        """The import sat inside the fail-open boundary and the construction did
        not, so an unbuildable store killed the monitor before its first tick."""
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "state").mkdir()
            (ws / "state" / "hitl").write_text("a regular file, not a directory")
            self.assertIsNone(M._hitl_manager(str(ws / "state" / "core-status.json")))

    def test_the_healthy_path_still_returns_a_manager(self):
        """Control: without it the test above passes on a function that always
        returns None, which is not the behaviour being pinned."""
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "state").mkdir()
            self.assertIsNotNone(M._hitl_manager(str(ws / "state" / "core-status.json")))


PERM = ("  Dangerous rm operation on a possibly-empty path\n  Do you want to proceed?\n"
        "  ❯ 1. Yes\n    2. Yes, and don't ask again for rm commands\n    3. No\n  Esc to cancel")


def _click(m, r, action_id):
    from hitl.schema import ActionReply
    m.apply_action(ActionReply(hitl_id=r.id, expected_revision=r.revision,
                               action_id=action_id, guard=r.guard))


class TestDrive(unittest.TestCase):
    """A click on the card is realised as keystrokes into the dialog it was made
    for — once, only by a human, only while that dialog is still on screen."""

    def _blocked(self, m, session="s"):
        return M.escalate(m, "blocked-human", "awaiting user: permission", "permission", PERM, session)

    def test_the_real_dialog_through_classify_is_a_no_yes_card(self):
        """Through the monitor's own classifier, not a hand-picked gate label: the
        live pane names this dialog `selection`, and the card must still be No/Yes."""
        state, detail, prompt, kind = M.compose_state(PERM, "working", True)
        self.assertEqual((state, kind), ("blocked-human", "selection"))
        r = M.escalate(_mgr(), state, detail, kind, prompt, "s")
        self.assertEqual(r.kind, "permission")
        self.assertEqual([a.label for a in r.actions], ["No", "Yes", "Open terminal"])

    def test_a_numbered_trust_dialog_through_classify_gets_no_button(self):
        """End to end through the monitor's own classifier: this pane is labelled
        `selection` (caret row nearest the bottom) and must still not offer Yes."""
        TRUST = ("  Do you trust the files in this folder?\n  ❯ 1. Yes, proceed\n"
                 "    2. No, exit\n  Enter to confirm · Esc to cancel")
        state, detail, prompt, kind = M.compose_state(TRUST, "working", True)
        self.assertEqual(kind, "selection", "fixture no longer reproduces the label collision")
        r = M.escalate(_mgr(), state, detail, kind, prompt, "s")
        self.assertEqual(r.kind, "core-blocked")
        self.assertEqual([a.label for a in r.actions], ["Open terminal"])

    def test_a_permission_gate_carries_no_yes_buttons_not_ack(self):
        r = self._blocked(_mgr())
        self.assertEqual(r.kind, "permission")
        self.assertEqual([a.label for a in r.actions], ["No", "Yes", "Open terminal"])

    def test_a_clicked_no_walks_the_caret_to_no_and_confirms(self):
        m, sent = _mgr(), []
        r = self._blocked(m)
        _click(m, r, "deny")
        acted = M.drive_escalations(m, "s", PERM, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, ["Down", "Down", "Enter"])
        self.assertEqual(acted, [(r.id, ["Down", "Down", "Enter"])])

    def test_a_click_is_driven_once_not_every_tick(self):
        m, sent = _mgr(), []
        r = self._blocked(m)
        _click(m, r, "allow")
        for _ in range(5):
            M.drive_escalations(m, "s", PERM, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, ["Enter"])

    def test_a_click_against_a_changed_dialog_sends_nothing_and_expires_the_card(self):
        from hitl.schema import STATUS_EXPIRED
        m, sent = _mgr(), []
        r = self._blocked(m)
        _click(m, r, "allow")
        other = PERM.replace("possibly-empty", "non-empty")
        M.drive_escalations(m, "s", other, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, [])
        self.assertEqual(m.get(r.id).status, STATUS_EXPIRED)
        self.assertEqual(m.get(r.id).answer, {"note": "This prompt has changed. Refresh the action."})

    def test_an_unclicked_card_is_never_driven(self):
        m, sent = _mgr(), []
        self._blocked(m)
        M.drive_escalations(m, "s", PERM, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, [])

    def test_another_sessions_click_is_not_driven_into_this_pane(self):
        m, sent = _mgr(), []
        r = self._blocked(m, session="worker-2")
        _click(m, r, "allow")
        M.drive_escalations(m, "worker-1", PERM, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, [])

    def test_a_policy_decision_is_never_typed_into_a_human_gate(self):
        from hitl.manager import POLICY_DECIDER
        m, sent = _mgr(), []
        r = self._blocked(m)
        _click(m, r, "allow")
        with m.store.locked():
            cur = m.get(r.id); cur.decided_by = POLICY_DECIDER; m.store.save(cur)
        M.drive_escalations(m, "s", PERM, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, [])

    def test_a_hook_driver_permission_card_is_not_resolved_by_the_core_moving(self):
        """TUI cards can now be kind=permission too; scope is the source, not the kind."""
        m = _mgr()
        from hitl.schema import Action, HumanRequirement
        hook = m.create(HumanRequirement(kind="permission", runtime="claude", message="may I",
                                         guard="hook:1", device={"id": "s", "name": "s"},
                                         subject={"session": "s", "tool": "Bash"},
                                         actions=[Action("allow", "allow_once", "Allow")]))
        mine = self._blocked(m)
        self.assertEqual(M.resolve_escalations(m, "s"), [mine.id])
        self.assertNotEqual(m.get(hook.id).status, STATUS_RESOLVED)

    def test_no_manager_is_survivable_for_the_driver_too(self):
        self.assertEqual(M.drive_escalations(None, "s", PERM, "blocked-human", lambda k: True), [])


class TestPartialSend(unittest.TestCase):
    """Reviewer P1: a key sequence that half-lands must never read as driven, and
    must never be finished later against a caret nobody can place."""

    def _blocked(self, m):
        return M.escalate(m, "blocked-human", "awaiting user: permission", "permission", PERM, "s")

    def test_a_refused_second_key_expires_the_card_and_records_only_what_went_in(self):
        from hitl.schema import STATUS_EXPIRED
        m, sent = _mgr(), []
        r = self._blocked(m)
        _click(m, r, "deny")                      # plan: Down, Down, Enter
        def flaky(k):
            sent.append(k)
            return len(sent) < 2                  # first Down accepted, second refused
        acted = M.drive_escalations(m, "s", PERM, "blocked-human", flaky)
        self.assertEqual(sent, ["Down", "Down"])
        self.assertEqual(acted, [(r.id, None)])
        cur = m.get(r.id)
        self.assertEqual(cur.status, STATUS_EXPIRED)
        self.assertEqual(cur.subject["driven_keys"], ["Down"])
        self.assertTrue(cur.subject["driven_partial"])
        self.assertEqual(cur.answer, {"note": "The answer did not take. Open the terminal to finish it."})

    def test_after_a_partial_send_nothing_is_ever_typed_again_for_that_card(self):
        m, sent = _mgr(), []
        r = self._blocked(m)
        _click(m, r, "deny")
        M.drive_escalations(m, "s", PERM, "blocked-human", lambda k: sent.append(k) or len(sent) < 2)
        moved = PERM.replace("  ❯ 1. Yes", "    1. Yes").replace("    2. Yes, and", "  ❯ 2. Yes, and")
        for _ in range(3):
            M.drive_escalations(m, "s", moved, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, ["Down", "Down"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
