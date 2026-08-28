#!/usr/bin/env python3
"""Tests for the runtime-API identity surface (identity_view.py + dispatch).

Contract: sutando.info/status/owner/allowlist report ONLY what the workspace
records say — daemon actor id, core-status.json, own heartbeat, and channel
access.json files. Ownership is never inferred from an allowlist entry.

Run: python3 tests/runtime-api-identity-view.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

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

# flake8: noqa: E402 — imports follow the sys.path bootstrap above
from identity_view import IdentityView
from dispatcher import RuntimeDispatcher
from protocol import ProtocolError


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
        self.assertNotIn("instances", out)

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

    def test_active_link_renders_display_credential_storage(self):
        self._mkchan("discord", env="DISCORD_TOKEN=x",
                     access=json.dumps({"tofuOwner": "u1"}))
        (self.channels / "README.md").write_text("not a channel dir")
        (self.state / "auth" / "entrance-links.json").write_text(json.dumps([
            {"provider": "discord", "status": "active",
             "authorized_by": "@own:x",
             "provider_subject": {"type": "bot_user", "id": "42"},
             "display": {"name": "SutandoBot"},
             "verification": {"method": "discord_token_introspection"},
             "credential": {"fingerprint": "sha256:ab"}}]))
        out = _mk(self.state, self.channels).entrances(details=True)
        self.assertEqual(len(out["channels"]), 1)  # stray file skipped
        ent = out["channels"][0]
        self.assertEqual(ent["status"], "active")
        self.assertEqual(ent["display"], {"name": "SutandoBot"})
        self.assertEqual(ent["credential"], {"fingerprint": "sha256:ab"})
        self.assertEqual(ent["storage"]["type"], "channel_directory")

    def test_devices_render_from_pairing_records(self):
        ddir = self.state / "auth" / "devices"
        ddir.mkdir(parents=True)
        (ddir / "d1.json").write_text(json.dumps(
            {"device_id": "d1", "label": "phone", "device_type": "mobile",
             "token_sha256": "aa", "granted_methods": ["task.submit"],
             "last_seen_at": "2026-08-23T00:00:00Z"}))
        (ddir / "d2.json").write_text(json.dumps({"device_id": "d2"}))
        (ddir / "bad.json").write_text("{nope")
        devs = _mk(self.state).stand_card(details=True)["devices"]
        by = {d["device_id"]: d for d in devs}
        self.assertEqual(set(by), {"d1", "d2"})  # corrupt record skipped
        self.assertEqual(by["d1"]["status"], "enrolled")
        self.assertEqual(by["d1"]["granted_methods"], ["task.submit"])
        self.assertEqual(by["d1"]["last_seen_at"], "2026-08-23T00:00:00Z")
        self.assertEqual(by["d2"]["status"], "configured_unverified")
        self.assertNotIn("last_seen_at", by["d2"])

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
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": "@stand:ag2.space"}))
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

    def test_verify_discord_lives_at_the_edge_and_shapes_display(self):
        calls = []

        class _Client:
            def get_json(self, path):
                calls.append(path)
                return {"id": 987, "global_name": "Sutando", "avatar": "abcd"}

        sys.path.insert(0, str(ROOT / "src"))
        try:
            import importlib
            ev = importlib.import_module("channels.discord.entrance_verify")
            link = ev.verify_discord(self.state, "tok-sekret",
                                     client=_Client())
            # default client is the census chokepoint, not hand-rolled REST
            self.assertEqual(ev.DiscordRestClient.__module__,
                             "channels.discord.client")
        finally:
            sys.path.remove(str(ROOT / "src"))
        self.assertEqual(calls, ["/users/@me"])
        self.assertEqual(link["provider_subject"],
                         {"type": "bot_user", "id": "987"})
        self.assertEqual(link["display"],
                         {"name": "Sutando", "avatar_url":
                          "https://cdn.discordapp.com/avatars/987/abcd.png"})
        self.assertEqual(link["verification"]["method"],
                         "discord_token_introspection")
        self.assertNotIn("tok-sekret", json.dumps(link))

    def test_load_links_absent_store_is_empty(self):
        self.assertEqual(self.el.load_links(self.state / "nope"), [])

    def test_active_link_lookup(self):
        self.assertIsNone(self.el.active_link(self.state, "discord"))
        made = self._link()
        got = self.el.active_link(self.state, "discord")
        self.assertEqual(got["link_id"], made["link_id"])

    def test_revoke_without_active_link_is_loud(self):
        with self.assertRaises(ValueError):
            self.el.revoke_link(self.state, "discord", "@o:x", "cleanup")


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
            "stand_id": "@stand:ag2.space", "authorized_by": "@o:x",
            "provider_subject": {"type": "bot_user", "id": "123"},
            "display": {"name": "sutando-bot"},
            "verification": {"method": "discord_token_introspection",
                             "verified_at": "t"}},
            # same-provider distractor: subject mismatch must be skipped
            {"link_id": "link_y", "provider": "discord", "status": "active",
             "stand_id": "@other:ag2.space", "authorized_by": "@o:x",
             "provider_subject": {"type": "bot_user", "id": "999"}}])
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
             "stand_id": "@s1:x", "authorized_by": "@o:x",
             "provider_subject": {"type": "bot_user", "id": "123"}},
            {"link_id": "b", "provider": "discord", "status": "active",
             "stand_id": "@s2:x", "authorized_by": "@o:x",
             "provider_subject": {"type": "bot_user", "id": "123"}}])
        out = _mk(self.state).resolve("discord", "123")
        self.assertFalse(out["resolved"])
        self.assertTrue(out["conflict"])
        self.assertEqual(len(out["candidates"]), 2)


class TestAuthorityBoundaries(unittest.TestCase):
    """Owner review 2026-08-23: identity mutations fail closed; revocation
    is layered; the full Discord lifecycle holds end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "auth").mkdir(parents=True)
        self.channels = Path(self.tmp.name) / "channels"
        d = self.channels / "discord"
        d.mkdir(parents=True)
        (d / ".env").write_text("DISCORD_TOKEN=tok")
        (d / "access.json").write_text("{}")
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "el_ab", Path(__file__).resolve().parent.parent / "src" / "entrance_links.py")
        self.el = ilu.module_from_spec(spec)
        spec.loader.exec_module(self.el)

    def tearDown(self):
        self.tmp.cleanup()

    def _enroll(self):
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": "@stand:ag2.space"}))

    def _verify(self):
        return self.el.upsert_link(
            self.state, "discord", {"type": "bot_user", "id": "123"},
            {"method": "discord_token_introspection", "verified_at": "t"},
            "sha256:abcd")

    def test_unresolved_identity_mutations_all_fail_closed(self):
        # no enrolled record: reads work, every mutation refuses
        v = _mk(self.state, self.channels)
        self.assertIn("stand", v.stand_card())  # read path fine
        for fn in (lambda: self._verify(),
                   lambda: self.el.authorize_link(self.state, "discord", "@o:x"),
                   lambda: self.el.revoke_link(self.state, "discord", "@o:x")):
            with self.assertRaises(PermissionError):
                fn()

    def test_resolve_requires_authorization_not_just_verification(self):
        # the verifier's exact control: provider-verified but owner-unlinked
        # must NOT resolve as an authorized Stand binding
        self._enroll()
        self._verify()
        v = _mk(self.state, self.channels)
        r = v.resolve("discord", "123")
        self.assertFalse(r["resolved"])
        self.assertTrue(r.get("verified_unlinked"))
        self.el.authorize_link(self.state, "discord", "@o:x")
        r2 = v.resolve("discord", "123")
        self.assertTrue(r2["resolved"])

    def test_reverify_never_transplants_another_stands_authorization(self):
        # kewei's control: Stand B's authorized row must not become Stand A's
        self._enroll()
        self._verify()
        self.el.authorize_link(self.state, "discord", "@owner-b:x")
        links = self.el.load_links(self.state)
        links[0]["stand_id"] = "@stand-b:x"
        (self.state / "auth" / "entrance-links.json").write_text(
            json.dumps(links))
        with self.assertRaises(ValueError):
            self._verify()  # enrolled Stand differs from the row's
        row = self.el.load_links(self.state)[0]
        self.assertEqual(row["stand_id"], "@stand-b:x")
        self.assertEqual(row["authorized_by"], "@owner-b:x")
        r = _mk(self.state, self.channels).resolve("discord", "123")
        self.assertNotEqual(r.get("stand_id"), self.el._enrolled_stand_id(
            self.state))

    def test_cross_stand_revocation_refused(self):
        self._enroll()
        self._verify()
        links = self.el.load_links(self.state)
        links[0]["stand_id"] = "@stand-b:x"
        (self.state / "auth" / "entrance-links.json").write_text(
            json.dumps(links))
        with self.assertRaises(ValueError):
            self.el.revoke_link(self.state, "discord", "@o:x")
        self.assertEqual(self.el.load_links(self.state)[0]["status"], "active")

    def test_reenrollment_during_lock_wait_uses_fresh_identity(self):
        # TOCTOU control: identity is snapshotted INSIDE the transaction — a
        # mutation that waited out a re-enrollment must act as the NEW Stand
        import fcntl
        import threading
        self._enroll()
        self._verify()
        lock_path = self.el.links_path(self.state).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)  # external holder blocks mutators
        result = {}

        def mutate():
            try:
                self.el.authorize_link(self.state, "discord", "@o:x")
                result["outcome"] = "authorized"
            except ValueError as e:
                result["outcome"] = f"refused: {e}"

        t = threading.Thread(target=mutate)
        t.start()
        import time
        time.sleep(0.3)  # mutator is now blocked on the ledger lock
        # re-enroll as a DIFFERENT Stand while the mutator waits
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": "@stand-b:x"}))
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
        t.join(timeout=10)
        # fresh snapshot = Stand B; the discord row belongs to the ORIGINAL
        # stand, so the mutation must refuse — stale-A authority never commits
        self.assertTrue(result.get("outcome", "").startswith("refused"),
                        result.get("outcome"))
        row = self.el.load_links(self.state)[0]
        self.assertNotIn("authorized_by", row)

    def test_concurrent_mutations_lose_nothing(self):
        # production mutators from N threads; the ledger lock must serialize
        # the whole load->mutate->save transaction (kewei's lost-update repro)
        import threading
        self._enroll()
        provs = [f"prov{i}" for i in range(8)]
        errs = []

        def verify(pv):
            try:
                self.el.upsert_link(
                    self.state, pv, {"type": "bot_user", "id": pv},
                    {"method": "m", "verified_at": "t"}, "sha256:ab")
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        threads = [threading.Thread(target=verify, args=(pv,)) for pv in provs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        links = self.el.load_links(self.state)
        self.assertEqual(sorted(l["provider"] for l in links), sorted(provs))
        auth_errs = []

        def authz(pv):
            try:
                self.el.authorize_link(self.state, pv, "@o:x")
            except Exception as e:  # noqa: BLE001
                auth_errs.append(e)

        threads = [threading.Thread(target=authz, args=(pv,)) for pv in provs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(auth_errs, [])
        authorized = [l["provider"] for l in self.el.load_links(self.state)
                      if l.get("authorized_by")]
        self.assertEqual(sorted(authorized), sorted(provs))  # zero lost updates

    def test_corrupt_store_surfaces_policy_invalid_never_no_links(self):
        # a present-but-unreadable store must never read as "no binding"
        self._enroll()
        self._verify()
        store = self.state / "auth" / "entrance-links.json"
        store.write_text("{not json")
        (self.channels / "discord").mkdir(parents=True, exist_ok=True)
        v = _mk(self.state, self.channels)
        ents = {e["provider"]: e for e in v.entrances()["channels"]}
        self.assertEqual(ents["discord"]["status"], "policy_invalid")
        self.assertIn("unreadable", ents["discord"]["policy_error"])
        r = v.resolve("discord", "123")
        self.assertFalse(r["resolved"])
        self.assertTrue(r.get("store_corrupt"))
        # mutations refuse rather than rebuilding the store empty
        for fn in (lambda: self._verify(),
                   lambda: self.el.authorize_link(self.state, "discord", "@o:x"),
                   lambda: self.el.revoke_link(self.state, "discord", "@o:x")):
            with self.assertRaises(ValueError):
                fn()
        self.assertEqual(store.read_text(), "{not json")  # untouched
        # non-list shape is corruption too, not an empty store
        store.write_text('{"links": []}')
        with self.assertRaises(ValueError):
            self.el.revoke_link(self.state, "discord", "@o:x")

    def test_cross_stand_authorization_refused(self):
        self._enroll()
        self._verify()
        # link recorded for a DIFFERENT stand must refuse authorization
        links = self.el.load_links(self.state)
        links[0]["stand_id"] = "@other-stand:x"
        (self.state / "auth" / "entrance-links.json").write_text(
            json.dumps(links))
        with self.assertRaises(ValueError):
            self.el.authorize_link(self.state, "discord", "@o:x")

    def test_full_discord_lifecycle(self):
        self._enroll()
        v = _mk(self.state, self.channels)
        # verify -> verified_unlinked
        self._verify()
        e = v.entrances()["channels"][0]
        self.assertEqual((e["status"], e["stand_binding"]),
                         ("verified_unlinked", "absent"))
        # owner authorize -> active, resolve returns the Stand
        lk = self.el.authorize_link(self.state, "discord", "@owner:x",
                                    confirmation_ref="msg$abc")
        self.assertEqual(lk["confirmation_ref"], "msg$abc")
        self.assertEqual(lk["audit"][-1]["op"], "authorize")
        e = v.entrances()["channels"][0]
        self.assertEqual((e["status"], e["stand_binding"]),
                         ("active", "authorized"))
        r = v.resolve("discord", "bot_user:123")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["stand_id"], "@stand:ag2.space")
        # revoke -> not active anywhere, authorization cleared, audited
        rv = self.el.revoke_link(self.state, "discord", "@owner:x", "test")
        self.assertEqual(rv["status"], "revoked")
        self.assertNotIn("authorized_by", rv)
        self.assertEqual(rv["audit"][-1]["op"], "revoke")
        e = v.entrances()["channels"][0]
        self.assertNotEqual(e["status"], "active")
        self.assertFalse(v.resolve("discord", "bot_user:123")["resolved"])

    def test_records_persist_facts_not_composite_states(self):
        self._enroll()
        self._verify()
        blob = (self.state / "auth" / "entrance-links.json").read_text()
        self.assertNotIn("verified_unlinked", blob)
        self.assertNotIn("configured_unverified", blob)


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
        # Asserted through the SHIPPED entry point: the enrolled lookup now
        # lives in rundir.py, which is what the CLI and shell read too.
        env = ("SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID")
        saved = {k: os.environ.pop(k, None) for k in env}
        try:
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "auth").mkdir()
                (Path(td) / "auth" / "ag2space.json").write_text(
                    '{"agent_id": "@enrolled:ag2.space"}')
                self.assertEqual(srv.resolve_actor_id(td), "@enrolled:ag2.space")
            with tempfile.TemporaryDirectory() as td2:
                self.assertEqual(srv.resolve_actor_id(td2), "local-agent")
            os.environ["SUTANDO_AGENT_ID"] = "@env:ag2.space"
            self.assertEqual(srv.resolve_actor_id(td), "@env:ag2.space")
        finally:
            for k in env:
                os.environ.pop(k, None)
                if saved.get(k) is not None:
                    os.environ[k] = saved[k]



class ChannelsIterationSkips(unittest.TestCase):
    def test_dir_without_access_json_and_unreadable_one_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            channels = Path(td) / "channels"
            state.mkdir()
            (channels / "bare").mkdir(parents=True)          # no access.json
            broken = channels / "broken"
            broken.mkdir()
            (broken / "access.json").write_text("{nope")      # unparseable
            good = channels / "good"
            good.mkdir()
            (good / "access.json").write_text(
                json.dumps({"tofuOwner": "u1", "allowFrom": ["u1"]}))
            v = _mk(state, channels)
            self.assertEqual(list(v.owner()["owners"].keys()), ["good"])
            self.assertEqual(v.allowlist()["channels"], {"good": ["u1"]})


class TestDiscordEntranceVerifyEdge(unittest.TestCase):
    """The provider-I/O edge: verify_discord lives in channels/discord and
    delegates the API read to DiscordRestClient; entrance_links itself makes
    no provider call (the census pins the literal to the chokepoint)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "auth").mkdir(parents=True)
        (self.state / "auth" / "ag2space.json").write_text(
            json.dumps({"agent_id": "@stand:ag2.space"}))
        sys.path.insert(0, str(ROOT / "src"))
        import importlib
        self.ev = importlib.import_module("channels.discord.entrance_verify")

    def tearDown(self):
        self.tmp.cleanup()
        sys.path.remove(str(ROOT / "src"))

    def test_edge_delegates_to_client_and_records_domain_facts(self):
        calls = []

        class FakeClient:
            def get_json(self, path):
                calls.append(path)
                return {"id": 987, "username": "bot", "avatar": "av"}

        link = self.ev.verify_discord(self.state, "tok-sekret",
                                      client=FakeClient())
        self.assertEqual(calls, ["/users/@me"])
        self.assertEqual(link["provider_subject"],
                         {"type": "bot_user", "id": "987"})
        self.assertEqual(link["verification"]["method"],
                         "discord_token_introspection")
        # only a fingerprint is recorded, never the credential
        self.assertNotIn("tok-sekret", json.dumps(link))
        self.assertTrue(link["credential"]["fingerprint"].startswith("sha256:"))

    def test_domain_module_offers_no_provider_io(self):
        import importlib
        el = importlib.import_module("entrance_links")
        self.assertFalse(hasattr(el, "verify_discord"))
        src = (ROOT / "src" / "entrance_links.py").read_text()
        self.assertNotIn("discord.com/api", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
