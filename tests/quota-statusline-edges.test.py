#!/usr/bin/env python3
"""Error and refusal paths of the status line quota recorder: the writer, the account
helpers, the session-config hook runner, the installer, the recorder and StopFailure
scripts. Everything runs in-process.

Run: python3 tests/quota-statusline-edges.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "quota-tracker" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "src"))  # ahead of the skill's quota_availability shim

import claude_account
import quota_record as qr
import session_config_hooks as sch
import statusline_config as cfg

T0 = 1795176000
P = "anthropic-ratelimit-unified-"


def _status(five=42, model="claude-opus-5-5"):
    rl = {"five_hour": {"used_percentage": five, "resets_at": T0 + 4 * 3600}}
    st = {"rate_limits": rl}
    if model:
        st["model"] = {"id": model, "display_name": "Opus"}
    return st


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.ws = self.d / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.core = self.d / "core"
        self.core.mkdir()
        (self.core / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-A"}}))
        self.core_patch = mock.patch.object(claude_account, "core_config_dir", return_value=str(self.core))
        self.core_patch.start()
        self.addCleanup(self.core_patch.stop)


class Writer(Tmp):
    def test_parse_iso_rejects_garbage(self):
        self.assertIsNone(qr._parse_iso("not a date"))
        self.assertIsNone(qr._parse_iso(None))

    def test_an_overage_record_is_not_a_rejection(self):
        self.assertIsNone(qr.rejected_at({"limit_state": "overage", "headers": {P + "status": "rejected"}}))

    def test_a_proxy_rejection_without_numbers_compares_against_its_headers(self):
        prev = {"limit_state": "rejected", "last_checked": qr.iso(T0),
                "headers": {P + "status": "rejected", P + "5h-utilization": "1", P + "5h-reset": str(T0 + 4 * 3600)}}
        held = qr.record_from_statusline(prev, _status(five=100), T0 + 10)
        self.assertEqual(held["limit_state"], "rejected")
        clear = qr.record_from_statusline(prev, _status(five=4), T0 + 20)
        self.assertEqual(clear["limit_state"], "allowed")

    def test_an_older_observation_returns_no_record(self):
        self.assertIsNone(qr.record_from_statusline({"last_checked": qr.iso(T0)}, _status(), T0 - 1))

    def test_a_status_without_a_model_keeps_the_last_request(self):
        prev = {"last_request": {"model": "claude-x", "at": qr.iso(T0)}}
        out = qr.record_from_statusline(prev, _status(model=None), T0 + 1)
        self.assertEqual(out["last_request"]["model"], "claude-x")

    def test_a_missing_transcript_falls_back_to_now(self):
        st = dict(_status(), transcript_path=str(self.d / "gone.jsonl"))
        self.assertEqual(qr.observed_at(st, now=T0), T0)

    def test_a_failed_write_leaves_no_temp_file(self):
        target = self.ws / "state" / "x.json"
        with self.assertRaises(TypeError):
            qr.write_atomic(target, {"bad": object()})
        self.assertEqual([p.name for p in target.parent.iterdir()], [])

    def test_a_held_lock_skips_the_observation(self):
        from file_lock import locked_file
        lock = qr.quota_path(self.ws).with_name(qr.QUOTA_FILE + ".lock")
        with mock.patch.object(qr, "LOCK_WAIT_S", 0.1):
            with locked_file(lock):
                result = []
                t = threading.Thread(target=lambda: result.append(qr.record_statusline(
                    self.ws, _status(), now=T0, config_dir=str(self.core), core_dir=str(self.core))))
                t.start()
                t.join(5)
        self.assertEqual(result, [False])
        self.assertFalse(qr.quota_path(self.ws).exists())

    def test_the_accounts_map_is_bounded(self):
        prev = {"accounts": {f"k{i:02d}": {"last_checked": qr.iso(T0 + i)} for i in range(qr.MAX_ACCOUNTS)}}
        out = qr.route(prev, "new", False, lambda e: {"last_checked": qr.iso(T0 + 99)})
        self.assertEqual(len(out["accounts"]), qr.MAX_ACCOUNTS)
        self.assertNotIn("k00", out["accounts"])
        self.assertIn("new", out["accounts"])


class Accounts(Tmp):
    def test_fingerprint_refusals(self):
        self.assertIsNone(claude_account.account_fingerprint(None))
        (self.d / "a").mkdir()
        (self.d / "a" / ".claude.json").write_text(json.dumps({"oauthAccount": "x"}))
        self.assertIsNone(claude_account.account_fingerprint(str(self.d / "a")))
        (self.d / "a" / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ""}}))
        self.assertIsNone(claude_account.account_fingerprint(str(self.d / "a")))
        self.assertEqual(claude_account.account_key(None), "unscoped")
        self.assertFalse(claude_account.is_identified("unscoped"))

    def test_core_config_dir_unknown_when_the_resolver_fails(self):
        self.core_patch.stop()
        import sutando_config
        with mock.patch.object(sutando_config, "resolve_claude_sutando_config_dir", side_effect=RuntimeError):
            self.assertIsNone(claude_account.core_config_dir())
        with mock.patch.object(sutando_config, "resolve_claude_sutando_config_dir", return_value=self.core):
            self.assertEqual(claude_account.core_config_dir(), str(self.core))


class _Proc:
    def __init__(self, pid=999999):
        self.pid, self.returncode, self.killed = pid, None, False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)


class SessionConfigHooks(Tmp):
    def test_kill_group_tolerates_a_gone_group_and_a_stuck_wait(self):
        sch._kill_group(_Proc())  # no such group; wait times out
        with mock.patch.object(sch, "WINDOWS", True):
            p = _Proc()
            sch._kill_group(p)
            self.assertTrue(p.killed)

    def test_budget_spent_and_unstartable_hooks_are_skipped(self):
        hook = self.d / "h.py"
        hook.write_text("")
        self.assertEqual(sch.run([("a", hook)], str(self.d), sys.executable, deadline=time.monotonic() - 1), [])
        self.assertEqual(sch.run([("a", hook)], str(self.d), str(self.d / "no-python"),
                                 deadline=time.monotonic() + 10), [])

    def test_discover_finds_declarers_and_survives_a_failure(self):
        found = []
        sch._discover(found)
        self.assertIsNone(found[-1])
        self.assertIn("quota-tracker", [n for n, _p in found[:-1]])
        import skill_roots
        found = []
        with mock.patch.object(skill_roots, "declared_scripts", side_effect=RuntimeError):
            sch._discover(found)
        self.assertEqual(found, [])

    def test_main(self):
        self.assertEqual(sch.main([]), 0)
        calls = []
        hooks = [("s", self.d / "s.py")]
        with mock.patch.object(sch, "_discover", side_effect=lambda f: f.extend([*hooks, None])), \
                mock.patch.object(sch, "run", side_effect=lambda h, c, py, dl: calls.append((h, c))):
            self.assertEqual(sch.main(["--config-dir", str(self.core)]), 0)
        self.assertEqual(calls, [(hooks, str(self.core))])
        calls.clear()
        with mock.patch.object(sch, "_discover", side_effect=lambda f: time.sleep(1)), \
                mock.patch.object(sch, "DISCOVERY_SECONDS", 0.1), \
                mock.patch.object(sch, "run", side_effect=lambda *a: calls.append(a)):
            self.assertEqual(sch.main(["--config-dir", str(self.core)]), 0)
        self.assertEqual(calls, [])


class Installer(Tmp):
    def test_config_fallbacks(self):
        bad = self.d / "manifest.json"
        bad.write_text("{nope")
        self.assertIsNone(cfg.manifest_config("X", bad))
        bare = self.d / "bare.json"
        bare.write_text("{}")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUOTA_STATUSLINE_ENABLED", None)
            self.assertTrue(cfg.enabled(bare))
        self.assertTrue(Path(cfg.workspace()).is_absolute())

    def test_a_failed_settings_write_leaves_no_temp_file(self):
        with self.assertRaises(TypeError):
            cfg._write_json(self.core / "settings.json", {"bad": object()})
        self.assertFalse(any(p.name.startswith(".settings.json.") for p in self.core.iterdir()))

    def test_a_shared_stopfailure_group_keeps_the_foreign_hook(self):
        cfg.install(str(self.core), True)
        st = json.loads((self.core / "settings.json").read_text())
        st["hooks"]["StopFailure"][0]["hooks"].append({"type": "command", "command": "notify"})
        (self.core / "settings.json").write_text(json.dumps(st))
        cfg.install(str(self.core), False)
        st = json.loads((self.core / "settings.json").read_text())
        self.assertEqual(st["hooks"]["StopFailure"][0]["hooks"], [{"type": "command", "command": "notify"}])

    def test_opt_out_without_a_saved_line_removes_the_recorder(self):
        cfg.install(str(self.core), True)
        self.assertEqual(cfg.install(str(self.core), False), "removed")
        self.assertNotIn("statusLine", json.loads((self.core / "settings.json").read_text()))


class Scripts(Tmp):
    def _run(self, path, data: bytes):
        mod = _load(path.stem.replace("-", "_"), path)
        out = io.BytesIO()
        fake_out = io.TextIOWrapper(out, write_through=True)
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(data))), \
                mock.patch.object(sys, "stdout", fake_out):
            mod.main()
            fake_out.flush()
        return mod, out.getvalue().decode()

    def test_recorder_survives_a_failing_record_passthrough_and_print(self):
        raw = json.dumps(_status()).encode()
        with mock.patch.object(qr, "record_statusline", side_effect=RuntimeError), \
                mock.patch.object(cfg, "workspace", return_value=self.ws), \
                mock.patch.object(cfg, "passthrough_command", return_value="whatever"), \
                mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 5)):
            _mod, line = self._run(SCRIPTS / "statusline-record.py", raw)
        self.assertIn("5h 42%", line)
        path = SCRIPTS / "statusline-record.py"
        mod = _load("statusline_record_print", path)
        with mock.patch.object(mod, "_short_line", side_effect=RuntimeError), \
                mock.patch.object(cfg, "workspace", return_value=self.ws), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw))):
            mod.main()  # nothing raised

    def test_stopfailure_survives_a_failing_record(self):
        with mock.patch.object(qr, "record_rate_limit", side_effect=RuntimeError), \
                mock.patch.object(cfg, "workspace", return_value=self.ws):
            self._run(SCRIPTS / "stopfailure-record.py", json.dumps({"error": "rate_limit"}).encode())


class InstallHook(Tmp):
    def test_main_installs_and_survives_a_failure(self):
        mod = _load("install_statusline_edges", SCRIPTS / "install-statusline.py")
        with mock.patch.object(sys, "stderr", io.StringIO()) as err, \
                mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": "true"}):
            self.assertEqual(mod.main(["--config-dir", str(self.core)]), 0)
            self.assertIn("installed", err.getvalue())
            with mock.patch.object(cfg, "install", side_effect=RuntimeError("boom")):
                self.assertEqual(mod.main(["--config-dir", str(self.core)]), 0)
            self.assertIn("not installed", err.getvalue())


class SignedIn(Tmp):
    def setUp(self):
        super().setUp()
        import claude_signed_in as csi
        self.csi = csi
        self.calls = []
        env = mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)

    def _runner(self, rc=0, out='{"loggedIn": true}', exc=None):
        def run(cmd, **kw):
            self.calls.append((cmd, kw.get("env")))
            if exc:
                raise exc
            return types.SimpleNamespace(returncode=rc, stdout=out)
        return run

    def _results(self):
        d = self.ws / "results"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def test_probe_requires_exit_zero_and_a_literal_true(self):
        env = {"PATH": "/bin"}
        self.assertIs(self.csi.signed_in(env, self._runner()), True)
        self.assertIs(self.csi.signed_in(env, self._runner(rc=1, out='{"loggedIn": true}')), False)
        self.assertIs(self.csi.signed_in(env, self._runner(out='{"loggedIn": "true"}')), False)
        self.assertIs(self.csi.signed_in(env, self._runner(out='{"loggedIn": 1}')), False)
        self.assertIs(self.csi.signed_in(env, self._runner(out="garbage")), False)
        self.assertIs(self.csi.signed_in(env, self._runner(exc=subprocess.TimeoutExpired("claude", 8))), False)
        self.assertIsNone(self.csi.signed_in(env, self._runner(exc=FileNotFoundError())))

    def test_probe_env_is_the_panes_without_the_proxy(self):
        launcher = {"PATH": "/bin", "ANTHROPIC_API_KEY": "launcher-only", "ANTHROPIC_BASE_URL": "http://localhost:7846"}
        pane = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "pane-only", "ANTHROPIC_BASE_URL": "http://127.0.0.1:7846/"}
        env = self.csi.direct_auth_env(launcher, str(self.core), pane)
        # The pane's environment, not the launcher's, and no environment login at all.
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.core))
        custom = self.csi.direct_auth_env({"ANTHROPIC_BASE_URL": "http://gw.example:1"}, str(self.core))
        self.assertEqual(custom["ANTHROPIC_BASE_URL"], "http://gw.example:1")
        tmux = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="A=1\n-B\nC=x=y\n")
        self.assertEqual(self.csi.pane_env("/sock", tmux), {"A": "1", "C": "x=y"})
        self.assertIsNone(self.csi.pane_env(None))
        self.assertIsNone(self.csi.pane_env("/sock", lambda a, **k: types.SimpleNamespace(returncode=1, stdout="")))
        self.assertIsNone(self.csi.pane_env("/sock", lambda a, **k: (_ for _ in ()).throw(OSError())))

    def test_an_environment_login_alone_is_not_signed_in(self):
        def claude(cmd, **kw):  # reports signed in whenever any environment login reaches it
            e = kw.get("env") or {}
            ok = any(e.get(k) for k in self.csi.ENV_LOGINS)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": ok}))
        for key in self.csi.ENV_LOGINS:
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: "set"}):
                row = self.csi.check(self.ws, str(self.core), runner=claude, now=T0)
                self.assertEqual(row["status"], "fail", row)

    def test_not_signed_in_fails_and_notifies_once_a_day(self):
        tmux = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="PATH=/bin\n")
        run = self._runner(rc=1, out='{"loggedIn": false}')
        row = self.csi.check(self.ws, str(self.core), socket="/sock", runner=run, tmux_runner=tmux, now=T0)
        self.assertEqual(row["status"], "fail")
        self.assertIn("claude auth login", row["detail"])
        self.assertIn(str(self.core), row["detail"])
        self.assertEqual(len(self._results()), 1)
        body = (self.ws / "results" / self._results()[0]).read_text()
        self.assertIn(self.csi.sign_in_command(str(self.core)), body)
        self.assertEqual(self.calls[-1][1]["CLAUDE_CONFIG_DIR"], str(self.core))
        self.csi.check(self.ws, str(self.core), runner=run, now=T0 + 3600)
        self.assertEqual(len(self._results()), 1, "deduped within the day")
        self.csi.check(self.ws, str(self.core), runner=run, now=T0 + 25 * 3600)
        self.assertEqual(len(self._results()), 2, "asked again the next day")

    def test_signed_in_and_unknown_raise_nothing(self):
        row = self.csi.check(self.ws, str(self.core), runner=self._runner(), now=T0)
        self.assertEqual(row["status"], "ok")
        row = self.csi.check(self.ws, str(self.core), runner=self._runner(exc=FileNotFoundError()), now=T0)
        self.assertEqual(row["status"], "ok")
        self.assertIn("not found", row["detail"])
        self.assertEqual(self._results(), [])
        self.assertIn("could not be resolved", self.csi.check(self.ws, None, runner=self._runner())["detail"])

    def test_records_are_per_config_dir_so_one_never_erases_another(self):
        other = self.d / "other"
        other.mkdir()
        self.csi.check(self.ws, str(self.core), runner=self._runner(rc=1), now=T0)
        self.csi.check(self.ws, str(other), runner=self._runner(), now=T0 + 1)
        self.assertIs(self.csi.read_record(self.ws, str(self.core))["signed_in"], False)
        self.assertIs(self.csi.read_record(self.ws, str(other))["signed_in"], True)
        self.assertNotEqual(self.csi.record_path(self.ws, str(self.core)), self.csi.record_path(self.ws, str(other)))
        self.csi.record_path(self.ws, str(self.core)).write_text("[]")
        self.assertEqual(self.csi.read_record(self.ws, str(self.core)), {})

    def test_proxy_routing_needs_no_own_sign_in(self):
        with mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": "false"}):
            row = self.csi.check(self.ws, str(self.core), runner=self._runner(rc=1), now=T0)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(self.calls, [])
        with mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": ""}):
            self.assertTrue(self.csi.direct_routing_on())  # the shipped manifest default
            import skill_roots
            with mock.patch.object(skill_roots, "skill_roots", side_effect=RuntimeError):
                self.assertFalse(self.csi.direct_routing_on())

    def test_a_failed_notice_or_record_write_still_reports(self):
        run = self._runner(rc=1)
        with mock.patch.object(self.csi, "_notify", side_effect=OSError), \
                mock.patch.object(self.csi, "write_record", side_effect=OSError):
            row = self.csi.check(self.ws, str(self.core), runner=run, now=T0)
        self.assertEqual(row["status"], "fail")

    def test_health_check_row(self):
        spec = importlib.util.spec_from_file_location("hc_sign_in", ROOT / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        hc.WORKSPACE_DIR = self.ws
        with mock.patch.object(hc, "_local_core_socket", return_value=None), \
                mock.patch.object(self.csi, "signed_in", return_value=False):
            row = hc.check_claude_sign_in()
        self.assertEqual((row["name"], row["status"]), ("claude-sign-in", "fail"))


class HealthCheck(Tmp):
    def _hc(self):
        spec = importlib.util.spec_from_file_location("hc_edges", ROOT / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        hc.WORKSPACE_DIR = self.ws
        return hc

    def test_another_logins_record_is_not_this_cores(self):
        hc = self._hc()
        qr.record_statusline(self.ws, _status(five=100), config_dir=str(self.core), core_dir=str(self.core))
        qr.record_rate_limit(self.ws, config_dir=str(self.core), core_dir=str(self.core))
        (self.core / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-Z"}}))
        for check in (hc.check_core_quota_exhausted(), hc.check_core_request_rejections()):
            self.assertEqual(check["status"], "ok", check)
            self.assertIn("another login", check["detail"])
        self.assertFalse(hc._quota_record_from_statusline())
        qr.quota_path(self.ws).write_text("{broken")
        self.assertFalse(hc._quota_record_from_statusline())

    def test_readers_show_nothing_for_another_logins_record(self):
        qr.record_statusline(self.ws, _status(five=55), config_dir=str(self.core), core_dir=str(self.core))
        (self.core / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-Z"}}))
        import dashboard
        with mock.patch.object(dashboard, "WORKSPACE_DIR", self.ws):
            self.assertEqual(dashboard.get_quota_status(), {"available": True})
        (self.core / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-A"}}))
        with mock.patch.object(dashboard, "WORKSPACE_DIR", self.ws):
            self.assertIn("headers", dashboard.get_quota_status())

    def test_read_quota_refuses_another_logins_record(self):
        qr.record_statusline(self.ws, _status(five=55), config_dir=str(self.core), core_dir=str(self.core))
        (self.core / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-Z"}}))
        env = {"SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_TEST_MODE": "1"}
        with mock.patch.dict(os.environ, env):
            mod = _load("read_quota_edges", SCRIPTS / "read-quota.py")
        mod.QUOTA_FILE = qr.quota_path(self.ws)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["read-quota.py"]), mock.patch.object(sys, "stdout", out):
            with self.assertRaises(SystemExit) as stop:
                mod.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("another login", out.getvalue())

    def test_statusline_freshness_with_an_unreadable_record(self):
        hc = self._hc()
        import quota_availability
        with mock.patch.object(quota_availability, "read_quota_record", return_value=None):
            c = hc._statusline_quota_freshness({"name": "quota-telemetry", "status": "ok"})
        self.assertEqual(c["status"], "ok")
        self.assertIn("age unknown", c["detail"])


if __name__ == "__main__":
    unittest.main()
