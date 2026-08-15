#!/usr/bin/env python3
"""An interrupted or unpublishable handoff must leave the previous snapshot
byte-identical and must not report success.

Runs the script against a synthetic checkout, so the assertions are about what
the filesystem holds -- source-shape checks alone pass under `mv ... || true`.

Run: python3 tests/session-handoff-atomic-write.test.py
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "session-handoff.sh"
PRIOR = "PRIOR SNAPSHOT — must survive a failed run\n"


class Harness(unittest.TestCase):
    """A synthetic checkout + workspace so the real script can be executed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.repo = root / "repo"
        self.ws = root / "ws"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "skills").mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "CLAUDE.md").write_text("# stub\n")
        self.ws.mkdir()
        for name in ("session-handoff.sh", "workspace_resolve.sh",
                     "sutando_config.py", "workspace_default.py"):
            src = REPO / "src" / name
            if src.exists():
                shutil.copy(src, self.repo / "src" / name)
        # Workspace resolution shells out to scripts/sutando-config.sh -> that
        # chain must be present or the run dies long before the publish step and
        # every assertion below passes for the wrong reason.
        (self.repo / "scripts").mkdir(exist_ok=True)
        for name in ("sutando-config.sh", "python-binary.sh"):
            src = REPO / "scripts" / name
            if src.exists():
                shutil.copy(src, self.repo / "scripts" / name)
        # Resolve to our sandbox workspace rather than the real one.
        (self.repo / "sutando.config.local.json").write_text(
            '{"workspace": {"path": "%s"}}\n' % self.ws
        )
        self.state = self.ws / "session-state.md"
        self.state.write_text(PRIOR)

    def tearDown(self):
        self._tmp.cleanup()

    #: Every exit from the publish step emits exactly one of these.
    PUBLISH_MARKERS = ("Session state saved", "publish failed", "capture incomplete")

    def assert_reached_publish(self, result):
        """Positive marker, never absence-of-known-failures: a negative list
        cannot enumerate every early exit, and one slipped through."""
        blob = (result.stdout or "") + (result.stderr or "")
        self.assertTrue(
            any(m in blob for m in self.PUBLISH_MARKERS),
            "harness never reached the publish step (no publish marker in "
            f"output) — this assertion would pass vacuously. Got: {blob[:300]!r}",
        )

    def run_handoff(self, env_extra=None, timeout=180):
        env = dict(os.environ)
        env["SUTANDO_REPO_DIR"] = str(self.repo)
        env.pop("SUTANDO_TEAM_RUNTIME", None)
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(self.repo / "src" / "session-handoff.sh")],
            capture_output=True, text=True, env=env, timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=str(self.repo),
        )


class TestPublishBehaviour(Harness):
    def test_failed_rename_keeps_the_snapshot_and_reports_failure(self):
        """A stub `mv` on PATH: a directory at the destination does not fail
        the rename (mv moves the file inside it), and chflags did not either."""
        bin_dir = pathlib.Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "mv"
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
        r = self.run_handoff(env_extra={"PATH": f"{bin_dir}:{os.environ.get('PATH','')}"})
        self.assert_reached_publish(r)
        self.assertNotEqual(r.returncode, 0, "a failed publish must exit non-zero")
        self.assertNotIn("Session state saved", r.stdout,
                         "a failed publish must not print the success line")
        self.assertEqual(self.state.read_text(), PRIOR,
                         "the previous snapshot must be byte-identical")
        self.assertNotIn("RELAY", r.stdout.upper().replace("RELAY NOTES", ""),
                         "relay retirement must not run after a failed publish")

    def test_interrupted_capture_leaves_the_previous_snapshot_intact(self):
        """Kill the run mid-capture; the old snapshot must be byte-identical."""
        env = {"PATH": os.environ.get("PATH", "")}
        proc = subprocess.Popen(
            ["bash", str(self.repo / "src" / "session-handoff.sh")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=str(self.repo),
            env={**os.environ, "SUTANDO_REPO_DIR": str(self.repo), **env},
        )
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        self.assertEqual(self.state.read_text(), PRIOR,
                         "an interrupted run must not touch the destination")

    def test_a_stage_is_used_not_the_destination(self):
        """Whatever the run does, it must not write the destination in place."""
        src = SCRIPT.read_text()
        self.assertNotIn('} > "$STATE_FILE"', src,
                         "capture must not redirect at the destination")
        self.assertIn('} > "$STATE_TMP"', src)

    def test_success_line_is_downstream_of_the_rename(self):
        """The message must be unreachable unless mv returned 0."""
        src = SCRIPT.read_text()
        tail = src.split('} > "$STATE_TMP"')[-1]
        mv_at = tail.find('mv "$STATE_TMP" "$STATE_FILE"')
        ok_at = tail.find("Session state saved")
        self.assertNotEqual(mv_at, -1, "publish must be a rename")
        self.assertNotEqual(ok_at, -1)
        self.assertLess(mv_at, ok_at,
                        "the success line must come after the rename, not before")
        self.assertRegex(tail[:ok_at], r'if\s+!\s+mv "\$STATE_TMP"',
                         "the rename's exit status must gate the success line")

    def test_relay_retirement_cannot_run_after_a_failed_publish(self):
        """Both failure paths exit before the relay block is reached."""
        src = SCRIPT.read_text()
        tail = src.split('} > "$STATE_TMP"')[-1]
        relay_at = tail.find("RELAY_PROCESSED")
        self.assertNotEqual(relay_at, -1)
        self.assertEqual(tail[:relay_at].count("exit 1"), 2,
                         "both the incomplete-capture and failed-publish paths "
                         "must exit before relay retirement")

    def test_script_is_syntactically_valid(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
