#!/usr/bin/env python3
"""Publish replaces the snapshot only on success; any other exit leaves the
previous one byte-identical. Run: python3 tests/session-handoff-atomic-write.test.py
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "session-handoff.sh"
PRIOR = "PRIOR SNAPSHOT — must survive a failed run\n"
MARKER = "<!-- session-handoff: capture complete -->"


class Harness(unittest.TestCase):
    """Synthetic checkout + workspace so the real script can be executed."""

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
        # Without this chain the run dies before publish and every assertion
        # below passes for the wrong reason.
        (self.repo / "scripts").mkdir(exist_ok=True)
        for name in ("sutando-config.sh", "python-binary.sh"):
            src = REPO / "scripts" / name
            if src.exists():
                shutil.copy(src, self.repo / "scripts" / name)
        # _find_repo_root() anchors on sutando.config.json, not the .local
        # override: writing only .local leaves root=None and resolves to $HOME.
        cfg = '{"workspace": {"path": "%s"}}\n' % self.ws
        (self.repo / "sutando.config.json").write_text(cfg)
        (self.repo / "sutando.config.local.json").write_text(cfg)
        self.state = self.ws / "session-state.md"
        self.state.write_text(PRIOR)

    def tearDown(self):
        self._tmp.cleanup()

    #: Every exit from the publish step emits exactly one of these.
    PUBLISH_MARKERS = ("Session state saved", "publish failed", "capture incomplete")

    def assert_reached_publish(self, result):
        # Positive marker, not absence-of-known-failures: a negative list cannot
        # enumerate every early exit, and one slipped through.
        blob = (result.stdout or "") + (result.stderr or "")
        self.assertTrue(
            any(m in blob for m in self.PUBLISH_MARKERS),
            "harness never reached the publish step (no publish marker in "
            f"output) — this assertion would pass vacuously. Got: {blob[:300]!r}",
        )

    def assert_isolated(self):
        """A tmpdir is not isolation until the resolved path is checked — this
        suite previously ran against ~/sutando-workspace."""
        out = subprocess.run(
            ["bash", str(self.repo / "scripts" / "sutando-config.sh"), "workspace"],
            capture_output=True, text=True, cwd=str(self.repo),
            env={**os.environ, "SUTANDO_REPO_DIR": str(self.repo)},
        ).stdout.strip()
        self.assertTrue(
            out and pathlib.Path(out).resolve() == self.ws.resolve(),
            f"harness is NOT isolated: script resolves workspace to {out!r}, "
            f"expected {self.ws}. Every assertion below would watch a file "
            f"nothing writes.",
        )

    def stages(self):
        """Staging files the script creates beside the destination."""
        return sorted(self.ws.glob("session-state.md.tmp.*"))

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
    def test_successful_publish_replaces_the_prior_snapshot(self):
        # The only case proving the harness can publish at all; without it the
        # failure assertions can hold vacuously.
        self.assert_isolated()
        r = self.run_handoff()
        self.assert_reached_publish(r)
        self.assertEqual(r.returncode, 0, f"expected success, got {r.returncode}: {r.stderr[:400]!r}")
        self.assertIn("Session state saved", r.stdout)
        body = self.state.read_text()
        self.assertNotEqual(body, PRIOR, "a successful publish must replace the previous snapshot")
        self.assertIn("## Recent Conversation", body,
                      "the published snapshot must be the real capture, not a stub")
        self.assertTrue(body.rstrip().endswith(MARKER),
                        "the terminal marker must be the last line of a published snapshot")
        self.assertEqual(self.stages(), [], "a successful publish must leave no staging file behind")

    def test_failed_rename_keeps_the_snapshot_and_reports_failure(self):
        # A stub `mv` on PATH: a directory at the destination does not fail the
        # rename (mv moves the file inside it), and chflags did not either.
        self.assert_isolated()
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
        # Synchronise on the staging file: a bare wait(timeout) passes when the
        # script dies early for an unrelated reason, interrupting nothing.
        self.assert_isolated()
        proc = subprocess.Popen(
            ["bash", str(self.repo / "src" / "session-handoff.sh")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=str(self.repo),
            env={**os.environ, "SUTANDO_REPO_DIR": str(self.repo)},
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.stages():
                    break
                if proc.poll() is not None:
                    self.fail(
                        "script exited before creating a staging file — nothing "
                        f"was interrupted (rc={proc.returncode})")
                time.sleep(0.02)
            else:
                self.fail("no staging file appeared within 30s — capture never started")

            self.assertIsNone(proc.poll(), "process must still be capturing when killed")
            proc.kill()
        finally:
            proc.communicate(timeout=10)

        self.assertEqual(self.state.read_text(), PRIOR,
                         "an interrupted run must not touch the destination")

    def test_kill_in_the_tail_window_does_not_publish(self):
        # Green under both gates: a killed run never reaches the gate at all.
        # test_the_gate_is_the_last_line_not_a_section is the discriminator.
        self.assert_isolated()
        proc = subprocess.Popen(
            ["bash", str(self.repo / "src" / "session-handoff.sh")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=str(self.repo),
            env={**os.environ, "SUTANDO_REPO_DIR": str(self.repo)},
        )
        try:
            deadline = time.monotonic() + 60
            killed_in_window = False
            while time.monotonic() < deadline:
                stage = self.stages()
                if stage:
                    text = stage[0].read_text(errors="replace")
                    if "## Recent Conversation" in text and MARKER not in text:
                        proc.kill()
                        killed_in_window = True
                        break
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(
                killed_in_window,
                "never observed a stage past the old gate and short of the marker "
                "— the window this test exists for was not exercised")
        finally:
            proc.communicate(timeout=10)

        self.assertEqual(
            self.state.read_text(), PRIOR,
            "a run killed in the tail window must not publish")

    def test_the_gate_is_the_last_line_not_a_section(self):
        # A section gate pins a token, not a position: any section added after
        # it narrows the gate with nothing to catch the regression.
        src = SCRIPT.read_text()
        self.assertIn('tail -n 1 "$STATE_TMP"', src,
                      "the publish gate must test the LAST line of the stage")
        self.assertNotIn(
            "grep -q '^## Recent Conversation' " + chr(34) + "$STATE_TMP" + chr(34), src,
            "the section-token gate must not be the publish gate")
        body = src.split('} > "$STATE_TMP"')[0]
        self.assertLess(body.rfind("## Relay Notes"), body.rfind("CAPTURE_END_MARKER"),
                        "the marker must be emitted after every section")

    def test_a_stage_is_used_not_the_destination(self):
        src = SCRIPT.read_text()
        self.assertNotIn('} > "$STATE_FILE"', src,
                         "capture must not redirect at the destination")
        self.assertIn('} > "$STATE_TMP"', src)

    def test_success_line_is_downstream_of_the_rename(self):
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
