#!/usr/bin/env python3
"""The core's runtime profile (runtime_profile.v1): the builder reproduces the contract fixtures'
model and plan for a Claude core with usage, a Codex core with none and a core at its limit; its
switch tokens are clicks the recovery executor admits; unknown is null; a missing or broken input
never raises; and contracts/runtime_profile.v1/ stays byte-identical to the backend and client.

Run: python3 tests/runtime-profile.test.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import core_recovery_state
import recovery_actions
import runtime_models
import runtime_profile as rp

CONTRACTS = REPO / "contracts" / "runtime_profile.v1"
# The same bytes are pinned in ag2-space/ag2space-backend and ag2-space/cinny-webclient.
CONTRACT_HASHES = {
    "README.md": "747845187a208e9def62fccabc297665158e47fb4cc2aa912fc2c76f3502bd69",
    "heartbeat.claude.json": "ff72a1ebc88570af1ff9eec960f514cac17b7f9fb35979124e71ff4776fc8030",
    "heartbeat.codex.json": "428294f9bc64dfc4a2db7093303b4c798bfaeba312bea2bbe01c484e1f75cffe",
    "heartbeat.limited.json": "f3b948975fcabcc28a07d5e2830325b2e6572afa42cf2cafc2ff03575b3ba2b4",
    "response.claude.json": "dbc38397f048c7f2d27f878181b9f2adfac21f3602d3501d8e6316ce9cf04f28",
    "response.codex.json": "e3bb8f0d6232202cf072531278050349323e1a0fbdfe393752af5b54b452bf9d",
    "response.limited.json": "d02cae3425ad48cedf91693011ec103adfed100d3f9cf9a520c6571eb72e8726",
    "schema.json": "ca29bf3d2b4b41272c43157856e39f3c50e9ad47a3d80512832df897e8e7bb04",
}
PICKER = (REPO / "tests" / "fixtures" / "pane-claude-model-picker.txt").read_text()
DIGEST = "3f9a2b" + "0" * 10  # core_limit_hold.account_fingerprint's shape: 16 hex


def block(name):
    return json.loads((CONTRACTS / name).read_text())["runtime_profile"]


def headers(**windows):
    """{"7d": (util, status, reset)} as the proxy's unified rate-limit headers."""
    out = {"anthropic-ratelimit-unified-status": "allowed"}
    for w, (util, status, reset) in windows.items():
        out[f"anthropic-ratelimit-unified-{w}-utilization"] = str(util)
        out[f"anthropic-ratelimit-unified-{w}-status"] = status
        out[f"anthropic-ratelimit-unified-{w}-reset"] = str(reset)
        if status == "rejected":
            out["anthropic-ratelimit-unified-status"] = "rejected"
    return out


def claude(now, quota, model="opus", **kw):
    args = dict(runtime="claude", model=rp.claude_model(model, [], "claude-opus-5-5"), offered=[],
                plan_name="Claude Max", account_digest=DIGEST, quota=quota, quota_age_s=5.0,
                hold_until=None, now=now)
    args.update(kw)
    return rp.derive(**args)


class Contract(unittest.TestCase):
    def test_contract_bytes_are_pinned(self):
        got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(CONTRACTS.iterdir())}
        self.assertEqual(got, CONTRACT_HASHES)

    def test_every_heartbeat_fixture_is_a_block_the_validator_accepts(self):
        for case in ("claude", "codex", "limited"):
            body = json.loads((CONTRACTS / f"heartbeat.{case}.json").read_text())
            self.assertIn(rp.CAPABILITY, body["capabilities"])
            self.assertEqual(rp.coerce(body["runtime_profile"]), body["runtime_profile"], case)
            self.assertLessEqual(len(json.dumps(body["runtime_profile"])), rp.MAX_BYTES)

    def test_the_schema_accepts_what_the_builder_sends(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed; coerce() carries the same shape checks")
        schema = json.loads((CONTRACTS / "schema.json").read_text())
        quota = {"headers": headers(**{"7d": (0.14, "allowed", 1790150400)})}
        jsonschema.Draft202012Validator(schema).validate(claude(1789999995, quota))

    def test_the_validator_refuses_what_the_schema_refuses(self):
        good = block("heartbeat.claude.json")
        bad = [dict(good, extra=1), dict(good, v=2), dict(good, model=None),
               dict(good, plan=dict(good["plan"], account="someone@example.com")),
               dict(good, plan=dict(good["plan"], window="hourly")),
               dict(good, plan=dict(good["plan"], usage_pct=101)),
               dict(good, switch_options=[{"token": "opus; rm", "display_name": "x"}]),
               dict(good, workers=[{"worker_id": "../w"}])]
        for b in bad:
            self.assertIsNone(rp.coerce(b), b)


class Shapes(unittest.TestCase):
    """Every malformed part is refused, and every input the builder cannot read is unknown."""

    def test_malformed_parts_are_refused(self):
        good = block("heartbeat.claude.json")
        opt = {"token": "o0123456789ab", "display_name": "Sonnet 5"}
        worker = {"worker_id": "w1"}
        bad = [dict(good, switch_options=[opt] * 7), dict(good, switch_options=["x"]),
               dict(good, plan=[]), dict(good, plan={"name": "Claude Max"}),
               dict(good, workers={}), dict(good, workers=[worker] * 17),
               dict(good, workers=[dict(worker, model={"id": "x"})]),
               dict(good, workers=[dict(worker, plan={"name": "x"})]),
               dict(good, workers=[dict(worker, extra=1)])]
        for b in bad:
            self.assertIsNone(rp.coerce(b), b)

    def test_unreadable_models_are_unknown(self):
        self.assertIsNone(rp._claude_model_from_id("gpt-5.5"))
        self.assertIsNone(rp._claude_model_from_id("claude-opus-20250101"), "a date alone is no version")
        self.assertIsNone(rp.claude_model("gpt-5.5", [], "claude-opus-5-5"))
        self.assertIsNone(rp.claude_model("default", [{"arg": "default", "model": "Something"}], None))
        self.assertEqual(rp.claude_model("default", [{"arg": "default", "model": "Opus 5.5"}], None)["id"],
                         "claude-opus-5-5")

    def test_at_most_six_switches_and_only_named_rows(self):
        rows = [{"key": f"o{i:012x}", "label": "Sonnet 5"} for i in range(8)] + [{"key": "bad key", "label": "x"}]
        got = rp.switch_options("claude", rows)
        self.assertEqual(len(got), rp.MAX_OPTIONS)
        self.assertEqual(got[0]["display_name"], "Claude Sonnet 5")

    def test_unknown_windows_and_unreadable_resets_are_skipped(self):
        quota = {"headers": {"anthropic-ratelimit-unified-1d-utilization": "0.5",
                             "anthropic-ratelimit-unified-5h-utilization": "0.25",
                             "anthropic-ratelimit-unified-5h-reset": "soon"}}
        got = rp.usage(quota, age_s=10.0, hold_until=None, now=1.0)
        self.assertEqual((got["usage_pct"], got["window"], got["resets_at"]), (25, "5h", None))
        self.assertEqual(rp.usage(quota, age_s=rp.QUOTA_STALE_S + 1, hold_until=None, now=1.0)["usage_pct"], None)

    def test_an_unresolvable_runtime_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("sutando_config.resolve_core_runtime", side_effect=RuntimeError("no config")):
            self.assertIsNone(rp._runtime(Path(tmp)))
            self.assertIsNone(rp.read(Path(tmp)))

    def test_show_prints_the_block_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("workspace_default.resolve_workspace", return_value=Path(tmp)), \
                mock.patch.object(rp, "read", return_value=None) as read, \
                mock.patch("sys.stdout") as out:
            self.assertEqual(rp.main(["show"]), 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        read.assert_called_once()
        out.write.assert_any_call("null")


class ClaudeCore(unittest.TestCase):
    def test_a_claude_core_with_usage_reads_as_the_fixture(self):
        want = block("heartbeat.claude.json")
        quota = {"headers": dict(headers(**{"5h": (0.03, "allowed", 1790010000),
                                            "7d": (0.14, "allowed", 1790150400)}),
                                 **{"anthropic-ratelimit-unified-representative-claim": "seven_day"})}
        got = claude(want["reported_at"], quota)
        self.assertEqual((got["v"], got["reported_at"], got["model"], got["plan"], got["workers"]),
                         (1, want["reported_at"], want["model"], want["plan"], []))

    def test_a_core_at_its_limit_reads_as_the_fixture(self):
        want = block("heartbeat.limited.json")
        quota = {"headers": dict(headers(**{"5h": (1.0, "rejected", 1790003600), "7d": (0.4, "allowed", 1790150400)}),
                                 **{"anthropic-ratelimit-unified-overage-status": "rejected"})}
        got = claude(want["reported_at"], quota)
        self.assertEqual((got["model"], got["plan"]), (want["model"], want["plan"]))

    def test_a_hold_in_force_names_its_own_end(self):
        quota = {"headers": headers(**{"7d": (0.2, "allowed", 1790150400)})}
        self.assertEqual(claude(1789999998, quota, hold_until=1790003600.5)["plan"]["limited_until"], 1790003600)
        self.assertIsNone(claude(1789999998, quota, hold_until=1789990000)["plan"]["limited_until"])

    def test_extra_usage_is_not_a_limit(self):
        quota = {"headers": dict(headers(**{"5h": (1.0, "rejected", 1790003600)}),
                                 **{"anthropic-ratelimit-unified-overage-status": "allowed"})}
        plan = claude(1789999998, quota)["plan"]
        self.assertEqual((plan["usage_pct"], plan["limited_until"]), (100, None))

    def test_a_reading_from_a_window_that_already_reset_is_unknown(self):
        quota = {"headers": headers(**{"7d": (0.9, "allowed", 1789990000)})}
        plan = claude(1789999995, quota)["plan"]
        self.assertEqual([plan[k] for k in ("usage_pct", "window", "resets_at", "limited_until")], [None] * 4)

    def test_no_reading_leaves_usage_null_and_keeps_the_plan(self):
        plan = claude(1789999995, None)["plan"]
        self.assertEqual(plan, {"account": "acct_3f9a2b", "name": "Claude Max", "usage_pct": None,
                                "window": None, "resets_at": None, "limited_until": None})

    def test_no_plan_name_means_no_plan_and_a_non_digest_never_becomes_the_account(self):
        self.assertIsNone(claude(1789999995, None, plan_name=None)["plan"])
        plan = claude(1789999995, None, account_digest="someone@example.com")["plan"]
        self.assertNotIn("account", plan)

    def test_the_model_is_named_by_version_never_by_alias(self):
        self.assertEqual(rp.claude_model("claude-sonnet-5[1m]", [], None),
                         {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "runtime": "claude"})
        self.assertEqual(rp.claude_model("claude-haiku-4-5-20251001", [], None)["display_name"], "Claude Haiku 4.5")
        self.assertIsNone(rp.claude_model("opus", [], None), "an alias with no version source is unknown")
        self.assertIsNone(rp.claude_model("opus", [], "claude-sonnet-5"), "another family's request says nothing")
        self.assertIsNone(rp.claude_model(None, [], "claude-opus-5-5"))
        self.assertIsNone(rp.derive(runtime="claude", model=None, offered=[], plan_name="Claude Max",
                                    account_digest=DIGEST, quota=None, quota_age_s=None, hold_until=None, now=1),
                          "an unknown core model sends no block: the schema requires one")


class Switches(unittest.TestCase):
    """Each switch token is the runtime list's row key, a click the recovery executor admits."""

    def test_tokens_are_the_executors_own_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            runtime_models.record(ws, "claude", runtime_models.parse_picker(PICKER, "claude"))
            rows = runtime_models.entries(ws, "claude")
            current = "opus"
            got = rp.derive(runtime="claude", model=rp.claude_model(current, rows, None),
                            offered=runtime_models.offer(ws, "claude", current, None), plan_name=None,
                            account_digest=None, quota=None, quota_age_s=None, hold_until=None, now=1)
            with mock.patch.object(recovery_actions, "current_model", return_value=current):
                allowed = recovery_actions.switch_offer(ws, "claude")
        tokens = [o["token"] for o in got["switch_options"]]
        self.assertTrue(tokens)
        self.assertEqual(tokens, allowed[:rp.MAX_OPTIONS])
        for o in got["switch_options"]:
            self.assertIsNotNone(core_recovery_state.parse_action_id(f"switch_model:{o['token']}", "claude"), o)
            self.assertNotIn(o["token"], ("opus", "sonnet", "haiku", "default"))
        self.assertEqual(got["model"]["runtime"], "claude")

    def test_a_codex_core_offers_no_switch(self):
        row = {"key": "o0123456789ab", "label": "gpt-6-sol", "model": "gpt-6-sol", "arg": None, "current": False}
        self.assertEqual(rp.switch_options("codex", [row]), [])


class CodexCore(unittest.TestCase):
    def test_a_codex_core_reports_its_model_and_no_usage(self):
        want = block("heartbeat.codex.json")
        rows = [{"key": "o0123456789ab", "label": "GPT-5.5", "model": "gpt-5.5", "arg": None, "current": True}]
        got = rp.derive(runtime="codex", model=rp.codex_model(rows), offered=rows, plan_name="ChatGPT Pro",
                        account_digest=DIGEST, quota={"headers": headers(**{"5h": (0.5, "allowed", 9e9)})},
                        quota_age_s=1.0, hold_until=None, now=want["reported_at"])
        self.assertEqual(got, {"v": 1, "reported_at": want["reported_at"], "model": want["model"],
                               "switch_options": [], "plan": None, "workers": []})

    def test_no_current_row_is_no_model(self):
        self.assertIsNone(rp.codex_model([{"key": "o1", "label": "gpt-5.5", "model": "gpt-5.5", "current": False}]))


class Gather(unittest.TestCase):
    """The files on disk, end to end, and every broken input fails open."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        self.cfg = Path(tmp.name) / "cfg"
        (self.ws / "state").mkdir(parents=True)
        self.cfg.mkdir()
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)})
        p.start()
        self.addCleanup(p.stop)
        self.write("core-runtime.json", {"runtime": "claude", "session": "s", "started_at": 1})
        (self.cfg / "settings.json").write_text(json.dumps({"model": "opus"}))
        (self.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "accountUuid": "u-1", "emailAddress": "someone@example.com", "planDisplayName": "Claude Max"}}))
        now = time.time()
        self.write("quota-state.json", {"last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                                        "last_request": {"model": "claude-opus-5-5"},
                                        "headers": headers(**{"7d": (0.14, "allowed", int(now) + 86400)})})

    def write(self, name, data):
        (self.ws / "state" / name).write_text(data if isinstance(data, str) else json.dumps(data))

    def test_the_block_from_the_files(self):
        got = rp.read(self.ws)
        self.assertEqual(got["model"], {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "runtime": "claude"})
        self.assertEqual((got["plan"]["name"], got["plan"]["usage_pct"], got["plan"]["window"]), ("Claude Max", 14, "weekly"))
        self.assertRegex(got["plan"]["account"], r"^acct_[0-9a-f]{6}$")
        text = json.dumps(got)
        for private in ("someone@example.com", "u-1", str(self.cfg), str(self.ws)):
            self.assertNotIn(private, text)

    def test_missing_or_malformed_inputs_never_raise(self):
        cases = [("quota-state.json", "{ not json"), ("quota-state.json", "[1, 2]"),
                 ("runtime-models.claude.json", "{ not json"), ("core-runtime.json", "[]"),
                 ("core-limit-hold.json", "{ not json"), ("core-limit-hold.json", json.dumps({"status": "held", "until": "x"}))]
        for name, text in cases:
            self.write(name, text)
            got = rp.read(self.ws)
            self.assertTrue(got is None or rp.coerce(got) == got, (name, text))
        (self.cfg / ".claude.json").write_text("{ not json")
        (self.cfg / "settings.json").write_text("{ not json")
        self.assertIsNone(rp.read(self.ws), "no model anywhere: no block, no exception")

    def test_a_failing_gather_or_an_oversized_block_reads_as_none(self):
        with mock.patch.object(rp, "gather", side_effect=RuntimeError("boom")):
            self.assertIsNone(rp.read(self.ws))
        big = dict(block("heartbeat.claude.json"), workers=[{"worker_id": f"w{i}", "model": {
            "id": "m" * 60, "display_name": "x" * 64, "runtime": "claude"}, "switch_options": [
            {"token": "t" * 60 + str(j), "display_name": "y" * 64} for j in range(6)]} for i in range(16)])
        self.assertIsNotNone(rp.coerce(big))
        with mock.patch.object(rp, "gather", return_value=big):
            self.assertIsNone(rp.read(self.ws))


if __name__ == "__main__":
    unittest.main()
