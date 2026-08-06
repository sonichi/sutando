#!/usr/bin/env python3
"""`vault set KEY VALUE` interception in the gateway bridge (owner gap-report
2026-08-06: an MS365 client secret matched no redaction pattern and persisted
in plaintext; the ag2space lane must give the same store-don't-persist
guarantee as the Slack/Discord bridges).

Contract pinned here, on the SHIPPED loader (src/remote-gateway-bridge.py):
 1. owner-tier + sink wired  -> sink called with (key, value); persisted body
    redacted, plaintext absent, body says stored.
 2. owner-tier + NO sink     -> no storage; body redacted, says not-stored.
 3. non-owner sender          -> sink NOT called even when wired; body redacted,
    says declined.
 4. prose mentioning the command is NOT intercepted (whole-body match only).

Run: python3 tests/gateway-vault-intercept.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"

SECRET = "si68Q-testvalue-not-a-real-secret"


def _load(name: str, ws: str):
    """Load the shipped loader with sandboxed dirs. The CALLER owns env
    lifetime — CLAUDE_CONFIG_DIR must stay applied across _write_task calls
    (the tierMap is resolved per call), so setUp applies and tearDown restores."""
    spec = importlib.util.spec_from_file_location(name, _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    # Override the module GLOBALS directly (post-exec set_dirs() cannot rebind
    # names the module captured at import — the exact mistake that let a draft
    # of this test write into the LIVE tasks dir).
    mod.TASKS_DIR = Path(ws) / "tasks"
    mod.RESULTS_DIR = Path(ws) / "results"
    mod.ARCHIVE_RESULTS_DIR = Path(ws) / "results" / "archive"
    assert str(mod.TASKS_DIR).startswith(ws), "test dirs must be sandboxed"
    return mod


class TestVaultIntercept(unittest.TestCase):
    def _task(self, body: str, user: str = "@qingyun:ag2.space") -> dict:
        return {"id": "task-vaulttest%d" % self._i, "task": body, "user_id": user,
                "source": "ag2space", "channel_id": "!r:ag2.space"}

    def setUp(self):
        self._i = 0
        self.tmp = tempfile.TemporaryDirectory()
        ws = Path(self.tmp.name)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()
        (ws / "state").mkdir()
        self.ws = ws
        env = {
            "CLAUDE_CONFIG_DIR": str(ws / "cfg"),
            "REMOTE_TASK_TOKEN": "https://gw.example|s",
        }
        self._old_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.mod = _load("rgb_vault_intercept", str(ws))

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _write(self, body, user="@qingyun:ag2.space"):
        self._i += 1
        tid = self.mod._write_task(self._task(body, user))
        return (self.ws / "tasks" / f"{tid}.txt").read_text()

    def test_owner_with_sink_stores_and_redacts(self):
        calls = []
        self.mod.VAULT_SINK = lambda k, v: calls.append((k, v)) or True
        content = self._write(f"vault set MS365_CLIENT_SECRET {SECRET}")
        self.assertEqual(calls, [("MS365_CLIENT_SECRET", SECRET)])
        self.assertNotIn(SECRET, content)
        self.assertIn("stored to the vault", content)

    def test_owner_without_sink_redacts_but_does_not_store(self):
        self.mod.VAULT_SINK = None
        content = self._write(f"vault set K1 {SECRET}")
        self.assertNotIn(SECRET, content)
        self.assertIn("no vault sink configured", content)

    def test_non_owner_never_reaches_sink(self):
        calls = []
        self.mod.VAULT_SINK = lambda k, v: calls.append((k, v)) or True
        # Down-tier the sender via the lane's tierMap (LOCAL file the bridge reads).
        import json
        acc = Path(os.environ.get("CLAUDE_CONFIG_DIR", "")) or self.ws / "cfg"
        acc_dir = self.ws / "cfg" / "channels" / "ag2space"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "access.json").write_text(
            json.dumps({"tierMap": {"@teammate:ag2.space": "team"}}))
        content = self._write(f"vault set K2 {SECRET}", user="@teammate:ag2.space")
        self.assertEqual(calls, [])
        self.assertNotIn(SECRET, content)
        self.assertIn("NOT stored", content)

    def test_prose_mention_not_intercepted(self):
        calls = []
        self.mod.VAULT_SINK = lambda k, v: calls.append((k, v)) or True
        content = self._write("how do I use vault set KEY VALUE from here?")
        self.assertEqual(calls, [])
        self.assertIn("vault set KEY VALUE", content)

    def test_sink_failure_still_redacts(self):
        def _boom(k, v):
            raise RuntimeError("keychain locked")
        self.mod.VAULT_SINK = _boom
        content = self._write(f"vault set K3 {SECRET}")
        self.assertNotIn(SECRET, content)
        self.assertIn("NOT stored", content)


class TestCanonicalSrcCopy(unittest.TestCase):
    """The canonical src/chat_secret_filter.py (package copy is generated from
    it) must carry the same vault-set behavior — covered directly so the diff
    gate sees the canonical lines, not just the package mirror."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "csf_canonical", Path(__file__).resolve().parent.parent / "src" / "chat_secret_filter.py")
        self.csf = importlib.util.module_from_spec(spec)
        # dataclass decoration resolves cls.__module__ via sys.modules — the
        # module must be registered BEFORE exec or the import dies.
        sys.modules["csf_canonical"] = self.csf
        spec.loader.exec_module(self.csf)

    def test_extract_shapes(self):
        self.assertEqual(self.csf.extract_vault_set("vault set K v"), ("K", "v"))
        self.assertIsNone(self.csf.extract_vault_set("say vault set K v"))
        self.assertIsNone(self.csf.extract_vault_set("vault set K v\nx"))
        self.assertIsNone(self.csf.extract_vault_set(""))

    def test_replacement_branches_never_contain_value(self):
        for kwargs, marker in (
            (dict(stored=True, owner=True), "stored to the vault"),
            (dict(stored=False, owner=False), "not owner-tier"),
            (dict(stored=False, owner=True), "no vault sink configured"),
        ):
            body = self.csf.vault_set_replacement("KEY", **kwargs)
            self.assertIn("KEY", body)
            self.assertIn(marker, body)
            self.assertIn("REDACTED", body)


class TestKeychainSinkWiring(unittest.TestCase):
    """The loader's _keychain_vault_sink shells to skills/secret-vault with the
    value on STDIN (never argv) and maps the exit code to bool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ws = Path(self.tmp.name)
        for d in ("tasks", "results", "state"):
            (ws / d).mkdir()
        env = {"CLAUDE_CONFIG_DIR": str(ws / "cfg"),
               "REMOTE_TASK_TOKEN": "https://gw.example|s"}
        self._old_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.mod = _load("rgb_sink_wiring", str(ws))

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_sink_invokes_secret_vault_with_stdin_value(self):
        import subprocess as sp
        from unittest.mock import patch
        with patch.object(sp, "run") as run:
            run.return_value = type("R", (), {"returncode": 0})()
            ok = self.mod._keychain_vault_sink("K1", "v-secret")
        self.assertTrue(ok)
        args, kwargs = run.call_args
        self.assertIn("secret-vault.py", " ".join(map(str, args[0])))
        self.assertEqual(args[0][-2:], ["set", "K1"])
        self.assertEqual(kwargs.get("input"), b"v-secret")
        self.assertNotIn("v-secret", " ".join(map(str, args[0])))

    def test_sink_maps_nonzero_to_false(self):
        import subprocess as sp
        from unittest.mock import patch
        with patch.object(sp, "run") as run:
            run.return_value = type("R", (), {"returncode": 1})()
            self.assertFalse(self.mod._keychain_vault_sink("K1", "v"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
