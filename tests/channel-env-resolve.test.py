#!/usr/bin/env python3
"""Contract tests for src/channel_env_resolve.py and its scripts/channel-env.sh
wrapper — which channel env file a caller is told to source.

The caller's contract is `set -a; . "$(bash scripts/channel-env.sh <src>)"; set
+a`, so the returned path is EXECUTED. Two rules therefore gate a candidate,
and each is pinned here at the level that actually failed:

  * containment — the first resolver keyed only on `[ -f "$f" ]`, which follows
    symlinks, so a `channels/<src>/.env` linked outside the tree was returned
    and sourced while channel_env_is_contained() said False for the same path.
  * a non-empty token — the first resolver grepped for `(REMOTE_TASK_TOKEN|
    AG2_REMOTE_TOKEN)=`, key-presence only, so a blank `.env` was selected over
    a sibling holding the real token and the notify still failed.

Both rules are owned elsewhere (channel_env_containment, channel_token); these
cases prove selection DELEGATES rather than approximates, including the
app-support relocation that is the live desktop layout.

Run: python3 tests/channel-env-resolve.test.py
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import channel_env_resolve as m  # noqa: E402

TOKEN_LINE = 'REMOTE_TASK_TOKEN="real-token"\n'


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SUTANDO_APP_SUPPORT")
        os.environ.pop("SUTANDO_APP_SUPPORT", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUTANDO_APP_SUPPORT", None)
        else:
            os.environ["SUTANDO_APP_SUPPORT"] = self._saved

    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _channel(self, source: str = "ag2space") -> tuple[Path, Path]:
        """(channels_dir, channels_dir/<source>), both created."""
        channels = self._tmpdir() / "channels"
        chan = channels / source
        chan.mkdir(parents=True)
        return channels, chan


class TestContainment(_Base):
    def test_symlink_out_of_channels_tree_is_refused(self):
        channels, chan = self._channel()
        outside = self._tmpdir() / "secret.env"
        outside.write_text('REMOTE_TASK_URL="https://evil.example"\n' + TOKEN_LINE)
        (chan / ".env").symlink_to(outside)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))

    def test_plain_file_inside_channels_tree_is_selected(self):
        """Positive control: the refusal above must not be vacuous."""
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_app_support_relocation_is_selected_when_configured(self):
        """The live desktop layout: .env symlinks to $SUTANDO_APP_SUPPORT."""
        channels, chan = self._channel()
        app = self._tmpdir()
        relocated = app / "channels" / "ag2space" / ".env"
        relocated.parent.mkdir(parents=True)
        relocated.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(relocated)
        os.environ["SUTANDO_APP_SUPPORT"] = str(app)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_same_relocation_fails_closed_when_unconfigured(self):
        channels, chan = self._channel()
        app = self._tmpdir()
        relocated = app / "channels" / "ag2space" / ".env"
        relocated.parent.mkdir(parents=True)
        relocated.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(relocated)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))

    def test_link_to_another_channels_file_under_app_root_is_refused(self):
        channels, chan = self._channel()
        app = self._tmpdir()
        other = app / "channels" / "other" / ".env"
        other.parent.mkdir(parents=True)
        other.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(other)
        os.environ["SUTANDO_APP_SUPPORT"] = str(app)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))


class TestNonEmptyToken(_Base):
    def test_blank_env_loses_to_sibling_with_real_token(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("REMOTE_TASK_TOKEN=\n")
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"),
                         chan / "relay-client.env")

    def test_dot_env_still_wins_when_it_holds_a_real_token(self):
        """Precedence is unchanged for a correct layout — only blanks lose."""
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        (chan / "relay-client.env").write_text('REMOTE_TASK_TOKEN="other"\n')
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_quoted_empty_value_does_not_count(self):
        channels, chan = self._channel()
        (chan / ".env").write_text('REMOTE_TASK_TOKEN=""\n')
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"),
                         chan / "relay-client.env")

    def test_legacy_alias_counts(self):
        channels, chan = self._channel()
        (chan / ".env").write_text('AG2_REMOTE_TOKEN="legacy"\n')
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_no_token_anywhere_resolves_to_none(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("MATRIX_USER=x\n")
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))

    def test_missing_channel_dir_resolves_to_none(self):
        channels, _ = self._channel()
        self.assertIsNone(m.resolve_channel_env(channels, "nosuch"))

    def test_sibling_order_is_deterministic(self):
        channels, chan = self._channel()
        (chan / "b-client.env").write_text('REMOTE_TASK_TOKEN="b"\n')
        (chan / "a-client.env").write_text('REMOTE_TASK_TOKEN="a"\n')
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"),
                         chan / "a-client.env")


class TestCli(_Base):
    """`main()` in-process. The wrapper cases below cover the same paths through
    bash, but only as a subprocess — which no coverage run can see."""

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_wrong_arity_is_usage_error(self):
        rc, out, err = self._main(["channel_env_resolve.py", "only-one"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)
        self.assertEqual(out, "")

    def test_missing_channel_dir_exits_one_and_names_the_path(self):
        channels, _ = self._channel()
        rc, out, err = self._main(["x", str(channels), "nosuch"])
        self.assertEqual(rc, 1)
        self.assertIn("no channel dir", err)
        self.assertEqual(out, "")

    def test_no_qualifying_candidate_exits_one_and_names_both_vars(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("REMOTE_TASK_TOKEN=\n")
        rc, out, err = self._main(["x", str(channels), "ag2space"])
        self.assertEqual(rc, 1)
        self.assertIn("REMOTE_TASK_TOKEN", err)
        self.assertIn("AG2_REMOTE_TOKEN", err)
        self.assertEqual(out, "", "stdout must stay empty — the caller sources it")

    def test_success_prints_only_the_path(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        rc, out, err = self._main(["x", str(channels), "ag2space"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(chan / ".env"))
        self.assertEqual(err, "")


class TestShellWrapper(_Base):
    """The wrapper is what callers actually invoke; pin its stdout + exit codes."""

    def _run(self, config_dir: Path, source: str = "ag2space", **env):
        environ = dict(os.environ)
        environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        environ.pop("SUTANDO_APP_SUPPORT", None)
        environ.update(env)
        return subprocess.run(
            ["bash", str(REPO / "scripts" / "channel-env.sh"), source],
            capture_output=True, text=True, env=environ)

    def test_prints_resolved_path_and_exits_zero(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / ".env"))

    def test_escaping_symlink_prints_nothing_on_stdout_and_exits_one(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        outside = self._tmpdir() / "secret.env"
        outside.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(outside)
        r = self._run(cfg)
        self.assertEqual(r.returncode, 1)
        # `. "$(...)"` on empty stdout must not source anything.
        self.assertEqual(r.stdout.strip(), "")

    def test_blank_env_is_skipped_for_the_sibling_holding_the_token(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text("REMOTE_TASK_TOKEN=\n")
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        r = self._run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / "relay-client.env"))

    def test_invalid_source_is_still_rejected_before_any_resolution(self):
        r = self._run(self._tmpdir(), source="../etc")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid source", r.stderr)

    def _unrunnable_python_on_path(self) -> str:
        """A PATH whose python3 cannot run — the shape a Mac without the Xcode
        CLT presents, which is why the wrapper must not shell a bare python3."""
        d = self._tmpdir() / "bin"
        d.mkdir()
        stub = d / "python3"
        stub.write_text('#!/bin/sh\necho "python3: not runnable" >&2\nexit 127\n')
        stub.chmod(0o755)
        return f"{d}:/usr/bin:/bin"

    def test_configured_interpreter_wins_over_an_unrunnable_path_python(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg, PATH=self._unrunnable_python_on_path(),
                      SUTANDO_PY=sys.executable)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / ".env"))

    def test_no_runnable_interpreter_is_loud_and_prints_nothing_on_stdout(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg, PATH=self._unrunnable_python_on_path(),
                      SUTANDO_PY=str(self._tmpdir() / "absent"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertNotEqual(r.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
