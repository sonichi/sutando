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


if __name__ == "__main__":
    unittest.main(verbosity=2)
