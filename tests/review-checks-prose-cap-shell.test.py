#!/usr/bin/env python3
"""Shell comment classification for the prose-cap gate.

The gate previously supported `.py` only, and fail-closed on anything else, so
the repo's two-line comment cap was unenforced on 216 tracked `.sh` files. The
blocker was never the `#` character: the cap counts runs of lines that are
ENTIRELY comments, so a mid-line `#` cannot open one. The live hazard is a
heredoc BODY line beginning with `#` -- 410 such lines in this repo.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "prose_cap", REPO / "scripts" / "review-checks-prose-cap.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class ShellCommentLines(unittest.TestCase):
    def setUp(self):
        self.m = _mod()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _w(self, body, name="f.sh"):
        p = Path(self.tmp.name) / name
        p.write_text(body)
        return str(p)

    def test_plain_comments_are_found(self):
        p = self._w("#!/bin/bash\n# one\n# two\necho hi\n")
        self.assertEqual(self.m.comment_lines(p), {2, 3})

    def test_shebang_is_not_a_comment(self):
        p = self._w("#!/bin/bash\necho hi\n")
        self.assertEqual(self.m.comment_lines(p), set())

    # --- the hazard this exists for -------------------------------------
    def test_heredoc_body_hashes_are_NOT_comments(self):
        p = self._w("cat <<'EOF' > out\n# not a comment\n# also not\nEOF\n# real\n")
        self.assertEqual(self.m.comment_lines(p), {5})

    def test_unquoted_heredoc_body_too(self):
        p = self._w("cat <<EOF\n# data\nEOF\n# real\n")
        self.assertEqual(self.m.comment_lines(p), {4})

    def test_dash_heredoc_allows_tab_indented_terminator(self):
        p = self._w("cat <<-EOF\n# data\n\tEOF\n# real\n")
        self.assertEqual(self.m.comment_lines(p), {4})

    # --- mid-line '#' is irrelevant, and that is the design claim -------
    def test_parameter_expansion_hash_is_not_a_comment_line(self):
        p = self._w('x=${v#pat}\ny="a#b"\n# real\n')
        self.assertEqual(self.m.comment_lines(p), {3})

    # --- here-STRING is not a heredoc -----------------------------------
    def test_here_string_does_not_open_a_heredoc(self):
        """`<<<` must not match -- guard BOTH sides, or it matches at offset 1."""
        p = self._w("grep x <<< \"$v\"\n# real\n# also real\n")
        self.assertEqual(self.m.comment_lines(p), {2, 3})

    def test_here_string_inside_a_quoted_string(self):
        p = self._w("M='# <<< managed block'\n# real\n")
        self.assertEqual(self.m.comment_lines(p), {2})

    # --- fail closed, never guess ---------------------------------------
    def test_unterminated_heredoc_is_undecidable_not_empty(self):
        """None (caller fails closed), never an empty set that reads as PASS."""
        p = self._w("cat <<EOF\n# data\nnever terminated\n")
        self.assertIsNone(self.m.comment_lines(p))

    def test_python_path_is_unchanged(self):
        p = self._w("# one\n# two\nx = 1  # trailing\n", name="f.py")
        self.assertEqual(self.m.comment_lines(p), {1, 2, 3})

    def test_accepts_a_Path_not_only_a_str(self):
        """The production caller passes PosixPath; a str-only fixture hides that."""
        p = Path(self.tmp.name) / "viapath.sh"
        p.write_text("cat <<EOF\n# data\nEOF\n# real\n")
        self.assertEqual(self.m.comment_lines(p), {4})

    def test_shell_ext_is_now_supported(self):
        self.assertIn(".sh", self.m.SUPPORTED_EXTS)
        self.assertIn(".py", self.m.SUPPORTED_EXTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
