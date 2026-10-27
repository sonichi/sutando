#!/usr/bin/env python3
"""scripts/witness-reap.sh runs the process-hygiene reaper once at the end of a witness.

Every case runs copies of the scripts in a temp tree: a stub sutando-config.sh points
the installed skills dir at a temp claude-home, and a stub reaper records its argv.
Under `env -i`, so no real reaper, workspace or ~/.claude is reached.
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
MANIFEST = REPO / "skills" / "process-hygiene" / "manifest.json"
KEY = "PROCESS_HYGIENE_WITNESS_REAP"
REPORT = ["--spotlight"]
REAP = ["--reap", "--spotlight"]

# Scripts that spawn a service, core, agent or browser to collect evidence; each must
# run the helper on exit.
WITNESS_SCRIPTS = ("scripts/restart-test-harness.sh",)
# Named like a witness but spawns nothing: it only reads the workspace a live bridge writes.
NOT_WITNESS_SCRIPTS = ("skills/mention-gate/scripts/witness.py",)

STUB_REAPER = """import json, sys
from pathlib import Path
with open(Path(__file__).with_name("calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print("process-hygiene: mode=stub roots=0 candidates=0")
sys.exit({rc})
"""


class Tree:
    """A copied repo layout with the helper and harness, a shipped stub skill, and a
    claude-home whose skills/ dir is what the stub sutando-config.sh reports."""

    def __init__(self, tmp):
        self.root = Path(tmp)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for src in (HELPER, HARNESS):
            shutil.copy2(src, self.repo / "scripts" / src.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.claude_home = self.root / "claude-home"
        (self.claude_home / "skills").mkdir(parents=True)
        (self.repo / "scripts" / "sutando-config.sh").write_text(
            '#!/bin/bash\n[ "$1" = claude-home-path ] || exit 1\nshift\n'
            f'p="{self.claude_home}"; for s in "$@"; do p="$p/$s"; done; printf %s "$p"\n')
        self.skill = self.repo / "skills" / "process-hygiene"

    def ship_skill(self, rc=0, opt_in=None):
        """The repo's copy of the skill: real manifest, stub reaper."""
        (self.skill / "scripts").mkdir(parents=True, exist_ok=True)
        (self.skill / "scripts" / "reap_orphans.py").write_text(STUB_REAPER.format(rc=rc))
        manifest = json.loads(MANIFEST.read_text())
        if opt_in is not None:
            manifest["config"][KEY] = opt_in
        (self.skill / "manifest.json").write_text(json.dumps(manifest))
        return self.skill / "scripts" / "calls.jsonl"

    def link_skill(self, target=None):
        """What skills/install.sh does: a symlink in the installed skills dir."""
        (self.claude_home / "skills" / "process-hygiene").symlink_to(target or self.skill)

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

    def run(self, script, *args, env=None):
        path = os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"])
        full = {"PATH": path, "HOME": str(self.home), "LANG": "C", **(env or {})}
        return subprocess.run(["bash", str(self.repo / "scripts" / script), *args],
                              env=full, capture_output=True, text=True, timeout=60)


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

    def test_shipped_manifest_declares_report_only_default(self):
        self.assertEqual(json.loads(MANIFEST.read_text())["config"][KEY], "0")

    def test_linked_skill_runs_report_only_by_default(self):
        log = self.t.ship_skill()
        self.t.link_skill()
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [REPORT])
        self.assertIn("witness-reap: process-hygiene: mode=stub", out.stderr)

    def test_env_opt_in_reaps(self):
        log = self.t.ship_skill()
        self.t.link_skill()
        self.assertEqual(self.t.run("witness-reap.sh", env={KEY: "1"}).returncode, 0)
        self.assertEqual(_calls(log), [REAP])

    def test_manifest_opt_in_reaps(self):
        log = self.t.ship_skill(opt_in="1")
        self.t.link_skill()
        self.assertEqual(self.t.run("witness-reap.sh").returncode, 0)
        self.assertEqual(_calls(log), [REAP])

    def test_env_overrides_manifest(self):
        log = self.t.ship_skill(opt_in="1")
        self.t.link_skill()
        self.t.run("witness-reap.sh", env={KEY: "0"})
        self.assertEqual(_calls(log), [REPORT])

    def test_cli_overrides_env(self):
        log = self.t.ship_skill()
        self.t.link_skill()
        self.t.run("witness-reap.sh", "--reap", env={KEY: "0"})
        self.t.run("witness-reap.sh", "--report-only", env={KEY: "1"})
        self.assertEqual(_calls(log), [REAP, REPORT])

    def test_shipped_but_not_linked_is_skipped(self):
        log = self.t.ship_skill()
        out = self.t.run("witness-reap.sh", env={KEY: "1"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [])
        self.assertEqual(out.stderr.strip().splitlines(),
                         ["witness-reap: process-hygiene skill not installed; skipping"])

    def test_link_to_a_dir_without_the_reaper_is_skipped(self):
        empty = self.t.root / "empty-skill"
        empty.mkdir()
        self.t.link_skill(empty)
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("process-hygiene skill not installed; skipping", out.stderr)

    def test_no_config_resolver_is_skipped(self):
        log = self.t.ship_skill()
        self.t.link_skill()
        (self.t.repo / "scripts" / "sutando-config.sh").unlink()
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [])
        self.assertIn("no installed skills dir resolved; skipping", out.stderr)

    def test_reaper_error_is_printed_and_exit_is_zero(self):
        log = self.t.ship_skill(rc=2)
        self.t.link_skill()
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [REPORT])
        self.assertIn("reaper exited rc=2", out.stderr)

    def test_reaper_runs_under_the_resolved_interpreter(self):
        log = self.t.ship_skill()
        self.t.link_skill()
        wrapper, used = self.t.wrapped_python()
        self.t.stub_resolver(wrapper)
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [REPORT])
        self.assertIn("used", used.read_text().splitlines())

    def test_resolver_finding_no_python_skips_rather_than_run_bare_python3(self):
        log = self.t.ship_skill()
        self.t.link_skill()
        self.t.stub_resolver("")
        out = self.t.run("witness-reap.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_calls(log), [])
        self.assertIn("no runnable python3", out.stderr)


class HarnessTest(unittest.TestCase):
    """The restart harness runs the helper on exit and keeps its own exit code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = Tree(self._tmp.name)
        self.log = self.t.ship_skill(rc=2)
        self.t.link_skill()
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
        self.assertEqual(_calls(self.log), [REPORT])

    def test_fail_keeps_rc_1_and_still_runs_the_reaper(self):
        out = self._harness("echo same")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(_calls(self.log), [REPORT])

    def test_usage_error_spawns_nothing_and_does_not_run_the_reaper(self):
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
