"""--fix must repair unregistered Claude hooks, and must NOT fire on the warn
branches the installer cannot repair.

The claude-hooks probe has existed since 2026-08-03; every app update strips
settings.json back to SessionStart alone and it has always taken a human reading
the warn to restore `PreCompact -> session-handoff.sh`. These tests pin the
repair, and pin that it stays keyed on a structured field rather than on the
warn's prose.
"""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


hc = _load()


def _fixture(root: Path, hooks_array: str, settings: dict):
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / "src" / "install-claude-hooks.sh").write_text(
        'SETTINGS="$REPO_DIR/.claude/settings.json"\n' + hooks_array + "\n"
    )
    (root / ".claude" / "settings.json").write_text(json.dumps(settings))


GOOD_HOOKS = 'HOOKS=(\n  "Stop|src/check-pending-tasks.sh|bash /x/src/check-pending-tasks.sh"\n)'


class ProbeAttachesStructuredField(unittest.TestCase):
    def test_missing_hook_carries_the_repairable_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fixture(root, GOOD_HOOKS, {"hooks": {}})
            r = hc.check_claude_hook_registration(repo_dir=root)
            self.assertEqual(r["status"], "warn")
            self.assertTrue(
                r.get("_unregistered_hooks"),
                "probe warns about an unregistered hook but exposes no structured "
                "field, so --fix would have to parse the sentence",
            )

    def test_non_repairable_warn_has_no_marker(self):
        """A warn the installer cannot fix must not invite a repair attempt."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _fixture(root, "HOOKS=NOT_AN_ARRAY", {"hooks": {}})
            r = hc.check_claude_hook_registration(repo_dir=root)
            self.assertEqual(r["status"], "warn")
            self.assertIsNone(r.get("_unregistered_hooks"))


class FixHandlerGating(unittest.TestCase):
    """Positive and negative arms, with the positive one proving the negative
    zero is meaningful — a handler that never runs would pass the negative test
    by construction."""

    def _run(self, check, out=None):
        calls = []
        self.kwargs = []
        real = subprocess.run

        def fake(cmd, *a, **k):
            calls.append(cmd)
            self.kwargs.append(k)
            return subprocess.CompletedProcess(cmd, 0, "install-claude-hooks: added=1\n", "")

        real_probe = hc.check_claude_hook_registration
        hc.subprocess.run = fake
        hc.check_claude_hook_registration = lambda *a, **k: {
            "name": "claude-hooks", "status": "ok", "detail": "all owned hooks registered"
        }
        try:
            import io
            hc.apply_claude_hooks_fix([check], stream=out if out is not None else io.StringIO())
        finally:
            # Restore the probe too: leaving it stubbed leaks into every later
            # test here, which happened on this file's first run.
            hc.subprocess.run = real
            hc.check_claude_hook_registration = real_probe
        return calls

    def test_runs_installer_when_hooks_unregistered(self):
        calls = self._run({"name": "claude-hooks", "status": "warn",
                           "detail": "x", "_unregistered_hooks": ["Stop:src/x.sh"]})
        self.assertEqual(len(calls), 1, "the repairable case did not invoke the installer")
        self.assertIn("install-claude-hooks.sh", " ".join(map(str, calls[0])))

    def test_installer_failure_warns_instead_of_raising(self):
        """A failed repair must not take the whole health check down with it."""
        calls = []
        real, real_probe = subprocess.run, hc.check_claude_hook_registration

        def boom(cmd, *a, **k):
            calls.append(cmd)
            raise OSError("bash vanished")

        hc.subprocess.run = boom
        hc.check_claude_hook_registration = lambda *a, **k: {
            "name": "claude-hooks", "status": "warn", "detail": "still broken"
        }
        import io
        buf = io.StringIO()
        try:
            check = {"name": "claude-hooks", "status": "warn",
                     "detail": "x", "_unregistered_hooks": ["Stop:src/x.sh"]}
            hc.apply_claude_hooks_fix([check], stream=buf)
        finally:
            hc.subprocess.run = real
            hc.check_claude_hook_registration = real_probe
        self.assertEqual(len(calls), 1, "the installer was never attempted")
        self.assertIn("could not run", buf.getvalue())
        self.assertIn("bash vanished", buf.getvalue())
        self.assertEqual(check["status"], "warn")

    def test_the_unattended_repair_omits_the_desktop_archiver(self):
        """`--fix` runs on a 30-min Timer in Sutando.app, so it may not install a
        hook that copies transcripts out of the workspace."""
        self._run({"name": "claude-hooks", "status": "warn", "detail": "x",
                   "_unregistered_hooks": ["Stop:src/x.sh", hc._TRANSCRIPT_ARCHIVE_HOOK]})
        env = self.kwargs[0].get("env") or {}
        self.assertEqual(env.get("SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE"), "1",
                         "the installer was invoked without the opt-out")
        self.assertIn("PATH", env, "env was replaced rather than extended")

    def test_archiver_alone_does_not_run_the_installer_at_all(self):
        """Nothing repairable remains, so an unattended pass must do nothing and
        say how to opt in — not install it as a side effect of the other hooks."""
        import io
        buf = io.StringIO()
        calls = self._run({"name": "claude-hooks", "status": "warn", "detail": "x",
                           "_unregistered_hooks": [hc._TRANSCRIPT_ARCHIVE_HOOK]}, out=buf)
        self.assertEqual(calls, [], "installed the ~/Desktop archiver unattended")
        self.assertIn("Opt in with", buf.getvalue())

    def test_control_a_repairable_hook_still_invokes_it(self):
        """Guards the two cases above from passing by never running anything."""
        calls = self._run({"name": "claude-hooks", "status": "warn", "detail": "x",
                           "_unregistered_hooks": ["Stop:src/x.sh"]})
        self.assertEqual(len(calls), 1)

    def test_skips_warn_without_the_marker(self):
        calls = self._run({"name": "claude-hooks", "status": "warn",
                           "detail": "could not parse HOOKS=(...)"})
        self.assertEqual(calls, [], "ran the installer on a warn it cannot repair")


class DispatchWiring(unittest.TestCase):
    def test_fix_dispatch_calls_the_handler(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn(
            "apply_claude_hooks_fix(checks, stream=", src,
            "handler is defined but never dispatched — --fix would be inert",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
