#!/usr/bin/env python3
"""The status line quota recorder: src/quota_record.py (the writer), the skill's
recorder / StopFailure / installer scripts, the session-config hook runner, and the
readers that must treat `source: "statusline"` without a proxy as healthy.

Run: python3 tests/quota-record.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "quota-tracker" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "src"))  # ahead of the skill's quota_availability shim

import claude_account
import quota_availability as qa
import quota_record as qr
import session_config_hooks as sch
import proxy_retire as pr
import statusline_config as cfg

FX = json.loads((ROOT / "tests" / "fixtures" / "quota-record-shape.parity.json").read_text())
P = "anthropic-ratelimit-unified-"
T0 = FX["now_epoch"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _status(five=42, seven=7, model="claude-opus-5-5"):
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five, "resets_at": T0 + 4 * 3600}
    if seven is not None:
        rl["seven_day"] = {"used_percentage": seven, "resets_at": T0 + 5 * 86400}
    return {"model": {"id": model, "display_name": "Opus"}, "rate_limits": rl}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.path = self.ws / "state" / "quota-state.json"
        root = Path(self._tmp.name)
        self.core = self._login(root / "core", "acct-A")
        self.same = self._login(root / "worker-same", "acct-A")
        self.other = self._login(root / "worker-other", "acct-B")
        # Readers verify a record against the core's current account, read from its config dir.
        patch = mock.patch.object(claude_account, "core_config_dir", return_value=self.core)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _login(d, uuid):
        d.mkdir()
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}))
        return str(d)

    def sl(self, status, now=None, cd=None):
        return qr.record_statusline(self.ws, status, now=now, config_dir=cd or self.core, core_dir=self.core)

    def rl(self, now=None, cd=None, **kw):
        return qr.record_rate_limit(self.ws, now=now, config_dir=cd or self.core, core_dir=self.core, **kw)

    def tearDown(self):
        self._tmp.cleanup()

    def rec(self):
        return json.loads(self.path.read_text())


class Parity(unittest.TestCase):
    def test_statusline_record_matches_the_proxy_shape(self):
        out = qr.record_from_statusline(FX["prev"], FX["statusline"], T0, now=T0)
        self.assertEqual(out.pop("source"), "statusline")
        self.assertEqual(out, FX["proxy_record"])


class Writer(Base):
    def test_sample_reads_back_through_the_readers(self):
        self.assertTrue(self.sl(_status(), now=T0))
        rec = qa.read_quota_record(self.ws, now=T0 + 30)
        self.assertTrue(rec.fresh())
        self.assertEqual(qa.classify_limit_state(rec.payload), "allowed")
        self.assertEqual(qa.quota_windows(rec.payload["headers"]),
                         {"5h": (0.42, None), "7d": (0.07, None)})
        self.assertTrue(qa.gate_windows_allowed(rec.payload))
        d = qa.availability_decision(rec.payload, base_url=None, stale=False)
        self.assertTrue(d["available"], d)
        self.assertEqual(rec.payload["headers"][P + "5h-reset"], str(T0 + 4 * 3600))

    def test_one_window_missing_still_writes(self):
        self.assertTrue(self.sl(_status(seven=None), now=T0))
        self.assertNotIn(P + "7d-utilization", self.rec()["headers"])

    def test_missing_or_partial_rate_limits_write_nothing(self):
        for status in ({"model": {"id": "x"}}, {"rate_limits": {}}, {"rate_limits": None},
                       {"rate_limits": {"five_hour": {"used_percentage": 3}}},
                       {"rate_limits": {"five_hour": {"resets_at": T0}}}, None, "junk"):
            with self.subTest(status=status):
                self.assertFalse(self.sl(status, now=T0))
                self.assertFalse(self.path.exists())

    def test_older_observation_does_not_overwrite_newer(self):
        self.sl(_status(five=50), now=T0)
        transcript = Path(self._tmp.name) / "t.jsonl"
        transcript.write_text("")
        os.utime(transcript, (T0 - 600, T0 - 600))
        old = dict(_status(five=10), transcript_path=str(transcript))
        self.assertFalse(self.sl(old, now=T0 + 60))
        self.assertEqual(self.rec()["utilization_5h"], 0.5)

    def test_recent_rejections_and_credential_state_are_preserved(self):
        self.path.write_text(json.dumps(dict(FX["prev"], account=claude_account.account_key(self.core))))
        self.sl(_status(), now=T0)
        rec = self.rec()
        self.assertEqual(rec["recent_rejections"], FX["prev"]["recent_rejections"])
        self.assertEqual(rec["credential_state"], "ok")

    def test_stopfailure_rejects_then_next_write_clears(self):
        self.sl(_status(five=98), now=T0)
        self.assertTrue(self.rl(now=T0 + 10, detail="429 Too Many Requests"))
        rec = qa.read_quota_record(self.ws, now=T0 + 20).payload
        self.assertEqual(qa.classify_limit_state(rec), "rejected")
        self.assertFalse(rec["available"])
        self.assertEqual(rec["recent_rejections"][-1]["status"], 429)
        self.assertEqual(rec["recent_rejections"][-1]["model"], "claude-opus-5-5")
        # The timer re-render with the same numbers before the reset keeps it rejected.
        self.sl(_status(five=98), now=T0 + 70)
        self.assertEqual(self.rec()["limit_state"], "rejected")
        # Unchanged numbers are no evidence, even after the reset; a later reply that moved is.
        self.sl(_status(five=98), now=T0 + 4 * 3600 + 1)
        self.assertEqual(self.rec()["limit_state"], "rejected")
        self.sl(_status(five=2), now=T0 + 4 * 3600 + 2)
        rec = self.rec()
        self.assertEqual(qa.classify_limit_state(rec), "allowed")
        self.assertTrue(rec["available"])
        self.assertNotIn("rejected_by", rec)
        self.assertEqual(len(rec["recent_rejections"]), 1)

    def test_a_reply_under_the_limit_clears_before_the_reset(self):
        self.sl(_status(five=98), now=T0)
        self.rl(now=T0 + 10)
        self.sl(_status(five=100), now=T0 + 20)
        self.assertEqual(self.rec()["limit_state"], "rejected")
        self.sl(_status(five=3), now=T0 + 30)
        self.assertEqual(self.rec()["limit_state"], "allowed")

    def test_rejection_ledger_is_bounded(self):
        for i in range(25):
            self.rl(now=T0 + i)
        self.assertEqual(len(self.rec()["recent_rejections"]), qr.MAX_RECENT_REJECTIONS)


    def test_an_older_observation_never_clears_a_later_rejection(self):
        self.sl(_status(five=40), now=T0)
        self.rl(now=T0 + 100)
        transcript = Path(self._tmp.name) / "t.jsonl"
        transcript.write_text("")
        os.utime(transcript, (T0 + 50, T0 + 50))
        self.sl(dict(_status(five=3), transcript_path=str(transcript)), now=T0 + 200)
        rec = qa.read_quota_record(self.ws, now=T0 + 210).payload
        self.assertEqual(rec["limit_state"], "rejected")
        self.assertFalse(qa.availability_decision(rec, base_url=None, stale=False)["available"])
        self.sl(_status(five=3), now=T0 + 300)
        self.assertEqual(self.rec()["limit_state"], "allowed")

    def test_an_unbound_record_is_advisory_and_never_clears(self):
        held = qr.record_with_rate_limit(qr.record_from_statusline({}, _status(five=40), T0), T0 + 10)
        out = qr.record_from_statusline(held, _status(five=3), T0 + 20, bound=False)
        self.assertEqual(out["limit_state"], "rejected")
        allowed = qr.record_from_statusline({}, _status(), T0)
        self.assertFalse(qa.from_statusline(allowed))
        d = qa.availability_decision(allowed, base_url=None, stale=False)
        self.assertEqual(d["unavailable_reason"], "not-routed")
        core = claude_account.account_key(self.core)
        self.assertTrue(qa.availability_decision(dict(allowed, account=core), base_url=None, stale=False)["available"])
        d = qa.availability_decision(dict(allowed, account=claude_account.account_key(self.other)),
                                     base_url=None, stale=False)
        self.assertEqual(d["unavailable_reason"], "not-routed")

    def test_a_later_429_than_the_observation_blocks_the_reader(self):
        rec = dict(qr.record_from_statusline({}, _status(), T0), account=claude_account.account_key(self.core))
        rec["recent_rejections"] = [{"ts": qr.iso(T0 + 5), "status": 429}]
        self.assertFalse(qa.availability_decision(rec, base_url=None, stale=False)["available"])

    def test_a_worker_on_another_account_never_touches_the_core(self):
        self.sl(_status(five=40), now=T0)
        self.sl(_status(five=90), now=T0 + 60, cd=self.other)
        self.rl(now=T0 + 70, cd=self.other)
        rec = qa.read_quota_record(self.ws, now=T0 + 80).payload
        self.assertEqual(rec["utilization_5h"], 0.4)
        self.assertEqual(qa.classify_limit_state(rec), "allowed")
        self.assertTrue(rec["available"])
        self.assertEqual(rec["recent_rejections"], [])
        theirs = rec["accounts"][claude_account.account_key(self.other)]
        self.assertEqual((theirs["utilization_5h"], theirs["limit_state"]), (0.9, "rejected"))
        self.assertEqual(rec["account"], claude_account.account_key(self.core))
        # Before the core has written at all, another account still leaves the top level empty.
        self.path.unlink()
        self.sl(_status(five=90), now=T0, cd=self.other)
        self.assertNotIn("headers", self.rec())

    def test_a_worker_on_the_same_account_updates_the_core(self):
        self.sl(_status(five=40), now=T0)
        self.sl(_status(five=55), now=T0 + 60, cd=self.same)
        self.assertEqual(self.rec()["utilization_5h"], 0.55)
        self.rl(now=T0 + 70, cd=self.same)
        self.assertEqual(qa.classify_limit_state(self.rec()), "rejected")

    def test_unidentified_logins_are_kept_apart_by_config_dir(self):
        a, b = Path(self._tmp.name) / "anon-a", Path(self._tmp.name) / "anon-b"
        a.mkdir()
        b.mkdir()
        self.assertNotEqual(claude_account.account_key(str(a)), claude_account.account_key(str(b)))
        self.assertFalse(claude_account.same_login(str(a), str(b)))
        # A shared directory with no identified account is not a verified login.
        self.assertFalse(claude_account.same_login(str(a), str(a)))
        self.assertFalse(claude_account.is_identified(claude_account.account_key(str(a))))
        self.assertTrue(claude_account.is_identified(claude_account.account_key(self.core)))

    def test_tracking_only_mode_with_proxy_routing_is_trusted(self):
        # Tracking on, direct routing off (the default): sessions still go through the proxy.
        self.assertTrue(self.sl(_status(five=30), now=None))
        rec = qa.read_quota_record(self.ws).payload
        self.assertIs(qa.trusted_record(rec), rec)
        for base in ("http://localhost:7846", None):
            d = qa.availability_decision(rec, base_url=base, stale=False)
            self.assertTrue(d["available"], (base, d))
            self.assertTrue(d["routed"])

    def test_an_account_switch_and_a_proxy_rollback_never_use_the_old_login(self):
        import core_limit_hold as clh
        self.sl(_status(five=40), now=None)
        self.rl()
        old = self.rec()
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-C"}}))
        for base in (None, "http://localhost:7846"):  # direct, and rolled back to the proxy
            d = qa.availability_decision(old, base_url=base, stale=False)
            self.assertFalse(d["available"], (base, d))
            self.assertEqual(d["unavailable_reason"], "not-routed")
        self.assertIsNone(qa.trusted_record(old))
        proxy = {k: v for k, v in old.items() if k not in ("source", "account", "accounts")}
        self.assertIs(qa.trusted_record(proxy), proxy)  # a proxy record is judged as before
        projects = Path(self._tmp.name) / "projects"
        projects.mkdir()
        decision = clh.decide(self.ws, projects_dir=projects, launch_model="claude-opus-5-5",
                              launch_account=claude_account.account_fingerprint(self.core))
        self.assertTrue(decision.launch, decision)  # the old login's 429 holds nothing
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-A"}}))
        self.assertIs(qa.trusted_record(old), old)

    def test_a_prior_logins_credit_rejection_never_marks_the_current_account_exhausted(self):
        import core_recovery_state as crs
        self.sl(_status(five=40), now=None)
        self.rl(detail="Your credit balance is too low to access the API")
        sup = {"state": "blocked-human", "kind": "usage-limit", "prompt": "limit reached"}
        import time as _t
        self.assertEqual(crs.gather(self.ws, sup, now=_t.time(), runtime="claude")["state"], "out_of_credits")
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-C"}}))
        self.assertEqual(crs.gather(self.ws, sup, now=_t.time(), runtime="claude")["state"], "paused_limit")

    def test_the_core_logins_status_line_429_is_evidence_for_its_own_account(self):
        import core_limit_hold as clh
        import time as _t
        self.sl(_status(five=40), now=None)
        self.rl(detail="usage limit reached")
        rec = self.rec()
        self.assertEqual(len(qa.record_rejections(rec)), 1)
        digest = claude_account.account_fingerprint(self.core)
        self.assertIs(qa.record_for_account(rec, digest), rec)
        self.assertIsNotNone(clh.record_evidence(qa.trusted_record(rec), _t.time(), digest))
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-C"}}))
        other = claude_account.account_fingerprint(self.core)
        self.assertIsNone(qa.record_for_account(rec, other))
        self.assertIsNone(clh.record_evidence(rec, _t.time(), other))

    def test_promotion_keeps_the_accounts_existing_evidence(self):
        self.sl(_status(five=40), now=T0)
        self.sl(_status(five=70), now=T0 + 10, cd=self.other)
        self.rl(now=T0 + 20, cd=self.other, detail="429")
        b = claude_account.account_key(self.other)
        before = self.rec()["accounts"][b]
        # The core's config dir signs in to the worker's account: that account becomes the top level.
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-B"}}))
        self.sl(_status(five=70), now=T0 + 30)
        rec = self.rec()
        self.assertEqual(rec["account"], b)
        self.assertEqual(rec["recent_rejections"], before["recent_rejections"])
        self.assertEqual(rec["limit_state"], "rejected")  # same numbers since the 429: still held
        self.assertEqual(rec["exhausted_since"], before["exhausted_since"])
        self.assertEqual(rec["accounts"][b]["recent_rejections"], before["recent_rejections"])

    def test_a_login_change_in_the_same_config_dir_inherits_nothing(self):
        self.sl(_status(five=80), now=T0)
        self.rl(now=T0 + 10)
        self.assertEqual(self.rec()["limit_state"], "rejected")
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-C"}}))
        self.sl(_status(five=80), now=T0 + 20)
        rec = qa.read_quota_record(self.ws, now=T0 + 30).payload
        self.assertEqual(rec["account"], claude_account.account_key(self.core))
        self.assertEqual(rec["limit_state"], "allowed")
        self.assertEqual(rec["recent_rejections"], [])
        self.assertNotIn("rejected_by", rec)
        self.assertTrue(qa.availability_decision(rec, base_url=None, stale=False)["available"])
        # A rejection under the new login does not reach back to the old one either.
        self.rl(now=T0 + 40)
        old_key = [k for k in self.rec()["accounts"] if k != claude_account.account_key(self.core)][0]
        self.assertEqual(len(self.rec()["accounts"][old_key]["recent_rejections"]), 1)

    def test_readers_ignore_a_record_from_a_previous_core_login(self):
        self.sl(_status(five=10), now=None)
        Path(self.core, ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-C"}}))
        rec = qa.read_quota_record(self.ws).payload
        self.assertFalse(qa.from_statusline(rec))
        self.assertFalse(qa.availability_decision(rec, base_url=None, stale=False)["available"])

    def test_an_unknown_login_is_advisory_only(self):
        anon = Path(self._tmp.name) / "anon"
        anon.mkdir()
        self.sl(_status(five=40), now=T0)
        self.sl(_status(five=99), now=T0 + 10, cd=str(anon))
        self.rl(now=T0 + 20, cd=str(anon))
        rec = self.rec()
        self.assertEqual(rec["utilization_5h"], 0.4)
        self.assertEqual(rec["limit_state"], "allowed")
        key = claude_account.account_key(str(anon))
        self.assertTrue(key.startswith("dir-"))
        self.assertIn(key, rec["accounts"])
        # The core itself without an identified account: written, but no reader trusts it.
        Path(self.core, ".claude.json").unlink()
        self.path.unlink()
        self.sl(_status(five=40), now=None)
        self.assertNotIn("headers", self.rec())
        self.assertIsNone(claude_account.core_account())
        unbound = next(iter(self.rec()["accounts"].values()))
        self.assertFalse(qa.from_statusline(dict(unbound, account=claude_account.account_key(self.core))))
        # An unbound entry seeds nothing: its own earlier rejection is not carried into the next write.
        self.rl(cd=str(anon))
        self.sl(_status(five=40), cd=str(anon))
        self.assertEqual(self.rec()["accounts"][key]["recent_rejections"], [])


class Scripts(Base):
    def _run(self, script, stdin: bytes, env=None):
        mod = _load(script.stem.replace("-", "_"), script)
        out = io.BytesIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin))
        fake_out = io.TextIOWrapper(out, write_through=True)
        with mock.patch.object(cfg, "workspace", return_value=self.ws), \
                mock.patch.object(claude_account, "core_config_dir", return_value=self.core), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.core, **(env or {})}), \
                mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out):
            mod.main()
            fake_out.flush()
        return out.getvalue().decode()

    def test_recorder_records_and_prints_a_short_line(self):
        line = self._run(SCRIPTS / "statusline-record.py", json.dumps(_status()).encode())
        self.assertEqual(line.strip(), "Opus · 5h 42% · 7d 7%")
        self.assertEqual(self.rec()["source"], "statusline")

    def test_recorder_passes_through_the_foreign_status_line(self):
        raw = json.dumps(_status()).encode()
        cmd = "python3 -c \"import sys,json; d=json.load(sys.stdin); print('FOREIGN', d['model']['id'])\""
        line = self._run(SCRIPTS / "statusline-record.py", raw, {"QUOTA_STATUSLINE_PASSTHROUGH": cmd})
        self.assertEqual(line, "FOREIGN claude-opus-5-5\n")
        self.assertTrue(self.path.exists())

    def test_recorder_survives_garbage(self):
        self._run(SCRIPTS / "statusline-record.py", b"\xff not json")
        self.assertFalse(self.path.exists())

    def test_stopfailure_only_on_rate_limit(self):
        self._run(SCRIPTS / "stopfailure-record.py", json.dumps({"error": "server_error"}).encode())
        self.assertFalse(self.path.exists())
        self._run(SCRIPTS / "stopfailure-record.py", json.dumps({"error": "rate_limit"}).encode())
        self.assertEqual(self.rec()["limit_state"], "rejected")


class Installer(Base):
    def setUp(self):
        super().setUp()
        self.ccd = Path(self._tmp.name) / "ccd"
        self.ccd.mkdir()
        self.settings = self.ccd / "settings.json"

    def test_installs_statusline_and_stopfailure(self):
        self.assertEqual(cfg.install(str(self.ccd), True), "installed")
        st = json.loads(self.settings.read_text())
        self.assertIn(cfg.STATUS_MARK, st["statusLine"]["command"])
        self.assertEqual(st["statusLine"]["refreshInterval"], 60)
        group = st["hooks"]["StopFailure"][0]
        self.assertEqual(group["matcher"], "rate_limit")
        self.assertIn(cfg.STOP_MARK, group["hooks"][0]["command"])
        self.assertEqual(cfg.install(str(self.ccd), True), "unchanged")

    def test_foreign_statusline_is_kept_passed_through_and_restored(self):
        foreign = {"type": "command", "command": "~/bin/my-status", "padding": 1}
        other_hook = {"matcher": "", "hooks": [{"type": "command", "command": "notify"}]}
        self.settings.write_text(json.dumps({"statusLine": foreign, "model": "opus",
                                             "hooks": {"StopFailure": [other_hook]}}))
        cfg.install(str(self.ccd), True)
        self.assertEqual(cfg.passthrough_command(str(self.ccd)), "~/bin/my-status")
        cfg.install(str(self.ccd), True)  # a re-run must not save our own line as foreign
        self.assertEqual(cfg.passthrough_command(str(self.ccd)), "~/bin/my-status")
        self.assertEqual(cfg.install(str(self.ccd), False), "removed")
        self.assertEqual(json.loads(self.settings.read_text()),
                         {"statusLine": foreign, "model": "opus", "hooks": {"StopFailure": [other_hook]}})
        self.assertIsNone(cfg.passthrough_command(str(self.ccd)))

    def test_off_with_nothing_installed_changes_nothing(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        self.assertEqual(cfg.install(str(self.ccd), False), "unchanged")

    def test_opt_out_keeps_the_backup_until_the_restore_is_read_back(self):
        foreign = {"type": "command", "command": "~/bin/my-status"}
        self.settings.write_text(json.dumps({"statusLine": foreign}))
        cfg.install(str(self.ccd), True)
        backup = self.ccd / cfg.PASSTHROUGH_FILE
        real_write = cfg._write_json

        def boom(path, data):
            if Path(path).name == "settings.json":
                raise OSError("disk full")
            real_write(path, data)

        with mock.patch.object(cfg, "_write_json", side_effect=boom):
            with self.assertRaises(OSError):
                cfg.install(str(self.ccd), False)
        self.assertTrue(backup.exists())

        def lossy(path, data):
            real_write(path, {k: v for k, v in data.items() if k != "statusLine"}
                       if Path(path).name == "settings.json" else data)

        with mock.patch.object(cfg, "_write_json", side_effect=lossy):
            self.assertIn("not verified", cfg.install(str(self.ccd), False))
        self.assertTrue(backup.exists())
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": cfg.hook_command(cfg.RECORDER)}}))
        self.assertEqual(cfg.install(str(self.ccd), False), "removed")
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"], foreign)
        self.assertFalse(backup.exists())

    def test_unreadable_backup_keeps_the_recorder(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": cfg.hook_command(cfg.RECORDER)}}))
        (self.ccd / cfg.PASSTHROUGH_FILE).write_text("{broken")
        self.assertIn("unreadable", cfg.install(str(self.ccd), False))
        self.assertIn(cfg.STATUS_MARK, json.loads(self.settings.read_text())["statusLine"]["command"])

    def test_unreadable_settings_are_left_alone(self):
        self.settings.write_text("{not json")
        self.assertTrue(cfg.install(str(self.ccd), True).startswith("skipped"))
        self.assertEqual(self.settings.read_text(), "{not json")

    def test_enabled_reads_env_then_manifest(self):
        with mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": ""}):
            self.assertTrue(cfg.enabled())
        with mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": "false"}):
            self.assertFalse(cfg.enabled())

    def test_manifest_declares_the_session_config_hook(self):
        m = json.loads((ROOT / "skills" / "quota-tracker" / "manifest.json").read_text())
        self.assertTrue((ROOT / "skills" / "quota-tracker" / m["on_session_config"]).is_file())
        self.assertEqual(m["config"]["QUOTA_STATUSLINE_ENABLED"], "true")


class SessionConfigHooks(Base):
    def test_runs_each_hook_with_the_config_dir_and_survives_failures(self):
        d = Path(self._tmp.name)
        ok = d / "ok.py"
        ok.write_text("import sys; open(sys.argv[2] + '/seen', 'w').write('1')\n")
        bad = d / "bad.sh"
        bad.write_text("exit 3\n")
        skipped = d / "x.rb"
        skipped.write_text("")
        out = sch.run([("a", ok), ("b", bad), ("c", skipped)], str(d), sys.executable,
                      deadline=__import__("time").monotonic() + 30)
        self.assertEqual(out, [("a", 0), ("b", 3)])
        self.assertTrue((d / "seen").exists())

    def test_a_timed_out_hook_and_its_children_are_killed_before_return(self):
        d = Path(self._tmp.name)
        late = d / "late-write"
        hook = d / "slow.sh"
        hook.write_text(f"(sleep 2; echo x > {late}) &\nsleep 30\n")
        leaky = d / "leaky.sh"
        leaky.write_text(f"(sleep 2; echo x > {late}.2) &\nexit 0\n")
        t0 = __import__("time").monotonic()
        with mock.patch.object(sch, "HOOK_SECONDS", 0.5):
            out = sch.run([("slow", hook), ("leaky", leaky)], str(d), sys.executable,
                          deadline=t0 + 30)
        self.assertEqual(out, [("slow", None), ("leaky", 0)])
        self.assertLess(__import__("time").monotonic() - t0, 10)
        __import__("time").sleep(3)
        self.assertFalse(late.exists())
        self.assertFalse(Path(f"{late}.2").exists())


class FakeRun:
    """Answers launchctl / ps / lsof from a table; records every argv."""

    def __init__(self, ps="", lsof_rc=1, lsof_out="", loaded=True, pid=4242, bootout_rc=0, cwd=None, ps_rc=0):
        self.calls, self.ps, self.lsof_rc, self.lsof_out = [], ps, lsof_rc, lsof_out
        self.loaded, self.pid, self.bootout_rc, self.cwd, self.ps_rc = loaded, pid, bootout_rc, cwd, ps_rc

    def __call__(self, argv):
        self.calls.append(argv)
        R = __import__("types").SimpleNamespace
        if argv[:2] == ["launchctl", "print"]:
            return R(returncode=0 if self.loaded else 113,
                     stdout=f"\tstate = running\n\tpid = {self.pid}\n" if self.loaded else "")
        if argv[:2] == ["launchctl", "bootout"]:
            self.loaded = self.loaded and self.bootout_rc != 0
            return R(returncode=self.bootout_rc, stdout="")
        if argv[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
            return R(returncode=0, stdout="")
        if argv[0] == "ps":
            return R(returncode=self.ps_rc, stdout=self.ps)
        if argv[0] == "lsof" and "cwd" in argv:
            return R(returncode=0, stdout=f"p{argv[3]}\nn{self.cwd}\n") if self.cwd else R(returncode=1, stdout="")
        if argv[0] == "lsof":
            return R(returncode=self.lsof_rc, stdout=self.lsof_out)
        raise AssertionError(argv)

    def booted_out(self):
        return any(a[:2] == ["launchctl", "bootout"] for a in self.calls)


class ProxyRetire(Base):
    def setUp(self):
        super().setUp()
        self.sl(_status(), now=None)  # a fresh core record
        p = [mock.patch.object(claude_account, "core_config_dir", return_value=self.core),
             mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": "true"})]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.killed = []

    def _gone(self, pid, sig):
        self.killed.append((pid, sig))
        if sig == 0:
            raise OSError("no such process")

    def retire(self, run):
        return pr.retire(self.ws, runner=run, kill=self._gone, wait_s=0.2)

    def test_kept_while_a_session_env_points_at_the_proxy(self):
        ps = (f" 101 /opt/bin/claude --name sutando-core CLAUDE_CONFIG_DIR={self.core} "
              "ANTHROPIC_BASE_URL=http://localhost:7846\n 102 /bin/zsh ANTHROPIC_BASE_URL=http://localhost:7846\n")
        run = FakeRun(ps=ps)
        said = self.retire(run)
        self.assertIn("pid 101", said)
        self.assertIn("pid 102 zsh", said)  # any process of this user, not only claude
        self.assertFalse(run.booted_out())
        rec = json.loads((self.ws / "state" / pr.RECORD).read_text())
        self.assertEqual(rec["status"], "kept")
        self.assertIn("pid 101", rec["reason"])

    def test_kept_while_launch_settings_point_at_the_proxy(self):
        Path(self.core, "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:7846"}}))
        run = FakeRun()
        self.assertIn("settings", self.retire(run))
        self.assertFalse(run.booted_out())

    def test_a_non_claude_process_pointing_at_the_proxy_keeps_it(self):
        ps = " 301 /usr/local/bin/node /srv/agent-sdk.js ANTHROPIC_BASE_URL=http://127.0.0.1:7846/ HOME=/h\n"
        run = FakeRun(ps=ps)
        said = self.retire(run)  # an idle SDK client: no connection open at this instant
        self.assertIn("pid 301 node", said)
        self.assertFalse(run.booted_out())
        unreadable = FakeRun(ps=" 302 /usr/local/bin/node /srv/agent-sdk.js\n")
        self.assertIn("pid 302 node (environment unreadable)", self.retire(unreadable))
        ps_argv = [a for a in run.calls if a[0] == "ps"][0]
        self.assertEqual(ps_argv[ps_argv.index("-U") + 1], str(os.getuid()))

    def test_a_failed_process_list_keeps_it(self):
        run = FakeRun(ps_rc=1)
        self.assertIn("process list unreadable", self.retire(run))
        self.assertFalse(run.booted_out())

    def test_an_unreadable_session_config_keeps_it(self):
        Path(self.core, "settings.json").write_text("{not json")
        run = FakeRun()
        self.assertIn("unreadable", self.retire(run))
        self.assertFalse(run.booted_out())

    def test_an_idle_session_still_pointing_at_the_proxy_keeps_it(self):
        proj = Path(self._tmp.name) / "proj"
        (proj / ".claude").mkdir(parents=True)
        (proj / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:7846"}}))
        run = FakeRun(ps=f" 101 /opt/bin/claude --name w CLAUDE_CONFIG_DIR={self.same}\n", cwd=str(proj))
        said = self.retire(run)  # no connection open: idle, yet still configured for the proxy
        self.assertIn("settings.local.json", said)
        self.assertFalse(run.booted_out())

    def test_an_unknown_session_keeps_it(self):
        run = FakeRun(ps=" 101 /opt/bin/claude --name w\n")
        self.assertIn("environment unreadable", self.retire(run))
        run = FakeRun(ps=f" 102 /opt/bin/claude --name w CLAUDE_CONFIG_DIR={self.same}\n")
        self.assertIn("working folder unreadable", self.retire(run))
        self.assertFalse(run.booted_out())

    def test_kept_while_a_connection_is_open(self):
        out = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
               "node 4242 u 20u IPv4 0x1 0t0 TCP 127.0.0.1:7846->127.0.0.1:50000 (ESTABLISHED)\n"
               "curl 999 u 3u IPv4 0x2 0t0 TCP 127.0.0.1:50000->127.0.0.1:7846 (ESTABLISHED)\n")
        run = FakeRun(lsof_rc=0, lsof_out=out)
        said = self.retire(run)
        self.assertIn("curl 999", said)
        self.assertFalse(run.booted_out())

    def test_kept_without_a_fresh_core_record(self):
        self.path.unlink()
        run = FakeRun()
        self.assertIn("no fresh status line record", self.retire(run))
        self.assertFalse(run.booted_out())

    def test_turned_off_when_everything_is_clear_and_reenabled_on_opt_out(self):
        proj = Path(self._tmp.name) / "proj"
        proj.mkdir()
        run = FakeRun(ps=" 101 /opt/bin/claude --name sutando-core ANTHROPIC_BASE_URL=http://example.test:1\n",
                      cwd=str(proj))
        said = self.retire(run)
        self.assertTrue(said.startswith("turned off"), said)
        self.assertTrue(run.booted_out())
        self.assertEqual(self.killed, [(4242, 0)])  # gone after bootout: never signalled
        rec = json.loads((self.ws / "state" / pr.RECORD).read_text())
        self.assertEqual((rec["status"], rec["by"]), ("retired", "quota-tracker"))
        plist = Path(self._tmp.name) / "LaunchAgents" / f"{pr.LABEL}.plist"
        plist.parent.mkdir()
        plist.write_text("<plist/>")
        with mock.patch.object(pr, "plist_path", return_value=plist):
            self.assertEqual(pr.restore(self.ws, runner=run), "restored")
        boot = [a for a in run.calls if a[:2] == ["launchctl", "bootstrap"]]
        self.assertEqual(boot[0][-1], str(plist))
        self.assertEqual(json.loads((self.ws / "state" / pr.RECORD).read_text())["status"], "restored")

    def test_a_job_that_outlives_bootout_is_stopped_by_its_own_pid_only(self):
        run = FakeRun(pid=5151)
        sent = []
        said = pr.retire(self.ws, runner=run, kill=lambda pid, sig: sent.append((pid, sig)), wait_s=0.2)
        self.assertTrue(said.startswith("turned off"), said)
        self.assertEqual({p for p, _s in sent}, {5151})
        self.assertIn(__import__("signal").SIGTERM, [s for _p, s in sent])

    def test_opt_out_never_starts_a_job_it_did_not_turn_off(self):
        run = FakeRun(loaded=False)
        self.assertEqual(pr.restore(self.ws, runner=run), "nothing to restore")
        self.assertFalse(any(a[:2] == ["launchctl", "bootstrap"] for a in run.calls))

    def test_switch_off_keeps_the_proxy(self):
        run = FakeRun()
        with mock.patch.dict(os.environ, {"QUOTA_STATUSLINE_ENABLED": "false"}):
            self.assertIn("tracking is off", self.retire(run))
        self.assertFalse(run.booted_out())


class HealthCheck(Base):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("hc_quota_record_test", ROOT / "src" / "health-check.py")
        self.hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hc)
        self.hc.WORKSPACE_DIR = self.ws
        self.sl(_status(), now=None)

    def test_quota_telemetry_ok_without_proxy(self):
        for proxy in ("down", "ok"):
            c = self.hc.check_quota_telemetry(proxy, core_env_prober=lambda: False)
            self.assertEqual(c["status"], "ok", c)
            self.assertIn("status line", c["detail"])

    def test_credential_proxy_down_is_ok_with_statusline_source(self):
        hc = self.hc
        with mock.patch.object(hc, "check_port",
                               side_effect=lambda *a, **k: {"name": "credential-proxy", "status": "down"}), \
                mock.patch.object(hc, "_proc_lstarts", return_value=([], [])), \
                mock.patch.object(hc, "_pin_verdicts", return_value=[]), \
                mock.patch.object(hc, "_apply_pin_findings", return_value=None):
            c = hc.check_credential_proxy()
            self.assertEqual(c["status"], "ok", c)
            self.assertFalse(c["live"])
            self.path.write_text(json.dumps(FX["proxy_record"]))
            self.assertEqual(hc.check_credential_proxy()["status"], "warn")

    def test_stale_statusline_record_warns_while_the_core_works(self):
        import time as _t
        stale = _t.time() - 30 * 60
        self.path.write_text(json.dumps(dict(self.rec(), last_checked=qr.iso(stale))))
        status = self.ws / "state" / "core-status.json"
        status.write_text("{}")
        c = self.hc.check_quota_telemetry("down")
        self.assertEqual(c["status"], "warn", c)
        os.utime(status, (stale, stale))  # the core has been quiet since the record
        self.assertEqual(self.hc.check_quota_telemetry("down")["status"], "ok")
        status.unlink()
        self.assertEqual(self.hc.check_quota_telemetry("down")["status"], "ok")


if __name__ == "__main__":
    unittest.main()
