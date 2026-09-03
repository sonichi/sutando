#!/usr/bin/env python3
"""Contract tests for scripts/install-worker-pool.sh preflight + plist shape.

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
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install-worker-pool.sh"
RUNTIME = REPO / "src" / "startup-runtime.sh"

STUB_CONFIG = """#!/bin/bash
case "$1" in
  workspace) printf '%s' "$STUB_WORKSPACE" ;;
  claude-sutando-config-dir) {config_dir_body} ;;
  core-config-dir-env-name) printf '%s' "${{STUB_CODEX_CONFIG_ENV:-}}" ;;
  core-config-dir-value) printf '%s' "${{STUB_CODEX_CONFIG_DIR:-}}" ;;
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
        shutil.copy(INSTALLER, repo / "scripts" / "install-worker-pool.sh")
        shutil.copy(REPO / "src" / "claude_config_dir.sh",
                    repo / "src" / "claude_config_dir.sh")
        shutil.copy(REPO / "src" / "pool_names.py", repo / "src" / "pool_names.py")
        shutil.copy(REPO / "scripts" / "python-binary.sh",
                    repo / "scripts" / "python-binary.sh")
        (repo / "src" / "launchd").mkdir()
        shutil.copy(REPO / "src" / "launchd" / "com.sutando.pool-lead.plist",
                    repo / "src" / "launchd" / "com.sutando.pool-lead.plist")
        for w in ("pool-worker-wrapper.sh", "pool-follower-beat.sh",
                  "pool-lead-wrapper.sh", "pool-lead-daemon.py",
                  "kick-pool.sh", "pool-runtime-drive.sh",
                  "uninstall-worker-pool.sh"):
            shutil.copy(REPO / "scripts" / w, repo / "scripts" / w)
        _write_exec(repo / "scripts" / "sutando-config.sh",
                    STUB_CONFIG.format(config_dir_body=config_dir_body))
        if with_skill:
            skill = repo / "skills" / "proactive-loop-pool"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# stub pool skill\n")
            (skill / "CODEX.md").write_text("# stub codex entry\n")
        return repo

    def make_env(self, td: Path, **extra):
        home = td / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        binstub = td / "bin"
        binstub.mkdir(exist_ok=True)
        for name in ("claude", "codex"):
            _write_exec(binstub / name, "#!/bin/bash\nexit 0\n")
        # Recording stubs: which jobs and sessions a run actually touched is
        # the contract for --only-worker and shrink; "exit 0" cannot answer it.
        _write_exec(binstub / "launchctl",
                    '#!/bin/bash\n[ -n "${LAUNCHCTL_LOG:-}" ] && '
                    'printf "%s\\n" "$*" >> "$LAUNCHCTL_LOG"\nexit 0\n')
        _write_exec(binstub / "tmux",
                    '#!/bin/bash\n[ -n "${TMUX_LOG:-}" ] && '
                    'printf "%s\\n" "$*" >> "$TMUX_LOG"\nexit 0\n')
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
            ["bash", str(repo / "scripts" / "install-worker-pool.sh"), *args],
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
            self.assertNotIn("__", repr(data), "unsubstituted plist placeholder")
            # The message must name the directory the logs are actually in.
            self.assertIn(str(logs), r.stdout)
            self.assertNotIn(f"{ws}/logs/core-", r.stdout)

    def test_lead_job_bakes_the_shared_helpers_interpreter(self):
        """POOL_PY must come from scripts/python-binary.sh, not a bare
        `command -v python3`: the launcher's SUTANDO_PY override outranks
        PATH there, and on a Mac without the CLT the PATH python3 is the
        stub that raises the install dialog."""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            override = td / "override" / "python3"
            override.parent.mkdir()
            _write_exec(override, f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
            resolved = td / "resolved-ccd"
            repo = self.make_repo(td, config_dir_body=f"printf '%s' '{resolved}'")
            env, home, _ = self.make_env(td, SUTANDO_PY=str(override))
            r = self.run_installer(repo, env, "1")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            envv = self.lead_plist(home)["EnvironmentVariables"]
            self.assertEqual(envv["POOL_PY"], str(override),
                             "the plist baked a PATH python3, not the resolved one")

    def test_follower_path_carries_the_npm_global_bin(self):
        # A non-login installer run snapshotted a PATH without npm's global
        # bin, so followers could not see globally-installed CLIs (codex).
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            resolved = td / "resolved-ccd"
            repo = self.make_repo(td, config_dir_body=f"printf '%s' '{resolved}'")
            env, home, ws = self.make_env(td)
            prefix = td / "npmprefix"
            (prefix / "bin").mkdir(parents=True)
            _write_exec(Path(env["PATH"].split(":")[0]) / "npm",
                        f'#!/bin/bash\n[ "$1" = "prefix" ] && printf "%s" "{prefix}"\n')
            r = self.run_installer(repo, env, "1")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            data = plistlib.loads(
                (home / "Library" / "LaunchAgents"
                 / "com.sutando.worker-1.plist").read_bytes())
            self.assertIn(str(prefix / "bin"),
                          data["EnvironmentVariables"]["PATH"].split(":"),
                          "installer must resolve npm's global bin itself, "
                          "not inherit it from the caller's shell")

    def test_followers_keep_their_supervised_shape(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _ = self.install(td, "2")
            agents = home / "Library" / "LaunchAgents"
            for i in (1, 2):
                with (agents / f"com.sutando.worker-{i}.plist").open("rb") as fh:
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
            shutil.copy(REPO / "scripts" / "uninstall-worker-pool.sh",
                        repo / "scripts" / "uninstall-worker-pool.sh")
            env, _, _ = self.make_env(td)  # same temp HOME + stub launchctl
            r = subprocess.run(
                ["bash", str(repo / "scripts" / "uninstall-worker-pool.sh")],
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
            other = subprocess.Popen(["bash", str(daemon)])
            marker = td / "ran"
            try:
                r = self.run_wrapper(td, marker)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(marker.exists(),
                                 "two leads from one checkout would both sweep")
                self.assertIn("standing down", r.stdout)
            finally:
                other.kill()
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
        _write_exec(repo / "scripts" / "install-worker-pool.sh",
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


class RuntimeDimensionTest(PoolInstallerHarness):
    """A core can be declared codex; unspecified stays claude, byte-for-byte."""

    def install(self, td: Path, *args, **envextra):
        resolved = td / "resolved-ccd"
        repo = self.make_repo(td, config_dir_body=f"printf '%s' '{resolved}'")
        env, home, ws = self.make_env(td, **envextra)
        r = self.run_installer(repo, env, *args)
        return repo, home, ws, env, r

    def core_env(self, home: Path, i: int):
        p = home / "Library" / "LaunchAgents" / f"com.sutando.worker-{i}.plist"
        self.assertTrue(p.exists(), f"worker-{i} got no launchd job")
        return plistlib.loads(p.read_bytes())["EnvironmentVariables"]

    def test_default_install_keeps_the_pre_runtime_claude_plist_values(self):
        # Regression: adding the dimension must not perturb any key the
        # installer already emitted, or every existing follower changes shape.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, home, ws, env, r = self.install(td, "1")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            envv = self.core_env(home, 1)
            binstub = env["PATH"].split(":")[0]
            self.assertEqual(
                {k: envv[k] for k in (
                    "SUTANDO_CORE_ID", "SUTANDO_CORE_POOL_SIZE",
                    "POOL_REPO_DIR", "POOL_CLAUDE_BIN", "POOL_TMUX_BIN",
                    "POOL_WORKSPACE", "CLAUDE_CONFIG_DIR")},
                {"SUTANDO_CORE_ID": "1", "SUTANDO_CORE_POOL_SIZE": "1",
                 "POOL_REPO_DIR": str(repo),
                 "POOL_CLAUDE_BIN": f"{binstub}/claude",
                 "POOL_TMUX_BIN": f"{binstub}/tmux",
                 "POOL_WORKSPACE": str(ws),
                 "CLAUDE_CONFIG_DIR": str(td / "resolved-ccd")})
            self.assertEqual(envv["POOL_RUNTIME"], "claude",
                             "unspecified must stay claude")
            self.assertEqual(envv["POOL_RUNTIME_BIN"], envv["POOL_CLAUDE_BIN"])
            self.assertNotIn("POOL_RUNTIME_CONFIG_ENV", envv,
                             "a claude core carries no codex config keys")

    def test_per_core_runtime_installs_one_codex_follower(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, env, r = self.install(td, "2", "--worker-runtime=2:codex")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            binstub = env["PATH"].split(":")[0]
            self.assertEqual(self.core_env(home, 1)["POOL_RUNTIME"], "claude")
            two = self.core_env(home, 2)
            self.assertEqual(two["POOL_RUNTIME"], "codex")
            self.assertEqual(two["POOL_RUNTIME_BIN"], f"{binstub}/codex",
                             "a codex core must carry the codex binary, not claude's")
            self.assertIn("runtime=codex", r.stdout)

    def test_runtime_flag_applies_to_every_core(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _, r = self.install(td, "2", "--runtime=codex")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            for i in (1, 2):
                self.assertEqual(self.core_env(home, i)["POOL_RUNTIME"], "codex")

    def test_env_default_runtime_is_honoured_and_the_flag_wins(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _, r = self.install(td, "1", SUTANDO_POOL_RUNTIME="codex")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertEqual(self.core_env(home, 1)["POOL_RUNTIME"], "codex")
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _, r = self.install(td, "1", "--runtime=claude",
                                           SUTANDO_POOL_RUNTIME="codex")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertEqual(self.core_env(home, 1)["POOL_RUNTIME"], "claude")

    def test_codex_core_carries_the_resolved_config_store(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            store = td / "codex-home"
            _, home, _, _, r = self.install(
                td, "1", "--runtime=codex",
                STUB_CODEX_CONFIG_ENV="CODEX_HOME",
                STUB_CODEX_CONFIG_DIR=str(store))
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            envv = self.core_env(home, 1)
            self.assertEqual(envv["POOL_RUNTIME_CONFIG_ENV"], "CODEX_HOME")
            self.assertEqual(envv["POOL_RUNTIME_CONFIG_DIR"], str(store))

    def test_unsupported_runtime_exits_2_and_installs_nothing(self):
        for args in (("1", "--runtime=gemini"), ("1", "--worker-runtime=1:gemini")):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as t:
                td = Path(t)
                _, home, _, _, r = self.install(td, *args)
                self.assertEqual(r.returncode, 2,
                                 f"an unknown runtime must fail loudly:\n{r.stdout}")
                self.assertIn("unsupported worker runtime", r.stderr)
                agents = home / "Library" / "LaunchAgents"
                self.assertEqual(list(agents.glob("com.sutando.core-*.plist")), [],
                                 "an unknown runtime must not silently install claude")

    def test_core_runtime_index_outside_the_pool_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, home, _, _, r = self.install(td, "2", "--worker-runtime=5:codex")
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("outside the installed pool", r.stderr)

    def test_codex_runtime_without_the_cli_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            resolved = td / "resolved-ccd"
            repo = self.make_repo(td, config_dir_body=f"printf '%s' '{resolved}'")
            env, home, _ = self.make_env(td)
            (Path(env["PATH"].split(":")[0]) / "codex").unlink()
            r = self.run_installer(repo, env, "1", "--runtime=codex")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("'codex' CLI not found", r.stderr)
            agents = home / "Library" / "LaunchAgents"
            self.assertEqual(list(agents.glob("com.sutando.core-*.plist")), [])

    def test_check_only_names_each_cores_runtime(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, _, _, env, r = self.install(
                td, "2", "--worker-runtime=2:codex", "--check-only")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            binstub = env["PATH"].split(":")[0]
            self.assertIn(f"worker-1 runtime=claude bin={binstub}/claude", r.stdout)
            self.assertIn(f"worker-2 runtime=codex bin={binstub}/codex", r.stdout)


class SingleCoreLifecycleTest(PoolInstallerHarness):
    """Turning one follower on or off must not restart the working pool."""

    def install_pool(self, td: Path, *args):
        resolved = td / "resolved-ccd"
        repo = self.make_repo(td, config_dir_body=f"printf '%s' '{resolved}'")
        env, home, ws = self.make_env(td)
        r = self.run_installer(repo, env, *args)
        self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
        return repo, env, home, ws

    def core_env(self, home: Path, i: int):
        p = home / "Library" / "LaunchAgents" / f"com.sutando.worker-{i}.plist"
        return plistlib.loads(p.read_bytes())["EnvironmentVariables"]

    def test_shared_drive_library_is_staged_beside_the_scripts(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, _, home, _ = self.install_pool(td, "1")
            lib = home / ".sutando" / "bin" / "pool-runtime-drive.sh"
            self.assertTrue(lib.exists(),
                            "kick-pool resolves it as a sibling of its staged self")
            self.assertTrue(os.access(lib, os.X_OK))

    def test_only_core_converts_one_follower_and_leaves_the_rest_loaded(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, home, _ = self.install_pool(td, "3")
            agents = home / "Library" / "LaunchAgents"
            before = {n: (agents / n).read_bytes()
                      for n in ("com.sutando.worker-1.plist",
                                "com.sutando.worker-3.plist",
                                "com.sutando.pool-lead.plist")}
            log = td / "launchctl.log"
            env2 = dict(env, LAUNCHCTL_LOG=str(log))
            r = self.run_installer(repo, env2, "--only-worker=2",
                                   "--worker-runtime=2:codex")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertEqual(self.core_env(home, 2)["POOL_RUNTIME"], "codex")
            self.assertEqual(self.core_env(home, 2)["SUTANDO_CORE_POOL_SIZE"], "3",
                             "an inferred pool size must match the installed pool")
            for n, body in before.items():
                self.assertEqual((agents / n).read_bytes(), body,
                                 f"{n} was rewritten by a single-core refresh")
            touched = log.read_text()
            for label in ("com.sutando.pool-lead", "com.sutando.worker-1",
                          "com.sutando.worker-3"):
                self.assertNotIn(label, touched,
                                 f"--only-worker restarted {label}")
            self.assertIn("com.sutando.worker-2", touched)

    def test_only_core_rejects_an_n_that_disagrees_with_the_installed_pool(self):
        """An installed size-2 pool refreshed as `--only-worker=2 3` would write
        POOL_SIZE=3 into core 2 alone, leaving it disagreeing with its peers."""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, _home, _ws = self.install_pool(td, "2")
            r = self.run_installer(repo, env, "3", "--only-worker=2")
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("preserves the installed pool size", r.stderr)

    def test_only_core_accepts_an_n_that_agrees(self):
        """Control: the rejection must be caused by DISAGREEMENT, not by the
        presence of an N — the out-of-pool test below relies on an agreeing N."""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, _home, _ws = self.install_pool(td, "2")
            r = self.run_installer(repo, env, "2", "--only-worker=1", "--check-only")
            self.assertNotIn("preserves the installed pool size", r.stderr)

    def test_only_core_outside_the_pool_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, home, _ = self.install_pool(td, "2")
            r = self.run_installer(repo, env, "2", "--only-worker=5")
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("outside the pool", r.stderr)

    def test_shrink_ends_the_removed_workers_session_and_beat(self):
        """`install 3` then `install 2` must end worker-3 entirely. Booting
        out the job and deleting the plist leaves its tmux session running
        and its beat fresh, so the lead keeps assigning to a seat nothing
        supervises. The kept workers' sessions must survive the resize."""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, home, ws = self.install_pool(td, "3")
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True, exist_ok=True)
            for i in (1, 2, 3):
                (cores / f"worker-{i}.alive").write_text("{}")
            tmux_log, lctl_log = td / "tmux.log", td / "launchctl.log"
            env2 = dict(env, TMUX_LOG=str(tmux_log), LAUNCHCTL_LOG=str(lctl_log))
            r = self.run_installer(repo, env2, "2")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            agents = home / "Library" / "LaunchAgents"
            self.assertFalse((agents / "com.sutando.worker-3.plist").exists())
            self.assertTrue((agents / "com.sutando.worker-2.plist").exists())
            self.assertIn("bootout gui/", lctl_log.read_text())
            self.assertIn("com.sutando.worker-3", lctl_log.read_text())
            sessions = tmux_log.read_text().splitlines()
            self.assertIn("kill-session -t worker-3", sessions,
                          "shrink removed the plist but left the session alive")
            for kept in ("worker-1", "worker-2"):
                self.assertNotIn(f"kill-session -t {kept}", sessions,
                                 f"resize ended {kept}, which stays in the pool")
            self.assertFalse((cores / "worker-3.alive").exists(),
                             "a stale beat keeps the lead assigning to a ghost")
            self.assertTrue((cores / "worker-1.alive").exists())
            self.assertTrue((cores / "worker-2.alive").exists())

    def test_uninstall_only_core_removes_plist_session_and_beat(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, home, ws = self.install_pool(td, "2")
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True, exist_ok=True)
            for i in (1, 2):
                (cores / f"worker-{i}.alive").write_text("{}")
            r = subprocess.run(
                ["bash", str(repo / "scripts" / "uninstall-worker-pool.sh"),
                 "--only-worker=2"],
                env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            agents = home / "Library" / "LaunchAgents"
            self.assertFalse((agents / "com.sutando.worker-2.plist").exists())
            self.assertTrue((agents / "com.sutando.worker-1.plist").exists(),
                            "a single-core teardown must not remove the pool")
            self.assertTrue((agents / "com.sutando.pool-lead.plist").exists())
            self.assertFalse((cores / "worker-2.alive").exists(),
                             "a stale beat keeps the lead assigning to a ghost")
            self.assertTrue((cores / "worker-1.alive").exists())

    def test_uninstall_only_core_clears_state_left_by_a_hand_built_core(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo, env, home, ws = self.install_pool(td, "1")
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True, exist_ok=True)
            (cores / "worker-4.alive").write_text("{}")
            r = subprocess.run(
                ["bash", str(repo / "scripts" / "uninstall-worker-pool.sh"),
                 "--only-worker=4"],
                env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((cores / "worker-4.alive").exists())


if __name__ == "__main__":
    unittest.main()
