"""A body-mention count must count mentions, and surface the NEWEST one.

`report()`'s no-heading branch used `break` after the first matching line in each
file, so `len(body)` counted (token, file) pairs rather than mentions: a file
holding twelve mentions of the subject reported "1 body mention(s)". It also
printed `body[0]`'s first line. Parking files are append-only, so that is the
OLDEST entry — the earliest guess — while the newest one carries the verdict and
any standing instruction.

Sibling guard: `warn-already-triaged-truncation.test.py` pins the same
header-disagrees-with-body failure on the CANDIDATES (heading) branch.
"""
import contextlib
import importlib.util
import io
import pathlib
import tempfile
import unittest

_SRC = (pathlib.Path(__file__).resolve().parents[1]
        / "skills" / "proactive-loop" / "scripts" / "warn-already-triaged.py")
_spec = importlib.util.spec_from_file_location("wat", _SRC)
wat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wat)

TOK = "snowflake-decode"


def _run(files, claim=f"the `{TOK}` path"):
    wat._LINES.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        verdict = wat.report(None, claim, files)
    return verdict, buf.getvalue()


class BodyMentionCount(unittest.TestCase):
    def test_counts_every_matching_line_not_one_per_file(self):
        """12 mentions in one file must not report as 1."""
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "build_log.md"
            p.write_text("".join(f"- pass note {i}: {TOK} re-fired\n"
                                 for i in range(12)))
            verdict, out = _run([p])
        self.assertEqual(verdict, "parked")
        self.assertIn("12 body mention(s)", out)
        self.assertNotIn("1 body mention(s)", out)

    def test_surfaces_the_newest_line_not_the_oldest(self):
        """The reported line must be the LAST match, with the first kept for context."""
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "build_log.md"
            # token on lines 1 and 5; nothing on the lines between.
            p.write_text(f"oldest: {TOK} looked transient\nfiller\nfiller\nfiller\n"
                         f"newest: {TOK} — do not re-investigate\n")
            verdict, out = _run([p])
        self.assertEqual(verdict, "parked")
        self.assertIn("NEWEST", out)
        self.assertIn("build_log.md:5", out)
        self.assertIn("oldest :1", out)

    def test_single_mention_still_reads_as_one(self):
        """Positive control: the honest count of one mention is still 1."""
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "notes.md"
            p.write_text(f"- only note: {TOK} happened once\n")
            verdict, out = _run([p])
        self.assertEqual(verdict, "parked")
        self.assertIn("1 body mention(s)", out)

    def test_heading_branch_still_wins_over_body(self):
        """Negative control: a heading match must NOT fall through to this branch."""
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "notes.md"
            p.write_text(f"## {TOK} triage\n\nbody mentions {TOK} again\n")
            verdict, out = _run([p])
        self.assertEqual(verdict, "parked")
        self.assertIn("CANDIDATES", out)
        self.assertNotIn("NO HEADING", out)
class MultiTokenAxis(unittest.TestCase):
    """Every test above uses ONE token; these pin the multi-token axis (sonichi, #4098).

    A claim normally yields several tokens, so a line matching two of them was
    counted twice, and the surfaced pair was chosen by token order rather than by
    which file actually holds the record.
    """

    def test_a_line_matching_two_tokens_counts_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "build_log.md"
            p.write_text("".join(f"- alpha-probe and beta-probe both here {i}\n"
                                 for i in range(5)))
            verdict, out = _run([p], claim="the `alpha-probe` and `beta-probe` paths")
        self.assertEqual(verdict, "parked")
        self.assertIn("5 body mention(s)", out)
        self.assertNotIn("10 body mention(s)", out)
        self.assertNotIn("+5", out)

    def test_the_file_holding_the_record_is_surfaced_not_the_first_token(self):
        """A 1-line aside must not outrank a 12-line standing instruction."""
        with tempfile.TemporaryDirectory() as d:
            aside = pathlib.Path(d) / "notes.md"
            aside.write_text("aside: alpha-probe mentioned once in passing\n")
            log = pathlib.Path(d) / "build_log.md"
            log.write_text("".join(f"- beta-probe re-fired; DO NOT re-investigate {i}\n"
                                   for i in range(12)))
            verdict, out = _run([aside, log],
                                claim="the `alpha-probe` and `beta-probe` paths")
        self.assertEqual(verdict, "parked")
        self.assertIn("12 body mention(s) in build_log.md", out)
        self.assertIn("build_log.md:12", out)
        self.assertNotIn("notes.md:1, oldest", out)

    def test_the_newest_lines_own_token_is_the_one_named(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "build_log.md"
            p.write_text("alpha-probe first\nfiller\nbeta-probe newest\n")
            verdict, out = _run([p], claim="the `alpha-probe` and `beta-probe` paths")
        self.assertEqual(verdict, "parked")
        self.assertIn("NEWEST 'beta-probe'", out)
        self.assertIn("build_log.md:3", out)

if __name__ == "__main__":
    unittest.main()
