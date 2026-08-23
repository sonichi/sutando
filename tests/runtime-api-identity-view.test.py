#!/usr/bin/env python3
"""Tests for the runtime-API identity surface (identity_view.py + dispatch).

Contract: sutando.info/status/owner/allowlist report ONLY what the workspace
records say — daemon actor id, core-status.json, own heartbeat, and channel
access.json files. Ownership is never inferred from an allowlist entry.

Run: python3 tests/runtime-api-identity-view.test.py
Exit: 0 on pass, 1 on fail.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from identity_view import IdentityView  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402
from protocol import ProtocolError  # noqa: E402


def _mk(state: Path, channels: Path | None = None, **kw) -> IdentityView:
    return IdentityView(state, "@me:example.org", channels_dir=channels,
                        host_label=kw.get("host_label"))


class IdentityViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.channels = Path(self.tmp.name) / "channels"

    def tearDown(self):
        self.tmp.cleanup()

    def _channel(self, name: str, payload: dict):
        d = self.channels / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "access.json").write_text(json.dumps(payload))

    def test_info_is_daemon_actor_plus_own_beat(self):
        cores = self.state / "cores"
        cores.mkdir()
        (cores / "my-host.alive").write_text(json.dumps(
            {"host": "my-host", "pid": 7, "socket": "/tmp/t.sock"}))
        v = _mk(self.state, host_label="my-host")
        info = v.info()
        self.assertEqual(info["agentId"], "@me:example.org")
        self.assertEqual(info["hostLabel"], "my-host")
        # runtime internals relocated to runtime.details (owner taxonomy)
        self.assertNotIn("pid", info)
        self.assertNotIn("socket", info)

    def test_status_reads_core_status_and_liveness(self):
        (self.state / "core-status.json").write_text(json.dumps(
            {"status": "running", "step": "doing a thing", "ts": 123}))
        cores = self.state / "cores"
        cores.mkdir()
        (cores / "h.alive").write_text("{}")
        v = _mk(self.state, host_label="h")
        st = v.status()
        self.assertEqual(st["status"], "running")
        self.assertEqual(st["step"], "doing a thing")
        self.assertTrue(st["alive"])

    def test_status_missing_file_is_unknown_not_crash(self):
        self.assertEqual(_mk(self.state).status()["status"], "unknown")

    def test_stale_own_beat_reports_not_alive(self):
        cores = self.state / "cores"
        cores.mkdir()
        f = cores / "h.alive"
        f.write_text("{}")
        old = time.time() - 300
        os.utime(f, (old, old))
        st = _mk(self.state, host_label="h").status()
        self.assertFalse(st["alive"])

    def test_owner_uses_explicit_fields_never_allowfrom(self):
        # telegram: explicit tofuOwner. slack: tierMap owner. discord: ONLY an
        # allowFrom list — no ownership metadata → must NOT appear as owner.
        self._channel("telegram", {"allowFrom": ["111"], "tofuOwner": "111"})
        self._channel("slack", {"allowFrom": ["U1", "U2"],
                                "tierMap": {"U1": "owner", "U2": "team"}})
        self._channel("discord", {"allowFrom": ["999"]})
        owners = _mk(self.state, self.channels).owner()["owners"]
        self.assertEqual(owners["telegram"]["tofuOwner"], "111")
        self.assertEqual(owners["slack"]["tierOwners"], ["U1"])
        self.assertNotIn("discord", owners)

    def test_allowlist_is_verbatim_per_channel(self):
        self._channel("ag2space", {"allowFrom": ["@a:hs", "@b:hs"]})
        self._channel("discord", {"allowFrom": ["1", "2"]})
        ch = _mk(self.state, self.channels).allowlist()["channels"]
        self.assertEqual(ch["ag2space"], ["@a:hs", "@b:hs"])
        self.assertEqual(ch["discord"], ["1", "2"])

    def test_unreadable_channel_is_skipped_not_fatal(self):
        self._channel("good", {"allowFrom": ["x"]})
        bad = self.channels / "bad"
        bad.mkdir(parents=True)
        (bad / "access.json").write_text("{nope")
        ch = _mk(self.state, self.channels).allowlist()["channels"]
        self.assertEqual(list(ch), ["good"])

    def test_no_channels_dir_yields_empty_surfaces(self):
        v = _mk(self.state, None)
        self.assertEqual(v.owner()["owners"], {})
        self.assertEqual(v.allowlist()["channels"], {})


class DispatchTests(unittest.TestCase):
    class _No:
        def __getattr__(self, name):
            raise AssertionError(f"sutando.* reached {name}")

    def test_all_four_methods_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "core-status.json").write_text('{"status":"idle","ts":1}')
            d = RuntimeDispatcher(self._No(), self._No(), "@me:x",
                                  executors={},
                                  identity_view=IdentityView(state, "@me:x"))
            for method in ("sutando.info", "sutando.status",
                           "sutando.owner", "sutando.allowlist"):
                out = asyncio.run(d.handle(method, {}))
                self.assertIsInstance(out, dict, method)
            self.assertEqual(
                asyncio.run(d.handle("sutando.info", {}))["agentId"], "@me:x")

    def test_unconfigured_identity_fails_loudly(self):
        d = RuntimeDispatcher(self._No(), self._No(), "@me:x",
                              executors={}, identity_view=None)
        with self.assertRaises(ProtocolError):
            asyncio.run(d.handle("sutando.info", {}))


class TestStand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "auth").mkdir(parents=True)
        self.channels = Path(self.tmp.name) / "channels"

    def tearDown(self):
        self.tmp.cleanup()

    def _enroll(self, agent_id="@stand:ag2.space"):
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": agent_id, "schema_version": "1"}))

    def _native_channel(self, payload):
        d = self.channels / "ag2space"
        d.mkdir(parents=True, exist_ok=True)
        (d / "access.json").write_text(json.dumps(payload))

    def test_full_record(self):
        self._enroll()
        self._native_channel({"tofuOwner": "@owner:ag2.space",
                              "tierMap": {"@x:ag2.space": "team"}})
        (self.state / "auth" / "stand.json").write_text(json.dumps(
            {"display_name": "Sutando",
             "owners": [{"person_id": "@owner:ag2.space",
                         "display_name": "Owner Person",
                         "role": "primary_owner"}]}))
        out = _mk(self.state, self.channels, host_label="host-1").stand_card()
        self.assertEqual(out["stand"],
                         {"stand_id": "@stand:ag2.space",
                          "display_name": "Sutando"})
        self.assertEqual(out["owners"],
                         [{"person_id": "@owner:ag2.space",
                           "display_name": "Owner Person",
                           "role": "primary_owner",
                           "verification": "explicit_owner_binding"}])
        self.assertEqual(out["owner_evidence"],
                         [{"provider": "ag2space",
                           "subject": "@owner:ag2.space"}])
        self.assertIn("channels", out)
        self.assertEqual(out["devices"], [])
        self.assertEqual(out["instances"], [])

    def test_absent_records_are_omitted_not_null(self):
        # no enrolled record, no channels: empty sections, no invented values
        out = _mk(self.state).stand_card()
        self.assertEqual(out["stand"], {})
        self.assertEqual(out["owners"], [])
        self.assertEqual(out["owner_evidence"], [])
        self.assertNotIn(None, out["stand"].values())

    def test_owner_never_promoted_from_channel_evidence(self):
        self._enroll()
        # tofuOwner is evidence — owners[] stays empty without stand.json,
        # and the evidence lands in owner_evidence, never in owners
        self._native_channel({"tofuOwner": "@o:ag2.space",
                              "tierMap": {"@o:ag2.space": "owner"}})
        d = self.channels / "slack"
        d.mkdir(parents=True)
        (d / "access.json").write_text(json.dumps({"tofuOwner": "U1"}))
        out = _mk(self.state, self.channels).stand_card()
        self.assertEqual(out["owners"], [])
        self.assertEqual(len(out["owner_evidence"]), 2)

    def test_dispatch_routes_stand(self):
        self._enroll()
        d = RuntimeDispatcher(DispatchTests._No(),
                              DispatchTests._No(), "@me:x",
                              executors={},
                              identity_view=IdentityView(self.state, "@me:x"))
        out = asyncio.run(d.handle("sutando.stand", {}))
        self.assertEqual(out["stand"]["stand_id"], "@stand:ag2.space")


class TestEntrances(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "auth").mkdir(parents=True)
        self.channels = Path(self.tmp.name) / "channels"

    def tearDown(self):
        self.tmp.cleanup()

    def _mkchan(self, name, env=None, access=None, extra=None):
        d = self.channels / name
        d.mkdir(parents=True, exist_ok=True)
        if env is not None:
            (d / ".env").write_text(env)
        if access is not None:
            (d / "access.json").write_text(access)
        if extra:
            for fn, body in extra.items():
                (d / fn).write_text(body)
        return d

    def test_statuses_are_honest_and_evidence_scoped(self):
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": "@stand:ag2.space"}))
        self._mkchan("ag2space", env="TOKEN=sekret-ag2",
                     access=json.dumps({"tofuOwner": "@owner:ag2.space"}))
        self._mkchan("discord", env="DISCORD_TOKEN=sekret-dc",
                     access=json.dumps({"tierMap": {"1": "owner"}}))
        self._mkchan("slack", env="SLACK_TOKEN=sekret-sl")
        self._mkchan("telegram")  # empty folder
        self._mkchan("broken", access="{not json")
        out = _mk(self.state, self.channels).entrances()
        by = {e["provider"]: e for e in out["channels"]}
        self.assertEqual(by["ag2space"]["status"], "configured_unverified")
        self.assertEqual(by["ag2space"]["evidence"]["subject_evidence"],
                         "@stand:ag2.space")
        self.assertEqual(by["ag2space"]["evidence"]["owner_id"],
                         "@owner:ag2.space")
        self.assertEqual(by["discord"]["status"], "configured_unverified")
        # tierMap is not owner evidence at the entrance level either
        self.assertNotIn("owner_id", by["discord"]["evidence"])
        self.assertEqual(by["slack"]["status"], "configured_unverified")
        self.assertEqual(by["telegram"]["status"], "not_configured")
        self.assertEqual(by["broken"]["status"], "policy_invalid")
        # nothing may ever claim "active" without provider verification (I2)
        self.assertNotIn("active", {e["status"] for e in out["channels"]})

    def test_env_contents_never_leak(self):
        self._mkchan("discord", env="DISCORD_TOKEN=sekret-dc-9911",
                     access=json.dumps({"tofuOwner": "u1"}))
        dumped = json.dumps(_mk(self.state, self.channels).entrances())
        self.assertNotIn("sekret", dumped)
        self.assertNotIn("9911", dumped)

    def test_backup_policy_files_ignored(self):
        self._mkchan("discord",
                     access=json.dumps({"tofuOwner": "current"}),
                     extra={"access.json.bak.1": json.dumps(
                         {"tofuOwner": "stale-backup"})})
        out = _mk(self.state, self.channels).entrances()
        dumped = json.dumps(out)
        self.assertNotIn("stale-backup", dumped)
        self.assertIn("current", dumped)

    def test_dispatch_routes_entrances(self):
        d = RuntimeDispatcher(DispatchTests._No(), DispatchTests._No(),
                              "@me:x", executors={},
                              identity_view=IdentityView(self.state, "@me:x",
                                                         channels_dir=self.channels))
        for method in ("sutando.channels", "sutando.entrances"):
            out = asyncio.run(d.handle(method, {}))
            self.assertIn("channels", out)


class TestEntranceLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "auth").mkdir(parents=True)
        self.channels = Path(self.tmp.name) / "channels"
        d = self.channels / "discord"
        d.mkdir(parents=True)
        (d / ".env").write_text("DISCORD_TOKEN=tok-sekret")
        (d / "access.json").write_text("{}")
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "entrance_links",
            Path(__file__).resolve().parent.parent / "src" / "entrance_links.py")
        self.el = ilu.module_from_spec(spec)
        spec.loader.exec_module(self.el)

    def tearDown(self):
        self.tmp.cleanup()

    def _link(self, subject_id="123"):
        return self.el.upsert_link(
            self.state, "discord", {"type": "bot_user", "id": subject_id},
            {"method": "discord_token_introspection", "verified_at": "t"},
            "sha256:abcd")

    def test_verified_link_without_authorization_is_unlinked(self):
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": "@stand:ag2.space"}))
        link = self._link()
        self.assertEqual(link["stand_id"], "@stand:ag2.space")
        out = _mk(self.state, self.channels).entrances()
        e = out["channels"][0]
        # introspection alone proves credential->subject, NOT authorization
        self.assertEqual(e["status"], "verified_unlinked")
        self.assertEqual(e["stand_binding"], "absent")
        self.assertEqual(e["identity"], {"type": "bot_user", "id": "123"})
        self.assertEqual(e["verification"]["method"],
                         "discord_token_introspection")

    def test_owner_authorization_activates_the_binding(self):
        self._link()
        self.el.authorize_link(self.state, "discord", "@owner:x")
        out = _mk(self.state, self.channels).entrances()
        e = out["channels"][0]
        self.assertEqual(e["status"], "active")
        self.assertEqual(e["stand_binding"], "authorized")
        self.assertEqual(e["authorized_by"], "@owner:x")

    def test_authorize_without_link_is_loud(self):
        with self.assertRaises(ValueError):
            self.el.authorize_link(self.state, "discord", "@owner:x")

    def test_no_link_stays_unverified(self):
        out = _mk(self.state, self.channels).entrances()
        self.assertEqual(out["channels"][0]["status"],
                         "configured_unverified")

    def test_unique_subject_conflict_is_loud(self):
        self._link("123")
        with self.assertRaises(ValueError):
            self._link("456")
        # same subject re-verifies in place (no duplicate rows)
        self._link("123")
        self.assertEqual(len(self.el.load_links(self.state)), 1)

    def test_no_credential_material_in_records_or_view(self):
        self._link()
        blob = self.el.links_path(self.state).read_text()
        dumped = json.dumps(_mk(self.state, self.channels).entrances())
        for hay in (blob, dumped):
            self.assertNotIn("tok-sekret", hay)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "auth").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_links(self, links):
        (self.state / "auth" / "entrance-links.json").write_text(
            json.dumps(links))

    def test_forward_hit_and_subject_prefix_forms(self):
        self._write_links([{
            "link_id": "link_x", "provider": "discord", "status": "active",
            "stand_id": "@stand:ag2.space",
            "provider_subject": {"type": "bot_user", "id": "123"},
            "display": {"name": "sutando-bot"},
            "verification": {"method": "discord_token_introspection",
                             "verified_at": "t"}}])
        v = _mk(self.state)
        for subject in ("123", "bot:123", "bot_user:123"):
            out = v.resolve("discord", subject)
            self.assertTrue(out["resolved"], subject)
            self.assertEqual(out["stand_id"], "@stand:ag2.space")
        self.assertEqual(v.resolve("discord", "123")["link"]["display"],
                         {"name": "sutando-bot"})

    def test_no_match_and_revoked_excluded(self):
        self._write_links([{
            "link_id": "l", "provider": "discord", "status": "revoked",
            "stand_id": "@s:x",
            "provider_subject": {"type": "bot_user", "id": "123"}}])
        out = _mk(self.state).resolve("discord", "123")
        self.assertFalse(out["resolved"])
        self.assertNotIn("conflict", out)

    def test_multi_stand_conflict_is_loud_never_autopicked(self):
        self._write_links([
            {"link_id": "a", "provider": "discord", "status": "active",
             "stand_id": "@s1:x",
             "provider_subject": {"type": "bot_user", "id": "123"}},
            {"link_id": "b", "provider": "discord", "status": "active",
             "stand_id": "@s2:x",
             "provider_subject": {"type": "bot_user", "id": "123"}}])
        out = _mk(self.state).resolve("discord", "123")
        self.assertFalse(out["resolved"])
        self.assertTrue(out["conflict"])
        self.assertEqual(len(out["candidates"]), 2)


class TestEnrolledActorFallback(unittest.TestCase):
    def test_enrolled_identity_beats_local_agent_fallback(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                              / "src" / "runtime-api"))
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "rt_server", Path(__file__).resolve().parent.parent
            / "src" / "runtime-api" / "server.py")
        srv = ilu.module_from_spec(spec)
        spec.loader.exec_module(srv)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "auth").mkdir()
            (Path(td) / "auth" / "ag2space.json").write_text(
                '{"agent_id": "@enrolled:ag2.space"}')
            self.assertEqual(srv._enrolled_agent_id(td), "@enrolled:ag2.space")
            self.assertIsNone(srv._enrolled_agent_id(None))
        with tempfile.TemporaryDirectory() as td2:
            self.assertIsNone(srv._enrolled_agent_id(td2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
