#!/usr/bin/env python3
"""The shared comment/command predicate, pinned by behaviour.

Three false-greens shipped because each guard carried its own `#` rule and none
of them was tested directly: a mention inside a quoted string counted as a call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from active_code import active_lines, invokes, python_args, unquoted, program_invokes, program_python_args  # noqa: E402

NAME = "discover-python-tests.sh"


class CommentBoundary(unittest.TestCase):
    def test_a_full_line_comment_is_dropped(self):
        self.assertEqual(active_lines("      # python3 dead.py"), [])

    def test_a_trailing_comment_is_cut_but_the_code_survives(self):
        self.assertEqual(active_lines("- run: python3 x.py  # why"),
                         ["- run: python3 x.py  "])

    def test_a_comment_after_a_separator_is_a_comment(self):
        """`:;# x` is a comment; a whitespace-only rule misses it."""
        self.assertEqual(active_lines("- run: :;# python3 dead.py"), ["- run: :;"])

    def test_a_hash_inside_a_token_is_not_a_comment(self):
        self.assertEqual(active_lines("colour=a#b"), ["colour=a#b"])

    def test_a_hash_inside_quotes_is_not_a_comment(self):
        self.assertEqual(active_lines('- run: echo "# not a comment"'),
                         ['- run: echo "# not a comment"'])


class CommandPosition(unittest.TestCase):
    def test_a_real_invocation_counts(self):
        for line in (f'bash scripts/{NAME} > "$F"', f"sh scripts/{NAME} > f",
                     f"scripts/{NAME} > f"):
            self.assertTrue(invokes(line, NAME), line)

    def test_an_assignment_is_not_an_invocation(self):
        """`T=x.sh` binds a name; the shell runs nothing and the file list is empty."""
        self.assertFalse(invokes(f"TARGET=scripts/{NAME}", NAME))

    def test_an_escaped_separator_does_not_start_a_command(self):
        r"""`echo a\; bash x.sh` is ONE echo -- the `;` is an argument."""
        self.assertFalse(invokes(rf"echo inert\; bash scripts/{NAME}", NAME))

    def test_a_longer_basename_is_not_this_command(self):
        """`not-x.sh` ends with `x.sh`; endswith accepted it, a basename does not."""
        self.assertFalse(invokes(f"bash scripts/not-{NAME}", NAME))

    def test_assignment_and_env_prefixes_still_find_the_real_call(self):
        """The peel must not reject a genuine call that carries prefixes."""
        self.assertTrue(invokes(f"VAR=1 bash scripts/{NAME}", NAME))
        self.assertTrue(invokes(f"env FOO=1 scripts/{NAME}", NAME))

    def test_an_echoed_quoted_call_is_not_an_invocation(self):
        """The defect: `echo "bash x.sh > f"` satisfied a substring+`>` check."""
        for line in (f'echo "bash scripts/{NAME} > files"', f"echo 'scripts/{NAME}'"):
            self.assertFalse(invokes(line, NAME), line)

    def test_a_commented_invocation_is_not_an_invocation(self):
        self.assertFalse(invokes(f"# bash scripts/{NAME} > f", NAME))
        self.assertFalse(invokes(f": > f ;# scripts/{NAME}", NAME))

    def test_an_invalid_identifier_prefix_is_not_an_assignment(self):
        # A hyphen can't start a shell identifier: bash tries to RUN it, rc=127.
        self.assertFalse(invokes(f"BAD-NAME=1 scripts/{NAME}", NAME))

    def test_a_trailing_backslash_inside_single_quotes_is_literal(self):
        # `'inert\'` closes there in bash — the `;` after it is a real separator.
        self.assertTrue(invokes(f"echo 'inert\\'; bash scripts/{NAME}", NAME))

    def test_unquoted_blanks_quoted_runs_only(self):
        self.assertEqual(unquoted("a 'bc' d"), "a      d")


class PythonArgsPosition(unittest.TestCase):
    def test_a_quoted_real_invocation_is_named(self):
        self.assertEqual(python_args("python3 'packages/x/test_real.py'"),
                         ["packages/x/test_real.py"])

    def test_an_echoed_unquoted_invocation_is_not_named(self):
        self.assertEqual(python_args("echo python3 packages/x/test_echo.py"), [])

    def test_a_timeout_wrapped_invocation_is_still_named(self):
        self.assertEqual(python_args("timeout -k 5 120 python3 x/test_real.py"),
                         ["x/test_real.py"])

    def test_a_bare_timeout_duration_with_no_flag_still_peels(self):
        self.assertEqual(python_args("timeout 120 python3 x/test_real.py"),
                         ["x/test_real.py"])

    def test_a_commented_invocation_is_not_named(self):
        self.assertEqual(python_args("# python3 dead.py"), [])


class ConditionalOperators(unittest.TestCase):
    """keweichen's [P2] on #4202: `&&`/`||` were split like `;`, so a command
    guarded by a false left side was still credited as invoked."""

    def test_a_short_circuited_and_call_is_not_invoked(self):
        self.assertFalse(invokes(
            f"false && bash scripts/{NAME} || true", NAME))

    def test_a_short_circuited_and_python_arg_is_not_named(self):
        self.assertEqual(python_args(
            "false && python3 packages/x/test_dead.py || true"), [])

    def test_an_unconditional_command_after_a_conditional_chain_still_counts(self):
        """`a; b && c; d` — `d` follows `;`, not `&&`, so it always runs."""
        self.assertTrue(invokes(f"echo hi; false && echo no; bash scripts/{NAME}", NAME))

    def test_the_first_command_of_an_and_chain_still_counts(self):
        self.assertTrue(invokes(f"bash scripts/{NAME} && echo done", NAME))

    def test_a_piped_command_still_counts(self):
        self.assertTrue(invokes(f"bash scripts/{NAME} | tee log", NAME))

    def test_a_backgrounded_command_still_counts(self):
        self.assertTrue(invokes(f"bash scripts/{NAME} &", NAME))


class MultiLinePrograms(unittest.TestCase):
    """keweichen's third [P2] on #4202: the AND-OR state `_segments()` tracks
    ended at each physical line, so `false &&` on one line never guarded the
    command on the next. Program-level scanning keeps it across the break."""

    def test_a_guard_at_the_end_of_a_line_guards_the_next_line(self):
        self.assertFalse(program_invokes(f": > files\nfalse &&\n  bash scripts/{NAME} > files || true\n", NAME))
        self.assertEqual(program_python_args("false &&\n  python3 packages/x/test_dead.py || true\n"), [])

    def test_a_newline_ends_an_unguarded_command_like_a_semicolon(self):
        self.assertTrue(program_invokes(f": > files\nbash scripts/{NAME} > files\n", NAME))
        self.assertEqual(program_python_args("echo hi\npython3 x/test_real.py\n"), ["x/test_real.py"])

    def test_a_guard_does_not_leak_past_the_line_it_closed_on(self):
        """`false && echo no` is complete on its line; the next line is unconditional."""
        self.assertTrue(program_invokes(f"false && echo no\nbash scripts/{NAME}\n", NAME))

    def test_a_backslash_continuation_is_one_logical_line(self):
        self.assertFalse(program_invokes(f"false && \\\n  bash scripts/{NAME}\n", NAME))

    def test_a_comment_line_inside_a_program_is_not_a_command(self):
        self.assertFalse(program_invokes(f"false &&\n  # bash scripts/{NAME}\n", NAME))

    def test_the_single_line_forms_agree_with_the_program_forms(self):
        line = f"echo hi; false && echo no; bash scripts/{NAME}"
        self.assertEqual(invokes(line, NAME), program_invokes(line, NAME))
        self.assertEqual(python_args("python3 x/a.py; false && python3 x/b.py"),
                         program_python_args("python3 x/a.py; false && python3 x/b.py"))


class PythonArgsScriptOperand(unittest.TestCase):
    """keweichen's second repro on the same [P2]: a `.py`-looking argument to
    `-c`/`-m` is the script's OWN argv, not something python loads."""

    def test_a_dash_c_argument_is_not_named(self):
        self.assertEqual(python_args("python3 -c 'pass' packages/x/test_argv.py"), [])

    def test_a_dash_m_argument_is_not_named(self):
        self.assertEqual(python_args("python3 -m mymod packages/x/test_argv.py"), [])

    def test_an_attached_dash_c_is_still_dash_c(self):
        """Python accepts `-cpass`; the flag is the letter, not the token."""
        self.assertEqual(python_args("python3 -cpass packages/x/test_dead.py"), [])
        self.assertEqual(python_args("python3 -mmymod packages/x/test_argv.py"), [])

    def test_a_clustered_short_option_carrying_c_is_dash_c(self):
        self.assertEqual(python_args("python3 -uc 'pass' packages/x/test_dead.py"), [])
        # A cluster WITHOUT c/m still leaves the script operand in place.
        self.assertEqual(python_args("python3 -uB packages/x/test_real.py"), ["packages/x/test_real.py"])

    def test_only_the_first_dot_py_token_is_named(self):
        """An argument to the real script that happens to end in .py is not
        itself invoked — only the script operand is."""
        self.assertEqual(
            python_args("python3 x/test_real.py --fixture x/data.py"),
            ["x/test_real.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
