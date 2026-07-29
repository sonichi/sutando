#!/usr/bin/env python3
"""Regression coverage for config-aware voice health reporting."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hc)


class VoiceHealthConfigTests(unittest.TestCase):
    def write_env(self, content: str) -> Path:
        temp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        self.addCleanup(Path(temp.name).unlink, missing_ok=True)
        temp.write(content)
        temp.close()
        return Path(temp.name)

    def test_missing_or_empty_config_disables_voice_checks(self) -> None:
        missing = Path(tempfile.gettempdir()) / "sutando-health-missing-dotenv"
        missing.unlink(missing_ok=True)
        for path in (missing, self.write_env("")):
            checks = hc.check_voice_stack(env={}, env_path=path)
            self.assertEqual([c["name"] for c in checks],
                             ["voice-agent", "voice-watchers", "voice-transport", "bodhi-dist"])
            self.assertTrue(all(c["status"] == "ok" for c in checks))
            self.assertTrue(all("disabled" in c["detail"] for c in checks))

        with mock.patch.dict(hc.os.environ, {}, clear=True), \
             mock.patch.object(hc, "_resolve_dotenv", return_value=missing):
            config = hc.resolve_voice_health_config()
        self.assertFalse(config["enabled"])

    def test_either_voice_key_keeps_checks_required(self) -> None:
        for content in (
            "GEMINI_API_KEY=main-key\n",
            "GEMINI_VOICE_API_KEY=voice-key\n",
        ):
            with self.subTest(content=content), \
                 mock.patch.object(hc, "check_port", return_value={
                     "name": "voice-agent", "status": "down", "detail": "port 9900",
                 }):
                checks = hc.check_voice_stack(env={}, env_path=self.write_env(content))
                self.assertEqual(checks[0]["status"], "down")
                self.assertEqual(checks[0]["name"], "voice-agent")

    def test_enabled_voice_preserves_full_probe_path(self) -> None:
        voice_ok = {"name": "voice-agent", "status": "ok", "detail": "listening"}
        watcher_ok = {"name": "voice-watchers", "status": "ok", "detail": "active"}
        transport_ok = {"name": "voice-transport", "status": "ok", "detail": "healthy"}
        bodhi_ok = {"name": "bodhi-dist", "status": "ok", "detail": "current"}
        with mock.patch.object(hc, "check_port", return_value=voice_ok), \
             mock.patch.object(hc, "mark_stale_if_outdated") as mark_stale, \
             mock.patch.object(hc, "check_voice_watchers", return_value=watcher_ok), \
             mock.patch.object(hc, "check_voice_transport", return_value=transport_ok), \
             mock.patch.object(hc, "check_bodhi_dist", return_value=bodhi_ok):
            checks = hc.check_voice_stack(
                env={},
                env_path=self.write_env("export GEMINI_API_KEY='configured key'\n"),
            )
        self.assertEqual(checks, [voice_ok, watcher_ok, transport_ok, bodhi_ok])
        mark_stale.assert_called_once()

    def test_configured_key_wins_over_explicit_skip_like_startup(self) -> None:
        path = self.write_env("SKIP_VOICE=1\nGEMINI_API_KEY=file-key\n")
        with mock.patch.object(hc, "check_port", return_value={
            "name": "voice-agent", "status": "down", "detail": "port 9900",
        }):
            checks = hc.check_voice_stack(env={}, env_path=path)
        self.assertEqual(checks[0]["name"], "voice-agent")
        self.assertEqual(checks[0]["status"], "down")

    def test_process_environment_wins_over_file(self) -> None:
        path = self.write_env("SKIP_VOICE=1\nGEMINI_API_KEY=file-key\n")
        with mock.patch.object(hc, "check_port", return_value={
            "name": "voice-agent", "status": "down", "detail": "port 9900",
        }):
            checks = hc.check_voice_stack(
                env={"SKIP_VOICE": "0", "GEMINI_API_KEY": "ambient-key"},
                env_path=path,
            )
        self.assertEqual(checks[0]["status"], "down")

        key_path = self.write_env("GEMINI_API_KEY=file-key\n")
        checks = hc.check_voice_stack(
            env={"GEMINI_API_KEY": "", "GEMINI_VOICE_API_KEY": ""},
            env_path=key_path,
        )
        self.assertTrue(all(c["status"] == "ok" for c in checks))
        self.assertTrue(all("disabled" in c["detail"] for c in checks))

    def test_unreadable_or_malformed_config_fails_visibly(self) -> None:
        path = self.write_env("GEMINI_API_KEY=file-key\n")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")), \
             mock.patch.object(hc, "check_port", return_value={
                 "name": "voice-agent", "status": "down", "detail": "port 9900",
             }):
            checks = hc.check_voice_stack(env={}, env_path=path)
        self.assertEqual(checks[0]["name"], "voice-config")
        self.assertEqual(checks[0]["status"], "down")
        self.assertIn("unreadable", checks[0]["detail"])

        checks = hc.check_voice_stack(
            env={},
            env_path=self.write_env('GEMINI_API_KEY="unterminated\n'),
        )
        self.assertEqual(checks[0]["name"], "voice-config")
        self.assertEqual(checks[0]["status"], "down")

        for content in (
            "SKIP_VOICE\n",
            "SKIP_VOICE=maybe\n",
            "GEMINI_API_KEY=two words\n",
        ):
            with self.subTest(content=content):
                checks = hc.check_voice_stack(env={}, env_path=self.write_env(content))
                self.assertEqual(checks[0]["name"], "voice-config")
                self.assertEqual(checks[0]["status"], "down")

        checks = hc.check_voice_stack(
            env={"SKIP_VOICE": "maybe"},
            env_path=self.write_env("# ignored\nUNRELATED\nOTHER=value\n"),
        )
        self.assertEqual(checks[0]["name"], "voice-config")
        self.assertEqual(checks[0]["status"], "down")

    def test_run_all_checks_skips_bodhi_probe_when_voice_disabled(self) -> None:
        disabled = {"enabled": False, "detail": "disabled for test"}
        unexpected = {
            "name": "bodhi-dist",
            "status": "warn",
            "detail": "voice artifact missing",
        }
        with mock.patch.object(hc, "resolve_voice_health_config", return_value=disabled), \
             mock.patch.object(hc, "check_bodhi_dist", return_value=unexpected) as bodhi_probe:
            checks = hc.run_all_checks()
        bodhi = next(check for check in checks if check["name"] == "bodhi-dist")
        self.assertEqual(bodhi["status"], "ok")
        self.assertIn("disabled", bodhi["detail"])
        bodhi_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
