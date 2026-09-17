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


class LiteralConstantAndOrChains(unittest.TestCase):
    """The `&&`/`||` under-credit named and deferred through every earlier
    round: `true && python3 x.py` and `false || python3 x.py` both really
    run x.py (confirmed by direct bash execution), and the old code never
    credited either. `true`/`false` are the only LHS whose exit status is
    known without a real shell, so only those two forms gain credit."""

    def test_true_and_and_credits_the_rhs(self):
        self.assertEqual(program_python_args("true && python3 packages/x/x.py"),
                          ["packages/x/x.py"])

    def test_false_or_or_credits_the_rhs(self):
        self.assertEqual(program_python_args("false || python3 packages/x/x.py"),
                          ["packages/x/x.py"])

    def test_true_or_or_does_not_credit_the_rhs(self):
        """true succeeds, so || never evaluates its right side."""
        self.assertEqual(program_python_args("true || python3 packages/x/x.py"), [])

    def test_false_and_and_still_does_not_credit_the_rhs(self):
        """The original false-positive this file exists to prevent -- must
        still refuse credit now that the true/false LHS case is handled."""
        self.assertEqual(program_python_args("false && python3 packages/x/x.py"), [])

    def test_an_undecidable_lhs_still_refuses_credit_for_the_rhs(self):
        """Only a literal true/false LHS is decidable; a real command's exit
        status is not known without running it, so the RHS stays uncredited."""
        self.assertEqual(
            program_python_args("python3 packages/x/real.py && python3 packages/x/x.py"),
            ["packages/x/real.py"])

    def test_undecidability_propagates_through_a_chain(self):
        """`true && false && python3 x.py`: true lets false run, but false
        breaks the chain, so x.py never runs -- confirmed by direct execution."""
        self.assertEqual(
            program_python_args("true && false && python3 packages/x/x.py"), [])
        self.assertEqual(
            program_python_args("false && true && python3 packages/x/x.py"), [])

    def test_a_short_circuited_segment_still_sets_the_compounds_status_for_or(self):
        """`false && python3 dead.py || python3 live.py`: dead.py never runs
        (already-documented behavior, preserved), but the compound's exit
        status is still `false`'s, so `|| live.py` DOES run -- confirmed by
        direct execution, which prints only "LIVE"."""
        self.assertEqual(
            program_python_args(
                "false && python3 packages/x/dead.py || python3 packages/x/live.py"),
            ["packages/x/live.py"])


class PipeIsNotAHardReset(unittest.TestCase):
    """keweichen round 11: a lone `|` is one syntactic unit with its guard,
    not an independently-reachable list -- all confirmed by direct bash
    execution (see the class-level cases in ci-covers-every-python-test for
    the exact commands and their real exit codes/output)."""

    def test_a_gated_pipe_credits_neither_stage(self):
        """`false && printf x | python3 dead.py`: Bash runs only `false`
        (exit 1); the old code treated the lone `|` as a hard reset and
        credited dead.py as freshly reachable."""
        self.assertEqual(
            program_python_args("false && printf x | python3 packages/x/dead.py"), [])

    def test_the_skipped_pipes_own_status_still_feeds_the_next_or(self):
        """Same LHS, now followed by `|| python3 live.py`: Bash prints only
        "LIVE" (the false&&(pipe) compound's exit is false's, so || runs) --
        dead.py must stay uncredited and live.py must gain it."""
        self.assertEqual(
            program_python_args(
                "false && printf x | python3 packages/x/dead.py "
                "|| python3 packages/x/live.py"),
            ["packages/x/live.py"])

    def test_an_ungated_pipe_still_credits_its_last_stage(self):
        """No guard at all: both real-world use (`echo x | python3 t.py`)
        and the round-10 controls must keep working."""
        self.assertEqual(
            program_python_args("echo x | python3 packages/x/live.py"),
            ["packages/x/live.py"])

    def test_pipefail_makes_a_later_and_and_see_the_pipes_real_failure(self):
        """Child regression, same round: with `set -o pipefail` active,
        `false | true && python3 dead.py` really exits 1 (pipefail reports
        the pipe's rightmost KNOWN failure, `false`'s, not `true`'s 0) and
        dead.py never runs -- confirmed by direct execution; the code had
        started crediting it."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\nfalse | true && python3 packages/x/dead.py"), [])

    def test_without_pipefail_the_same_pipe_credits_the_and_and(self):
        """Same pipe, no `set -o pipefail`: Bash's pipe exit is `true`'s (0),
        so `&&` DOES run dead.py -- confirmed by direct execution. Pins the
        control the pipefail case above is not a universal refusal."""
        self.assertEqual(
            program_python_args("false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_pipefail_survives_a_semicolon_and_a_combined_short_opt_form(self):
        """`set -eo pipefail` (combined short opts) turns it on just as
        `-o pipefail` does, and it stays on across a `;` -- real `set`
        semantics are script-scoped, not per-statement."""
        self.assertEqual(
            program_python_args(
                "set -eo pipefail; true\nfalse | true && python3 packages/x/dead.py"), [])


class PipefailIsNotABareRegex(unittest.TestCase):
    """keweichen round 12: the round-11 `_sets_pipefail` regex missed real
    Bash `set` semantics on four axes -- all confirmed by direct bash
    execution first, none of these guessed from documentation."""

    def test_o_anywhere_in_the_cluster_still_takes_the_next_word(self):
        """`-oe pipefail`: Bash's `-o` consumes the NEXT ARGV WORD as its
        value no matter where `o` sits in a combined short-opt cluster --
        round 11's regex required `o` to be the cluster's LAST letter."""
        self.assertEqual(
            program_python_args(
                "set -oe pipefail\nfalse | true && python3 packages/x/dead.py"), [])

    def test_multiple_toggles_apply_in_argv_order_last_one_wins(self):
        """`set +o pipefail -o pipefail`: Bash re-evaluates left to right, so
        this ENDS enabled -- checking `+o` unconditionally before `-o`
        (round 11) always returned disabled regardless of what followed."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail -o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_pipe_stage_set_runs_in_a_subshell_and_never_reaches_the_parent(self):
        """A `set` that is itself part of a multi-stage pipe (`set +o
        pipefail | cat`) executes in a subshell; Bash's OWN prior `set -o
        pipefail` in the parent shell is unaffected."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\nset +o pipefail | cat\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_an_undecidable_gate_makes_pipefail_state_unknown_not_silently_kept(self):
        """`python3 -c 'pass' && set -o pipefail`: whether the toggle runs is
        undecidable here (the LHS isn't a literal), so pipefail's state must
        become genuinely UNKNOWN going forward, not silently stay at its old
        value -- Bash really does reach the toggle (python3 -c 'pass' always
        succeeds), so the safe, honest answer here still ends up refusing
        credit rather than confidently crediting it."""
        self.assertEqual(
            program_python_args(
                "python3 -c 'pass' && set -o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_leading_bang_negates_the_whole_pipelines_result(self):
        """`! false | true`: Bash's pipefail-adjusted pipe exit is `false`'s
        (1), and a leading `!` negates the PIPELINE's result (0), so `&&`
        DOES run the RHS -- child false-orphan regression, same round: the
        parent (pre-round-11) correctly named it; round 11 dropped it."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n! false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_bang_without_pipefail_still_negates(self):
        """Same negation, no pipefail: pipe exit is `true`'s (0), `!` negates
        to failure (1), so `&&` must NOT run -- pins the control the
        pipefail case above is not a universal credit."""
        self.assertEqual(
            program_python_args("! false | true && python3 packages/x/dead.py"), [])


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
