#!/usr/bin/env python3
"""gateway() channel-.env tier — coverage-gate home.

Sibling of `room_ops_gateway_vault.test.py`, for the same reason its docstring
gives: the diff-coverage gate discovers only `tests/*.test.py`
(`scripts/coverage-gate.sh` -> `find tests -name '*.test.py'`), while the room-ops
suite lives under `skills/agent-room-ops/`. Those tests DO run in CI
(`ci.yml` invokes the file directly) but not under instrumentation, so the changed
`_gateway.py` lines read as uncovered. This file drives the shipped symbols
in-process so the gate measures them.

Hermeticity: `_channel_env_file()` resolves a REAL path under $CLAUDE_CONFIG_DIR,
so every test here either injects `env_file=` explicitly or points
CLAUDE_CONFIG_DIR at a temp dir. Nothing reads the operator's channel .env.

Run: python3 tests/room_ops_gateway_channel_env.test.py
"""
import os
import sys
import types
import shutil
import pathlib
import tempfile
import unittest
from unittest import mock

_VAULT_STORE: dict = {}


def _fake_get_vault_key(var):
    if var in _VAULT_STORE:
        return _VAULT_STORE[var]
    raise KeyError(var)


_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = _fake_get_vault_key
sys.modules["vault_intercept"] = _FAKE_VI

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import _gateway  # noqa: E402

_ENVK = ("GATEWAY_URL", "GATEWAY_TOKEN", "RELAY_URL", "REMOTE_TASK_URL",
         "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENVK}
        for k in _ENVK:
            os.environ.pop(k, None)
        _VAULT_STORE.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _envfile(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        f = os.path.join(d, ".env")
        with open(f, "w") as fh:
            fh.write(body)
        return f


class ChannelEnvLocator(_Base):
    """The REAL _channel_env_file() — the function the skill suite shadows."""

    def test_resolves_under_claude_config_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ch = os.path.join(d, "channels", "ag2space")
        os.makedirs(ch)
        env = os.path.join(ch, ".env")
        with open(env, "w") as fh:
            fh.write("REMOTE_TASK_TOKEN=x\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            got = _gateway._channel_env_file()
        self.assertIsNotNone(got, "real locator returned None for an existing file")
        # Compare the RESOLVED path: a truthiness check passes on any wrong-but-
        # existing path, which is the failure this test exists to catch.
        self.assertEqual(os.path.realpath(str(got)), os.path.realpath(env))

    def test_honors_the_lane_channel_dir(self):
        # The gateway bridge already keys its config on REMOTE_TASK_CHANNEL_DIR;
        # a hardcoded "ag2space" here served PROD's credential to every lane.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        envs = {}
        for name in ("ag2space", "dev-ag2space"):
            ch = os.path.join(d, "channels", name)
            os.makedirs(ch)
            envs[name] = os.path.join(ch, ".env")
            with open(envs[name], "w") as fh:
                fh.write("REMOTE_TASK_TOKEN=%s\n" % name)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d,
                                          "REMOTE_TASK_CHANNEL_DIR": "dev-ag2space"}):
            got = _gateway._channel_env_file()
        self.assertIsNotNone(got, "lane dir exists but the locator returned None")
        self.assertEqual(os.path.realpath(str(got)), os.path.realpath(envs["dev-ag2space"]))
        # Both files exist, so a locator that ignores the lane returns the PROD
        # one rather than None — the assertion above is what discriminates.
        self.assertNotEqual(os.path.realpath(str(got)), os.path.realpath(envs["ag2space"]))

    def test_lane_dir_absent_is_none_not_prod(self):
        # Fail closed: an unconfigured lane must NOT inherit prod's credential.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ch = os.path.join(d, "channels", "ag2space")
        os.makedirs(ch)
        with open(os.path.join(ch, ".env"), "w") as fh:
            fh.write("REMOTE_TASK_TOKEN=prod\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d,
                                          "REMOTE_TASK_CHANNEL_DIR": "dev-ag2space"}):
            self.assertIsNone(_gateway._channel_env_file())

    def test_none_when_absent(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            self.assertIsNone(_gateway._channel_env_file())

    def test_none_when_core_src_missing(self):
        # Call/effect control: without it this passes when the stub is never
        # invoked at all and the None comes from the guard swallowing an error.
        calls = []

        def not_on_path():
            calls.append(1)
            return False

        with mock.patch.object(_gateway, "_core_src_on_path", not_on_path):
            self.assertIsNone(_gateway._channel_env_file())
        self.assertEqual(calls, [1], "stub never ran - None came from elsewhere")

    def test_none_rather_than_raising(self):
        with mock.patch.object(_gateway, "_core_src_on_path",
                               lambda: (_ for _ in ()).throw(RuntimeError("boom"))):
            self.assertIsNone(_gateway._channel_env_file())


class FromChannelEnv(_Base):
    def test_first_non_empty_alias_wins(self):
        f = self._envfile("RELAY_TOKEN=\nREMOTE_TASK_TOKEN=second\n")
        got = _gateway._from_channel_env(
            ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"), env_file=f)
        self.assertEqual(got, "second")

    def test_empty_when_no_alias_present(self):
        f = self._envfile("SOMETHING_ELSE=1\n")
        self.assertEqual(_gateway._from_channel_env(("GATEWAY_TOKEN",), env_file=f), "")

    def test_empty_when_locator_returns_none(self):
        with mock.patch.object(_gateway, "_channel_env_file", lambda: None):
            self.assertEqual(_gateway._from_channel_env(("GATEWAY_TOKEN",)), "")

    def test_empty_when_core_src_missing(self):
        f = self._envfile("REMOTE_TASK_TOKEN=x\n")
        # Control as above: the file HAS the token, so "" must come from the
        # off-path branch actually running, not from a swallowed TypeError.
        calls = []

        def not_on_path():
            calls.append(1)
            return False

        with mock.patch.object(_gateway, "_core_src_on_path", not_on_path):
            self.assertEqual(_gateway._from_channel_env(("REMOTE_TASK_TOKEN",), env_file=f), "")
        self.assertEqual(calls, [1], "stub never ran - '' came from elsewhere")

    def test_resolution_failure_is_absence_not_a_crash(self):
        # The guard around the locator+imports: anything raising there must read
        # as "no credential here", never propagate into the caller's gateway().
        def boom():
            raise RuntimeError("locator exploded")
        with mock.patch.object(_gateway, "_channel_env_file", boom):
            self.assertEqual(_gateway._from_channel_env(("REMOTE_TASK_TOKEN",)), "")

    def test_undecodable_file_is_absence_not_a_crash(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        f = os.path.join(d, ".env")
        with open(f, "wb") as fh:
            fh.write(b"REMOTE_TASK_TOKEN=\xff\xfe\x00binary\n")
        self.assertEqual(_gateway._from_channel_env(("REMOTE_TASK_TOKEN",), env_file=f), "")


class GatewayPrecedence(_Base):
    """env -> channel .env -> vault, end to end through gateway()."""

    def test_file_supplies_token_and_url(self):
        f = self._envfile("REMOTE_TASK_URL=https://gw.example\nREMOTE_TASK_TOKEN=sekret\n")
        with mock.patch.object(_gateway, "_channel_env_file", lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(base, "https://gw.example")
        self.assertEqual(headers.get("Authorization"), "Bearer sekret")

    def test_env_beats_file(self):
        f = self._envfile("REMOTE_TASK_URL=https://file.example\nREMOTE_TASK_TOKEN=from-file\n")
        os.environ["GATEWAY_URL"] = "https://env.example"
        os.environ["GATEWAY_TOKEN"] = "from-env"
        with mock.patch.object(_gateway, "_channel_env_file", lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(base, "https://env.example")
        self.assertEqual(headers.get("Authorization"), "Bearer from-env")

    def test_file_beats_vault(self):
        f = self._envfile("REMOTE_TASK_URL=https://file.example\nREMOTE_TASK_TOKEN=from-file\n")
        _VAULT_STORE["REMOTE_TASK_TOKEN"] = "https://vault.example|from-vault"
        with mock.patch.object(_gateway, "_channel_env_file", lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(headers.get("Authorization"), "Bearer from-file")

    def test_legacy_alias_resolves_from_file(self):
        f = self._envfile("REMOTE_TASK_URL=https://gw.example\nAG2_REMOTE_TOKEN=legacy\n")
        with mock.patch.object(_gateway, "_channel_env_file", lambda: f):
            _, headers = _gateway.gateway()
        self.assertEqual(headers.get("Authorization"), "Bearer legacy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
