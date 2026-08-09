#!/usr/bin/env python3
"""`[dm-only]` may over-trigger routing, but it must never edit the owner's text.

The marker is DETECTED with `search()` anywhere in the body. That is deliberate
and documented: it is what makes the privacy guard undefeatable by marker ORDER,
so a body carrying private data can never be redirected to a shared channel.
Over-triggering that guard fails SAFE — a reply goes to the DM instead of a
channel — so detection is left exactly as it was.

Stripping was the problem. The same anywhere-matching regex also removed every
occurrence from the delivered body, so a result that merely DISCUSSED the marker
was silently rewritten:

    in   - #2170 [dm-only]: closes the leak vector, but only suppresses redirects
    out  - #2170 : closes the leak vector, but only suppresses redirects

That is not a routing outcome and it does not fail safe — it is silent
corruption of owner-facing text with no indication. Detection and stripping were
already separate statements, so narrowing the strip costs the guard nothing.

Fix: strip only a STANDALONE marker (alone on its line). Detection unchanged.

Run: python3 tests/dm-only-strip-preserves-prose.test.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import result_markers as rm  # noqa: E402

PROSE = "- #2170 [dm-only]: closes the leak vector, but only suppresses redirects"
REDIRECT = "[channel: 1485653767402553457]"


def kinds(body: str) -> "list[str]":
    return [a.kind for a in rm.parse_markers(body).actions]


def out_body(body: str) -> str:
    return rm.parse_markers(body).body.strip()


class TestGuardIsUnchanged(unittest.TestCase):
    """Detection must still be anywhere — these pass BEFORE and AFTER the fix,
    by design. They exist so a future 'tidy-up' cannot quietly anchor it."""

    def test_standalone_marker_still_routes_dm_only(self):
        self.assertIn("dm-only", kinds("[dm-only]\nPrivate body."))

    def test_marker_after_a_redirect_still_suppresses_it(self):
        acts = kinds(f"{REDIRECT}\n[dm-only]\nPrivate.")
        self.assertIn("dm-only", acts)
        self.assertNotIn("redirect", acts, "order-independence is the guarantee")

    def test_prose_mention_still_routes_dm_only(self):
        """Over-triggering is retained ON PURPOSE — it fails safe."""
        self.assertIn("dm-only", kinds(f"{REDIRECT}\n{PROSE}"))

    def test_a_body_with_neither_still_redirects(self):
        self.assertIn("redirect", kinds(f"{REDIRECT}\nOrdinary reply."))


class TestBodyIsNoLongerMangled(unittest.TestCase):
    """The regression. Each of these FAILS against origin/main."""

    def test_prose_mention_survives_verbatim(self):
        self.assertEqual(out_body(PROSE), PROSE)

    def test_prose_mention_survives_alongside_a_redirect(self):
        self.assertIn("[dm-only]", out_body(f"{REDIRECT}\n{PROSE}"))

    def test_mid_sentence_marker_is_not_excised(self):
        b = "We set [dm-only] on that result and it worked."
        self.assertEqual(out_body(b), b)


class TestStandaloneStrippingStillWorks(unittest.TestCase):
    """The behaviour the strip exists for must survive the narrowing."""

    def test_standalone_marker_is_removed_from_the_body(self):
        self.assertNotIn("[dm-only]", out_body("[dm-only]\nPrivate body."))

    def test_indented_standalone_marker_is_removed(self):
        self.assertNotIn("[dm-only]", out_body("   [dm-only]   \nPrivate."))

    def test_standalone_marker_after_redirect_is_removed(self):
        self.assertNotIn("[dm-only]", out_body(f"{REDIRECT}\n[dm-only]\nPrivate."))

    def test_stripping_does_not_eat_surrounding_content(self):
        body = "[dm-only]\nline one\nline two"
        self.assertEqual(out_body(body), "line one\nline two")


class TestTaskBridgeTsParity(unittest.TestCase):
    r"""`src/task-bridge.ts` is the one result consumer that CANNOT call
    parse_markers (wrong language), so `tests/bridge-marker-no-leak.test.py`'s
    route-through-the-parser invariant structurally cannot cover it — and that
    hole is exactly where this regression landed.

    After the Python strip was narrowed, task-bridge kept its own
    `/\[dm-only\]\s*/gi`, so the SAME body was preserved on
    Discord/Slack/Telegram and silently rewritten on voice/task. Inconsistent
    delivery is worse than uniformly-wrong delivery. If it must hand-roll, the
    expression is pinned here to the Python semantics."""

    TS = REPO / "src" / "task-bridge.ts"

    def _expr(self) -> str:
        m = re.search(r"\.replace\(\s*(/[^\n]*?dm-only[^\n]*?/[gimsuy]*)", self.TS.read_text())
        self.assertIsNotNone(m, "could not find the dm-only replace() in task-bridge.ts")
        return m.group(1)

    def test_expression_is_anchored_multiline_and_case_insensitive(self):
        r"""The regression was an UNANCHORED /[dm-only]\s*/gi."""
        e = self._expr()
        flags = e.rsplit("/", 1)[1]
        self.assertIn("^", e, "must anchor to line start = standalone only")
        self.assertIn("m", flags, "needs multiline or ^ only matches the body start")
        self.assertIn("i", flags, "Python side is IGNORECASE")

    def _run_js(self, body: str) -> str:
        if not shutil.which("node"):
            self.skipTest("node not available")
        e = self._expr()
        pat, flags = e.rsplit("/", 1)
        js = ("const s=%r;process.stdout.write(s.replace(%s/%s,'').trim());"
              % (body, pat, flags))
        return subprocess.run(["node", "-e", js], capture_output=True,
                              text=True, timeout=30).stdout

    def test_inline_prose_survives_the_voice_task_path(self):
        """Executed against the shipped expression, not asserted from source."""
        self.assertEqual(self._run_js(PROSE), PROSE)

    def test_standalone_marker_is_still_stripped(self):
        self.assertEqual(self._run_js("[dm-only]\nPrivate body."), "Private body.")

    def test_ts_and_python_agree_on_every_dm_only_case(self):
        """Parity, both engines, same inputs."""
        for body in (PROSE, "[dm-only]\nPrivate body.",
                     "   [dm-only]   \nPrivate.",
                     "We set [dm-only] mid-sentence."):
            with self.subTest(body=body[:32]):
                self.assertEqual(self._run_js(body), out_body(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
