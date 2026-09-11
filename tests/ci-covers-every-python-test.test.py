#!/usr/bin/env python3
"""Every test-looking Python file must actually be executed by CI.

CI discovers Python tests with `find tests skills -name '*.test.py'`. Anything
outside those roots or suffix runs only if ci.yml names it explicitly. A fixed list is
maintenance the next author will not know they owe: a test added under
packages/*/tests/ is silently never run, and reads as coverage anyway.

This guard makes that gap self-detecting instead of silent. It failed to exist
when five suites -- including the transport gate and the src/-vs-package drift
guard -- had never run in CI.
"""
import glob
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
COVGATE = REPO / "scripts" / "coverage-gate.sh"


def _uncommented(text: str) -> str:
    """Shell/YAML lines with comments removed. Applied to BOTH halves: an earlier
    cut stripped comments only while locating the `find`, then scanned the raw
    text for assignments, so `# roots+=(skills)` still counted."""
    out = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out.append(ln.split(" #", 1)[0])
    return "\n".join(out)


def _find_roots_in(wiring: Path):
    """The roots the `find ... -name '*.test.py'` in ONE file actually passes.

    Parses the CONSUMER, not the declaration, and honours assignment ORDER: a
    later `roots=(...)` resets what earlier `+=` appended."""
    text = _uncommented(wiring.read_text())
    cmd = [ln for ln in text.splitlines()
           if re.search(r"find\s+.+-name\s+'\*\.test\.py'", ln)]
    if not cmd:
        raise AssertionError(f"no `find ... -name '*.test.py'` COMMAND in {wiring.name}")
    if len(cmd) > 1:
        raise AssertionError(f"{wiring.name} has {len(cmd)} test-discovery finds; "
                             "this guard assumes one and would check only part of it")
    m = re.search(r"find\s+(.+?)\s+-name\s+'\*\.test\.py'", cmd[0])
    args = m.group(1).strip()
    var = re.fullmatch(r'"\$\{(\w+)\[@\]\}"', args)
    if not var:
        return {a.strip('"\'') for a in args.split() if not a.startswith("-")}
    name = var.group(1)
    roots = set()
    # ANCHORED on the exact variable (a bare \w* also matched `oldroots`), and
    # applied in source order so a reset after an append is not silently unioned.
    for a in re.finditer(rf"(?:^|[;\s]){re.escape(name)}(\+?=)\(([^)]*)\)", text, re.M):
        vals = {t for t in a.group(2).split() if t and not t.startswith("$")}
        roots = (roots | vals) if a.group(1) == "+=" else set(vals)
    return roots


def ci_find_roots():
    """Per-file root sets, keyed by wiring file. Never unioned: each runner must
    independently reach every required root."""
    return {w.name: _find_roots_in(w) for w in (CI, COVGATE)}


def discovered_by_find():
    """What EVERY runner's find roots reach (intersection is the honest floor)."""
    per_file = ci_find_roots()
    if not per_file or not all(per_file.values()):
        raise AssertionError(f"a wiring file passes no find roots: {per_file}")
    roots = set.intersection(*per_file.values())
    found = set()
    for root in sorted(roots):
        found |= {str(Path(p)) for p in glob.glob(f"{root}/**/*.test.py", recursive=True)}
    return {p for p in found if "node_modules" not in p}


def named_in_workflows():
    """Files any workflow invokes explicitly, e.g. `python3 path/to/x.py`."""
    named = set()
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        for m in re.finditer(r"python3?\s+(\S+\.py)", wf.read_text()):
            named.add(m.group(1))
    return named


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

    def test_a_skill_owned_test_dir_is_discovered(self):
        """The skills root must be reached by discovery, not just by the glob's
        shape — a real committed skill test is the only proof of that."""
        import os
        os.chdir(REPO)
        found = discovered_by_find()
        self.assertTrue(
            any(f.startswith("skills/") and f.endswith(".test.py") for f in found),
            "no skills/**/*.test.py discovered — the skills root is not being walked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
