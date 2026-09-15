#!/usr/bin/env python3
"""Behavioural coverage for the web client's pending-question triage queue.

The browser half of this feature had no tests at all: the flat question list was
rendered, escaped and wired to POST /answer entirely unguarded. That mattered less
when every question was on screen. The queue renders exactly ONE, so a defect in
the cursor, the ordering it is handed, or the wait label does not degrade the view
— it decides which single question the owner is asked, and hides the rest.

These execute the real helper source out of web-client.ts in node, rather than
asserting on its text. An earlier generation of pending-question tests asserted on
source text and passed for the entire period POST /answer was 100% broken.

Run: python3 tests/web-client-question-queue.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()

START = "// ─── Pending-question triage queue helpers"
END = "// ─── End pending-question triage queue helpers"


def _helper_source() -> str:
    assert START in SOURCE, "the triage queue helpers lost their extraction marker"
    start = SOURCE.index(START)
    end = SOURCE.index(END, start)
    return SOURCE[start:end]


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not available")
    return node


SHIM = r"""
function esc(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
"""


def _probe(script: str):
    """Run the real helper source plus `script` in node; return its JSON stdout."""
    program = SHIM + _helper_source() + "\n" + script
    result = subprocess.run(
        [_node(), "--input-type=module", "-e", program],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)


ROWS = """
const rows = [
  {id: 'Q1', text: 'Oldest thing', detail: 'detail one', age_days: 29, refs: [], blocks: 0, recheck: null},
  {id: 'Q2', text: 'Blocked thing', detail: 'detail two', age_days: 5, refs: [10], blocks: 1, recheck: null},
  {id: 'Q3', text: 'Fresh thing', detail: 'detail three', age_days: 0, refs: [], blocks: 0, recheck: null}
];
"""


class OneAtATime(unittest.TestCase):
    def test_only_the_cursor_question_is_rendered(self):
        out = _probe(ROWS + """
        const html = renderQuestionQueue(rows, 0);
        console.log(JSON.stringify({
          items: (html.match(/class="q-item"/g) || []).length,
          inputs: (html.match(/class="q-input"/g) || []).length,
          hasFirst: html.includes('Oldest thing'),
          hasSecond: html.includes('Blocked thing'),
          hasThird: html.includes('Fresh thing')
        }));
        """)
        self.assertEqual(1, out["items"], "the queue rendered more than one question")
        self.assertEqual(1, out["inputs"])
        self.assertTrue(out["hasFirst"])
        self.assertFalse(out["hasSecond"], "a non-cursor question leaked into the card")
        self.assertFalse(out["hasThird"])

    def test_the_card_says_where_in_the_queue_it_is(self):
        out = _probe(ROWS + """
        console.log(JSON.stringify({
          first: renderQuestionQueue(rows, 0).includes('1 of 3'),
          second: renderQuestionQueue(rows, 1).includes('2 of 3')
        }));
        """)
        self.assertTrue(out["first"])
        self.assertTrue(out["second"])

    def test_an_empty_queue_says_so_instead_of_rendering_a_card(self):
        out = _probe("""
        const html = renderQuestionQueue([], 0);
        console.log(JSON.stringify({empty: html.includes('No pending questions'),
                                    items: (html.match(/class="q-item"/g) || []).length}));
        """)
        self.assertTrue(out["empty"])
        self.assertEqual(0, out["items"])


class Cursor(unittest.TestCase):
    def test_next_wraps_rather_than_running_off_the_end(self):
        # Next has to be free to press: from the last question it returns to the
        # first, so skipping never strands the owner on an empty card.
        out = _probe(ROWS + """
        console.log(JSON.stringify([0,1,2,3,4].map(function(i){
          return questionQueueCursor(rows, i);
        })));
        """)
        self.assertEqual([0, 1, 2, 0, 1], out)

    def test_a_cursor_past_a_shrunken_queue_is_clamped_not_crashed(self):
        out = _probe("""
        console.log(JSON.stringify({
          cursor: questionQueueCursor([{id:'a'}], 7),
          empty: questionQueueCursor([], 3)
        }));
        """)
        self.assertEqual(0, out["cursor"])
        self.assertEqual(0, out["empty"])


class WaitIsVisible(unittest.TestCase):
    def test_the_wait_is_spelled_out_on_the_card(self):
        out = _probe("""
        console.log(JSON.stringify({
          many: questionWaitLabel({age_days: 29}),
          one: questionWaitLabel({age_days: 1}),
          today: questionWaitLabel({age_days: 0}),
          unknown: questionWaitLabel({age_days: null})
        }));
        """)
        self.assertEqual("waiting 29 days", out["many"])
        self.assertEqual("waiting 1 day", out["one"])
        self.assertEqual("asked today", out["today"])
        self.assertEqual("age unknown", out["unknown"],
                         "an undated question must not read as brand new")

    def test_the_wait_label_reaches_the_rendered_card(self):
        out = _probe(ROWS + """
        console.log(JSON.stringify({shown: renderQuestionQueue(rows, 0).includes('waiting 29 days')}));
        """)
        self.assertTrue(out["shown"])

    def test_what_is_blocked_is_stated_only_when_something_is(self):
        out = _probe("""
        console.log(JSON.stringify({
          none: questionBlocksLabel({refs: [], blocks: 0}),
          one: questionBlocksLabel({refs: [10], blocks: 1}),
          many: questionBlocksLabel({refs: [10, 11], blocks: 2}),
          cleared: questionBlocksLabel({refs: [10], blocks: 0})
        }));
        """)
        self.assertEqual("", out["none"])
        self.assertEqual("blocking 1 item", out["one"])
        self.assertEqual("blocking 2 items", out["many"])
        self.assertEqual("nothing still blocked", out["cleared"])


class RecheckBanner(unittest.TestCase):
    def test_a_stale_verdict_is_shown_on_the_card(self):
        out = _probe("""
        const html = questionRecheckHtml({recheck: {status: 'stale', note: '#3346 merged'}});
        console.log(JSON.stringify({shown: html.includes('#3346 merged'), stale: html.includes('q-stale')}));
        """)
        self.assertTrue(out["shown"])
        self.assertTrue(out["stale"])

    def test_an_undecided_recheck_renders_nothing_rather_than_a_reassurance(self):
        # Silence means "not checked". A banner saying anything at all here would
        # make an unreachable gh look like a clean bill of health.
        out = _probe("""
        console.log(JSON.stringify({
          none: questionRecheckHtml({recheck: null}),
          blank: questionRecheckHtml({recheck: {status: 'stale', note: ''}}),
          missing: questionRecheckHtml({})
        }));
        """)
        self.assertEqual("", out["none"])
        self.assertEqual("", out["blank"])
        self.assertEqual("", out["missing"])


class Actions(unittest.TestCase):
    def test_the_card_offers_approve_reject_reply_next_and_dismiss(self):
        out = _probe(ROWS + """
        const html = renderQuestionQueue(rows, 0);
        console.log(JSON.stringify({
          approve: html.includes('data-ans="Approved"'),
          reject: html.includes('data-ans="Rejected"'),
          reply: html.includes('data-qact="reply"'),
          next: html.includes('data-qact="next"'),
          dismiss: html.includes('data-qact="dismiss"'),
          send: html.includes('q-send')
        }));
        """)
        for action in ("approve", "reject", "reply", "next", "dismiss", "send"):
            self.assertTrue(out[action], f"the {action} action is missing from the card")

    def test_declared_options_replace_the_default_approve_reject_pair(self):
        out = _probe("""
        const rows = [{id: 'Q1', text: 'Pick', age_days: 2, refs: [], blocks: 0,
                       recheck: null, options: ['Revert', 'Leave it']}];
        const html = renderQuestionQueue(rows, 0);
        console.log(JSON.stringify({
          revert: html.includes('data-ans="Revert"'),
          leave: html.includes('data-ans="Leave it"'),
          approved: html.includes('data-ans="Approved"')
        }));
        """)
        self.assertTrue(out["revert"])
        self.assertTrue(out["leave"])
        self.assertFalse(out["approved"], "options must replace, not accompany, approve/reject")

    def test_every_action_carries_the_question_id_it_acts_on(self):
        out = _probe(ROWS + """
        const html = renderQuestionQueue(rows, 1);
        console.log(JSON.stringify({
          ids: (html.match(/data-qid="Q2"/g) || []).length,
          wrong: (html.match(/data-qid="Q1"/g) || []).length
        }));
        """)
        self.assertGreaterEqual(out["ids"], 5)
        self.assertEqual(0, out["wrong"], "an action was wired to a question not on screen")


class Escaping(unittest.TestCase):
    def test_question_text_and_options_are_escaped(self):
        out = _probe("""
        const rows = [{id: 'Q<1>', text: '<img src=x onerror=boom>', detail: '<b>d</b>',
                       age_days: 1, refs: [], blocks: 0, recheck: null,
                       options: ['<script>evil()</script>']}];
        const html = renderQuestionQueue(rows, 0);
        console.log(JSON.stringify({
          img: html.includes('<img src=x'),
          script: html.includes('<script>'),
          escaped: html.includes('&lt;img src=x')
        }));
        """)
        self.assertFalse(out["img"], "question text was injected as live HTML")
        self.assertFalse(out["script"], "an option was injected as live HTML")
        self.assertTrue(out["escaped"])

    def test_a_recheck_note_is_escaped(self):
        out = _probe("""
        const html = questionRecheckHtml({recheck: {status: 'stale', note: '<img src=x>'}});
        console.log(JSON.stringify({raw: html.includes('<img src=x'), esc: html.includes('&lt;img')}));
        """)
        self.assertFalse(out["raw"])
        self.assertTrue(out["esc"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
