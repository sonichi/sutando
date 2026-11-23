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
    def _main(self, out=None, exc=None):
        import claude_signed_in as csi

        def runner(cmd):
            self.assertEqual(cmd, ["claude", "auth", "status", "--json"])
            if exc:
                raise exc
            return 0, out
        return csi.main(["--config-dir", str(self.core)], runner=runner, workspace=self.ws)

    def _recorded(self):
        return json.loads((self.ws / "state" / "claude-direct-routing.json").read_text())[os.path.realpath(self.core)]

    def test_signed_in_not_signed_in_and_unknown(self):
        self.assertEqual(self._main('{"loggedIn": true, "email": "x"}'), 0)
        self.assertIs(self._recorded()["signed_in"], True)
        self.assertNotIn("email", json.dumps(self._recorded()))
        self.assertEqual(self._main('{"loggedIn": false}'), 1)
        self.assertIs(self._recorded()["signed_in"], False)
        self.assertEqual(self._main(exc=subprocess.TimeoutExpired("claude", 8)), 2)
        self.assertIsNone(self._recorded()["signed_in"])
        self.assertEqual(self._main("not json"), 2)
        self.assertEqual(self._main(exc=OSError("no claude")), 2)

    def test_the_real_runner_scopes_the_config_dir_and_times_out(self):
        import claude_signed_in as csi
        stub = self.d / "claude"
        stub.write_text('#!/bin/sh\nif [ "$CLAUDE_CONFIG_DIR" = "%s" ]; then echo \'{"loggedIn": true}\'; fi\n' % self.core)
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(stub)}):
            self.assertIs(csi.signed_in(str(self.core)), True)
        slow = self.d / "slow"
        slow.write_text("#!/bin/sh\nsleep 5\n")
        slow.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(slow)}):
            self.assertIsNone(csi.signed_in(str(self.core), timeout=0.3))
        with mock.patch.object(csi, "signed_in", side_effect=RuntimeError), \
                mock.patch("workspace_default.resolve_workspace", return_value=self.ws):
            self.assertEqual(csi.main(["--config-dir", str(self.core)]), 2)
        (self.ws / "state" / "claude-direct-routing.json").write_text("[]")
        csi.record(self.ws, str(self.core), True)
        with mock.patch("workspace_default.status_path", side_effect=OSError):
            self.assertEqual(csi.main(["--config-dir", str(self.core)], runner=lambda c: (0, '{"loggedIn": true}'),
                                      workspace=self.ws), 0)

    def test_health_check_warns_for_a_dir_that_stayed_on_the_proxy(self):
        import claude_signed_in as csi
        spec = importlib.util.spec_from_file_location("hc_signed_in", ROOT / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        hc.WORKSPACE_DIR = self.ws
        self.assertEqual(hc.check_claude_direct_routing()["status"], "ok")
        csi.record(self.ws, str(self.core), True)
        self.assertEqual(hc.check_claude_direct_routing()["status"], "ok")
        csi.record(self.ws, str(self.d), False)
        c = hc.check_claude_direct_routing()
        self.assertEqual(c["status"], "warn")
        self.assertIn(os.path.realpath(self.d), c["detail"])
        csi.record(self.ws, str(self.d), None, now=time.time() - 3 * 86400)
        self.assertEqual(hc.check_claude_direct_routing()["status"], "ok")


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
