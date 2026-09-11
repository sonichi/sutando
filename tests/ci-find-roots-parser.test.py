#!/usr/bin/env python3
"""The wiring parser in ci-covers-every-python-test must not be fooled by shell text.

Run: python3 tests/ci-find-roots-parser.test.py

Each case below was a live false-green in review (keweichen, 2026-09-11): the
parser returned `skills + tests` while the shell reached only `tests`. They are
fixtures here rather than hand-run controls, because a mutation nobody checked in
lets a reversion to the broken parser pass on intact wiring.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("g", REPO / "tests" / "ci-covers-every-python-test.test.py")
_g = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_g)
except SystemExit:
    pass

FIND = """          find "${roots[@]}" -name '*.test.py' -not -path '*/node_modules/*' | sort\n"""

# (label, wiring body, roots the SHELL actually reaches)
CASES = [
    ("intact",              'roots=(tests); if [ -d skills ]; then roots+=(skills); fi\n' + FIND, {"tests", "skills"}),
    ("commented append",    'roots=(tests)\n# roots+=(skills)\n' + FIND,                          {"tests"}),
    ("trailing comment",    'roots=(tests)  # roots+=(skills)\n' + FIND,                          {"tests"}),
    ("reset after append",  'roots=(tests); roots+=(skills); roots=(tests)\n' + FIND,             {"tests"}),
    ("similar var name",    'roots=(tests)\noldroots+=(skills)\n' + FIND,                         {"tests"}),
    ("literal find args",   'roots=(tests); roots+=(skills)\n' + "          find tests -name '*.test.py'\n", {"tests"}),
]


class TestFindRootsParser(unittest.TestCase):
    def test_each_shell_shape_reports_what_the_shell_reaches(self):
        with tempfile.TemporaryDirectory() as td:
            for label, body, want in CASES:
                f = Path(td) / f"{label.replace(' ', '_')}.sh"
                f.write_text(body)
                got = _g._find_roots_in(f)
                self.assertEqual(got, want,
                                 f"[{label}] parser says {sorted(got)}, shell reaches {sorted(want)}")

    def test_a_file_with_no_discovery_find_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "none.sh"; f.write_text("roots=(tests skills)\necho hi\n")
            with self.assertRaises(AssertionError):
                _g._find_roots_in(f)

    def test_two_discovery_finds_refuse_rather_than_check_one(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "two.sh"; f.write_text('roots=(tests)\n' + FIND + FIND)
            with self.assertRaises(AssertionError):
                _g._find_roots_in(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
