#!/usr/bin/env python3
"""A compaction must leave a durable trace, and the recorder must be reachable.

Nothing on disk marked a context compaction, so "did context roll over just
before that failure?" was unanswerable. Written as .test.py because CI collects
`find tests -name '*.test.py'` — a .test.sh sibling is never run.

Nothing here runs session-handoff.sh end to end. There is no verified way to
point it at a throwaway workspace: a temp repo carrying its own
sutando.config.local.json still resolved to the LIVE workspace when measured, so
a harness that "ran the script" would write into the owner's real state/.
"""
from pathlib import Path
import json
import re
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "session-handoff.sh"
FN = "record_compaction_event"


def _fn_source() -> str:
    """The real function body, extracted — never a re-typed copy."""
    text = SCRIPT.read_text()
    m = re.search(rf"^{FN}\(\) \{{.*?^\}}", text, re.S | re.M)
    if not m:
        raise AssertionError(f"{FN} not found in {SCRIPT}")
    return m.group(0)


def _run(workspace: Path, *calls: tuple) -> subprocess.CompletedProcess:
    """Exec the extracted function against a temp WORKSPACE_DIR."""
    body = [f'WORKSPACE_DIR={workspace!s}', _fn_source()]
    for transcript, trigger in calls:
        body.append(f'{FN} "{transcript}" "{trigger}"')
    body.append("exit 0")
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write("\n".join(body))
        path = fh.name
    return subprocess.run(["bash", path], capture_output=True, text=True, timeout=30)


class CallSiteIsReachable(unittest.TestCase):
    """A recorder nothing calls is the defect this change is about."""

    def setUp(self):
        self.text = SCRIPT.read_text()
        self.lines = self.text.splitlines()

    def test_the_function_is_defined(self):
        # (?m) is load-bearing: assertRegex uses re.search, so a bare ^ anchors
        # to the start of the whole file, not to a line.
        self.assertRegex(self.text, rf"(?m)^{FN}\(\) \{{", "recorder is not defined")

    def _line_of(self, pattern):
        for i, l in enumerate(self.lines, 1):
            if re.match(pattern, l):
                return i
        return None

    def test_there_is_a_column_zero_call_site(self):
        """A bare grep also matches a call nested in a function or an if."""
        self.assertIsNotNone(self._line_of(rf'^{FN} "'),
                             "defined but never called at top level")

    def test_the_call_is_outside_the_definition(self):
        start = self._line_of(rf"^{FN}\(\) \{{")
        end = next(i for i, l in enumerate(self.lines[start:], start + 1)
                   if l == "}")
        call = self._line_of(rf'^{FN} "')
        self.assertGreater(call, end,
                           f"call at {call} is inside the body (ends {end})")

    def test_no_unclosed_conditional_precedes_the_call(self):
        """Column 0 rules out an indented call, not a top-level `if` wrapping it."""
        start = self._line_of(rf"^{FN}\(\) \{{")
        end = next(i for i, l in enumerate(self.lines[start:], start + 1)
                   if l == "}")
        call = self._line_of(rf'^{FN} "')
        between = self.lines[end:call - 1]
        opens = sum(1 for l in between if re.match(r"^(if|case|while|until|for) ", l))
        closes = sum(1 for l in between if re.match(r"^(fi|esac|done)$", l))
        self.assertLessEqual(opens, closes,
                             f"unclosed conditional before the call "
                             f"(opens={opens} closes={closes}) — may not run")

    def test_the_log_lives_under_state(self):
        self.assertIn("state/compactions.jsonl", self.text,
                      "workspace contract puts state under state/")


class TheRecordItWrites(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.log = self.ws / "state" / "compactions.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def test_one_valid_json_line_with_every_field(self):
        _run(self.ws, ("/some/path/transcript-abc.jsonl", "precompact"))
        self.assertTrue(self.log.is_file(), "no line written")
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        for k in ("ts", "epoch", "host", "transcript", "trigger"):
            self.assertIn(k, rec)
        self.assertEqual(rec["transcript"], "transcript-abc.jsonl",
                         "transcript must be the basename, not the full path")
        self.assertEqual(rec["trigger"], "precompact")
        self.assertIsInstance(rec["epoch"], int)

    def test_it_appends_rather_than_overwrites(self):
        _run(self.ws, ("/a.jsonl", "precompact"), ("/b.jsonl", "precompact"))
        self.assertEqual(len(self.log.read_text().strip().splitlines()), 2)

    def test_bounded_so_a_long_lived_core_cannot_grow_it_forever(self):
        self.log.write_text('{"ts":"x","epoch":0}\n' * 600)
        _run(self.ws, ("/third.jsonl", "precompact"))
        lines = self.log.read_text().strip().splitlines()
        self.assertLessEqual(len(lines), 500, "unbounded")
        self.assertIn("third.jsonl", lines[-1],
                      "the trim must keep the NEWEST event, not the oldest")

    def test_an_unwritable_log_is_never_fatal(self):
        """This runs inside PreCompact; a nonzero exit is worse than no line."""
        ws = Path(self._td.name) / "ro"
        ws.mkdir()
        (ws / "state").write_text("a file where the dir must go")
        self.assertEqual(_run(ws, ("/x.jsonl", "precompact")).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
