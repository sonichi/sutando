#!/usr/bin/env python3
"""One model of a question to the owner renders the card, the plain text and the reminder; both
ways of asking build it; and no rendering shows a machine name, asks the owner to keep a record,
or leaves them without a way to answer."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))

import owner_question as oq
import pending_questions_ask as ask
import pending_questions_remind as remind

_spec = importlib.util.spec_from_file_location("ask_owner_cli_model", REPO / "scripts" / "ask-owner.py")
CLI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CLI)

HOST = "Some-Hostname-9"
ROW = "https://rows.example.test/pending-questions"
KEY = "sk-ant-api03-" + "a1B2" * 8
# The owner's real case, with placeholders: three answers written as prose, and two links.
PROSE = ("Should the new page go out this week? Three ways to go: (1) not now, wait for the review; "
         "(2) promote the page only; (3) build the full flow first.")
THREE = [("Not now", "wait for the review"), ("Promote the page", "the page only goes live"),
         ("Build the flow", "the full flow is built first")]
REFS = [{"label": "The page", "url": "https://pages.example.test/launch", "title": "Launch page"},
        {"label": "#12", "url": "https://code.example.test/pull/12", "state": "ready"}]
CHORES = ("Status on the row", "set its Status", "update the row", "mark the row", "edit the row",
          "Pending questions database", "needs your word")


def model(question="Should the new page go out this week?", options=THREE, **kw):
    fields = dict(asker="Blue Fox", host=HOST, link=ROW, extra={"refs": REFS})
    fields.update(kw)
    return ask.question_model(question, "The launch is on Friday.", None, None, options, **fields)


TABLE = {
    "three options and two links": model(),
    "a proposed default of its own": ask.question_model("Merge the two?", "CI is green.", "merge it", "it is green",
                                                        [("Hold", "wait")], asker="Blue Fox", host=HOST, link=ROW),
    "a default that names an option": ask.question_model("Merge the two?", None, "Hold", None,
                                                         [("Go", "merge"), ("Hold", "wait")], asker="Blue Fox",
                                                         host=HOST, link=ROW),
    "an open question": ask.question_model("What should the page be called?", "It is the launch page.",
                                           asker="Blue Fox", host=HOST, link=ROW),
    "no name known": ask.question_model("Merge the two?", None, None, None, [("Go", "merge")], host=HOST, link=ROW),
    "a secret in the context": ask.question_model("Rotate the key?", f"The old one is {KEY}.", None, None,
                                                  [("Rotate", "a new key is issued")], asker="Blue Fox"),
}


class EveryRendering(unittest.TestCase):
    def renderings(self, q):
        return {"card title": q.card_title(), "card first line": q.card_message().split("\n")[0],
                "text": q.text(), "reminder": q.reminder(), "pointer": q.pointer()}

    def test_no_rendering_names_the_machine_the_row_or_a_chore(self):
        for name, q in TABLE.items():
            for where, shown in self.renderings(q).items():
                with self.subTest(question=name, where=where):
                    for leak in (HOST, ROW, KEY, "sk-ant", *CHORES):
                        self.assertNotIn(leak, shown)

    def test_the_machine_and_the_row_are_details_of_the_card_only(self):
        details = TABLE["three options and two links"].card_message().partition("\n\n")[2]
        self.assertIn(f"From your Mac named {HOST}", details.splitlines())
        self.assertIn(f"Record: {ROW}", details.splitlines())

    def test_the_card_the_text_and_the_reminder_ask_the_same_thing(self):
        for name, q in TABLE.items():
            with self.subTest(question=name):
                head = q.headline
                self.assertEqual(q.card_message().split("\n")[0], head)
                self.assertIn(f"\n{head}\n", q.text())
                self.assertIn(head, q.reminder())
                self.assertIn(head, q.pointer())
                labels = [label for _, label in q.card_actions()]
                listed = [ln.split(". ", 1)[1].split(" — ")[0] for ln in q.text().splitlines()
                          if len(ln) > 2 and ln[1:3] == ". " and ln[0].isupper()]
                self.assertEqual(listed, labels, "the text lists exactly the card's buttons, in order")
                self.assertEqual(list(q.card_subject()["choices"]), [aid for aid, _ in q.card_actions()])

    def test_every_rendering_ends_with_a_way_to_answer_and_never_sends_the_owner_back(self):
        for name, q in TABLE.items():
            for where in ("text", "reminder", "pointer"):
                shown = self.renderings(q)[where]
                with self.subTest(question=name, where=where):
                    self.assertRegex(shown, r"(?i)reply here .*in your own words")
                    self.assertNotRegex(shown, r"(?i)tap .* again|try again|does not answer")
        digest = oq.waiting_digest(["Merge the two?", "Rename the page?"], ["CI is green", ""])
        self.assertIn("2 questions are still waiting for your answer", digest)
        self.assertTrue(digest.endswith(oq.ANSWER_HOW))
        self.assertIn("…and 1 more", oq.waiting_digest(["a", "b", "c"], shown=2))
        self.assertIn("1 question is still waiting", oq.waiting_digest(["a"]))

    def test_buttons_carry_the_choices_and_the_card_does_not_type_them_out(self):
        q = TABLE["three options and two links"]
        self.assertEqual([label for _, label in q.card_actions()], [label for label, _ in THREE])
        for label, effect in THREE:
            self.assertNotIn(f"{label}: {effect}", q.card_message())
        subject = q.card_subject()
        self.assertEqual(subject["refs"], REFS)
        self.assertTrue(subject["details_folded"])
        self.assertEqual(q.text().count("https://"), len(REFS), "references are short labelled links")
        self.assertIn("The page — Launch page: https://pages.example.test/launch", q.text())

    def test_the_asker_is_the_agents_name_or_your_agent(self):
        self.assertEqual(TABLE["three options and two links"].card_title(), "Blue Fox asks you to decide")
        self.assertEqual(TABLE["an open question"].lead(), "Blue Fox has a question for you")
        self.assertEqual(TABLE["no name known"].card_title(), "Your agent asks you to decide")
        self.assertEqual(oq.Question("Q?", asker="sutando-core").who, "Your agent")
        self.assertIn("Why Approve: it is green", TABLE["a proposed default of its own"].card_message())


class Validation(unittest.TestCase):
    def test_answers_written_as_prose_are_refused_with_what_to_pass(self):
        prose = oq.Question.from_text(PROSE)
        with self.assertRaises(oq.QuestionError) as refused:
            prose.validate()
        self.assertIn("pass one option per answer", str(refused.exception))
        self.assertEqual(prose.validate(open_question=True), prose)
        self.assertEqual(model(PROSE).problems(), [], "with options the same wording is a card")
        two_way = oq.Question.from_text("Merge it now? yes/no")
        self.assertTrue(two_way.problems())
        self.assertEqual(two_way.problems(proposes=True), [])
        self.assertEqual(oq.Question.from_text("What should the page be called?", "It is the launch page.").problems(), [])

    def test_each_refusal_says_the_way_through(self):
        o = oq.Option
        cases = {
            "the one sentence": oq.Question(" "),
            "put what it does in the option's effect": oq.Question("Q?", options=(o("a", "x" * 41),)),
            "say what it removes": oq.Question("Q?", options=(o("a", "Delete", destructive=True),)),
            "give each answer its own": oq.Question("Q?", options=(o("a", "Go"), o("b", "go "))),
            "group them or link one page": oq.Question("Q?", refs=(oq.Ref("r", "https://example.test/1"),) * 21),
            "must be https": oq.Question("Q?", refs=(oq.Ref("r", "http://example.test/1"),)),
            "name an option's own label": oq.Question("Q?", options=(o("a", "Go"),), proposed="zz"),
        }
        for way, q in cases.items():
            with self.subTest(way=way):
                self.assertTrue(any(way in p for p in q.problems()), q.problems())
        ok = oq.Question("Q?", options=(o("a", "Delete", effect="the draft is removed", destructive=True),))
        self.assertEqual(ok.problems(), [])

    def test_a_ref_is_parsed_checked_and_kept_small_on_the_wire(self):
        ref = oq.parse_ref("label=#12; url=https://code.example.test/pull/12 ;state=ready")
        self.assertEqual(ref.to_wire(), {"label": "#12", "url": "https://code.example.test/pull/12", "state": "ready"})
        self.assertEqual(ref.line(), "#12: https://code.example.test/pull/12")
        for bad in ("label=#1", "label=#1;url=ftp://x", "label=#1;url=https://x/1;state=done",
                    "label=#1;url=https://x/1;colour=red", "label=#1;url=https://x/1;label=#2", "nokey"):
            with self.assertRaises(oq.QuestionError):
                oq.parse_ref(bad)
        self.assertEqual(oq.refs_from([ref, REFS[0]])[1].title, "Launch page")
        self.assertEqual((oq.host_detail(""), oq.row_detail(None)), ("", ""))


class BothWaysOfAskingDelegate(unittest.TestCase):
    """Neither entry point has words of its own: each builds the model and prints what it renders."""

    def test_the_skills_text_card_and_pointer_are_the_models(self):
        body, routed = ask.proactive_body("Merge the two?", "CI is green.", "Blue Fox", ask.Destination(),
                                          options=[("Go", "merge")])
        expected = ask.question_model("Merge the two?", "CI is green.", None, None, [("Go", "merge")], asker="Blue Fox")
        self.assertEqual((body, routed), ("[dm-only]\n" + expected.text(), False))
        card = ask.build_card("ask-1", "Merge the two?", "CI is green.", HOST, None, None, [("Go", "merge")],
                              link=ROW, asker="Blue Fox")
        asked = ask.question_model("Merge the two?", "CI is green.", None, None, [("Go", "merge")], asker="Blue Fox",
                                   host=HOST, link=ROW, ask_id="ask-1")
        self.assertEqual((card.message, card.title, card.subject), (asked.card_message(), asked.card_title(),
                                                                     asked.card_subject()))
        self.assertEqual(ask.card_pointer_body("Merge the two?", "Blue Fox"),
                         "[dm-only]\n" + oq.Question("Merge the two?", asker="Blue Fox").pointer())
        for gone in ("card_message", "headline"):
            self.assertFalse(hasattr(ask, gone), f"{gone} was a private copy of the wording")

    def test_ask_owner_without_a_store_sends_the_models_text(self):
        ws = Path(tempfile.mkdtemp(prefix="owner-question-"))
        (ws / "results").mkdir()
        with mock.patch.object(CLI, "local_name", return_value="Blue Fox"):
            CLI.ask_without_store("What should the page be called?", "It is the launch page.", ws, "no adapter", REFS)
        [sent] = (ws / "results").glob("proactive-*.txt")
        want = oq.Question.from_text("What should the page be called?", "It is the launch page.", asker="Blue Fox",
                                     refs=oq.refs_from(REFS))
        self.assertEqual(sent.read_text(), "[dm-only]\n" + want.text())

    def test_ask_owner_refuses_the_owners_example_and_asks_nothing(self):
        ws = Path(tempfile.mkdtemp(prefix="owner-question-"))
        (ws / "results").mkdir()
        err = io.StringIO()
        with self.assertRaises(SystemExit) as stop, redirect_stderr(err), redirect_stdout(io.StringIO()):
            CLI.main([PROSE, "--workspace", str(ws)])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("pass --option 'Label=what it does' once per answer", err.getvalue())
        self.assertIn("--open-question", err.getvalue())
        self.assertEqual(list((ws / "results").iterdir()), [], "nothing was asked")

    def test_the_reminder_is_the_models_digest(self):
        questions = [{"title": "Merge the two?", "snippet": "CI is green"}, {"title": "Rename the page?"}]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(remind, "RESULTS_DIR", Path(td)):
            remind.notify_discord_dm(questions)
            [sent] = Path(td).glob("proactive-*.txt")
            body = sent.read_text()
        self.assertEqual(body, oq.waiting_digest(["Merge the two?", "Rename the page?"], ["CI is green", ""]))
        for chore in CHORES:
            self.assertNotIn(chore, body)


class ATypedReplyClosesTheRow(unittest.TestCase):
    """The owner answers a question card in their own words; the agent closes the row, not the owner."""

    def setUp(self):
        from hitl.cards import manager_for
        self.ws = Path(tempfile.mkdtemp(prefix="owner-question-reply-"))
        self.manager = manager_for(self.ws)
        card = ask.build_card("ask-7", "Merge the two?", None, HOST, None, None, [("Go", "merge")], asker="Blue Fox")
        self.card = self.manager.create(card)
        self.manager.record_projection(self.card.id, self.card.revision, "$card-event", None)

    def task(self, tier="owner", reply_to="$card-event"):
        path = self.ws / "task-1.txt"
        path.write_text(f"id: task-1\naccess_tier: {tier}\nreply_to_event: {reply_to}\ntask: go ahead\nreply_to_event: $x\n")
        return str(path)

    def run_answered(self, task, resolved=(True, "closed ask-7 as Answered")):
        import pending_questions_room_db as adapter
        import pq
        out = io.StringIO()
        with mock.patch.object(adapter, "resolve", return_value=resolved) as resolve, redirect_stdout(out):
            rc = pq.main(["answered", "--task-file", task, "--workspace", str(self.ws)])
        return rc, out.getvalue(), resolve

    def test_a_reply_to_the_card_closes_its_question_as_answered(self):
        self.assertEqual(ask.ask_for_reply(self.ws, "$card-event", self.manager), "ask-7")
        rc, said, resolve = self.run_answered(self.task())
        self.assertEqual((rc, said.strip()), (0, "closed ask-7 as Answered"))
        self.assertEqual(resolve.call_args.args[1:], ("ask-7", "Answered"))
        self.assertEqual(self.run_answered(self.task(), resolved=(False, "room unreachable"))[0], 1)

    def test_anything_else_closes_nothing_and_says_the_way_through(self):
        for task in (self.task(tier="team"), self.task(reply_to="$other"), self.task(reply_to=""),
                     str(self.ws / "missing.txt")):
            rc, said, resolve = self.run_answered(task)
            self.assertEqual(rc, 1, said)
            resolve.assert_not_called()
            self.assertTrue(said.startswith("not closed: "), said)
        self.assertIn("pq.py resolve <id> --answered", self.run_answered(self.task(reply_to="$other"))[1])
        self.manager.resolve(self.card.id)
        self.assertIsNone(ask.ask_for_reply(self.ws, "$card-event", self.manager), "a settled card answers nothing")
        self.assertIsNone(ask.ask_for_reply(self.ws, " ", self.manager))


if __name__ == "__main__":
    unittest.main()
