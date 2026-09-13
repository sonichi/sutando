#!/usr/bin/env python3
"""The question queue must visibly redraw after Next/Dismiss, not just update state.

Sibling review on #4003 (2026-09-13): updateDynamicRegion() skips rendering while
window._drLocalContent is true — set by switchDRTab() for as long as the questions
tab is open — so advanceQuestionQueue()/dropQuestionFromQueue() changed the queue's
state but the card on screen never moved. tests/web-client-question-queue.test.py
only extracts the PURE render helpers (renderQuestionQueue, questionQueueCursor,
...); it never calls the two functions this bug lives in, so 15/15 passed there
throughout. This test runs the REAL, unmodified action functions against a minimal
synthetic DOM, the way the review itself verified the defect.

Run: python3 tests/web-client-question-queue-redraw.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()

# ensureTabStructure through dropQuestionFromQueue: everything the two action
# functions call, stopping before dismissQuestion (which reaches fetch()).
START = "function ensureTabStructure() {"
END = "function dismissQuestion(qid) {"

# The unreached 'tasks' branch's regex /^\\[/ needs later-file context this
# slice cuts away to close; neutralize it so the slice parses (see PR body).
_HAZARD = r"/^\\[/.test(rawText)"


def _action_source() -> str:
    assert START in SOURCE, "the tab-highlight init line moved or was reworded"
    assert END in SOURCE, "dismissQuestion moved — re-anchor the slice"
    assert SOURCE.count(_HAZARD) >= 1, "the tasks-branch regex moved — re-check the neutralization"
    start = SOURCE.index(START)
    end = SOURCE.index(END, start)
    return SOURCE[start:end].replace(_HAZARD, "false")


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
var document = {
  _els: {},
  getElementById: function(id) {
    if (!document._els[id]) document._els[id] = {id: id, innerHTML: '', style: {}};
    return document._els[id];
  }
};
var window = {};
"""

# A fresh render every time, so a dismiss test can't pass on content a prior
# Next in the same run happened to leave behind (see PR body).
SETUP = r"""
window._drActiveTab = 'questions';
window._drLocalContent = true;   // the state the whole time the tab is open
window._drQueueChecked = true;   // skip the fetch-based refresh inside renderTabContent
window._drTaskCount = 0;
window._drNoteCount = 0;
window._drQueue = [
  {id: 'Q1', text: 'First question', detail: '', age_days: 5, refs: [], blocks: 0, recheck: null},
  {id: 'Q2', text: 'Second question', detail: '', age_days: 3, refs: [], blocks: 0, recheck: null}
];
window._drQuestions = window._drQueue.slice();
window._drQueueIndex = 0;
renderTabContent();
var initial = document.getElementById('dr-content').innerHTML;
"""


def _run(driver_tail: str):
    program = SHIM + _action_source() + "\n" + SETUP + driver_tail
    result = subprocess.run(
        [_node(), "--input-type=module", "-e", program],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)


class QueueRedraw(unittest.TestCase):
    def test_next_visibly_advances_the_card(self):
        out = _run(r"""
            advanceQuestionQueue(1);  // Next, the way the click handler calls it
            console.log(JSON.stringify({
              initial: initial,
              after: document.getElementById('dr-content').innerHTML,
            }));
        """)
        self.assertIn("First question", out["initial"])
        self.assertNotIn("First question", out["after"],
                         "Next changed the cursor but the card on screen did not move")
        self.assertIn("Second question", out["after"])

    def test_dismiss_visibly_removes_the_card_it_dismissed(self):
        # Dismiss the FIRST card directly — no prior Next — so a pass cannot be
        # explained by content a different action left on screen.
        out = _run(r"""
            dropQuestionFromQueue('Q1');
            console.log(JSON.stringify({
              initial: initial,
              after: document.getElementById('dr-content').innerHTML,
              remainingIds: window._drQueue.map(function(q) { return q.id; }),
            }));
        """)
        self.assertIn("First question", out["initial"])
        self.assertNotIn("First question", out["after"],
                         "Dismiss removed Q1 from the queue but its card stayed on screen")
        self.assertIn("Second question", out["after"])
        self.assertEqual(["Q2"], out["remainingIds"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
