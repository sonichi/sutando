#!/usr/bin/env python3
"""Contract tests for src/channel_env_containment.py — the single shared owner
of "is this channels/<source>/.env somewhere we trust?".

Before this module existed, the same ~8-line rule was pasted byte-identical
into skills/task-progress/scripts/notify.py (_channel_env_is_contained, the
sender) and src/core-supervisor-relay.py (_is_deliverable, the probe) behind a
"widen BOTH together or never" comment — the exact duplication CLAUDE.md's
architecture rules call out as the defect, not a tidiness issue, and #2701
already shipped one sender/probe mismatch from hand-syncing it. This file
pins the shared function's accept/refuse behavior directly; each caller's own
delegation test (in tests/task-progress-skill.test.py and
tests/core-supervisor-relay.test.py) then only has to prove it calls through
here rather than re-verifying every case.

Cases below are the union of what TestChannelEnvContainment (task-progress-
skill.test.py) and TestResolveActiveTarget's containment-specific cases
(core-supervisor-relay.test.py) already covered — none invented, none dropped.

Run: python3 tests/channel-env-containment-contract.test.py
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import channel_env_containment as m  # noqa: E402


class TestChannelEnvIsContained(unittest.TestCase):
    def setUp(self):
        self._saved_app_support = os.environ.get("SUTANDO_APP_SUPPORT")
        os.environ.pop("SUTANDO_APP_SUPPORT", None)

    def tearDown(self):
        if self._saved_app_support is None:
            os.environ.pop("SUTANDO_APP_SUPPORT", None)
        else:
            os.environ["SUTANDO_APP_SUPPORT"] = self._saved_app_support

    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def test_plain_file_inside_channels_dir_is_contained(self):
        root = self._tmpdir()
        channels_dir = root / "channels"
        env_path = channels_dir / "ag2space" / ".env"
        env_path.parent.mkdir(parents=True)
        env_path.write_text("REMOTE_TASK_TOKEN=x\n")
        self.assertTrue(m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_symlinked_channels_dir_is_still_contained(self):
        # A symlinked channels/ dir itself is fine; what matters is where the
        # RESOLVED entry lands, not whether channels_dir is itself a link.
        real_root = self._tmpdir()
        (real_root / "ag2space").mkdir()
        (real_root / "ag2space" / ".env").write_text("REMOTE_TASK_TOKEN=x\n")
        root = self._tmpdir()
        channels_dir = root / "channels"
        channels_dir.symlink_to(real_root)
        env_path = channels_dir / "ag2space" / ".env"
        self.assertTrue(m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_exact_relocation_under_app_support_is_contained(self):
        cfg = self._tmpdir()
        app = self._tmpdir()
        real_dir = app / "channels" / "ag2space"
        real_dir.mkdir(parents=True)
        real = real_dir / ".env"
        real.write_text("REMOTE_TASK_TOKEN=x\n")
        channels_dir = cfg / "channels"
        link_dir = channels_dir / "ag2space"
        link_dir.mkdir(parents=True)
        env_path = link_dir / ".env"
        env_path.symlink_to(real)
        os.environ["SUTANDO_APP_SUPPORT"] = str(app)
        self.assertTrue(m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_relocation_refused_when_app_support_unset(self):
        cfg = self._tmpdir()
        app = self._tmpdir()
        real_dir = app / "channels" / "ag2space"
        real_dir.mkdir(parents=True)
        real = real_dir / ".env"
        real.write_text("REMOTE_TASK_TOKEN=x\n")
        channels_dir = cfg / "channels"
        link_dir = channels_dir / "ag2space"
        link_dir.mkdir(parents=True)
        env_path = link_dir / ".env"
        env_path.symlink_to(real)
        # SUTANDO_APP_SUPPORT left unset — no approved second root.
        self.assertFalse(m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_relocation_refused_under_a_different_root(self):
        cfg = self._tmpdir()
        app = self._tmpdir()
        other = self._tmpdir()
        real_dir = app / "channels" / "ag2space"
        real_dir.mkdir(parents=True)
        real = real_dir / ".env"
        real.write_text("REMOTE_TASK_TOKEN=x\n")
        channels_dir = cfg / "channels"
        link_dir = channels_dir / "ag2space"
        link_dir.mkdir(parents=True)
        env_path = link_dir / ".env"
        env_path.symlink_to(real)
        os.environ["SUTANDO_APP_SUPPORT"] = str(other)
        self.assertFalse(m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_off_exact_path_under_app_support_is_refused(self):
        # Same approved root, but not the exact <source>/.env under it: another
        # channel's dir, outside channels/ entirely, or a differently named file.
        cases = {
            "other channel": ("discord", "channels", ".env"),
            "outside channels/": ("ag2space", "workspace", ".env"),
            "wrong filename": ("ag2space", "channels", "secrets.txt"),
        }
        for label, (dirname, subdir, filename) in cases.items():
            with self.subTest(label):
                cfg = self._tmpdir()
                app = self._tmpdir()
                real_dir = app / subdir / dirname
                real_dir.mkdir(parents=True)
                real = real_dir / filename
                real.write_text("REMOTE_TASK_TOKEN=x\n")
                channels_dir = cfg / "channels"
                link_dir = channels_dir / "ag2space"
                link_dir.mkdir(parents=True)
                env_path = link_dir / ".env"
                env_path.symlink_to(real)
                os.environ["SUTANDO_APP_SUPPORT"] = str(app)
                self.assertFalse(
                    m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_arbitrary_escape_with_no_app_support_at_all_is_refused(self):
        cfg = self._tmpdir()
        outside = self._tmpdir()
        real = outside / ".env"
        real.write_text("REMOTE_TASK_TOKEN=x\n")
        channels_dir = cfg / "channels"
        link_dir = channels_dir / "ag2space"
        link_dir.mkdir(parents=True)
        env_path = link_dir / ".env"
        env_path.symlink_to(real)
        # No SUTANDO_APP_SUPPORT set at all — plain traversal-style escape.
        self.assertFalse(m.channel_env_is_contained(env_path, channels_dir, "ag2space"))

    def test_accepts_str_paths_same_as_path_objects(self):
        # Callers pass either str (core-supervisor-relay.py, via os.path.join)
        # or Path (notify.py) — the function must not care.
        root = self._tmpdir()
        channels_dir = root / "channels"
        env_path = channels_dir / "ag2space" / ".env"
        env_path.parent.mkdir(parents=True)
        env_path.write_text("REMOTE_TASK_TOKEN=x\n")
        self.assertTrue(
            m.channel_env_is_contained(str(env_path), str(channels_dir), "ag2space"))


if __name__ == "__main__":
    unittest.main()
