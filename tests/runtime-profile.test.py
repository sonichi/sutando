#!/usr/bin/env python3
"""The core's runtime profile (runtime_profile.v1): the builder reproduces the contract fixtures'
model and plan name for a Claude core and a Codex core; it carries no usage or limit state (that is
health.quota, quota_status.v1); its switch tokens are clicks the recovery executor admits; unknown
is null; a missing or broken input never raises; and contracts/runtime_profile.v1/ is pinned.

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

import core_limit_hold
import core_recovery_state
import recovery_actions
import runtime_models
import runtime_profile as rp

CONTRACTS = REPO / "contracts" / "runtime_profile.v1"
# The consumers (ag2-space/ag2space-backend, ag2-space/cinny-webclient) pin their own copies.
CONTRACT_HASHES = {
    "README.md": "ef3a811f48f090cdb5065d6750339def2d79172d8416c9b241eb1a662e2549c6",
    "heartbeat.claude.json": "d02490ee96a00fe009b7654ab9bc7a4c9c725dc4d2d4e41d094f2de40e2e5698",
    "heartbeat.codex.json": "c2557f8712d1e42ead34f58c57b997091f8d915120b28228b40f05342f5c0e09",
    "heartbeat.limited.json": "22c78f0f95501624a8e4b3f7253fcbbdcbf901c0dc1c806c75789b998c5bdeb9",
    "response.claude.json": "846b209c441a824cd30f58854819a0bdb1f530667aba402a87dc67d4eda72272",
    "response.codex.json": "637b17d0aa771f5b609e4bfa894c3532dae0f0c0ac051fb0d492cdb7c1356653",
    "response.limited.json": "57cc952524e593d832a958825f37f09d8151843fbfef7d58b0df874083d15f60",
    "schema.json": "1144423a98ab38db231dce4a6ac02288c3a2e5741aad62b9611f35a2da96caf6",
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


#: What health.quota (quota_status.v1) carries; no key of the profile may restate it.
QUOTA_KEYS = {"usage_pct", "used_pct", "window", "resets_at", "resumes_at", "limited_until", "limit_state",
              "five_hour", "seven_day", "last_checked", "quota", "usage", "utilization"}


def keys_of(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


def claude(now, model="opus", **kw):
    args = dict(runtime="claude", model=rp.claude_model(model, [], "claude-opus-5-5"), offered=[],
                plan_name="Claude Max", account_digest=DIGEST, now=now)
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
        jsonschema.Draft202012Validator(schema).validate(claude(1789999995))

    def test_no_contract_file_carries_usage_or_limit_state(self):
        schema = json.loads((CONTRACTS / "schema.json").read_text())
        self.assertEqual(set(schema["$defs"]["plan"]["properties"]), {"account", "name"})
        self.assertEqual(schema["$defs"]["plan"]["required"], ["name"])
        for path in sorted(CONTRACTS.glob("*.json")):
            self.assertEqual(keys_of(json.loads(path.read_text())) & QUOTA_KEYS, set(), path.name)

    def test_the_validator_refuses_what_the_schema_refuses(self):
        good = block("heartbeat.claude.json")
        bad = [dict(good, extra=1), dict(good, v=2), dict(good, model=None),
               dict(good, plan=dict(good["plan"], account="someone@example.com")),
               dict(good, plan=dict(good["plan"], usage_pct=14)),
               dict(good, plan=dict(good["plan"], limited_until=None)),
               dict(good, plan={"account": "acct_3f9a2b"}),
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
               dict(good, plan=[]), dict(good, plan={"name": ""}),
               dict(good, workers={}), dict(good, workers=[worker] * 17),
               dict(good, workers=[dict(worker, model={"id": "x"})]),
               dict(good, workers=[dict(worker, plan={"name": "x", "window": "5h"})]),
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
        rows = [{"key": f"o{i:012x}", "label": "Sonnet 5", "model": "Sonnet 5"} for i in range(8)] \
            + [{"key": "bad key", "label": "x", "model": "Sonnet 5"}]
        got = rp.switch_options("claude", rows)
        self.assertEqual(len(got), rp.MAX_OPTIONS)
        self.assertEqual(got[0]["display_name"], "Claude Sonnet 5")

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
    def test_a_claude_core_reads_as_the_fixture(self):
        for case in ("claude", "limited"):
            want = block(f"heartbeat.{case}.json")
            got = claude(want["reported_at"])
            self.assertEqual((got["v"], got["reported_at"], got["model"], got["plan"], got["workers"]),
                             (1, want["reported_at"], want["model"], want["plan"], []), case)

    def test_the_plan_is_its_name_and_account_and_nothing_about_usage(self):
        got = claude(1789999995)
        self.assertEqual(got["plan"], {"account": "acct_3f9a2b", "name": "Claude Max"})
        self.assertEqual(keys_of(got) & QUOTA_KEYS, set(), "quota rides health.quota, never the profile")

    def test_no_plan_name_means_no_plan_and_a_non_digest_never_becomes_the_account(self):
        self.assertIsNone(claude(1789999995, plan_name=None)["plan"])
        plan = claude(1789999995, account_digest="someone@example.com")["plan"]
        self.assertNotIn("account", plan)

    def test_the_model_is_named_by_version_never_by_alias(self):
        self.assertEqual(rp.claude_model("claude-sonnet-5[1m]", [], None),
                         {"id": "claude-sonnet-5[1m]", "display_name": "Claude Sonnet 5", "runtime": "claude"})
        self.assertEqual(rp.claude_model("claude-haiku-4-5-20251001", [], None)["display_name"], "Claude Haiku 4.5")
        self.assertIsNone(rp.claude_model("opus", [], None), "an alias with no version source is unknown")
        self.assertIsNone(rp.claude_model("opus", [], "claude-sonnet-5"), "another family's request says nothing")
        self.assertIsNone(rp.claude_model(None, [], "claude-opus-5-5"))
        self.assertIsNone(rp.derive(runtime="claude", model=None, offered=[], plan_name="Claude Max",
                                    account_digest=DIGEST, now=1),
                          "an unknown core model sends no block: the schema requires one")


class Labels(unittest.TestCase):
    """Names a client shows come from validated ids or a plain plan name, never copied free text."""

    def test_a_plan_name_that_is_not_a_plain_name_is_dropped(self):
        for name in ("Claude Max", "Ultra", "Claude Max 20x", "Pro+"):
            self.assertEqual(claude(1789999995, plan_name=name)["plan"]["name"], name)
        for name in ("someone@example.com", "sk-ant-api03-AbCdEf0123456789", "/Users/someone/.claude",
                     "Max 0123456789abcdef", "Claude Max (team: acme)", "a b c d e"):
            self.assertIsNone(claude(1789999995, plan_name=name)["plan"], name)

    def test_a_switch_is_named_by_the_model_it_runs_not_the_pickers_text(self):
        rows = [{"key": "o0123456789ab", "label": "Opus 5.5 sk-ant-api03-secret", "model": "Opus 5.5",
                 "arg": "claude-opus-5-5"},
                {"key": "o0123456789ac", "label": "Sonnet 5 (1M context)", "model": "Sonnet 5",
                 "arg": "claude-sonnet-5[1m]"},
                {"key": "o0123456789ad", "label": "Mystery", "model": None, "arg": "x"}]
        self.assertEqual(rp.switch_options("claude", rows),
                         [{"token": "o0123456789ab", "display_name": "Claude Opus 5.5"},
                          {"token": "o0123456789ac", "display_name": "Claude Sonnet 5 (1M context)"}])

    def test_a_codex_model_is_named_from_its_id(self):
        row = {"key": "o1", "label": "gpt-5.5 sk-secret", "model": "gpt-5.5", "current": True}
        self.assertEqual(rp.codex_model([row]), {"id": "gpt-5.5", "display_name": "GPT-5.5", "runtime": "codex"})

    def test_the_account_label_is_the_recovery_records_own(self):
        self.assertEqual(claude(1789999995)["plan"]["account"], core_recovery_state.account_label(DIGEST))


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
                            account_digest=None, now=1)
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
    def test_a_codex_core_reports_its_model_and_no_plan(self):
        want = block("heartbeat.codex.json")
        rows = [{"key": "o0123456789ab", "label": "GPT-5.5", "model": "gpt-5.5", "arg": None, "current": True}]
        got = rp.derive(runtime="codex", model=rp.codex_model(rows), offered=rows, plan_name="ChatGPT Pro",
                        account_digest=DIGEST, now=want["reported_at"])
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
        self.now = now
        self.digest = core_limit_hold.account_fingerprint(str(self.cfg))
        self.quota(now, 0.14)

    def write(self, name, data):
        (self.ws / "state" / name).write_text(data if isinstance(data, str) else json.dumps(data))

    def test_the_block_from_the_files(self):
        got = rp.read(self.ws)
        self.assertEqual(got["model"], {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "runtime": "claude"})
        self.assertEqual(set(got["plan"]), {"account", "name"})
        self.assertEqual(got["plan"]["name"], "Claude Max")
        self.assertRegex(got["plan"]["account"], r"^acct_[0-9a-f]{6}$")
        self.assertEqual(keys_of(got) & QUOTA_KEYS, set())
        text = json.dumps(got)
        for private in ("someone@example.com", "u-1", str(self.cfg), str(self.ws)):
            self.assertNotIn(private, text)

    def quota(self, at, util, account=True):
        """The proxy's write; `account` True stamps the signed-in account's label, a str another's."""
        rec = {"last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
               "last_request": {"model": "claude-opus-5-5"},
               "headers": headers(**{"7d": (util, "allowed", int(at) + 86400)})}
        if account:
            rec["account"] = self.label() if account is True else account
        self.write("quota-state.json", rec)

    def model_list(self):
        """The runtime's own list names the model, so it does not hang on the proxy's reading."""
        runtime_models.record(self.ws, "claude", runtime_models.parse_picker(PICKER, "claude"))

    def label(self):
        return core_recovery_state.account_label(core_limit_hold.account_fingerprint(str(self.cfg)))

    def sign_in(self, uuid):
        (self.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "accountUuid": uuid, "emailAddress": "someone@example.com", "planDisplayName": "Claude Max"}}))

    def hold(self, **kw):
        self.write("core-limit-hold.json", dict({"status": "held", "until": self.now + 3600,
                                                 "account": self.digest, "model": "claude-opus-5-5"}, **kw))

    def test_no_quota_reading_or_hold_changes_the_plan(self):
        """A rejected window, a reading at 100% and a binding hold: the plan says none of it."""
        self.model_list()
        base = rp.read(self.ws, self.now + 1)
        rec = {"last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now)), "account": self.label(),
               "last_request": {"model": "claude-opus-5-5"},
               "headers": dict(headers(**{"5h": (1.0, "rejected", int(self.now) + 3600)}),
                               **{"anthropic-ratelimit-unified-overage-status": "rejected"})}
        self.write("quota-state.json", rec)
        self.hold()
        got = rp.read(self.ws, self.now + 1)
        self.assertEqual(got["plan"], base["plan"])
        self.assertEqual(keys_of(got) & QUOTA_KEYS, set())

    def test_another_accounts_last_request_does_not_name_the_model(self):
        """No model list: only the proxy's last request turns the `opus` alias into a version."""
        self.assertEqual(rp.read(self.ws, self.now + 1)["model"]["id"], "claude-opus-5-5")
        a = self.label()
        self.sign_in("u-2")
        self.quota(self.now + 2, 0.5, account=a)
        self.assertIsNone(rp.read(self.ws, self.now + 3), "A's request names no model for B")
        self.quota(self.now + 4, 0.5)
        self.assertEqual(rp.read(self.ws, self.now + 5)["model"]["id"], "claude-opus-5-5", "B's own request does")

    def test_the_status_lines_record_names_the_model_only_for_its_own_login(self):
        """A direct core: the status line stamps its record with the login's full digest."""
        rec = {"source": "statusline", "account": self.digest, "last_request": {"model": "claude-opus-5-5"},
               "last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now))}
        self.write("quota-state.json", rec)
        self.assertEqual(rp.read(self.ws, self.now + 1)["model"]["id"], "claude-opus-5-5")
        self.write("quota-state.json", dict(rec, account="f" * 16))
        self.assertIsNone(rp.read(self.ws, self.now + 1), "a previous login's record names no model")

    def test_an_unlabeled_reading_or_an_unknown_account_names_no_model(self):
        self.quota(self.now, 0.14, account=False)
        self.assertIsNone(rp.read(self.ws, self.now + 1), "an unlabeled request names no model")
        self.quota(self.now, 0.14)
        (self.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"planDisplayName": "Claude Max"}}))
        self.assertIsNone(rp.read(self.ws, self.now + 1), "no account to match the reading to")
        self.model_list()
        plan = rp.read(self.ws, self.now + 1)["plan"]
        self.assertEqual(plan, {"name": "Claude Max"})

    def offered(self, **kw):
        return recovery_actions.switch_offer(self.ws, "claude", **kw)[:rp.MAX_OPTIONS]

    def test_the_switches_are_the_cards_own_while_held(self):
        runtime_models.record(self.ws, "claude", runtime_models.parse_picker(PICKER, "claude"))
        self.write("model-switch.json", {"model": "sonnet"})
        got = [o["token"] for o in rp.read(self.ws, self.now + 1)["switch_options"]]
        self.assertEqual(got, self.offered(), "the executor's allowlist, current read as the card reads it")
        self.hold()
        got = rp.read(self.ws, self.now + 1)["switch_options"]
        self.assertEqual([o["token"] for o in got], self.offered(avoid="claude-opus-5-5"))
        self.assertNotIn("Claude Opus 5.5", [o["display_name"] for o in got], "the held model is not offered")

    def test_the_cli_account_file_is_read_once_per_beat(self):
        real, reads = Path.read_text, []

        def counting(path, *a, **kw):
            if path.name == ".claude.json":
                reads.append(path)
            return real(path, *a, **kw)
        runtime_models.record(self.ws, "claude", runtime_models.parse_picker(PICKER, "claude"))
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=counting):
            self.assertIsNotNone(rp.read(self.ws, self.now + 1))
        self.assertEqual(len(reads), 1)

    def test_a_codex_picker_read_before_this_launch_names_no_model(self):
        rows = [{"key": "gpt-5.5", "label": "gpt-5.5", "model": "gpt-5.5", "arg": None, "current": True}]
        self.write("core-runtime.json", {"runtime": "codex", "session": "s", "started_at": self.now - 60})
        runtime_models.record(self.ws, "codex", rows, now=self.now - 120)
        self.assertIsNone(rp.read(self.ws, self.now), "a picker from the previous launch")
        runtime_models.record(self.ws, "codex", rows, now=self.now - 30)
        self.assertEqual(rp.read(self.ws, self.now)["model"]["id"], "gpt-5.5")
        self.write("core-runtime.json", {"runtime": "codex", "session": "s"})
        self.assertIsNone(rp.read(self.ws, self.now), "no launch time: nothing binds the picker to it")

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
