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

    def test_a_compound_and_condition_is_evaluated_not_split(self):
        """qingyun-wu round 24: `if true && false` is FALSE overall (the
        `&&` chains inside the condition, not between top-level commands)
        -- confirmed by direct execution: the else runs. `_raw_segments`
        used to split at the `&&` before `_if_head` ever saw the whole
        condition, corrupting the one word it needed."""
        self.assertEqual(program_python_args(
            "if true && false; then\n  python3 packages/x/then.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/else.py"])

    def test_a_compound_or_condition_is_evaluated_not_split(self):
        """qingyun-wu round 24: `if false || true` is TRUE overall --
        confirmed by direct execution: the then runs."""
        self.assertEqual(program_python_args(
            "if false || true; then\n  python3 packages/x/then.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/then.py"])

    def test_a_three_term_compound_condition_evaluates_left_to_right(self):
        """`true || false && false` is FALSE overall -- confirmed by
        direct execution. `||` short-circuits the middle `false` (never
        runs), carrying `true`'s success into `&&`, which then runs and
        is decided by the trailing `false`."""
        self.assertEqual(program_python_args(
            "if true || false && false; then\n  python3 packages/x/then.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/else.py"])

    def test_unary_negation_of_a_literal_condition_picks_the_live_arm(self):
        """qingyun-wu round 28: `if ! true` used to be unrecognized ('other'),
        crediting both arms -- confirmed by direct execution that Bash only
        ever runs the else here."""
        self.assertEqual(program_python_args(
            "if ! true; then\n  python3 packages/x/test_dead.py\n"
            "else\n  python3 packages/x/test_live.py\nfi\n"),
            ["packages/x/test_live.py"])
        self.assertEqual(program_python_args(
            "if ! false; then\n  python3 packages/x/test_live.py\n"
            "else\n  python3 packages/x/test_dead.py\nfi\n"),
            ["packages/x/test_live.py"])

    def test_two_separately_negated_operands_each_get_their_own_negation(self):
        """`! true && ! false` is FALSE overall (false && true) -- confirmed
        by direct execution. Each `!` binds to the operand right after it,
        not to the whole chain."""
        self.assertEqual(program_python_args(
            "if ! true && ! false; then\n  python3 packages/x/then.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/else.py"])
        self.assertEqual(program_python_args(
            "if ! false && ! false; then\n  python3 packages/x/then.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/then.py"])

    def test_double_negation_on_one_operand_toggles_twice(self):
        """keweichen round 29: `! ! true` is valid on Bash 5.2/5.3 (confirmed
        live) and toggles twice back to true -- a round-28 fix consumed only
        ONE `!` per operand on the claim that a second is always a syntax
        error, which held on this host's Bash 3.2.57 but not on 5.3.20."""
        self.assertEqual(program_python_args(
            "if ! ! true; then\n  python3 packages/x/test_live.py\n"
            "else\n  python3 packages/x/test_dead.py\nfi\n"),
            ["packages/x/test_live.py"])
        self.assertEqual(program_python_args(
            "if ! ! false; then\n  python3 packages/x/test_dead.py\n"
            "else\n  python3 packages/x/test_live.py\nfi\n"),
            ["packages/x/test_live.py"])

    def test_triple_negation_toggles_an_odd_number_of_times(self):
        """Confirmed live on Bash 5.3.20: `! ! ! true` is false (odd count
        of `!` toggles an odd number of times, same as one `!`)."""
        self.assertEqual(program_python_args(
            "if ! ! ! true; then\n  python3 packages/x/test_dead.py\n"
            "else\n  python3 packages/x/test_live.py\nfi\n"),
            ["packages/x/test_live.py"])

    def test_a_taken_if_arm_drops_every_later_elif(self):
        """qingyun-wu round 24: once `if true` is taken, the following
        `elif true` never runs regardless of ITS OWN condition -- confirmed
        by direct execution: only the if-body prints."""
        self.assertEqual(program_python_args(
            "if true; then\n  python3 packages/x/if.py\n"
            "elif true; then\n  python3 packages/x/elif.py\nfi\n"),
            ["packages/x/if.py"])

    def test_a_taken_elif_arm_drops_the_following_else(self):
        """qingyun-wu round 24: once an `elif` is taken, the trailing
        `else` never runs -- confirmed by direct execution."""
        self.assertEqual(program_python_args(
            "if false; then\n  python3 packages/x/if.py\n"
            "elif true; then\n  python3 packages/x/elif.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/elif.py"])

    def test_a_nonliteral_arm_poisons_chain_exclusivity_to_unknown(self):
        """An undecidable earlier arm means a later arm's own reachability
        can't be proven dead either -- both must stay credited, matching
        the file's own 'never wrongly drop' rule for a single undecidable
        condition."""
        self.assertEqual(program_python_args(
            'if [ "$X" = y ]; then\n  python3 packages/x/if.py\n'
            "elif true; then\n  python3 packages/x/elif.py\nfi\n"),
            ["packages/x/if.py", "packages/x/elif.py"])

    def test_an_unknown_arm_followed_by_a_guaranteed_true_one_proves_else_dead(self):
        """keweichen round 25: unlike the case above (no `else`), a
        trailing `else` here is provably dead either way -- either the
        undecidable `if` already ran, or it didn't and the guaranteed-true
        `elif` surely did. Confirmed by direct execution: only elif_ran
        prints. 'unknown' must resolve to True once a later arm is
        certain, not propagate forever."""
        self.assertEqual(program_python_args(
            'if [ "$X" = y ]; then\n  python3 packages/x/if.py\n'
            "elif true; then\n  python3 packages/x/elif.py\n"
            "else\n  python3 packages/x/else.py\nfi\n"),
            ["packages/x/if.py", "packages/x/elif.py"])


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


class FunctionScopedInvocations(unittest.TestCase):
    """keweichen's [P2 blocker] on #4202 round 30: `program_invokes()` credited
    a helper call sitting inside a function DEFINITION that nothing ever
    calls -- neither runs unless something invokes the function."""

    def test_a_call_inside_an_uncalled_function_is_not_credited(self):
        self.assertFalse(program_invokes(
            f"discover() {{\n  bash scripts/{NAME} > files\n}}\n"
            f"printf '%s\\n' tests/only.test.py > files\n", NAME))

    def test_the_same_call_is_credited_once_the_function_is_invoked(self):
        self.assertTrue(program_invokes(
            f"discover() {{\n  bash scripts/{NAME} > files\n}}\ndiscover\n", NAME))

    def test_a_plain_top_level_call_is_unaffected(self):
        self.assertTrue(program_invokes(f"bash scripts/{NAME} > files\n", NAME))

    def test_reachability_is_transitive_through_a_called_function(self):
        self.assertTrue(program_invokes(
            f"inner() {{\n  bash scripts/{NAME}\n}}\nouter() {{\n  inner\n}}\nouter\n", NAME))

    def test_a_dead_transitive_chain_stays_uncredited(self):
        self.assertFalse(program_invokes(
            f"inner() {{\n  bash scripts/{NAME}\n}}\nouter() {{\n  inner\n}}\n", NAME))

    def test_a_one_liner_function_is_tracked_as_its_own_single_line_body(self):
        """round 33, keweichen: `name() { cmd; }` closes on its own line, so
        the earlier stripper found no separate body line and left this
        line out of every function's in_body set -- an UNCALLED one-liner's
        command then read as unconditional top-level code and got credited."""
        from active_code import _function_bodies
        self.assertEqual(_function_bodies(f"discover() {{ bash scripts/{NAME}; }}\n"),
                          [("discover", 0, (), 0, 0)])
        self.assertFalse(program_invokes(
            f"discover() {{ bash scripts/{NAME}; echo done; }}\nprintf ok\n", NAME))

    def test_an_uncalled_function_does_not_hide_an_unrelated_top_level_call(self):
        self.assertTrue(program_invokes(
            f"discover() {{\n  echo dead\n}}\nbash scripts/{NAME}\n", NAME))

    def test_a_parameter_expansion_inside_an_uncalled_body_does_not_close_it_early(self):
        """keweichen's [P2 blocker] round 30b: `${x}`'s closing brace alone
        was counted as ending the function block, so the real call after it
        read as top-level and got credited though the function is uncalled."""
        text = f"dead() {{\n  echo ${{x}}\n  bash scripts/{NAME} > files\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "dead\n", NAME))

    def test_a_nested_parameter_expansion_is_skipped_as_one_unit(self):
        text = f"dead() {{\n  echo ${{x:-${{y}}}}\n  bash scripts/{NAME}\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))

    def test_split_brace_function_is_recognized_uncalled(self):
        """kewei-red-ag2space round 32: `name()` with `{` on its OWN line is
        a valid, common spelling `_FUNC_START_RE` alone cannot see -- the
        body then reads as unconditional top-level code."""
        text = f"discover()\n{{\n  bash scripts/{NAME} > files\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))

    def test_split_brace_function_called_is_credited(self):
        text = f"discover()\n{{\n  bash scripts/{NAME} > files\n}}\ndiscover\n"
        self.assertTrue(program_invokes(text, NAME))

    def test_split_brace_function_keyword_form(self):
        text = f"function discover()\n{{\n  bash scripts/{NAME}\n}}\ndiscover\n"
        self.assertTrue(program_invokes(text, NAME))

    def test_bare_function_keyword_with_no_parens_is_recognized(self):
        """kewei-red-ag2space round 33: `function name { ... }` (no `()` at
        all) is valid Bash that neither existing FUNC_START regex sees."""
        text = f"function discover {{\n  bash scripts/{NAME} > files\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "discover\n", NAME))

    def test_bare_function_keyword_split_brace_is_recognized(self):
        text = f"function discover\n{{\n  bash scripts/{NAME}\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))

    def test_escaped_closing_brace_in_a_default_value_does_not_close_early(self):
        """kewei-red-ag2space round 33: `${x:-\\}}`'s escaped `}` is a
        LITERAL default-value character, not the expansion's own closer --
        misreading it let the real closer fall through to the outer counter
        and close the function block one line early."""
        text = "discover() {\n  echo ${x:-\\}}\n  bash scripts/" + NAME + "\n}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "discover\n", NAME))

    def test_shadowed_definition_keeps_only_the_last_ones_reachability(self):
        """kewei-red-ag2space round 33: Bash redefinition is last-wins -- an
        earlier same-named definition never executes, whatever calls the
        name. Tracking reachability by name alone kept a dead first
        definition's helper call "reachable" once the decoy shadow ran."""
        text = (f"discover() {{\n  bash scripts/{NAME}\n}}\n"
                f"discover() {{\n  printf '%s\\n' tests/only.test.py\n}}\ndiscover\n")
        self.assertFalse(program_invokes(text, NAME))

    def test_shadowed_definition_the_last_ones_own_call_still_credits(self):
        """Control for the above: when the LAST definition is the one that
        calls the helper, it must still be credited once invoked."""
        text = (f"discover() {{\n  printf '%s\\n' tests/only.test.py\n}}\n"
                f"discover() {{\n  bash scripts/{NAME}\n}}\ndiscover\n")
        self.assertTrue(program_invokes(text, NAME))

    def test_space_before_parens_is_still_a_definition_not_a_call(self):
        """kewei-red-ag2space round 33 (#4391 follow-up): `discover ()` with
        a space tokenizes its OWN declaration line as a bare call to
        "discover" -- `discover()` (no space) only avoided this by luck,
        since the glued token can't equal the bare name."""
        text = f"discover () {{\n  bash scripts/{NAME}\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "discover\n", NAME))

    def test_redefinition_is_last_wins_only_as_of_the_call_not_globally(self):
        """A decoy defined BEFORE the call and a real helper defined AFTER
        it: at runtime the call reaches the decoy, since the helper doesn't
        exist yet. A single global "last definition" wrongly credited the
        helper regardless of where the call sits relative to it."""
        text = (f"discover() {{ printf decoy; }}\n"
                f"discover\n"
                f"discover() {{\n  bash scripts/{NAME}\n}}\n")
        self.assertFalse(program_invokes(text, NAME))

    def test_a_call_between_two_definitions_reaches_the_earlier_one(self):
        """Mirror of the above: the helper is defined and called BEFORE a
        later decoy redefinition. The call already reached the helper --
        a later shadow can't retroactively un-run it."""
        text = (f"discover() {{\n  bash scripts/{NAME}\n}}\n"
                f"discover\n"
                f"discover() {{ printf decoy; }}\n")
        self.assertTrue(program_invokes(text, NAME))

    def test_a_call_before_any_definition_invokes_nothing(self):
        """Calling a name before it has been defined at all is a real Bash
        `command not found` -- it must never resolve to a LATER definition
        of the same name."""
        text = f"discover\ndiscover() {{\n  bash scripts/{NAME}\n}}\n"
        self.assertFalse(program_invokes(text, NAME))

    def test_a_command_sharing_the_closing_brace_line_is_still_tracked(self):
        """`cmd; }` puts the last real command on the SAME line as the
        closer -- that line sat outside every recorded span, so an
        uncalled function's last command read as unconditional top-level."""
        text = f"dead() {{\n  echo hi\n  bash scripts/{NAME}; }}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "dead\n", NAME))

    def test_a_brace_word_outside_command_position_is_not_structural(self):
        """`echo hi } more` is `}` as a plain ARGUMENT to echo -- Bash never
        reaches it as the reserved word, but counting every brace character
        regardless of position closed the function one line early and let
        the real helper call after it read as top-level."""
        text = f"dead() {{\n  echo hi }} more\n  bash scripts/{NAME}\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "dead\n", NAME))

    def test_a_called_one_liner_still_credits_its_own_body(self):
        """kewei-red-ag2space round 33 follow-up: `discover() { helper; }`
        then `discover` still read as calling nothing -- the one-liner's own
        declaration text ("discover() {") glued to its body made the whole
        line tokenize as a call to "discover()", never to the real command."""
        text = f"discover() {{ bash scripts/{NAME}; }}\ndiscover\n"
        self.assertTrue(program_invokes(text, NAME))

    def test_an_open_line_with_no_further_body_line_before_a_bare_close_is_tracked(self):
        """kewei-red-ag2space round 33 follow-up: `discover() { :; helper`
        closed by a BARE `}` on the very next line (no body line between
        them) vanished from `_function_bodies` entirely -- its content then
        read as unconditional top-level code, though discover is never called."""
        text = f"discover() {{ :; bash scripts/{NAME}\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "discover\n", NAME))

    def test_an_and_guard_on_the_prior_line_still_gates_the_call(self):
        """kewei-red-ag2space round 33 follow-up: `called_on()` scanned each
        physical line alone, so a guard opened on the PRIOR line was invisible
        -- `false &&\\n  discover` read as an unconditional call to discover."""
        text = f"discover() {{\n  bash scripts/{NAME}\n}}\nfalse &&\n  discover\n"
        self.assertFalse(program_invokes(text, NAME))

    def test_an_and_guard_true_on_the_prior_line_still_credits_the_call(self):
        """Control for the above: a TRUE guard on the prior line lets the
        chain proceed, so the call after it must still be credited."""
        text = f"discover() {{\n  bash scripts/{NAME}\n}}\ntrue &&\n  discover\n"
        self.assertTrue(program_invokes(text, NAME))

    def test_a_multiline_if_false_block_still_gates_the_call_inside_it(self):
        """kewei-red-ag2space round 33 follow-up: the same per-line blindness
        for `if false; then\\n  discover\\nfi` -- Bash never runs discover."""
        text = f"discover() {{\n  bash scripts/{NAME}\n}}\nif false; then\n  discover\nfi\n"
        self.assertFalse(program_invokes(text, NAME))

    def test_a_multiline_if_true_block_still_credits_the_call_inside_it(self):
        """Control for the above: a TRUE literal condition really does run
        the branch, so the call inside it must still be credited."""
        text = f"discover() {{\n  bash scripts/{NAME}\n}}\nif true; then\n  discover\nfi\n"
        self.assertTrue(program_invokes(text, NAME))

    def test_a_nested_call_resolves_against_the_outer_calls_own_line_not_its_body_line(self):
        """kewei-red-ag2space round 33 follow-up: `outer` calls `inner` from
        a fixed line inside outer's own body; a LATER redefinition of inner,
        reached before outer is ever invoked, is the one that actually runs
        -- resolving against the inner call's own (always-earlier) textual
        position instead credited the shadowed, never-executed definition."""
        text = (f"inner() {{\n  bash scripts/{NAME}\n}}\n"
                f"outer() {{\n  inner\n}}\n"
                f"inner() {{\n  printf DECOY\n}}\n"
                f"outer\n")
        self.assertFalse(program_invokes(text, NAME))

    def test_a_nested_call_reaches_a_redefinition_that_lands_before_the_outer_call(self):
        """Mirror of the above: the helper-carrying redefinition of inner
        lands BEFORE outer is invoked, so outer's own call now reaches it."""
        text = (f"inner() {{\n  printf DECOY\n}}\n"
                f"outer() {{\n  inner\n}}\n"
                f"inner() {{\n  bash scripts/{NAME}\n}}\n"
                f"outer\n")
        self.assertTrue(program_invokes(text, NAME))

    def test_a_dead_and_a_live_identically_worded_call_are_not_conflated(self):
        """kewei-red-ag2space round 34 follow-up: a dead `discover` call
        (inside `if false`) and a later live one, worded identically, must
        not have the live segment's credit misattributed to the dead line
        -- that would resolve against the WRONG point in the program and
        credit whichever definition preceded the dead line instead of the
        one active when the real call actually runs. One-liner definitions
        on purpose: a multi-line definition's own bare closing `}` becomes
        an extra top-level segment that happens to reabsorb the
        misalignment, masking exactly this defect."""
        text = (f"discover() {{ bash scripts/{NAME}; }}\n"
                f"if false; then\n  discover\nfi\n"
                f"discover() {{ printf DECOY; }}\n  discover\n")
        self.assertFalse(program_invokes(text, NAME))

    def test_a_second_call_to_the_same_function_re_resolves_under_a_later_redefinition(self):
        """kewei-red-ag2space round 34 follow-up: deduping reachable spans
        by (start, end) alone skipped re-processing a function's body on a
        SECOND call, so a redefinition landing between the two calls never
        got explored -- the second call must still resolve independently."""
        text = ("inner() { printf DECOY; }\n"
                "outer() { inner; }\n"
                "outer\n"
                f"inner() {{ bash scripts/{NAME}; }}\n"
                "outer\n")
        self.assertTrue(program_invokes(text, NAME))

    def test_a_split_opener_brace_line_can_carry_body_content_too(self):
        """kewei-red-ag2space round 34 follow-up: `name()\\n{ :; helper`
        (content sharing the split opener's OWN brace line, closed by a
        bare `}` with no body line between) vanished from
        `_function_bodies` entirely -- only a BARE `{`-only line was
        recognized as the split form's brace line."""
        text = f"discover()\n{{ :; bash scripts/{NAME}\n}}\nprintf ok\n"
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text + "discover\n", NAME))

    def test_an_escaped_semicolon_does_not_open_a_manufactured_command_start(self):
        """kewei-red-ag2space round 34: exact fixture. `\\;` is a LITERAL
        semicolon (an argument), never a separator -- treating it as one
        put the reserved-word check at a manufactured command-start right
        before the literal `}` argument that followed, closing the
        function one line early. Direct Bash: exits 0, prints only TOP,
        never runs the helper."""
        text = ("discover() {\n"
                "  printf x \\; } more\n"
                f"  bash scripts/{NAME}\n"
                "}\nprintf TOP\n")
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text.replace(
            "}\nprintf TOP\n", "}\ndiscover\nprintf TOP\n"), NAME))

    def test_a_compact_close_semicolon_still_decrements_depth(self):
        """kewei-red-ag2space round 34: `}` immediately followed by `;`
        (no space, `};`) is still a valid, structural close -- Bash really
        runs the command after it. The token-boundary check required
        whitespace (or string-edge) on both sides, missing an adjacent
        operator character as an equally valid boundary."""
        text = ("outer() {\n"
                f"  {{ printf nested; }}; bash scripts/{NAME}\n"
                "}\nouter\n")
        self.assertTrue(program_invokes(text, NAME))
        self.assertFalse(program_invokes(text.replace("}\nouter\n", "}\nprintf ok\n"), NAME))

    def test_a_compact_open_semicolon_still_increments_depth(self):
        """Mirror of the above: `{` immediately preceded by `;` (no space,
        `;{`) is an equally valid open -- confirmed by direct execution."""
        text = ("outer() {\n"
                f"  printf x;{{ bash scripts/{NAME}; }}\n"
                "}\nouter\n")
        self.assertTrue(program_invokes(text, NAME))
        self.assertFalse(program_invokes(text.replace("}\nouter\n", "}\nprintf ok\n"), NAME))

    def test_a_leading_bare_brace_token_is_a_group_opener_not_the_command(self):
        """kewei-red-ag2space round 34 follow-up: a command-group's own
        `{ helper` segment tokenized "{" as the command, never "helper" --
        the group opener must be peeled like `env`/`bash` are. Spaced on
        both sides so brace-depth counting alone (already correct here)
        isn't what's under test -- only the token-peel is."""
        text = f"outer() {{\n  {{ bash scripts/{NAME}; }}\n}}\nouter\n"
        self.assertTrue(program_invokes(text, NAME))
        self.assertFalse(program_invokes(text.replace("outer\n", "printf ok\n"), NAME))

    def test_a_quoted_or_escaped_brace_is_a_real_command_name_not_a_group_opener(self):
        """kewei-red-ag2space round 35: `'{'`/`\\{` are Bash attempts to run
        a program literally named `{` (rc 127) -- shlex resolves both to
        the identical bare token `{` the group-opener peel could not tell
        apart from the real reserved word."""
        self.assertFalse(program_invokes(f"'{{' bash scripts/{NAME}\n", NAME))
        self.assertFalse(program_invokes(f"\\{{ bash scripts/{NAME}\n", NAME))
        self.assertTrue(program_invokes(f"{{ bash scripts/{NAME}; }}\n", NAME))

    def test_a_nested_multiline_function_is_gated_by_its_own_reachability(self):
        """kewei-red-ag2space round 35: `outer` merely DEFINING a nested
        `inner` (never calling it) must not credit inner's body -- outer
        being reachable is not the same as inner being called."""
        text = (f"outer() {{\n  inner() {{\n    bash scripts/{NAME}\n  }}\n}}\nouter\n")
        self.assertFalse(program_invokes(text, NAME))
        called = text.replace("  }\n}\nouter\n", "  }\n  inner\n}\nouter\n")
        self.assertTrue(program_invokes(called, NAME))

    def test_a_real_call_after_a_nested_definition_is_still_in_the_outer_span(self):
        """kewei-red-ag2space round 35: a nested definition's own close
        must not be mistaken for the ENCLOSING function's close -- content
        AFTER the nested def (here, outer's real helper call) has to stay
        inside outer's tracked span, or an uncalled outer still credits it
        as unconditional top-level text. Direct Bash: prints only TOP."""
        text = ("outer() {\n"
                "  inner() {\n"
                "    printf never\n"
                "  }\n"
                f"  bash scripts/{NAME}\n"
                "}\nprintf TOP\n")
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text.replace("}\nprintf TOP\n", "}\nouter\nprintf TOP\n"), NAME))

    def test_a_backslash_continued_line_is_not_a_fresh_command_start(self):
        """kewei-red-ag2space round 35: the continued half of a
        backslash-newline command is still mid-command, so its `}` is a
        plain argument, never a line-start reserved word -- confirmed by
        direct execution (prints only TOP, the helper never runs)."""
        text = ("discover() {\n"
                "  printf x \\\n"
                "    } more\n"
                f"  bash scripts/{NAME}\n"
                "}\nprintf TOP\n")
        self.assertFalse(program_invokes(text, NAME))
        self.assertTrue(program_invokes(text.replace("}\nprintf TOP\n", "}\ndiscover\nprintf TOP\n"), NAME))

    def test_negation_before_a_group_still_sees_the_group_opener(self):
        """kewei-red-ag2space round 36 follow-up: `! { helper; }` really
        runs helper (direct execution) -- computing the group-opener check
        against the pre-`!`-peel text left the now-leading `{` unrecognized."""
        self.assertTrue(program_invokes(f"! {{ bash scripts/{NAME}; }}\n", NAME))

    def test_a_semicolon_before_a_continuation_still_reopens_command_start(self):
        """kewei-red-ag2space round 36 follow-up: `:;` before a
        backslash-newline already re-arms command position, so the `}` on
        the continued line closes the function for real -- confirmed by
        direct execution (the helper runs unconditionally, outside it)."""
        text = "outer() {\n  :; \\\n}\n" + f"bash scripts/{NAME}\n"
        self.assertTrue(program_invokes(text, NAME))

    def test_an_outer_and_a_nested_one_liner_do_not_alias_by_span(self):
        """kewei-red-ag2space round 36 follow-up: an outer one-liner and a
        nested one-liner inside it can share an identical (start, end)
        span; reachability keyed on span alone credited the never-called
        nested one just because the span happened to match the outer's."""
        text = f"outer() {{\n  inner() {{ bash scripts/{NAME}; }}\n}}\nouter\n"
        self.assertFalse(program_invokes(text, NAME))
        called = text.replace("}\nouter\n", "}\n  inner\n}\nouter\n")
        self.assertTrue(program_invokes(called, NAME))

    def test_content_after_a_nested_close_belongs_to_the_enclosing_body(self):
        """kewei-red-ag2space round 36 follow-up: `}; helper` puts the
        ENCLOSING function's own command on the same line as a NESTED
        function's close -- that command must survive even when the
        nested function is never called and its body gets blanked."""
        text = ("outer() {\n  inner() {\n    :\n"
                f"  }}; bash scripts/{NAME}\n}}\nouter\n")
        self.assertTrue(program_invokes(text, NAME))
        self.assertFalse(program_invokes(text.replace("}\nouter\n", "}\nprintf ok\n"), NAME))

    def test_a_call_before_its_own_nested_definition_invokes_nothing(self):
        """qingyun-sutando / kewei-red-ag2space round 36 follow-up: a name
        called then defined, both inside the SAME body, must resolve to
        nothing -- real Bash reports command-not-found, since the later
        definition has not executed yet at the point of the call."""
        text = f"outer() {{\n  inner\n  inner() {{ bash scripts/{NAME}; }}\n}}\nouter\n"
        self.assertFalse(program_invokes(text, NAME))
        reordered = f"outer() {{\n  inner() {{ bash scripts/{NAME}; }}\n  inner\n}}\nouter\n"
        self.assertTrue(program_invokes(reordered, NAME))


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

    def test_a_literal_false_decides_the_compound_even_behind_an_undecidable_lhs(self):
        """`printf if && false || python3 live.py`: `printf if`'s own exit
        status is undecidable, but `X && false` is false EITHER WAY -- if
        printf fails the chain is already false, if it succeeds `false`
        runs and is false -- so `|| live.py` runs regardless (round 27,
        keweichen: confirmed on real Bash 3.2.57 and 5.2.32)."""
        self.assertEqual(
            program_python_args(
                "printf if && false || python3 packages/x/live.py"),
            ["packages/x/live.py"])

    def test_an_undecidable_lhs_then_a_literal_true_stays_undecidable(self):
        """`printf if && true && python3 x.py`: unlike the `false` case
        above, `X && true` equals X -- if printf fails the chain never
        reaches x.py, so this genuinely depends on printf's own status and
        stays uncredited, same as any other undecidable LHS (round 27,
        keweichen asked for this case too; declined -- it would regress
        test_an_undecidable_lhs_still_refuses_credit_for_the_rhs above)."""
        self.assertEqual(
            program_python_args(
                "printf if && true && python3 packages/x/x.py"), [])


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

    def test_amp_pipe_is_reachability_equivalent_to_a_lone_pipe(self):
        """`|&` (`2>&1 |`) was parsed as `|` then a hard-reset lone `&`, so a
        guard's dead stage became freshly reachable -- confirmed on Bash
        5.2.32 by keweichen (this repo's workflows target ubuntu-latest,
        Bash 4+): `false && printf x |& python3 dead.py` never launches
        Python. This host's own bash (3.2.57) can't run `|&` at all, so this
        pins the reported transcript rather than a local repro."""
        self.assertEqual(
            program_python_args("false && printf x |& python3 packages/x/dead.py"), [])

    def test_amp_pipe_still_credits_when_the_guard_is_true(self):
        """Control for the same construct: an unguarded/true-gated `|&`
        still launches both stages."""
        self.assertEqual(
            program_python_args("true && printf x |& python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_set_dash_dash_is_positional_params_not_a_pipefail_toggle(self):
        """`set -- +o pipefail`: `--` ends option scanning, so `+o pipefail`
        becomes $1/$2, not a toggle -- keweichen round 13, confirmed by
        direct execution: pipefail stays ON from the earlier `set -o
        pipefail` and dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\nset -- +o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_shell_quoted_pipefail_value_still_toggles_it(self):
        """`set -o 'pipefail'`: the quotes are shell quoting, not part of the
        value -- confirmed by direct execution."""
        self.assertEqual(
            program_python_args(
                "set -o 'pipefail'\nfalse | true && python3 packages/x/dead.py"), [])

    def test_a_negated_direct_invocation_still_credits_the_script(self):
        """`! python3 x.py` really executes python3 (on both Bash 3.2 and
        5.2, confirmed by direct execution) -- `!` inverts the reported
        status only, never the fact that the command ran."""
        self.assertEqual(
            program_python_args("! python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_a_quoted_bang_is_a_literal_command_name_not_negation(self):
        """`'!' python3 x.py`: Bash 3.2 and 5.2 both try to run a program
        literally named `!` and return 127 -- python3 never launches.
        Round-14's peel matched on the shlex-tokenized `!` regardless of
        quoting, since shlex already erases it -- keweichen round 15."""
        self.assertEqual(program_python_args("'!' python3 packages/x/dead.py"), [])

    def test_an_escaped_bang_is_also_a_literal_command_name(self):
        self.assertEqual(program_python_args(r"\! python3 packages/x/dead.py"), [])

    def test_bang_after_env_is_envs_argument_not_negation(self):
        """`env ! python3 x.py`: `!` is not the pipeline's own leading
        token here -- `env` tries to run a program named `!` and fails."""
        self.assertEqual(program_python_args("env ! python3 packages/x/dead.py"), [])

    def test_bang_after_an_assignment_prefix_is_also_not_negation(self):
        self.assertEqual(program_python_args("X=1 ! python3 packages/x/dead.py"), [])

    def test_set_option_scanning_stops_at_the_first_non_option_word(self):
        """`set -o pipefail positional +o pipefail`: Bash's own `set` ends
        option processing at the first non-`-`/`+` word, so the trailing
        `+o pipefail` is just $2/$3, not a second toggle -- keweichen
        round 15, confirmed by direct execution: pipefail stays ON."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set -o pipefail positional +o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_an_unresolved_set_o_value_makes_pipefail_state_unknown(self):
        """`OPT=pipefail; set -o "$OPT"`: Bash really resolves $OPT and
        enables pipefail -- confirmed by direct execution -- but a static
        read cannot know that, so this must land on the SAFE side (refuse
        credit) via genuine uncertainty, not by silently asserting the
        toggle did nothing."""
        self.assertEqual(
            program_python_args(
                'OPT=pipefail\nset -o "$OPT"\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_single_quoted_dollar_value_is_a_literal_bash_rejects(self):
        """`set -o '$OPT'`: shlex already erased the single-quoting by the
        time a token exists, giving the identical "$OPT" text as the
        expandable bare/double-quoted forms -- but Bash rejects it as an
        invalid literal option name and leaves pipefail untouched, so the
        later pipe's real exit is `true`'s and dead.py DOES run -- keweichen
        round 16, confirmed by direct execution on Bash 3.2 and 5.2."""
        self.assertEqual(
            program_python_args(
                "set -o '$OPT'\nfalse | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_an_escaped_dollar_value_is_also_a_rejected_literal(self):
        self.assertEqual(
            program_python_args(
                r"set -o \$OPT" "\n"
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_an_expansion_in_the_option_slot_itself_is_also_unknown(self):
        """`FLAG=-o; set "$FLAG" pipefail`: Bash resolves $FLAG to `-o` and
        really enables pipefail -- confirmed by direct execution -- but a
        static read cannot tell an expansion in OPTION position from the
        end of options, so nothing after it is safe to interpret either."""
        self.assertEqual(
            program_python_args(
                'FLAG=-o\nset "$FLAG" pipefail\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_lone_dash_also_ends_sets_own_option_scanning(self):
        """`set - +o pipefail`: a lone `-` is Bash's OWN end-of-options
        marker for `set`, same as `--` -- confirmed by direct execution:
        pipefail (enabled earlier) stays on and dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\nset - +o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_quote_provenance_is_per_word_not_a_whole_command_search(self):
        """keweichen round 17: `set -o "$OPT" '$OPT'` -- the FIRST occurrence
        (double-quoted, expandable) is the one `-o` actually consumes and
        really enables pipefail; the second (single-quoted) is just a
        positional leftover. A whole-command substring search for a
        literal copy of "$OPT" finds the SECOND one and wrongly calls the
        FIRST occurrence literal too -- confirmed by direct execution:
        pipefail goes on, dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "OPT=pipefail\n"
                "set -o \"$OPT\" '$OPT'\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_dollar_escaped_inside_double_quotes_is_still_a_literal(self):
        """`set -o "\\$OPT"`: the backslash escapes `$` INSIDE double
        quotes too, so this never expands and Bash rejects it as a literal
        invalid option name -- confirmed by direct execution: dead.py runs
        (pipefail never gets enabled)."""
        self.assertEqual(
            program_python_args(
                'set -o "\\$OPT"\n'
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_split_quoting_of_the_dollar_sign_is_also_a_literal(self):
        """`set -o '$'OPT`: adjacent quoted/unquoted spans concatenate into
        ONE word (`'$'` + `OPT` = the literal string `$OPT`, no space
        between them) -- confirmed by direct execution: Bash rejects it,
        dead.py runs."""
        self.assertEqual(
            program_python_args(
                "set -o '$'OPT\n"
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_an_unrecognized_option_name_aborts_the_whole_set_invocation(self):
        """`set -o pipefail; set -o invalid +o pipefail`: Bash's `set`
        validates each `-o` NAME and aborts the instant it sees one it
        doesn't recognize -- the trailing `+o pipefail` in the SAME
        invocation is never reached, so pipefail (enabled earlier) stays
        on -- confirmed by direct execution: dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set -o invalid +o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_tab_after_bang_still_negates_the_pipeline(self):
        """`!<TAB>false | true`: `_command_tokens` already peeled a
        tab-separated `!`; `_raw_segments`'s own negation check required a
        literal space and missed the tab form -- confirmed by direct
        execution: dead.py runs (pipefail's real exit gets negated)."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "!\tfalse | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_bare_brace_expansion_of_the_o_value_is_resolved(self):
        """`set -o pipe{fail,foo}`: Bash brace-expands this to two words,
        `pipefail` and `pipefoo`, and `-o` consumes the first -- confirmed
        by direct execution: pipefail goes on, dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipe{fail,foo}\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_quoted_brace_expansion_is_suppressed_and_stays_literal(self):
        """Control for the case above: quoting suppresses brace expansion
        entirely -- confirmed by direct execution: Bash rejects the
        literal string and dead.py runs."""
        self.assertEqual(
            program_python_args(
                "set -o 'pipe{fail,foo}'\n"
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_an_empty_quoted_word_ends_sets_own_option_scanning(self):
        """`set "" +o pipefail`: Bash preserves `""` as the first
        positional argument, which ends `set`'s option scanning before
        `+o pipefail` is ever reached -- confirmed by direct execution:
        pipefail (already on) stays on, dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                'set "" +o pipefail\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_the_inverse_empty_quoted_word_also_ends_scanning(self):
        """Control for the case above, the opposite direction: pipefail
        starts off, `set "" -o pipefail` never reaches `-o`, so it stays
        off -- confirmed by direct execution: dead.py runs."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                'set "" -o pipefail\n'
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_interactive_comments_is_a_recognized_set_o_name(self):
        """`set +o interactive-comments -o pipefail`: interactive-comments
        is a real, valid `set -o` name on both installed shells --
        confirmed by direct execution: it does not abort the invocation,
        so the later `-o pipefail` is reached and enables it."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                "set +o interactive-comments -o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_brace_expansion_is_one_argv_word_per_candidate_not_one_word_with_many_readings(self):
        """`set +o {errexit,pipefail}`: Bash brace-expands this into TWO
        argv words, `errexit` and `pipefail` -- `+o` consumes only the
        first (`errexit`); the bare `pipefail` next to it, with no
        leading `-`/`+`, ends `set`'s own option scanning like any other
        positional word -- confirmed by direct execution: pipefail
        (already on) stays on, dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set +o {errexit,pipefail}\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_brace_expansion_is_quote_scoped_per_delimiter_not_per_whole_word(self):
        """`set -o "p"ipe{fail,foo}`: only the leading `p` is quoted, the
        `{fail,foo}` that follows is entirely unquoted, so Bash still
        brace-expands it -- confirmed by direct execution: pipefail goes
        on, dead.py never runs. A whole-word `any_quoted` flag would
        wrongly suppress this because SOME part of the word was quoted."""
        self.assertEqual(
            program_python_args(
                'set -o "p"ipe{fail,foo}\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_redirection_is_consumed_by_the_shell_and_never_reaches_argv(self):
        """`set -o >/dev/null pipefail`: Bash strips the redirection
        before invoking `set`, which then sees only `-o pipefail` --
        confirmed by direct execution: pipefail goes on, dead.py never
        runs. Treating `>/dev/null` as `-o`'s literal value would misread
        it as an unrecognized option name and abort instead."""
        self.assertEqual(
            program_python_args(
                "set -o >/dev/null pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_redirect_to_an_empty_target_aborts_the_whole_command(self):
        """`set +o >"" pipefail`: Bash rejects an empty redirect target
        before the command ever runs -- confirmed by direct execution:
        `set` never executes, pipefail (already on) stays on, dead.py
        never runs. Modeling every redirect as a successfully-stripped
        no-op would wrongly apply `+o pipefail` and credit dead.py."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                'set +o >"" pipefail\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_an_escaped_digit_before_a_redirect_is_a_real_word_not_an_fd_prefix(self):
        r"""`set \2>/dev/null +o pipefail`: the escaped `2` is a real argv
        word (Bash: `set 2 +o pipefail`), which ends `set`'s own option
        scanning before `+o pipefail` is ever reached -- confirmed by
        direct execution: pipefail (already on) stays on, dead.py never
        runs. A value-only digit check can't tell a bare fd number from an
        escaped one."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set \\2>/dev/null +o pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_process_substitution_is_not_a_redirect(self):
        """`set -o <(true) pipefail`: Bash substitutes a literal
        `/dev/fd/N` argv word, which is an unrecognized `-o` name and
        aborts the whole invocation -- confirmed by direct execution:
        pipefail (already off) stays off, dead.py runs. Mistaking this
        for a real redirect would discard it and wrongly enable pipefail
        from the `pipefail` word next to it."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                "set -o <(true) pipefail\n"
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_an_empty_heredoc_delimiter_is_valid_not_a_failed_redirect(self):
        """`set -o <<"" pipefail`: an empty here-doc delimiter is ordinary,
        valid Bash syntax (it just matches the very next blank line) --
        NOT a failed empty-filename redirect, so the `-o pipefail` on the
        SAME line still takes effect. Confirmed by direct execution:
        pipefail (starting off) turns on, dead.py never runs. Starting
        from off (not on) makes this discriminating -- treating `<<` like
        a failed `<`/`>` returns "unchanged" (None), which off-by-luck
        matches "on" only when the prior state already happened to be on."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                'set -o <<"" pipefail\n'
                "\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_process_substitution_glued_to_a_flag_cluster_is_unknown(self):
        """`set +u<(echo o) pipefail`: Bash expands this to `+u/dev/fd/N`,
        rejects the `/` as an invalid option and aborts, retaining
        pipefail -- confirmed by direct execution: dead.py never runs.
        Copying the substitution's literal text (`echo o`) into the word
        used to leak an `o` into the cluster scan, wrongly matching a
        real `+o` and crediting dead.py from the `pipefail` word after
        it -- the standalone case above (no glued prefix) is unaffected
        and still correctly aborts as an unrecognized literal name."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set +u<(echo o) pipefail\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_redirect_target_behind_a_variable_is_unknown_not_assumed_nonempty(self):
        """`set +o >"$EMPTY" pipefail` with EMPTY unset: the target's
        RUNTIME value decides whether the redirect succeeds, not its
        source text -- confirmed by direct execution: Bash rejects the
        expanded-empty filename exactly like a literal `>""` would,
        pipefail (already on) stays on, dead.py never runs. A target
        carrying `$`/backtick can't be judged empty-or-not from its
        source characters alone."""
        self.assertEqual(
            program_python_args(
                "EMPTY=\n"
                "set -o pipefail\n"
                'set +o >"$EMPTY" pipefail\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_heredoc_with_no_delimiter_word_at_all_is_a_syntax_error(self):
        """`set +o pipefail <<` with nothing after the operator: Bash
        reports a syntax error and never runs anything -- confirmed by
        direct execution (rc=2). Distinct from `<<""` (a delimiter word
        IS present, just empty, and is valid) -- this line has NO
        delimiter word at all, so it never executes and pipefail
        (already on) stays on, dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set +o pipefail <<\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_an_expandable_redirect_on_a_non_set_command_does_not_poison_pipefail(self):
        """`printf x >"$OUT"`: only `set` can change pipefail -- confirmed
        by direct execution: printf's own redirect uncertainty is
        irrelevant, pipefail (already off) stays off, dead.py runs.
        Returning "unknown" before checking the command name would wrongly
        treat every command with an uncertain redirect as pipefail-
        relevant, not just `set`."""
        self.assertEqual(
            program_python_args(
                "OUT=/dev/null\n"
                "set +o pipefail\n"
                'printf x >"$OUT"\n'
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_a_leading_expandable_redirect_still_poisons_pipefail_on_set(self):
        """`>"$EMPTY" set +o pipefail`: the redirect precedes the command
        word, so `words` is empty at the moment it's seen -- confirmed by
        direct execution: Bash rejects the expanded-empty target (rc=1),
        `set +o` never runs, pipefail (already on) stays on, dead.py never
        runs. Deciding at the redirect (round 21's gate) reads a leading
        redirect's uncertainty as not-yet-`set` and discards it forever."""
        self.assertEqual(
            program_python_args(
                "EMPTY=\n"
                "set -o pipefail\n"
                '>"$EMPTY" set +o pipefail\n'
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_delimiter_less_heredoc_halts_the_whole_program_not_just_its_line(self):
        """`set -o pipefail <<` with nothing after the operator: a real
        Bash PARSE error, confirmed by direct execution (rc=2, nothing
        after it ever runs). Starting pipefail OFF (not on) makes this
        discriminating -- modeling the line as merely "no state change"
        (round 21's `None`) leaves pipefail off, the pipe's off-status
        succeeds, and dead.py wrongly runs; only "parsing terminates"
        blocks it."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                "set -o pipefail <<\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_an_ordinary_prefix_glued_to_process_substitution_is_already_positional(self):
        """`set x<(true) -o pipefail`: Bash's option scan stops at the
        leading `x` the instant it sees it -- confirmed by direct
        execution: pipefail (already off) stays off, dead.py runs. `x` is
        not `-`/`+`-shaped, so it is already a guaranteed positional word
        without any expandable-marking at all; marking it expandable
        anyway (round 20's over-broad `if val:`) wrongly poisoned it into
        "unknown" and stopped crediting dead.py."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                "set x<(true) -o pipefail\n"
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_process_substitution_inside_an_already_supported_brace_group_is_expandable(self):
        """`set {+u<(echo o),pipefail}`: Bash expands the ONE brace group
        to `+u/dev/fd/N` and `pipefail`, rejects the invalid option,
        retains pipefail -- confirmed by direct execution: dead.py never
        runs. At the point `<(` is seen the `{` hasn't split yet, so
        `val[0]` alone is `{`, not `+`; round 23 peeks past a leading
        unquoted `{` to the char that will start the first split word."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set {+u<(echo o),pipefail}\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_a_heredoc_body_line_is_data_never_a_command(self):
        """`: <<'EOF' / set -o pipefail << / EOF / python3 dead.py`: the
        middle line is literal heredoc BODY text, never executed --
        confirmed by direct execution: dead.py runs. Scanned as a real
        command it looks exactly like the fatal delimiter-less-heredoc
        case (round 22), which would wrongly halt the whole rest of the
        program; round 23 strips heredoc bodies before any command scan."""
        self.assertEqual(
            program_python_args(
                ": <<'EOF'\n"
                "set -o pipefail <<\n"
                "EOF\n"
                "python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_a_backgrounded_set_does_not_mutate_the_parent_shell(self):
        """qingyun-wu round 24: `set +o pipefail &` runs in a SUBSHELL like
        a pipe stage does -- confirmed by direct execution: pipefail
        (already on) stays on in the parent, dead.py never runs."""
        self.assertEqual(
            program_python_args(
                "set -o pipefail\n"
                "set +o pipefail &\n"
                "wait\n"
                "false | true && python3 packages/x/dead.py"), [])

    def test_arithmetic_left_shift_is_not_a_heredoc(self):
        """keweichen round 24: `<<` inside `$((...))` is arithmetic
        left-shift, not a redirect at all -- confirmed by direct
        execution: dead.py runs. Misread as a heredoc it swallows the
        rest of the program as a never-terminated body."""
        self.assertEqual(
            program_python_args(
                "x=$((1 << 2))\n"
                "python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_a_blank_heredoc_terminator_is_not_stripped_before_it_is_seen(self):
        """keweichen round 25: `: <<''` (empty quoted delimiter) terminates
        at the first BLANK line -- confirmed by direct execution: dead.py
        runs. The old pipeline ran comment/blank-line dropping BEFORE
        heredoc-body stripping, so the blank terminator line was already
        gone by the time the heredoc scan looked for it, and everything
        after was wrongly swallowed as an unterminated body."""
        self.assertEqual(
            program_python_args(
                ": <<''\n"
                "\n"
                "python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_a_quoted_hash_heredoc_terminator_is_not_read_as_a_comment(self):
        """A terminator line that's literally `#` is heredoc DATA, never a
        real Bash comment -- confirmed by direct execution: dead.py runs.
        The old pipeline's separate comment-stripping pass treated it as
        a whole-line comment and dropped it before the heredoc scan ever
        saw it, for the same reason as the blank-terminator case above."""
        self.assertEqual(
            program_python_args(
                ": <<'#'\n"
                "#\n"
                "python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_process_substitution_glued_to_a_non_splitting_brace_stays_literal(self):
        """keweichen round 25: `{+u}` has no comma anywhere in it, so it
        stays ONE literal word (never splits) -- confirmed by direct
        execution: pipefail (already off) stays off, dead.py runs. The
        round-23 fix peeked past ANY leading `{` regardless of whether the
        group would actually split, wrongly marking this word expandable
        and poisoning a case the parser's own scan-stop already handled."""
        self.assertEqual(
            program_python_args(
                "set +o pipefail\n"
                "set {+u}<(echo o) -o pipefail\n"
                "false | true && python3 packages/x/dead.py"),
            ["packages/x/dead.py"])

    def test_a_heredoc_nested_inside_arithmetic_command_substitution_still_runs(self):
        """keweichen round 25: `$(( $(cmd <<EOF ...) ))` -- the nested
        `$(...)` re-enters a real command context, so its own heredoc is
        genuine (`body.py` is data, never invoked) and the outer
        arithmetic's `<<` suppression must not swallow it too. Confirmed
        by direct execution: only live.py runs."""
        self.assertEqual(
            program_python_args(
                "x=$(( $(cat <<EOF >/dev/null\n"
                "python3 packages/x/body.py\n"
                "EOF\n"
                "echo 1\n"
                ") ))\n"
                "python3 packages/x/live.py\n"),
            ["packages/x/live.py"])

    def test_if_as_a_plain_argument_is_not_a_branch_opener(self):
        """keweichen round 25: `printf if` -- `if` is ordinary argv here,
        not a command-position keyword, so the `||` after it must not be
        masked. Confirmed by direct execution: dead.py runs."""
        self.assertEqual(
            program_python_args(
                "false && printf if || python3 packages/x/dead.py\n"),
            ["packages/x/dead.py"])


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
