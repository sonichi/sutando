#!/usr/bin/env python3
"""Contract tests for src/connections.py — the one writer of state/connections.json.

Pins: schema validation on read, atomic publication, single-writer locking,
preview before save (server from the gateway's own answer, else its host),
confirm against expected_server, idempotent add per server, stable primary with
an explicit set_primary, vault fail-closed, env import once, status/state join,
launch env shape + records, reconnect, and remove leaving the others alone.
The vault, the gateway probe and the process spawn are fakes handed in.

Run: python3 tests/connections.test.py
"""
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import connections as C  # noqa: E402
import dotspace  # noqa: E402

# Tests resolve offline: the real resolver with no well-known fetch and this registry.
REGISTRY = {"instances": [{"server": "s.space", "gateway_host": "chat.s.space"}]}


def offline_resolver(host):
    return dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)


C.DEFAULT_RESOLVER = offline_resolver

TOKEN_A = "https://chat.alpha.example|secret-alpha"
TOKEN_B = "https://relay.beta.example:8443|secret-beta"


class FakeVault:
    def __init__(self, *, fail_set=False, fail_delete=False):
        self.store = {}
        self.fail_set, self.fail_delete = fail_set, fail_delete

    def get(self, key):
        if key not in self.store:
            raise KeyError(key)
        return self.store[key]

    def set(self, key, value):
        if self.fail_set:
            raise RuntimeError("keychain locked")
        self.store[key] = value

    def delete(self, key):
        if self.fail_delete:
            raise RuntimeError("keychain locked")
        self.store.pop(key, None)


def agents(*ids):
    """A gateway probe answering GET /v1/agents with these agent rows."""
    def fetch(url, token):
        return {"agents": [{"id": i, "owner": "@owner:" + i.split(":", 1)[1]} for i in ids]}
    return fetch


def http_error(code):
    def fetch(url, token):
        raise urllib.error.HTTPError(url, code, "nope", {}, None)
    return fetch


def unreachable(url, token):
    raise ConnectionRefusedError("down")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.vault = FakeVault()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, token=TOKEN_A, *, fetch=agents("@sutando:alpha.example"), expected=None, **kw):
        kw.setdefault("added_by", "owner")
        preview = C.preview_connection(token, fetch=fetch)
        return C.add_connection(token, expected_server=expected or preview["server"], preview=preview,
                                workspace=self.ws, vault=self.vault, **kw)

    def rows(self):
        return C.list_connections(self.ws)


class TokenDerivation(unittest.TestCase):
    def test_host_fallback_is_the_lowercased_gateway_host(self):
        self.assertEqual(C.server_of_token(TOKEN_A), "chat.alpha.example")
        self.assertEqual(C.server_of_token(TOKEN_B), "relay.beta.example")
        self.assertEqual(C.server_of_token("HTTPS://GW.Alpha.Example/|s"), "gw.alpha.example")

    def test_bare_secret_and_empty_secret_have_no_server(self):
        for token in ("opaque-bearer", "https://gw.alpha.example|", "gw.alpha.example|s", ""):
            self.assertEqual(C.server_of_token(token), "", token)

    def test_gateway_url_is_kept_exactly_as_issued(self):
        self.assertEqual(C.gateway_url_of("https://Chat.Alpha.Example/v1/|s"), "https://Chat.Alpha.Example/v1/")
        self.assertEqual(C.secret_of("https://x|abc"), "abc")

    def test_long_server_names_never_share_a_slug(self):
        a = C.instance_for("a" * 40 + ".example")
        b = C.instance_for("a" * 40 + ".example2")
        self.assertTrue(len(a) <= 32 and len(b) <= 32)
        self.assertNotEqual(a, b)
        self.assertTrue(C.INSTANCE_RE.match(a))
        self.assertEqual(C.instance_for("s.space"), "s-space")

    def test_mxid_server_instance_and_token_ref(self):
        self.assertEqual(C.server_of_mxid("@bot:s.space"), "s.space")
        self.assertEqual(C.server_of_mxid("@bot:S.Space:8448"), "s.space")
        self.assertEqual(C.server_of_mxid("not-an-mxid"), "")
        self.assertEqual(C.instance_for("s.space"), "s-space")
        self.assertEqual(C.token_ref_for("s-space"), "AG2_REMOTE_TOKEN_S_SPACE")
        self.assertEqual(C.token_ref_for(""), "REMOTE_TASK_TOKEN")
        self.assertEqual(C.channel_dir_for("dev", {}), "dev-ag2space")
        self.assertEqual(C.channel_dir_for("s-space", {"REMOTE_TASK_CHANNEL_DIR_S_SPACE": "custom"}), "custom")


class Preview(unittest.TestCase):
    def test_one_agent_verifies_the_server_from_its_mxid(self):
        p = C.preview_connection(TOKEN_A, fetch=agents("@sutando:alpha.example"))
        self.assertEqual(p, {"gateway_url": "https://chat.alpha.example", "gateway_host": "chat.alpha.example",
                             "server": "alpha.example", "server_source": "identity",
                             "agent_mxid": "@sutando:alpha.example", "verified": True,
                             "authenticated": True, "error": None})

    def test_ambiguous_or_empty_fleet_falls_back_to_the_gateway_host(self):
        p = C.preview_connection(TOKEN_A, fetch=agents("@a:alpha.example", "@b:alpha.example"))
        self.assertEqual((p["server"], p["verified"], p["authenticated"], p["agent_mxid"]),
                         ("chat.alpha.example", False, True, None))
        p = C.preview_connection(TOKEN_A, fetch=lambda u, t: {"agents": []})
        self.assertEqual((p["server"], p["verified"]), ("chat.alpha.example", False))

    def test_auth_rejection_and_outage_are_named_without_the_secret(self):
        p = C.preview_connection(TOKEN_A, fetch=http_error(401))
        self.assertEqual((p["authenticated"], p["error"], p["verified"]), (False, "auth_rejected", False))
        p = C.preview_connection(TOKEN_A, fetch=http_error(503))
        self.assertEqual((p["authenticated"], p["error"]), (None, "http_503"))
        p = C.preview_connection(TOKEN_A, fetch=unreachable)
        self.assertEqual(p["authenticated"], None)
        self.assertIn("unreachable", p["error"])
        self.assertNotIn("secret-alpha", json.dumps(p))

    def test_probe_sends_the_secret_as_bearer_to_the_embedded_gateway(self):
        seen = {}

        def fetch(url, token):
            seen["url"], seen["token"] = url, token
            return {}
        C.preview_connection("https://chat.alpha.example/|abc", fetch=fetch)
        self.assertEqual(seen, {"url": "https://chat.alpha.example/", "token": "https://chat.alpha.example/|abc"})

    def test_token_without_gateway_url_is_refused(self):
        with self.assertRaises(C.InvalidToken):
            C.preview_connection("opaque", fetch=unreachable)

    def test_404_is_not_a_gateway(self):
        p = C.preview_connection(TOKEN_A, fetch=http_error(404))
        self.assertEqual((p["authenticated"], p["error"], p["server_source"]), (None, "not_a_gateway", "gateway"))

    def test_resolver_names_the_server_of_a_gateway_host(self):
        for token in ("https://chat.s.space/relay|s1", "https://matrix.s.space/relay|s2"):
            p = C.preview_connection(token, fetch=unreachable)
            self.assertEqual((p["server"], p["server_source"], p["verified"]), ("s.space", "resolver", False), token)
        p = C.preview_connection(TOKEN_A, fetch=unreachable)
        self.assertEqual((p["server"], p["server_source"]), ("chat.alpha.example", "gateway"))
        p = C.preview_connection("https://chat.s.space|s", fetch=agents("@bot:other.example"))
        self.assertEqual((p["server"], p["server_source"]), ("other.example", "identity"), "identity beats the resolver")

    def test_resolver_is_optional_and_may_fail(self):
        self.assertEqual(C.resolve_server("chat.s.space", resolver=lambda h: None), "")
        self.assertEqual(C.resolve_server("chat.s.space", resolver=lambda h: (_ for _ in ()).throw(OSError())), "")

        class Stub:
            server, source = "stub.example", "config"
        self.assertEqual(C.resolve_server("anything", resolver=lambda h: Stub()), "stub.example")
        saved = C.DEFAULT_RESOLVER
        try:
            C.DEFAULT_RESOLVER = lambda h: Stub()
            self.assertEqual(C.preview_connection(TOKEN_A, fetch=unreachable)["server_source"], "resolver")
        finally:
            C.DEFAULT_RESOLVER = saved

    def test_probe_refuses_redirects(self):
        import http.server
        import threading

        class Redirecting(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
        t = threading.Thread(target=srv.handle_request)
        t.start()
        try:
            p = C.preview_connection(f"http://127.0.0.1:{srv.server_address[1]}|s", resolver=lambda h: None)
        finally:
            t.join(5)
            srv.server_close()
        self.assertEqual((p["authenticated"], p["error"]), (None, "http_302"))


class AddConnection(Base):
    def test_add_writes_row_and_stores_secret_only_in_vault(self):
        row, created = self.add()
        self.assertTrue(created)
        self.assertEqual(row["server"], "alpha.example")
        self.assertEqual(row["instance"], "alpha-example")
        self.assertEqual(row["token_ref"], "AG2_REMOTE_TOKEN_ALPHA_EXAMPLE")
        self.assertEqual(row["gateway_url"], "https://chat.alpha.example")
        self.assertEqual(row["agent_mxid"], "@sutando:alpha.example")
        self.assertTrue(row["verified"])
        self.assertTrue(row["primary"], "the first row ever saved is the primary")
        self.assertEqual(row["added_by"], "owner")
        self.assertTrue(row["added_at"].endswith("Z"))
        self.assertEqual(self.vault.store[row["token_ref"]], TOKEN_A)
        text = C.record_path(self.ws).read_text()
        self.assertNotIn("secret-alpha", text)
        self.assertEqual(self.rows(), [row])
        self.assertEqual(oct(C.record_path(self.ws).stat().st_mode & 0o777), "0o600")

    def test_unverified_add_records_the_host_and_verified_false(self):
        row, _ = self.add(fetch=unreachable)
        self.assertEqual((row["server"], row["verified"], row.get("agent_mxid"), row["server_source"]),
                         ("chat.alpha.example", False, None, "gateway"))

    def test_two_endpoint_tokens_of_one_homeserver_make_one_row(self):
        first, created = self.add("https://chat.s.space/relay|s1", fetch=unreachable)
        second, created2 = self.add("https://matrix.s.space/relay|s2", fetch=unreachable)
        self.assertEqual((created, created2), (True, False))
        self.assertEqual(second, first)
        self.assertEqual((first["server"], first["server_source"], first["instance"]), ("s.space", "resolver", "s-space"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(C.launch_plan({}, workspace=self.ws)), 1)

    def test_same_gateway_host_matches_without_a_resolver(self):
        first, _ = self.add("https://chat.alpha.example/relay|s1", fetch=unreachable)
        again, created = self.add("https://chat.alpha.example/other|s2", fetch=unreachable)
        self.assertFalse(created)
        self.assertEqual(again["instance"], first["instance"])
        self.assertEqual(len(self.rows()), 1)

    def test_verified_add_rekeys_a_provisional_row_in_place(self):
        first, _ = self.add(fetch=unreachable)
        self.assertEqual((first["server"], first["server_source"]), ("chat.alpha.example", "gateway"))
        again, created = self.add("https://chat.alpha.example|rotated", fetch=agents("@sutando:alpha.example"))
        self.assertFalse(created)
        self.assertEqual((again["server"], again["server_source"], again["verified"], again["agent_mxid"]),
                         ("alpha.example", "identity", True, "@sutando:alpha.example"))
        self.assertEqual(again["instance"], first["instance"], "the slug is stable")
        self.assertEqual(again["token_ref"], first["token_ref"])
        self.assertEqual(self.vault.store[first["token_ref"]], TOKEN_A, "the stored token is untouched")
        self.assertEqual(self.rows(), [again])
        self.assertEqual(C.status_for(again, self.ws)["server_source"], "identity")
        # Once verified, a later unverified or differing probe never demotes it.
        later, _ = self.add("https://chat.alpha.example|x", fetch=unreachable)
        self.assertEqual((later["server"], later["server_source"]), ("alpha.example", "identity"))

    def test_expected_server_must_match_what_the_gateway_says_now(self):
        with self.assertRaises(C.ConnectionMismatch) as cm:
            self.add(expected="chat.alpha.example")
        self.assertIn("alpha.example", str(cm.exception))
        self.assertIn("chat.alpha.example", str(cm.exception))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.vault.store, {})
        with self.assertRaises(C.ConnectionMismatch):
            C.add_connection(TOKEN_A, added_by="owner", expected_server="", fetch=unreachable,
                             workspace=self.ws, vault=self.vault)
        row, _ = self.add(expected="ALPHA.example")
        self.assertEqual(row["server"], "alpha.example")

    def test_add_is_idempotent_per_server_and_never_moves_primary(self):
        first, created = self.add()
        again, created2 = self.add("https://matrix.alpha.example|other-secret")
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(again, first)
        self.assertEqual(self.vault.store, {first["token_ref"]: TOKEN_A})
        second, created3 = self.add(TOKEN_B, fetch=agents("@sutando:beta.example"))
        self.assertTrue(created3)
        self.assertFalse(second["primary"])
        self.assertEqual([r["primary"] for r in self.rows()], [True, False])
        self.assertEqual(len(C.launch_plan({}, workspace=self.ws)), 2)

    def test_token_without_gateway_url_is_refused(self):
        with self.assertRaises(C.InvalidToken):
            C.add_connection("opaque-bearer", added_by="owner", expected_server="x", fetch=unreachable,
                             workspace=self.ws, vault=self.vault)
        self.assertEqual(self.rows(), [])

    def test_vault_failure_is_fail_closed(self):
        self.vault.fail_set = True
        with self.assertRaises(C.VaultUnavailable) as cm:
            self.add()
        self.assertNotIn("secret-alpha", str(cm.exception))
        self.assertEqual(self.rows(), [])

    def test_vault_not_importable_is_fail_closed(self):
        class Broken:
            def __getattr__(self, name):
                raise AttributeError(name)
        with self.assertRaises(C.VaultUnavailable):
            C.add_connection(TOKEN_A, added_by="owner", expected_server="chat.alpha.example",
                             fetch=unreachable, workspace=self.ws, vault=Broken())

    def test_instance_collision_and_bad_inputs(self):
        self.add(instance="shared")
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta.example"), instance="shared")
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta.example"), added_by="robot")
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta.example"), instance="has space")
        self.assertEqual(len(self.rows()), 1)


class Primary(Base):
    def test_set_primary_moves_exactly_one_flag(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        row = C.set_primary("beta-example", workspace=self.ws)
        self.assertTrue(row["primary"])
        self.assertEqual([(r["instance"], r["primary"]) for r in self.rows()],
                         [("alpha-example", False), ("beta-example", True)])
        with self.assertRaises(C.UnknownConnection):
            C.set_primary("nope", workspace=self.ws)

    def test_primary_in_the_plan_keeps_the_proactive_room(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        plan = C.launch_plan({}, workspace=self.ws)
        self.assertEqual([(e["instance"], e["primary"]) for e in plan],
                         [("alpha-example", True), ("beta-example", False)])


class RemoveConnection(Base):
    def test_remove_drops_row_vault_key_and_launch_record_only_for_that_instance(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        for inst in ("alpha-example", "beta-example"):
            C._record_launch(inst, self.ws, ok=True, pid=1)
        gone = C.remove_connection("beta-example", workspace=self.ws, vault=self.vault)
        self.assertEqual(gone["instance"], "beta-example")
        self.assertEqual([r["instance"] for r in self.rows()], ["alpha-example"])
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_EXAMPLE"})
        self.assertFalse(C.launch_record_path("beta-example", self.ws).exists())
        self.assertTrue(C.launch_record_path("alpha-example", self.ws).exists())
        self.assertTrue(self.rows()[0]["primary"])
        self.assertEqual(sum(r["primary"] for r in self.rows()), 1)

    def test_primary_needs_force_and_no_row_inherits_the_flag(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        with self.assertRaises(C.PrimaryProtected):
            C.remove_connection("alpha-example", workspace=self.ws, vault=self.vault)
        self.assertEqual(len(self.rows()), 2)
        C.remove_connection("alpha-example", force=True, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["primary"]) for r in self.rows()], [("beta-example", False)])
        row, _ = self.add()
        self.assertTrue(row["primary"], "an install with no primary gives it to the next row saved")

    def test_unknown_instance(self):
        with self.assertRaises(C.UnknownConnection):
            C.remove_connection("nope", workspace=self.ws, vault=self.vault)

    def test_vault_delete_failure_keeps_the_row(self):
        self.add()
        self.vault.fail_delete = True
        with self.assertRaises(C.VaultUnavailable):
            C.remove_connection("alpha-example", force=True, workspace=self.ws, vault=self.vault)
        self.assertEqual(len(self.rows()), 1)


class ImportFromEnv(Base):
    ENV = {"REMOTE_TASK_TOKEN": TOKEN_A, "AG2_REMOTE_TOKEN_DEV": TOKEN_B,
           "AG2_REMOTE_TOKEN_EMPTY": "  ", "AG2_REMOTE_TOKEN_BARE": "opaque", "UNRELATED": "x"}

    def test_import_creates_rows_once(self):
        created = C.import_from_env(self.ENV, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["token_ref"], r["primary"], r["added_by"], r["verified"])
                          for r in created],
                         [("", "REMOTE_TASK_TOKEN", True, "env", False),
                          ("dev", "AG2_REMOTE_TOKEN_DEV", False, "env", False)])
        self.assertEqual(created[1]["server"], "relay.beta.example")
        self.assertEqual(self.vault.store, {"REMOTE_TASK_TOKEN": TOKEN_A, "AG2_REMOTE_TOKEN_DEV": TOKEN_B})
        self.assertEqual(C.import_from_env(self.ENV, workspace=self.ws, vault=self.vault), [])
        self.assertEqual(len(self.rows()), 2)

    def test_import_never_overwrites_an_existing_row_or_vault_value(self):
        self.vault.store["AG2_REMOTE_TOKEN_DEV"] = "already-there"
        C.import_from_env({"AG2_REMOTE_TOKEN_DEV": TOKEN_B}, workspace=self.ws, vault=self.vault)
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_DEV"], "already-there")
        before = self.rows()[0]
        C.import_from_env({"AG2_REMOTE_TOKEN_DEV": "https://elsewhere.example|zzz"},
                          workspace=self.ws, vault=self.vault)
        self.assertEqual(self.rows()[0], before)

    def test_import_skips_a_server_that_already_has_a_row(self):
        self.add(TOKEN_B, fetch=unreachable)
        self.assertEqual(C.import_from_env({"AG2_REMOTE_TOKEN_DEV": TOKEN_B}, workspace=self.ws, vault=self.vault), [])

    def test_named_env_tokens_never_become_primary_or_inherit_the_room(self):
        created = C.import_from_env({"AG2_REMOTE_TOKEN_DEV": TOKEN_B}, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["primary"]) for r in created], [("dev", False)])
        entry = C.plan_entry("dev", {}, workspace=self.ws)
        env = C.launch_env(entry, {"REMOTE_PROACTIVE_ROOM": "!parent:example"}, vault=self.vault)
        self.assertEqual(env["REMOTE_PROACTIVE_ROOM"], "")
        later = C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["primary"]) for r in later], [("", True)])
        row, _ = self.add("https://gamma.example|g", fetch=unreachable)
        self.assertFalse(row["primary"], "a saved row never displaces the primary")

    def test_import_uses_the_resolver_and_collapses_endpoints(self):
        created = C.import_from_env({"AG2_REMOTE_TOKEN_CHAT": "https://chat.s.space|a",
                                     "AG2_REMOTE_TOKEN_MATRIX": "https://matrix.s.space|b"},
                                    workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["server"], r["server_source"]) for r in created],
                         [("chat", "s.space", "resolver")])

    def test_import_with_nothing_set_writes_nothing(self):
        self.assertEqual(C.import_from_env({}, workspace=self.ws, vault=self.vault), [])
        self.assertFalse(C.record_path(self.ws).exists())

    def test_import_is_fail_closed_when_the_vault_refuses(self):
        self.vault.fail_set = True
        with self.assertRaises(C.VaultUnavailable):
            C.import_from_env(self.ENV, workspace=self.ws, vault=self.vault)
        self.assertEqual(self.rows(), [])


class Record(Base):
    def test_missing_record_lists_empty(self):
        self.assertEqual(self.rows(), [])

    def test_schema_errors_are_named(self):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True)

        def row(**over):
            base = {"server": "a", "instance": "x", "gateway_url": "", "token_ref": "T", "primary": True,
                    "added_by": "env", "added_at": "", "verified": False, "server_source": "gateway"}
            base.update(over)
            return base
        cases = [
            ("not json{", "not valid JSON"),
            (json.dumps({"schema_version": 2, "connections": []}), "schema_version"),
            (json.dumps({"schema_version": 1, "connections": {}}), "not a list"),
            (json.dumps({"schema_version": 1, "connections": [{"server": "a"}]}), "lacks"),
            (json.dumps({"schema_version": 1, "connections": [row(), row(server="b", instance="y", token_ref="U")]}),
             "more than one primary"),
            (json.dumps({"schema_version": 1, "connections": [row(), row(server="b", token_ref="U")]}),
             "duplicate instance"),
            (json.dumps({"schema_version": 1, "connections": [row(added_by="nobody")]}), "added_by"),
            (json.dumps({"schema_version": 1, "connections": [row(verified="yes")]}), "verified"),
            (json.dumps({"schema_version": 1, "connections": [row(server_source="guess")]}), "server_source"),
        ]
        for text, needle in cases:
            path.write_text(text)
            with self.assertRaises(C.SchemaError) as cm:
                self.rows()
            self.assertIn(needle, str(cm.exception), text)

    def test_oversized_record_is_refused_before_parsing(self):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text("x" * (C.MAX_BYTES + 1))
        with self.assertRaises(C.SchemaError) as cm:
            self.rows()
        self.assertIn("bound", str(cm.exception))

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.add()
        names = sorted(p.name for p in C.record_path(self.ws).parent.iterdir())
        self.assertEqual(names, sorted([C.LOCK_NAME, C.STATE_NAME]))
        self.assertEqual(json.loads(C.record_path(self.ws).read_text())["schema_version"], 1)

    def test_concurrent_writers_serialize_through_the_lock(self):
        errors = []

        def worker(i):
            try:
                C.add_connection(f"https://gw{i}.example|s{i}", added_by="ui", expected_server=f"gw{i}.example",
                                 fetch=unreachable, workspace=self.ws, vault=self.vault)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = self.rows()
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(r["primary"] for r in rows), 1)

    def test_transaction_failure_writes_nothing(self):
        self.add(instance="shared")
        before = C.record_path(self.ws).read_text()
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta.example"), instance="shared")
        self.assertEqual(C.record_path(self.ws).read_text(), before)


class StatusJoin(Base):
    def _sidecar(self, name, **payload):
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        (self.ws / "state" / name).write_text(json.dumps(payload))

    def test_states(self):
        self.assertEqual(C.derive_state({}, {}), ("pending", None))
        self.assertEqual(C.derive_state({}, {"ok": True, "pid": 4}), ("pending", None))
        self.assertEqual(C.derive_state({}, {"ok": False, "reason": "no_token"}), ("failed", "no_token"))
        self.assertEqual(C.derive_state({"connected": True}, {"ok": True}), ("connected", None))
        self.assertEqual(C.derive_state({"connected": False, "error": "timed out"}, {}), ("disconnected", None))
        self.assertEqual(C.derive_state({"connected": False, "error": "HTTP 401"}, {}), ("failed", "auth_rejected"))
        self.assertEqual(C.derive_state({"connected": False, "reenroll": {"pending": True}}, {}),
                         ("failed", "auth_rejected"))

    def test_status_joins_the_instance_suffixed_file_and_launch_record(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        dev, _ = self.add(TOKEN_B, fetch=agents("@s:beta.example"), instance="dev")
        self._sidecar("gateway-status.json", connected=True, last_ok_ts=1700000000)
        self._sidecar("gateway-status.dev.json", connected=False, last_ok_ts=None, error="HTTP 401 rejected")
        legacy, secondary = (C.status_for(r, self.ws) for r in self.rows())
        self.assertEqual((legacy["status"], legacy["state"]), ({"connected": True, "last_ok_ts": 1700000000}, "connected"))
        self.assertEqual((secondary["state"], secondary["reason"]), ("failed", "auth_rejected"))
        self.assertEqual(secondary["instance"], dev["instance"])
        C._record_launch("dev", self.ws, ok=False, reason="no_token")
        self.assertEqual(C.status_for(dev, self.ws)["reason"], "no_token")

    def test_missing_or_broken_status_reads_as_pending(self):
        row, _ = self.add()
        self.assertEqual(C.status_for(row, self.ws)["status"], {"connected": None, "last_ok_ts": None})
        self.assertEqual(C.status_for(row, self.ws)["state"], "pending")
        self._sidecar(f"gateway-status.{row['instance']}.json")
        (self.ws / "state" / f"gateway-status.{row['instance']}.json").write_text("{broken")
        self.assertEqual(C.status_for(row, self.ws)["state"], "pending")


class Launching(Base):
    def setUp(self):
        super().setUp()
        self.spawned = []

        def spawn(argv, env, log_path):
            self.spawned.append((argv, env, log_path))
            return 4242
        self.spawn = spawn
        self.repo = self.ws / "repo"
        self.logs = self.ws / "logs"

    def test_plan_lists_rows_with_an_instance_then_unrecorded_env_vars(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        self.add(TOKEN_B, fetch=agents("@s:beta.example"), instance="dev")
        env = {"AG2_REMOTE_TOKEN_DEV": TOKEN_B, "AG2_REMOTE_TOKEN_QA": "https://qa.example|q",
               "REMOTE_TASK_CHANNEL_DIR_QA": "qa-custom"}
        self.assertEqual(C.launch_plan(env, workspace=self.ws), [
            {"instance": "dev", "token_ref": "AG2_REMOTE_TOKEN_DEV", "channel_dir": "dev-ag2space", "primary": False},
            {"instance": "qa", "token_ref": "AG2_REMOTE_TOKEN_QA", "channel_dir": "qa-custom", "primary": False},
        ])
        self.assertEqual([p["instance"] for p in C.launch_plan({"AG2_REMOTE_TOKEN_X": TOKEN_B}, workspace=self.ws)],
                         ["dev", "x"])
        with self.assertRaises(C.UnknownConnection):
            C.plan_entry("nope", {}, workspace=self.ws)

    def test_launch_env_shape_token_from_env_then_vault(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        entry = C.plan_entry("beta-example", {}, workspace=self.ws)
        base = {"REMOTE_PROACTIVE_ROOM": "!room:primary", "REMOTE_TASK_TIER": "owner", "PATH": "/usr/bin"}
        env = C.launch_env(entry, base, vault=self.vault)
        self.assertEqual({k: env[k] for k in ("GATEWAY_INSTANCE", "REMOTE_TASK_TOKEN", "REMOTE_TASK_URL",
                                                "REMOTE_TASK_CHANNEL_DIR", "REMOTE_PROACTIVE_ROOM",
                                                "SUTANDO_SUPERVISED", "REMOTE_TASK_TIER")},
                         {"GATEWAY_INSTANCE": "beta-example", "REMOTE_TASK_TOKEN": TOKEN_B, "REMOTE_TASK_URL": "",
                          "REMOTE_TASK_CHANNEL_DIR": "beta-example-ag2space", "REMOTE_PROACTIVE_ROOM": "!room:primary",
                          "SUTANDO_SUPERVISED": "1", "REMOTE_TASK_TIER": "owner"})
        self.assertIn("REMOTE_TASK_TOKEN_FILE", env)
        self.add(TOKEN_A, instance="alpha")
        entry = C.plan_entry("alpha", {}, workspace=self.ws)
        env = C.launch_env(entry, dict(base, AG2_REMOTE_TOKEN_ALPHA="https://env.example|from-env"), vault=self.vault)
        self.assertEqual(env["REMOTE_TASK_TOKEN"], "https://env.example|from-env", "the env var wins over the vault")
        self.assertEqual(env["REMOTE_PROACTIVE_ROOM"], "", "a secondary gets no proactive room")
        self.vault.store.clear()
        with self.assertRaises(C.NoToken):
            C.launch_env(entry, base, vault=self.vault)

    def test_launch_spawns_the_bridge_and_records_the_pid(self):
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        record = C.launch("beta-example", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                          vault=self.vault, spawn=self.spawn, python="py3")
        argv, env, log_path = self.spawned[0]
        self.assertEqual(argv, ["py3", str(self.repo / "src" / "remote-gateway-bridge.py")])
        self.assertEqual(env["GATEWAY_INSTANCE"], "beta-example")
        self.assertEqual(log_path, self.logs / "remote-gateway-bridge.beta-example.log")
        self.assertEqual((record["ok"], record["pid"]), (True, 4242))
        self.assertEqual(json.loads(C.launch_record_path("beta-example", self.ws).read_text())["pid"], 4242)
        self.assertEqual(C.status_for(self.rows()[0], self.ws)["launched_at"], record["ts"])

    def test_launch_failures_are_recorded_as_failed_with_a_reason(self):
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        self.vault.store.clear()
        with self.assertRaises(C.LaunchFailed):
            C.launch("beta-example", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                     vault=self.vault, spawn=self.spawn)
        self.assertEqual(C.status_for(self.rows()[0], self.ws)["reason"], "no_token")
        self.vault.store["AG2_REMOTE_TOKEN_BETA_EXAMPLE"] = TOKEN_B

        def boom(argv, env, log_path):
            raise OSError("no interpreter")
        with self.assertRaises(C.LaunchFailed) as cm:
            C.launch("beta-example", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                     vault=self.vault, spawn=boom)
        self.assertNotIn("secret-beta", str(cm.exception))
        self.assertEqual(C.status_for(self.rows()[0], self.ws)["reason"], "launch_failed")

    def test_reconnect_stops_the_lock_holder_then_relaunches(self):
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        locks = self.ws / "state" / "locks"
        locks.mkdir(parents=True)
        lock = locks / "gateway-bridge.beta-example.lock"
        lock.write_text("31337\n")
        self.assertEqual(C.bridge_pid("beta-example", self.ws), 0, "an unheld lock file names no live bridge")
        fd = os.open(str(lock), os.O_RDWR)
        from file_lock import lock_fd
        lock_fd(fd)
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            from file_lock import unlock_fd
            unlock_fd(fd)
        try:
            self.assertEqual(C.bridge_pid("beta-example", self.ws), 31337)
            record = C.reconnect("beta-example", repo=self.repo, logs_dir=self.logs, environ={},
                                 workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=kill)
        finally:
            os.close(fd)
        self.assertEqual(killed, [(31337, 15)])
        self.assertEqual((record["stopped_pid"], record["pid"]), (31337, 4242))
        self.assertEqual(len(self.spawned), 1)

    def test_reconnect_learns_the_identity_and_rekeys(self):
        row, _ = self.add(TOKEN_B, fetch=unreachable)
        self.assertEqual(row["server_source"], "gateway")
        record = C.reconnect("relay-beta-example", repo=self.repo, logs_dir=self.logs, environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=lambda *a: None,
                             fetch=agents("@sutando:beta.example"))
        self.assertEqual(record["pid"], 4242)
        after = self.rows()[0]
        self.assertEqual((after["server"], after["server_source"], after["instance"], after["agent_mxid"]),
                         ("beta.example", "identity", "relay-beta-example", "@sutando:beta.example"))
        self.assertEqual(self.spawned[0][1]["GATEWAY_INSTANCE"], "relay-beta-example")
        # A probe that fails leaves the row as it was and still launches.
        C.reconnect("relay-beta-example", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                    vault=self.vault, spawn=self.spawn, kill=lambda *a: None, fetch=unreachable)
        self.assertEqual(self.rows()[0]["server_source"], "identity")
        self.assertEqual(len(self.spawned), 2)

    def test_reconnect_without_a_running_bridge_just_launches(self):
        self.add(TOKEN_B, fetch=agents("@s:beta.example"))
        record = C.reconnect("beta-example", repo=self.repo, logs_dir=self.logs, environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.spawn,
                             kill=lambda *a: self.fail("nothing to kill"))
        self.assertEqual(record["stopped_pid"], 0)


class Edges(Base):
    """The defensive branches: malformed URLs, the real vault binding, the real spawn, the
    rollback after a refused write, the lock-holder reads and the resolver import."""

    def test_malformed_urls_have_no_host(self):
        self.assertEqual(C.server_of_token("https://[bad|secret"), "")
        self.assertEqual(C.host_of_url("https://[bad"), "")
        self.assertEqual(C.resolve_server("", resolver=lambda h: self.fail("never asked")), "")

    def test_default_resolver_import_paths(self):
        saved = C.DEFAULT_RESOLVER
        try:
            C.DEFAULT_RESOLVER = None
            self.assertIs(C._default_resolver(), __import__("dotspace").dotspace_for)
            C.DEFAULT_RESOLVER = None
            with unittest.mock.patch.dict(sys.modules, {"dotspace": None}):
                self.assertIsNone(C._default_resolver())
                self.assertEqual(C.resolve_server("chat.s.space"), "")
        finally:
            C.DEFAULT_RESOLVER = saved

    def test_probe_success_path_over_http(self):
        import http.server
        import threading

        class Agents(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"agents": [{"id": "@bot:probe.example"}]}).encode())

            def log_message(self, *a):
                pass
        seen = {}
        srv = http.server.HTTPServer(("127.0.0.1", 0), Agents)
        t = threading.Thread(target=srv.handle_request)
        t.start()
        try:
            p = C.preview_connection(f"http://127.0.0.1:{srv.server_address[1]}/relay|the-secret")
        finally:
            t.join(5)
            srv.server_close()
        self.assertEqual(seen, {"auth": "Bearer the-secret", "path": "/relay/v1/agents"})
        self.assertEqual((p["server"], p["server_source"], p["verified"]), ("probe.example", "identity", True))

    def test_vault_binding_uses_the_secret_vault_module(self):
        calls = []

        class Stub:
            def get_vault_key(self, k):
                calls.append(("get", k)); return "v"

            def set_vault_key(self, k, v):
                calls.append(("set", k))

            def delete_vault_key(self, k):
                calls.append(("delete", k))
        with unittest.mock.patch.dict(sys.modules, {"vault_intercept": Stub()}):
            v = C._vault(None)
            v.set("K", "v"); self.assertEqual(v.get("K"), "v"); v.delete("K")
        self.assertEqual(calls, [("set", "K"), ("get", "K"), ("delete", "K")])
        with unittest.mock.patch.dict(sys.modules, {"vault_intercept": None}):
            with self.assertRaises(C.VaultUnavailable):
                C._vault(None)

    def test_default_workspace_comes_from_the_resolver(self):
        os.environ["SUTANDO_TEST_MODE"] = "1"
        os.environ["SUTANDO_WORKSPACE"] = str(self.ws)
        try:
            self.assertEqual(C.record_path(None), self.ws.resolve() / "state" / C.STATE_NAME)
            self.assertEqual(C.list_connections(), [])
        finally:
            del os.environ["SUTANDO_TEST_MODE"], os.environ["SUTANDO_WORKSPACE"]

    def test_remaining_schema_branches(self):
        row = {"server": "a", "instance": "x", "gateway_url": "", "token_ref": "T", "primary": True,
               "added_by": "env", "added_at": "", "verified": False, "server_source": "gateway"}
        for payload, needle in (
            ({"schema_version": 1, "connections": ["nope"]}, "not an object"),
            ({"schema_version": 1, "connections": [dict(row, server=3)]}, "not a string"),
            ({"schema_version": 1, "connections": [dict(row, instance="bad slug!")]}, "not a slug"),
            ({"schema_version": 1, "connections": [dict(row, agent_mxid=7)]}, "agent_mxid"),
            ({"schema_version": 1, "connections": [dict(row, instance=f"i{i}", token_ref=f"T{i}", server=f"s{i}",
                                                       primary=False) for i in range(C.MAX_ROWS + 1)]}, "more than"),
        ):
            with self.assertRaises(C.SchemaError) as cm:
                C._validate(payload)
            self.assertIn(needle, str(cm.exception))

    def test_add_without_a_server_in_the_preview_is_refused(self):
        with self.assertRaises(C.InvalidToken):
            C.add_connection(TOKEN_A, added_by="ui", expected_server="x", preview={"server": ""},
                             workspace=self.ws, vault=self.vault)

    def test_oversized_write_is_refused_and_the_vault_rolled_back(self):
        preview = C.preview_connection(TOKEN_A, fetch=unreachable)
        preview["agent_mxid"] = "@x:" + "y" * (C.MAX_BYTES + 10)
        with self.assertRaises(C.SchemaError):
            C.add_connection(TOKEN_A, added_by="ui", expected_server=preview["server"], preview=preview,
                             workspace=self.ws, vault=self.vault)
        self.assertEqual(self.vault.store, {}, "the stored secret was taken back out")
        self.assertEqual(self.rows(), [])
        self.vault.fail_delete = True
        with self.assertRaises(C.SchemaError):
            C.add_connection(TOKEN_A, added_by="ui", expected_server=preview["server"], preview=preview,
                             workspace=self.ws, vault=self.vault)

    def test_token_file_override_and_missing_channel_home(self):
        entry = {"instance": "dev", "channel_dir": "dev-ag2space"}
        self.assertEqual(C._token_file(entry, {"REMOTE_TASK_TOKEN_FILE_DEV": str(self.ws / "nope.env")}), "")
        (self.ws / "dev.env").write_text("")
        self.assertEqual(C._token_file(entry, {"REMOTE_TASK_TOKEN_FILE_DEV": str(self.ws / "dev.env")}),
                         str(self.ws / "dev.env"))
        with unittest.mock.patch.dict(sys.modules, {"util_paths": None}):
            self.assertEqual(C._token_file(entry, {}), "")

    def test_real_spawn_detaches_and_logs(self):
        log = self.ws / "logs" / "bridge.log"
        pid = C._spawn([sys.executable, "-c", "print('hello from the bridge')"], isolated_child_env(), log)
        self.assertGreater(pid, 0)
        for _ in range(50):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if log.exists() and "hello" in log.read_text():
                break
            time.sleep(0.1)
        self.assertIn("hello from the bridge", log.read_text())

    def test_lock_holder_reads(self):
        locks = self.ws / "state" / "locks"
        locks.mkdir(parents=True)
        lock = locks / "gateway-bridge.dev.lock"
        lock.write_text("not-a-pid\n")
        from file_lock import lock_fd, unlock_fd
        fd = os.open(str(lock), os.O_RDWR)
        lock_fd(fd)
        try:
            self.assertEqual(C.bridge_pid("dev", self.ws), 0, "garbage in the lock reads as no bridge")
            lock.write_text("4242\n")
            self.assertEqual(C.bridge_pid("dev", self.ws), 4242)
            released = []

            def kill(pid, sig):
                def later():
                    time.sleep(0.3)
                    unlock_fd(fd)
                    released.append(pid)
                threading.Thread(target=later).start()
            self.assertEqual(C.stop_bridge("dev", workspace=self.ws, kill=kill), 4242)
        finally:
            os.close(fd)
        self.assertEqual(released, [4242])
        self.assertEqual(C.stop_bridge("dev", workspace=self.ws, kill=lambda *a: self.fail("nothing to kill")), 0)

    def test_reconnect_tolerates_an_unusable_stored_token(self):
        row, _ = self.add(TOKEN_B, fetch=unreachable)
        self.vault.store[row["token_ref"]] = "opaque-bearer"
        record = C.reconnect(row["instance"], repo=self.ws, logs_dir=self.ws / "logs", environ={},
                             workspace=self.ws, vault=self.vault, spawn=lambda a, e, l: 5, kill=lambda *a: None)
        self.assertEqual(record["pid"], 5)
        self.assertEqual(self.rows()[0]["server_source"], "gateway")


if __name__ == "__main__":
    unittest.main(verbosity=1)
