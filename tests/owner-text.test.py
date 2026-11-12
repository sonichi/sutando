#!/usr/bin/env python3
"""Text the owner reads names the agent and the cause in one plain line; ids, session names,
paths, state codes and key fragments never reach a title, a first line, a reminder or a
text-only fallback. Pinned as properties of the text, not as wording."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import owner_text
from hitl import renotify, tui_gate
from hitl.manager import HitlManager, HitlStore
from hitl.projector import card_wire, fallback_body, project
from hitl.schema import NOTICE_FIELD, WIRE_FIELD, Action, HumanRequirement

_spec = importlib.util.spec_from_file_location("core_input_watch_owner_text", REPO / "src" / "core-input-watch.py")
ciw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ciw)

HEX = "7c700e11a8d94ea9b86f52d70134ac68"
SESSION = f"sutando-worker-{HEX}"
SOCKET = "/tmp/owner-text-test/pane.sock"
KEY = "sk-ant-api03-" + "a1B2" * 8
LEAKS = (HEX, HEX[:8], SESSION, "sutando-core", SOCKET, "tmux", "blocked-human", "sk-", "Bearer ey")
T0 = 1_800_000_000.0


def plain(case, text):
    case.assertEqual(owner_text.internals(text), [], text)
    for leak in LEAKS:
        case.assertNotIn(leak, text)


class Redaction(unittest.TestCase):
    def test_key_like_fragments_are_replaced_and_the_rest_is_kept(self):
        got = owner_text.redact(f"401 from the API: {KEY}, header Bearer eyJhbGciOi.payload.sig, "
                                "X-Api-Key=hunter2secret, retry in 5s")
        for leak in (KEY, "sk-ant", "eyJhbGciOi", "hunter2secret"):
            self.assertNotIn(leak, got)
        self.assertIn("401 from the API", got)
        self.assertIn("retry in 5s", got)
        self.assertIn("X-Api-Key=[redacted]", got)

    def test_a_short_sk_fragment_and_a_provider_signature_both_go(self):
        self.assertEqual(owner_text.redact("key sk-abc123 ends"), "key [redacted] ends")
        self.assertNotIn("ghp_", owner_text.redact("token ghp_" + "a" * 36))
        self.assertEqual(owner_text.redact("api_key=sk-proj-" + "a" * 40), "api_key=[redacted]")
        self.assertEqual(owner_text.redact("AKIA" + "A" * 16), "[REDACTED-AWS Access Key]")

    def test_ordinary_text_and_none_are_untouched(self):
        for text in ("Reviewer has a task that wasn't delivered for 15 minutes.", "risk-free task", "key=ab"):
            self.assertEqual(owner_text.redact(text), text)
        self.assertEqual(owner_text.redact(None), "")


class Shapes(unittest.TestCase):
    def test_each_internal_shape_is_named(self):
        cases = {f"worker Reviewer ({HEX[:8]}…)": ["hex-id"], f"session {SESSION}": ["hex-id", "session"],
                 "tmux attach": ["session"], f"socket {SOCKET}": ["path"], "state blocked-human": ["state-code"],
                 "see task-fcc45a4ac1": ["hex-id", "record-id"], "card hitl_ab12": ["record-id"], "run python3 src/x.py": ["command"],
                 f"auth {KEY}": ["key"]}
        for text, want in cases.items():
            self.assertEqual(owner_text.internals(text), want, text)

    def test_what_a_person_reads_carries_none(self):
        for text in ("Reviewer has a task that wasn't delivered for 15 minutes.",
                     "It resets at 3:40 PM and it continues by itself then.", "Still blocked after 1h 27m",
                     "See https://example.com/a/b for the page", "Merged on 2026-11-11", None):
            self.assertEqual(owner_text.internals(text), [], text)

    def test_a_name_drops_a_bracketed_id_and_an_identifier_is_no_name(self):
        self.assertEqual(owner_text.named(f"Reviewer ({HEX[:8]}…)"), "Reviewer")
        self.assertEqual(owner_text.named("Reviewer (night shift)"), "Reviewer (night shift)")
        self.assertEqual(owner_text.named(SESSION), "Your agent")
        self.assertEqual(owner_text.named("", "A worker"), "A worker")

    def test_card_text_puts_the_sentence_first_and_redacts_the_details(self):
        text = owner_text.card_text("Reviewer is signed out.", ["  raw: " + KEY, "", None, f"Session: {SESSION}"])
        self.assertEqual(text.split("\n\n")[0], "Reviewer is signed out.")
        self.assertEqual(text.split("\n\n")[1].splitlines(), ["  raw: [redacted]", f"Session: {SESSION}"])
        self.assertEqual(owner_text.card_text("Only a sentence."), "Only a sentence.")

    def test_the_text_only_form_is_the_first_line_of_a_folded_card_and_all_of_any_other(self):
        folded = owner_text.card_text("Reviewer is signed out.", [f"Session: {SESSION}"])
        self.assertEqual(owner_text.fallback_text(folded, True), "Reviewer is signed out.")
        self.assertEqual(owner_text.fallback_text("Merge it?\n\nOptions below " + KEY, False),
                         "Merge it?\n\nOptions below [redacted]")
        self.assertEqual(owner_text.owner_line(f"{SESSION} · blocked-human"), owner_text.GENERIC_LINE)
        self.assertEqual(owner_text.owner_line("", "nothing"), "nothing")

    def test_a_reset_time_reads_as_a_clock(self):
        now = time.mktime((2026, 11, 11, 9, 0, 0, 0, 0, -1))
        later = time.mktime((2026, 11, 11, 15, 40, 0, 0, 0, -1))
        self.assertEqual(owner_text.clock(later, now), "3:40 PM")
        self.assertEqual(owner_text.clock(time.mktime((2026, 11, 12, 0, 5, 0, 0, 0, -1)), now), "Nov 12, 12:05 AM")
        self.assertEqual(owner_text.resets(later, now), "It resets at 3:40 PM.")
        for unknown in (None, True, "soon", now - 1):
            self.assertEqual(owner_text.resets(unknown, now), "")
        self.assertTrue(owner_text.clock(time.time() + 60))


class AgentName(unittest.TestCase):
    """The name on a card comes from local records: the primary connection row, then the environment."""

    def test_the_connection_row_wins_then_the_environment_then_the_default(self):
        import agent_local_name
        import connections
        row = {"agent_display_name": "Blue Fox", "primary": True}
        with patch.object(connections, "primary_row", return_value=row):
            self.assertEqual(agent_local_name.local_name(environ={"AGENT_DISPLAY_NAME": "Env Name"}), "Blue Fox")
        with patch.object(connections, "primary_row", return_value=None):
            self.assertEqual(agent_local_name.local_name(environ={"AGENT_DISPLAY_NAME": "Env Name"}), "Env Name")
            self.assertEqual(agent_local_name.local_name(environ={"SUTANDO_DISPLAY_NAME": "First",
                                                                  "AGENT_DISPLAY_NAME": "Second"}), "First")
            self.assertEqual(agent_local_name.local_name(environ={}), "Your agent")

    def test_an_identifier_or_an_unreadable_record_is_no_name(self):
        import agent_local_name
        import connections
        with patch.object(connections, "primary_row", return_value={"agent_display_name": SESSION}):
            self.assertEqual(agent_local_name.local_name("An agent", environ={}), "An agent")
        with patch.object(connections, "primary_row", side_effect=RuntimeError("unreadable")):
            self.assertEqual(agent_local_name.local_name(environ={"AGENT_DISPLAY_NAME": "Env Name"}), "Env Name")
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(agent_local_name.local_name(workspace=Path(empty), environ={}), "Your agent")


class BlockedCard(unittest.TestCase):
    """The agent-is-blocked card: its title is the agent's name and a plain state."""

    def manager(self):
        return HitlManager(HitlStore(Path(tempfile.mkdtemp(prefix="owner-text-"))))

    def raised(self, state="blocked-human", kind="unknown", prompt="Proceed with the plan?", seat=None, **kw):
        with patch.object(ciw.agent_local_name, "local_name", return_value="Blue Fox"):
            return ciw.escalate(self.manager(), state, "awaiting user", kind, prompt, "sutando-core",
                                seat=seat, **kw)

    def test_title_first_line_and_buttons_are_plain_for_every_state(self):
        cases = (("blocked-human", "unknown", "Proceed with the plan?"),
                 ("blocked-human", "usage-limit", "You've hit your usage limit · resets 3pm"),
                 ("logged-out", None, None))
        for state, kind, prompt in cases:
            for seat in (None, "Reviewer"):
                req = self.raised(state, kind, prompt, seat=seat)
                who = seat or "Blue Fox"
                with self.subTest(state=state, kind=kind, seat=seat):
                    for text in [req.title, req.message.splitlines()[0]] + [a.label for a in req.actions]:
                        plain(self, text)
                    if req.kind == "core-blocked":
                        self.assertTrue(req.title.startswith(who + " "), req.title)
                        self.assertTrue(req.message.startswith(who + " "), req.message)
                    self.assertEqual(req.device, {"id": "sutando-core", "name": who})
                    self.assertEqual(req.subject["session"], "sutando-core")

    def test_the_state_code_the_session_and_the_terminal_lines_are_details(self):
        req = self.raised(prompt=f"Proceed with the plan?\nAuthorization: Bearer {KEY}")
        head, _, details = req.message.partition("\n\n")
        self.assertEqual(head, "Blue Fox is waiting on a question in its terminal and cannot continue until you answer.")
        self.assertEqual(req.title, "Blue Fox is waiting for you")
        for line in ("What its terminal shows:", "  Proceed with the plan?",
                     "State: blocked-human — awaiting user", "Session: sutando-core"):
            self.assertIn(line, details.splitlines())
        self.assertNotIn(KEY, req.message)
        self.assertTrue(req.subject[owner_text.FOLDED])

    def test_a_usage_limit_gives_the_reset_time_when_the_terminal_states_it(self):
        now = time.mktime((2026, 11, 11, 9, 0, 0, 0, 0, -1))
        body = ciw.escalation_message("blocked-human", "d", "usage-limit",
                                      "You've hit your usage limit · resets 3:40pm", "s", "claude", "Blue Fox", now)
        self.assertEqual(body.splitlines()[0], "Blue Fox has reached its usage limit. "
                                               "It resets at 3:40 PM and it continues by itself then.")
        unknown = ciw.escalation_message("blocked-human", "d", "usage-limit", "You've hit your usage limit",
                                         "s", "claude", "Blue Fox", now)
        self.assertEqual(unknown.splitlines()[0],
                         "Blue Fox has reached its usage limit and continues by itself when it resets.")
        with patch.dict(sys.modules, {"core_limit_hold": None}):
            self.assertIsNone(ciw._limit_reset("resets 3pm", now))
        self.assertEqual(tui_gate.blocked_title(SESSION, limit=True), "Your agent has reached its usage limit")

    def test_a_queued_count_stays_below_the_first_line(self):
        req = self.raised(queued=2)
        self.assertNotIn("queued", req.message.splitlines()[0])
        self.assertIn("2 queued task(s)", req.message)

    def test_a_card_with_buttons_keeps_its_question_whole_for_a_text_reader(self):
        req = self.raised(kind="selection", prompt="Pick one:\n❯ 1. Stop and wait\n  2. Switch\n")
        self.assertEqual(req.kind, "choice")
        self.assertNotIn(owner_text.FOLDED, req.subject)


def blocked(message, title="", device=None, subject=None, kind="core-blocked", created=T0):
    return HumanRequirement(kind=kind, runtime="claude", message=message, title=title, guard="g",
                            device=device or {"id": SESSION, "name": "Reviewer"}, subject=subject or {},
                            actions=[Action(id="open_terminal", kind="open_terminal", label="Open terminal")],
                            created_at=created, updated_at=created)


class Notices(unittest.TestCase):
    """The follow-up messages: one plain sentence each."""

    NEW = blocked(owner_text.card_text("Reviewer has a task that wasn't delivered for 15 minutes.",
                                       [f"Worker id: {HEX}", f"Session: {SESSION}"]),
                  "Reviewer has an undelivered task", subject={"cleared": "It has it now."})
    OLD = blocked(f"worker Reviewer ({HEX[:8]}…) (tmux session {SESSION}) owes work",
                  "sutando-core · blocked-human", device={"id": "sutando-core", "name": "sutando-core"})

    def test_a_reminder_names_no_session_id_or_state_code(self):
        for req in (self.NEW, self.OLD):
            body = renotify.body(renotify.REMINDER, req, T0 + 31 * 60, queued=2)
            plain(self, body)
            self.assertIn("Still blocked after 31m", body)
            self.assertIn("2 tasks are waiting", body)
        self.assertIn("Reviewer has a task that wasn't delivered for 15 minutes",
                      renotify.body(renotify.REMINDER, self.NEW, T0 + 31 * 60))
        self.assertIn(owner_text.GENERIC_LINE, renotify.body(renotify.REMINDER, self.OLD, T0 + 31 * 60))

    def test_a_recovery_says_what_cleared_in_the_cards_own_words(self):
        self.assertEqual(renotify.body(renotify.RECOVERY, self.NEW, T0),
                         "✓ Reviewer has a task that wasn't delivered for 15 minutes. It has it now.")
        old = renotify.body(renotify.RECOVERY, self.OLD, T0)
        plain(self, old)
        self.assertEqual(old, "✓ No longer blocked — Your agent can continue; Sutando has continued its work.")

    def test_a_sign_in_recovery_names_a_seat_only_by_a_display_name(self):
        for name, want in (("Reviewer", "✓ Signed in (Reviewer) — Sutando has continued its work."),
                           (SESSION, "✓ Signed in — Sutando has continued its work.")):
            req = blocked("Claude needs you to sign in again", kind="auth", device={"id": SESSION, "name": name})
            self.assertEqual(renotify.body(renotify.RECOVERY, req, T0), want)

    def test_a_card_that_could_not_be_shown_is_announced_plainly(self):
        for req in (self.NEW, self.OLD):
            body = renotify.body(renotify.FAILED, req, T0)
            plain(self, body)
            self.assertTrue(body.startswith("⚠ Sutando could not show you a card — "), body)
        self.assertEqual(renotify._headline(blocked("", title="")), owner_text.GENERIC_LINE)


class Projection(unittest.TestCase):
    def setUp(self):
        self.mgr = HitlManager(HitlStore(Path(tempfile.mkdtemp(prefix="owner-text-proj-"))))
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return {"ok": True, "event_id": f"$e{len(self.sent)}"}

    def test_a_folded_card_sends_its_first_line_as_text_and_everything_on_the_card(self):
        req = self.mgr.create(blocked(
            owner_text.card_text("Reviewer is signed out, so every task sent to it waits.",
                                 ["Its terminal shows:", f"  API Error: 401 {KEY}", f"Session: {SESSION}"]),
            "Reviewer is signed out", subject={owner_text.FOLDED: True}))
        project(self.mgr, self.send, "!room:example.com", clock=lambda: T0)
        [payload] = self.sent
        plain(self, payload["body"])
        self.assertIn("Reviewer is signed out, so every task sent to it waits.", payload["body"])
        self.assertNotIn("· Reviewer)", payload["body"], "the name is not repeated as a device")
        wire = payload["extra_content"][WIRE_FIELD]
        self.assertIn(f"Session: {SESSION}", wire["message"], "the details stay on the card")
        self.assertNotIn(KEY, wire["message"])
        self.assertEqual(wire["device"]["id"], SESSION)
        self.assertEqual(wire["id"], req.id)

    def test_a_decision_keeps_its_whole_text_and_an_identifier_is_not_a_device_name(self):
        ask = blocked("Merge the fix?\n\nIt is green.", "Question", kind="choice",
                      device={"id": "sutando-core", "name": "sutando-core", "host": "studio"})
        body = fallback_body(ask)
        self.assertIn("Merge the fix?\n\nIt is green.", body)
        self.assertTrue(body.endswith("(on studio)"), body)
        named = blocked("Merge the fix?", kind="choice", device={"id": "c", "name": "Blue Fox", "host": "studio"})
        self.assertTrue(fallback_body(named).endswith("(on studio · Blue Fox)"))

    def test_the_card_title_is_redacted_too(self):
        wire = card_wire(blocked("m", title=f"Token {KEY} refused"))
        self.assertEqual(wire["title"], "Token [redacted] refused")
        self.assertNotIn("title", card_wire(blocked("m")))

    def test_the_tool_input_summary_is_redacted_and_ids_are_kept(self):
        command = f"curl -H 'Authorization: Bearer {KEY}' https://example.com/{HEX}"
        req = blocked(f"Claude wants to run Bash: {command}", kind="permission",
                      subject={"tool": "Bash", "input": command, "session": SESSION})
        wire = card_wire(req)
        self.assertNotIn(KEY, wire["subject"]["input"])
        self.assertNotIn(KEY, wire["message"])
        self.assertIn(HEX, wire["subject"]["input"], "an id is not a secret")
        self.assertEqual(wire["subject"]["tool"], "Bash")
        self.assertEqual(req.subject["input"], command, "the stored requirement is untouched")
        self.assertEqual(card_wire(blocked("m", subject={"options": ["a"]}))["subject"], {"options": ["a"]})

    def test_a_reminder_goes_out_redacted_and_tagged_with_its_cards(self):
        req = self.mgr.create(blocked(f"Reviewer cannot use the key {KEY}", "Reviewer is stuck"))
        clock = [T0]
        project(self.mgr, self.send, "!room:example.com", clock=lambda: clock[0])
        clock[0] = T0 + 31 * 60
        project(self.mgr, self.send, "!room:example.com", clock=lambda: clock[0])
        reminder = self.sent[-1]
        self.assertEqual(reminder["extra_content"][NOTICE_FIELD]["ids"], [req.id])
        self.assertNotIn(KEY, reminder["body"])
        self.assertIn("Still blocked after 31m", reminder["body"])


if __name__ == "__main__":
    unittest.main()
