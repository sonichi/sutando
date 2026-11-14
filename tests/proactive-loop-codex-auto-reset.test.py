#!/usr/bin/env python3
"""Exercise earned-reset policy without contacting a live Codex account."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/proactive-loop/scripts/codex-auto-reset.py"
spec = importlib.util.spec_from_file_location("codex_auto_reset", SCRIPT)
assert spec and spec.loader
reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)
NOW = 1_800_000_000


def limits(*, used: float = 100, seconds_left: int = 2 * 86400,
           credits: int = 1, account_id: str = "account-1",
           credit_rows: list[dict] | None = None) -> dict:
    return {
        "accountId": account_id,
        "rateLimits": {"limitId": "codex", "primary": None, "secondary": None},
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 100, "windowDurationMins": 300,
                            "resetsAt": NOW + 300},
                "secondary": {"usedPercent": used,
                              "windowDurationMins": 10080,
                              "resetsAt": NOW + seconds_left},
            },
        },
        "rateLimitResetCredits": {
            "availableCount": credits,
            "credits": credit_rows if credit_rows is not None else [
                {"id": "credit-1", "status": "available",
                 "resetType": "codexRateLimits", "expiresAt": None}],
        },
    }


class FakeServer:
    def __init__(self, snapshots: list[dict] | None = None,
                 outcomes: list[str | Exception] | None = None,
                 account_id: str = "account-1",
                 account_ids: list[str] | None = None):
        self.snapshots = list(snapshots or [limits()])
        self.outcomes = list(outcomes or ["reset"])
        self.account_id = account_id
        self.account_ids = list(account_ids) if account_ids is not None else None
        self.calls: list[tuple[str, dict | None]] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "account/read":
            account_id = (self.account_ids.pop(0) if len(self.account_ids) > 1
                          else self.account_ids[0]) if self.account_ids is not None else self.account_id
            return {"account": {"type": "chatgpt", "email": "test@example.com"},
                    "workspaceRouting": {"chatgptAccountId": account_id}}
        if method == "account/rateLimits/read":
            return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if method == "account/rateLimitResetCredit/consume":
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return {"outcome": outcome}
        raise AssertionError(method)

    @property
    def consumes(self) -> list[dict]:
        return [params for method, params in self.calls
                if method == "account/rateLimitResetCredit/consume"]


class ResetPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex"
        self.home.mkdir()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        patch = mock.patch.dict(os.environ, {reset.ENABLE_ENV: "1"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_fractional_threshold_and_weekly_window(self) -> None:
        below = FakeServer([limits(used=99.89)])
        self.assertEqual(reset.tick(below, self.workspace, NOW)["status"], "above-reserve")
        self.assertEqual(below.consumes, [])
        exact = FakeServer([limits(used=99.9), limits(used=99.9), limits(used=0)])
        self.assertEqual(reset.tick(exact, self.workspace, NOW)["status"], "reset")
        self.assertEqual(len(exact.consumes), 1)
        self.assertEqual(exact.consumes[0]["creditId"], "credit-1")

    def test_one_full_day_boundary(self) -> None:
        soon = FakeServer([limits(seconds_left=86399)])
        self.assertEqual(reset.tick(soon, self.workspace, NOW)["status"], "weekly-reset-soon")
        self.assertEqual(soon.consumes, [])
        boundary = FakeServer([limits(seconds_left=86400)])
        self.assertEqual(reset.tick(boundary, self.workspace, NOW)["status"], "reset")

    def test_rechecks_day_boundary_immediately_before_consume(self) -> None:
        server = FakeServer([limits(seconds_left=86400)])
        with mock.patch.object(reset.time, "time", side_effect=[NOW, NOW + 1]):
            self.assertEqual(reset.tick(server, self.workspace)["status"], "weekly-reset-soon")
        self.assertEqual(server.consumes, [])
        state = reset._read_state(reset._state_path(self.workspace, "account-1"), "account-1")
        self.assertIsNone(state["pending"])

    def test_no_available_credit(self) -> None:
        server = FakeServer([limits(credits=0, credit_rows=[])])
        self.assertEqual(reset.tick(server, self.workspace, NOW)["status"], "no-credit")
        self.assertEqual(server.consumes, [])

    def test_details_must_name_an_eligible_credit(self) -> None:
        invalid = [{"id": "wrong", "status": "available", "resetType": "unknown"}]
        server = FakeServer([limits(credit_rows=invalid)])
        self.assertEqual(reset.tick(server, self.workspace, NOW)["status"], "no-credit")
        self.assertEqual(server.consumes, [])

    def test_count_only_credit_omits_credit_id(self) -> None:
        snapshot = limits()
        snapshot["rateLimitResetCredits"]["credits"] = None
        server = FakeServer([snapshot])
        self.assertEqual(reset.tick(server, self.workspace, NOW)["status"], "reset")
        self.assertNotIn("creditId", server.consumes[0])

    def test_invalid_or_mismatched_account_fails_closed(self) -> None:
        mismatch = FakeServer([limits(account_id="other")])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(mismatch, self.workspace, NOW)
        self.assertEqual(mismatch.consumes, [])
        missing = FakeServer([limits(account_id="")], account_id="")
        with self.assertRaises(reset.AutoResetError):
            reset.tick(missing, self.workspace, NOW)
        self.assertEqual(missing.consumes, [])

    def test_unsupported_account_and_old_cli_skip_without_spending(self) -> None:
        class MissingRouting(FakeServer):
            def call(self, method: str, params: dict | None = None) -> dict:
                result = super().call(method, params)
                if method == "account/read":
                    result.pop("workspaceRouting")
                return result

        old_cli = MissingRouting([limits()])
        self.assertEqual(reset.tick(old_cli, self.workspace, NOW)["status"],
                         "unsupported-codex-cli")
        self.assertEqual(old_cli.consumes, [])

        class ApiKeyAccount(FakeServer):
            def call(self, method: str, params: dict | None = None) -> dict:
                result = super().call(method, params)
                if method == "account/read":
                    result["account"]["type"] = "apiKey"
                return result

        api_key = ApiKeyAccount([limits()])
        self.assertEqual(reset.tick(api_key, self.workspace, NOW)["status"],
                         "unsupported-account")
        self.assertEqual(api_key.consumes, [])

    def test_account_switch_with_null_usage_id_fails_closed(self) -> None:
        snapshot = limits()
        snapshot["accountId"] = None
        switched_under_lock = FakeServer([snapshot], account_ids=["account-1", "account-2"])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(switched_under_lock, self.workspace, NOW)
        self.assertEqual(switched_under_lock.consumes, [])
        switched_before_consume = FakeServer(
            [snapshot], account_ids=["account-1", "account-1", "account-2"])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(switched_before_consume, self.workspace, NOW)
        self.assertEqual(switched_before_consume.consumes, [])

    def test_invalid_weekly_fields_fail_closed(self) -> None:
        for value in (float("nan"), True, "100", 101):
            with self.subTest(value=value):
                server = FakeServer([limits(used=value)])
                with self.assertRaises(reset.AutoResetError):
                    reset.tick(server, self.workspace, NOW)
                self.assertEqual(server.consumes, [])

    def test_monthly_window_is_not_weekly_quota(self) -> None:
        snapshot = limits(used=0)
        snapshot["rateLimitsByLimitId"]["codex"]["primary"] = {
            "usedPercent": 100, "windowDurationMins": 30 * 1440,
            "resetsAt": NOW + 10 * 86400}
        server = FakeServer([snapshot])
        self.assertEqual(reset.tick(server, self.workspace, NOW)["status"], "above-reserve")
        self.assertEqual(server.consumes, [])
        snapshot["rateLimitsByLimitId"]["codex"]["secondary"] = None
        with self.assertRaises(reset.AutoResetError):
            reset.tick(FakeServer([snapshot]), self.workspace, NOW)

    def test_invalid_credit_count_fails_closed(self) -> None:
        snapshot = limits()
        snapshot["rateLimitResetCredits"]["availableCount"] = True
        server = FakeServer([snapshot])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(server, self.workspace, NOW)
        self.assertEqual(server.consumes, [])

    def test_timeout_preserves_key_and_retries_same_attempt(self) -> None:
        first = FakeServer([limits()], [reset.AutoResetError("timed out")])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(first, self.workspace, NOW)
        key = first.consumes[0]["idempotencyKey"]
        state = reset._read_state(reset._state_path(self.workspace, "account-1"), "account-1")
        self.assertEqual(state["pending"]["key"], key)
        second = FakeServer([limits()], ["alreadyRedeemed"])
        self.assertEqual(reset.tick(second, self.workspace, NOW)["status"], "alreadyRedeemed")
        self.assertEqual(second.consumes[0]["idempotencyKey"], key)
        self.assertIsNone(reset._read_state(reset._state_path(self.workspace, "account-1"), "account-1")["pending"])

    def test_no_second_spend_until_later_low_sample(self) -> None:
        first = FakeServer([limits()])
        self.assertEqual(reset.tick(first, self.workspace, NOW)["status"], "reset")
        stale = FakeServer([limits()])
        self.assertEqual(reset.tick(stale, self.workspace, NOW)["status"], "awaiting-lower-sample")
        self.assertEqual(stale.consumes, [])
        low = FakeServer([limits(used=0)])
        self.assertEqual(reset.tick(low, self.workspace, NOW)["status"], "rearmed")
        new_usage = FakeServer([limits()])
        self.assertEqual(reset.tick(new_usage, self.workspace, NOW)["status"], "reset")
        self.assertNotEqual(first.consumes[0]["idempotencyKey"],
                            new_usage.consumes[0]["idempotencyKey"])

    def test_window_identity_change_stays_blocked_until_lower_sample(self) -> None:
        self.assertEqual(reset.tick(FakeServer([limits()]), self.workspace, NOW)["status"], "reset")
        changed = FakeServer([limits(seconds_left=3 * 86400)])
        self.assertEqual(reset.tick(changed, self.workspace, NOW)["status"], "awaiting-lower-sample")
        self.assertEqual(changed.consumes, [])
        low = FakeServer([limits(used=0, seconds_left=3 * 86400)])
        self.assertEqual(reset.tick(low, self.workspace, NOW)["status"], "rearmed")
        renewed = FakeServer([limits(seconds_left=3 * 86400)])
        self.assertEqual(reset.tick(renewed, self.workspace, NOW)["status"], "reset")

    def test_ambiguous_attempt_keeps_key_across_window_change(self) -> None:
        first = FakeServer([limits()], [reset.AutoResetError("lost response")])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(first, self.workspace, NOW)
        pending_key = first.consumes[0]["idempotencyKey"]
        changed = FakeServer([limits(seconds_left=3 * 86400)])
        self.assertEqual(reset.tick(changed, self.workspace, NOW)["status"], "awaiting-lower-sample")
        self.assertEqual(changed.consumes, [])
        state = reset._read_state(reset._state_path(self.workspace, "account-1"), "account-1")
        self.assertEqual(state["pending"]["key"], pending_key)
        low = FakeServer([limits(used=0, seconds_left=3 * 86400)])
        self.assertEqual(reset.tick(low, self.workspace, NOW)["status"], "reconciled")
        self.assertIsNone(reset._read_state(reset._state_path(self.workspace, "account-1"), "account-1")["pending"])

    def test_disabled_gate_does_not_read_account(self) -> None:
        with mock.patch.dict(os.environ, {reset.ENABLE_ENV: "0"}):
            server = FakeServer([limits()])
            self.assertEqual(reset.tick(server, self.workspace, NOW)["status"], "disabled")
            self.assertEqual(server.calls, [])

    def test_invalid_or_missing_gate_fails_closed(self) -> None:
        manifest = Path(self.temp.name) / "manifest.json"
        manifest.write_text(json.dumps({"config": {reset.ENABLE_ENV: "1"}}))
        self.assertTrue(reset.enabled({}, manifest))
        self.assertFalse(reset.enabled({reset.ENABLE_ENV: "perhaps"}, manifest))
        manifest.write_text("{}")
        self.assertFalse(reset.enabled({}, manifest))
        self.assertFalse(reset.enabled({}, manifest.parent / "missing.json"))

    def test_account_and_fallback_limit_validation(self) -> None:
        with self.assertRaises(reset.AutoResetError):
            reset._account_id({"account": {"type": "apiKey"}}, limits())
        bad = limits()
        bad["accountId"] = 7
        with self.assertRaises(reset.AutoResetError):
            reset._account_id({"account": {"type": "chatgpt"},
                               "workspaceRouting": {"chatgptAccountId": "account-1"}}, bad)
        fallback = limits()
        fallback.pop("rateLimitsByLimitId")
        fallback["rateLimits"] = limits()["rateLimitsByLimitId"]["codex"]
        self.assertEqual(reset._weekly_window(fallback)[0], "codex")
        fallback["rateLimits"]["limitId"] = "spark"
        with self.assertRaises(reset.AutoResetError):
            reset._weekly_window(fallback)

    def test_consume_outcomes_and_post_read_mismatch(self) -> None:
        no_credit = FakeServer([limits()], ["noCredit"])
        self.assertEqual(reset.tick(no_credit, self.workspace, NOW)["status"], "no-credit")
        unknown = FakeServer([limits()], ["unknown"])
        with self.assertRaises(reset.AutoResetError):
            reset.tick(unknown, self.workspace, NOW)
        path = reset._state_path(self.workspace, "account-1")
        path.unlink()
        mismatch = FakeServer([limits(), limits(), limits(account_id="other")])
        result = reset.tick(mismatch, self.workspace, NOW)
        self.assertEqual(result["status"], "reset")
        self.assertIsNone(result["postResetUsedPercent"])

    def test_corrupt_state_fails_closed(self) -> None:
        path = reset._state_path(self.workspace, "account-1")
        path.parent.mkdir(parents=True)
        base = {"version": 1, "accountId": "account-1", "pending": None,
                "blockedWindow": None, "armed": False}
        malformed = ["not-json", json.dumps({key: value for key, value in base.items()
                                              if key != "pending"}),
                     json.dumps({**base, "pending": {"key": 7, "window": "codex:10080:1"}}),
                     json.dumps({**base, "pending": {"key": "not-a-uuid", "window": "x"}}),
                     json.dumps({**base, "pending": {"key": "ca4b3f5c-bb13-475a-9898-5e849124220b",
                                                     "window": 7}})]
        for payload in malformed:
            with self.subTest(payload=payload):
                path.write_text(payload)
                server = FakeServer([limits()])
                with self.assertRaises(reset.AutoResetError):
                    reset.tick(server, self.workspace, NOW)
                self.assertEqual(server.consumes, [])


FAKE_APP_SERVER = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

now = int(os.environ["FAKE_RESET_NOW"])
used = float(os.environ.get("FAKE_RESET_USED", "100"))
record = os.environ["FAKE_RESET_RECORD"]
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {"serverInfo": {"name": "fake", "version": "1"}}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt"},
                  "workspaceRouting": {"chatgptAccountId": "account-1"}}
    elif method == "account/rateLimits/read":
        result = {"accountId": "account-1",
                  "rateLimits": {"limitId": "codex"},
                  "rateLimitsByLimitId": {"codex": {
                      "limitId": "codex", "secondary": {
                          "usedPercent": used, "windowDurationMins": 10080,
                          "resetsAt": now + 172800}}},
                  "rateLimitResetCredits": {"availableCount": 1, "credits": None}}
    elif method == "account/rateLimitResetCredit/consume":
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(request["params"]) + "\n")
            stream.flush()
        time.sleep(0.3)
        result = {"outcome": "reset"}
    else:
        result = {}
    sys.stdout.write(json.dumps({"id": request["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


class FakeAppServerProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.other_home = self.root / "other-home"
        self.workspace = self.root / "workspace"
        self.home.mkdir()
        self.other_home.mkdir()
        self.workspace.mkdir()
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.fake.chmod(0o755)
        self.record = self.root / "consumes.jsonl"
        self.env = {**os.environ, reset.ENABLE_ENV: "1",
                    "FAKE_RESET_NOW": str(int(time.time())),
                    "FAKE_RESET_RECORD": str(self.record)}

    def command(self, home: Path | None = None) -> list[str]:
        return [sys.executable, str(SCRIPT), "--workspace", str(self.workspace),
                "--codex-home", str(home or self.home), "--codex-bin", str(self.fake), "--json"]

    def test_stdio_protocol_and_cross_process_lock(self) -> None:
        processes = [subprocess.Popen(self.command(home), env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True) for home in (self.home, self.other_home)]
        outputs = [process.communicate(timeout=20) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0], outputs)
        statuses = [json.loads(out)["status"] for out, _err in outputs]
        self.assertCountEqual(statuses, ["reset", "awaiting-lower-sample"])
        self.assertEqual(len(self.record.read_text().splitlines()), 1)

    def test_app_server_client_and_tick_in_process(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            with reset.AppServer(str(self.fake), self.home, self.workspace) as server:
                result = reset.tick(server, self.workspace, int(self.env["FAKE_RESET_NOW"]))
        self.assertEqual(result["status"], "reset")
        self.assertEqual(len(self.record.read_text().splitlines()), 1)

    def test_cli_main_in_process(self) -> None:
        argv = [str(SCRIPT), "--workspace", str(self.workspace),
                "--codex-home", str(self.home), "--codex-bin", str(self.fake), "--json"]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", argv):
            with mock.patch.object(sys, "stdout", output):
                self.assertEqual(reset.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "reset")

    def test_cli_disabled_and_error_in_process(self) -> None:
        argv = [str(SCRIPT), "--workspace", str(self.workspace),
                "--codex-home", str(self.home), "--codex-bin", str(self.fake), "--json"]
        output = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, reset.ENABLE_ENV: "0"}):
            with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stdout", output):
                self.assertEqual(reset.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "disabled")
        output = io.StringIO()
        bad_argv = [*argv[:-2], str(self.root / "missing-codex"), "--json"]
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(sys, "argv", bad_argv), mock.patch.object(sys, "stdout", output):
                self.assertEqual(reset.main(), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_disable_prevents_app_server_launch(self) -> None:
        env = {**self.env, reset.ENABLE_ENV: "0"}
        process = subprocess.run(self.command(), env=env, capture_output=True,
                                 text=True, timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)["status"], "disabled")
        self.assertFalse(self.record.exists())


class AppServerFailureTests(unittest.TestCase):
    def client(self) -> reset.AppServer:
        client = reset.AppServer.__new__(reset.AppServer)
        client.buffer = bytearray()
        client.next_id = 1
        client.selector = mock.Mock()
        client.process = types.SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        return client

    def test_invalid_and_oversized_json_lines(self) -> None:
        client = self.client()
        deadline = time.monotonic() + 1
        for payload in (b"[]\n", b"{bad}\n", b'{"x":NaN}\n',
                        b"x" * (reset.MAX_RPC_LINE_BYTES + 1) + b"\n",
                        b"x" * (reset.MAX_RPC_LINE_BYTES + 1)):
            with self.subTest(size=len(payload)):
                client.buffer = bytearray(payload)
                with self.assertRaises(reset.AutoResetError):
                    client._line(deadline)

    def test_line_timeout_and_closed_pipe(self) -> None:
        client = self.client()
        with self.assertRaises(reset.AutoResetError):
            client._line(time.monotonic() - 1)
        client.selector.select.return_value = []
        with self.assertRaises(reset.AutoResetError):
            client._line(time.monotonic() + 1)
        client.selector.select.return_value = [1]
        client.process.stdout.fileno.return_value = 7
        with mock.patch.object(reset.os, "read", return_value=b""):
            with self.assertRaises(reset.AutoResetError):
                client._line(time.monotonic() + 1)

    def test_pipe_and_protocol_errors(self) -> None:
        client = self.client()
        client.process.stdin.write.side_effect = BrokenPipeError()
        with self.assertRaises(reset.AutoResetError):
            client._send({"method": "x"})
        client._send = mock.Mock()
        client._line = mock.Mock(side_effect=[{"method": "notice"}, {"id": 1, "result": {}}])
        self.assertEqual(client.call("read"), {})
        client._line = mock.Mock(return_value={"id": 3, "result": {}})
        with self.assertRaises(reset.AutoResetError):
            client.call("read")
        client._line = mock.Mock(return_value={"id": 3, "error": {"code": -1}})
        with self.assertRaises(reset.AutoResetError):
            client.call("read")
        client._line = mock.Mock(return_value={"method": "notice"})
        with self.assertRaises(reset.AutoResetError):
            client.call("read")

    def test_startup_failure_closes_process_and_kill_timeout(self) -> None:
        client = self.client()
        client.call = mock.Mock(side_effect=reset.AutoResetError("failed initialize"))
        client.close = mock.Mock()
        with self.assertRaises(reset.AutoResetError):
            client.__enter__()
        client.close.assert_called_once()
        client = self.client()
        client.process = mock.Mock()
        client.process.poll.return_value = None
        client.process.wait.side_effect = [subprocess.TimeoutExpired("fake", 2), None]
        client.process.stdin = None
        client.process.stdout = None
        client.close()
        client.process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
