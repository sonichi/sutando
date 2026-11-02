"""A truncated candidate list must count what it withholds.

`report()` prints the TRUE number of candidates in its header and used to print
three of them. Hits are ordered by token then file — not by relevance — so the
three shown are an arbitrary subset, and a reader who verified every line on
screen could conclude "not parked" while the matching entry sat below the cut.
That is the failure this pins: the header and the body disagreed silently.
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


def _parking(dirpath, n):
    """n parking files, each with ONE heading carrying the token."""
    out = []
    for i in range(n):
        p = pathlib.Path(dirpath) / f"note-{i:02d}.md"
        p.write_text(f"## snowflake-decode case {i}\n\nbody\n")
        out.append(p)
    return out


def _run(files, claim="the `snowflake-decode` path"):
    wat._LINES.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        verdict = wat.report(None, claim, files)
    return verdict, buf.getvalue()


class Truncation(unittest.TestCase):
    def test_the_header_count_and_the_shown_lines_agree_or_the_gap_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            files = _parking(d, 20)   # literal, so this arm does not
                                     # depend on the constant existing
            verdict, out = _run(files)
        self.assertEqual(verdict, "parked")
        shown = out.count("via '")
        header = int(out.split("(", 1)[1].split(")", 1)[0])
        self.assertGreater(header, shown, "fixture must exceed the display cap")
        # The gap is the whole point: it must be stated, with the real number.
        self.assertIn(f"+{header - shown} further candidate", out)

    def test_nothing_is_withheld_silently_below_the_cap(self):
        with tempfile.TemporaryDirectory() as d:
            files = _parking(d, 3)
            _, out = _run(files)
        self.assertEqual(out.count("via '"), 3)
        self.assertNotIn("further candidate", out)

    def test_the_cap_is_large_enough_to_have_shown_the_real_miss(self):
        """The incident had 7 heading hits and the answer was the 7th. A cap of
        3 hid it; any cap below 7 would hide it again."""
        self.assertGreaterEqual(getattr(wat, "SHOW_CANDIDATES", 3), 7)


if __name__ == "__main__":
    unittest.main()
