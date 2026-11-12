#!/usr/bin/env python3
"""ask-owner's decision card: the buttons are the options, and the optional references reach it.

- buttons: `--option` labels are the buttons, in the order given; a `--default` that names one of
  them marks it and adds none; any other default stays its own first button, Approve;
- clicks: every button resolves to its own label and consequence;
- `--ref` / `--effect` / `--primary`: validated at the command line, carried on the wire only
  when given, and the refs are listed as links wherever the question is asked as text;
- what the owner reads: the agent's name and never the machine's, the question's first sentence
  as the headline, and no row link outside the card's details;
- choices written as prose with no `--option` are refused, unless `--open-question` says so.
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PQ = REPO / "skills" / "pending-questions" / "scripts"
for p in (REPO / "src", PQ):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import agent_local_name
import owner_question
import pending_questions_ask as ask
import pending_questions_clicks as clicks
import pending_questions_reader as reader
import pending_questions_store as pq_store
from hitl import cards
from hitl.manager import HitlManager, HitlStore
from hitl.schema import Action, ActionReply, HumanRequirement

ADAPTER = str(PQ / "pending_questions_room_db.py")
GO_NOT_NOW = [("Go", "merge both now"), ("Not now", "leave them open")]
REFS = [{"label": "#12", "url": "https://example.test/pull/12", "title": "Fix the button", "state": "ready"},
        {"label": "#13", "url": "https://example.test/pull/13"}]


def _load_cli():
    spec = importlib.util.spec_from_file_location("ask_owner_cli_t", REPO / "scripts" / "ask-owner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CLI = _load_cli()


def _ws(last_active="ag2space") -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ask-owner-buttons-"))
    (ws / "state").mkdir()
    (ws / "results").mkdir()
    (ws / "state" / "last-owner-activity.json").write_text(json.dumps({"channel": last_active}))
    return ws


def _manager(ws: Path) -> HitlManager:
    return HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))


def _card(ws, **kw):
    m = _manager(ws)
    out = ask.queue_question("Merge the two?", workspace=ws, host="h", manager=m, **kw)
    ask.create_card(out)
    return m, out, (m.get(out["card"]) if out["card"] else None)


def _run(ws, *argv, adapter=ADAPTER, question="Merge the two?"):
    """(exit code, stdout, stderr) of the command line, in process."""
    out, err = io.StringIO(), io.StringIO()
    args = [question, "--urgency", "durable", "--workspace", str(ws), *argv]
    if adapter:
        args += ["--store-adapter", adapter]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = CLI.main(args)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _only_card(ws):
    (req,) = _manager(ws).active()
    return req


def _texts(ws) -> str:
    return "\n".join(p.read_text() for p in sorted((ws / "results").glob("proactive-*")))


def _click_each(ws, **kw) -> dict:
    """{label: (action id, label, consequence)} as a click on each button resolves, one card each."""
    seen = {}
    labels = [label for _, label in ask.card_actions(kw.get("default_action"), kw.get("options"))]
    for label in labels:
        w = _ws()
        m, out, req = _card(w, **kw)
        aid = next(a.id for a in req.actions if a.label == label)
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id=aid, guard=req.guard))
        (c,) = clicks.clicks(w, lambda ws_, ask_id, token: (clicks.CLOSED, "closed"), manager=m)
        self_line = reader.click_line(c)
        assert f"chose {aid} {label!r}" in self_line, self_line
        seen[label] = (c["action_id"], c["label"], c["consequence"])
    return seen


class ADefaultThatNamesAnOption(unittest.TestCase):
    """`--option Go=… --option "Not now=…" --default "Not now"` once showed a generic Approve
    button whose click resolved to "Not now"."""
    KW = dict(default_action="Not now", reason="nothing is urgent", options=GO_NOT_NOW)

    def test_the_buttons_are_exactly_the_option_labels_in_order(self):
        _, _, req = _card(_ws(), **self.KW)
        self.assertEqual([(a.id, a.label) for a in req.actions], [("option-0", "Go"), ("option-1", "Not now")])
        self.assertNotIn("Approve", [a.label for a in req.actions])
        self.assertNotIn("approve", [a.id for a in req.actions])

    def test_the_default_marks_its_option_and_adds_none(self):
        _, _, req = _card(_ws(), **self.KW)
        self.assertEqual(req.subject["default"], "option-1")
        self.assertEqual(req.subject["choices"], {"option-0": "merge both now", "option-1": "leave them open"})
        self.assertIn("Why Not now: nothing is urgent", req.message)
        self.assertNotIn("Approve", req.message)

    def test_each_click_resolves_to_its_own_label(self):
        self.assertEqual(_click_each(_ws(), **self.KW),
                         {"Go": ("option-0", "Go", "merge both now"),
                          "Not now": ("option-1", "Not now", "leave them open")})

    def test_the_match_ignores_case_and_outer_whitespace(self):
        for default in ("not now", "  NOT NOW ", "Not now"):
            with self.subTest(default=default):
                _, _, req = _card(_ws(), default_action=default, options=GO_NOT_NOW)
                self.assertEqual([a.label for a in req.actions], ["Go", "Not now"])
                self.assertEqual(req.subject["default"], "option-1")

    def test_the_row_lists_the_same_options_with_no_approve(self):
        body = pq_store.row_body("Merge the two?", "Not now", "nothing is urgent", GO_NOT_NOW, "sent")
        self.assertIn("**Go** -> merge both now\n**Not now** -> leave them open", body)
        self.assertNotIn("Approve", body)

    def test_through_the_command_line(self):
        ws = _ws()
        code, out, _ = _run(ws, "--option", "Go=merge both now", "--option", "Not now=leave them open",
                            "--default", "Not now")
        self.assertEqual(code, 0)
        self.assertIn("decision card", out)
        self.assertEqual([a.label for a in _only_card(ws).actions], ["Go", "Not now"])


class OtherDefaultsAreUnchanged(unittest.TestCase):
    def test_a_default_that_names_no_option_is_its_own_first_button(self):
        _, _, req = _card(_ws(), default_action="merge it", options=[("Hold", "wait")])
        self.assertEqual([(a.id, a.label) for a in req.actions], [("approve", "Approve"), ("option-1", "Hold")])
        self.assertEqual(req.subject, {"ask_id": req.guard, "details_folded": True, "default": "approve",
                                       "choices": {"approve": "merge it", "option-1": "wait"}})
        self.assertEqual(_click_each(_ws(), default_action="merge it", options=[("Hold", "wait")]),
                         {"Approve": ("approve", "Approve", "merge it"), "Hold": ("option-1", "Hold", "wait")})

    def test_a_default_alone_is_approve_and_decline(self):
        _, _, req = _card(_ws(), default_action="go", reason="green")
        self.assertEqual([a.label for a in req.actions], ["Approve", "Decline"])
        self.assertIn("Why Approve: green", req.message)

    def test_options_alone_are_the_buttons(self):
        _, _, req = _card(_ws(), options=GO_NOT_NOW)
        self.assertEqual([(a.id, a.label) for a in req.actions], [("option-0", "Go"), ("option-1", "Not now")])
        self.assertNotIn("default", req.subject)

    def test_default_option_is_none_without_a_match(self):
        self.assertIsNone(pq_store.default_option(None, GO_NOT_NOW))
        self.assertIsNone(pq_store.default_option("merge it", GO_NOT_NOW))
        self.assertIsNone(pq_store.default_option("Go", ()))
        self.assertEqual(pq_store.default_option(" go ", [("", "blank"), *GO_NOT_NOW]), 0)


class TheWire(unittest.TestCase):
    def test_no_new_flag_leaves_the_wire_as_it_was(self):
        _, out, req = _card(_ws(), default_action="merge it", reason="green", options=[("Hold", "wait")])
        wire = req.to_wire()
        self.assertEqual(wire["actions"], [{"id": "approve", "kind": "confirmation", "label": "Approve"},
                                           {"id": "option-1", "kind": "confirmation", "label": "Hold"}])
        self.assertEqual(wire["subject"], {"choices": {"approve": "merge it", "option-1": "wait"}})
        self.assertEqual(sorted(wire), ["actions", "device", "guard", "id", "kind", "message", "revision",
                                        "runtime", "status", "subject", "title", "turn_on_action"])
        self.assertEqual(wire["message"], "Merge the two?\n\nWhy Approve: green\n\nFrom your Mac named h",
                         "the buttons carry the choices; they are not typed out above them")

    def test_a_marked_default_adds_nothing_to_the_wire(self):
        _, _, req = _card(_ws(), default_action="Not now", options=GO_NOT_NOW)
        wire = req.to_wire()
        self.assertEqual(wire["subject"], {"choices": {"option-0": "merge both now", "option-1": "leave them open"}})
        self.assertTrue(all(sorted(a) == ["id", "kind", "label"] for a in wire["actions"]))

    def test_refs_effects_and_style_reach_the_wire(self):
        extra = {"refs": REFS, "effects": [("go", "both merge"), ("Not now", "nothing changes")], "primary": " GO "}
        m, _, req = _card(_ws(), default_action="Not now", options=GO_NOT_NOW, extra=extra)
        wire = m.get(req.id).to_wire()
        self.assertEqual(wire["subject"]["refs"], REFS)
        self.assertEqual(wire["subject"]["effects"], {"option-0": "both merge", "option-1": "nothing changes"})
        self.assertEqual(wire["actions"], [
            {"id": "option-0", "kind": "confirmation", "label": "Go", "style": "primary"},
            {"id": "option-1", "kind": "confirmation", "label": "Not now"}])

    def test_style_is_on_the_wire_only_for_the_action_the_subject_styles(self):
        req = HumanRequirement(kind="choice", runtime="rt", message="m", subject={"styles": {"b": "primary"}},
                               actions=[Action(id="a", kind="k", label="A"), Action(id="b", kind="k", label="B")])
        self.assertEqual(req.to_wire()["actions"], [{"id": "a", "kind": "k", "label": "A"},
                                                    {"id": "b", "kind": "k", "label": "B", "style": "primary"}])
        self.assertNotIn("subject", req.to_wire(), "the styles themselves stay on the host")

    def test_a_stored_action_keeps_the_three_keys_an_older_reader_loads(self):
        ws = _ws()
        _, out, req = _card(ws, options=GO_NOT_NOW, extra={"primary": "Go"})
        stored = json.loads((ws / "state" / "hitl" / "requirements" / f"{req.id}.json").read_text())
        stored = stored.get("requirement", stored)
        self.assertEqual([sorted(a) for a in stored["actions"]], [["id", "kind", "label"]] * 2)
        self.assertEqual(stored["subject"]["styles"], {"option-0": "primary"})

    def test_the_subject_whitelist_forwards_refs_and_effects_and_nothing_private(self):
        req = HumanRequirement(kind="choice", runtime="rt", message="m",
                               subject={"refs": REFS, "effects": {"a": "x"}, "default": "a", "ask_id": "q"})
        self.assertEqual(req.to_wire()["subject"], {"refs": REFS, "effects": {"a": "x"}})

    def test_an_effect_or_primary_for_no_button_builds_no_card(self):
        for extra in ({"effects": [("Maybe", "x")]}, {"primary": "Maybe"}):
            with self.subTest(extra=extra):
                ws = _ws()
                _, out, req = _card(ws, options=GO_NOT_NOW, extra=extra)
                self.assertIsNone(req)
                self.assertIn("no button is labelled 'Maybe'", out["card_error"])
                self.assertTrue(out["proactive_file"], "asked as text instead")


class RefsAsText(unittest.TestCase):
    LINES = ["#12 — Fix the button: https://example.test/pull/12", "#13: https://example.test/pull/13"]

    def test_ref_lines_are_label_title_url(self):
        self.assertEqual([r.line() for r in owner_question.refs_from(REFS)], self.LINES)

    def test_a_text_ask_lists_each_ref_on_its_own_line(self):
        ws = _ws("discord")
        _, out, req = _card(ws, options=GO_NOT_NOW, extra={"refs": REFS})
        self.assertIsNone(req)
        body = _texts(ws)
        for line in self.LINES:
            self.assertIn(f"\n{line}\n", body)

    def test_a_card_that_falls_back_to_text_lists_them_too(self):
        ws = _ws()
        m = _manager(ws)
        out = ask.queue_question("Merge the two?", workspace=ws, host="h", manager=m, options=GO_NOT_NOW,
                                 extra={"refs": REFS})
        m.create = lambda card: (_ for _ in ()).throw(OSError("disk full"))
        self.assertEqual(ask.create_card(out), ask.CARD_NOT_STORED)
        for ref in REFS:
            self.assertIn(ref["url"], _texts(ws))

    def test_a_text_ask_without_refs_lists_none(self):
        body, _ = ask.proactive_body("Q?", "ctx", "Blue Fox", ask.Destination())
        self.assertEqual(body, "[dm-only]\nBlue Fox has a question for you:\n\nQ?\n\nctx\n\n"
                               "Reply here in your own words.\n")

    def test_with_no_store_the_dm_lists_them(self):
        ws = _ws("discord")
        lines = CLI.ask_without_store("Merge the two?", "ctx", ws, "no adapter", REFS)
        self.assertTrue(any(ln.startswith("sent: queued") for ln in lines), lines)
        body = _texts(ws)
        for line in self.LINES:
            self.assertIn(f"\n{line}\n", body)
        self.assertNotIn("https://", "\n".join(CLI.ask_without_store("Q?", None, _ws("discord"), "none")))


HOST = "Some-Hostname-9"
ROW = "https://rows.example.test/pending-questions"
# The wording of a question that once reached the owner as plain text, with placeholders.
PROSE_Q = "Should the new page go out this week?"
PROSE_CTX = ("Three ways to go: (1) not now, wait for the review; (2) promote the page only; "
             "(3) build the full flow first.")


class WhatTheOwnerReads(unittest.TestCase):
    """The agent's name, never the machine's; one short headline; no row link in the body."""

    def _queued(self, ws, **kw):
        m = _manager(ws)
        with mock.patch.object(ask, "asker_name", return_value="Blue Fox"):
            out = ask.queue_question(workspace=ws, host=HOST, manager=m, link=ROW, **kw)
            ask.create_card(out)
        return out, (m.get(out["card"]) if out["card"] else None)

    def test_the_card_names_the_agent_and_never_the_machine(self):
        _, req = self._queued(_ws(), question="Merge the two?", options=GO_NOT_NOW)
        wire = req.to_wire()
        self.assertEqual(wire["title"], "Blue Fox asks you to decide")
        head, _, details = wire["message"].partition("\n\n")
        self.assertNotIn(HOST, wire["title"] + head)
        self.assertIn(f"From your Mac named {HOST}", details.splitlines(), "the machine is a detail only")
        self.assertEqual(wire["device"]["name"], "Blue Fox")
        self.assertNotIn("core", wire["title"])

    def test_the_text_names_the_agent_and_never_the_machine(self):
        ws = _ws("discord")
        self._queued(ws, question="What should the page be called?", context="It is the launch page.")
        body = _texts(ws)
        self.assertEqual(body.splitlines()[1], "Blue Fox has a question for you:")
        self.assertNotIn(HOST, body)
        self.assertNotIn("needs your word", body)

    def test_the_pointer_to_a_card_names_the_agent(self):
        body = ask.card_pointer_body("Merge the two?", "Blue Fox")
        self.assertIn("Blue Fox sent you a question as a card", body)
        self.assertIn("reply here in your own words", body, "the pointer is never a dead end")
        self.assertNotIn("core", body)

    def test_the_no_store_dm_names_the_agent(self):
        ws = _ws("discord")
        with mock.patch.object(CLI, "local_name", return_value="Blue Fox"):
            CLI.ask_without_store("What should the page be called?", None, ws, "no adapter")
        self.assertEqual(_texts(ws).splitlines()[1], "Blue Fox has a question for you:")

    def test_with_no_name_known_it_is_your_agent(self):
        empty = Path(tempfile.mkdtemp(prefix="ask-owner-noname-"))
        self.assertEqual(agent_local_name.local_name(workspace=empty, environ={}), "Your agent")
        self.assertEqual(agent_local_name.local_name(workspace=empty, environ={"SUTANDO_DISPLAY_NAME": "Blue Fox"}),
                         "Blue Fox")

    def test_a_text_ask_carries_no_row_link(self):
        ws = _ws("discord")
        self._queued(ws, question="What should the page be called?")
        body = _texts(ws)
        self.assertNotIn(ROW, body)
        self.assertNotIn("Status on the row", body)
        self.assertTrue(body.endswith("\nReply here in your own words.\n"))

    def test_a_card_keeps_the_row_link_below_the_first_blank_line(self):
        _, req = self._queued(_ws(), question="Merge the two?", options=GO_NOT_NOW)
        head, _, details = req.message.partition("\n\n")
        self.assertEqual(head, "Merge the two?")
        self.assertNotIn("http", head)
        self.assertIn(ROW, details)

    def test_the_first_sentence_is_the_headline_and_the_rest_is_details(self):
        long = ("Should the new page go out this week? The review found two open points, and the "
                "second one touches the sign-up flow.\nA later line.")
        _, req = self._queued(_ws(), question=long, context="Why now: the launch.", options=GO_NOT_NOW)
        lines = req.message.split("\n")
        self.assertEqual(lines[0], "Should the new page go out this week?")
        self.assertEqual(lines[1], "", "the details start after the first blank line")
        self.assertIn("The review found two open points", req.message.partition("\n\n")[2])
        self.assertIn("A later line.", req.message)

    def test_headline_splits_only_at_a_sentence_end(self):
        split = owner_question.split_headline
        self.assertEqual(split("Merge #1?"), ("Merge #1?", ""))
        self.assertEqual(split("Ship v1.2 now? It is green."), ("Ship v1.2 now?", "It is green."))
        self.assertEqual(split(" One line\n\nbelow it "), ("One line", "below it"))


class ChoicesWrittenAsProse(unittest.TestCase):
    def test_the_detector_sees_a_list_of_answers(self):
        listed = [(PROSE_Q, PROSE_CTX),
                  (PROSE_Q + " (1) not now (2) promote the page only (3) build the flow", None),
                  ("Which way?", "Options: wait, promote, build"),
                  ("Which way?", "option: wait"),
                  ("Which way? A) wait B) promote C) build", None),
                  ("Which way? (a) wait (b) promote", None),
                  ("Which way?", "1. wait for the review\n2. promote the page\n3. build the flow")]
        for q, ctx in listed:
            with self.subTest(q=q, ctx=ctx):
                self.assertEqual(CLI.enumerated_choice(q, ctx), "listed")

    def test_the_detector_sees_a_two_way_question(self):
        for q in ("Merge it now? yes/no", "Merge it now (y/n)", "Should I promote the page or wait?",
                  "Ship today, or hold for the review?"):
            with self.subTest(q=q):
                self.assertEqual(CLI.enumerated_choice(q), "two-way")

    def test_an_open_question_is_not_a_choice(self):
        opens = [("What should the launch page be called?", None),
                 ("Who should review the page?", "It changes the sign-up flow. Version 1.2 is live."),
                 ("Which room do you want the report in?", "The work order lists 2 pages."),
                 ("Your DM with me was upgraded to a new room I'm not in. Please invite me there and repoint "
                  "my DM.", None),
                 ("Startup stopped: the CLI needs a login. Log in now? (remedy in the details)",
                  "the gate stopped startup before services launched. Remedy: sign in, or run the login step."),
                 ("What next?", "I could not reach the store or the page."),
                 ("", None)]
        for q, ctx in opens:
            with self.subTest(q=q):
                self.assertEqual(CLI.enumerated_choice(q, ctx), "")

    def test_prose_choices_with_no_option_are_refused_and_nothing_is_asked(self):
        for argv in ((PROSE_Q, "--context", PROSE_CTX), ("Merge it now? yes/no",),
                     (PROSE_Q, "--context", PROSE_CTX, "--default", "not now")):
            with self.subTest(argv=argv):
                ws = _ws()
                code, out, err = _run(ws, *argv[1:], question=argv[0])
                self.assertEqual((code, out), (2, ""))
                self.assertIn("pass --option 'Label=what it does' once per answer", err)
                self.assertEqual((_manager(ws).active(), _texts(ws)), ([], ""))

    def test_the_same_question_with_options_is_a_card_with_those_buttons(self):
        ws = _ws()
        code, _, _ = _run(ws, "--context", "The review found two open points.",
                          "--option", "Not now=wait for the review", "--option", "Promote only=promote the page",
                          "--option", "Build first=build the full flow", question=PROSE_Q)
        self.assertEqual(code, 0)
        req = _only_card(ws)
        self.assertEqual([a.label for a in req.actions], ["Not now", "Promote only", "Build first"])
        self.assertEqual(req.message.split("\n")[0], PROSE_Q)
        self.assertEqual(_texts(ws), "", "no text beside the card")

    def test_a_two_way_question_with_a_default_is_approve_and_decline(self):
        ws = _ws()
        self.assertEqual(_run(ws, "--default", "merge it", question="Merge it now? yes/no")[0], 0)
        self.assertEqual([a.label for a in _only_card(ws).actions], ["Approve", "Decline"])

    def test_open_question_asks_it_as_text(self):
        ws = _ws()
        code, _, _ = _run(ws, "--open-question", question="Should the page be called Launch or Start?")
        self.assertEqual(code, 0)
        self.assertEqual(_manager(ws).active(), [])
        self.assertIn("Should the page be called Launch or Start?", _texts(ws))

    def test_the_engine_conflict_fallback_relays_its_proposal_as_an_open_question(self):
        src = (REPO / "skills" / "engine-conflict-resolve" / "scripts" / "deliver.py").read_text()
        self.assertIn('"--open-question"', src)


class TheCommandLine(unittest.TestCase):
    OPTS = ("--option", "Go=merge both now", "--option", "Not now=leave them open")

    def test_the_flags_reach_the_card(self):
        ws = _ws()
        code, out, err = _run(ws, *self.OPTS, "--default", "Not now",
                              "--ref", "label=#12; url=https://example.test/pull/12?a=b ;title=Fix the button;"
                                       "state=ready;state_label=Ready to merge;group=core",
                              "--ref", "label=#13;url=https://example.test/pull/13",
                              "--effect", "Go=both merge", "--primary", "go")
        self.assertEqual((code, err.count("ask-owner: this store adapter")), (0, 0), err)
        wire = _only_card(ws).to_wire()
        self.assertEqual(wire["subject"]["refs"], [
            {"label": "#12", "url": "https://example.test/pull/12?a=b", "title": "Fix the button",
             "state": "ready", "state_label": "Ready to merge", "group": "core"},
            {"label": "#13", "url": "https://example.test/pull/13"}])
        self.assertEqual(wire["subject"]["effects"], {"option-0": "both merge"})
        self.assertEqual([a.get("style") for a in wire["actions"]], ["primary", None])

    def test_without_the_flags_the_adapter_is_called_as_before(self):
        ws = _ws()
        self.assertEqual(_run(ws, *self.OPTS)[0], 0)
        wire = _only_card(ws).to_wire()
        self.assertEqual(sorted(wire["subject"]), ["choices"])
        self.assertTrue(all("style" not in a for a in wire["actions"]))

    def test_a_bad_ref_is_refused(self):
        bad = {"label=#1;url=http://example.test/1": "must be https",
               "label=#1;url=https://": "must be https",
               "label=#1;url=https://example.test/1;state=done": "a ref state is one of",
               "label=#1;url=https://example.test/1;colour=red": "a ref is",
               "label=#1;url=https://example.test/1;label=#2": "a ref is",
               "label=#1;url=https://example.test/1;title=": "a ref is",
               "label=#1;url=https://example.test/1;nokey": "a ref is",
               "url=https://example.test/1": "needs both label and url",
               "label=#1": "needs both label and url"}
        for ref, why in bad.items():
            with self.subTest(ref=ref):
                ws = _ws()
                code, _, err = _run(ws, *self.OPTS, "--ref", ref)
                self.assertEqual(code, 2)
                self.assertIn(why, err)
                self.assertEqual(_manager(ws).active(), [], "nothing was asked")

    def test_more_than_twenty_refs_are_refused(self):
        refs = [x for i in range(21) for x in ("--ref", f"label=#{i};url=https://example.test/{i}")]
        ws = _ws()
        code, _, err = _run(ws, *self.OPTS, *refs)
        self.assertEqual(code, 2)
        self.assertIn("at most 20 --ref, got 21", err)
        self.assertEqual(_run(_ws(), *self.OPTS, *refs[:40])[0], 0)

    def test_an_effect_or_primary_for_no_button_is_refused(self):
        for flags in (("--effect", "Maybe=who knows"), ("--primary", "Approve")):
            with self.subTest(flags=flags):
                ws = _ws()
                code, _, err = _run(ws, *self.OPTS, *flags)
                self.assertEqual(code, 2)
                self.assertIn("no button is labelled", err)
                self.assertEqual((_manager(ws).active(), _texts(ws)), ([], ""), "nothing was asked")
        for effect in ("Go", "=x", "Go="):
            with self.subTest(effect=effect):
                code, _, err = _run(_ws(), *self.OPTS, "--effect", effect)
                self.assertEqual(code, 2)
                self.assertIn("an effect is", err)

    def test_an_effect_may_name_the_approve_of_a_default_of_its_own(self):
        ws = _ws()
        self.assertEqual(_run(ws, "--default", "merge it", "--effect", "Approve=it merges", "--primary", "Approve")[0], 0)
        wire = _only_card(ws).to_wire()
        self.assertEqual(wire["subject"]["effects"], {"approve": "it merges"})
        self.assertEqual(wire["actions"][0], {"id": "approve", "kind": "confirmation", "label": "Approve",
                                              "style": "primary"})

    def test_an_adapter_without_the_support_is_asked_without_them(self):
        old = Path(tempfile.mkdtemp(prefix="ask-owner-old-adapter-")) / "old_adapter.py"
        old.write_text("def room_store(ws):\n    return None, 'none'\n\n"
                       "def ask_owner(question, **kw):\n"
                       "    assert 'extra' not in kw\n    return {'record': 'r', 'heading': '## h'}\n\n"
                       "def report_lines(out):\n    return ['asked the old way']\n")
        code, out, err = _run(_ws(), *self.OPTS, "--ref", "label=#1;url=https://example.test/1", adapter=str(old))
        self.assertEqual(code, 0)
        self.assertIn("asked the old way", out)
        self.assertIn("takes no --ref/--effect/--primary", err)

    def test_with_no_store_the_refs_are_in_the_dm(self):
        ws = _ws("discord")
        missing = str(ws / "no-such-adapter.py")
        code, out, _ = _run(ws, "--ref", "label=#1;url=https://example.test/1", adapter=missing)
        self.assertEqual(code, 0)
        self.assertIn("NO STORE", out)
        self.assertIn("\n#1: https://example.test/1\n", _texts(ws))


if __name__ == "__main__":
    unittest.main()
