#!/usr/bin/env python3
"""scripts/witness-reap.sh runs the process-hygiene reaper once at the end of a witness.

Every case runs copies of the scripts in a temp tree with a stub reaper that records
its argv, under `env -i`, so no real reaper, workspace or ~/.claude is reached.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "witness-reap.sh"
HARNESS = REPO / "scripts" / "restart-test-harness.sh"
REL = Path("process-hygiene") / "scripts" / "reap_orphans.py"

# Scripts that spawn a service, core, agent or browser to collect evidence; each must
# run the helper on exit.
WITNESS_SCRIPTS = ("scripts/restart-test-harness.sh",)
# Named like a witness but spawns nothing: it only reads the workspace a live bridge writes.
NOT_WITNESS_SCRIPTS = ("skills/mention-gate/scripts/witness.py",)

STUB_REAPER = """import json, sys
from pathlib import Path
with open(Path(__file__).with_name("calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print("process-hygiene: mode=reap roots=0 candidates=0 stub")
sys.exit({rc})
"""


def _env(home):
    path = os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"])
    return {"PATH": path, "HOME": str(home), "LANG": "C"}


class Tree:
    """A copied repo layout: scripts/ with the helper and harness, optional stub skills."""

    def __init__(self, tmp):
        self.root = Path(tmp)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for src in (HELPER, HARNESS):
            shutil.copy2(src, self.repo / "scripts" / src.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def stub_skill(self, skills_root, rc=0):
        reaper = Path(skills_root) / REL
        reaper.parent.mkdir(parents=True, exist_ok=True)
        reaper.write_text(STUB_REAPER.format(rc=rc))
        return reaper.with_name("calls.jsonl")

    def stub_workspace(self):
        ws = self.root / "ws"
        ws.mkdir()
        cfg = self.repo / "scripts" / "sutando-config.sh"
        cfg.write_text(f'#!/bin/bash\n[ "$1" = workspace ] && printf %s "{ws}"\n')
        return ws

    def stub_resolver(self, interpreter):
        """A scripts/python-binary.sh whose resolve_python prints `interpreter` (may be empty)."""
        (self.repo / "scripts" / "python-binary.sh").write_text(
            f"resolve_python() {{ printf %s \"{interpreter}\"; }}\n")

    def wrapped_python(self):
        """An interpreter that records each use, then runs the real one."""
        wrapper = self.root / "resolved-python"
        log = self.root / "resolved-python.log"
        wrapper.write_text(f'#!/bin/bash\necho used >> "{log}"\nexec "{sys.executable}" "$@"\n')
        wrapper.chmod(0o755)
        return wrapper, log

    def run(self, script, *args):
        return subprocess.run(["bash", str(self.repo / "scripts" / script), *args],
                              env=_env(self.home), capture_output=True, text=True, timeout=60)


def _calls(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


class HelperTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = Tree(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_runs_reaper_once_with_reap_and_spotlight(self):
        log = self.t.stub_skill(self.t.repo / "skills", rc=0)
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [["--reap", "--spotlight"]])
        self.assertIn("witness-reap: process-hygiene: mode=reap", out.stderr)

    def test_reaper_error_is_printed_and_exit_is_zero(self):
        log = self.t.stub_skill(self.t.repo / "skills", rc=2)
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [["--reap", "--spotlight"]])
        self.assertIn("reaper exited rc=2", out.stderr)

    def test_absent_skill_is_skipped_with_one_line(self):
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr.strip().splitlines(),
                         ["witness-reap: process-hygiene skill not installed; skipping"])

    def test_reaper_runs_under_the_resolved_interpreter(self):
        log = self.t.stub_skill(self.t.repo / "skills", rc=0)
        wrapper, used = self.t.wrapped_python()
        self.t.stub_resolver(wrapper)
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(used.read_text().splitlines(), ["used"])
        self.assertEqual(_calls(log), [["--reap", "--spotlight"]])

    def test_resolver_finding_no_python_skips_rather_than_run_bare_python3(self):
        log = self.t.stub_skill(self.t.repo / "skills", rc=0)
        self.t.stub_resolver("")
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [])
        self.assertIn("no runnable python3", out.stderr)

    def test_workspace_skill_is_found(self):
        ws = self.t.stub_workspace()
        log = self.t.stub_skill(ws / "skills", rc=0)
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [["--reap", "--spotlight"]])

    def test_shipped_skill_shadows_the_workspace_copy(self):
        ws = self.t.stub_workspace()
        ws_log = self.t.stub_skill(ws / "skills", rc=0)
        repo_log = self.t.stub_skill(self.t.repo / "skills", rc=0)
        self.assertEqual(self.t.run("witness-reap.sh").returncode, 0)
        self.assertEqual(_calls(repo_log), [["--reap", "--spotlight"]])
        self.assertEqual(_calls(ws_log), [])


class HarnessTest(unittest.TestCase):
    """The restart harness runs the helper on exit and keeps its own exit code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = Tree(self._tmp.name)
        self.log = self.t.stub_skill(self.t.repo / "skills", rc=2)
        counter = self.t.root / "counter"
        self.changing = f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"; echo pid-$n'

    def tearDown(self):
        self._tmp.cleanup()

    def _harness(self, identity, *extra):
        return self.t.run("restart-test-harness.sh", "--start", "true", "--restart", "true",
                          "--probe", "true", "--identity", identity, "--settle", "0", *extra)

    def test_pass_keeps_rc_0_after_a_failing_reaper(self):
        out = self._harness(self.changing)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(_calls(self.log), [["--reap", "--spotlight"]])

    def test_fail_keeps_rc_1_and_still_reaps(self):
        out = self._harness("echo same")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(_calls(self.log), [["--reap", "--spotlight"]])

    def test_usage_error_spawns_nothing_and_does_not_reap(self):
        out = self.t.run("restart-test-harness.sh", "--bogus", "x")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(_calls(self.log), [])


class WitnessListTest(unittest.TestCase):
    """Structural pin: every witness script delegates to the helper, and a new script
    named like a witness must be classified here."""

    def test_each_witness_script_calls_the_helper(self):
        for rel in WITNESS_SCRIPTS:
            with self.subTest(script=rel):
                text = (REPO / rel).read_text()
                active = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
                self.assertTrue(any("witness-reap.sh" in ln for ln in active), rel)

    def test_every_witness_named_script_is_classified(self):
        found = set()
        for top in ("scripts", "skills", "src"):
            for dirpath, dirnames, filenames in os.walk(REPO / top):
                dirnames[:] = [d for d in dirnames if d not in ("node_modules", "__pycache__")]
                for name in filenames:
                    if (name.startswith(("witness", "wit-")) and name.endswith((".sh", ".py"))
                            and name != HELPER.name):
                        found.add(str((Path(dirpath) / name).relative_to(REPO)))
        self.assertEqual(found - set(WITNESS_SCRIPTS) - set(NOT_WITNESS_SCRIPTS), set())


if __name__ == "__main__":
    unittest.main()
