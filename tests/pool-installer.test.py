#!/usr/bin/env python3
"""Contract tests for scripts/install-core-pool.sh preflight + plist shape.

The installer must resolve CLAUDE_CONFIG_DIR through the repo's shared helper
(src/claude_config_dir.sh) — the same answer startup.sh exports — and check the
pool skill in THAT dir. A preflight that checks a different, host-specific
directory fails on every host but the one it was written on.

The lead gets a launchd job like every other Sutando service, with the pool's
TCC-safe shape (staged wrapper, home cwd, Library logs — launchd cannot open
log paths under the Documents tree), and startup.sh prefers that job over an
unsupervised launch via pool_lead_supervised().

Everything runs against a fake repo in a temp dir with stub `launchctl`, `tmux`
and `claude` on PATH and a temp $HOME, so the live pool's launchd domain is
never touched.
"""
import os
import plistlib
import re
import signal
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install-core-pool.sh"
RUNTIME = REPO / "src" / "startup-runtime.sh"

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
        (repo / "src" / "launchd").mkdir()
        shutil.copy(REPO / "src" / "launchd" / "com.sutando.pool-lead.plist",
                    repo / "src" / "launchd" / "com.sutando.pool-lead.plist")
        for w in ("pool-core-wrapper.sh", "pool-follower-beat.sh",
                  "pool-lead-wrapper.sh", "pool-lead-daemon.py"):
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
        (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        binstub = td / "bin"
        binstub.mkdir(exist_ok=True)
        for name in ("launchctl", "tmux", "claude"):
            _write_exec(binstub / name, "#!/bin/bash\nexit 0\n")
        ws = td / "ws"
        ws.mkdir(exist_ok=True)
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


class LeadPlistTest(PoolInstallerHarness):
    def install(self, td: Path, *args):
        resolved = td / "resolved-ccd"
        repo = self.make_repo(td, config_dir_body=f"printf '%s' '{resolved}'")
        env, home, ws = self.make_env(td)
        r = self.run_installer(repo, env, *args)
        self.assertEqual(r.returncode, 0, f"install failed:\n{r.stdout}\n{r.stderr}")
        return repo, home, ws, r

    def lead_plist(self, home: Path):
        p = home / "Library" / "LaunchAgents" / "com.sutando.pool-lead.plist"
        self.assertTrue(p.exists(), "the lead got no launchd job")
        with p.open("rb") as fh:
            return plistlib.load(fh)

    def test_lead_job_is_keepalive_and_tcc_safe(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, home, ws, r = self.install(td, "2")
            data = self.lead_plist(home)
            self.assertEqual(data["Label"], "com.sutando.pool-lead")
            self.assertIs(data["KeepAlive"], True, "a lead nothing restarts is "
                                                   "the defect being fixed")
            self.assertIs(data["RunAtLoad"], True)
            self.assertIsInstance(data["ThrottleInterval"], int)
            wrapper = Path(data["ProgramArguments"][1])
            self.assertEqual(wrapper, home / ".sutando" / "bin" /
                             "pool-lead-wrapper.sh",
                             "launchd must exec the staged wrapper, not a repo path")
            self.assertTrue(os.access(wrapper, os.X_OK))
            logs = home / "Library" / "Application Support" / "Sutando" / "logs"
            for key in ("StandardOutPath", "StandardErrorPath"):
                self.assertTrue(Path(data[key]).parent == logs,
                                f"{key}={data[key]} is not TCC-safe")
                self.assertNotIn(str(ws), data[key])
                self.assertNotIn(str(repo), data[key])
            envv = data["EnvironmentVariables"]
            self.assertEqual(envv["POOL_REPO_DIR"], str(repo))
            self.assertTrue(Path(envv["POOL_PY"]).name.startswith("python3"))
            self.assertIsNone(
                re.search(r"__[A-Z][A-Z0-9_]*__", repr(data)),
                "unsubstituted plist placeholder")
            # The message must name the directory the logs are actually in.
            self.assertIn(str(logs), r.stdout)
            self.assertNotIn(f"{ws}/logs/core-", r.stdout)

    def test_followers_keep_their_supervised_shape(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _ = self.install(td, "2")
            agents = home / "Library" / "LaunchAgents"
            for i in (1, 2):
                with (agents / f"com.sutando.core-{i}.plist").open("rb") as fh:
                    data = plistlib.load(fh)
                self.assertIs(data["KeepAlive"], True)
                self.assertIn("Application Support/Sutando/logs",
                              data["StandardOutPath"])

    def test_lead_only_installs_the_lead_alone(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _ = self.install(td, "2", "--lead-only")
            self.lead_plist(home)
            agents = home / "Library" / "LaunchAgents"
            self.assertEqual(sorted(p.name for p in agents.glob("*.plist")),
                             ["com.sutando.pool-lead.plist"])

    def test_uninstall_removes_the_lead_job(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, home, _, _ = self.install(td, "1")
            shutil.copy(REPO / "scripts" / "uninstall-core-pool.sh",
                        repo / "scripts" / "uninstall-core-pool.sh")
            env, _, _ = self.make_env(td)  # same temp HOME + stub launchctl
            r = subprocess.run(
                ["bash", str(repo / "scripts" / "uninstall-core-pool.sh")],
                env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            agents = home / "Library" / "LaunchAgents"
            self.assertEqual(list(agents.glob("*.plist")), [],
                             "a KeepAlive lead outlived the pool it leads")


class LeadWrapperTest(unittest.TestCase):
    """The staged wrapper is the one-lead-per-install gate."""

    def run_wrapper(self, td: Path, marker: Path):
        py = td / "stub-py"
        _write_exec(py, f'#!/bin/bash\necho "$@" > "{marker}"\n')
        env = dict(os.environ, POOL_REPO_DIR=str(td), POOL_PY=str(py),
                   SUTANDO_POOL_LEAD_DEFER_S="0")
        return subprocess.run(
            ["bash", str(REPO / "scripts" / "pool-lead-wrapper.sh")],
            env=env, capture_output=True, text=True, timeout=60)

    def test_execs_the_daemon_when_no_lead_runs(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            (td / "scripts").mkdir()
            (td / "scripts" / "pool-lead-daemon.py").write_text("")
            marker = td / "ran"
            r = self.run_wrapper(td, marker)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(marker.read_text().strip(),
                             str(td / "scripts" / "pool-lead-daemon.py"))

    def test_stands_down_when_this_checkouts_lead_is_running(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            (td / "scripts").mkdir()
            daemon = td / "scripts" / "pool-lead-daemon.py"
            daemon.write_text("sleep 60\n")
            # A live process whose argv carries this checkout's daemon path.
            other = subprocess.Popen(
                ["bash", str(daemon)], start_new_session=True)
            marker = td / "ran"
            try:
                r = self.run_wrapper(td, marker)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(marker.exists(),
                                 "two leads from one checkout would both sweep")
                self.assertIn("standing down", r.stdout)
            finally:
                os.killpg(other.pid, signal.SIGKILL)
                other.wait(timeout=10)


class PoolLeadSupervisedTest(unittest.TestCase):
    """startup.sh's helper prefers launchd and installs the job when missing."""

    def drive(self, td: Path, *, service_loaded: bool, installer_rc=0):
        repo = td / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "src" / "launchd").mkdir(parents=True)
        (repo / "scripts" / "pool-lead-daemon.py").write_text("")
        (repo / "src" / "launchd" / "com.sutando.pool-lead.plist").write_text("")
        started = td / "lead-started"
        _write_exec(repo / "scripts" / "install-core-pool.sh",
                    f'#!/bin/bash\necho "$@" > "{td / "installer-args"}"\n'
                    f'[ {installer_rc} -eq 0 ] && touch "{started}"\n'
                    f'exit {installer_rc}\n')
        binstub = td / "bin"
        binstub.mkdir()
        _write_exec(binstub / "launchctl",
                    "#!/bin/bash\n"
                    f'case "$1" in print) exit {0 if service_loaded else 1};; '
                    'kickstart) exit 0;; esac\nexit 0\n')
        # pgrep answers "is a lead from this checkout running?" — true only once
        # something claims to have started one.
        _write_exec(binstub / "pgrep", f'#!/bin/bash\n[ -e "{started}" ]\n')
        if service_loaded:
            started.touch()
        script = (f'source "{RUNTIME}"\nREPO="{repo}"\n'
                  'SUTANDO_POOL_LEAD_WAIT_S=2\n'
                  'pool_lead_supervised\necho "RC=$?"\n')
        env = dict(os.environ,
                   PATH=f"{binstub}:{os.environ['PATH']}")
        r = subprocess.run(["bash", "-c", script], env=env,
                           capture_output=True, text=True, timeout=60)
        return r, td / "installer-args"

    def test_loaded_service_is_not_reinstalled(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, args = self.drive(td, service_loaded=True)
            self.assertIn("RC=0", r.stdout, r.stderr)
            self.assertFalse(args.exists(),
                             "a loaded lead job must not be reinstalled")

    def test_missing_service_is_installed_lead_only(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, args = self.drive(td, service_loaded=False)
            self.assertIn("RC=0", r.stdout, r.stderr)
            self.assertEqual(args.read_text().strip(), "--lead-only",
                             "self-install must not re-bootstrap followers")

    def test_failed_install_reports_so_the_caller_can_fall_back(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, _ = self.drive(td, service_loaded=False, installer_rc=1)
            self.assertIn("RC=1", r.stdout, r.stderr)


if __name__ == "__main__":
    unittest.main()
