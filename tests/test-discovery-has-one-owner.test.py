#!/usr/bin/env python3
"""Both runners must DELEGATE test discovery to one helper, not re-implement it.

Run: python3 tests/test-discovery-has-one-owner.test.py

Four review rounds of parser edge cases came from two runners each writing their
own `find` and a guard reading that text back. The helper is now the single
owner; these pins stop a runner re-growing its own copy, which is what made the
drift invisible before.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from active_code import active_lines, invokes, program_invokes  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DISCOVER = REPO / "scripts" / "discover-python-tests.sh"
RUNNERS = [REPO / ".github" / "workflows" / "ci.yml",
           REPO / "scripts" / "coverage-gate.sh",
           REPO / "package.json"]
INLINE_FIND = re.compile(r"\bfind\b.*-name\s+'\*\.test\.py'")



def _shell_text(runner: Path) -> str:
    """The SHELL the runner actually executes.

    package.json embeds its script inside a JSON string, so reading the file as
    text puts the whole pipeline on one JSON line and no command position is
    visible. Ask each format for its shell rather than pattern-matching bytes.

    Joining every script's value let an unrelated key satisfy "actively
    invokes the helper" while `test:py` — the one `npm test` actually
    reaches — was gutted to a no-op. Pin that one script, not the object.
    """
    if runner.name == "package.json":
        import json
        return json.loads(runner.read_text()).get("scripts", {}).get("test:py", "")
    return runner.read_text()


class TestDiscoveryHasOneOwner(unittest.TestCase):
    def test_the_helper_exists_and_is_executable(self):
        self.assertTrue(DISCOVER.is_file(), f"{DISCOVER} missing")
        self.assertTrue(DISCOVER.stat().st_mode & 0o111, f"{DISCOVER} is not executable")

    def test_every_runner_ACTIVELY_invokes_the_helper(self):
        """A substring assertion passes on a COMMENT naming the helper while the
        runner writes an empty file list and exits green — measured, not feared."""
        for r in RUNNERS:
            active = [ln for ln in active_lines(_shell_text(r))
                      if DISCOVER.name in ln]
            self.assertTrue(active,
                            f"{r.name} names {DISCOVER.name} only in a comment (or not at all) — "
                            "a named-but-uncalled helper leaves the runner discovering nothing")
            # The WHOLE program, not a line at a time: `false &&` at the end of
            # one line guards the call on the next, and a per-line scan credited it.
            self.assertTrue(program_invokes(_shell_text(r), DISCOVER.name),
                            f"{r.name} mentions {DISCOVER.name} outside a comment but never "
                            f"runs it in command position: {active}")

    def test_package_json_check_is_pinned_to_test_py(self):
        # An unrelated script must not satisfy this for package.json — only
        # test:py, the one `npm test` actually reaches, may.
        import json
        import tempfile
        pkg = json.loads((REPO / "package.json").read_text())
        pkg["scripts"]["test:py"] = ': > "$RECDIR/files"'
        pkg["scripts"]["unrelated"] = f"bash scripts/{DISCOVER.name}"
        with tempfile.TemporaryDirectory() as td:
            mutated = Path(td) / "package.json"
            mutated.write_text(json.dumps(pkg))
            active = [ln for ln in active_lines(_shell_text(mutated)) if DISCOVER.name in ln]
        self.assertEqual(active, [],
                         "test:py was gutted but an unrelated script still satisfied "
                         "the guard — package.json must be pinned to test:py alone")

    def test_no_runner_reimplements_discovery(self):
        for r in RUNNERS:
            offenders = [ln.strip() for ln in active_lines(_shell_text(r))
                         if INLINE_FIND.search(ln)]
            self.assertEqual(offenders, [],
                             f"{r.name} has its own test-discovery find: {offenders}. "
                             "Two implementations drift, and the drift is silent.")

    def test_the_helper_actually_reaches_the_tests_root(self):
        out = subprocess.run(["bash", str(DISCOVER)], cwd=str(REPO),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, f"helper failed: {out.stderr.strip()}")
        paths = out.stdout.split()
        self.assertTrue(any(p.startswith("tests/") for p in paths), "no tests/ files discovered")

    def test_the_helper_reaches_the_skills_root_too(self):
        # A synthetic tree: no committed skill puts a *.test.py directly under
        # skills/ today, so a real-content check here would fail regardless.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "tests").mkdir()
            (td / "tests" / "a.test.py").write_text("")
            (td / "skills" / "demo" / "tests").mkdir(parents=True)
            (td / "skills" / "demo" / "tests" / "b.test.py").write_text("")
            (td / "scripts").mkdir()
            shutil.copy2(DISCOVER, td / "scripts" / DISCOVER.name)
            out = subprocess.run(["bash", "scripts/" + DISCOVER.name], cwd=str(td),
                                 capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, f"helper failed: {out.stderr.strip()}")
        paths = out.stdout.split()
        self.assertIn("skills/demo/tests/b.test.py", paths,
                      "the helper's skills-root walk did not reach a real skill test")

    def test_a_commented_out_call_does_not_satisfy_delegation(self):
        """keweichen's mutation, committed: the runner names the helper in a
        comment and writes an empty list. CI then runs zero tests and exits
        green, so this must be indistinguishable from no delegation at all."""
        mutated = ': > "$RECDIR/files"  # ' + DISCOVER.name
        active = [ln for ln in [mutated]
                  if DISCOVER.name in ln and not ln.lstrip().startswith("#")
                  and not re.match(r"^[^#]*#[^#]*" + re.escape(DISCOVER.name), ln)]
        self.assertEqual(active, [],
                         "a commented-out call is being counted as an active one")

    def test_the_helper_refuses_to_emit_an_empty_list(self):
        """Zero discovered tests must fail closed AT the helper. A consumer handed
        an empty list runs nothing, records failed=0 and exits green — and because
        this suite is itself discovered by the helper, it would not run to notice."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "tests").mkdir()
            shutil.copy(DISCOVER, Path(td) / DISCOVER.name)
            r = subprocess.run(["bash", DISCOVER.name], cwd=td, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0,
                            "helper exited 0 with no tests discovered — silently green")
        self.assertEqual(r.stdout.strip(), "", "helper emitted a list while refusing")

    def test_the_helper_is_order_and_comment_proof_by_construction(self):
        """The property the parser could never hold: there is nothing to parse."""
        body = DISCOVER.read_text()
        self.assertNotIn("grep", body.split("find")[0],
                         "the helper should RUN find, not derive roots from text")


class TestAdjacentGuardDefects(unittest.TestCase):
    """Two false-greens keweichen measured in the guard itself, as fixtures.

    Each drives the real function; the first versions asserted on a local string
    and on a helper called directly, so neither could fail when its subject broke.
    """

    def _guard(self):
        import importlib.util as _i
        spec = _i.spec_from_file_location(
            "g", REPO / "tests" / "ci-covers-every-python-test.test.py")
        m = _i.module_from_spec(spec)
        try:
            spec.loader.exec_module(m)
        except SystemExit:
            pass
        return m

    def test_a_path_with_a_space_stays_one_path(self):
        """Drives discovered_by_find(); whitespace splitting makes this fail."""
        import types
        m = self._guard()
        canned = types.SimpleNamespace(
            returncode=0, stdout="tests/space name.test.py\ntests/ok.test.py\n", stderr="")
        real = m.subprocess
        m.subprocess = types.SimpleNamespace(run=lambda *a, **k: canned)
        try:
            got = m.discovered_by_find()
        finally:
            m.subprocess = real
        self.assertEqual(got, {"tests/space name.test.py", "tests/ok.test.py"})

    def test_a_quoted_call_in_a_run_body_is_not_a_caller(self):
        """`echo 'python3 x.py'` names a path without running it.

        Counting it masks the file from the orphan check, which is the one
        thing this guard exists to find."""
        m = self._guard()
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "x.yml").write_text(
                "      - run: python3 real/thing.py\n"
                "      - run: echo 'python3 packages/x/test_echoed.py'\n"
                "      - name: python3 packages/x/test_nameonly.py\n")
            real = m.REPO
            m.REPO = Path(td)
            try:
                named = m.named_in_workflows()
            finally:
                m.REPO = real
        self.assertIn("real/thing.py", named)
        self.assertNotIn("packages/x/test_echoed.py", named,
                         "a quoted path read as an invocation")
        self.assertNotIn("packages/x/test_nameonly.py", named,
                         "a path under name: is data, not a command")

    def test_a_call_guarded_across_a_line_break_is_not_an_active_caller(self):
        """keweichen's third [P2] on #4202: the shell state that `&&` opens at the
        end of one line was lost at the physical line boundary, so both real
        consumers credited a call Bash never reaches."""
        program = f": > files\nfalse &&\n  bash scripts/{DISCOVER.name} > files || true\n"
        self.assertFalse(program_invokes(program, DISCOVER.name))
        # The same helper, unguarded on its own line, still counts.
        self.assertTrue(program_invokes(f": > files\nbash scripts/{DISCOVER.name} > files\n", DISCOVER.name))

    def test_a_commented_invocation_is_not_an_active_caller(self):
        """Drives named_in_workflows() over a real workflow file on disk."""
        m = self._guard()
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "x.yml").write_text(
                "      # python3 packages/x/test_dead.py\n"
                "      - run: python3 real/thing.py\n")
            real = m.REPO
            m.REPO = Path(td)
            try:
                named = m.named_in_workflows()
            finally:
                m.REPO = real
        self.assertIn("real/thing.py", named)
        self.assertNotIn("packages/x/test_dead.py", named,
                         "a commented-out invocation still reads as a caller")


if __name__ == "__main__":
    unittest.main(verbosity=2)
