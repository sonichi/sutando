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

    def write_managed(self, content: str) -> Path:
        """Write a managed-credentials.json and point WORKSPACE_DIR at its tree."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        auth = root / "state" / "auth"
        auth.mkdir(parents=True)
        (auth / "managed-credentials.json").write_text(content)
        prior = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = root
        self.addCleanup(setattr, hc, "WORKSPACE_DIR", prior)
        return root

    @property
    def _no_dotenv(self) -> Path:
        missing = Path(tempfile.gettempdir()) / "sutando-health-missing-dotenv"
        missing.unlink(missing_ok=True)
        return missing

    # --- the MANAGED tier ----------------------------------------------------
    # #2197 review blocker (john-the-dev 2026-07-30T01:53): startup-runtime.sh
    # boots voice on a managed credential, while this resolver read only
    # SKIP_VOICE / GEMINI_VOICE_API_KEY / GEMINI_API_KEY. A managed-only install
    # therefore ran voice while all four checks reported `ok — disabled` — an
    # outage rendered as a green light, which is worse than having no check.
    def test_managed_voice_slot_enables_checks(self) -> None:
        self.write_managed('{"capabilities": {"gemini-voice": {"key": "k"}}}')
        cfg = hc.resolve_voice_health_config(env={}, env_path=self._no_dotenv)
        self.assertTrue(cfg["enabled"])
        self.assertIn("managed", cfg["detail"])

    def test_managed_text_slot_is_the_documented_fallback(self) -> None:
        # Mirrors CAPABILITY_FALLBACKS['gemini-voice'] = ['gemini-voice','gemini-text'],
        # the same order startup-runtime.sh accepts. Diverging here would
        # re-open the disagreement in the other direction.
        self.write_managed('{"capabilities": {"gemini-text": {"key": "k"}}}')
        self.assertTrue(hc.resolve_voice_health_config(
            env={}, env_path=self._no_dotenv)["enabled"])

    def test_no_managed_file_stays_disabled(self) -> None:
        root = self.write_managed("{}")
        (root / "state" / "auth" / "managed-credentials.json").unlink()
        self.assertFalse(hc.resolve_voice_health_config(
            env={}, env_path=self._no_dotenv)["enabled"])

    def test_empty_managed_key_is_not_a_credential(self) -> None:
        self.write_managed('{"capabilities": {"gemini-voice": {"key": ""}}}')
        self.assertFalse(hc.resolve_voice_health_config(
            env={}, env_path=self._no_dotenv)["enabled"])

    def test_malformed_managed_file_skips_the_tier_matching_the_launcher(self) -> None:
        # Deliberately NOT fail-closed, unlike the dotenv parsing: a malformed
        # managed file means the managed tier is unusable, so startup will not
        # boot voice either — reporting "enabled" would invent an outage that
        # cannot exist. Match the launcher; the bug was the two disagreeing.
        self.write_managed("{not json")
        self.assertFalse(hc.resolve_voice_health_config(
            env={}, env_path=self._no_dotenv)["enabled"])

    def test_non_object_capabilities_skips_the_tier(self) -> None:
        # `capabilities` present but not an object. Mirrors the launcher, which
        # raises ValueError("capabilities is not an object") and skips the tier.
        # Verified reachable before writing this: `.get("capabilities") or {}`
        # coerces FALSY values to {}, so `null` never reaches the isinstance
        # branch — only a truthy non-dict like 42 or [1,2] does. The coverage gate
        # flagged this line as untested on my first push, and it was right: none
        # of the other five cases can reach it.
        for bad in ('{"capabilities": 42}', '{"capabilities": [1, 2]}'):
            with self.subTest(bad=bad):
                self.write_managed(bad)
                self.assertFalse(hc.resolve_voice_health_config(
                    env={}, env_path=self._no_dotenv)["enabled"])

    # --- S1 truth table: voicePreference / quarantined (design 2b, WS2 Step 4)
    # health-check implements the SHARED credential-source table (amendment S1)
    # alongside the launcher, the TS/python resolvers, and the desktop
    # supervisor's spawn-env gate; tests/voice-preference-consumers.test.sh
    # drives them all over one fixture matrix — these cases pin this module's
    # own decisions + detail strings.

    _BOTH_SLOTS = ('"capabilities": {"gemini-voice": {"key": "managed-v"},'
                   ' "gemini-text": {"key": "managed-t"}}')

    def test_byok_preference_hides_managed_entries_from_the_gate(self) -> None:
        root = self.write_managed('{%s, "voicePreference": "byok"}' % self._BOTH_SLOTS)
        path = root / "state" / "auth" / "managed-credentials.json"
        self.assertFalse(hc.managed_voice_credential_present(path))

    def test_byok_preference_without_env_key_reads_disabled_with_a_reason(self) -> None:
        # Impl plan WS2 Step 4: a byok-with-no-env-key install must read as
        # *disabled with a reason*, not "managed credential configured".
        self.write_managed('{%s, "voicePreference": "byok"}' % self._BOTH_SLOTS)
        cfg = hc.resolve_voice_health_config(env={}, env_path=self._no_dotenv)
        self.assertFalse(cfg["enabled"])
        self.assertIn("BYOK preference set (managed entries ignored)", cfg["detail"])

    def test_byok_preference_with_env_key_is_enabled_via_env(self) -> None:
        self.write_managed('{%s, "voicePreference": "byok"}' % self._BOTH_SLOTS)
        cfg = hc.resolve_voice_health_config(
            env={"GEMINI_API_KEY": "byo-mk"}, env_path=self._no_dotenv)
        self.assertTrue(cfg["enabled"])
        self.assertIn("BYOK preference", cfg["detail"])
        self.assertNotIn("managed voice credential", cfg["detail"])

    def test_quarantined_entries_are_absent_in_every_mode(self) -> None:
        root = self.write_managed('{%s, "quarantined": true}' % self._BOTH_SLOTS)
        path = root / "state" / "auth" / "managed-credentials.json"
        self.assertFalse(hc.managed_voice_credential_present(path))
        # Unset preference: the legacy walk falls through to env...
        cfg = hc.resolve_voice_health_config(
            env={"GEMINI_API_KEY": "byo-mk"}, env_path=self._no_dotenv)
        self.assertTrue(cfg["enabled"])
        # ...and with no env key the quarantined entries must not enable.
        cfg = hc.resolve_voice_health_config(env={}, env_path=self._no_dotenv)
        self.assertFalse(cfg["enabled"])

    def test_quarantined_false_keeps_the_tier(self) -> None:
        root = self.write_managed('{%s, "quarantined": false}' % self._BOTH_SLOTS)
        path = root / "state" / "auth" / "managed-credentials.json"
        self.assertTrue(hc.managed_voice_credential_present(path))

    def test_managed_preference_with_usable_entry_is_enabled(self) -> None:
        self.write_managed('{%s, "voicePreference": "managed"}' % self._BOTH_SLOTS)
        cfg = hc.resolve_voice_health_config(env={}, env_path=self._no_dotenv)
        self.assertTrue(cfg["enabled"])
        self.assertIn("managed", cfg["detail"])

    def test_s1_env_key_never_satisfies_a_managed_preference(self) -> None:
        """S1's load-bearing row: managed preference + quarantined/missing managed
        entries + a present env key -> DISABLED.

        An env key silently satisfying a managed preference is the
        logout-quarantine bypass the design closes (design 2b).
        """
        for doc in (
            '{%s, "voicePreference": "managed", "quarantined": true}' % self._BOTH_SLOTS,
            '{"capabilities": {}, "voicePreference": "managed"}',
        ):
            with self.subTest(doc=doc):
                self.write_managed(doc)
                cfg = hc.resolve_voice_health_config(
                    env={"GEMINI_VOICE_API_KEY": "vk", "GEMINI_API_KEY": "mk"},
                    env_path=self._no_dotenv)
                self.assertFalse(cfg["enabled"])
                self.assertIn("voicePreference=managed", cfg["detail"])

    def test_r15_revision_fields_are_tolerated_and_ignored(self) -> None:
        self.write_managed(
            '{%s, "preferenceRevision": 7, "sessionRevision": 3}' % self._BOTH_SLOTS)
        cfg = hc.resolve_voice_health_config(env={}, env_path=self._no_dotenv)
        self.assertTrue(cfg["enabled"])
        self.assertIn("managed", cfg["detail"])

    def test_managed_voice_preference_vocabulary(self) -> None:
        rows = (
            ('{"capabilities": {}, "voicePreference": "managed"}', "managed"),
            ('{"capabilities": {}, "voicePreference": "byok"}', "byok"),
            ('{"capabilities": {}}', "unset"),
            ('{"capabilities": {}, "voicePreference": "MANAGED"}', "unset"),
            ('{"capabilities": {}, "voicePreference": 42}', "unset"),
            ("{not json", "unset"),
        )
        for doc, expected in rows:
            with self.subTest(doc=doc):
                root = self.write_managed(doc)
                path = root / "state" / "auth" / "managed-credentials.json"
                self.assertEqual(hc.managed_voice_preference(path), expected)
        missing = Path(tempfile.gettempdir()) / "no-such-managed-credentials.json"
        missing.unlink(missing_ok=True)
        self.assertEqual(hc.managed_voice_preference(missing), "unset")

    def test_managed_credential_wins_over_inherited_skip_voice(self) -> None:
        """Managed key + inherited SKIP_VOICE=1 must report ENABLED, like the launcher.

        Replaces test_skip_voice_still_wins_over_a_managed_credential, which pinned
        the OPPOSITE precedence from the thing that actually boots voice.
        `configure_startup_runtime()` UNSETS an inherited SKIP_VOICE when a managed
        credential is present (startup-runtime.sh:57-58), so asserting "disabled"
        here made the test suite certify the exact disagreement it should have
        caught: voice running, health reporting disabled.
        (#2197 review blocker, john-the-dev 2026-07-31T05:37.)
        """
        self.write_managed('{"capabilities": {"gemini-voice": {"key": "k"}}}')
        cfg = hc.resolve_voice_health_config(
            env={"SKIP_VOICE": "1"}, env_path=self._no_dotenv)
        self.assertTrue(cfg["enabled"])
        self.assertIn("managed", cfg["detail"])

    def test_skip_voice_still_disables_without_any_credential(self) -> None:
        """The control: SKIP_VOICE=1 must STILL disable when no tier supplies a key.

        Without this, the change above could be satisfied by ignoring SKIP_VOICE
        entirely, and the discriminator would have stopped being able to say NO.
        """
        self.write_managed('{"capabilities": {}}')
        cfg = hc.resolve_voice_health_config(
            env={"SKIP_VOICE": "1"}, env_path=self._no_dotenv)
        self.assertFalse(cfg["enabled"])
        self.assertIn("SKIP_VOICE=1", cfg["detail"])

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

    def test_managed_only_install_runs_the_REAL_probe_path(self) -> None:
        """A managed-only install must actually PROBE, not just resolve enabled.

        This is the specific regression asked for in the #2197 review
        ("add a managed-only regression proving the real probe path executes"),
        and the existing cases did not cover it. They prove the two halves
        SEPARATELY: the managed cases assert `resolve_voice_health_config()`
        returns enabled, and `test_enabled_voice_preserves_full_probe_path`
        asserts the four probes run — but that one enables voice with a dotenv
        `GEMINI_API_KEY`, never a managed credential.

        The seam between them is where the original bug lived: if
        `check_voice_stack` ever consults a different predicate than the
        resolver, a managed-only install short-circuits to four
        "ok - disabled" results — a real outage reported as healthy — and every
        pre-existing test still passes. So this asserts the composition, with
        NO voice env key and NO dotenv anywhere: managed credential alone must
        reach `check_port` and friends.
        """
        self.write_managed('{"capabilities": {"gemini-voice": {"key": "k"}}}')
        voice_ok = {"name": "voice-agent", "status": "down", "detail": "port 9900"}
        watcher_ok = {"name": "voice-watchers", "status": "ok", "detail": "active"}
        transport_ok = {"name": "voice-transport", "status": "ok", "detail": "healthy"}
        bodhi_ok = {"name": "bodhi-dist", "status": "ok", "detail": "current"}
        with mock.patch.dict(hc.os.environ, {}, clear=True), \
             mock.patch.object(hc, "check_port", return_value=voice_ok) as probe, \
             mock.patch.object(hc, "mark_stale_if_outdated"), \
             mock.patch.object(hc, "check_voice_watchers", return_value=watcher_ok), \
             mock.patch.object(hc, "check_voice_transport", return_value=transport_ok), \
             mock.patch.object(hc, "check_bodhi_dist", return_value=bodhi_ok):
            checks = hc.check_voice_stack(env={}, env_path=self._no_dotenv)

        # The probe ran at all — the assertion the "disabled" bug would break.
        probe.assert_called_once()
        # And a DOWN result survives to the caller rather than being masked as
        # "ok - disabled". Deliberately using status=down: an all-ok expectation
        # would also be satisfied by the disabled path, which returns ok.
        self.assertEqual(checks, [voice_ok, watcher_ok, transport_ok, bodhi_ok])
        self.assertEqual(checks[0]["status"], "down")
        for c in checks:
            self.assertNotIn("disabled", c["detail"])

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
