#!/usr/bin/env python3
"""src/codex_home.py is the one owner of the Codex core's home, and every launcher delegates.

The contract: resolve one effective home, create it only after an explicit ok / no-host on a
desktop host, refuse (creating and writing nothing) on anything else, and keep a
non-desktop checkout exactly as before.

Run: python3 tests/codex-home.test.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import codex_home  # noqa: E402

COPIED = ("scripts/sutando-config.sh", "scripts/python-binary.sh", "src/sutando_config.py",
          "src/codex_home.py", "src/codex_host_instructions.py",
          "src/agent/codex/codex-home.sh", "src/startup-runtime.sh", "src/repo_root.sh",
          "src/watcher_sentinel.sh", "skills/install.sh", "src/skill_audience.py",
          "sutando.config.json")


class Layout(unittest.TestCase):
    """A checkout, desktop-hosted or not, with the real config scripts and a temp HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.owner = self.home / ".codex"

    def tearDown(self):
        self.tmp.cleanup()

    def checkout(self, desktop=True, env_name="CODEX_HOME"):
        app = self.root / "app-support"
        self.engine = app / "engine" if desktop else self.root / "code"
        repo = self.engine / "sutando"
        for rel in COPIED:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, repo / rel)
        (repo / "src" / "__init__.py").touch()
        skill = repo / "skills" / "tutorial"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: tutorial\n---\n")
        if desktop:
            (self.engine / "AGENTS.md").write_text("host\n")
        self.workspace = (app if desktop else self.root) / "workspace"
        self.workspace.mkdir(parents=True)
        self.app_home = self.workspace / ".codex-sutando"
        config = json.loads((repo / "sutando.config.json").read_text())
        config["workspace"] = {"path": str(self.workspace)}
        config["core"] = {"runtime": "codex"}
        config["core_config_dirs"] = [
            {"id": "claude", "type": "claude", "env_name": "CLAUDE_CONFIG_DIR", "synced": False,
             "value": str(self.workspace / ".claude-sutando")},
            {"id": "codex", "type": "codex", "env_name": env_name, "synced": False,
             "value": str(self.app_home)}]
        (repo / "sutando.config.json").write_text(json.dumps(config))
        self.repo = repo
        return repo

    def tree(self):
        found = []
        for top, dirs, files in os.walk(self.root):
            for name in dirs + files:
                found.append(os.path.relpath(os.path.join(top, name), self.root))
        return sorted(p for p in found if "__pycache__" not in p)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SUTANDO_", "CODEX_", "CLAUDE_"))}
        env.update({"HOME": str(self.home), "SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER": "1"})
        env.update(extra)
        return env

    def bash(self, script, **extra):
        return subprocess.run(["/bin/bash", "-c", script], env=self.env(**extra), cwd=self.root,
                              capture_output=True, text=True, timeout=120)

    def prepare(self, home=None, environ=None):
        with patch.dict(sys.modules):
            sys.modules.pop("src.sutando_config", None)
            sys.modules.pop("src", None)
            return codex_home.prepare(self.repo, home or self.home, environ or {})


class Contract(Layout):
    def test_a_desktop_app_home_is_checked_then_created_and_is_the_one_home(self):
        self.checkout()
        self.assertEqual(self.prepare(), ("CODEX_HOME", str(self.app_home), str(self.app_home)))
        self.assertTrue(self.app_home.is_dir())

    def test_a_desktop_owner_link_to_the_missing_app_home_is_refused_creating_nothing(self):
        self.checkout()
        self.owner.symlink_to(self.app_home)
        before = self.tree()
        with self.assertRaisesRegex(codex_home.Refused, "owner's own Codex home"):
            self.prepare()
        self.assertEqual(self.tree(), before)

    def test_a_desktop_invalid_home_is_refused(self):
        self.checkout()
        with patch.object(codex_home.codex_host_instructions, "check", return_value="invalid-home"):
            with self.assertRaisesRegex(codex_home.Refused, "symlink or non-directory"):
                self.prepare()
        self.assertFalse(self.app_home.exists())

    def test_a_failing_or_unknown_check_refuses_creating_nothing(self):
        self.checkout()
        for name, effect in (("raises", RuntimeError("boom")), ("empty", ""), ("unknown", "maybe")):
            with self.subTest(check=name):
                kwargs = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
                with patch.object(codex_home.codex_host_instructions, "check", **kwargs):
                    with self.assertRaisesRegex(codex_home.Refused, "could not be checked"):
                        self.prepare()
                self.assertFalse(self.app_home.exists())

    def test_a_desktop_alternate_env_name_is_refused_before_any_write(self):
        self.checkout(env_name="CODEX_CONFIG_DIR")
        before = self.tree()
        with self.assertRaisesRegex(codex_home.Refused, "not a CODEX_HOME"):
            self.prepare()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.owner.exists())

    def test_a_newline_in_the_configured_home_is_refused(self):
        self.checkout()
        with patch.object(codex_home, "configured", return_value=("CODEX_HOME", "/a\nb")):
            with self.assertRaisesRegex(codex_home.Refused, "newline"):
                self.prepare()

    def test_no_codex_entry_is_refused_on_a_desktop(self):
        self.checkout()
        with patch.object(codex_home, "configured", return_value=("", "")):
            with self.assertRaises(codex_home.Refused):
                self.prepare()

    def test_a_non_desktop_checkout_is_unchanged(self):
        self.checkout(desktop=False)
        self.assertEqual(self.prepare(), ("CODEX_HOME", str(self.app_home), str(self.app_home)))
        self.assertTrue(self.app_home.is_dir())

    def test_a_non_desktop_alternate_env_name_uses_the_home_codex_reads(self):
        self.checkout(desktop=False, env_name="CODEX_CONFIG_DIR")
        env, value, path = self.prepare(environ={})
        self.assertEqual((env, value, path), ("CODEX_CONFIG_DIR", str(self.app_home),
                                              str(self.home / ".codex")))
        _, _, path = self.prepare(environ={"CODEX_HOME": "/elsewhere"})
        self.assertEqual(path, "/elsewhere")

    def test_a_non_desktop_checkout_without_a_codex_entry_keeps_codex_defaults(self):
        self.checkout(desktop=False)
        with patch.object(codex_home, "configured", return_value=("", "")):
            self.assertEqual(self.prepare(environ={}), ("", "", str(self.home / ".codex")))

    def test_a_config_without_a_codex_entry_reads_as_unconfigured(self):
        self.checkout(desktop=False)
        config = json.loads((self.repo / "sutando.config.json").read_text())
        config["core_config_dirs"] = config["core_config_dirs"][:1]
        (self.repo / "sutando.config.json").write_text(json.dumps(config))
        self.assertEqual(self.prepare(environ={}), ("", "", str(self.home / ".codex")))

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Path, "home", return_value=self.home), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = codex_home.main([str(a) for a in argv])
        return rc, out.getvalue().split("\n"), err.getvalue()

    def test_cli_prints_env_value_and_path_and_exit_codes(self):
        self.checkout()
        rc, lines, _ = self.main("prepare", self.repo)
        self.assertEqual((rc, lines[:3]), (0, ["CODEX_HOME", str(self.app_home), str(self.app_home)]))
        shutil.rmtree(self.app_home)
        self.owner.symlink_to(self.app_home)
        rc, lines, err = self.main("prepare", self.repo)
        self.assertEqual((rc, lines[:3]), (3, ["CODEX_HOME", str(self.app_home), ""]))
        self.assertIn("owner's own Codex home", err)
        with patch.object(codex_home, "configured", side_effect=RuntimeError("bad config")):
            rc, _, err = self.main("prepare", self.repo)
        self.assertEqual(rc, 1)
        self.assertIn("bad config", err)

    def test_cli_on_a_refusal_with_unreadable_config_prints_empty_lines(self):
        self.checkout()
        calls = []

        def first_then_broken(repo):
            calls.append(repo)
            if len(calls) > 1:
                raise RuntimeError("gone")
            return "CODEX_HOME", "relative"
        with patch.object(codex_home, "configured", first_then_broken):
            rc, lines, _ = self.main("prepare", self.repo)
        self.assertEqual((rc, lines[:3]), (3, ["", "", ""]))


class ShellAdapter(Layout):
    SNIPPET = ('. "$R/src/agent/codex/codex-home.sh"; sutando_codex_home_prepare "$R" $PYARG; '
               'echo "rc=$? home=[$SUTANDO_CODEX_HOME] refused=[$SUTANDO_CODEX_HOME_REFUSED] '
               'CODEX_HOME=[${CODEX_HOME:-}]"')

    def run_adapter(self, pyarg="", **extra):
        return self.bash(self.SNIPPET, R=str(self.repo), PYARG=pyarg, **extra)

    def test_a_desktop_home_is_prepared_through_the_owner(self):
        self.checkout()
        out = self.run_adapter()
        self.assertIn(f"rc=0 home=[{self.app_home}] refused=[] CODEX_HOME=[{self.app_home}]",
                      out.stdout, out.stderr)
        self.assertTrue(self.app_home.is_dir())

    def test_a_refused_desktop_home_warns_and_creates_nothing(self):
        self.checkout()
        self.owner.symlink_to(self.app_home)
        before = self.tree()
        out = self.run_adapter()
        self.assertIn("rc=0 home=[] refused=[1]", out.stdout)
        self.assertIn("not creating it", out.stderr)
        self.assertEqual(self.tree(), before)

    def test_a_failing_checker_or_no_python_refuses_on_a_desktop(self):
        self.checkout()
        broken = self.root / "broken-python"
        broken.write_text("#!/bin/sh\nexit 7\n")
        broken.chmod(0o755)
        silent = self.root / "silent-python"
        silent.write_text("#!/bin/sh\nexit 0\n")
        silent.chmod(0o755)
        before = self.tree()
        no_python = {"SUTANDO_PY": str(self.root / "none"), "PATH": "/nonexistent"}
        for name, pyarg, extra in (("erroring", str(broken), {}), ("no-output", str(silent), {}),
                                   ("no-python", "", no_python)):
            with self.subTest(python=name):
                out = self.run_adapter(pyarg=pyarg, **extra)
                self.assertIn("home=[] refused=[1]", out.stdout, out.stderr)
                self.assertIn("not creating it", out.stderr)
                self.assertFalse(os.path.lexists(self.app_home))
        self.assertEqual(self.tree(), before)

    def test_a_non_desktop_checkout_without_python_is_unchanged(self):
        self.checkout(desktop=False)
        out = self.run_adapter(SUTANDO_PY=str(self.root / "none"), PATH="/nonexistent")
        self.assertIn(f"rc=0 home=[{self.home / '.codex'}] refused=[]", out.stdout, out.stderr)
        self.assertNotIn("not creating it", out.stderr)

    def test_a_non_desktop_unreadable_config_returns_1_as_before(self):
        self.checkout(desktop=False)
        broken = self.root / "broken-python"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        out = self.run_adapter(pyarg=str(broken))
        self.assertIn(f"rc=1 home=[{self.home / '.codex'}] refused=[]", out.stdout, out.stderr)
        self.assertIn("could not resolve the configured Codex home", out.stderr)


class StartupSteps(Layout):
    """startup.sh's steps that touch CODEX_HOME before it execs the launcher."""

    def test_the_auth_preflight_with_an_owner_link_leaves_the_tree_unchanged(self):
        self.checkout()
        self.owner.symlink_to(self.app_home)
        before = self.tree()
        out = self.bash('. "$R/src/startup-runtime.sh"; preflight_selected_core_auth codex; '
                        'echo "rc=$?"', R=str(self.repo), SUTANDO_SKIP_AUTH_PREFLIGHT="1")
        self.assertIn("rc=0", out.stdout, out.stderr)
        self.assertIn("not creating it", out.stderr)
        self.assertEqual(self.tree(), before)

    def test_the_auth_preflight_still_prepares_a_normal_desktop_home(self):
        self.checkout()
        out = self.bash('. "$R/src/startup-runtime.sh"; preflight_selected_core_auth codex; '
                        'echo "rc=$? CODEX_HOME=$CODEX_HOME"', R=str(self.repo),
                        SUTANDO_SKIP_AUTH_PREFLIGHT="1")
        self.assertIn(f"rc=0 CODEX_HOME={self.app_home}", out.stdout, out.stderr)
        self.assertTrue(self.app_home.is_dir())

    def test_the_auth_preflight_reports_an_unreadable_config(self):
        self.checkout(desktop=False)
        (self.repo / "sutando.config.json").write_text("{not json")
        out = self.bash('. "$R/src/startup-runtime.sh"; preflight_selected_core_auth codex; '
                        'echo "rc=$?"', R=str(self.repo), SUTANDO_SKIP_AUTH_PREFLIGHT="1")
        self.assertIn("rc=1", out.stdout, out.stderr)
        self.assertIn("could not resolve the Codex config directory", out.stderr)

    def test_skills_install_with_an_owner_link_links_nothing_into_codex(self):
        self.checkout()
        self.owner.symlink_to(self.app_home)
        out = self.bash('bash "$R/skills/install.sh"', R=str(self.repo),
                        CLAUDE_CONFIG_DIR=str(self.workspace / ".claude-sutando"),
                        SUTANDO_CORE_RUNTIME="codex")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("the Codex home was refused; no skills linked there", out.stdout)
        self.assertFalse(os.path.lexists(self.app_home))

    def test_skills_install_on_a_normal_desktop_links_into_the_app_home(self):
        self.checkout()
        out = self.bash('bash "$R/skills/install.sh"', R=str(self.repo),
                        CLAUDE_CONFIG_DIR=str(self.workspace / ".claude-sutando"),
                        SUTANDO_CORE_RUNTIME="codex")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((self.app_home / "skills" / "tutorial").is_symlink(), out.stdout)


class ProjectTrust(Layout):
    """codex_project_trust.py writes only into a home it was handed, and never makes one."""

    def run_trust(self, home, working_dir):
        return subprocess.run([sys.executable, str(REPO / "src/codex_project_trust.py"),
                               str(home), str(working_dir), str(working_dir)],
                              env=self.env(), capture_output=True, text=True, timeout=60)

    def git_checkout(self):
        work = self.root / "work"
        work.mkdir()
        subprocess.run(["git", "init", "-q", str(work)], check=True, env=self.env())
        return work

    def test_a_missing_home_fails_and_is_not_created(self):
        work = self.git_checkout()
        missing = self.root / "app-support" / "workspace" / ".codex-sutando"
        before = self.tree()
        out = self.run_trust(missing, work)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("codex project trust:", out.stderr)
        self.assertEqual(self.tree(), before)

    def test_an_empty_home_fails_and_writes_nothing(self):
        work = self.git_checkout()
        before = self.tree()
        out = self.run_trust("", work)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertEqual(self.tree(), before)

    def test_a_dangling_owner_link_is_not_followed_into_creation(self):
        work = self.git_checkout()
        target = self.root / "elsewhere"
        self.owner.symlink_to(target)
        before = self.tree()
        out = self.run_trust(self.owner, work)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(self.tree(), before)

    def test_an_existing_home_gets_the_entry(self):
        work = self.git_checkout()
        self.owner.mkdir()
        out = self.run_trust(self.owner, work)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "trusted"), out.stderr)
        self.assertIn("trust_level", (self.owner / "config.toml").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
