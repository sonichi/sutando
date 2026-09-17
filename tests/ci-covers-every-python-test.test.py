#!/usr/bin/env python3
"""Every test-looking Python file must actually be executed by CI.

CI discovers Python tests with `find tests -name '*.test.py'`. Anything outside
that root or suffix runs only if ci.yml names it explicitly. A fixed list is
maintenance the next author will not know they owe: a test added under
packages/*/tests/ is silently never run, and reads as coverage anyway.

This guard makes that gap self-detecting instead of silent. It failed to exist
when five suites -- including the transport gate and the src/-vs-package drift
guard -- had never run in CI.
"""
import glob
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from active_code import active_lines, active_text, python_args, program_python_args  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
COVGATE = REPO / "scripts" / "coverage-gate.sh"
DISCOVER = REPO / "scripts" / "discover-python-tests.sh"


def discovered_by_find():
    """What CI actually discovers — by RUNNING the one discovery owner.

    Earlier revisions parsed `find` and its root assignments out of ci.yml and
    coverage-gate.sh. Four review rounds found four ways that lied (unioned
    roots hiding a one-sided loss, comments, reset-vs-append order, assignments
    placed after the consumer). Reading a declaration is not observing the
    behaviour; executing the shared helper is."""
    out = subprocess.run(["bash", str(DISCOVER)], cwd=str(REPO),
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"{DISCOVER.name} failed rc={out.returncode}: {out.stderr.strip()}")
    # splitlines, not split: a path containing a space becomes two fake paths
    # under whitespace splitting, and both then read as undiscovered.
    return {p for p in out.stdout.splitlines() if p and "node_modules" not in p}


def _uncommented(text: str) -> str:
    """Lines with comments removed. A commented-out invocation is not a caller."""
    return active_text(text)


def _yaml_scalar(value: str) -> str:
    """A wholly YAML-quoted `run:` value, unwrapped.

    The quotes are YAML's, not the shell's; leaving them makes the shell-quote
    blanking swallow a real invocation."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" and v[0] not in v[1:-1]:
        return v[1:-1]
    return value


def _run_bodies(text: str) -> list[str]:
    """Lines inside a workflow `run:` value — the only place a command executes.

    A path under `name:` or `if:` is data; scanning the whole file counts it."""
    out, indent = [], None
    for ln in text.splitlines():
        stripped = ln.strip()
        m = re.match(r"-?\s*run:\s*\|?-?\s*(.*)$", stripped)
        if m and re.search(r"(^|\s)run:", stripped):
            # The KEY's column, not the line's: a `- ` list marker sits left of
            # it, so a sibling key would otherwise read as a continuation line.
            indent = ln.index("run:")
            if m.group(1):
                out.append(_yaml_scalar(m.group(1)))
            continue
        if indent is not None:
            if stripped and (len(ln) - len(ln.lstrip())) <= indent:
                indent = None
            else:
                out.append(ln)
    return out


def named_in_workflows():
    """Files any workflow ACTIVELY invokes, e.g. `python3 path/to/x.py`.

    Position, not presence, via python_args(): `echo python3 x.py` (no
    quotes to blank) used to name x.py as if it ran, and a real quoted
    invocation like `python3 'x.py'` used to be blanked to nothing along
    with it — a regex over unquoted text can't tell an argument from a
    caller in either direction."""
    named = set()
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        named.update(_named_in(wf.read_text()))
    return named


def _named_in(workflow_text: str) -> set:
    """One workflow's actively-invoked scripts: run bodies scanned as ONE program,
    so a `false &&` guard survives the line break it sits on."""
    return set(program_python_args("\n".join(_run_bodies(workflow_text))))


def test_looking_files():
    """Git-TRACKED test-looking files only.

    A bare glob also sees transient files that other suites create while running
    (temp fixtures written inside the tree), which made this guard fail in one CI
    step and pass in another within the same job — order-dependent, and a flaky
    guard is worse than none. Only committed files are part of the repo's test
    surface, so ask git rather than the filesystem."""
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout — this guard is about committed files")
    files = [f for f in out.stdout.decode().split("\0") if f]
    keep = set()
    for f in files:
        base = Path(f).name
        if "node_modules" in f:
            continue
        if base.endswith(".test.py") or base.startswith("test_") and base.endswith(".py") \
           or base.endswith("_test.py"):
            keep.add(str(Path(f)))
    return keep


def orphans_in(all_files, discovered, named):
    """The actual rule, extracted so a synthetic case can pin it.

    Inlined, this was un-pinnable: gutting it to `orphans = []` passed the whole
    suite, because the only other test exercised set arithmetic in isolation
    rather than the code path the real assertion uses."""
    return sorted(set(all_files) - set(discovered) - set(named))


class TestCICoversEveryPythonTest(unittest.TestCase):
    def test_no_python_test_is_invisible_to_ci(self):
        import os
        os.chdir(REPO)
        orphans = orphans_in(test_looking_files(), discovered_by_find(), named_in_workflows())
        self.assertEqual(
            orphans, [],
            "these test files are never executed by CI — either move them to "
            "tests/<name>.test.py (auto-discovered) or name them explicitly in "
            "a workflow:\n  " + "\n  ".join(orphans),
        )

    def test_the_guard_can_actually_fail(self):
        """A guard that cannot fire is the bug it exists to catch.

        Exercises orphans_in() — the same function the real assertion calls — so
        stubbing that computation breaks this case too. Testing the set algebra
        inline instead left the real check gutted-and-green."""
        self.assertEqual(
            orphans_in({"packages/somewhere/tests/test_invented.py"}, set(), set()),
            ["packages/somewhere/tests/test_invented.py"],
            "an out-of-tree file must register as an orphan")

    def test_a_discovered_file_is_not_an_orphan(self):
        self.assertEqual(orphans_in({"tests/x.test.py"}, {"tests/x.test.py"}, set()), [])

    def test_a_workflow_named_file_is_not_an_orphan(self):
        self.assertEqual(orphans_in({"scripts/y.py"}, set(), {"scripts/y.py"}), [])


class TestRunBodiesAreScannedAsAProgram(unittest.TestCase):
    """keweichen's [P2] on #4202 (third round): per-line scanning of run bodies,
    and the two YAML repairs that no test pinned -- each case below is one
    in-memory mutation he ran that stayed green."""

    def test_a_guard_at_the_end_of_a_line_still_guards_the_next_line(self):
        wf = "steps:\n  - run: |\n      false &&\n        python3 packages/x/test_dead.py || true\n"
        self.assertEqual(_named_in(wf), set())

    def test_a_wholly_quoted_run_scalar_names_its_real_call(self):
        # Drop the _yaml_scalar() unwrap and the shell-quote blanking swallows this.
        wf = "steps:\n  - run: \"python3 packages/x/test_real.py\"\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})

    def test_a_sibling_env_block_is_not_a_run_body(self):
        # Key-column indent: `env:` sits at the key's column, and its block-scalar
        # value is a bare invocation -- the line a dash-based indent would swallow.
        wf = ("steps:\n  - run: |\n      python3 packages/x/test_real.py\n"
              "    env:\n      CMD: |\n        python3 packages/x/test_inert.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})

    def test_an_attached_dash_c_does_not_name_its_argument(self):
        wf = "steps:\n  - run: python3 -cpass packages/x/test_dead.py\n"
        self.assertEqual(_named_in(wf), set())


class OptionContractThroughTheConsumerPath(unittest.TestCase):
    """keweichen's round-6 ask: pin the option contract through
    _named_in() -> orphans_in(), not only python_args() directly -- a
    workflow that credits a terminal flag's argument would silently drop
    a real orphan from CI-coverage scrutiny (false-green); one that gives
    up at `--` or a separate-value long option would flag a genuinely
    covered test as an orphan (false-orphan, the safe direction, but
    still a false alarm this suite would otherwise manufacture)."""

    def test_a_terminal_flag_does_not_false_green_an_orphan(self):
        """If -V's argument were credited, this test would vanish from
        the orphan set though python3 never opens it -- the dangerous
        direction, checked through the real consumer, not just python_args()."""
        wf = "steps:\n  - run: python3 -V packages/x/test_dead.py\n"
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_bare_double_dash_does_not_false_orphan_a_real_test(self):
        """A workflow line most of this repo's own CI could plausibly write
        -- `--` before a path defends against an accidental leading-dash
        filename. Giving up at `--` would flag this real, executed test
        as an orphan."""
        wf = "steps:\n  - run: python3 -- packages/x/test_real.py\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_a_separate_value_long_option_does_not_false_orphan_either(self):
        wf = ("steps:\n  - run: python3 --check-hash-based-pycs always "
              "packages/x/test_real.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_hidden_compat_flags_do_not_false_orphan(self):
        """keweichen's round-8 ask: -R/-t are real (missing from --help,
        confirmed by execution) and must not cost a covered test its
        credit through the actual consumer path."""
        for flag in ("-R", "-t"):
            wf = f"steps:\n  - run: python3 {flag} packages/x/test_real.py\n"
            self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
            self.assertEqual(
                orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_an_invalid_hash_policy_value_does_not_false_green_an_orphan(self):
        wf = ("steps:\n  - run: python3 --check-hash-based-pycs sometimes "
              "packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_q_and_x_do_not_false_orphan_through_the_consumer_path(self):
        """keweichen's round-9 ask: pin q/x as unchanged-neighbor controls
        so a future table edit that drops them again fails here too, not
        just in the direct python_args() tests."""
        for flag in ("-q", "-x"):
            wf = f"steps:\n  - run: python3 {flag} packages/x/test_real.py\n"
            self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
            self.assertEqual(
                orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_true_and_and_does_not_false_orphan_through_the_consumer_path(self):
        """The literal `&&`/`||` gap, pinned through the actual
        _named_in()/orphans_in() path, not just program_python_args()."""
        wf = "steps:\n  - run: true && python3 packages/x/test_real.py\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_false_or_or_does_not_false_orphan_through_the_consumer_path(self):
        wf = "steps:\n  - run: false || python3 packages/x/test_real.py\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_false_and_and_still_does_not_false_green_through_the_consumer_path(self):
        """The original defect this file exists to prevent, re-checked
        through the consumer path now that true/false LHS is decidable."""
        wf = "steps:\n  - run: false && python3 packages/x/test_dead.py\n"
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_gated_pipe_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen's round-11 finding, pinned through _named_in()/orphans_in():
        a lone `|` used to reset reachability, so a guard's dead pipe still
        credited its script through this exact path."""
        wf = ("steps:\n  - run: false && printf x | "
              "python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_pipefail_does_not_false_orphan_through_the_consumer_path(self):
        """Same round's child regression: `set -o pipefail` makes a later
        `&&` see the pipe's real failure, pinned through the consumer path."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_leading_o_cluster_form_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 12: `set -oe pipefail` (o first, not last)."""
        wf = ("steps:\n  - run: |\n"
              "      set -oe pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_negated_pipeline_does_not_false_orphan_through_the_consumer_path(self):
        """Round 12's child false-orphan regression: a leading `!` negates
        the pipe's pipefail-adjusted result, so `&&` DOES run this one."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      ! false | true && python3 packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_amp_pipe_does_not_false_orphan_through_the_consumer_path(self):
        """`|&` was parsed as `|` then a hard-reset `&`, undoing the outer
        guard -- confirmed on Bash 5.2.32 (keweichen), pinned here."""
        wf = ("steps:\n  - run: false && printf x |& "
              "python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_set_dash_dash_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 13: `--` ends `set` option scanning."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set -- +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_negated_direct_invocation_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 13: `!` doesn't hide the invocation from the
        consumer path either."""
        wf = "steps:\n  - run: '! python3 packages/x/test_live.py'\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_a_quoted_bang_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 15: a SHELL-quoted `'!'` is a literal command
        name (Bash: 127), not the negation reserved word."""
        wf = "steps:\n  - run: \"'!' python3 packages/x/test_dead.py\"\n"
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_set_scanning_stop_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 15: `set` ends its own option scanning at the
        first non-option word, pinned through the consumer path."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set -o pipefail positional +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
