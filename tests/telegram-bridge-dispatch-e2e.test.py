#!/usr/bin/env python3
"""End-to-end coverage of telegram-bridge main()'s inbound dispatch (#898).

tests/telegram-bridge-tofu.test.py and telegram-bridge-access.test.py pin
load_allowed() and tofu_onboard() one at a time. This file drives the real
main() poll loop, with only urllib's Telegram boundary faked, and checks the
wiring between them: who gets onboarded when access.json is missing, who passes
the allowlist gate (an onboarded or listed owner gets a task, anyone else is
dropped without TOFU), and that deleting access.json later still leaves the
enrollment-code gate armed (#899).

Run: python3 tests/telegram-bridge-dispatch-e2e.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BRIDGE_SRC = REPO / "src" / "telegram-bridge.py"
TOKEN = "synthetic-telegram-token"
API_PREFIX = f"https://api.telegram.org/bot{TOKEN}/"
OWNER, STRANGER, RACER = 4242, 777, 9001

# Seeded before any bridge import (lint-hermetic-bridge-tests); each Scenario then repoints
# CLAUDE_CONFIG_DIR at its own temp dir, since the bridge resolves ACCESS_FILE at import.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-dispatch-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')
sys.path.insert(0, str(REPO / "src"))


class _Stop(BaseException):
    """Ends main(): its poll loop swallows Exception, so this must not be one."""


class Scenario:
    """One bridge process: a fresh config dir, workspace and module, fed scripted getUpdates batches."""

    def __init__(self, access: "str | None"):
        self.config = Path(tempfile.mkdtemp(prefix="ccd-tg-dispatch-"))
        self.ws = Path(tempfile.mkdtemp(prefix="tg-dispatch-ws-"))
        self.channel = self.config / "channels" / "telegram"
        self.channel.mkdir(parents=True)
        self.access = self.channel / "access.json"
        if access is not None:
            self.access.write_text(access)
        os.environ.update({
            "CLAUDE_CONFIG_DIR": str(self.config), "TELEGRAM_BOT_TOKEN": TOKEN,
            "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
            "SUTANDO_TELEMETRY": "0", "SUTANDO_PROGRESS_STREAM": "0",
        })
        spec = importlib.util.spec_from_file_location("telegram_bridge_dispatch_e2e", BRIDGE_SRC)
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        assert self.bridge.ACCESS_FILE == self.access, self.bridge.ACCESS_FILE
        assert self.bridge.TASKS_DIR == self.ws.resolve() / "tasks", self.bridge.TASKS_DIR
        self.calls: list = []
        self.log = ""

    def run(self, *batches):
        """Run main() over `batches`; a callable batch is called (with the bridge) at that poll."""
        pending = list(batches)

        def urlopen(req, **_kw):
            assert req.full_url.startswith(API_PREFIX), req.full_url
            method = req.full_url[len(API_PREFIX):]
            params = json.loads(req.data) if req.data else {}
            self.calls.append((method, params))
            if method == "getMe":
                result = {"username": "synthetic_bot", "can_read_all_group_messages": False}
            elif method == "getUpdates":
                if not pending:
                    raise _Stop()
                batch = pending.pop(0)
                result = batch(self.bridge) if callable(batch) else batch
            elif method in ("sendChatAction", "sendMessage"):
                result = True if method == "sendChatAction" else {"message_id": 1}
            else:
                raise AssertionError(f"unexpected Telegram call: {method}")
            return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())

        out = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(time, "sleep"), \
                mock.patch.object(self.bridge, "tofu_onboard", wraps=self.bridge.tofu_onboard) as tofu, \
                contextlib.redirect_stdout(out):
            try:
                self.bridge.main()
            except _Stop:
                pass
        self.log = out.getvalue()
        return tofu

    def sent(self, method):
        return [p for m, p in self.calls if m == method]

    def task(self, update) -> Path:
        msg = update["message"]
        return self.bridge.TASKS_DIR / f"task-tg{msg['chat']['id']}~{msg['message_id']}.txt"

    def tasks(self):
        return sorted(p.name for p in self.bridge.TASKS_DIR.glob("task-*.txt"))


_next_id = [100]


def dm(sender, text, username=None):
    _next_id[0] += 1
    mid = _next_id[0]
    return {"update_id": mid, "message": {
        "message_id": mid, "from": {"id": sender, "username": username or f"user{sender}"},
        "chat": {"id": sender, "type": "private"}, "text": text,
    }}


class TelegramDispatch(unittest.TestCase):

    def assertAccepted(self, s, update, text):
        body = s.task(update).read_text()
        self.assertIn("source: telegram\n", body)
        self.assertIn(f"chat_id: {update['message']['chat']['id']}\n", body)
        self.assertIn(f"[Telegram @{update['message']['from']['username']}] {text}", body)

    def test_missing_access_onboards_first_dm_with_code_as_owner(self):
        s = Scenario(access=None)
        no_code = dm(STRANGER, "hello bot")
        holder = {}

        def owner_then_stranger(bridge):
            holder["owner"] = dm(OWNER, f"hi it's me {bridge._TOFU_ENROLLMENT_CODE}", "owner")
            holder["late"] = dm(STRANGER, f"me too {bridge._TOFU_ENROLLMENT_CODE}")
            return [holder["owner"], holder["late"]]

        tofu = s.run([no_code], owner_then_stranger)
        owner, late = holder["owner"], holder["late"]

        prompts = s.sent("sendMessage")
        self.assertEqual(len(prompts), 1, s.log)
        self.assertEqual(prompts[0]["chat_id"], STRANGER)
        self.assertIn("Enrollment code required", prompts[0]["text"])
        tofu.assert_called_once_with(str(OWNER), "owner")
        data = json.loads(s.access.read_text())
        self.assertEqual(data["allowFrom"], [str(OWNER)])
        self.assertEqual(data["tofuOwner"], str(OWNER))
        self.assertEqual(data["tofuOnboardedUsername"], "owner")
        if os.name != "nt":
            self.assertEqual(s.access.stat().st_mode & 0o777, 0o600)
        self.assertAccepted(s, owner, owner["message"]["text"])
        self.assertEqual(s.tasks(), [s.task(owner).name], "only the onboarded owner's DM becomes a task")
        self.assertEqual(s.sent("sendChatAction"), [{"chat_id": OWNER, "action": "typing"}])
        self.assertIn(f"Dropped message from non-allowed @{late['message']['from']['username']}", s.log)

    def test_listed_owner_dm_becomes_a_task(self):
        s = Scenario(access=json.dumps({"allowFrom": [str(OWNER)]}))
        before = s.access.read_bytes()
        update = dm(OWNER, "please summarise the notes", "owner")
        tofu = s.run([update])
        self.assertIsNone(s.bridge._TOFU_ENROLLMENT_CODE)
        tofu.assert_not_called()
        self.assertAccepted(s, update, "please summarise the notes")
        self.assertEqual(s.tasks(), [s.task(update).name])
        self.assertEqual(s.sent("sendChatAction"), [{"chat_id": OWNER, "action": "typing"}])
        self.assertEqual(s.sent("sendMessage"), [])
        self.assertEqual(s.access.read_bytes(), before)

    def test_unlisted_sender_is_dropped_without_tofu(self):
        s = Scenario(access=json.dumps({"allowFrom": [str(OWNER)]}))
        before = s.access.read_bytes()
        update = dm(STRANGER, "let me in")
        tofu = s.run([update])
        tofu.assert_not_called()
        self.assertEqual(s.tasks(), [])
        self.assertEqual(s.sent("sendChatAction"), [])
        self.assertEqual(s.sent("sendMessage"), [])
        self.assertEqual(s.access.read_bytes(), before)
        self.assertIn(f"Dropped message from non-allowed @user{STRANGER}", s.log)

    def test_access_deleted_after_enrolment_still_requires_the_code(self):
        s = Scenario(access=None)
        holder = {}

        def enrol(bridge):
            holder["owner"] = dm(OWNER, f"code {bridge._TOFU_ENROLLMENT_CODE}", "owner")
            return [holder["owner"]]

        def delete_then_stranger(bridge):
            bridge.ACCESS_FILE.unlink()
            return [dm(STRANGER, "I'll be the owner now")]

        tofu = s.run(enrol, delete_then_stranger)
        tofu.assert_called_once_with(str(OWNER), "owner")
        self.assertFalse(s.access.exists(), "no code, so the stranger is never onboarded")
        self.assertEqual(s.tasks(), [s.task(holder["owner"]).name])
        prompts = s.sent("sendMessage")
        self.assertEqual([p["chat_id"] for p in prompts], [STRANGER])
        self.assertIn("Enrollment code required", prompts[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
