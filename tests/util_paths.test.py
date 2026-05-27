"""Tests for util_paths.py — personal_path, shared_personal_path, claude_home_path."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import util_paths


class TestPersonalPath(unittest.TestCase):
    def test_memory_dir_takes_precedence(self):
        with tempfile.TemporaryDirectory() as memory_root, \
                tempfile.TemporaryDirectory() as ws:
            host = __import__("socket").gethostname().split(".")[0]
            machine_dir = Path(memory_root) / f"machine-{host}"
            machine_dir.mkdir()
            (machine_dir / "stand-identity.json").write_text("{}")
            env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PRIVATE_DIR"}
            env["SUTANDO_MEMORY_DIR"] = memory_root
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("stand-identity.json", Path(ws))
        self.assertEqual(p, machine_dir / "stand-identity.json")

    def test_falls_back_to_workspace_when_no_memory_dir(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "stand-identity.json").write_text("{}")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("stand-identity.json", Path(ws))
        self.assertEqual(p, Path(ws) / "stand-identity.json")

    def test_avatar_tries_assets_dir_first(self):
        with tempfile.TemporaryDirectory() as ws:
            assets = Path(ws) / "assets"
            assets.mkdir()
            (assets / "stand-avatar.png").write_bytes(b"png")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("stand-avatar.png", Path(ws))
        self.assertEqual(p, assets / "stand-avatar.png")

    def test_avatar_falls_back_to_workspace_root_when_no_assets(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "stand-avatar.png").write_bytes(b"png")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("stand-avatar.png", Path(ws))
        self.assertEqual(p, Path(ws) / "stand-avatar.png")

    def test_returns_preferred_private_path_when_nothing_exists(self):
        with tempfile.TemporaryDirectory() as memory_root, \
                tempfile.TemporaryDirectory() as ws:
            host = __import__("socket").gethostname().split(".")[0]
            env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PRIVATE_DIR"}
            env["SUTANDO_MEMORY_DIR"] = memory_root
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("nonexistent.json", Path(ws))
        self.assertEqual(p, Path(memory_root) / f"machine-{host}" / "nonexistent.json")

    def test_returns_workspace_path_when_no_memory_dir_and_nothing_exists(self):
        with tempfile.TemporaryDirectory() as ws:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("nonexistent.json", Path(ws))
        self.assertEqual(p, Path(ws) / "nonexistent.json")

    def test_avatar_preferred_path_is_assets_when_nothing_exists(self):
        with tempfile.TemporaryDirectory() as ws:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.personal_path("stand-avatar.png", Path(ws))
        self.assertEqual(p, Path(ws) / "assets" / "stand-avatar.png")

    def test_legacy_private_dir_honored_with_deprecation(self):
        with tempfile.TemporaryDirectory() as private_root, \
                tempfile.TemporaryDirectory() as ws:
            host = __import__("socket").gethostname().split(".")[0]
            machine_dir = Path(private_root) / f"machine-{host}"
            machine_dir.mkdir()
            (machine_dir / "stand-identity.json").write_text("{}")
            env = {k: v for k, v in os.environ.items() if k != "SUTANDO_MEMORY_DIR"}
            env["SUTANDO_PRIVATE_DIR"] = private_root
            buf = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch("sys.stderr", buf):
                p = util_paths.personal_path("stand-identity.json", Path(ws))
        self.assertEqual(p, machine_dir / "stand-identity.json")
        self.assertIn("DEPRECATION", buf.getvalue())


class TestSharedPersonalPath(unittest.TestCase):
    def test_memory_dir_takes_precedence(self):
        with tempfile.TemporaryDirectory() as memory_root, \
                tempfile.TemporaryDirectory() as ws:
            (Path(memory_root) / "notes").mkdir()
            env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PRIVATE_DIR"}
            env["SUTANDO_MEMORY_DIR"] = memory_root
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.shared_personal_path("notes", Path(ws))
        self.assertEqual(p, Path(memory_root) / "notes")

    def test_falls_back_to_workspace_when_private_missing(self):
        with tempfile.TemporaryDirectory() as memory_root, \
                tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "notes").mkdir()
            env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PRIVATE_DIR"}
            env["SUTANDO_MEMORY_DIR"] = memory_root
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.shared_personal_path("notes", Path(ws))
        self.assertEqual(p, Path(ws) / "notes")

    def test_returns_workspace_path_when_no_memory_dir(self):
        with tempfile.TemporaryDirectory() as ws:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.shared_personal_path("notes", Path(ws))
        self.assertEqual(p, Path(ws) / "notes")

    def test_returns_private_preferred_when_nothing_exists(self):
        with tempfile.TemporaryDirectory() as memory_root, \
                tempfile.TemporaryDirectory() as ws:
            env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PRIVATE_DIR"}
            env["SUTANDO_MEMORY_DIR"] = memory_root
            with patch.dict(os.environ, env, clear=True):
                p = util_paths.shared_personal_path("notes", Path(ws))
        self.assertEqual(p, Path(memory_root) / "notes")

    def test_expands_tilde_in_memory_dir(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
        env["SUTANDO_MEMORY_DIR"] = "~/fake-memory"
        with patch.dict(os.environ, env, clear=True):
            p = util_paths.shared_personal_path("notes")
        self.assertNotIn("~", str(p))


class TestClaudeHomePath(unittest.TestCase):
    def test_returns_claude_home_env_when_set(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"CLAUDE_HOME": d}, clear=False):
                p = util_paths.claude_home_path()
        self.assertEqual(p, Path(d))

    def test_returns_home_dot_claude_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_HOME"}
        with patch.dict(os.environ, env, clear=True):
            p = util_paths.claude_home_path()
        self.assertEqual(p, Path.home() / ".claude")

    def test_joins_subpath_components(self):
        with patch.dict(os.environ, {"CLAUDE_HOME": "/tmp/claude"}, clear=False):
            p = util_paths.claude_home_path("channels", "discord", "access.json")
        self.assertEqual(p, Path("/tmp/claude/channels/discord/access.json"))

    def test_expands_tilde_in_claude_home(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_HOME"}
        env["CLAUDE_HOME"] = "~/fake-claude"
        with patch.dict(os.environ, env, clear=True):
            p = util_paths.claude_home_path()
        self.assertNotIn("~", str(p))
        self.assertTrue(str(p).startswith(str(Path.home())))


if __name__ == "__main__":
    unittest.main()
