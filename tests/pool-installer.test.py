#!/usr/bin/env python3
"""Contract tests for scripts/install-core-pool.sh preflight + plist shape.

The installer must resolve CLAUDE_CONFIG_DIR through the repo's shared helper
(src/claude_config_dir.sh) — the same answer startup.sh exports — and check the
pool skill in THAT dir. A preflight that checks a different, host-specific
directory fails on every host but the one it was written on.

Everything runs against a fake repo in a temp dir with stub `launchctl`, `tmux`
and `claude` on PATH and a temp $HOME, so the live pool's launchd domain is
never touched.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install-core-pool.sh"

STUB_CONFIG = """#!/bin/bash
case "$1" in
  workspace) printf '%s' "$STUB_WORKSPACE" ;;
  claude-sutando-config-dir) {config_dir_body} ;;
  *) echo "stub sutando-config: unsupported '$1'" >&2; exit 2 ;;
esac
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


class PoolInstallerHarness(unittest.TestCase):
    def make_repo(self, td: Path, *, config_dir_body: str, with_skill=True):
        """A minimal checkout: the real installer + the real config resolver."""
        repo = td / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "src").mkdir(parents=True)
        shutil.copy(INSTALLER, repo / "scripts" / "install-core-pool.sh")
        shutil.copy(REPO / "src" / "claude_config_dir.sh",
                    repo / "src" / "claude_config_dir.sh")
        for w in ("pool-core-wrapper.sh", "pool-follower-beat.sh"):
            shutil.copy(REPO / "scripts" / w, repo / "scripts" / w)
        _write_exec(repo / "scripts" / "sutando-config.sh",
                    STUB_CONFIG.format(config_dir_body=config_dir_body))
        if with_skill:
            skill = repo / "skills" / "proactive-loop-pool"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# stub pool skill\n")
        return repo

    def make_env(self, td: Path, **extra):
        home = td / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        binstub = td / "bin"
        binstub.mkdir()
        for name in ("launchctl", "tmux", "claude"):
            _write_exec(binstub / name, "#!/bin/bash\nexit 0\n")
        ws = td / "ws"
        ws.mkdir()
        env = dict(
            os.environ,
            HOME=str(home),
            PATH=f"{binstub}:/usr/bin:/bin:/usr/sbin:/sbin",
            STUB_WORKSPACE=str(ws),
        )
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.update(extra)
        return env, home, ws

    def run_installer(self, repo: Path, env, *args):
        return subprocess.run(
            ["bash", str(repo / "scripts" / "install-core-pool.sh"), *args],
            env=env, capture_output=True, text=True, timeout=120)


class PreflightConfigDirTest(PoolInstallerHarness):
    def test_preflight_checks_the_resolved_config_dir(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            resolved = td / "resolved-ccd"
            repo = self.make_repo(
                td, config_dir_body=f"printf '%s' '{resolved}'")
            # A caller-set CLAUDE_CONFIG_DIR that is NOT the resolved answer:
            # the installer must follow the helper, like startup.sh does.
            env, home, _ = self.make_env(
                td, CLAUDE_CONFIG_DIR=str(td / "caller-ccd"))
            r = self.run_installer(repo, env, "1", "--check-only")
            self.assertEqual(r.returncode, 0, f"preflight failed:\n{r.stderr}")
            self.assertIn(f"CLAUDE_CONFIG_DIR={resolved}", r.stdout)
            link = resolved / "skills" / "proactive-loop-pool"
            self.assertTrue(link.is_symlink(), "skill was not linked into the "
                                               "resolved config dir")
            self.assertEqual(
                Path(os.readlink(link)), repo / "skills" / "proactive-loop-pool")
            self.assertIn(str(link), r.stdout,
                          "preflight must name the dir it checked")
            self.assertFalse(
                (home / ".claude").exists(),
                "installer must not fall back to a default claude home")

    def test_preflight_fails_when_the_checkout_lacks_the_skill(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            resolved = td / "resolved-ccd"
            repo = self.make_repo(
                td, config_dir_body=f"printf '%s' '{resolved}'",
                with_skill=False)
            env, _, _ = self.make_env(td)
            r = self.run_installer(repo, env, "1", "--check-only")
            self.assertEqual(r.returncode, 1)
            self.assertIn("skill not found", r.stderr)
            self.assertIn(str(resolved / "skills" / "proactive-loop-pool"),
                          r.stderr, "the guard must name the resolved dir")

    def test_refuses_when_the_config_helper_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo = self.make_repo(
                td,
                config_dir_body="echo 'bad config' >&2; exit 1")
            env, home, _ = self.make_env(
                td, CLAUDE_CONFIG_DIR=str(td / "caller-ccd"))
            r = self.run_installer(repo, env, "1", "--check-only")
            self.assertNotEqual(r.returncode, 0,
                                "an unresolvable config dir must refuse, not "
                                "guess a credential store")
            self.assertFalse((td / "caller-ccd" / "skills").exists())
            self.assertFalse((home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
