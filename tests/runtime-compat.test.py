#!/usr/bin/env python3
"""runtime_compat: discovery, classification, repair planning, assisted repair, auth.

Every binary here is a real executable shell stub on a temp PATH, so the probe
launches it exactly as it launches the user's CLI. Test names match the
"Test" column of docs/runtime-compat-repair-matrix.md.

Run: python3 tests/runtime-compat.test.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import owner_reply  # noqa: E402
import runtime_compat as rc  # noqa: E402

CLAUDE_FLAGS = " ".join(rc.RUNTIMES["claude"].required_flags)
CODEX_FLAGS = " ".join(rc.RUNTIMES["codex"].required_flags)
Disk = namedtuple("Disk", "total used free")
PLENTY = Disk(10 ** 12, 0, 10 ** 12)


def write_exe(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def fake_cli(path: Path, version: str = "2.1.0", flags: str = CLAUDE_FLAGS,
             version_rc: int = 0, login_rc: int = 0, label: str = "(Claude Code)") -> str:
    return write_exe(path, f"""case "$1" in
  --version) echo "{version} {label}"; exit {version_rc} ;;
  --help) echo "Usage: x [options] {flags}"; exit 0 ;;
  login) exit {login_rc} ;;
esac
exit 0
""")


def fake_npm(path: Path, script: str) -> str:
    """An npm stub; $PREFIX is the --prefix argument it was given."""
    return write_exe(path, f"""PREFIX=""
while [ $# -gt 0 ]; do [ "$1" = --prefix ] && PREFIX="$2"; shift; done
{script}
""")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "runtimes"
        self.bin1 = self.tmp / "bin1"
        self.bin2 = self.tmp / "bin2"
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin1}:{self.bin2}:/usr/bin:/bin",
                    "SUTANDO_RUNTIMES_DIR": str(self.root)}
        self.sleeps = []
        self.said = []
        self._system = dict(rc.SYSTEM_LOCATIONS)
        rc.SYSTEM_LOCATIONS.update({"claude": (), "codex": ()})

    def tearDown(self):
        rc.SYSTEM_LOCATIONS.update(self._system)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def discover(self, runtime="claude", **kw):
        return rc.discover(runtime, env=self.env, **kw)

    def install(self, runtime="claude", **kw):
        kw.setdefault("disk_usage", lambda _p: PLENTY)
        return rc.assisted_install(runtime, env=self.env, sleep=self.sleeps.append,
                                   log=self.said.append, **kw)

    def managed_version(self, version="2.1.0", flags=CLAUDE_FLAGS, name="claude"):
        target = self.root / name / "versions" / version / "bin" / name
        fake_cli(target, version, flags)
        rc._swap_link(self.root / "bin" / name, target)
        return target


class Discovery(Base):
    def test_discovery_reads_version_and_capabilities(self):
        p = fake_cli(self.bin1 / "claude", "2.1.286")
        r = self.discover()
        self.assertEqual(r.status, rc.OK)
        self.assertEqual(r.selected.path, p)
        self.assertEqual(r.selected.version, "2.1.286")
        self.assertEqual(r.selected.missing_flags, [])

    def test_missing_when_nothing_installed(self):
        r = self.discover()
        self.assertEqual(r.status, rc.MISSING)
        self.assertIsNone(r.selected)
        self.assertIsNone(r.problem)

    def test_known_location_found_off_path(self):
        p = fake_cli(self.home / ".npm-global" / "bin" / "codex", "0.160.1", CODEX_FLAGS,
                     label="")
        r = self.discover("codex")
        self.assertEqual(r.selected.path, p)
        self.assertEqual(r.selected.source, "known")

    def test_system_dirs_override(self):
        p = fake_cli(self.tmp / "sys" / "claude")
        self.env["PATH"] = "/usr/bin:/bin"
        self.env["SUTANDO_RUNTIME_SYSTEM_DIRS"] = str(self.tmp / "sys")
        self.assertEqual(self.discover().selected.path, p)

    def test_known_location_used_when_path_install_is_broken(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        good = fake_cli(self.home / ".local" / "bin" / "claude", "2.1.0")
        r = self.discover()
        self.assertEqual((r.selected.path, r.selected.source), (good, "known"))

    def test_working_path_install_is_never_overridden_by_a_known_location(self):
        first = fake_cli(self.bin1 / "claude", "2.0.5")
        fake_cli(self.home / ".local" / "bin" / "claude", "2.9.0")
        r = self.discover()
        self.assertEqual(r.selected.path, first)
        self.assertEqual([c.source for c in r.candidates], ["PATH"])

    def test_too_old(self):
        fake_cli(self.bin1 / "claude", "1.0.99")
        r = self.discover()
        self.assertEqual(r.status, rc.TOO_OLD)
        self.assertIn("older than the minimum 2.0.0", r.problem.detail)

    def test_known_bad(self):
        spec = rc.RuntimeSpec(**dict(rc.RUNTIMES["claude"].__dict__, known_bad=((2, 1, 5),)))
        fake_cli(self.bin1 / "claude", "2.1.5")
        self.assertEqual(self.discover(spec=spec).status, rc.KNOWN_BAD)

    def test_newer_untested_is_usable_with_warning(self):
        fake_cli(self.bin1 / "claude", "3.0.0")
        r = self.discover()
        self.assertEqual(r.status, rc.NEWER_UNTESTED)
        self.assertTrue(r.selected.usable)
        step = rc.plan(r, root=self.root)
        self.assertFalse(step.consent_required)
        self.assertIn("@2", step.command)

    def test_incompatible_when_flag_missing(self):
        fake_cli(self.bin1 / "claude", "2.1.0", CLAUDE_FLAGS.replace("--remote-control", ""))
        r = self.discover()
        self.assertEqual(r.status, rc.INCOMPATIBLE)
        self.assertEqual(r.problem.missing_flags, ["--remote-control"])

    def test_help_failure_is_incompatible(self):
        write_exe(self.bin1 / "claude", 'case "$1" in --version) echo 2.1.0;; *) exit 2;; esac\n')
        self.assertEqual(self.discover().status, rc.INCOMPATIBLE)

    def test_broken_when_exec_fails(self):
        fake_cli(self.bin1 / "claude", "2.1.0", version_rc=1)
        r = self.discover()
        self.assertEqual(r.status, rc.BROKEN)
        self.assertIn("--version exited 1", r.problem.detail)

    def test_broken_when_not_executable(self):
        (self.bin1).mkdir()
        (self.bin1 / "claude").write_text("x")
        self.assertIn("not executable", self.discover().problem.detail)

    def test_broken_partial_binary_from_interrupted_update(self):
        write_exe(self.bin1 / "claude", "")
        (self.bin1 / "claude").write_bytes(b"\x7fELF\x00partial")
        (self.bin1 / "claude").chmod(0o755)
        self.assertEqual(self.discover().status, rc.BROKEN)

    def test_broken_dangling_link_from_interrupted_update(self):
        self.bin1.mkdir()
        os.symlink(str(self.tmp / "gone" / "claude"), str(self.bin1 / "claude"))
        r = self.discover()
        self.assertEqual(r.status, rc.BROKEN)
        self.assertIn("missing file", r.problem.detail)

    def test_broken_when_no_version_printed(self):
        write_exe(self.bin1 / "claude", "echo hello\n")
        self.assertIn("no version", self.discover().problem.detail)

    def test_broken_when_version_hangs(self):
        def run(argv, **_kw):
            raise rc.subprocess.TimeoutExpired(argv, 1)
        fake_cli(self.bin1 / "claude")
        self.assertIn("did not answer", self.discover(run=run).problem.detail)

    def test_broken_when_help_cannot_run(self):
        real = rc._run

        def run(argv, **kw):
            if argv[-1] == "--help":
                raise OSError("boom")
            return real(argv, **kw)
        fake_cli(self.bin1 / "claude")
        self.assertIn("--help could not run", self.discover(run=run).problem.detail)

    def test_broken_when_launch_raises(self):
        def run(argv, **_kw):
            raise OSError(8, "Exec format error")
        fake_cli(self.bin1 / "claude")
        self.assertIn("Exec format error", self.discover(run=run).problem.detail)

    def test_multiple_path_installs_first_usable_wins(self):
        fake_cli(self.bin1 / "claude", "1.0.0")
        good = fake_cli(self.bin2 / "claude", "2.1.0")
        r = self.discover()
        self.assertEqual(r.selected.path, good)
        self.assertEqual([c.status for c in r.candidates], [rc.TOO_OLD, rc.OK])

    def test_multiple_path_installs_keep_path_order_when_both_work(self):
        first = fake_cli(self.bin1 / "claude", "3.0.0")
        fake_cli(self.bin2 / "claude", "2.1.0")
        self.assertEqual(self.discover().selected.path, first)

    def test_same_install_twice_on_path_is_one_candidate(self):
        p = fake_cli(self.bin1 / "claude")
        self.bin2.mkdir()
        os.symlink(p, str(self.bin2 / "claude"))
        self.assertEqual(len(self.discover().candidates), 1)

    def test_directory_named_like_the_cli_is_ignored(self):
        (self.bin1 / "claude").mkdir(parents=True)
        self.assertEqual(self.discover().candidates, [])

    def test_managed_install_used_after_broken_path_install(self):
        fake_cli(self.bin1 / "claude", "2.1.0", version_rc=3)
        self.managed_version("2.2.0")
        r = self.discover()
        self.assertEqual(r.selected.source, "managed")
        self.assertEqual(r.selected.version, "2.2.0")

    def test_report_to_dict_round_trips_as_json(self):
        fake_cli(self.bin1 / "claude")
        d = json.loads(json.dumps(self.discover().to_dict()))
        self.assertEqual(d["status"], rc.OK)
        self.assertEqual(rc.Report("claude", [], None).to_dict()["selected"], None)

    def test_managed_root_defaults(self):
        self.assertEqual(rc.managed_root({"XDG_DATA_HOME": "/nonexistent/x"}),
                         Path("/nonexistent/x/sutando/runtimes"))
        self.assertTrue(str(rc.managed_root({})).endswith("sutando/runtimes"))
        self.assertTrue(str(rc.managed_root(None)))

    def test_parse_version(self):
        self.assertEqual(rc.parse_version("codex-cli 0.160.1"), (0, 160, 1))
        self.assertIsNone(rc.parse_version(""))


class Planner(Base):
    def plan(self, runtime="claude"):
        return rc.plan(self.discover(runtime), root=self.root)

    def test_plan_missing(self):
        s = self.plan()
        self.assertEqual(s.status, rc.MISSING)
        self.assertIn("claude.ai/install.sh", s.command)
        self.assertTrue(s.consent_required)
        self.assertIn("--yes", s.assisted)
        self.assertIn("doctor --runtime claude", s.verify)

    def test_plan_ok(self):
        fake_cli(self.bin1 / "claude")
        s = self.plan()
        self.assertEqual((s.status, s.command, s.consent_required), (rc.OK, "No action needed.", False))

    def test_plan_too_old_claude_uses_its_own_updater(self):
        p = fake_cli(self.bin1 / "claude", "1.5.0")
        s = self.plan()
        self.assertEqual(s.command, f"{p} update")
        self.assertIn(str(self.root), s.assisted_touches)

    def test_plan_too_old_npm_install_updates_that_prefix(self):
        prefix = self.tmp / "npmprefix"
        (prefix / "lib" / "node_modules" / "@openai" / "codex").mkdir(parents=True)
        fake_cli(prefix / "bin" / "codex", "0.10.0", CODEX_FLAGS, label="")
        self.env["PATH"] = f"{prefix / 'bin'}:/usr/bin:/bin"
        s = self.plan("codex")
        self.assertEqual(s.command, f"npm install -g --prefix {prefix} @openai/codex@0")

    def test_plan_too_old_codex_elsewhere(self):
        fake_cli(self.bin1 / "codex", "0.1.0", CODEX_FLAGS, label="")
        self.assertIn("@openai/codex@0", self.plan("codex").command)

    def test_plan_broken_reinstalls(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        s = self.plan()
        self.assertEqual(s.status, rc.BROKEN)
        self.assertIn("will not use it", s.reason)
        self.assertIsNotNone(s.assisted)

    def test_plan_broken_node_missing_points_at_node(self):
        write_exe(self.bin1 / "codex", "echo 'env: node: No such file or directory' >&2; exit 127\n")
        s = self.plan("codex")
        self.assertIn("Node.js", s.command)
        self.assertIsNone(s.assisted)


class AssistedRepair(Base):
    def npm(self, script):
        fake_npm(self.bin1 / "npm", script)

    def good_npm(self, version="2.3.0"):
        self.npm(f"""mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/claude" <<'EOS'
#!/bin/sh
case "$1" in --version) echo "{version} (Claude Code)";; --help) echo "{CLAUDE_FLAGS}";; esac
EOS
chmod +x "$PREFIX/bin/claude"
""")

    def test_assisted_install_launches_before_reporting_success(self):
        self.good_npm()
        r = self.install()
        self.assertTrue(r.ok, r)
        self.assertEqual(r.path, str(self.root / "bin" / "claude"))
        self.assertIn("2.3.0", r.detail)
        self.assertEqual(self.discover().selected.source, "managed")
        self.assertEqual(list((self.root / "claude").glob(".staging-*")), [])

    def test_reinstall_of_same_version_keeps_the_live_target(self):
        self.good_npm("2.3.0")
        self.assertTrue(self.install().ok)
        live = self.root / "claude" / "versions" / "2.3.0" / "bin" / "claude"
        before = live.stat().st_ino
        self.assertTrue(self.install().ok)
        self.assertEqual(live.stat().st_ino, before)
        self.assertTrue(any("reusing it" in m for m in self.said))

    def test_reinstall_replaces_a_broken_copy_of_the_same_version(self):
        broken = self.root / "claude" / "versions" / "2.3.0" / "bin" / "claude"
        write_exe(broken, "exit 1\n")
        self.good_npm("2.3.0")
        r = self.install()
        self.assertTrue(r.ok, r)
        self.assertEqual(self.discover().selected.version, "2.3.0")
        self.assertEqual(list((self.root / "claude").glob(".replaced-*")), [])

    def test_concurrent_installs_are_serialized(self):
        import threading
        sys.path.insert(0, str(REPO / "src"))
        from file_lock import locked_file
        self.good_npm()
        done = threading.Event()
        (self.root / "claude").mkdir(parents=True)
        with locked_file(self.root / "claude" / ".lock"):
            t = threading.Thread(target=lambda: (self.install(), done.set()))
            t.start()
            self.assertFalse(done.wait(1.5))
        t.join(30)
        self.assertTrue(done.is_set())

    def test_npm_missing(self):
        self.env["PATH"] = str(self.tmp / "empty")
        r = self.install()
        self.assertEqual(r.reason, "no-installer")
        self.assertIn("claude.ai/install.sh", r.user_action)

    def test_disk_full_checked_before_download(self):
        marker = self.tmp / "npm-ran"
        self.npm(f'touch "{marker}"\n')
        r = self.install(disk_usage=lambda _p: Disk(10, 10, 5 * 1024 * 1024))
        self.assertEqual(r.reason, "disk-full")
        self.assertFalse(marker.exists())
        self.assertIn("Free at least", r.user_action)

    def test_disk_full_during_download_is_not_retried(self):
        self.npm('echo "npm ERR! code ENOSPC" >&2; exit 1\n')
        r = self.install()
        self.assertEqual((r.reason, r.attempts, self.sleeps), ("disk-full", 1, []))

    def test_blocked_download_retries_with_backoff_then_guides(self):
        old = self.managed_version("2.1.0")
        self.npm('echo "npm ERR! getaddrinfo ENOTFOUND registry.npmjs.org" >&2; exit 1\n')
        r = self.install()
        self.assertEqual(r.reason, "blocked-download")
        self.assertEqual(r.attempts, 3)
        self.assertEqual(self.sleeps, [2.0, 4.0])
        self.assertIn("registry.npmjs.org", r.user_action)
        self.assertEqual(os.path.realpath(self.root / "bin" / "claude"), os.path.realpath(old))

    def test_blocked_download_recovers_on_retry(self):
        count = self.tmp / "count"
        self.good_npm()
        body = (self.bin1 / "npm").read_text()
        (self.bin1 / "npm").write_text(body.replace(
            '\nmkdir -p "$PREFIX/bin"',
            f'\nif [ ! -f "{count}" ]; then touch "{count}"; echo ETIMEDOUT >&2; exit 1; fi\n'
            'mkdir -p "$PREFIX/bin"', 1))
        r = self.install()
        self.assertTrue(r.ok, r)
        self.assertEqual((r.attempts, self.sleeps), (2, [2.0]))

    def test_generic_failure_and_unlaunchable_npm(self):
        self.npm('echo "npm ERR! something odd" >&2; exit 1\n')
        self.assertEqual(self.install(attempts=1).reason, "install-failed")

        def run(argv, **_kw):
            raise OSError("cannot exec")
        self.assertEqual(self.install(attempts=1, run=run).reason, "install-failed")

    def test_install_timeout_is_retried(self):
        def run(argv, **_kw):
            raise rc.subprocess.TimeoutExpired(argv, 1)
        self.npm("exit 0\n")
        r = self.install(attempts=2, run=run)
        self.assertEqual((r.reason, r.attempts), ("install-failed", 2))

    def test_interrupted_update_keeps_old_version(self):
        old = self.managed_version("2.1.0")
        self.npm('mkdir -p "$PREFIX/bin"; printf "#!/bin/sh\\nech" > "$PREFIX/bin/claude"; '
                 'chmod +x "$PREFIX/bin/claude"; kill -9 $$\n')
        r = self.install(attempts=1)
        self.assertFalse(r.ok)
        self.assertEqual(list((self.root / "claude").glob(".staging-*")), [])
        r2 = self.discover()
        self.assertEqual(os.path.realpath(r2.selected.path), os.path.realpath(old))

    def test_leftover_staging_is_reported_then_cleaned(self):
        self.managed_version("2.1.0")
        stale = self.root / "claude" / ".staging-999999-1"
        fake_cli(stale / "bin" / "claude", "2.4.0")
        r = self.discover(pid_alive=lambda _pid: False)
        self.assertEqual(r.interrupted, [str(stale)])
        self.assertEqual(r.selected.version, "2.1.0")
        self.good_npm()
        self.assertTrue(self.install(pid_alive=lambda _pid: False).ok)
        self.assertFalse(stale.exists())

    def test_live_staging_is_not_treated_as_interrupted(self):
        live = self.root / "claude" / f".staging-{os.getpid()}-1"
        live.mkdir(parents=True)
        self.assertEqual(self.discover().interrupted, [])
        self.assertFalse(rc._pid_alive(0))
        self.assertTrue(rc._pid_alive(1))

    def test_unusable_download_keeps_old_version(self):
        old = self.managed_version("2.1.0")
        self.good_npm(version="1.0.0")
        r = self.install()
        self.assertEqual(r.reason, "verify-failed")
        self.assertEqual(os.path.realpath(self.root / "bin" / "claude"), os.path.realpath(old))

    def test_link_that_will_not_launch_is_not_success(self):
        self.good_npm()
        real = rc.probe

        def probe(path, spec, **kw):
            c = real(path, spec, **kw)
            if kw.get("source") == "managed":
                c.status, c.detail = rc.BROKEN, "would not start"
            return c
        orig, rc.probe = rc.probe, probe
        try:
            r = self.install()
        finally:
            rc.probe = orig
        self.assertEqual(r.reason, "verify-failed")
        self.assertIn("would not start", r.detail)
        self.assertFalse(os.path.lexists(self.root / "bin" / "claude"))

    def test_failed_post_swap_verify_restores_the_previous_link(self):
        old = self.managed_version("2.1.0")
        self.good_npm("2.3.0")
        real = rc.probe

        def probe(path, spec, **kw):
            c = real(path, spec, **kw)
            if kw.get("source") == "managed":
                c.status, c.detail = rc.BROKEN, "would not start"
            return c
        orig, rc.probe = rc.probe, probe
        try:
            r = self.install()
        finally:
            rc.probe = orig
        self.assertEqual(r.reason, "verify-failed")
        self.assertIn("previous version was restored", r.detail)
        self.assertEqual(os.readlink(self.root / "bin" / "claude"), str(old))
        self.assertEqual(self.discover().selected.version, "2.1.0")

    def test_swap_link_replaces_stale_temp(self):
        target = self.tmp / "t"
        target.write_text("")
        tmp = self.root / "bin" / f".claude.tmp-{os.getpid()}"
        tmp.parent.mkdir(parents=True)
        os.symlink("nowhere", str(tmp))
        rc._swap_link(self.root / "bin" / "claude", target)
        self.assertEqual(os.readlink(self.root / "bin" / "claude"), str(target))

    def test_interrupted_updates_without_runtime_dir(self):
        self.assertEqual(rc.interrupted_updates(self.root, "claude", lambda _p: False), [])
        (self.root / "claude" / ".staging-x").mkdir(parents=True)
        self.assertEqual(len(rc.interrupted_updates(self.root, "claude", lambda _p: True)), 1)


class Auth(Base):
    def test_codex_logged_out_points_to_login(self):
        p = fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, login_rc=1, label="")
        self.env["CODEX_HOME"] = str(self.home / "codex home")
        a = rc.auth_status("codex", p, env=self.env)
        self.assertEqual(a["login"], "logged-out")
        self.assertIn("login", a["remedy"])
        self.assertIn("'", a["remedy"])

    def test_codex_logged_in_and_unknown(self):
        p = fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, label="")
        self.assertEqual(rc.auth_status("codex", p, env=self.env)["login"], "ok")

        def run(argv, **_kw):
            raise OSError("x")
        self.assertEqual(rc.auth_status("codex", p, env=self.env, run=run)["login"], "unknown")

    def test_claude_auth_uses_auth_preflight(self):
        cfg = self.tmp / "cfg"
        cfg.mkdir()
        a = rc.auth_status("claude", "claude", env=self.env, claude_config_dir=str(cfg),
                           keychain_check=lambda: False)
        self.assertEqual(a["login"], "logged-out")
        self.assertIn("/login", a["remedy"])
        (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"x": 1}}))
        (cfg / ".credentials.json").write_text("{}")
        self.assertEqual(rc.auth_status("claude", "claude", env=self.env,
                                        claude_config_dir=str(cfg))["login"], "ok")
        self.assertEqual(rc.auth_status("claude", "claude", env=self.env)["login"], "unknown")


class Ensure(Base):
    def report(self, usable=True, status=rc.OK):
        c = rc.Candidate("/nonexistent/claude", "PATH", status if usable else rc.TOO_OLD, "2.1.0")
        return rc.Report("claude", [c], c if usable else None, ["/nonexistent/.staging-1-1"])

    def ensure(self, reports, auths=None, **kw):
        reports = list(reports)
        auths = list(auths or [{"login": "ok"}])
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        kw.setdefault("say", self.said.append)
        return rc.ensure("claude", env=self.env,
                         discover_fn=lambda *_a, **_k: reports.pop(0) if len(reports) > 1 else reports[0],
                         auth_fn=lambda *_a, **_k: auths.pop(0) if len(auths) > 1 else auths[0],
                         sleep=sleep, now=lambda: clock[0], **kw)

    def test_usable_returns_path(self):
        self.assertEqual(self.ensure([self.report()]), (0, "/nonexistent/claude"))
        self.assertTrue(any("interrupted update" in m for m in self.said))

    def test_unusable_without_consent_stops_with_fix_and_resume_hint(self):
        calls = []
        code, path = self.ensure([self.report(False)], repair=True, consent=lambda _q: False,
                                 repair_fn=lambda *a, **k: calls.append(1),
                                 resume_hint="bash src/agent/start-cli.sh")
        self.assertEqual((code, path, calls), (rc.EXIT_UNUSABLE, None, []))
        text = "\n".join(self.said)
        self.assertIn("fix:", text)
        self.assertIn("Then run again: bash src/agent/start-cli.sh", text)

    def test_unusable_without_repair_offers_assisted_command(self):
        self.ensure([self.report(False)])
        self.assertTrue(any("let Sutando install" in m for m in self.said))

    def test_repair_with_consent_resumes_the_same_launch(self):
        code, path = self.ensure(
            [self.report(False), self.report()], repair=True, consent=lambda _q: True,
            repair_fn=lambda *a, **k: rc.RepairResult(True, "installed", "claude 2.3.0"))
        self.assertEqual((code, path), (0, "/nonexistent/claude"))
        self.assertTrue(any("repair succeeded" in m for m in self.said))

    def test_failed_repair_names_the_next_step(self):
        code, _ = self.ensure(
            [self.report(False)], repair=True, consent=lambda _q: True,
            repair_fn=lambda *a, **k: rc.RepairResult(False, "disk-full", "5 MB", "Free 600 MB"))
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertTrue(any("next: Free 600 MB" in m for m in self.said))

    def test_waits_for_the_user_fix_then_continues(self):
        code, _ = self.ensure([self.report(False), self.report(False), self.report()], wait_s=30)
        self.assertEqual(code, 0)
        self.assertTrue(any("continuing the launch" in m for m in self.said))

    def test_wait_times_out(self):
        self.assertEqual(self.ensure([self.report(False)], wait_s=12)[0], rc.EXIT_UNUSABLE)

    def test_newer_untested_warns_and_continues(self):
        self.assertEqual(self.ensure([self.report(status=rc.NEWER_UNTESTED)])[0], 0)

    def test_logged_out_enforced_stops(self):
        out = {"login": "logged-out", "remedy": "run: codex login", "detail": "not authenticated"}
        code, _ = self.ensure([self.report()], [out], enforce_auth=True)
        self.assertEqual(code, rc.EXIT_LOGIN)
        self.assertTrue(any("not authenticated" in m for m in self.said))

    def test_logged_out_advisory_continues(self):
        out = {"login": "logged-out", "remedy": "/login", "detail": "x"}
        self.assertEqual(self.ensure([self.report()], [out])[0], 0)

    def test_auth_recovery_waits_for_login_then_continues(self):
        out = {"login": "logged-out", "remedy": "run: codex login", "detail": "x"}
        code, _ = self.ensure([self.report()], [out, out, {"login": "ok"}], enforce_auth=True,
                              wait_s=60)
        self.assertEqual(code, 0)


class OwnerReplyBase(Base):
    """A temp workspace with tasks/, results/ and state/, and helpers for owner replies."""

    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir(parents=True)
        self.repairs = 0

    def notices(self):
        return sorted((self.ws / "results").glob("proactive-runtime-compat-*.txt"))

    def reply(self, text, name="task-100", tier="owner"):
        path = self.ws / "tasks" / f"{name}.txt"
        path.write_text(f"source: ag2space\nchannel_id: !dm:x\naccess_tier: {tier}\ntask: {text}\n")
        return path

    def ensure(self, runtime="claude", **kw):
        kw.setdefault("say", self.said.append)
        return rc.ensure(runtime, env=self.env, workspace=self.ws, notify_owner=True,
                         repair=True, consent=lambda _q: False, **kw)

    def counting_repair(self, *a, **k):
        self.repairs += 1
        return rc.assisted_install(*a, disk_usage=lambda _p: PLENTY, sleep=self.sleeps.append, **k)

    def incident(self, runtime="claude"):
        return json.loads((self.ws / "state" / f"runtime-compat-{runtime}.json").read_text())



class OwnerReply(OwnerReplyBase):
    """Off a terminal: one DM per incident, the owner's bare reply repairs, and the launch goes on."""

    def test_off_tty_queues_one_dm_per_incident(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        self.assertEqual(self.ensure()[0], rc.EXIT_UNUSABLE)
        self.assertEqual(self.ensure()[0], rc.EXIT_UNUSABLE)
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        body = notices[0].read_text()
        self.assertTrue(body.startswith("[dm-only]\n"))
        self.assertIn('Reply "install compatible claude"', body)
        self.assertIn(str(self.root), body)
        self.assertIn("claude.ai/install.sh", body)
        self.assertIn('"retry now"', body)
        self.assertEqual(self.incident()["notice"], notices[0].name)

    def test_dm_is_retried_under_the_same_name_until_queued(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        (self.ws / "results").rmdir()
        (self.ws / "results").write_text("not a directory")
        self.ensure()
        self.assertFalse(self.incident()["notice_queued"])
        name = self.incident()["notice"]
        (self.ws / "results").unlink()
        (self.ws / "results").mkdir()
        self.ensure()
        self.assertEqual([p.name for p in self.notices()], [name])
        self.assertTrue(self.incident()["notice_queued"])

    def test_owner_install_reply_repairs_answers_archives_and_continues(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        AssistedRepair.good_npm(self, "2.3.0")
        self.ensure(repair_fn=self.counting_repair)
        task = self.reply("Install compatible claude.")
        code, path = self.ensure(repair_fn=self.counting_repair)
        self.assertEqual((code, path), (0, str(self.root / "bin" / "claude")))
        self.assertEqual(self.repairs, 1)
        answer = (self.ws / "results" / task.name).read_text()
        self.assertIn("Installed claude 2.3.0", answer)
        self.assertFalse(task.exists())
        self.assertEqual(len(list((self.ws / "tasks" / "archive").rglob(task.name))), 1)
        self.assertEqual(self.incident()["status"], "resolved")
        self.assertEqual(len(self.notices()), 1)

    def test_reply_during_the_wait_continues_the_same_launch(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        AssistedRepair.good_npm(self, "2.3.0")
        clock = [0.0]

        def sleep(s):
            clock[0] += s
            if not (self.ws / "tasks" / "task-200.txt").exists():
                self.reply("install compatible claude", name="task-200")
        code, path = self.ensure(repair_fn=self.counting_repair, wait_s=60, sleep=sleep,
                                 now=lambda: clock[0])
        self.assertEqual(code, 0)
        self.assertEqual(self.repairs, 1)
        self.assertTrue((self.ws / "results" / "task-200.txt").exists())

    def test_a_reply_is_acted_on_once(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        AssistedRepair.good_npm(self)
        self.ensure()
        task = self.reply("install compatible claude")
        rec = self.incident()
        rec["actions"] = [{"task": task.stem, "phrase": "install compatible claude",
                           "applied": True, "reply": "recorded answer"}]
        (self.ws / "state" / "runtime-compat-claude.json").write_text(json.dumps(rec))
        self.assertEqual(self.ensure(repair_fn=self.counting_repair)[0], rc.EXIT_UNUSABLE)
        self.assertEqual(self.repairs, 0)
        self.assertEqual((self.ws / "results" / task.name).read_text(), "recorded answer\n")

    def test_retry_now_after_a_manual_fix(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        self.ensure()
        fake_cli(self.bin1 / "claude", "2.1.0")
        task = self.reply("retry now")
        self.assertEqual(self.ensure(repair_fn=self.counting_repair)[0], 0)
        self.assertEqual(self.repairs, 0)
        self.assertIn("works now", (self.ws / "results" / task.name).read_text())

    def test_failed_repair_is_answered_with_the_next_step(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        self.ensure()
        task = self.reply("install compatible claude")
        blocked = rc.RepairResult(False, "blocked-download", "ENOTFOUND", "Check your network")
        self.assertEqual(self.ensure(repair_fn=lambda *a, **k: blocked)[0], rc.EXIT_UNUSABLE)
        text = (self.ws / "results" / task.name).read_text()
        self.assertIn("That did not fix it", text)
        self.assertIn("Check your network", text)

    def test_retry_that_still_fails_says_why(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        self.ensure()
        task = self.reply("retry now")
        self.ensure()
        self.assertIn("still cannot start", (self.ws / "results" / task.name).read_text())

    def test_other_text_non_owner_and_no_incident_are_ignored(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        early = self.reply("install compatible claude", name="task-001")
        early_inc = rc.Incident(self.ws, "claude")
        early_inc.take_replies()
        self.assertEqual(early_inc.entries, [])
        self.ensure()
        early.unlink()
        self.reply("please install compatible claude", name="task-002")
        self.reply("install compatible claude", name="task-003", tier="guest")
        self.reply("install compatible codex", name="task-004")
        self.ensure(repair_fn=self.counting_repair)
        self.assertEqual(self.repairs, 0)
        self.assertEqual(sorted(p.name for p in (self.ws / "results").glob("task-*.txt")), [])

    def test_codex_login_dm_then_retry_now(self):
        fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, label="", login_rc=1)
        self.assertEqual(self.ensure("codex", enforce_auth=True)[0], rc.EXIT_LOGIN)
        body = self.notices()[0].read_text()
        self.assertIn("not logged in", body)
        self.assertIn("codex login", body)
        fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, label="")
        task = self.reply("retry now")
        self.assertEqual(self.ensure("codex", enforce_auth=True)[0], 0)
        self.assertIn("works now", (self.ws / "results" / task.name).read_text())
        self.assertEqual(self.incident("codex")["status"], "resolved")

    def test_owner_reply_helpers(self):
        self.assertEqual(owner_reply.normalize("  Retry   NOW!! "), "retry now")
        unreadable = self.ws / "tasks" / "task-9.txt"
        self.assertIsNone(owner_reply.owner_reply_text(unreadable))
        sys.path.insert(0, str(REPO / "src"))
        orig = owner_reply.Path.read_text

        def boom(self_, *a, **k):
            if self_.name == "task-8.txt":
                raise OSError("gone")
            return orig(self_, *a, **k)
        self.reply("retry now", name="task-8")
        owner_reply.Path.read_text = boom
        try:
            self.assertIsNone(owner_reply.owner_reply_text(self.ws / "tasks" / "task-8.txt"))
        finally:
            owner_reply.Path.read_text = orig
        self.assertTrue(str(owner_reply._ws(None)))

    def test_main_workspace_fallback(self):
        orig = rc._workspace
        self.assertTrue(rc._workspace() is None or rc._workspace().name)
        import workspace_default
        real = workspace_default.resolve_workspace

        def boom():
            raise RuntimeError("no config")
        workspace_default.resolve_workspace = boom
        try:
            self.assertIsNone(orig())
        finally:
            workspace_default.resolve_workspace = real


class NoNode(Base):
    """A clean machine: no npm and no Node on PATH, only the shell tools an installer uses."""

    def setUp(self):
        super().setUp()
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        for name in ("bash", "sh", "mkdir", "chmod", "cat", "cp", "dirname", "printf"):
            found = shutil.which(name)
            if found:
                os.symlink(found, str(self.tools / name))
        self.env["PATH"] = f"{self.bin1}:{self.tools}"
        self.bin1.mkdir()
        self.native = self.home / ".local" / "bin" / "claude"

    def fake_native_curl(self, version="2.4.0"):
        """curl stub that prints an installer script; bash runs it the way the docs pipe it."""
        script = (f'mkdir -p "$HOME/.local/bin"\n'
                  f"cat > \"$HOME/.local/bin/claude\" <<'EOS'\n#!/bin/sh\n"
                  f'case "$1" in --version) echo "{version} (Claude Code)";; --help) echo "{CLAUDE_FLAGS}";; esac\n'
                  "EOS\nchmod +x \"$HOME/.local/bin/claude\"\necho \"channel=$1\" >> \"$HOME/install.log\"\n")
        write_exe(self.bin1 / "curl", f'echo "$*" >> "$HOME/curl.log"\ncat <<\'EOF\'\n{script}EOF\n')

    def test_no_installer_at_all_gives_a_manual_step(self):
        s = rc.plan(self.discover(), root=self.root, env=self.env)
        self.assertIsNone(s.assisted)
        self.assertIn("claude.ai/install.sh", s.command)
        c = rc.plan(self.discover("codex"), root=self.root, env=self.env)
        self.assertIsNone(c.assisted)
        self.assertIn("chatgpt.com/codex/install.sh", c.command)
        self.assertIn("brew install --cask codex", c.command)

    def test_claude_plan_without_npm_uses_the_native_installer_and_names_what_it_touches(self):
        self.fake_native_curl()
        s = rc.plan(self.discover(), root=self.root, env=self.env)
        self.assertIsNotNone(s.assisted)
        self.assertIn(str(self.native), s.assisted_touches)
        self.assertIn(str(self.home / ".local" / "share" / "claude"), s.assisted_touches)
        self.assertIn(str(self.root), s.assisted_touches)

    def test_claude_native_install_without_node(self):
        self.fake_native_curl("2.4.0")
        r = self.install()
        self.assertTrue(r.ok, r)
        self.assertEqual(os.readlink(self.root / "bin" / "claude"), str(self.native))
        self.assertIn("https://claude.ai/install.sh", (self.home / "curl.log").read_text())
        self.assertIn("channel=stable", (self.home / "install.log").read_text())
        self.assertEqual(self.discover().selected.version, "2.4.0")

    def test_native_install_that_does_not_run_is_not_success(self):
        self.fake_native_curl("1.0.0")
        r = self.install()
        self.assertEqual(r.reason, "verify-failed")
        self.assertFalse(os.path.lexists(self.root / "bin" / "claude"))

    def test_native_install_blocked_download(self):
        write_exe(self.bin1 / "curl", 'echo "curl: (6) Could not resolve host: claude.ai" >&2; exit 6\n')
        r = self.install(attempts=2)
        self.assertEqual((r.reason, r.attempts), ("blocked-download", 2))
        self.assertIn("vendor's download site", r.user_action)

    def test_codex_without_npm_uses_homebrew_when_present(self):
        write_exe(self.bin1 / "brew", f"""[ "$*" = "install --cask codex" ] || exit 2
d="$(dirname "$0")"
printf '#!/bin/sh\\ncase "$1" in --version) echo "codex-cli 0.160.1";; --help) echo "{CODEX_FLAGS}";; esac\\n' > "$d/codex"
chmod +x "$d/codex"
""")
        s = rc.plan(self.discover("codex"), root=self.root, env=self.env)
        self.assertIn("Homebrew", s.assisted_touches)
        r = self.install("codex")
        self.assertTrue(r.ok, r)
        self.assertEqual(os.readlink(self.root / "bin" / "codex"), str(self.bin1 / "codex"))


class ConcurrentRepair(Base):
    def test_two_racing_repairs_install_once_and_both_see_the_result(self):
        import threading
        count = self.tmp / "npm-runs"
        AssistedRepair.good_npm(self, "2.3.0")
        body = (self.bin1 / "npm").read_text()
        (self.bin1 / "npm").write_text(body.replace(
            '\nmkdir -p "$PREFIX/bin"', f'\necho run >> "{count}"; sleep 1\nmkdir -p "$PREFIX/bin"', 1))
        results = []

        def go():
            results.append(rc.assisted_install("claude", env=self.env, disk_usage=lambda _p: PLENTY,
                                               sleep=lambda _s: None))
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results), results)
        self.assertEqual(count.read_text().count("run"), 1)
        self.assertEqual(results[0].path, results[1].path)


class Crash(BaseException):
    """Stands in for the process dying at a step boundary: nothing catches it."""


class DurableReplies(OwnerReplyBase):
    """Each step of a reply is recorded as it lands; a crash replays only what did not."""

    def open_incident(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        AssistedRepair.good_npm(self, "2.3.0")
        self.ensure()
        return self.reply("install compatible claude")

    def crash_on(self, step):
        """The step's work lands, then the process dies before its mark is saved."""
        orig = rc.Incident.mark

        def mark(inc, task, s):
            if s == step:
                rc.Incident.mark = orig
                raise Crash()
            orig(inc, task, s)
        rc.Incident.mark = mark
        self.addCleanup(setattr, rc.Incident, "mark", orig)

    def answers(self, task):
        found = list((self.ws / "results").rglob(f"{task.stem}.txt"))
        return [p.read_text() for p in found]

    def assert_done_once(self, task):
        rec = self.incident()["actions"]
        self.assertEqual([(e["task"], e.get("applied"), e.get("answered"), e.get("archived")) for e in rec],
                         [(task.stem, True, True, True)])
        self.assertEqual(len(self.answers(task)), 1)
        self.assertFalse(task.exists())

    def test_crash_after_the_reply_is_recorded_reruns_only_the_action(self):
        task = self.open_incident()

        def dying(*a, **k):
            raise Crash()
        with self.assertRaises(Crash):
            self.ensure(repair_fn=dying)
        self.assertEqual(self.incident()["actions"][0].get("applied"), None)
        self.assertEqual(self.answers(task), [])
        self.assertEqual(self.ensure(repair_fn=self.counting_repair)[0], 0)
        self.assertEqual(self.repairs, 1)
        self.assert_done_once(task)

    def test_crash_after_apply_does_not_repair_again(self):
        task = self.open_incident()
        orig = owner_reply.finish

        def dying(*a, **k):
            raise Crash()
        owner_reply.finish = dying
        try:
            with self.assertRaises(Crash):
                self.ensure(repair_fn=self.counting_repair)
        finally:
            owner_reply.finish = orig
        self.assertTrue(self.incident()["actions"][0]["applied"])
        self.ensure(repair_fn=self.counting_repair)
        self.assertEqual(self.repairs, 1)
        self.assert_done_once(task)
        self.assertIn("Installed claude 2.3.0", self.answers(task)[0])

    def test_crash_after_the_answer_is_written_never_sends_it_twice(self):
        task = self.open_incident()
        self.crash_on("answered")
        with self.assertRaises(Crash):
            self.ensure(repair_fn=self.counting_repair)
        first = self.answers(task)
        self.assertEqual(len(first), 1)
        (self.ws / "results" / f"{task.stem}.txt").write_text("delivered copy\n")
        self.ensure(repair_fn=self.counting_repair)
        self.assert_done_once(task)
        self.assertEqual(self.answers(task), ["delivered copy\n"])

    def test_crash_after_answered_mark_archives_on_replay(self):
        task = self.open_incident()
        self.crash_on("archived")
        with self.assertRaises(Crash):
            self.ensure(repair_fn=self.counting_repair)
        self.ensure(repair_fn=self.counting_repair)
        self.assert_done_once(task)

    def test_answer_delivered_and_archived_by_the_bridge_is_not_resent(self):
        task = self.open_incident()
        self.crash_on("answered")
        with self.assertRaises(Crash):
            self.ensure(repair_fn=self.counting_repair)
        live = self.ws / "results" / f"{task.stem}.txt"
        month = self.ws / "results" / "archive" / "2026-10"
        month.mkdir(parents=True)
        live.rename(month / live.name)
        self.ensure(repair_fn=self.counting_repair)
        self.assertFalse(live.exists())
        self.assert_done_once(task)

    def test_reply_after_the_wait_window_is_handled_on_the_next_launch(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        AssistedRepair.good_npm(self, "2.3.0")
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        self.assertEqual(self.ensure(wait_s=600, sleep=sleep, now=lambda: clock[0])[0],
                         rc.EXIT_UNUSABLE)
        self.assertGreaterEqual(clock[0], 600)
        task = self.reply("install compatible claude", name="task-700")
        self.assertEqual(self.ensure(repair_fn=self.counting_repair)[0], 0)
        self.assert_done_once(task)

    def test_unfinished_reply_survives_a_new_incident(self):
        task = self.open_incident()
        orig = owner_reply.finish

        def dying(*a, **k):
            raise Crash()
        owner_reply.finish = dying
        try:
            with self.assertRaises(Crash):
                self.ensure(repair_fn=self.counting_repair)
        finally:
            owner_reply.finish = orig
        inc = rc.Incident(self.ws, "claude")
        inc.close()
        inc.notify("a new incident")
        self.assertEqual([e["task"] for e in rc.Incident(self.ws, "claude").entries], [task.stem])


class PublishFaults(Base):
    """Fault at every publication boundary: the old version and the link always survive."""

    npm = AssistedRepair.npm
    good_npm = AssistedRepair.good_npm
    install = Base.install

    def setUp(self):
        super().setUp()
        self.old = self.managed_version("2.1.0")
        self.stale = self.root / "claude" / "versions" / "2.3.0"
        write_exe(self.stale / "bin" / "claude", "echo stale; exit 1\n")
        self.good_npm("2.3.0")

    def assert_intact(self):
        link = self.root / "bin" / "claude"
        self.assertEqual(os.readlink(link), str(self.old))
        self.assertTrue(os.path.exists(link), "dangling managed link")
        self.assertEqual(self.discover().selected.version, "2.1.0")
        self.assertIn("echo stale", (self.stale / "bin" / "claude").read_text())
        leftovers = [p.name for p in (self.root / "claude").iterdir() if p.name.startswith((".staging", ".replaced", ".swap"))]
        self.assertEqual(leftovers, [])

    def fail_nth_rename(self, n, after=False):
        calls = []
        real = rc._rename

        def rename(a, b):
            calls.append(1)
            if len(calls) == n:
                if after:
                    real(a, b)
                raise Crash()
            return real(a, b)
        rc._rename = rename
        self.addCleanup(setattr, rc, "_rename", real)

    def test_fault_after_staging(self):
        real = rc.probe

        def probe(path, spec, **kw):
            if kw.get("source") == "staged":
                raise Crash()
            return real(path, spec, **kw)
        rc.probe = probe
        try:
            with self.assertRaises(Crash):
                self.install()
        finally:
            rc.probe = real
        self.assert_intact()

    def test_fault_after_the_old_folder_is_moved_aside(self):
        self.fail_nth_rename(1, after=True)
        with self.assertRaises(Crash):
            self.install()
        self.assert_intact()

    def test_fault_renaming_the_new_folder_into_place(self):
        self.fail_nth_rename(2)
        with self.assertRaises(Crash):
            self.install()
        self.assert_intact()

    def test_fault_after_the_new_folder_is_in_place(self):
        self.fail_nth_rename(2, after=True)
        with self.assertRaises(Crash):
            self.install()
        self.assert_intact()

    def test_fault_swapping_the_link(self):
        real = rc._swap_link
        calls = []

        def swap(link, target):
            calls.append(1)
            if len(calls) == 1:
                raise Crash()
            return real(link, target)
        rc._swap_link = swap
        try:
            with self.assertRaises(Crash):
                self.install()
        finally:
            rc._swap_link = real
        self.assert_intact()

    def test_fault_in_the_live_probe(self):
        real = rc.probe

        def probe(path, spec, **kw):
            if kw.get("source") == "managed":
                raise Crash()
            return real(path, spec, **kw)
        rc.probe = probe
        try:
            with self.assertRaises(Crash):
                self.install()
        finally:
            rc.probe = real
        self.assert_intact()

    def test_live_probe_fails_restores_old_folder_and_link(self):
        real = rc.probe

        def probe(path, spec, **kw):
            c = real(path, spec, **kw)
            if kw.get("source") == "managed":
                c.status, c.detail = rc.BROKEN, "would not start"
            return c
        rc.probe = probe
        try:
            r = self.install()
        finally:
            rc.probe = real
        self.assertEqual(r.reason, "verify-failed")
        self.assert_intact()

    def test_process_killed_mid_swap_is_undone_by_the_next_run(self):
        real_undo = rc._undo

        def dead_undo(*a, **k):
            raise Crash()
        rc._undo = dead_undo
        self.fail_nth_rename(2, after=True)
        try:
            with self.assertRaises(Crash):
                self.install()
        finally:
            rc._undo = real_undo
        self.assertTrue(list((self.root / "claude").glob(".swap-*.json")))
        done = rc.recover_swaps(self.root, "claude", pid_alive=lambda _p: False, log=self.said.append)
        self.assertEqual(len(done), 1)
        self.assert_intact()

    def kill_inside_undo(self, boundary):
        """Fail the live probe so _undo runs, then kill the process at `boundary` inside it."""
        state = {"in_undo": False}
        real_undo, real_probe = rc._undo, rc.probe
        seams = {"phase": "_write_journal", "link": "_swap_link", "drop_new": "_rmtree",
                 "restore_old": "_rename"}
        name = seams[boundary]
        real_seam = getattr(rc, name)

        def seam(*a, **k):
            out = real_seam(*a, **k)
            if state["in_undo"]:
                raise Crash()
            return out

        def undo(*a, **k):
            state["in_undo"] = True
            try:
                return real_undo(*a, **k)
            finally:
                state["in_undo"] = False

        def probe(path, spec, **kw):
            if kw.get("source") == "managed":
                raise Crash()
            return real_probe(path, spec, **kw)
        return {"_undo": undo, "probe": probe, name: seam}

    def patched(self, patches):
        saved = {k: getattr(rc, k) for k in patches}
        for k, v in patches.items():
            setattr(rc, k, v)
        return saved

    def recover(self):
        return rc.recover_swaps(self.root, "claude", pid_alive=lambda _p: False, log=self.said.append)

    def assert_undo_survives(self, boundary):
        saved = self.patched(self.kill_inside_undo(boundary))
        try:
            with self.assertRaises(Crash):
                self.install()
        finally:
            for k, v in saved.items():
                setattr(rc, k, v)
        self.assertTrue(list((self.root / "claude").glob(".swap-*.json")), "the journal must survive the kill")
        saved = self.patched({k: v for k, v in self.kill_inside_undo(boundary).items() if k != "probe"})
        try:
            try:
                self.recover()
            except Crash:
                pass
        finally:
            for k, v in saved.items():
                setattr(rc, k, v)
        self.recover()
        self.recover()
        self.assert_intact()

    def kill_at_journal_delete(self):
        """The rollback finished its file moves; the process dies as it deletes the journal."""
        real_unlink = Path.unlink
        fired = []

        def unlink(p, *a, **k):
            if p.name.startswith(".swap-") and p.suffix == ".json" and not fired:
                fired.append(1)
                raise Crash()
            return real_unlink(p, *a, **k)
        Path.unlink = unlink
        self.addCleanup(setattr, Path, "unlink", real_unlink)
        return fired

    def test_owner_repro_undo_killed_before_the_journal_delete_then_two_recoveries(self):
        real_probe = rc.probe

        def probe(path, spec, **kw):
            if kw.get("source") == "managed":
                raise Crash()
            return real_probe(path, spec, **kw)
        rc.probe = probe
        fired = self.kill_at_journal_delete()
        try:
            with self.assertRaises(Crash):
                self.install()
        finally:
            rc.probe = real_probe
        self.assertEqual(fired, [1])
        self.assertTrue((self.stale / "bin" / "claude").exists(), "the original is back before recovery")
        self.recover()
        self.recover()
        self.assert_intact()
        self.assertEqual(list((self.root / "claude").glob(".swap-*")), [])

    def test_undo_killed_after_the_phase_is_written_then_recovered_twice(self):
        self.assert_undo_survives("phase")

    def test_undo_killed_after_the_link_is_restored_then_recovered_twice(self):
        self.assert_undo_survives("link")

    def test_undo_killed_after_the_new_folder_is_removed_then_recovered_twice(self):
        self.assert_undo_survives("drop_new")

    def test_undo_killed_after_the_old_folder_is_restored_then_recovered_twice(self):
        self.assert_undo_survives("restore_old")

    def test_committed_swap_left_by_a_killed_process_is_finished(self):
        self.assertTrue(self.install().ok)
        journal = self.root / "claude" / ".swap-999999-1.json"
        aside = self.root / "claude" / ".replaced-999999-1-2.0.0"
        aside.mkdir()
        journal.write_text(json.dumps({"previous": None, "final": None, "aside": str(aside),
                                       "placed": False, "phase": "committed"}))
        bad = self.root / "claude" / ".swap-999998-1.json"
        bad.write_text("{not json")
        rc.recover_swaps(self.root, "claude", pid_alive=lambda _p: False)
        self.assertFalse(aside.exists() or journal.exists() or bad.exists())
        self.assertEqual(self.discover().selected.version, "2.3.0")
        live = self.root / "claude" / f".swap-{os.getpid()}-1.json"
        live.write_text("{}")
        self.assertEqual(rc.recover_swaps(self.root, "claude"), [])
        self.assertEqual(rc.recover_swaps(self.root, "codex"), [])


class Cli(Base):
    def main(self, *argv, consent=lambda _q: False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main(list(argv), env=self.env, consent=consent)
        return code, out.getvalue(), err.getvalue()

    def test_doctor_text_and_json(self):
        fake_cli(self.bin1 / "claude", "1.0.0")
        fake_cli(self.bin2 / "claude", "2.1.0")
        fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, login_rc=1, label="")
        (self.root / "codex" / ".staging-999999-1").mkdir(parents=True)
        code, out, _ = self.main("doctor")
        self.assertEqual(code, 0)
        self.assertIn("* [ok]", out)
        self.assertIn("[too-old]", out)
        self.assertIn("login:", out)
        self.assertIn("interrupted update", out)
        code, out, _ = self.main("doctor", "--runtime", "claude", "--json")
        self.assertEqual(json.loads(out)[0]["plan"]["status"], rc.OK)

    def test_doctor_unusable_exits_3_with_assisted_fix(self):
        code, out, _ = self.main("doctor", "--runtime", "claude")
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertIn("assisted fix", out)

    def test_repair_requires_consent(self):
        self.assertEqual(self.main("repair", "--runtime", "claude")[0], rc.EXIT_USAGE)

    def test_repair_reports_failure(self):
        self.env["PATH"] = str(self.tmp / "empty")
        code, _, err = self.main("repair", "--runtime", "claude", "--yes")
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertIn("no-installer", err)

    def test_repair_success(self):
        AssistedRepair.good_npm(self)
        old = rc.shutil.disk_usage
        rc.shutil.disk_usage = lambda _p: PLENTY
        try:
            code, _, err = self.main("repair", "--runtime", "claude", "--yes")
        finally:
            rc.shutil.disk_usage = old
        self.assertEqual(code, 0, err)

    def test_ensure_prints_selected_path(self):
        p = fake_cli(self.bin1 / "claude")
        code, out, _ = self.main("ensure", "--runtime", "claude")
        self.assertEqual((code, out.strip()), (0, p))

    def test_ensure_consent_env_runs_the_repair(self):
        AssistedRepair.good_npm(self)
        self.env["SUTANDO_RUNTIME_REPAIR_CONSENT"] = "1"
        old = rc.shutil.disk_usage
        rc.shutil.disk_usage = lambda _p: PLENTY
        try:
            code, out, err = self.main("ensure", "--runtime", "claude", "--repair")
        finally:
            rc.shutil.disk_usage = old
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), str(self.root / "bin" / "claude"))

    def test_ensure_unusable_exit_code(self):
        self.assertEqual(self.main("ensure", "--runtime", "codex")[0], rc.EXIT_UNUSABLE)


class HealthCheck(unittest.TestCase):
    """health-check reports the same verdict and names the fix."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hc_compat", REPO / "src" / "health-check.py")
        cls.hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hc)

    def check(self, report, runtime="claude"):
        return self.hc.check_core_runtime_compat(runtime, discover=lambda *_a, **_k: report)

    def test_ok_warn_down(self):
        ok = rc.Candidate("/nonexistent/claude", "PATH", rc.OK, "2.1.0")
        self.assertEqual(self.check(rc.Report("claude", [ok], ok))["status"], "ok")
        new = rc.Candidate("/nonexistent/claude", "PATH", rc.NEWER_UNTESTED, "3.0.0", detail="newer")
        r = self.check(rc.Report("claude", [new], new))
        self.assertEqual(r["status"], "warn")
        self.assertIn("Fix:", r["detail"])
        down = self.check(rc.Report("claude", [], None))
        self.assertEqual(down["status"], "down")
        self.assertIn("claude.ai/install.sh", down["detail"])
        self.assertIn("--yes", down["detail"])

    def test_unknown_and_unresolved_runtime(self):
        self.assertEqual(self.check(None, runtime="agy")["status"], "warn")

        def boom(*_a, **_k):
            raise ValueError("bad config")
        orig, self.hc.resolve_core_runtime = self.hc.resolve_core_runtime, boom
        try:
            r = self.hc.check_core_runtime_compat()
        finally:
            self.hc.resolve_core_runtime = orig
        self.assertIn("bad config", r["detail"])

    def test_registered_in_run_checks(self):
        self.assertIn("checks.append(check_core_runtime_compat())",
                      (REPO / "src" / "health-check.py").read_text())


AssistedRepair.good_npm.__doc__ = "Shared with Cli: an npm stub that installs a working CLI."
Cli.npm = OwnerReplyBase.npm = ConcurrentRepair.npm = AssistedRepair.npm


if __name__ == "__main__":
    unittest.main(verbosity=1)
