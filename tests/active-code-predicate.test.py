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


class DeadBranches(unittest.TestCase):
    """qingyun-wu's blocking finding on #4202: a compound-command guard credited
    a Python test that sits inside a static `if false` branch Bash never runs."""

    def test_a_dead_if_false_branch_is_not_named(self):
        self.assertEqual(program_python_args(
            "if false\nthen\n  python3 packages/x/test_dead.py\nfi\n"), [])

    def test_the_same_dead_branch_with_then_on_the_if_line_is_not_named(self):
        self.assertEqual(program_python_args(
            "if false; then\n  python3 packages/x/test_dead.py\nfi\n"), [])

    def test_an_if_true_branch_still_counts(self):
        self.assertEqual(program_python_args(
            "if true\nthen\n  python3 packages/x/test_live.py\nfi\n"),
            ["packages/x/test_live.py"])

    def test_the_else_of_a_dead_if_false_still_counts(self):
        self.assertEqual(program_python_args(
            "if false\nthen\n  python3 packages/x/test_dead.py\n"
            "else\n  python3 packages/x/test_alive.py\nfi\n"),
            ["packages/x/test_alive.py"])

    def test_the_else_of_an_if_true_is_dead(self):
        self.assertEqual(program_python_args(
            "if true\nthen\n  python3 packages/x/test_live.py\n"
            "else\n  python3 packages/x/test_dead.py\nfi\n"),
            ["packages/x/test_live.py"])

    def test_an_undecidable_condition_credits_both_branches(self):
        """Only a literal `false`/`true` is decidable here; anything else must
        not be silently dropped either way."""
        self.assertEqual(program_python_args(
            'if [ "$X" = y ]\nthen\n  python3 packages/x/test_a.py\n'
            "else\n  python3 packages/x/test_b.py\nfi\n"),
            ["packages/x/test_a.py", "packages/x/test_b.py"])

    def test_a_nested_dead_branch_inside_a_live_one_is_not_named(self):
        self.assertEqual(program_python_args(
            "if true\nthen\n  if false\n  then\n    python3 packages/x/test_inner_dead.py\n  fi\n"
            "  python3 packages/x/test_outer_live.py\nfi\n"),
            ["packages/x/test_outer_live.py"])

    def test_glued_single_line_dead_form_is_not_named(self):
        self.assertEqual(program_python_args(
            "if false; then python3 packages/x/test_dead.py; fi\n"), [])


class GluedBranchCommand(unittest.TestCase):
    """qingyun-wu's + keweichen's blocking finding on round 2 of #4202: a
    real command glued onto the SAME segment as `then`/`else` was dropped
    outright instead of analyzed, so `if true; then python3 x.py; fi` read
    as uninvoked though Bash runs it."""

    def test_glued_then_on_a_live_branch_is_named(self):
        self.assertEqual(program_python_args(
            "if true; then python3 packages/x/test_live.py; fi\n"),
            ["packages/x/test_live.py"])

    def test_glued_else_on_a_live_branch_is_named(self):
        self.assertEqual(program_python_args(
            "if false; then :; else python3 packages/x/test_live.py; fi\n"),
            ["packages/x/test_live.py"])

    def test_glued_then_on_a_dead_branch_is_still_not_named(self):
        self.assertEqual(program_python_args(
            "if false; then python3 packages/x/test_dead.py; fi\n"), [])

    def test_glued_else_on_a_dead_branch_is_still_not_named(self):
        self.assertEqual(program_python_args(
            "if true; then :; else python3 packages/x/test_dead.py; fi\n"), [])

    def test_a_nested_if_glued_to_the_outer_then_is_not_named(self):
        """keweichen's round-3 finding on #4202: the outer `then`'s glued
        remainder can itself be a whole `if` statement, whose own dead
        branch must still be dropped, not credited as a plain command."""
        self.assertEqual(program_python_args(
            "if true; then if false; then python3 packages/x/test_dead.py; fi; fi\n"), [])

    def test_a_nested_ifs_else_glued_to_the_outer_then_is_named(self):
        self.assertEqual(program_python_args(
            "if true; then if false; then python3 packages/x/dead.py; "
            "else python3 packages/x/live.py; fi; fi\n"), ["packages/x/live.py"])

    def test_a_sibling_after_a_dead_nested_if_stays_dead(self):
        """keweichen's round-4 finding: skipping dispatch for a DEAD glued
        remainder never pushed the nested if's own frame, so its `fi`
        popped the OUTER frame instead -- a sibling command right after
        read as reachable again though the outer condition is still false."""
        self.assertEqual(program_python_args(
            "if false; then if true; then python3 packages/x/dead1.py; fi; "
            "python3 packages/x/dead2.py; fi\n"), [])


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

    def test_dash_w_takes_a_separate_value_and_a_script_still_follows(self):
        """keweichen's [P2]: -W's value isn't the flag itself, so the loop
        must skip it rather than stop and misread it as the script."""
        self.assertEqual(python_args("python3 -W ignore packages/x/test_real.py"),
                          ["packages/x/test_real.py"])

    def test_dash_x_value_looking_like_a_dot_py_is_not_the_script(self):
        """The value itself must not be credited even when it ends in .py —
        `-c pass` means no script ever loads."""
        self.assertEqual(python_args("python3 -X packages/x/test_x.py -c pass"), [])

    def test_an_attached_dash_w_value_is_still_skipped_as_one_token(self):
        """keweichen's round-3 finding: an ATTACHED -W/-X value (`-Wmodule`)
        must not be misread as a `-c`/`-m` cluster just because the value
        happens to contain the letter c or m."""
        self.assertEqual(python_args("python3 -Wmodule packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -Xtracemalloc packages/x/test_real.py"),
                          ["packages/x/test_real.py"])

    def test_a_clustered_prefix_before_w_or_x_still_takes_a_separate_value(self):
        """keweichen's round-4 finding: `-uW` ends in W just like bare `-W`,
        so its value is the NEXT token too, not attached to `-uW` itself."""
        self.assertEqual(python_args("python3 -uW ignore packages/x/test_real.py"),
                          ["packages/x/test_real.py"])

    def test_a_clustered_prefix_before_x_then_a_real_dash_c_names_nothing(self):
        """The clustered `-uX`'s value is the next token (`dev.py`, never a
        script); the real `-c` right after still consumes everything else."""
        self.assertEqual(python_args("python3 -uX dev.py -c pass"), [])

    def test_a_clustered_flag_owns_the_value_right_after_it_in_the_token(self):
        """keweichen's round-5 finding: a last-char-only test can't tell a
        separate-value flag from an attached value that itself ends in
        W/X. `-uWX` has W owning attached value "X"; `-uXdevW` has X
        owning attached value "devW" -- both still run the script that
        follows, confirmed by direct python3 execution."""
        self.assertEqual(python_args("python3 -uWX packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -uXdevW packages/x/test_real.py"),
                          ["packages/x/test_real.py"])


class PythonArgsOptionContract(unittest.TestCase):
    """keweichen's round-6 finding: the parser had no notion of a TERMINAL
    option (exits before any script runs) or of `--`/a separate-value long
    option (both still let a real script through). Every case here was
    confirmed by direct python3 execution before being pinned."""

    def test_dash_v_and_dash_h_are_terminal_anywhere_in_a_cluster(self):
        self.assertEqual(python_args("python3 -V packages/x/test_dead.py"), [])
        self.assertEqual(python_args("python3 -h packages/x/test_dead.py"), [])
        self.assertEqual(python_args("python3 -uV packages/x/test_dead.py"), [])
        self.assertEqual(python_args("python3 -Vu packages/x/test_dead.py"), [])

    def test_long_terminal_forms_are_terminal(self):
        self.assertEqual(python_args("python3 --version packages/x/test_dead.py"), [])
        self.assertEqual(python_args("python3 --help packages/x/test_dead.py"), [])

    def test_a_bare_double_dash_still_passes_the_real_script_through(self):
        """`python3 -- T.py` really does run T.py -- the option list ends,
        the script does not disappear with it."""
        self.assertEqual(python_args("python3 -- packages/x/test_real.py"),
                          ["packages/x/test_real.py"])

    def test_a_separate_value_long_option_still_reaches_the_script(self):
        self.assertEqual(
            python_args("python3 --check-hash-based-pycs always packages/x/test_real.py"),
            ["packages/x/test_real.py"])

    def test_the_equals_form_of_that_long_option_is_unknown_to_real_python(self):
        """`--check-hash-based-pycs=always` is NOT accepted (real python3
        exits with 'Unknown option'); failing closed here matches that."""
        self.assertEqual(
            python_args("python3 --check-hash-based-pycs=always packages/x/test_real.py"), [])

    def test_an_unrecognized_option_fails_closed_short_and_long(self):
        self.assertEqual(python_args("python3 -Z packages/x/test_real.py"), [])
        self.assertEqual(python_args("python3 --frobnicate packages/x/test_real.py"), [])

    def test_hidden_compat_flags_r_and_t_still_reach_the_script(self):
        """keweichen's round-8 finding: -R and -t are accepted (confirmed by
        an exhaustive a-z/A-Z execution sweep on both this host's python3
        and the repo's 3.9 floor) but absent from `python3 --help` -- the
        round-7 table, built from --help alone, silently dropped them."""
        self.assertEqual(python_args("python3 -R packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -t packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -uR packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -ut packages/x/test_real.py"),
                          ["packages/x/test_real.py"])

    def test_dash_p_fails_closed_because_the_repo_has_a_39_floor(self):
        """-P exists on 3.11+ but errors as unknown on 3.9 (confirmed by
        direct execution against /usr/bin/python3 3.9.6) -- one workflow in
        this repo pins exactly 3.9, so crediting -P universally risks a
        false-green under the interpreter that would actually refuse it."""
        self.assertEqual(python_args("python3 -P packages/x/test_real.py"), [])

    def test_check_hash_based_pycs_rejects_an_out_of_domain_value(self):
        """Real python3 accepts only always/default/never; anything else
        exits 2 before opening any script -- confirmed by direct execution."""
        self.assertEqual(
            python_args("python3 --check-hash-based-pycs sometimes packages/x/test_dead.py"), [])
        self.assertEqual(python_args("python3 --check-hash-based-pycs"), [])

    def test_check_hash_based_pycs_still_reaches_the_script_on_each_valid_value(self):
        for value in ("always", "default", "never"):
            self.assertEqual(
                python_args(f"python3 --check-hash-based-pycs {value} packages/x/test_real.py"),
                ["packages/x/test_real.py"])

    def test_q_and_x_are_unchanged_neighbors_of_the_round_8_edit_and_still_reach_the_script(self):
        """keweichen's round-9 finding: rewriting _VALUELESS_CHARS to add
        R/t and drop P also silently dropped q and x, which round 7 already
        had right. Confirmed by direct execution on both this host's python3
        and the 3.9 floor with a 2-line fixture (a 1-line one hides -x's
        effect: it skips line 1, so a 1-line script prints nothing either way)."""
        self.assertEqual(python_args("python3 -q packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -x packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -uq packages/x/test_real.py"),
                          ["packages/x/test_real.py"])
        self.assertEqual(python_args("python3 -ux packages/x/test_real.py"),
                          ["packages/x/test_real.py"])

    def test_dash_p_is_a_stated_conservative_policy_not_a_claim_of_exactness(self):
        """-P is valid on 3.11+ and this repo's ci.yml runs an unpinned,
        likely-newer stock interpreter -- but python39-compat.yml pins
        exactly 3.9, and one table covers every workflow file regardless of
        which interpreter a given job actually uses. Refusing -P is the
        deliberate, version-NEUTRAL choice (never claim a script ran when
        any interpreter this repo tests would refuse it), not a claim that
        the table models -P's real cross-version behavior exactly."""
        self.assertEqual(python_args("python3 -P packages/x/test_real.py"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
