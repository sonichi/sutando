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

import contextlib
import json
import os
import socket
import subprocess
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
import workspace_lock  # noqa: E402
from _helpers import bridge_lease  # noqa: E402

# Above any pid_max (2**22 on Linux, 99999 on macOS): a spawn fake's pid that never runs.
DEAD_PID = 4_194_404

# Tests resolve offline: the real resolver with no well-known fetch and this registry.
REGISTRY = {"instances": [{"server": "s.space", "gateway_host": "chat.s.space"},
                          {"server": "ag2.space", "gateway_host": "chat.ag2.space"},
                          {"server": "alpha-fixture.net", "gateway_host": "chat.alpha-fixture.net"},
                          {"server": "beta-fixture.net", "gateway_host": "relay.beta-fixture.net"}]}
UNREGISTERED = "https://gw.unregistered-fixture.net|secret-u"


def offline_resolver(host):
    return dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)


C.DEFAULT_RESOLVER = offline_resolver


def public_getaddrinfo(host, port, **kw):
    """A resolver that puts every name on one public address."""
    return [(2, 1, 6, "", ("93.184.216.34", port))]


C.DEFAULT_GETADDRINFO = public_getaddrinfo

TOKEN_A = "https://chat.alpha-fixture.net|secret-alpha"
TOKEN_B = "https://relay.beta-fixture.net:8443|secret-beta"


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
    def fetch(url, token, address=None):
        return {"agents": [{"id": i, "owner": "@owner:" + i.split(":", 1)[1]} for i in ids]}
    return fetch


def http_error(code):
    def fetch(url, token, address=None):
        raise urllib.error.HTTPError(url, code, "nope", {}, None)
    return fetch


def unreachable(url, token, address=None):
    raise ConnectionRefusedError("down")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.vault = FakeVault()
        self.children = []

    def tearDown(self):
        for proc in self.children:
            bridge_lease.stop(proc)
        self.tmp.cleanup()

    def bridge_spawn(self, note=None):
        """A spawn whose child takes the env's bridge lease through the production writer and holds it."""
        def spawn(argv, env, log_path):
            if note is not None:
                note(argv, env, log_path)
            proc = bridge_lease.start(self.ws, env["GATEWAY_INSTANCE"], wait=False)
            self.children.append(proc)
            return proc
        return spawn

    def child(self, n=-1):
        return self.children[n].pid

    def add(self, token=TOKEN_A, *, fetch=agents("@sutando:alpha-fixture.net"), expected=None, **kw):
        kw.setdefault("added_by", "owner")
        preview = C.preview_connection(token, fetch=fetch)
        return C.add_connection(token, expected_server=expected or preview["server"], preview=preview,
                                workspace=self.ws, vault=self.vault, **kw)

    def rows(self):
        return C.list_connections(self.ws)


class TokenDerivation(unittest.TestCase):
    def test_host_fallback_is_the_lowercased_gateway_host(self):
        self.assertEqual(C.server_of_token(TOKEN_A), "chat.alpha-fixture.net")
        self.assertEqual(C.server_of_token(TOKEN_B), "relay.beta-fixture.net")
        self.assertEqual(C.server_of_token("HTTPS://GW.Alpha-Fixture.NET/|s"), "gw.alpha-fixture.net")

    def test_bare_secret_and_empty_secret_have_no_server(self):
        for token in ("opaque-bearer", "https://gw.alpha-fixture.net|", "gw.alpha-fixture.net|s", ""):
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
        p = C.preview_connection(TOKEN_A, fetch=agents("@sutando:alpha-fixture.net"))
        self.assertEqual(p, {"gateway_url": "https://chat.alpha-fixture.net", "gateway_host": "chat.alpha-fixture.net",
                             "server": "alpha-fixture.net", "server_source": "identity",
                             "agent_mxid": "@sutando:alpha-fixture.net", "verified": True,
                             "agent_display_name": None, "agent_avatar_url": None,
                             "owner_mxid": "@owner:alpha-fixture.net",
                             "authenticated": True, "error": None})

    def test_identity_fields_come_from_the_one_verified_listing_entry_only(self):
        def listing(**extra):
            return lambda u, t, a=None: {"agents": [dict({"id": "@sutando:alpha-fixture.net"}, **extra)]}
        p = C.preview_connection(TOKEN_A, fetch=listing(display_name="  Sutando  ", avatar_url="mxc://alpha-fixture.net/abc",
                                                        owner="@owner:alpha-fixture.net"))
        self.assertEqual((p["agent_display_name"], p["agent_avatar_url"], p["owner_mxid"]),
                         ("Sutando", "mxc://alpha-fixture.net/abc", "@owner:alpha-fixture.net"))
        p = C.preview_connection(TOKEN_A, fetch=listing(display_name="", avatar_url="https://cdn.example/a.png",
                                                        owner="not-an-mxid"))
        self.assertEqual((p["agent_display_name"], p["agent_avatar_url"], p["owner_mxid"]), (None, None, None),
                         "an empty name, a non-mxc avatar and a non-mxid owner are dropped, not stored")
        p = C.preview_connection(TOKEN_A, fetch=listing(display_name=7, avatar_url=["mxc://x/y"], owner={"id": "x"}))
        self.assertEqual((p["agent_display_name"], p["agent_avatar_url"], p["owner_mxid"]), (None, None, None))
        # An unverified listing names no identity at all, whatever its entries carry.
        p = C.preview_connection(TOKEN_A, fetch=lambda u, t, a=None: {"agents": [
            {"id": "@a:alpha-fixture.net", "owner": "@o:alpha-fixture.net"},
            {"id": "@b:alpha-fixture.net", "owner": "@o:alpha-fixture.net"}]})
        self.assertEqual([p[k] for k in C.IDENTITY_FIELDS], [None] * 4)

    def test_ambiguous_or_empty_fleet_leaves_the_registered_server_unverified(self):
        p = C.preview_connection(TOKEN_A, fetch=agents("@a:alpha-fixture.net", "@b:alpha-fixture.net"))
        self.assertEqual((p["server"], p["server_source"], p["verified"], p["authenticated"], p["agent_mxid"]),
                         ("alpha-fixture.net", "resolver", False, True, None))
        p = C.preview_connection(TOKEN_A, fetch=lambda u, t: {"agents": []})
        self.assertEqual((p["server"], p["verified"]), ("alpha-fixture.net", False))

    def test_a_listing_cannot_establish_an_identity_for_an_unregistered_gateway(self):
        p = C.preview_connection("https://evil-fixture.net/relay|s", fetch=agents("@sutando:ag2.space"))
        self.assertEqual((p["server"], p["server_source"], p["verified"], p["agent_mxid"], p["authenticated"]),
                         ("evil-fixture.net", "gateway", False, None, True))

    def test_probe_refuses_addresses_loopback_private_and_single_label_hosts(self):
        for host in ("127.0.0.1:9", "10.0.0.1", "192.168.1.1", "[::1]:8", "localhost", "intranet", "127.1",
                     "0x7f.1", "169.254.1.1"):
            with self.assertRaises(C.InvalidToken, msg=host) as cm:
                C.preview_connection(f"https://{host}/relay|s", fetch=lambda *a: self.fail(f"probed {host}"))
            self.assertEqual(str(cm.exception), "invalid_gateway")
        self.assertTrue(C.gateway_host_ok("chat.s.space"))
        self.assertFalse(C.gateway_host_ok(""))
        with unittest.mock.patch.dict(sys.modules, {"dotspace": None}):
            self.assertTrue(C.gateway_host_ok("chat.s.space"))
            self.assertFalse(C.gateway_host_ok("bad_host.example"))

    def test_probe_requires_https(self):
        with self.assertRaises(C.InvalidToken) as cm:
            C.preview_connection("http://chat.alpha-fixture.net/relay|s", fetch=lambda *a: self.fail("probed"))
        self.assertEqual(str(cm.exception), "invalid_gateway")
        self.assertEqual(C.gateway_url_of("http://chat.alpha-fixture.net/relay|s"), "http://chat.alpha-fixture.net/relay",
                         "env rows may still carry an http gateway; only the probe refuses it")

    def test_probe_refuses_reserved_suffixes_before_any_network(self):
        for host in ("foo.localhost:8443", "printer.local", "api.internal", "gw.test", "x.example", "x.invalid",
                     "abc.onion", "router.home.arpa"):
            with self.assertRaises(C.InvalidToken, msg=host) as cm:
                C.preview_connection(f"https://{host}/relay|s", fetch=lambda *a: self.fail(f"probed {host}"),
                                     getaddrinfo=lambda *a, **k: self.fail(f"resolved {host}"))
            self.assertEqual(str(cm.exception), "invalid_gateway")

    def test_shared_and_special_ranges_and_bad_urls_are_refused_before_any_probe(self):
        """CGNAT 100.64/10 (a tailnet range), 6to4, site-local and their mapped forms are not
        global; a bad port or userinfo in the URL refuses the token before resolution."""
        def resolving_to(*ips):
            def getaddrinfo(host, port, **kw):
                return [(2, 1, 6, "", (ip, port)) for ip in ips]
            return getaddrinfo
        calls = []
        def fetch(url, token, address):
            calls.append(address); return {"agents": []}
        for ips in (("100.64.0.1",), ("::ffff:100.64.0.1",), ("2002:c000:0204::1",), ("fec0::1",),
                    ("2002:0a00:0005::1",), ("2002:7f00:1::1",), ("2002:c0a8:0101::1",), ("2002:6440:0001::1",),
                    ("2001:0:4136:e378:8000:63bf:f5ff:fffa",)):
            with self.assertRaises(C.InvalidToken, msg=ips) as cm:
                C.preview_connection("https://chat.alpha-fixture.net/relay|s", fetch=fetch,
                                     getaddrinfo=resolving_to(*ips))
            self.assertEqual(str(cm.exception), "invalid_gateway")
        resolved = []
        def getaddrinfo(host, port, **kw):
            resolved.append((host, port)); return [(2, 1, 6, "", ("93.184.216.34", port))]
        for url in ("https://chat.alpha-fixture.net:99999/relay", "https://chat.alpha-fixture.net:abc/relay",
                    "https://chat.alpha-fixture.net:0/relay", "https://u:p@chat.alpha-fixture.net/relay",
                    "https://u@chat.alpha-fixture.net/relay"):
            with self.assertRaises(C.InvalidToken, msg=url) as cm:
                C.preview_connection(url + "|s", fetch=fetch, getaddrinfo=getaddrinfo)
            self.assertEqual(str(cm.exception), "invalid_gateway")
        self.assertEqual((resolved, calls), ([], []), "nothing resolved or probed for a refused URL")
        C.preview_connection("https://chat.alpha-fixture.net:8448/relay|s", fetch=fetch, getaddrinfo=getaddrinfo)
        self.assertEqual(resolved, [("chat.alpha-fixture.net", 8448)])

    def test_probe_resolves_once_and_refuses_any_non_public_answer(self):
        def resolving_to(*ips):
            def getaddrinfo(host, port, **kw):
                return [(2, 1, 6, "", (ip, port)) for ip in ips]
            return getaddrinfo
        for ips in (("127.0.0.1",), ("10.0.0.5",), ("fe80::1",), ("::ffff:127.0.0.1",), ("0.0.0.0",),
                    ("224.0.0.1",), ("93.184.216.34", "10.0.0.5")):
            with self.assertRaises(C.InvalidToken, msg=ips) as cm:
                C.preview_connection("https://chat.alpha-fixture.net|s", fetch=lambda *a: self.fail(f"probed {ips}"),
                                     getaddrinfo=resolving_to(*ips))
            self.assertEqual(str(cm.exception), "invalid_gateway")
        seen = {}

        def fetch(url, token, address):
            seen["address"] = address
            return {}
        p = C.preview_connection("https://chat.alpha-fixture.net:8448|s", fetch=fetch,
                                 getaddrinfo=resolving_to("93.184.216.34", "2606:4700::1111"))
        self.assertEqual(seen, {"address": "93.184.216.34"}, "the probe is pinned to the first vetted address")
        self.assertTrue(p["authenticated"])

        def failing(host, port, **kw):
            raise socket.gaierror("no such host")
        p = C.preview_connection("https://chat.alpha-fixture.net|s", fetch=lambda *a: self.fail("probed"),
                                 getaddrinfo=failing)
        self.assertEqual((p["authenticated"], p["error"]), (None, "unreachable (resolution)"))
        self.assertIsNone(C.vetted_address("x.net", 443, getaddrinfo=lambda *a, **k: []))
        self.assertFalse(C._public_address("not-an-ip"))

    def test_fetch_refuses_redirects_over_the_pinned_connection(self):
        import http.server
        import threading

        class Redirecting(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "https://elsewhere-fixture.net/")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
        srv.timeout = 5
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                C._fetch_agents("https://chat.alpha-fixture.net", "https://chat.alpha-fixture.net|s", "127.0.0.1",
                                connect=lambda addr, port, host, timeout: socket.create_connection(
                                    (addr, srv.server_address[1]), timeout=timeout))
        finally:
            t.join(5)
            srv.server_close()
        self.assertEqual(cm.exception.code, 302)

class AddConnection(Base):
    def test_add_writes_row_and_stores_secret_only_in_vault(self):
        row, created = self.add()
        self.assertTrue(created)
        self.assertEqual(row["server"], "alpha-fixture.net")
        self.assertEqual(row["instance"], "alpha-fixture-net")
        self.assertEqual(row["token_ref"], "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET")
        self.assertEqual(row["gateway_url"], "https://chat.alpha-fixture.net")
        self.assertEqual(row["agent_mxid"], "@sutando:alpha-fixture.net")
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
        row, _ = self.add(UNREGISTERED, fetch=unreachable)
        self.assertEqual((row["server"], row["verified"], row.get("agent_mxid"), row["server_source"]),
                         ("gw.unregistered-fixture.net", False, None, "gateway"))
        row, _ = self.add(fetch=unreachable)
        self.assertEqual((row["server"], row["verified"], row["server_source"]), ("alpha-fixture.net", False, "resolver"))

    def test_spoofed_identity_never_shadows_the_real_connection(self):
        evil, created = self.add("https://evil-fixture.net/relay|evil-secret", fetch=agents("@sutando:ag2.space"))
        self.assertTrue(created)
        self.assertEqual((evil["server"], evil["server_source"], evil["verified"]), ("evil-fixture.net", "gateway", False))
        real, created = self.add("https://chat.ag2.space/relay|real-secret", fetch=agents("@sutando:ag2.space"))
        self.assertTrue(created, "the real token is stored, not swallowed by the provisional row")
        self.assertEqual((real["server"], real["server_source"], real["verified"]), ("ag2.space", "identity", True))
        self.assertEqual(self.vault.store[real["token_ref"]], "https://chat.ag2.space/relay|real-secret")
        self.assertEqual([r["server"] for r in self.rows()], ["evil-fixture.net", "ag2.space"])

    def test_a_provisional_row_matches_only_its_own_gateway_host(self):
        first, _ = self.add(UNREGISTERED, fetch=unreachable)
        other, created = self.add("https://other.unregistered-fixture.net|s", fetch=unreachable)
        self.assertTrue(created)
        self.assertNotEqual(other["instance"], first["instance"])
        again, created = self.add("https://gw.unregistered-fixture.net/v2|s2", fetch=unreachable)
        self.assertFalse(created)
        self.assertEqual(again["instance"], first["instance"])

    def test_a_rejected_token_is_never_stored(self):
        with self.assertRaises(C.TokenRejected):
            self.add(fetch=http_error(401))
        self.assertEqual((self.rows(), self.vault.store), ([], {}))
        row, created = self.add(fetch=unreachable)
        self.assertTrue(created, "an unreachable gateway is stored as a pending, unverified row")
        self.assertFalse(row["verified"])
        self.assertEqual(C.status_for(row, self.ws)["state"], "pending")

    def test_instance_must_be_a_string_and_a_foreign_vault_key_is_never_overwritten(self):
        for bad in (["x"], 5):
            with self.assertRaises(C.ConnectionsError):
                self.add(instance=bad)
        self.vault.store["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"] = "someone-else's secret"
        with self.assertRaises(C.VaultKeyInUse):
            self.add()
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"], "someone-else's secret")
        self.assertEqual(self.rows(), [])

    def test_two_endpoint_tokens_of_one_homeserver_make_one_row(self):
        first, created = self.add("https://chat.s.space/relay|s1", fetch=unreachable)
        second, created2 = self.add("https://matrix.s.space/relay|s2", fetch=unreachable)
        self.assertEqual((created, created2), (True, False))
        self.assertEqual(second, first)
        self.assertEqual((first["server"], first["server_source"], first["instance"]), ("s.space", "resolver", "s-space"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(C.launch_plan({}, workspace=self.ws)), 1)

    def test_same_gateway_host_matches_without_a_resolver(self):
        first, _ = self.add("https://chat.alpha-fixture.net/relay|s1", fetch=unreachable)
        again, created = self.add("https://chat.alpha-fixture.net/other|s2", fetch=unreachable)
        self.assertFalse(created)
        self.assertEqual(again["instance"], first["instance"])
        self.assertEqual(len(self.rows()), 1)

    def test_verified_add_rekeys_a_provisional_row_in_place(self):
        first, _ = self.add(fetch=unreachable)
        self.assertEqual((first["server"], first["server_source"]), ("alpha-fixture.net", "resolver"))
        again, created = self.add("https://chat.alpha-fixture.net|rotated", fetch=agents("@sutando:alpha-fixture.net"))
        self.assertFalse(created)
        self.assertEqual((again["server"], again["server_source"], again["verified"], again["agent_mxid"]),
                         ("alpha-fixture.net", "identity", True, "@sutando:alpha-fixture.net"))
        self.assertEqual(again["instance"], first["instance"], "the slug is stable")
        self.assertEqual(again["token_ref"], first["token_ref"])
        self.assertEqual(self.vault.store[first["token_ref"]], TOKEN_A, "the stored token is untouched")
        self.assertEqual(self.rows(), [again])
        self.assertEqual(C.status_for(again, self.ws)["server_source"], "identity")
        # Once verified, a later unverified or differing probe never demotes it.
        later, _ = self.add("https://chat.alpha-fixture.net|x", fetch=unreachable)
        self.assertEqual((later["server"], later["server_source"]), ("alpha-fixture.net", "identity"))

    def test_expected_server_must_match_what_the_gateway_says_now(self):
        with self.assertRaises(C.ConnectionMismatch) as cm:
            self.add(expected="chat.alpha-fixture.net")
        self.assertIn("alpha-fixture.net", str(cm.exception))
        self.assertIn("chat.alpha-fixture.net", str(cm.exception))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.vault.store, {})
        with self.assertRaises(C.ConnectionMismatch):
            C.add_connection(TOKEN_A, added_by="owner", expected_server="", fetch=unreachable,
                             workspace=self.ws, vault=self.vault)
        row, _ = self.add(expected="ALPHA-fixture.net")
        self.assertEqual(row["server"], "alpha-fixture.net")

    def test_add_is_idempotent_per_server_and_never_moves_primary(self):
        first, created = self.add()
        again, created2 = self.add("https://matrix.alpha-fixture.net|other-secret")
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(again, first)
        self.assertEqual(self.vault.store, {first["token_ref"]: TOKEN_A})
        second, created3 = self.add(TOKEN_B, fetch=agents("@sutando:beta-fixture.net"))
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
            C.add_connection(TOKEN_A, added_by="owner", expected_server="alpha-fixture.net",
                             fetch=unreachable, workspace=self.ws, vault=Broken())

    def test_reserved_instance_names_are_refused(self):
        """`known-servers` and `token` are fixed names on the HTTP resource: a row so named could
        never be removed or updated there."""
        for name in C.RESERVED_INSTANCES:
            with self.assertRaises(C.ConnectionsError, msg=name) as cm:
                self.add(instance=name)
            self.assertIn("reserved", str(cm.exception))
            self.assertNotIsInstance(cm.exception, C.Refusal, "a bad request, not a state refusal")
        self.assertEqual((self.rows(), self.vault.store), ([], {}), "nothing written")
        self.assertEqual(self.add(instance="known-servers-2")[0]["instance"], "known-servers-2")

    def test_instance_collision_and_bad_inputs(self):
        self.add(instance="shared")
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), instance="shared")
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), added_by="robot")
        with self.assertRaises(C.ConnectionsError):
            self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), instance="has space")
        self.assertEqual(len(self.rows()), 1)


class Primary(Base):
    def test_set_primary_moves_exactly_one_flag(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        row = C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertTrue(row["primary"])
        self.assertEqual([(r["instance"], r["primary"]) for r in self.rows()],
                         [("alpha-fixture-net", False), ("beta-fixture-net", True)])
        with self.assertRaises(C.UnknownConnection):
            C.set_primary("nope", workspace=self.ws)

    def test_primary_instance_names_the_row_the_legacy_launch_defers_to(self):
        """None with no record or no primary row; '' when the legacy row is primary; the slug of a
        named primary, which is what makes the legacy bridge launch without the proactive room."""
        self.assertIsNone(C.primary_instance(self.ws))
        self.add()
        self.assertEqual(C.primary_instance(self.ws), "alpha-fixture-net")
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertEqual(C.primary_instance(self.ws), "beta-fixture-net")
        C.remove_connection("beta-fixture-net", force=True, workspace=self.ws, vault=self.vault, kill=lambda *a, **k: None)
        self.assertIsNone(C.primary_instance(self.ws), "no row inherits the primary flag")

    def test_primary_in_the_plan_keeps_the_proactive_room(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        plan = C.launch_plan({}, workspace=self.ws)
        self.assertEqual([(e["instance"], e["primary"]) for e in plan],
                         [("alpha-fixture-net", True), ("beta-fixture-net", False)])

    def test_handoff_the_new_primary_says_it_needs_a_relaunch_and_its_reconnect_carries_the_room(self):
        """set_primary moves the flag only; a running bridge keeps the environment it was launched
        with. The row says so (`relaunch_needed`), and the documented order is promote, reconnect
        the new primary (the launch now carries REMOTE_PROACTIVE_ROOM), then disconnect the old."""
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertFalse(C.set_primary("alpha-fixture-net", workspace=self.ws)["relaunch_needed"], "already primary")
        beta = bridge_lease.start(self.ws, "beta-fixture-net")
        spawned = []
        spawn = self.bridge_spawn(lambda argv, env, log_path: spawned.append((env["GATEWAY_INSTANCE"],
                                                                             env["REMOTE_PROACTIVE_ROOM"])))
        environ = {"REMOTE_PROACTIVE_ROOM": "!nudges:beta-fixture.net"}
        try:
            out = C.set_primary("beta-fixture-net", workspace=self.ws)
            self.assertEqual((out["primary"], out["relaunch_needed"]), (True, True), "beta's bridge runs without the room")
            self.assertFalse(C.set_primary("beta-fixture-net", workspace=self.ws)["relaunch_needed"], "no second promotion")
            self.assertNotIn("relaunch_needed", self.rows()[1], "a hint for the caller, not a row field")
            record = C.reconnect("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ=environ,
                                 workspace=self.ws, vault=self.vault, spawn=spawn, fetch=unreachable)
        finally:
            bridge_lease.stop(beta)
        self.assertEqual((record["stopped_pid"], spawned), (beta.pid, [("beta-fixture-net", "!nudges:beta-fixture.net")]),
                         "the new primary's relaunch carries the room")
        alpha = C.plan_entry("alpha-fixture-net", environ, workspace=self.ws)
        self.assertFalse(alpha["primary"])
        self.assertEqual(C.launch_env(alpha, environ, vault=self.vault)["REMOTE_PROACTIVE_ROOM"], "",
                         "the old primary's next launch has no room")
        self.assertTrue(C.disconnect("alpha-fixture-net", workspace=self.ws, kill=lambda *a: None)["disabled"])


class RemoveConnection(Base):
    def test_remove_stops_the_bridge_holding_the_lock(self):
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        killed = []

        def kill(pid, sig):
            killed.append(pid)
            os.kill(pid, sig)
        try:
            gone = C.remove_connection("beta-fixture-net", force=True, workspace=self.ws, vault=self.vault, kill=kill)
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual((killed, gone["stopped_pid"]), ([bridge.pid], bridge.pid))

    def test_removed_env_row_is_not_imported_again(self):
        env = {"AG2_REMOTE_TOKEN_DEV": TOKEN_B}
        C.import_from_env(env, workspace=self.ws, vault=self.vault)
        C.remove_connection("dev", workspace=self.ws, vault=self.vault)
        self.assertEqual(C.removed_env_refs(self.ws), ["AG2_REMOTE_TOKEN_DEV"])
        self.assertEqual(C.import_from_env(env, workspace=self.ws, vault=self.vault), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(C.launch_plan(env, workspace=self.ws), [], "a removed env token is not relaunched either")
        row, _ = self.add(TOKEN_B, fetch=unreachable, instance="dev")
        self.assertEqual(row["token_ref"], "AG2_REMOTE_TOKEN_DEV")
        self.assertEqual(C.removed_env_refs(self.ws), [], "an explicit add clears the tombstone")

    def test_remove_drops_row_vault_key_and_launch_record_only_for_that_instance(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        for inst in ("alpha-fixture-net", "beta-fixture-net"):
            C._record_launch(inst, self.ws, ok=True, pid=1)
        gone = C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault)
        self.assertEqual(gone["instance"], "beta-fixture-net")
        self.assertEqual([r["instance"] for r in self.rows()], ["alpha-fixture-net"])
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"})
        self.assertFalse(C.launch_record_path("beta-fixture-net", self.ws).exists())
        self.assertTrue(C.launch_record_path("alpha-fixture-net", self.ws).exists())
        self.assertTrue(self.rows()[0]["primary"])
        self.assertEqual(sum(r["primary"] for r in self.rows()), 1)

    def test_primary_needs_force_and_no_row_inherits_the_flag(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        with self.assertRaises(C.PrimaryProtected):
            C.remove_connection("alpha-fixture-net", workspace=self.ws, vault=self.vault)
        self.assertEqual(len(self.rows()), 2)
        C.remove_connection("alpha-fixture-net", force=True, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["primary"]) for r in self.rows()], [("beta-fixture-net", False)])
        row, _ = self.add()
        self.assertTrue(row["primary"], "an install with no primary gives it to the next row saved")

    def test_unknown_instance(self):
        with self.assertRaises(C.UnknownConnection):
            C.remove_connection("nope", workspace=self.ws, vault=self.vault)

    def test_vault_delete_failure_keeps_the_row(self):
        self.add()
        self.vault.fail_delete = True
        with self.assertRaises(C.VaultUnavailable):
            C.remove_connection("alpha-fixture-net", force=True, workspace=self.ws, vault=self.vault)
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
        self.assertEqual((created[1]["server"], created[1]["server_source"]), ("beta-fixture.net", "resolver"))
        self.assertEqual(self.vault.store, {"REMOTE_TASK_TOKEN": TOKEN_A, "AG2_REMOTE_TOKEN_DEV": TOKEN_B})
        self.assertEqual(C.import_from_env(self.ENV, workspace=self.ws, vault=self.vault), [])
        self.assertEqual(len(self.rows()), 2)

    def test_import_never_overwrites_an_existing_row_or_vault_value(self):
        self.vault.store["AG2_REMOTE_TOKEN_DEV"] = "already-there"
        C.import_from_env({"AG2_REMOTE_TOKEN_DEV": TOKEN_B}, workspace=self.ws, vault=self.vault)
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_DEV"], "already-there")
        before = self.rows()[0]
        C.import_from_env({"AG2_REMOTE_TOKEN_DEV": "https://elsewhere-fixture.net|zzz"},
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
        row, _ = self.add("https://gamma-fixture.net|g", fetch=unreachable)
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


class ChannelFileBase(Base):
    """A Claude home of its own, so the channel env file the resolver picks is this test's."""

    def setUp(self):
        super().setUp()
        self.home = self.ws / "claude-home"
        self.channel = self.home / "channels" / "ag2space"
        self.channel.mkdir(parents=True)
        patcher = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_relay_env(self, line: str) -> Path:
        path = self.channel / "relay-client.env"
        path.write_text(f"# written by the connect flow\n{line}\n")
        return path


class ImportFromChannelFile(ChannelFileBase):
    def test_file_token_creates_the_primary_row_when_the_environment_has_none(self):
        self.write_relay_env(f'REMOTE_TASK_TOKEN="{TOKEN_A}"')
        created = C.import_from_env({}, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["token_ref"], r["primary"], r["added_by"], r["verified"], r["gateway_url"])
                          for r in created],
                         [("", "REMOTE_TASK_TOKEN", True, "env", False, "https://chat.alpha-fixture.net")])
        self.assertEqual(self.vault.store, {"REMOTE_TASK_TOKEN": TOKEN_A}, "the quotes are peeled, as from the env")
        self.assertEqual(C.import_from_env({}, workspace=self.ws, vault=self.vault), [], "once")
        self.assertEqual(len(self.rows()), 1)

    def test_legacy_spelling_in_the_file_counts_too(self):
        self.write_relay_env(f"AG2_REMOTE_TOKEN={TOKEN_B}")
        created = C.import_from_env({}, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["token_ref"], r["server"]) for r in created],
                         [("", "REMOTE_TASK_TOKEN", "beta-fixture.net")])

    def test_environment_wins_over_the_file(self):
        self.write_relay_env(f"REMOTE_TASK_TOKEN={TOKEN_B}")
        created = C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["gateway_url"]) for r in created], [("", "https://chat.alpha-fixture.net")])
        self.assertEqual(self.vault.store, {"REMOTE_TASK_TOKEN": TOKEN_A})

    def test_neither_source_writes_nothing_and_raises_nothing(self):
        self.assertEqual(C.import_from_env({}, workspace=self.ws, vault=self.vault), [])
        self.write_relay_env("REMOTE_TASK_TOKEN=")
        self.assertEqual(C.import_from_env({}, workspace=self.ws, vault=self.vault), [], "a blank value is no token")
        self.assertFalse(C.record_path(self.ws).exists())

    def test_a_removed_name_in_the_file_stays_skipped(self):
        self.write_relay_env(f"REMOTE_TASK_TOKEN={TOKEN_A}")
        C.import_from_env({}, workspace=self.ws, vault=self.vault)
        C.remove_connection("", force=True, workspace=self.ws, vault=self.vault)
        self.assertEqual(C.removed_env_refs(self.ws), ["REMOTE_TASK_TOKEN"])
        self.assertEqual(C.import_from_env({}, workspace=self.ws, vault=self.vault), [])
        self.assertEqual(self.rows(), [])

    def test_no_channel_home_is_no_token(self):
        self.assertEqual(C.primary_token_from_channel_file({}), "")
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.ws / "nowhere")}):
            self.assertEqual(C.primary_token_from_channel_file({}), "")

    def test_a_resolver_error_is_no_token_and_the_import_still_answers(self):
        self.write_relay_env(f"REMOTE_TASK_TOKEN={TOKEN_A}")
        import channel_credentials
        with unittest.mock.patch.object(channel_credentials, "channel_credentials",
                                        side_effect=PermissionError("channels dir unreadable")):
            self.assertEqual(C.primary_token_from_channel_file({}), "")
            self.assertEqual(C.import_from_env({}, workspace=self.ws, vault=self.vault), [])
        self.assertFalse(C.record_path(self.ws).exists())


class AddAdoptsLaunchKeys(ChannelFileBase):
    """A launch key the desktop host wrote but no row holds: an add of its server must adopt it under
    the instance the host's launcher runs (the key's own suffix), never start a second bridge."""

    def setUp(self):
        super().setUp()
        self.spawned = []
        self.spawn = self.bridge_spawn(lambda argv, env, log_path: self.spawned.append(env["GATEWAY_INSTANCE"]))

    def test_an_unimported_launch_key_in_the_channel_file_is_adopted(self):
        (self.channel / ".env").write_text(f"AG2_REMOTE_TOKEN='{TOKEN_A}'\nAG2_REMOTE_TOKEN_BETA_FIXTURE_NET='{TOKEN_B}'\n")
        row, created = self.add("https://relay.beta-fixture.net:8443|secret-panel", fetch=agents("@s:beta-fixture.net"))
        self.assertFalse(created, "the launch key's row is the connection")
        self.assertEqual((row["instance"], row["token_ref"]), ("beta_fixture_net", "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the launcher's token is kept")
        self.assertEqual([r["instance"] for r in self.rows()], ["", "beta_fixture_net"], "one row per server")
        C.start_added(row, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                      vault=self.vault, spawn=self.spawn)
        desktop_lane = bridge_lease.start(self.ws, "beta_fixture_net", wait=False)
        self.children.append(desktop_lane)
        self.assertEqual(desktop_lane.wait(timeout=10), 75, "the desktop lane's bridge stands down to the same lease")
        self.assertEqual(self.spawned, ["beta_fixture_net"])

    def test_a_key_that_appears_after_a_panel_row_becomes_its_alias(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        alias_key = "AG2_REMOTE_TOKEN_BETA_DFIXTURE_DNET"
        C.import_from_env({alias_key: "https://relay.beta-fixture.net:8443|desktop"}, workspace=self.ws, vault=self.vault)
        rows = self.rows()
        self.assertEqual([(r["instance"], r.get("launch_keys")) for r in rows], [("beta-fixture-net", [alias_key])],
                         "one row, the key recorded as its alias, no second row")
        self.assertEqual([e["instance"] for e in C.launch_plan({alias_key: "x|y"}, workspace=self.ws)], ["beta-fixture-net"],
                         "the plan never launches the alias as a lane of its own")
        desktop = bridge_lease.start(self.ws, "beta_dfixture_dnet")
        self.children.append(desktop)
        kw = dict(repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws, vault=self.vault,
                  spawn=self.spawn)
        self.assertEqual(C.start("beta-fixture-net", **kw)["as"], "beta_dfixture_dnet")
        self.assertEqual(C.launch("beta-fixture-net", **kw)["lease"], "alias_running")
        self.assertEqual(self.spawned, [], "no second bridge beside the live alias")
        self.assertTrue(C.status_for(self.rows()[0], self.ws)["running"])
        self.assertEqual(C.bridge_pid("beta_dfixture_dnet", self.ws), desktop.pid, "the live lease was not moved")
        C.remove_connection("beta-fixture-net", force=True, workspace=self.ws, vault=self.vault)
        self.assertEqual(desktop.wait(timeout=10), 0, "removing it stops the bridge under its alias name too")

    def test_stop_row_stops_every_name_and_returns_the_first_pid(self):
        row = {"instance": "beta-fixture-net", "launch_keys": ["AG2_REMOTE_TOKEN_ONE", "AG2_REMOTE_TOKEN_TWO"]}
        pids = {"beta-fixture-net": 0, "one": 11, "two": 22}
        called = []
        def fake_stop(inst, **_kw):
            called.append(inst)
            return pids[inst]
        with unittest.mock.patch.object(C, "stop_bridge", fake_stop):
            self.assertEqual(C._stop_row(row, self.ws, None), 11)
        self.assertEqual(called, ["beta-fixture-net", "one", "two"])

    def test_disconnect_and_reconnect_reach_the_alias_bridge(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C.import_from_env({"AG2_REMOTE_TOKEN_BETA_DFIXTURE_DNET": "https://relay.beta-fixture.net:8443|d"},
                          workspace=self.ws, vault=self.vault)
        desktop = bridge_lease.start(self.ws, "beta_dfixture_dnet")
        self.children.append(desktop)
        out = C.reconnect("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                          workspace=self.ws, vault=self.vault, spawn=self.spawn, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual((out["stopped_pid"], desktop.wait(timeout=10)), (desktop.pid, 0), "reconnect replaces the alias bridge")
        self.assertEqual(self.spawned, ["beta-fixture-net"], "and starts the one canonical name")
        C.disconnect("beta-fixture-net", workspace=self.ws)
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0)

    def test_a_running_launcher_bridge_keeps_its_lease_through_an_add(self):
        desktop = bridge_lease.start(self.ws, "beta_fixture_net")
        self.children.append(desktop)
        with unittest.mock.patch.dict(os.environ, {"AG2_REMOTE_TOKEN_BETA_FIXTURE_NET": TOKEN_B}):
            row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        record = C.start_added(row, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                               vault=self.vault, spawn=self.spawn)
        self.assertEqual((record["lease"], record["pid"], self.spawned), ("already_running", desktop.pid, []))
        self.assertEqual(C.bridge_pid("beta_fixture_net", self.ws), desktop.pid)

    def test_launch_keys_must_be_launch_key_names(self):
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        rec = json.loads(C.record_path(self.ws).read_text())
        rec["connections"][0]["launch_keys"] = ["PATH"]
        C.record_path(self.ws).write_text(json.dumps(rec))
        with self.assertRaises(C.SchemaError):
            C.list_connections(self.ws)

    def test_an_environment_launch_key_is_adopted_too(self):
        with unittest.mock.patch.dict(os.environ, {"AG2_REMOTE_TOKEN_BETA_FIXTURE_NET": TOKEN_B}):
            row, created = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual((created, row["instance"]), (False, "beta_fixture_net"))

    def test_an_unreadable_channel_file_is_no_keys(self):
        import channel_env_resolve
        with unittest.mock.patch.object(channel_env_resolve, "resolve_channel_env", side_effect=OSError("denied")):
            self.assertEqual(C.channel_launch_keys({"AG2_REMOTE_TOKEN_QA": " https://qa|q ", "AG2_REMOTE_TOKEN_NIL": ""}),
                             {"AG2_REMOTE_TOKEN_QA": "https://qa|q"})

    def test_without_launch_keys_the_server_slug_is_kept(self):
        row, created = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual((created, row["instance"]), (True, "beta-fixture-net"))


class ListSelfHeals(ChannelFileBase):
    def test_an_explicitly_emptied_record_is_not_refilled_from_the_file(self):
        self.write_relay_env(f"REMOTE_TASK_TOKEN={TOKEN_A}")
        self.assertEqual(len(C.list_connections(self.ws, vault=self.vault)), 1)
        C.remove_connection("", force=True, workspace=self.ws, vault=self.vault)
        self.assertTrue(C.record_path(self.ws).exists(), "removal leaves an initialized, empty record")
        self.assertEqual(C.list_connections(self.ws, vault=self.vault), [],
                         "a removed row must not come back while its token still sits in the file")
        self.assertEqual(C.list_connections(self.ws, vault=self.vault), [], "and stays gone on the next list")

    def test_an_empty_record_imports_the_file_token_and_persists_it(self):
        self.write_relay_env(f"REMOTE_TASK_TOKEN={TOKEN_A}")
        rows = C.list_connections(self.ws, vault=self.vault)
        self.assertEqual([(r["instance"], r["token_ref"], r["primary"]) for r in rows], [("", "REMOTE_TASK_TOKEN", True)])
        self.assertTrue(C.record_path(self.ws).exists(), "persisted, not answered from the file")
        self.assertEqual(C._read(C.record_path(self.ws)), rows)
        self.assertEqual(self.vault.store, {"REMOTE_TASK_TOKEN": TOKEN_A})

    def test_an_empty_record_with_nothing_to_import_lists_empty(self):
        self.assertEqual(C.list_connections(self.ws, vault=self.vault), [])
        self.assertFalse(C.record_path(self.ws).exists())

    def test_a_record_with_rows_never_imports(self):
        self.add()
        self.write_relay_env(f"REMOTE_TASK_TOKEN={TOKEN_B}")
        with unittest.mock.patch.object(C, "import_from_env") as imported:
            rows = C.list_connections(self.ws, vault=self.vault)
        imported.assert_not_called()
        self.assertEqual([r["instance"] for r in rows], ["alpha-fixture-net"])

    def test_a_failing_import_is_logged_and_the_list_still_answers(self):
        with unittest.mock.patch.object(C, "import_from_env", side_effect=RuntimeError("keychain locked")):
            with self.assertLogs("connections", level="WARNING") as logs:
                self.assertEqual(C.list_connections(self.ws, vault=self.vault), [])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("keychain locked", logs.output[0], "the cause is named by type, never by message")

    def test_a_broken_record_still_raises(self):
        C.record_path(self.ws).parent.mkdir(parents=True)
        C.record_path(self.ws).write_text("{broken")
        with self.assertRaises(C.SchemaError):
            C.list_connections(self.ws, vault=self.vault)


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
            (json.dumps({"schema_version": 1, "connections": [], "removed_env": "x"}), "removed_env"),
            (json.dumps({"schema_version": 1, "connections": [row(disabled="yes")]}), "disabled is not a boolean"),
            (json.dumps({"schema_version": 1, "connections": [row(agent_display_name=1)]}), "agent_display_name"),
            (json.dumps({"schema_version": 1, "connections": [row(owner_mxid=["@o:a"])]}), "owner_mxid"),
            (json.dumps({"schema_version": 1, "connections": [row(agent_avatar_url="https://cdn.example/a")]}),
             "agent_avatar_url is not an mxc://"),
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
        path = C.record_path(self.ws)
        before = path.read_bytes()
        calls = []
        real_replace = os.replace

        def replace(src, dst):
            calls.append((Path(src), Path(dst), Path(src).read_text()))
            raise OSError("publish interrupted")
        with unittest.mock.patch.object(C.os, "replace", replace), self.assertRaises(OSError):
            self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual(len(calls), 1, "the record is published by exactly one os.replace")
        tmp, dst, body = calls[0]
        self.assertEqual((dst, tmp.parent), (path, path.parent), "the temp file sits beside the record")
        self.assertIn("beta-fixture-net", body, "the whole new record was in the temp file before the swap")
        self.assertEqual(path.read_bytes(), before, "a failed publish leaves the record as it was")
        names = sorted(p.name for p in path.parent.iterdir())
        self.assertEqual(names, sorted([C.LOCK_NAME, C.STATE_NAME]), "no temp file is left behind")
        self.assertIs(os.replace, real_replace)
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    def test_concurrent_writers_serialize_through_the_lock(self):
        errors = []

        def worker(i):
            try:
                C.add_connection(f"https://gw{i}-fixture.net|s{i}", added_by="ui", expected_server=f"gw{i}-fixture.net",
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
            self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), instance="shared")
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
        self.assertEqual(C.derive_state({"connected": True}, {"ok": True}, True), ("parked", "disabled"),
                         "a disabled row is parked whatever a stale sidecar says")
        self.assertEqual(C.derive_state({}, {"ok": False, "reason": "no_token"}, True), ("parked", "disabled"))

    def test_status_joins_the_instance_suffixed_file_and_launch_record(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        dev, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), instance="dev")
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
        self.spawn = self.bridge_spawn(lambda argv, env, log_path: self.spawned.append((argv, env, log_path)))
        self.repo = self.ws / "repo"
        self.logs = self.ws / "logs"

    def test_plan_lists_rows_with_an_instance_then_unrecorded_env_vars(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), instance="dev")
        env = {"AG2_REMOTE_TOKEN_DEV": TOKEN_B, "AG2_REMOTE_TOKEN_QA": "https://qa-fixture.net|q",
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
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        entry = C.plan_entry("beta-fixture-net", {}, workspace=self.ws)
        base = {"REMOTE_PROACTIVE_ROOM": "!room:primary", "REMOTE_TASK_TIER": "owner", "PATH": "/usr/bin"}
        env = C.launch_env(entry, base, vault=self.vault)
        self.assertEqual({k: env[k] for k in ("GATEWAY_INSTANCE", "REMOTE_TASK_TOKEN", "REMOTE_TASK_URL",
                                                "REMOTE_TASK_CHANNEL_DIR", "REMOTE_PROACTIVE_ROOM",
                                                "SUTANDO_SUPERVISED", "REMOTE_TASK_TIER")},
                         {"GATEWAY_INSTANCE": "beta-fixture-net", "REMOTE_TASK_TOKEN": TOKEN_B, "REMOTE_TASK_URL": "",
                          "REMOTE_TASK_CHANNEL_DIR": "beta-fixture-net-ag2space", "REMOTE_PROACTIVE_ROOM": "!room:primary",
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
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        record = C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                          vault=self.vault, spawn=self.spawn, python="py3")
        argv, env, log_path = self.spawned[0]
        self.assertEqual(argv, ["py3", str(self.repo / "src" / "remote-gateway-bridge.py")])
        self.assertEqual(env["GATEWAY_INSTANCE"], "beta-fixture-net")
        self.assertEqual(log_path, self.logs / "remote-gateway-bridge.beta-fixture-net.log")
        self.assertEqual((record["ok"], record["pid"], record["lease"]), (True, self.child(), "held"))
        self.assertEqual(json.loads(C.launch_record_path("beta-fixture-net", self.ws).read_text())["pid"], self.child())
        self.assertEqual(C.status_for(self.rows()[0], self.ws)["launched_at"], record["ts"])

    def test_launch_failures_are_recorded_as_failed_with_a_reason(self):
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.vault.store.clear()
        with self.assertRaises(C.LaunchFailed):
            C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                     vault=self.vault, spawn=self.spawn)
        self.assertEqual(C.status_for(self.rows()[0], self.ws)["reason"], "no_token")
        self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"] = TOKEN_B

        def boom(argv, env, log_path):
            raise OSError("no interpreter")
        with self.assertRaises(C.LaunchFailed) as cm:
            C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                     vault=self.vault, spawn=boom)
        self.assertNotIn("secret-beta", str(cm.exception))
        self.assertEqual(C.status_for(self.rows()[0], self.ws)["reason"], "launch_failed")

    def test_reconnect_stops_the_lock_holder_then_relaunches(self):
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0, "no lease names no bridge")
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            os.kill(pid, sig)
        try:
            self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), bridge.pid)
            record = C.reconnect("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={},
                                 workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=kill, fetch=unreachable)
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual(killed, [(bridge.pid, 15)])
        self.assertEqual((record["stopped_pid"], record["pid"]), (bridge.pid, self.child()))
        self.assertEqual(len(self.spawned), 1)

    def test_reconnect_learns_the_identity_and_rekeys(self):
        row, _ = self.add(TOKEN_B, fetch=unreachable)
        self.assertEqual(row["server_source"], "resolver")
        record = C.reconnect("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=lambda *a: None,
                             fetch=agents("@sutando:beta-fixture.net"))
        self.assertEqual(record["pid"], self.child())
        after = self.rows()[0]
        self.assertEqual((after["server"], after["server_source"], after["instance"], after["agent_mxid"]),
                         ("beta-fixture.net", "identity", "beta-fixture-net", "@sutando:beta-fixture.net"))
        self.assertEqual(self.spawned[0][1]["GATEWAY_INSTANCE"], "beta-fixture-net")
        # A probe that fails leaves the row as it was and still launches.
        C.reconnect("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                    vault=self.vault, spawn=self.spawn, fetch=unreachable)
        self.assertEqual(self.rows()[0]["server_source"], "identity")
        self.assertEqual(len(self.spawned), 2)

    def test_unreadable_record_fails_the_plan_loudly_and_env_only_still_launches(self):
        C.record_path(self.ws).parent.mkdir(parents=True)
        C.record_path(self.ws).write_text('{"schema_version": 1, "connections": [{"tru')
        with self.assertRaises(C.SchemaError) as cm:
            C.launch_plan({"AG2_REMOTE_TOKEN_DEV": TOKEN_B}, workspace=self.ws)
        self.assertIn("not valid JSON", str(cm.exception))
        with self.assertRaises(C.SchemaError):
            C.launch("dev", repo=self.repo, logs_dir=self.logs, environ={"AG2_REMOTE_TOKEN_DEV": TOKEN_B},
                     workspace=self.ws, vault=self.vault, spawn=self.spawn)
        record = C.launch("dev", repo=self.repo, logs_dir=self.logs, environ={"AG2_REMOTE_TOKEN_DEV": TOKEN_B},
                          workspace=self.ws, vault=self.vault, spawn=self.spawn, env_only=True)
        self.assertEqual(record["pid"], self.child())
        env = self.spawned[0][1]
        self.assertEqual((env["GATEWAY_INSTANCE"], env["REMOTE_TASK_TOKEN"], env["REMOTE_PROACTIVE_ROOM"]),
                         ("dev", TOKEN_B, ""))
        with self.assertRaises(C.UnknownConnection):
            C.plan_entry("nope", {"AG2_REMOTE_TOKEN_DEV": TOKEN_B}, workspace=self.ws, env_only=True)

    def test_launch_holds_the_record_lock_from_the_plan_read_to_the_spawn(self):
        """A disconnect that starts between the plan read and the spawn waits for the spawn, then
        stops the bridge it launched; a bridge never comes up against a row that says it is down."""
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.add()  # alpha becomes the second row so beta can be disconnected
        C.set_primary("alpha-fixture-net", workspace=self.ws)
        order = []
        disconnected = threading.Event()

        def spawn(argv, env, log_path):
            from file_lock import try_lock_fd, unlock_fd
            fd = os.open(str(C._lock_path(self.ws)), os.O_RDWR)
            try:
                held = not try_lock_fd(fd)
                if not held:
                    unlock_fd(fd)
            finally:
                os.close(fd)
            order.append(("spawn", held))
            t = threading.Thread(target=lambda: (C.disconnect("beta-fixture-net", workspace=self.ws),
                                                 order.append("disconnected"), disconnected.set()))
            t.start()
            time.sleep(0.4)
            order.append(("during spawn, disabled", self.rows()[0].get("disabled")))
            return self.bridge_spawn()(argv, env, log_path)
        record = C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                          vault=self.vault, spawn=spawn)
        self.assertTrue(disconnected.wait(5))
        self.assertEqual(order, [("spawn", True), ("during spawn, disabled", False), "disconnected"],
                         "the lock is held across the spawn; the disconnect commits only after it")
        self.assertTrue(record["ok"])
        self.assertTrue(self.rows()[0]["disabled"])

    def test_the_child_takes_its_lease_while_launch_holds_the_record_lock(self):
        """launch holds connections.lock until the lease: a child that needed it first would stall."""
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        script = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import workspace_lock as wl\n"
            "from file_lock import try_lock_fd\n"
            "fd = os.open(sys.argv[3], os.O_RDWR)\n"
            "print('record-lock-free' if try_lock_fd(fd) else 'record-lock-held', flush=True)\n"
            "os.close(fd)\n"
            "r = wl.acquire(sys.argv[4], Path(sys.argv[2]))\n"
            "print(r.status, flush=True)\n"
            "time.sleep(30)\n")
        started = []

        def spawn(argv, env, log_path):
            proc = subprocess.Popen([sys.executable, "-c", script, str(ROOT / "src"), str(self.ws),
                                     str(C._lock_path(self.ws)), C._bridge_role(env["GATEWAY_INSTANCE"])],
                                    stdout=subprocess.PIPE, env=isolated_child_env(), text=True)
            started.append(proc)
            return proc
        try:
            t0 = time.monotonic()
            record = C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={},
                              workspace=self.ws, vault=self.vault, spawn=spawn)
            elapsed = time.monotonic() - t0
            self.assertEqual(started[0].stdout.readline().strip(), "record-lock-held")
            self.assertNotEqual(started[0].stdout.readline().strip(), "deferred")
        finally:
            for proc in started:
                bridge_lease.stop(proc)
        self.assertEqual((record["ok"], record["lease"]), (True, "held"))
        self.assertLess(elapsed, C.LEASE_TIMEOUT_S / 2, "the lease came without waiting on the record lock")

    def test_reconnect_without_a_running_bridge_just_launches(self):
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        record = C.reconnect("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.spawn,
                             kill=lambda *a: self.fail("nothing to kill"), fetch=unreachable)
        self.assertEqual(record["stopped_pid"], 0)


class Starting(Base):
    """`start` / `start_added`: launch now unless the lease is already held; a failure is a record."""

    def setUp(self):
        super().setUp()
        self.envs = []
        self.spawn = self.bridge_spawn(lambda argv, env, log_path: self.envs.append(env))
        self.kw = dict(repo=self.ws / "repo", logs_dir=self.ws / "logs", workspace=self.ws, vault=self.vault)

    def test_an_added_row_starts_and_reads_running(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        record = C.start_added(row, environ={}, spawn=self.spawn, **self.kw)
        self.assertEqual((record["ok"], record["lease"], record["pid"]), (True, "held", self.child()))
        status = C.status_for(self.rows()[0], self.ws)
        self.assertEqual((status["running"], status["state"]), (True, "pending"))

    def test_a_second_lane_starts_beside_the_running_primary(self):
        primary, _ = self.add()
        C.start_added(primary, environ={}, spawn=self.spawn, **self.kw)
        second, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C.start_added(second, environ={"REMOTE_PROACTIVE_ROOM": "!room"}, spawn=self.spawn, **self.kw)
        self.assertEqual([C.bridge_pid(r["instance"], self.ws) > 0 for r in self.rows()], [True, True], "both lanes run")
        self.assertEqual(self.envs[1]["REMOTE_PROACTIVE_ROOM"], "", "only the primary routes proactive messages")
        self.assertEqual(C.primary_instance(self.ws), primary["instance"])

    def test_a_held_lease_is_never_started_twice(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        try:
            record = C.start("beta-fixture-net", environ={}, spawn=self.spawn, **self.kw)
            self.assertEqual((record["ok"], record["lease"], record["pid"], self.envs), (True, "already_running", bridge.pid, []))
        finally:
            bridge_lease.stop(bridge)

    def test_two_concurrent_starts_leave_one_bridge(self):
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        gate = threading.Barrier(2)
        records = []

        def go():
            gate.wait()
            records.append(C.start("beta-fixture-net", environ={}, spawn=self.spawn, **self.kw))
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        holder = C.bridge_pid("beta-fixture-net", self.ws)
        live = [c for c in self.children if c.poll() is None]
        self.assertEqual(([r["ok"] for r in records], [c.pid for c in live]), ([True, True], [holder]),
                         "both starts succeed and exactly one bridge holds the lease")

    def test_a_failed_start_is_failed_with_its_reason(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.vault.store.clear()
        record = C.start_added(row, environ={}, spawn=self.spawn, **self.kw)
        self.assertEqual((record["ok"], record["reason"]), (False, "no_token"))
        status = C.status_for(self.rows()[0], self.ws)
        self.assertEqual((status["state"], status["reason"], status["running"]), ("failed", "no_token", False))

    def test_a_refusal_during_start_is_a_failed_record_not_an_exception(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        def refuse(*_a, **_kw):
            raise C.VaultUnavailable("secret vault not importable (ImportError)")
        with unittest.mock.patch.object(C, "launch_env", refuse):
            record = C.start_added(row, environ={}, spawn=self.spawn, **self.kw)
        self.assertEqual((record["ok"], record["reason"]), (False, "vault_unavailable"))
        self.assertEqual([r["instance"] for r in self.rows()], ["beta-fixture-net"], "the saved row stays")
        status = C.status_for(self.rows()[0], self.ws)
        self.assertEqual((status["state"], status["reason"]), ("failed", "vault_unavailable"))

    def test_a_lease_that_proves_nothing_reads_running_null(self):
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        lock = self.ws / "state" / "locks" / "gateway-bridge.beta-fixture-net.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-a-lease\n")
        self.assertIsNone(C.status_for(row, self.ws)["running"])

    def test_parked_and_legacy_rows_are_not_started(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        row, _ = self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        legacy, parked = self.rows()
        self.assertIsNone(C.start_added(legacy, environ={}, spawn=self.spawn, **self.kw))
        self.assertIsNone(C.start_added(parked, environ={}, spawn=self.spawn, **self.kw))
        self.assertEqual(self.envs, [])
        self.assertEqual((C.status_for(parked, self.ws)["state"], C.status_for(parked, self.ws)["running"]), ("parked", False))


class Identity(Base):
    def test_add_stores_the_identity_fields_a_verified_preview_carries(self):
        def listing(url, token, address=None):
            return {"agents": [{"id": "@sutando:alpha-fixture.net", "display_name": "Sutando",
                                "avatar_url": "mxc://alpha-fixture.net/av", "owner": "@owner:alpha-fixture.net"}]}
        row, _ = self.add(fetch=listing)
        self.assertEqual((row["agent_mxid"], row["agent_display_name"], row["agent_avatar_url"], row["owner_mxid"]),
                         ("@sutando:alpha-fixture.net", "Sutando", "mxc://alpha-fixture.net/av", "@owner:alpha-fixture.net"))
        listed = C.status_for(self.rows()[0], self.ws)
        self.assertEqual(listed["owner_mxid"], "@owner:alpha-fixture.net")
        self.assertNotIn("secret-alpha", json.dumps(listed))

    def test_unverified_rows_list_the_identity_fields_as_none(self):
        row, _ = self.add(fetch=unreachable)
        self.assertEqual([k in row for k in C.IDENTITY_FIELDS], [False] * 4, "nothing unknown is written")
        listed = C.status_for(row, self.ws)
        self.assertEqual([listed[k] for k in C.IDENTITY_FIELDS], [None] * 4)
        self.assertFalse(listed["disabled"])

    def test_learn_refreshes_identity_fields_on_an_identity_row_without_rekeying_it(self):
        row, _ = self.add(fetch=agents("@sutando:alpha-fixture.net"))
        self.assertEqual((row["server_source"], row.get("agent_display_name")), ("identity", None))
        seen = dict(C.preview_connection(TOKEN_A, fetch=unreachable), verified=True,
                    agent_mxid="@sutando:alpha-fixture.net", agent_display_name="Renamed")
        after = C.learn_identity(row["instance"], seen, workspace=self.ws)
        self.assertEqual((after["server"], after["server_source"], after["agent_display_name"]),
                         ("alpha-fixture.net", "identity", "Renamed"))
        other = dict(seen, server="beta-fixture.net", agent_display_name="Elsewhere")
        after = C.learn_identity(row["instance"], other, workspace=self.ws)
        self.assertEqual((after["server"], after["agent_display_name"]), ("alpha-fixture.net", "Renamed"),
                         "a verified preview of another server neither re-keys nor relabels the row")
        after = C.learn_identity(row["instance"], dict(seen, verified=False, agent_display_name="Nope"), workspace=self.ws)
        self.assertEqual(after["agent_display_name"], "Renamed", "an unverified preview teaches nothing")

    def test_another_agent_on_the_same_server_never_touches_an_identity_row(self):
        """Adding agent B's code where agent A's row is: A's row (identity fields, token) is as it
        was, and A's own rotation still passes its identity check afterwards."""
        def listing(url, token, address=None):
            return {"agents": [{"id": "@sutando:alpha-fixture.net", "display_name": "Sutando",
                                "avatar_url": "mxc://alpha-fixture.net/av", "owner": "@owner:alpha-fixture.net"}]}
        row_a, _ = self.add(fetch=listing)
        before = dict(row_a)

        def other(url, token, address=None):
            return {"agents": [{"id": "@other:alpha-fixture.net", "display_name": "Other",
                                "avatar_url": "mxc://alpha-fixture.net/other", "owner": "@someone:alpha-fixture.net"}]}
        token_b = "https://chat.alpha-fixture.net|secret-other"
        existing, created = self.add(token_b, fetch=other)
        self.assertFalse(created, "the duplicate-server rule answers with the existing row, as before")
        self.assertEqual(self.rows(), [before], "row A is untouched: identity fields and all")
        self.assertEqual(self.vault.store, {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET": TOKEN_A}, "A's token is untouched")
        seen_b = C.preview_connection(token_b, fetch=other)
        self.assertTrue(seen_b["verified"])
        self.assertEqual(C.learn_identity("alpha-fixture-net", seen_b, workspace=self.ws), before,
                         "a verified preview of another agent of that server teaches nothing")
        out = C.update_token("alpha-fixture-net", "https://chat.alpha-fixture.net|secret-alpha-2", repo=self.ws / "repo",
                             logs_dir=self.ws / "logs", environ={}, workspace=self.ws, vault=self.vault,
                             spawn=lambda *a: DEAD_PID, kill=lambda *a: None, fetch=listing)
        self.assertEqual((out["agent_mxid"], out["agent_display_name"]), ("@sutando:alpha-fixture.net", "Sutando"))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"], "https://chat.alpha-fixture.net|secret-alpha-2")


class Disconnect(Base):
    def setUp(self):
        super().setUp()
        self.spawned = []
        self.spawn = self.bridge_spawn(lambda argv, env, log_path: self.spawned.append(env["GATEWAY_INSTANCE"]))
        self.add()  # alpha is primary
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C._record_launch("beta-fixture-net", self.ws, ok=True, pid=1)

    def test_disconnect_disables_stops_once_and_keeps_row_vault_and_launch_record(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            os.kill(pid, sig)
        try:
            out = C.disconnect("beta-fixture-net", workspace=self.ws, kill=kill)
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual(killed, [(bridge.pid, 15)], "the stop helper ran exactly once")
        self.assertEqual((out["instance"], out["disabled"], out["stopped_pid"]), ("beta-fixture-net", True, bridge.pid))
        rows = {r["instance"]: r for r in self.rows()}
        self.assertEqual((rows["beta-fixture-net"]["disabled"], rows["alpha-fixture-net"]["disabled"]), (True, False))
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET", "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"},
                         "the token stays in the vault")
        self.assertTrue(C.launch_record_path("beta-fixture-net", self.ws).exists())
        self.assertNotIn("secret-beta", json.dumps(out))

    def test_disconnecting_a_disabled_row_is_a_no_op(self):
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("nothing runs"))
        again = C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("nothing runs"))
        self.assertEqual((again["disabled"], again["stopped_pid"]), (True, 0))

    def test_the_primary_cannot_be_disconnected_while_it_is_primary(self):
        with self.assertRaises(C.PrimaryCannotDisconnect) as cm:
            C.disconnect("alpha-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("never stopped"))
        self.assertTrue(str(cm.exception).startswith("primary_cannot_disconnect"))
        self.assertIsInstance(cm.exception, C.PrimaryProtected)
        self.assertFalse(self.rows()[0].get("disabled"))
        C.set_primary("beta-fixture-net", workspace=self.ws)
        out = C.disconnect("alpha-fixture-net", workspace=self.ws, kill=lambda *a: None)
        self.assertTrue(out["disabled"], "once another row is primary, the old primary can go down")
        with self.assertRaises(C.UnknownConnection):
            C.disconnect("nope", workspace=self.ws)

    def test_a_disabled_row_cannot_become_primary(self):
        """A disabled row never launches: as primary, no bridge would carry the proactive room."""
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        with self.assertRaises(C.DisabledCannotBePrimary) as cm:
            C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertTrue(str(cm.exception).startswith("disabled_cannot_be_primary"))
        self.assertEqual(cm.exception.CODE, "disabled_cannot_be_primary")
        self.assertIsInstance(cm.exception, C.Refusal)
        self.assertEqual([(r["instance"], r["primary"]) for r in self.rows()],
                         [("alpha-fixture-net", True), ("beta-fixture-net", False)], "the primary is unchanged")
        self.assertEqual(C.primary_instance(self.ws), "alpha-fixture-net")
        C.reconnect("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                    vault=self.vault, spawn=self.spawn, kill=lambda *a: None, fetch=unreachable)
        self.assertTrue(C.set_primary("beta-fixture-net", workspace=self.ws)["primary"], "reconnected, it can be primary")

    def test_the_legacy_row_cannot_be_disconnected_here(self):
        """The unsuffixed bridge is launched from REMOTE_TASK_TOKEN by the legacy launchers, which
        never read the flag: a disabled legacy row would say down over a running bridge."""
        C.import_from_env({"REMOTE_TASK_TOKEN": "https://chat.s.space|legacy"}, workspace=self.ws, vault=self.vault)
        with self.assertRaises(C.LegacyRowUnmanaged) as cm:
            C.disconnect("", workspace=self.ws, kill=lambda *a: self.fail("nothing runs"))
        self.assertTrue(str(cm.exception).startswith("legacy_row_unmanaged"))
        self.assertIsInstance(cm.exception, C.Refusal)
        legacy = {r["instance"]: r for r in self.rows()}[""]
        self.assertEqual((legacy["disabled"], legacy["primary"]), (False, False), "the row is unchanged")

    def test_a_kill_the_process_refuses_or_a_vanished_process(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net", mode="retain")

        def refuse(pid, sig):
            raise PermissionError("not my process")
        try:
            with self.assertRaises(C.BridgeStillRunning) as cm:
                C.disconnect("beta-fixture-net", workspace=self.ws, kill=refuse)
            self.assertEqual((cm.exception.pid, cm.exception.CODE), (bridge.pid, "bridge_still_running"))
            self.assertIn("PermissionError", str(cm.exception))
            self.assertFalse(self.beta()["disabled"], "a refused signal leaves the row enabled")

            def vanished(pid, sig):
                os.kill(pid, sig)
                bridge.wait()  # gone, its lease retained on disk
                raise ProcessLookupError(pid)
            out = C.disconnect("beta-fixture-net", workspace=self.ws, kill=vanished)
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual((out["disabled"], out["stopped_pid"]), (True, bridge.pid), "a process gone by itself counts as stopped")
        held = workspace_lock.read_lease("gateway-bridge.beta-fixture-net", self.ws)
        self.assertEqual((held["pid"], held.get("retained")), (bridge.pid, True),
                         "the lease outlived its writer, and the stop still knew it was gone")

    def test_a_lease_that_proves_no_holder_refuses_and_signals_nothing(self):
        """Fail closed: an unreadable record, a pid without a start time, another host's lease or a
        start time that cannot be checked is a refusal naming the cause, never pid 0, never a signal."""
        lock = self.ws / "state" / "locks" / "gateway-bridge.beta-fixture-net.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        host = workspace_lock._host_label()
        bound = {"role": "gateway-bridge.beta-fixture-net", "workspace": str(self.ws), "schema_version": 1}
        sleeper = __import__("subprocess").Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            cases = {"not-a-lease\n": "not a lease record",
                     json.dumps({"pid": "x", "host": host}): "no valid pid",
                     json.dumps(dict(bound, pid=sleeper.pid, host=host, heartbeat_at=int(time.time()))): "records no start time",
                     json.dumps(dict(bound, pid=sleeper.pid, host="elsewhere", heartbeat_at=int(time.time()))): "host 'elsewhere'"}
            for body, cause in cases.items():
                lock.write_text(body)
                with self.assertRaises(C.BridgeLeaseIndeterminate, msg=cause) as cm:
                    C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("signalled"))
                self.assertIn(cause, str(cm.exception))
                self.assertTrue(str(cm.exception).startswith("bridge_lease_indeterminate"))
                self.assertFalse(self.beta()["disabled"], cause)
            with unittest.mock.patch.object(workspace_lock, "proc_start", lambda pid: None):
                lock.write_text(json.dumps(dict(bound, pid=sleeper.pid, host=host, proc_start="x:1")))
                with self.assertRaises(C.BridgeLeaseIndeterminate) as cm:
                    C.stop_bridge("beta-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("signalled"))
                self.assertIn("cannot be read", str(cm.exception))
        finally:
            sleeper.kill()
            sleeper.wait()
        lock.write_text(json.dumps(dict(bound, pid=sleeper.pid, host=host, proc_start="x:1")))
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0, "a writer proven gone holds nothing")

    def beta(self):
        return {r["instance"]: r for r in self.rows()}["beta-fixture-net"]

    def test_a_bridge_that_keeps_its_lock_is_never_marked_disabled(self):
        """Real children hold the lease through workspace_lock.acquire + heartbeat, as the bridge
        does. One ignores SIGTERM: the stop deadline passes, the refusal names its pid, the row
        stays enabled and the lease is still held. One exits on SIGTERM and releases: a real stop,
        and only then `disabled`."""
        stubborn = bridge_lease.start(self.ws, "beta-fixture-net", mode="ignore")
        try:
            self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), stubborn.pid, "the child holds the lease")
            with unittest.mock.patch.object(C, "STOP_TIMEOUT_S", 0.6):
                with self.assertRaises(C.BridgeStillRunning) as cm:
                    C.disconnect("beta-fixture-net", workspace=self.ws)
            self.assertEqual(cm.exception.pid, stubborn.pid)
            self.assertIn("still held", str(cm.exception))
            self.assertFalse(self.beta()["disabled"], "the row is as it was")
            self.assertEqual(C.status_for(self.beta(), self.ws)["state"], "pending", "nothing masks the sidecar")
            self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), stubborn.pid, "the lease is still held")
        finally:
            bridge_lease.stop(stubborn)
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0, "killed, its lease proves no holder")
        willing = bridge_lease.start(self.ws, "beta-fixture-net")
        try:
            self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), willing.pid, "a successor reaped the dead lease")
            out = C.disconnect("beta-fixture-net", workspace=self.ws)
        finally:
            willing.wait(timeout=5)
            bridge_lease.stop(willing)
        self.assertEqual((out["disabled"], out["stopped_pid"]), (True, willing.pid), "a real SIGTERM stop, then the flag")
        self.assertEqual(willing.returncode, 0)
        self.assertIsNone(workspace_lock.read_lease("gateway-bridge.beta-fixture-net", self.ws), "released at exit")
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0)

    def test_a_disabled_row_is_disconnected_in_the_list_whatever_the_sidecar_says(self):
        (self.ws / "state" / "gateway-status.beta-fixture-net.json").write_text(json.dumps({"connected": True, "last_ok_ts": 5}))
        before = C.status_for(self.rows()[1], self.ws)
        self.assertEqual((before["state"], before["disabled"]), ("connected", False))
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        after = C.status_for(self.rows()[1], self.ws)
        self.assertEqual((after["state"], after["reason"], after["disabled"]), ("parked", "disabled", True))
        self.assertEqual(after["status"], {"connected": True, "last_ok_ts": 5}, "the sidecar reading is still shown")
        self.assertEqual((C.status_for(self.rows()[0], self.ws)["state"], C.status_for(self.rows()[0], self.ws)["disabled"]),
                         ("pending", False))

    def test_the_plan_omits_a_disabled_row_and_its_env_token(self):
        self.assertEqual([e["instance"] for e in C.launch_plan({}, workspace=self.ws)], ["alpha-fixture-net", "beta-fixture-net"])
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        env = {"AG2_REMOTE_TOKEN_BETA_FIXTURE_NET": TOKEN_B}
        self.assertEqual([e["instance"] for e in C.launch_plan(env, workspace=self.ws)], ["alpha-fixture-net"],
                         "a restart leaves it down; the env token does not bring it back either")
        with self.assertRaises(C.ConnectionsError) as cm:
            C.launch("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                     workspace=self.ws, vault=self.vault, spawn=self.spawn)
        self.assertIn("disconnected; reconnect it", str(cm.exception))
        self.assertNotIsInstance(cm.exception, C.UnknownConnection)
        self.assertEqual(self.spawned, [])

    def test_env_only_still_refuses_a_row_a_readable_record_marks_disabled(self):
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        env = {"AG2_REMOTE_TOKEN_BETA_FIXTURE_NET": TOKEN_B}
        with self.assertRaises(C.ConnectionsError) as cm:
            C.launch("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ=env,
                     workspace=self.ws, vault=self.vault, spawn=self.spawn, env_only=True)
        self.assertIn("disconnected; reconnect it", str(cm.exception))
        self.assertNotIsInstance(cm.exception, C.UnknownConnection)
        with self.assertRaises(C.ConnectionsError, msg="the env spelling of the instance names the same row"):
            C.plan_entry("beta_fixture_net", env, workspace=self.ws, env_only=True)
        self.assertEqual(self.spawned, [])
        self.assertEqual(C.plan_entry("alpha_fixture_net", {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET": TOKEN_A},
                                      workspace=self.ws, env_only=True)["instance"], "alpha_fixture_net",
                         "an enabled row still launches from the environment")

    def test_reconnect_clears_the_flag_and_launches(self):
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        record = C.reconnect("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=lambda *a: None, fetch=unreachable)
        self.assertEqual((record["ok"], record["pid"], record["stopped_pid"]), (True, self.child(), 0))
        self.assertEqual(self.spawned, ["beta-fixture-net"])
        row = C.status_for(self.rows()[1], self.ws)
        self.assertEqual((row["disabled"], row["state"]), (False, "pending"))
        self.assertEqual([e["instance"] for e in C.launch_plan({}, workspace=self.ws)], ["alpha-fixture-net", "beta-fixture-net"])
        # A failed row still reconnects as before, enabled rows untouched by the flag path.
        self.vault.store.pop("AG2_REMOTE_TOKEN_BETA_FIXTURE_NET")
        with self.assertRaises(C.LaunchFailed):
            C.reconnect("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                        workspace=self.ws, vault=self.vault, spawn=self.spawn, fetch=unreachable)
        self.assertEqual(C.status_for(self.rows()[1], self.ws)["state"], "failed")
        self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"] = TOKEN_B
        record = C.reconnect("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.spawn, fetch=unreachable)
        self.assertTrue(record["ok"])

    def test_old_records_without_the_flag_load_as_enabled(self):
        path = C.record_path(self.ws)
        payload = json.loads(path.read_text())
        for r in payload["connections"]:
            r.pop("disabled")
        path.write_text(json.dumps(payload))
        rows = self.rows()
        self.assertEqual([("disabled" in r) for r in rows], [False, False])
        self.assertEqual([C.status_for(r, self.ws)["disabled"] for r in rows], [False, False])
        self.assertEqual(len(C.launch_plan({}, workspace=self.ws)), 2)
        self.assertEqual(C.known_servers(self.ws, instances=[dotspace.DotSpace(server="beta-fixture.net")])[0]["disabled"], False)

    def test_remove_still_drops_a_disabled_row_with_its_key(self):
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        gone = C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault)
        self.assertEqual((gone["instance"], gone["stopped_pid"]), ("beta-fixture-net", 0))
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"})
        self.assertFalse(C.launch_record_path("beta-fixture-net", self.ws).exists())


class UpdateToken(Base):
    NEW_B = "https://relay.beta-fixture.net:8443/relay2|secret-beta-rotated"

    def setUp(self):
        super().setUp()
        self.spawned = []

        self.spawn = self.bridge_spawn(lambda argv, env, log_path: self.spawned.append((env["GATEWAY_INSTANCE"],
                                                                                         env["REMOTE_TASK_TOKEN"])))
        self.add()  # alpha is primary
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C._record_launch("beta-fixture-net", self.ws, ok=False, reason="no_token")

    def update(self, instance, token, **kw):
        kw.setdefault("fetch", agents("@s:beta-fixture.net"))
        kw.setdefault("kill", lambda *a: self.fail("nothing runs"))
        kw.setdefault("spawn", self.spawn)
        kw.setdefault("environ", {})
        return C.update_token(instance, token, repo=self.ws / "repo", logs_dir=self.ws / "logs",
                              workspace=self.ws, vault=self.vault, **kw)

    def beta(self):
        return {r["instance"]: r for r in self.rows()}["beta-fixture-net"]

    def test_same_server_and_agent_replaces_the_secret_and_relaunches(self):
        self.assertEqual(C.status_for(self.beta(), self.ws)["state"], "failed")
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        before = self.beta()
        self.assertEqual(C.status_for(before, self.ws)["state"], "parked")
        out = self.update("beta-fixture-net", self.NEW_B)
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], self.NEW_B, "replaced under the same key")
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET", "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"})
        after = self.beta()
        self.assertEqual((after["instance"], after["token_ref"], after["primary"], after["added_at"], after["server"],
                          after["agent_mxid"]),
                         (before["instance"], before["token_ref"], False, before["added_at"], "beta-fixture.net",
                          "@s:beta-fixture.net"))
        self.assertEqual((after["disabled"], after["gateway_url"]), (False, "https://relay.beta-fixture.net:8443/relay2"))
        self.assertEqual(self.spawned, [("beta-fixture-net", self.NEW_B)], "launched once, on the new code")
        self.assertEqual((out["launch"]["ok"], out["launch"]["pid"], out["stopped_pid"]), (True, self.child(), 0))
        self.assertEqual(C.status_for(after, self.ws)["state"], "pending", "the failed launch record is replaced")
        self.assertEqual([e["instance"] for e in C.launch_plan({}, workspace=self.ws)], ["alpha-fixture-net", "beta-fixture-net"])
        self.assertNotIn("secret-beta", json.dumps(out))
        self.assertFalse(self.rows()[0].get("disabled"), "the other row is untouched")

    def test_a_row_without_an_identity_learns_it_from_the_new_code(self):
        row, _ = self.add("https://chat.s.space|secret-s", fetch=unreachable)
        self.assertEqual((row["server"], row["server_source"], row.get("agent_mxid")), ("s.space", "resolver", None))
        out = self.update("s-space", "https://chat.s.space|secret-s-rotated", fetch=agents("@sutando:s.space"))
        self.assertEqual((out["server"], out["server_source"], out["verified"], out["agent_mxid"], out["owner_mxid"]),
                         ("s.space", "identity", True, "@sutando:s.space", "@owner:s.space"))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_S_SPACE"], "https://chat.s.space|secret-s-rotated")
        self.assertEqual(self.spawned, [("s-space", "https://chat.s.space|secret-s-rotated")])

    def test_a_code_for_another_server_is_refused_and_touches_nothing(self):
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        with self.assertRaises(C.TokenServerMismatch) as cm:
            self.update("beta-fixture-net", "https://chat.alpha-fixture.net|secret-alpha-2",
                        fetch=agents("@sutando:alpha-fixture.net"))
        self.assertTrue(str(cm.exception).startswith("token_server_mismatch"))
        self.assertEqual(cm.exception.CODE, "token_server_mismatch")
        self.assertIsInstance(cm.exception, C.TokenMismatch)
        self.assertNotIn("secret-alpha", str(cm.exception))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the vault is untouched")
        self.assertEqual(self.spawned, [], "no launch")
        self.assertEqual((self.beta()["disabled"], self.beta()["gateway_url"]), (True, "https://relay.beta-fixture.net:8443"))

    def test_a_code_for_another_agent_is_refused(self):
        with self.assertRaises(C.TokenAgentMismatch) as cm:
            self.update("beta-fixture-net", self.NEW_B, fetch=agents("@other:beta-fixture.net"))
        self.assertTrue(str(cm.exception).startswith("token_agent_mismatch"))
        self.assertIn("@other:beta-fixture.net", str(cm.exception))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the vault is untouched")
        self.assertEqual((self.spawned, self.beta()["agent_mxid"]), ([], "@s:beta-fixture.net"))

    def test_unknown_instance_rejected_code_and_vault_failure(self):
        with self.assertRaises(C.UnknownConnection):
            self.update("nope", self.NEW_B, fetch=lambda *a: self.fail("an unknown instance is never probed"))
        with self.assertRaises(C.TokenRejected):
            self.update("beta-fixture-net", self.NEW_B, fetch=http_error(401))
        self.vault.fail_set = True
        with self.assertRaises(C.VaultUnavailable):
            self.update("beta-fixture-net", self.NEW_B)
        self.assertEqual((self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], self.spawned), (TOKEN_B, []))
        self.assertEqual(self.beta()["gateway_url"], "https://relay.beta-fixture.net:8443", "the row is unchanged")

    def test_the_running_bridge_is_stopped_before_the_relaunch(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            os.kill(pid, sig)
        try:
            out = self.update("beta-fixture-net", self.NEW_B, kill=kill)
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual((killed, out["stopped_pid"]), ([(bridge.pid, 15)], bridge.pid))
        self.assertEqual(self.spawned, [("beta-fixture-net", self.NEW_B)])

    def test_a_failed_relaunch_is_the_rows_state(self):
        def boom(argv, env, log_path):
            raise OSError("no interpreter")
        out = self.update("beta-fixture-net", self.NEW_B, spawn=boom)
        self.assertEqual((out["launch"]["ok"], out["launch"]["reason"]), (False, "launch_failed"))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], self.NEW_B, "the secret is replaced anyway")
        self.assertEqual(C.status_for(self.beta(), self.ws)["state"], "failed")

    def test_the_legacy_row_is_the_environments_and_is_refused(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": "https://chat.s.space|legacy"}, workspace=self.ws, vault=self.vault)
        with self.assertRaises(C.LegacyRowUnmanaged) as cm:
            self.update("", "https://chat.s.space|legacy-rotated", fetch=lambda *a: self.fail("never probed"))
        self.assertTrue(str(cm.exception).startswith("legacy_row_unmanaged"))
        self.assertIn("REMOTE_TASK_TOKEN", str(cm.exception))
        self.assertIsInstance(cm.exception, C.Refusal)
        self.assertEqual(self.vault.store["REMOTE_TASK_TOKEN"], "https://chat.s.space|legacy", "the vault is untouched")
        self.assertEqual(self.spawned, [])
        with self.assertRaises(C.UnknownConnection):
            C.update_token("", "x", repo=self.ws, logs_dir=self.ws, environ={}, workspace=Path(self.tmp.name) / "empty",
                           vault=self.vault)

    def test_a_shadowing_environment_variable_refuses_the_update_and_touches_nothing(self):
        """The bridge reads AG2_REMOTE_TOKEN_<INST> before the vault (launch_env, the bridge's own
        SOURCES order): with it set, a vault write would be accepted and never used."""
        C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: None)
        environ = {"AG2_REMOTE_TOKEN_BETA_FIXTURE_NET": TOKEN_B}
        with self.assertRaises(C.TokenSourceShadowed) as cm:
            self.update("beta-fixture-net", self.NEW_B, environ=environ, fetch=lambda *a: self.fail("never probed"))
        self.assertTrue(str(cm.exception).startswith("token_source_shadowed"))
        self.assertEqual(cm.exception.variable, "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET")
        self.assertIn("AG2_REMOTE_TOKEN_BETA_FIXTURE_NET", str(cm.exception))
        self.assertNotIn("secret-beta", str(cm.exception))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the vault is untouched")
        self.assertEqual(self.spawned, [], "no launch")
        self.assertEqual((self.beta()["disabled"], self.beta()["gateway_url"]), (True, "https://relay.beta-fixture.net:8443"))
        # Another row's variable, or a blank value, shadows nothing.
        out = self.update("beta-fixture-net", self.NEW_B,
                          environ={"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET": TOKEN_A, "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET": "  "})
        self.assertEqual((self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], out["disabled"]), (self.NEW_B, False))
        self.assertEqual(self.spawned, [("beta-fixture-net", self.NEW_B)], "launched once, on the new code")

    def test_a_row_naming_an_agent_needs_a_preview_confirming_that_agent(self):
        """Without a verified agent (gateway down, an error, a listing naming none or several) the
        code is not shown to be this connection's; the old secret stays."""
        before = dict(self.beta())
        for fetch, label in ((unreachable, "unreachable"), (http_error(500), "http_500"),
                             (agents(), "empty listing"), (agents("@a:beta-fixture.net", "@b:beta-fixture.net"), "two agents")):
            with self.assertRaises(C.TokenUnverified, msg=label) as cm:
                self.update("beta-fixture-net", self.NEW_B, fetch=fetch)
            self.assertTrue(str(cm.exception).startswith("token_unverified"), label)
            self.assertIn("@s:beta-fixture.net", str(cm.exception))
            self.assertIsInstance(cm.exception, C.TokenMismatch)
            self.assertNotIn("secret-beta", str(cm.exception))
        with self.assertRaises(C.TokenAgentMismatch):
            self.update("beta-fixture-net", self.NEW_B, fetch=agents("@other:beta-fixture.net"))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the vault is untouched")
        self.assertEqual((self.spawned, self.beta()), ([], before), "the row is unchanged")
        # A provisional row names no agent: an unconfirmed code still rotates it, as before.
        row, _ = self.add(UNREGISTERED, fetch=unreachable)
        out = self.update(row["instance"], "https://gw.unregistered-fixture.net|secret-u2", fetch=unreachable)
        self.assertEqual((out["server_source"], out.get("agent_mxid")), ("gateway", None))
        self.assertEqual(self.vault.store[row["token_ref"]], "https://gw.unregistered-fixture.net|secret-u2")

    def test_a_write_that_fails_puts_the_previous_secret_back(self):
        """The vault is written before the record; a record write that fails leaves both as they were."""
        def refuse(path, record):
            raise OSError("disk full")
        with unittest.mock.patch.object(C, "_write", refuse):
            with self.assertRaises(OSError):
                self.update("beta-fixture-net", self.NEW_B)
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the previous secret is back")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.beta()["gateway_url"], "https://relay.beta-fixture.net:8443", "the row is unchanged")
        # A key the vault did not hold before goes back to absent.
        del self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"]
        with unittest.mock.patch.object(C, "_write", refuse):
            with self.assertRaises(OSError):
                self.update("beta-fixture-net", self.NEW_B)
        self.assertNotIn("AG2_REMOTE_TOKEN_BETA_FIXTURE_NET", self.vault.store)

    def test_an_unreadable_previous_secret_refuses_before_the_stop_and_the_write(self):
        """A failed read is not an absent secret: rolling back to "absent" would delete the working code."""
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")

        def broken_get(key):
            raise RuntimeError("keychain locked")
        try:
            with unittest.mock.patch.object(self.vault, "get", broken_get):
                with self.assertRaises(C.VaultUnreadable) as cm:
                    self.update("beta-fixture-net", self.NEW_B, kill=lambda *a: self.fail("stopped"))
            self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), bridge.pid, "the bridge still runs")
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual(cm.exception.CODE, "vault_unreadable")
        self.assertIn("AG2_REMOTE_TOKEN_BETA_FIXTURE_NET", str(cm.exception))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the old code is intact")
        self.assertEqual((self.beta()["gateway_url"], self.spawned), ("https://relay.beta-fixture.net:8443", []))

    def test_a_failed_restore_is_the_error_never_a_claimed_rollback(self):
        sets = []

        def set_once(key, value):
            sets.append(value)
            if len(sets) > 1:
                raise RuntimeError("keychain locked")
            self.vault.store[key] = value

        def refuse(path, record):
            raise OSError("disk full")
        with unittest.mock.patch.object(self.vault, "set", set_once), unittest.mock.patch.object(C, "_write", refuse):
            with self.assertRaises(C.TokenRestoreFailed) as cm:
                self.update("beta-fixture-net", self.NEW_B)
        message = str(cm.exception)
        self.assertEqual(cm.exception.CODE, "token_restore_failed")
        for part in ("OSError", "disk full", "restoring the previous secret failed", "RuntimeError", "holds the NEW code"):
            self.assertIn(part, message)
        self.assertNotIn("secret-beta", message)
        self.assertEqual(sets, [self.NEW_B, TOKEN_B], "the restore was attempted with the previous secret")
        self.assertEqual(self.beta()["gateway_url"], "https://relay.beta-fixture.net:8443", "the row is unchanged")
        self.assertEqual(self.spawned, [])

    def test_a_vault_write_failing_after_the_stop_relaunches_the_old_bridge(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        self.vault.fail_set = True
        try:
            with self.assertRaises(C.VaultUnavailable) as cm:
                self.update("beta-fixture-net", self.NEW_B, kill=os.kill)
            self.assertEqual(bridge.wait(timeout=5), 0, "the running bridge was stopped first")
        finally:
            bridge_lease.stop(bridge)
        self.assertIn("relaunched on the unchanged code", str(cm.exception))
        self.assertEqual(self.spawned, [("beta-fixture-net", TOKEN_B)], "relaunched on the old code")
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B)

    def test_a_kill_the_process_refuses_leaves_vault_and_row_unchanged(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")

        def kill(pid, sig):
            raise PermissionError("not my process")
        try:
            with self.assertRaises(C.BridgeStillRunning) as cm:
                self.update("beta-fixture-net", self.NEW_B, kill=kill)
        finally:
            bridge_lease.stop(bridge)
        self.assertTrue(str(cm.exception).startswith("bridge_still_running"))
        self.assertEqual(cm.exception.pid, bridge.pid)
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], TOKEN_B, "the vault is untouched")
        self.assertEqual(self.beta()["gateway_url"], "https://relay.beta-fixture.net:8443", "the row is unchanged")
        self.assertEqual(self.spawned, [])

    def test_a_server_already_another_rows_is_refused_before_the_vault_and_the_stop(self):
        """A provisional row whose new code now verifies as a server another row holds: the
        duplicate is caught while building the new row, not by the record write after the vault
        and the stop."""
        row, _ = self.add(UNREGISTERED, fetch=unreachable)
        self.assertEqual((row["server_source"], row["instance"]), ("gateway", "gw-unregistered-fixture-net"))
        bridge = bridge_lease.start(self.ws, row["instance"])
        try:
            with self.assertRaises(C.TokenServerMismatch) as cm:
                self.update(row["instance"], "https://gw.unregistered-fixture.net|secret-u2",
                            fetch=agents("@s:beta-fixture.net"),
                            resolver=lambda host: dotspace.DotSpace(server="beta-fixture.net", source="config"),
                            kill=lambda *a: self.fail("the stop was reached"))
        finally:
            bridge_lease.stop(bridge)
        self.assertIn("beta-fixture-net", str(cm.exception))
        self.assertEqual(self.vault.store[row["token_ref"]], UNREGISTERED, "the vault is untouched")
        self.assertEqual({r["instance"]: r["server"] for r in self.rows()}[row["instance"]], "gw.unregistered-fixture.net")
        self.assertEqual(self.spawned, [])


class LeaseFence(Base):
    """launch keeps the record lock until its child holds the bridge lease (the production writer),
    so a disconnect never lands in the window before the bridge has one."""

    def setUp(self):
        super().setUp()
        self.add()  # alpha is primary
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.repo, self.logs = self.ws / "repo", self.ws / "logs"

    def race(self, *, wait_for_lease=True):
        """launch beta with a child that takes its lease 0.6 s after the spawn; a disconnect starts
        as soon as the spawn returned. The disconnect's outcome and the child."""
        self.children = []
        spawned = threading.Event()
        inner = bridge_lease.spawner(self.ws, delay=0.6, started=self.children)

        def spawn(argv, env, log_path):
            proc = inner(argv, env, log_path)
            spawned.set()
            return proc
        result = {}

        def run_launch():
            result["launch"] = C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={},
                                        workspace=self.ws, vault=self.vault, spawn=spawn)
        patch = (unittest.mock.patch.object(C, "_await_lease", lambda instance, pid, *a: ("held", pid)) if not wait_for_lease
                 else unittest.mock.patch.object(C, "LEASE_TIMEOUT_S", C.LEASE_TIMEOUT_S))
        with patch:
            t = threading.Thread(target=run_launch)
            t.start()
            self.assertTrue(spawned.wait(10))
            out = C.disconnect("beta-fixture-net", workspace=self.ws)
            t.join(10)
        return result.get("launch"), out, self.children[0]

    def test_a_disconnect_right_after_the_spawn_stops_the_bridge_it_started(self):
        launched, out, child = self.race()
        self.assertEqual((launched["ok"], launched["pid"], launched["lease"]), (True, child.pid, "held"))
        self.assertEqual((out["disabled"], out["stopped_pid"]), (True, child.pid), "the disconnect found the lease")
        self.assertEqual(child.wait(timeout=5), 0, "stopped by SIGTERM, lease released")
        self.assertIsNone(workspace_lock.read_lease("gateway-bridge.beta-fixture-net", self.ws))

    def test_control_without_the_wait_the_disconnect_misses_the_bridge(self):
        """The same race with the wait removed: the disconnect sees no lease, writes `disabled`,
        and the bridge then takes its lease over a row that says it is down."""
        launched, out, child = self.race(wait_for_lease=False)
        self.assertEqual((out["disabled"], out["stopped_pid"]), (True, 0))
        self.assertEqual(child.stdout.readline().strip(), "held")
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), child.pid, "a live bridge under a disabled row")

    def test_a_child_without_a_lease_by_the_deadline_is_killed_and_the_launch_fails(self):
        self.children = []
        with unittest.mock.patch.object(C, "LEASE_TIMEOUT_S", 0.5):
            with self.assertRaises(C.LaunchFailed) as cm:
                C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                         vault=self.vault, spawn=bridge_lease.spawner(self.ws, delay=30, started=self.children))
        child = self.children[0]
        self.assertIsNotNone(child.poll(), "launch killed its own child")
        self.assertIn("took no lease", str(cm.exception))
        record = json.loads(C.launch_record_path("beta-fixture-net", self.ws).read_text())
        self.assertEqual((record["ok"], record["reason"], record["pid"]), (False, "lease_timeout", child.pid))
        self.assertEqual(C.status_for({"instance": "beta-fixture-net"}, self.ws)["state"], "failed")

    def test_a_child_standing_down_to_a_live_holder_ends_the_wait(self):
        holder = bridge_lease.start(self.ws, "beta-fixture-net")
        self.children = [holder]
        launched = C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                            vault=self.vault, spawn=bridge_lease.spawner(self.ws, started=self.children))
        self.assertEqual(launched["lease"], "exited", "the duplicate deferred and exited")
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), holder.pid)

    def test_launching_an_unknown_instance_before_any_record_raises_instead_of_hanging(self):
        """plan_entry runs under launch's record lock; listing would import the env token and take
        that lock a second time in the same process."""
        ws = self.ws / "fresh"
        result = {}

        def run():
            try:
                C.launch("nope", repo=self.repo, logs_dir=self.logs, workspace=ws, vault=self.vault,
                         spawn=lambda *a: self.fail("spawned"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        with unittest.mock.patch.dict(os.environ, {"REMOTE_TASK_TOKEN": TOKEN_A}):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(5)
        self.assertFalse(t.is_alive(), "launch hung on its own record lock")
        self.assertIsInstance(result.get("error"), C.UnknownConnection)


class LegacyHandoff(Base):
    """The legacy unsuffixed bridge reads the primary only when its launchers spawn it, so moving
    the primary off it is refused while it runs, before anything is written."""

    def setUp(self):
        super().setUp()
        C.import_from_env({"REMOTE_TASK_TOKEN": "https://chat.s.space|legacy"}, workspace=self.ws, vault=self.vault)
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual(C.primary_instance(self.ws), "", "the legacy row is primary")

    def test_promoting_off_a_running_legacy_bridge_is_refused_before_any_effect(self):
        legacy = bridge_lease.start(self.ws, "")
        try:
            with self.assertRaises(C.LegacyPrimaryRunning) as cm:
                C.set_primary("beta-fixture-net", workspace=self.ws)
        finally:
            bridge_lease.stop(legacy)
        e = cm.exception
        self.assertEqual((e.CODE, e.pid), ("legacy_primary_running", legacy.pid))
        self.assertIsInstance(e, C.Refusal)
        for step in ("restart.sh --stop-only", "make beta-fixture-net primary", "startup.sh", "nothing changed"):
            self.assertIn(step, str(e))
        self.assertEqual(C.primary_instance(self.ws), "", "the record is unchanged")

    def test_with_the_legacy_bridge_stopped_the_handoff_gives_the_room_to_the_new_primary_only(self):
        out = C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertEqual((out["primary"], out["relaunch_needed"]), (True, False))
        self.assertEqual(C.primary_instance(self.ws), "beta-fixture-net",
                         "non-empty: the legacy launchers spawn it with REMOTE_PROACTIVE_ROOM empty")
        environ = {"REMOTE_PROACTIVE_ROOM": "!nudges:s.space"}
        plan = C.launch_plan(environ, workspace=self.ws)
        self.assertEqual([(e["instance"], e["primary"]) for e in plan], [("beta-fixture-net", True)],
                         "the legacy row is never a plan entry")
        self.assertEqual(C.launch_env(plan[0], environ, vault=self.vault)["REMOTE_PROACTIVE_ROOM"], "!nudges:s.space")

    def test_a_legacy_lease_that_proves_no_holder_refuses_too(self):
        lock = self.ws / "state" / "locks" / "gateway-bridge.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-a-lease")
        with self.assertRaises(C.BridgeLeaseIndeterminate):
            C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertEqual(C.primary_instance(self.ws), "")

    def test_a_named_primary_moves_with_its_bridges_running(self):
        C.add_connection(TOKEN_A, added_by="owner", expected_server="alpha-fixture.net", workspace=self.ws,
                         vault=self.vault, preview=C.preview_connection(TOKEN_A, fetch=agents("@a:alpha-fixture.net")))
        self.assertFalse(C.set_primary("beta-fixture-net", workspace=self.ws)["relaunch_needed"])
        alpha = bridge_lease.start(self.ws, "alpha-fixture-net")
        try:
            out = C.set_primary("alpha-fixture-net", workspace=self.ws)
        finally:
            bridge_lease.stop(alpha)
        self.assertEqual((out["primary"], out["relaunch_needed"]), (True, True), "relaunch_needed reads the same lease")


class KnownServers(Base):
    INSTANCES = [dotspace.DotSpace(server="alpha-fixture.net", gateway_host="chat.alpha-fixture.net"),
                 dotspace.DotSpace(server="beta-fixture.net", gateway_host="relay.beta-fixture.net"),
                 dotspace.DotSpace(server="gamma-fixture.net")]

    def test_empty_record_offers_every_registered_server_unconnected(self):
        self.assertEqual(C.known_servers(self.ws, instances=self.INSTANCES), [
            {"server": "alpha-fixture.net", "connected": False, "instance": None, "primary": False, "disabled": False},
            {"server": "beta-fixture.net", "connected": False, "instance": None, "primary": False, "disabled": False},
            {"server": "gamma-fixture.net", "connected": False, "instance": None, "primary": False, "disabled": False},
        ])

    def test_rows_join_by_server_with_their_flags(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"), instance="beta")
        C.disconnect("beta", workspace=self.ws, kill=lambda *a: None)
        out = C.known_servers(self.ws, instances=self.INSTANCES)
        self.assertEqual([(s["server"], s["connected"], s["instance"], s["primary"], s["disabled"]) for s in out], [
            ("alpha-fixture.net", True, "alpha-fixture-net", True, False),
            ("beta-fixture.net", True, "beta", False, True),
            ("gamma-fixture.net", False, None, False, False)])
        self.assertNotIn("secret", json.dumps(out))
        self.assertNotIn("token_ref", json.dumps(out), "the picker sees no vault key names")

    def test_each_server_appears_once_in_registry_order(self):
        twice = [*self.INSTANCES, dotspace.DotSpace(server="ALPHA-fixture.net"), dotspace.DotSpace(server="")]
        self.assertEqual([s["server"] for s in C.known_servers(self.ws, instances=twice)],
                         ["alpha-fixture.net", "beta-fixture.net", "gamma-fixture.net"])

    def test_default_registry_comes_from_the_resolver(self):
        out = C.known_servers(self.ws)
        servers = [s["server"] for s in out]
        self.assertEqual(servers, [e["server"] for e in dotspace.DEFAULT_INSTANCES],
                         "with no config section, the built-in instances, once each")
        self.assertEqual(len(set(servers)), len(servers))
        self.assertTrue(all(s["connected"] is False for s in out))
        with unittest.mock.patch.dict(sys.modules, {"dotspace": None}):
            self.assertEqual(C.known_servers(self.ws), [], "without the resolver there is nothing to offer")

    def test_a_broken_record_is_a_schema_error_not_an_empty_offer(self):
        C.record_path(self.ws).parent.mkdir(parents=True)
        C.record_path(self.ws).write_text("{nope")
        with self.assertRaises(C.SchemaError):
            C.known_servers(self.ws, instances=self.INSTANCES)


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
                seen["host"] = self.headers.get("Host")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"agents": [{"id": "@bot:probe-fixture.net"}]}).encode())

            def log_message(self, *a):
                pass
        seen = {}
        srv = http.server.HTTPServer(("127.0.0.1", 0), Agents)
        srv.timeout = 5
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        connected = []

        def connect(address, port, host, timeout):
            connected.append((address, port, host))
            return socket.create_connection((address, srv.server_address[1]), timeout=timeout)
        try:
            answer = C._fetch_agents("https://chat.probe-fixture.net:8448/relay",
                                     "https://chat.probe-fixture.net:8448/relay|the-secret", "127.0.0.1",
                                     connect=connect)
        finally:
            t.join(5)
            srv.server_close()
        self.assertEqual(seen, {"auth": "Bearer the-secret", "path": "/relay/v1/agents", "host": "chat.probe-fixture.net:8448"})
        self.assertEqual(connected, [("127.0.0.1", 8448, "chat.probe-fixture.net")],
                         "the socket goes to the vetted address; SNI and Host carry the name")
        self.assertEqual(answer, {"agents": [{"id": "@bot:probe-fixture.net"}]})
        with self.assertRaises(OSError):
            C._connect("127.0.0.1", 9, "nowhere-fixture.net", 1)

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
        child = C._spawn([sys.executable, "-c", "print('hello from the bridge')"], isolated_child_env(), log)
        self.assertGreater(child.pid, 0)
        child.wait(timeout=10)
        self.assertIn("hello from the bridge", log.read_text())

    def test_lock_holder_reads(self):
        """bridge_pid reads workspace_lock's own lease: the pid a live writer recorded, 0 with no
        lease or a writer proven gone, and stop_bridge waits for the release."""
        self.assertEqual(C.bridge_pid("dev", self.ws), 0, "no lease, no bridge")
        bridge = bridge_lease.start(self.ws, "dev")
        try:
            self.assertEqual(C.bridge_pid("dev", self.ws), bridge.pid)
            sent = []

            def kill(pid, sig):
                sent.append(pid)
                threading.Timer(0.3, os.kill, (pid, sig)).start()
            self.assertEqual(C.stop_bridge("dev", workspace=self.ws, kill=kill), bridge.pid)
        finally:
            bridge_lease.stop(bridge)
        self.assertEqual(sent, [bridge.pid])
        self.assertEqual(C.stop_bridge("dev", workspace=self.ws, kill=lambda *a: self.fail("nothing to kill")), 0)

    def test_the_guard_runs_on_confirm_and_on_reconnect(self):
        preview = C.preview_connection(TOKEN_A, fetch=unreachable)
        with self.assertRaises(C.InvalidToken):
            C.add_connection(TOKEN_A, added_by="ui", expected_server=preview["server"], fetch=unreachable,
                             getaddrinfo=lambda h, p, **k: [(2, 1, 6, "", ("10.0.0.5", p))],
                             workspace=self.ws, vault=self.vault)
        self.assertEqual(self.rows(), [])
        row, _ = self.add(TOKEN_B, fetch=unreachable)
        private = lambda h, p, **k: [(2, 1, 6, "", ("127.0.0.1", p))]  # noqa: E731
        C.reconnect(row["instance"], repo=self.ws, logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                    vault=self.vault, spawn=self.bridge_spawn(), kill=lambda *a: None,
                    fetch=lambda *a: self.fail("probed a private address"), getaddrinfo=private)
        self.assertEqual(self.rows()[0]["server_source"], "resolver", "nothing learned, bridge launched anyway")

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

        def fetch(url, token, address):
            seen["url"], seen["token"], seen["address"] = url, token, address
            return {}
        C.preview_connection("https://chat.alpha-fixture.net/|abc", fetch=fetch)
        self.assertEqual(seen, {"url": "https://chat.alpha-fixture.net/", "token": "https://chat.alpha-fixture.net/|abc",
                                "address": "93.184.216.34"})

    def test_token_without_gateway_url_is_refused(self):
        with self.assertRaises(C.InvalidToken):
            C.preview_connection("opaque", fetch=unreachable)

    def test_404_is_not_a_gateway(self):
        p = C.preview_connection(TOKEN_A, fetch=http_error(404))
        self.assertEqual((p["authenticated"], p["error"], p["server_source"]), (None, "not_a_gateway", "resolver"))

    def test_resolver_names_the_server_of_a_gateway_host(self):
        for token in ("https://chat.s.space/relay|s1", "https://matrix.s.space/relay|s2"):
            p = C.preview_connection(token, fetch=unreachable)
            self.assertEqual((p["server"], p["server_source"], p["verified"]), ("s.space", "resolver", False), token)
        p = C.preview_connection(UNREGISTERED, fetch=unreachable)
        self.assertEqual((p["server"], p["server_source"]), ("gw.unregistered-fixture.net", "gateway"))
        p = C.preview_connection("https://chat.s.space|s", fetch=agents("@bot:other-fixture.net"))
        self.assertEqual((p["server"], p["server_source"], p["verified"], p["agent_mxid"]),
                         ("s.space", "resolver", False, None), "a listing that disagrees with the registry confirms nothing")

    def test_resolver_is_optional_and_may_fail(self):
        self.assertEqual(C.resolve_server("chat.s.space", resolver=lambda h: None), "")
        self.assertEqual(C.resolve_server("chat.s.space", resolver=lambda h: (_ for _ in ()).throw(OSError())), "")

        class Stub:
            server, source = "stub-fixture.net", "config"
        self.assertEqual(C.resolve_server("anything", resolver=lambda h: Stub()), "stub-fixture.net")
        saved = C.DEFAULT_RESOLVER
        try:
            C.DEFAULT_RESOLVER = lambda h: Stub()
            self.assertEqual(C.preview_connection(TOKEN_A, fetch=unreachable)["server_source"], "resolver")
        finally:
            C.DEFAULT_RESOLVER = saved

    def test_reconnect_tolerates_an_unusable_stored_token(self):
        row, _ = self.add(TOKEN_B, fetch=unreachable)
        self.vault.store[row["token_ref"]] = "opaque-bearer"
        record = C.reconnect(row["instance"], repo=self.ws, logs_dir=self.ws / "logs", environ={},
                             workspace=self.ws, vault=self.vault, spawn=self.bridge_spawn(), kill=lambda *a: None,
                             fetch=unreachable)
        self.assertEqual(record["pid"], self.child())
        self.assertEqual(self.rows()[0]["server_source"], "resolver")


def plant_lease(ws, instance, **fields):
    """A lease record at `instance`'s path: the writer's own fields, overridden by `fields`."""
    lock = ws / "state" / "locks" / f"{C._bridge_role(instance)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    record = {"role": C._bridge_role(instance), "pid": os.getpid(), "host": workspace_lock._host_label(),
              "workspace": str(ws), "acquired_at": now, "heartbeat_at": now, "schema_version": 1}
    record.update(fields)
    lock.write_text(json.dumps(record))
    return lock


class LeaseBinding(Base):
    """A lease proves a holder only for the role and workspace it was written for, and only while
    its pid is still the process (the same start time) that wrote it."""

    def setUp(self):
        super().setUp()
        self.add()  # alpha is primary
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.repo, self.logs = self.ws / "repo", self.ws / "logs"

    def test_a_copied_lease_never_attributes_another_bridges_pid(self):
        alpha = bridge_lease.start(self.ws, "alpha-fixture-net")
        self.children.append(alpha)
        locks = self.ws / "state" / "locks"
        (locks / "gateway-bridge.beta-fixture-net.lock").write_text(
            (locks / "gateway-bridge.alpha-fixture-net.lock").read_text())
        with self.assertRaises(C.BridgeLeaseIndeterminate) as cm:
            C.bridge_pid("beta-fixture-net", self.ws)
        self.assertIn("for role 'gateway-bridge.alpha-fixture-net'", str(cm.exception))
        with self.assertRaises(C.BridgeLeaseIndeterminate):
            C.stop_bridge("beta-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("signalled alpha"))
        with self.assertRaises(C.BridgeLeaseIndeterminate):
            C.disconnect("beta-fixture-net", workspace=self.ws, kill=lambda *a: self.fail("signalled alpha"))
        self.assertIsNone(alpha.poll(), "alpha still runs")
        self.assertFalse({r["instance"]: r for r in self.rows()}["beta-fixture-net"]["disabled"])

    def delayed_launch(self, planted):
        """launch beta with a child that takes its lease 0.6 s after the spawn, `planted` written at
        the child's pid the moment it is spawned; a disconnect starts once the spawn returned."""
        spawned = threading.Event()
        inner = bridge_lease.spawner(self.ws, delay=0.6, started=self.children)

        def spawn(argv, env, log_path):
            proc = inner(argv, env, log_path)
            plant_lease(self.ws, "beta-fixture-net", pid=proc.pid, **planted)
            spawned.set()
            return proc
        result = {}

        def run():
            try:
                result["launch"] = C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={},
                                            workspace=self.ws, vault=self.vault, spawn=spawn)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(spawned.wait(10))
        try:
            result["disconnect"] = C.disconnect("beta-fixture-net", workspace=self.ws)
        except Exception as e:  # noqa: BLE001
            result["disconnect_error"] = e
        t.join(30)
        return result, self.children[0]

    def test_a_same_pid_record_with_another_start_time_is_not_the_childs_lease(self):
        result, child = self.delayed_launch({"proc_start": "another-generation:1"})
        self.assertEqual((result["launch"]["ok"], result["launch"]["lease"]), (True, "held"),
                         "launch waited for the child's own lease")
        self.assertEqual((result["disconnect"]["disabled"], result["disconnect"]["stopped_pid"]), (True, child.pid),
                         "the disconnect waited for the launch and stopped the bridge it started")
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0, "no bridge under the disabled row")

    def test_a_same_pid_record_from_another_host_is_not_the_childs_lease(self):
        result, child = self.delayed_launch({"host": "elsewhere"})
        self.assertIsInstance(result.get("error"), C.LaunchFailed, "never read as held")
        self.assertIn("rc 75", str(result["error"]))
        self.assertEqual(child.wait(timeout=5), 75, "the child deferred to the other host's lease")
        self.assertIsInstance(result.get("disconnect_error"), C.BridgeLeaseIndeterminate)
        self.assertFalse({r["instance"]: r for r in self.rows()}["beta-fixture-net"]["disabled"])


class ChildExit(Base):
    """A child that exits is a launch failure unless another process is proven to hold the lease."""

    def setUp(self):
        super().setUp()
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.repo, self.logs = self.ws / "repo", self.ws / "logs"

    def launch(self, spawn):
        return C.launch("beta-fixture-net", repo=self.repo, logs_dir=self.logs, environ={}, workspace=self.ws,
                        vault=self.vault, spawn=spawn)

    def test_a_child_that_exits_before_any_lease_fails_the_launch(self):
        def spawn(argv, env, log_path):
            proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(2)"], env=isolated_child_env())
            self.children.append(proc)
            return proc
        with self.assertRaises(C.LaunchFailed) as cm:
            self.launch(spawn)
        self.assertIn("exited (rc 2)", str(cm.exception))
        record = json.loads(C.launch_record_path("beta-fixture-net", self.ws).read_text())
        self.assertEqual((record["ok"], record["reason"], record["rc"]), (False, "exited", 2))
        self.assertEqual(C.status_for({"instance": "beta-fixture-net"}, self.ws)["state"], "failed")
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0)

    def retained(self):
        old = bridge_lease.start(self.ws, "beta-fixture-net", mode="retain")
        old.terminate()
        self.assertEqual(old.wait(timeout=5), 0)
        bridge_lease.stop(old)
        lease = workspace_lock.read_lease("gateway-bridge.beta-fixture-net", self.ws)
        self.assertTrue(lease["retained"])
        return lease

    def test_a_retained_lease_that_outlasts_the_bound_is_refused_by_name(self):
        self.retained()
        t0 = time.monotonic()
        with self.assertRaises(C.LaunchFailed) as cm:
            self.launch(bridge_lease.spawner(self.ws, started=self.children))
        self.assertLess(time.monotonic() - t0, C.LEASE_TIMEOUT_S / 2, "refused at once, not after the bound")
        self.assertIn("retained lease", str(cm.exception))
        self.assertEqual(self.children[0].returncode, 75)
        record = json.loads(C.launch_record_path("beta-fixture-net", self.ws).read_text())
        self.assertEqual((record["ok"], record["reason"]), (False, "retained_lease"))

    def test_a_retained_lease_that_goes_stale_within_the_bound_is_waited_out(self):
        lease = self.retained()
        lock = self.ws / "state" / "locks" / "gateway-bridge.beta-fixture-net.lock"
        real = bridge_lease.spawner(self.ws, started=self.children)

        def spawn(argv, env, log_path):
            # The first child defers to the fresh lease however slowly it starts; only then does
            # the lease age to go stale within the bound, so the second child takes it.
            proc = real(argv, env, log_path)
            if len(self.children) == 1:
                proc.wait(timeout=10)
                lease["heartbeat_at"] = int(time.time()) - workspace_lock.DEFAULT_STALE_SECONDS + 2
                lock.write_text(json.dumps(lease))
            return proc
        record = self.launch(spawn)
        self.assertEqual(len(self.children), 2, "the first child stood down, the second took the lease")
        self.assertEqual(self.children[0].returncode, 75)
        self.assertEqual((record["ok"], record["lease"], record["pid"]), (True, "held", self.children[1].pid))
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), self.children[1].pid)


class GatedVault(FakeVault):
    """A vault whose first read from thread B waits for `gate`: B is paused at its snapshot."""

    def __init__(self):
        super().__init__()
        self.gate, self.b_reading = threading.Event(), threading.Event()

    def get(self, key):
        value = super().get(key)
        if threading.current_thread().name == "B" and not self.b_reading.is_set():
            self.b_reading.set()
            self.gate.wait(10)
        return value


class TokenConcurrency(Base):
    """The snapshot an update restores from is taken under the record lock, so another writer that
    commits in between is never undone (production `_transaction`, real threads)."""

    REF = "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"
    A = "https://relay.beta-fixture.net:8443/a|secret-beta-A"
    B = "https://relay.beta-fixture.net:8443/b|secret-beta-B"

    def setUp(self):
        super().setUp()
        self.vault = GatedVault()
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))

    def update(self, token):
        return C.update_token("beta-fixture-net", token, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                              workspace=self.ws, vault=self.vault, spawn=self.bridge_spawn(), kill=os.kill,
                              fetch=agents("@s:beta-fixture.net"))

    def interleave(self, other):
        """B pauses at its snapshot; `other` runs (given 1 s), then B resumes and its row write fails."""
        real_write = C._write
        errors = {}

        def write(path, record):
            if threading.current_thread().name == "B":
                raise OSError("disk full")
            real_write(path, record)

        def run_b():
            try:
                self.update(self.B)
            except Exception as e:  # noqa: BLE001
                errors["B"] = e
        with unittest.mock.patch.object(C, "_write", write):
            b = threading.Thread(target=run_b, name="B")
            b.start()
            self.assertTrue(self.vault.b_reading.wait(10))
            o = threading.Thread(target=other, name="other")
            o.start()
            o.join(1.0)
            self.vault.gate.set()
            b.join(30)
            o.join(30)
        return errors.get("B")

    def test_a_failed_update_never_restores_over_a_committed_rotation(self):
        error = self.interleave(lambda: self.update(self.A))
        self.assertIsInstance(error, OSError)
        row = {r["instance"]: r for r in self.rows()}["beta-fixture-net"]
        self.assertEqual(row["gateway_url"], "https://relay.beta-fixture.net:8443/a", "A's row is committed")
        self.assertEqual(self.vault.store[self.REF], self.A, "and the vault holds A's code, not B's stale snapshot")

    def test_a_failed_update_never_restores_over_a_remove_and_re_add(self):
        readded = "https://relay.beta-fixture.net:8443|secret-beta-readded"

        def remove_and_re_add():
            C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault)
            self.add(readded, fetch=agents("@s:beta-fixture.net"))
        error = self.interleave(remove_and_re_add)
        self.assertIsInstance(error, (OSError, C.UnknownConnection))
        self.assertIn("beta-fixture-net", {r["instance"] for r in self.rows()})
        self.assertEqual(self.vault.store[self.REF], readded, "the re-added row's code survives B's failure")


class Keychain:
    """`security` for vault_intercept: add/find/delete-generic-password over a dict, with faults."""

    def __init__(self, items):
        self.items, self.fail_add_after, self.fail_find = dict(items), None, False
        self.adds = 0

    def __call__(self, argv, capture_output=True, timeout=None, **kw):
        verb, key = argv[1], argv[argv.index("-s") + 1]
        if verb == "add-generic-password":
            self.adds += 1
            if self.fail_add_after is not None and self.adds > self.fail_add_after:
                return subprocess.CompletedProcess(argv, 1, b"", b"locked")
            self.items[key] = argv[argv.index("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if verb == "find-generic-password":
            if self.fail_find:
                return subprocess.CompletedProcess(argv, 51, b"", b"")
            if key not in self.items:
                return subprocess.CompletedProcess(argv, 44, b"", b"")
            return subprocess.CompletedProcess(argv, 0, self.items[key].encode() + b"\n", b"")
        return subprocess.CompletedProcess(argv, 0 if self.items.pop(key, None) is not None else 44, b"", b"")


class VaultPartialWrite(Base):
    """update_token over the real vault_intercept (Keychain then manifest): once the write began, what
    the vault holds is read back, never assumed."""

    REF = "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"
    NEW = "https://relay.beta-fixture.net:8443|secret-beta-rotated"

    def setUp(self):
        super().setUp()
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        import vault_intercept
        self.vi = vault_intercept
        self.keychain = Keychain(self.vault.store)
        self.spawned = []
        manifest = self.ws / "vault-keys.json"
        # Only vault_intercept's view of subprocess: the host label and the children keep the real one.
        security = unittest.mock.Mock(wraps=subprocess, run=self.keychain, TimeoutExpired=subprocess.TimeoutExpired)
        self.patches = [unittest.mock.patch.object(vault_intercept, "subprocess", security),
                        unittest.mock.patch.object(vault_intercept, "_manifest_path", lambda: str(manifest))]
        for p in self.patches:
            p.start()
        self.vault = C._VaultIntercept()
        self.bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        self.children.append(self.bridge)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def update(self):
        spawn = self.bridge_spawn(lambda argv, env, log_path: self.spawned.append(env["REMOTE_TASK_TOKEN"]))
        return C.update_token("beta-fixture-net", self.NEW, repo=self.ws / "repo", logs_dir=self.ws / "logs",
                              environ={}, workspace=self.ws, vault=self.vault, spawn=spawn, kill=os.kill,
                              fetch=agents("@s:beta-fixture.net"))

    def test_keychain_written_then_manifest_failing_is_put_back_and_read_back(self):
        """Both directions fail after the Keychain write: the NEW code's and the restore's."""
        with unittest.mock.patch.object(self.vi, "_register_key", side_effect=OSError("manifest read-only")):
            with self.assertRaises(C.VaultUnavailable) as cm:
                self.update()
        self.assertEqual(self.keychain.items[self.REF], TOKEN_B, "read back as the previous secret")
        self.assertEqual(self.spawned, [TOKEN_B], "relaunched on the code the vault really holds")
        self.assertIn("relaunched on the unchanged code", str(cm.exception))
        self.assertEqual({r["instance"]: r for r in self.rows()}["beta-fixture-net"]["gateway_url"],
                         "https://relay.beta-fixture.net:8443")

    def test_a_restore_that_fails_says_the_read_back_found_the_new_code(self):
        self.keychain.fail_add_after = 1
        with unittest.mock.patch.object(C, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(C.TokenRestoreFailed) as cm:
                self.update()
        self.assertEqual(self.keychain.items[self.REF], self.NEW)
        self.assertIn("read back, AG2_REMOTE_TOKEN_BETA_FIXTURE_NET holds the NEW code", str(cm.exception))
        self.assertNotIn("secret-beta", str(cm.exception))
        self.assertEqual(self.spawned, [], "never relaunched")

    def test_a_value_the_public_vault_writer_committed_meanwhile_is_never_restored_over(self):
        """The public writer (set_vault_key) takes no connections lock: the restore is a compare-and-swap
        on the stored value, so a third value is left in place and the outcome is token_state_unknown."""
        theirs = "https://relay.beta-fixture.net:8443|secret-beta-public"

        def write(path, record):
            self.vi.set_vault_key(self.REF, theirs)
            raise OSError("disk full")
        with unittest.mock.patch.object(C, "_write", write):
            with self.assertRaises(C.ConnectionsError) as cm:
                self.update()
        self.assertEqual(self.keychain.items[self.REF], theirs, "the public writer's value survives")
        self.assertIsInstance(cm.exception, C.TokenStateUnknown)
        self.assertIn("another writer's value", str(cm.exception))
        self.assertNotIn("secret-beta", str(cm.exception))
        self.assertEqual(self.spawned, [], "no relaunch on a value this update cannot vouch for")
        self.assertEqual({r["instance"]: r for r in self.rows()}["beta-fixture-net"]["gateway_url"],
                         "https://relay.beta-fixture.net:8443", "the row is unchanged")

    def test_a_failed_publication_with_no_other_writer_restores_the_previous_secret(self):
        with unittest.mock.patch.object(C, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(C.ConnectionsError) as cm:
                self.update()
        self.assertNotIsInstance(cm.exception, C.TokenStateUnknown)
        self.assertEqual(self.keychain.items[self.REF], TOKEN_B, "compared as the new code, swapped back")
        self.assertEqual(self.spawned, [TOKEN_B], "the stopped bridge relaunched on the previous secret")
        self.assertIn("relaunched on the unchanged code", str(cm.exception))

    def test_a_read_back_that_cannot_tell_is_token_state_unknown(self):
        def write(path, record):
            self.keychain.fail_find = True
            raise OSError("disk full")
        with unittest.mock.patch.object(C, "_write", write):
            with self.assertRaises(C.TokenStateUnknown) as cm:
                self.update()
        message = str(cm.exception)
        self.assertEqual(cm.exception.CODE, "token_state_unknown")
        self.assertIsInstance(cm.exception, C.Refusal)
        self.assertTrue(message.startswith("token_state_unknown"))
        for claim in ("NEW code", "unchanged code", "previous secret is back", "secret-beta"):
            self.assertNotIn(claim, message)
        self.assertIn("not known which secret", message)
        self.assertEqual(self.spawned, [], "no relaunch on an assumed code")


class LegacyCarrier(Base):
    """Exactly one bridge carries the proactive room: the legacy bridge settles it under the record
    lock right after its lease, and a named promotion is refused while it runs."""

    SCRIPT = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import workspace_lock as wl, connections as C\n"
        "ws = Path(sys.argv[2])\n"
        "print('read', C.primary_instance(ws), flush=True)\n"
        "sys.stdin.readline()\n"
        "assert wl.acquire('gateway-bridge', ws).status != 'deferred'\n"
        "print('keep', C.legacy_keeps_proactive_room(ws), flush=True)\n"
        "time.sleep(30)\n")

    def setUp(self):
        super().setUp()
        C.import_from_env({"REMOTE_TASK_TOKEN": "https://chat.s.space|legacy"}, workspace=self.ws, vault=self.vault)
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))

    def launcher(self):
        proc = subprocess.Popen([sys.executable, "-c", self.SCRIPT, str(ROOT / "src"), str(self.ws)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=isolated_child_env(), text=True)
        self.children.append(proc)
        return proc

    def carriers(self, keep):
        named = C.primary_instance(self.ws)
        return (["legacy"] if keep else []) + ([named] if named else [])

    def test_no_primary_with_a_live_legacy_lease_refuses_a_named_promotion(self):
        self.add(fetch=agents("@a:alpha-fixture.net"))
        C.set_primary("alpha-fixture-net", workspace=self.ws)
        C.remove_connection("alpha-fixture-net", force=True, workspace=self.ws, vault=self.vault)
        self.assertIsNone(C.primary_instance(self.ws))
        legacy = bridge_lease.start(self.ws, "")
        self.children.append(legacy)
        with self.assertRaises(C.LegacyPrimaryRunning) as cm:
            C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertEqual(cm.exception.pid, legacy.pid)
        self.assertIsNone(C.primary_instance(self.ws), "nothing changed")

    def test_a_launcher_that_read_before_the_promotion_drops_the_room_after_its_lease(self):
        launcher = self.launcher()
        self.assertEqual(launcher.stdout.readline().strip(), "read", "it read no named primary: room passed on")
        C.set_primary("beta-fixture-net", workspace=self.ws)
        launcher.stdin.write("\n")
        launcher.stdin.flush()
        line = launcher.stdout.readline().strip()
        self.assertEqual(line, "keep False", "settled after the lease, it drops the room")
        self.assertEqual(self.carriers(line == "keep True"), ["beta-fixture-net"], "exactly one proactive carrier")

    def test_a_legacy_bridge_that_kept_the_room_blocks_the_promotion(self):
        launcher = self.launcher()
        launcher.stdout.readline()
        launcher.stdin.write("\n")
        launcher.stdin.flush()
        keep = launcher.stdout.readline().strip() == "keep True"
        self.assertTrue(keep)
        with self.assertRaises(C.LegacyPrimaryRunning):
            C.set_primary("beta-fixture-net", workspace=self.ws)
        self.assertEqual(self.carriers(keep), ["legacy"], "exactly one proactive carrier")

    def test_an_unreadable_record_keeps_the_room(self):
        C.record_path(self.ws).write_text("{broken")
        self.assertTrue(C.legacy_keeps_proactive_room(self.ws), "fail-open, as the env-only launch")


class ReconcileUnderLock(Base):
    """A failed update reconciles the vault while it still holds the record lock: a writer that
    would commit meanwhile waits, so its committed row and secret are never split."""

    REF = "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"
    A = "https://relay.beta-fixture.net:8443/a|secret-beta-A"
    B = "https://relay.beta-fixture.net:8443/b|secret-beta-B"

    def setUp(self):
        super().setUp()
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))

    def update(self, token):
        return C.update_token("beta-fixture-net", token, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                              workspace=self.ws, vault=self.vault, spawn=self.bridge_spawn(), kill=os.kill,
                              fetch=agents("@s:beta-fixture.net"))

    def pause_b_in_reconcile(self, other):
        """B's row write fails; B pauses inside its reconcile while `other` runs. Whether `other`
        finished during the pause, and B's error."""
        real_write, real_reconcile = C._write, C._reconcile_vault
        in_reconcile, gate, errors = threading.Event(), threading.Event(), {}

        def write(path, record):
            if threading.current_thread().name == "B":
                raise OSError("disk full")
            real_write(path, record)

        def paused_reconcile_vault(*a, **k):
            if threading.current_thread().name == "B":
                in_reconcile.set()
                gate.wait(10)
            return real_reconcile(*a, **k)

        def run_b():
            try:
                self.update(self.B)
            except Exception as e:  # noqa: BLE001
                errors["B"] = e
        with unittest.mock.patch.object(C, "_write", write), unittest.mock.patch.object(C, "_reconcile_vault", paused_reconcile_vault):
            b = threading.Thread(target=run_b, name="B")
            b.start()
            self.assertTrue(in_reconcile.wait(10))
            o = threading.Thread(target=other, name="other")
            o.start()
            o.join(1.0)
            other_done = not o.is_alive()
            gate.set()
            b.join(30)
            o.join(30)
        return other_done, errors.get("B")

    def test_a_rotation_committing_during_the_reconcile_waits_and_wins(self):
        other_done, error = self.pause_b_in_reconcile(lambda: self.update(self.A))
        self.assertIsInstance(error, OSError)
        self.assertFalse(other_done, "A was blocked on the record lock while B reconciled")
        row = {r["instance"]: r for r in self.rows()}["beta-fixture-net"]
        self.assertEqual((row["gateway_url"], self.vault.store[self.REF]),
                         ("https://relay.beta-fixture.net:8443/a", self.A), "row and vault agree on A")

    def test_a_remove_and_re_add_during_the_reconcile_waits_and_wins(self):
        readded = "https://relay.beta-fixture.net:8443|secret-beta-readded"

        def remove_and_re_add():
            C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault)
            self.add(readded, fetch=agents("@s:beta-fixture.net"))
        other_done, error = self.pause_b_in_reconcile(remove_and_re_add)
        self.assertIsInstance(error, OSError)
        self.assertFalse(other_done, "the remove waited for B's reconcile")
        self.assertIn("beta-fixture-net", {r["instance"] for r in self.rows()})
        self.assertEqual(self.vault.store[self.REF], readded, "the re-added row's code survives B's failure")


@unittest.skipUnless(hasattr(os, "waitid"), "needs waitid(WNOWAIT) to hold a zombie")
class ZombieChild(Base):
    """A child that took its lease and then exited is not a held lease, even before it is reaped."""

    SCRIPT = ("import os, sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
              "import workspace_lock as wl\n"
              "assert wl.acquire(sys.argv[3], Path(sys.argv[2])).status != 'deferred'\n"
              "os._exit(2)\n")

    def test_a_child_that_writes_its_lease_then_exits_2_fails_the_launch(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))

        def spawn(argv, env, log_path):
            proc = subprocess.Popen([sys.executable, "-c", self.SCRIPT, str(ROOT / "src"), str(self.ws),
                                     C._bridge_role(env["GATEWAY_INSTANCE"])], env=isolated_child_env())
            self.children.append(proc)
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)  # exited, not yet reaped
            return proc
        with self.assertRaises(C.LaunchFailed) as cm:
            C.launch("beta-fixture-net", repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                     workspace=self.ws, vault=self.vault, spawn=spawn)
        self.assertIn("exited (rc 2)", str(cm.exception))
        self.assertEqual(self.children[0].returncode, 2)
        record = json.loads(C.launch_record_path("beta-fixture-net", self.ws).read_text())
        self.assertEqual((record["ok"], record["reason"]), (False, "exited"))
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), 0)


@unittest.skipIf(os.name == "nt", "zombies are a POSIX state")
class ZombieWithoutWaitid(Base):
    """Where os.waitid is missing (macOS before 3.13), an exited, unreaped bridge still reads as stopped."""

    BRIDGE = ("import os, signal, sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
              "import workspace_lock as wl\nws, role = Path(sys.argv[2]), sys.argv[3]\nwl.acquire(role, ws)\n"
              "signal.signal(signal.SIGTERM, lambda *a: (wl.release(role, ws), os._exit(0)))\n"
              "print('held', flush=True)\nwhile True:\n    time.sleep(0.2)\n")
    PARENT = ("import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', *sys.argv[1:]])\n"
              "time.sleep(600)\n")
    INST = "beta-fixture-net"

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(C, "STOP_TIMEOUT_S", 10)
        patcher.start()
        self.addCleanup(patcher.stop)
        if hasattr(os, "waitid"):
            waitid = os.waitid
            del os.waitid
            self.addCleanup(setattr, os, "waitid", waitid)
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.args = [self.BRIDGE, str(ROOT / "src"), str(self.ws), C._bridge_role(self.INST)]

    def start(self, argv):
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, env=isolated_child_env(), text=True)
        self.children.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def assert_stops_promptly(self, pid):
        t0 = time.monotonic()
        self.assertEqual(C.stop_bridge(self.INST, workspace=self.ws), pid)
        self.assertLess(time.monotonic() - t0, 3, "an exited bridge was waited on as if it still ran")
        self.assertEqual(C.bridge_pid(self.INST, self.ws), 0)

    def test_our_exited_unreaped_child_is_stopped(self):
        proc = self.start([sys.executable, "-c", *self.args])
        self.assert_stops_promptly(proc.pid)

    def test_another_process_exited_unreaped_child_is_stopped(self):
        self.start([sys.executable, "-c", self.PARENT, *self.args])
        pid = C.bridge_pid(self.INST, self.ws)
        self.assertTrue(pid)
        self.assert_stops_promptly(pid)


@unittest.skipIf(os.name == "nt", "zombies are a POSIX state")
class ZombieProbeFailures(unittest.TestCase):
    """A probe that cannot answer reads as still running, so stop_bridge keeps waiting instead of guessing."""

    def test_a_waitpid_error_other_than_not_our_child_reads_as_running(self):
        with unittest.mock.patch.object(C.os, "waitpid", side_effect=PermissionError):
            self.assertFalse(C._zombie(12345))

    def test_our_child_still_running_reads_as_running(self):
        with unittest.mock.patch.object(C.os, "waitpid", return_value=(0, 0)):
            self.assertFalse(C._zombie(12345))

    def test_ps_that_cannot_run_reads_as_running(self):
        with unittest.mock.patch.object(C.os, "waitpid", side_effect=ChildProcessError), \
                unittest.mock.patch.object(C.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            self.assertFalse(C._zombie(12345))


FAKE_SECURITY = r"""#!/usr/bin/env bash
verb="$1"; shift; key=""; value=""
while [ $# -gt 0 ]; do case "$1" in -s) key="$2"; shift 2;; -w) if [ "$verb" = add-generic-password ]; then value="$2"; shift 2; else shift; fi;; *) shift;; esac; done
case "$verb" in
  add-generic-password) printf '%s' "$value" > "$FAKE_KEYCHAIN/$key";;
  find-generic-password) [ -f "$FAKE_KEYCHAIN/$key" ] && cat "$FAKE_KEYCHAIN/$key" || exit 44;;
  delete-generic-password) [ -f "$FAKE_KEYCHAIN/$key" ] && rm "$FAKE_KEYCHAIN/$key" || exit 44;;
  *) exit 1;;
esac
"""


@unittest.skipIf(os.name == "nt", "the stub Keychain is a bash script")
class CrossProcessVaultBarrier(Base):
    """The public writer, in another process, cannot land between a failed update's compare and its
    restore: it waits for update_token's vault transaction, then lands, and its value is final."""

    REF = "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"
    NEW = "https://relay.beta-fixture.net:8443|secret-beta-rotated"
    THIRD = "https://relay.beta-fixture.net:8443|secret-beta-public"
    PUBLIC = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport vault_intercept as v\n"
              "print(v._manifest_path(), flush=True)\nv.set_vault_key(sys.argv[2], sys.argv[3])\n"
              "print('stored', flush=True)\n")

    def setUp(self):
        super().setUp()
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        import vault_intercept
        self.vi = vault_intercept
        self.keychain = self.ws / "keychain"
        self.keychain.mkdir()
        for key, value in self.vault.store.items():
            (self.keychain / key).write_text(value)
        bindir = self.ws / "bin"
        bindir.mkdir()
        (bindir / "security").write_text(FAKE_SECURITY)
        (bindir / "security").chmod(0o755)
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_KEYCHAIN": str(self.keychain),
                    "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_TEST_MODE": "1"}
        manifest = str(self.ws / "state" / "secret-vault" / "keys.json")
        self.patches = [unittest.mock.patch.dict(os.environ, self.env),
                        unittest.mock.patch.object(vault_intercept, "_manifest_path", lambda: manifest)]
        for p in self.patches:
            p.start()
        self.manifest = manifest
        self.vault = C._VaultIntercept()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def barrier(self, public):
        """B's record write fails; once its reconcile has read NEW, a public set_vault_key starts in
        another process. Whether it was still running during the pause, and its exit code."""
        real_snapshot = C._vault_snapshot
        state = {"failed": False, "child": None, "blocked": None}

        def write(path, record):
            state["failed"] = True
            raise OSError("disk full")

        def snapshot(vault, key):
            value = real_snapshot(vault, key)
            if state["failed"] and state["child"] is None:
                self.assertEqual(value, self.NEW, "the reconcile observed this update's own write")
                child = subprocess.Popen([sys.executable, "-c", self.PUBLIC, str(ROOT / "src"), self.REF, public],
                                         stdout=subprocess.PIPE, env=isolated_child_env(**self.env), text=True)
                state["child"] = child
                state["manifest"] = child.stdout.readline().strip()
                time.sleep(0.6)
                state["blocked"] = child.poll() is None
            return value
        with unittest.mock.patch.object(C, "_write", write), unittest.mock.patch.object(C, "_vault_snapshot", snapshot):
            with self.assertRaises(OSError):
                C.update_token("beta-fixture-net", self.NEW, repo=self.ws / "repo", logs_dir=self.ws / "logs",
                               environ={}, workspace=self.ws, vault=self.vault, spawn=self.bridge_spawn(),
                               kill=os.kill, fetch=agents("@s:beta-fixture.net"))
        child = state["child"]
        self.assertIsNotNone(child, "the reconcile read NEW")
        self.assertEqual(os.path.realpath(state["manifest"]), os.path.realpath(self.manifest), "one vault")
        rc = child.wait(10)
        child.stdout.close()
        return state["blocked"], rc

    def test_a_public_write_between_the_compare_and_the_restore_waits_and_is_final(self):
        blocked, rc = self.barrier(self.THIRD)
        self.assertTrue(blocked, "the public writer waited for update_token's vault transaction")
        self.assertEqual(rc, 0)
        self.assertEqual((self.keychain / self.REF).read_text(), self.THIRD, "the public value is the final one")

    def test_a_public_write_of_the_same_new_value_is_final_too(self):
        """ABA: a public write of exactly the update's new code is not mistaken for the update's own."""
        blocked, rc = self.barrier(self.NEW)
        self.assertTrue(blocked)
        self.assertEqual(rc, 0)
        self.assertEqual((self.keychain / self.REF).read_text(), self.NEW, "the public write landed after the restore")

    def test_the_normal_restore_still_puts_the_previous_secret_back(self):
        with unittest.mock.patch.object(C, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                C.update_token("beta-fixture-net", self.NEW, repo=self.ws / "repo", logs_dir=self.ws / "logs",
                               environ={}, workspace=self.ws, vault=self.vault, spawn=self.bridge_spawn(),
                               kill=os.kill, fetch=agents("@s:beta-fixture.net"))
        self.assertEqual((self.keychain / self.REF).read_text(), TOKEN_B, "the previous secret is back")

    def test_lock_order_connections_then_vault_never_deadlocks(self):
        """update_token holds connections.lock then the vault lock; a public writer and a remove run
        beside it and every one finishes."""
        done = []

        def public():
            self.vi.set_vault_key("AG2_REMOTE_TOKEN_OTHER", "x")
            done.append("public")

        def remove():
            C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault)
            done.append("remove")

        def update():
            try:
                C.update_token("beta-fixture-net", self.NEW, repo=self.ws / "repo", logs_dir=self.ws / "logs",
                               environ={}, workspace=self.ws, vault=self.vault, spawn=self.bridge_spawn(),
                               kill=os.kill, fetch=agents("@s:beta-fixture.net"))
            except C.UnknownConnection:
                pass
            done.append("update")
        threads = [threading.Thread(target=f) for f in (update, public, remove)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(done), ["public", "remove", "update"], "no deadlock")


class BusyVault(FakeVault):
    """A vault whose writer lock another writer holds past the wait (vault_intercept.VaultBusy)."""

    def writer_lock(self, wait_s=None):
        raise RuntimeError("vault_busy: another vault writer held the writer lock for 30s; nothing was written")


class RealLockVault(FakeVault):
    """FakeVault values under vault_intercept's real cross-process writer lock (a temp lock path)."""

    def writer_lock(self, wait_s=None):
        import vault_intercept
        return vault_intercept.writer_lock(wait_s)


class VaultWriterBusy(Base):
    REF = "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"

    def setUp(self):
        super().setUp()
        import vault_intercept
        self.vi = vault_intercept
        manifest = str(self.ws / "vault" / "keys.json")
        for target, value in (("_manifest_path", lambda: manifest), ("_LEGACY_MANIFEST_PATH", manifest),
                              ("_WRITER_LOCK_WAIT_S", 2.0)):
            patcher = unittest.mock.patch.object(vault_intercept, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.vault = RealLockVault()
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        self.killed = []

    def update(self, kill, vault=None):
        return C.update_token("beta-fixture-net", UpdateToken.NEW_B, repo=self.ws / "repo", logs_dir=self.ws / "logs",
                              environ={}, workspace=self.ws, vault=vault or self.vault, spawn=self.bridge_spawn(),
                              kill=kill, fetch=agents("@s:beta-fixture.net"))

    def beta(self):
        return {r["instance"]: r for r in self.rows()}["beta-fixture-net"]

    def stop_and_record(self, pid, sig):
        self.killed.append(pid)
        os.kill(pid, sig)

    def hold_lock_in_child(self):
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import vault_intercept as v;"
                "v._manifest_path = lambda: sys.argv[2]; cm = v.writer_lock(); cm.__enter__();"
                "print('held', flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "src"), self.vi._manifest_path()],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), "held")

    def test_a_held_vault_lock_refuses_before_the_bridge_is_stopped(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        self.children.append(bridge)
        before = self.beta()
        self.hold_lock_in_child()
        began = time.monotonic()
        with self.assertRaises(C.VaultUnavailable) as ctx:
            self.update(kill=lambda pid, sig: self.killed.append((pid, sig)))
        waited = time.monotonic() - began
        self.assertLess(waited, C.VAULT_PROBE_WAIT_S + 1.5, "the probe waits briefly, never out the lock")
        self.assertIn("vault_busy", str(ctx.exception))
        self.assertNotIn("secret-beta", str(ctx.exception))
        self.assertEqual(self.killed, [], "no SIGTERM")
        self.assertEqual(C.bridge_pid("beta-fixture-net", self.ws), bridge.pid, "the same bridge still runs")
        self.assertEqual(self.vault.store[self.REF], TOKEN_B, "the vault is untouched")
        self.assertEqual(self.beta(), before, "the row is untouched")

    def test_a_brief_writer_on_the_lock_is_waited_out_and_the_update_lands(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        self.children.append(bridge)
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import vault_intercept as v;"
                "v._manifest_path = lambda: sys.argv[2]; cm = v.writer_lock(); cm.__enter__();"
                "print('held', flush=True); time.sleep(0.5); cm.__exit__(None, None, None)")
        child = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "src"), self.vi._manifest_path()],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), "held")
        out = self.update(kill=self.stop_and_record)
        self.assertEqual((self.killed, out["stopped_pid"]), ([bridge.pid], bridge.pid))
        self.assertEqual(self.vault.store[self.REF], UpdateToken.NEW_B, "a chat write's brief hold does not refuse the update")

    def test_a_vault_write_the_bridge_finishes_while_it_stops_lands_without_a_stall(self):
        bridge = bridge_lease.start(self.ws, "beta-fixture-net")
        self.children.append(bridge)
        landed = []

        def kill(pid, sig):
            # The bridge finishes an in-flight vault write before it exits on SIGTERM.
            def write():
                with self.vi.writer_lock():
                    landed.append(time.monotonic())
            t = threading.Thread(target=write)
            t.start()
            t.join(5)
            self.killed.append(pid)
            os.kill(pid, sig)
        began = time.monotonic()
        out = self.update(kill)
        self.assertLess(time.monotonic() - began, 1.5, "no wait toward the lock cap")
        self.assertEqual((self.killed, out["stopped_pid"]), ([bridge.pid], bridge.pid))
        self.assertEqual(len(landed), 1, "the bridge's own write landed during the stop")
        self.assertEqual(self.vault.store[self.REF], UpdateToken.NEW_B)

    def test_the_normal_path_is_unchanged(self):
        out = self.update(kill=lambda *a: self.fail("nothing runs"))
        self.assertEqual(out["stopped_pid"], 0)
        self.assertEqual(self.vault.store[self.REF], UpdateToken.NEW_B)
        self.assertEqual(self.beta()["gateway_url"], C.gateway_url_of(UpdateToken.NEW_B))

    def test_a_hung_keychain_write_names_vault_keychain_timeout_and_no_secret(self):
        import stat
        bindir = self.ws / "bin"
        bindir.mkdir()
        stub = bindir / "security"
        stub.write_text('#!/bin/sh\ncase "$1" in find-generic-password) exit 44;; esac\nexec sleep 30\n')
        stub.chmod(stat.S_IRWXU)
        with unittest.mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}), \
                unittest.mock.patch.object(self.vi, "_KEYCHAIN_WRITE_TIMEOUT_S", 0.5):
            with self.assertRaises(C.ConnectionsError) as ctx:
                self.update(kill=lambda *a: self.fail("nothing runs"), vault=C._VaultIntercept())
        text = str(ctx.exception)
        self.assertIn("vault_keychain_timeout", text)
        for secret in ("secret-beta", UpdateToken.NEW_B, TOKEN_B):
            self.assertNotIn(secret, text)

    def keychain_stub(self, *flags):
        """A `security` stand-in whose writes hang (one item file); `commits` lands a write first."""
        import stat
        bindir = self.ws / "bin"
        bindir.mkdir(exist_ok=True)
        for flag in flags:
            (bindir / flag).write_text("")
        stub = bindir / "security"
        stub.write_text(f"""#!/bin/sh
D='{bindir}'; verb="$1"; shift; v=""
while [ $# -gt 0 ]; do [ "$1" = "-w" ] && [ "$verb" = add-generic-password ] && v="$2"; shift; done
case "$verb" in
  find-generic-password) [ -f "$D/item" ] && {{ cat "$D/item"; exit 0; }}; exit 44;;
  add-generic-password) [ -f "$D/commits" ] && printf '%s' "$v" > "$D/item";;
  delete-generic-password) [ -f "$D/commits" ] && rm -f "$D/item";;
esac
exec sleep 30
""")
        stub.chmod(stat.S_IRWXU)
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}))
        stack.enter_context(unittest.mock.patch.object(self.vi, "_KEYCHAIN_WRITE_TIMEOUT_S", 0.5))
        stack.enter_context(unittest.mock.patch.object(self.vi, "_KEYCHAIN_READ_TIMEOUT_S", 0.5))
        return stack, bindir / "item"

    def fresh_ws(self):
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        self.ws = Path(self.tmp2.name)
        self.vault = C._VaultIntercept()

    def test_an_add_whose_store_lands_then_hangs_is_saved(self):
        self.fresh_ws()
        stack, item = self.keychain_stub("commits")
        with stack:
            row, _ = self.add()
        self.assertEqual(item.read_text(), TOKEN_A)
        self.assertEqual([r["instance"] for r in self.rows()], [row["instance"]])
        self.assertIn(row["token_ref"], self.vi._read_manifest(), "the manifest matches the Keychain")

    def test_an_add_whose_store_did_not_land_fails_naming_the_timeout(self):
        self.fresh_ws()
        stack, item = self.keychain_stub()
        with stack, self.assertRaises(C.VaultUnavailable) as ctx:
            self.add()
        self.assertIn("vault_keychain_timeout", str(ctx.exception))
        self.assertFalse(item.exists())
        self.assertEqual(self.rows(), [])

    def test_a_remove_whose_delete_lands_then_hangs_drops_the_row(self):
        self.fresh_ws()
        stack, item = self.keychain_stub("commits")
        with stack:
            row, _ = self.add()
            C.remove_connection(row["instance"], force=True, workspace=self.ws, vault=self.vault)
        self.assertFalse(item.exists())
        self.assertEqual(self.rows(), [])
        self.assertNotIn(row["token_ref"], self.vi._read_manifest())

    def test_the_cause_passes_only_a_vault_code(self):
        self.assertEqual(C._vault_cause(RuntimeError("vault_busy: held 30s")), "RuntimeError: vault_busy")
        self.assertEqual(C._vault_cause(RuntimeError("vault: failed to store 'K': sk-secret")), "RuntimeError")
        self.assertEqual(C._vault_cause(RuntimeError("https://x|secret: y")), "RuntimeError")


class RemoveClearsSidecar(Base):
    def test_a_re_added_row_lists_pending_not_the_removed_rows_status(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C._record_launch("beta-fixture-net", self.ws, ok=True, pid=1)
        (self.ws / "state" / "gateway-status.beta-fixture-net.json").write_text(
            json.dumps({"connected": True, "last_ok_ts": 1700000000}))
        (self.ws / "gateway-status.beta-fixture-net.json").write_text(json.dumps({"connected": True}))
        C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault, kill=lambda *a: None)
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        beta = {r["instance"]: r for r in C.list_connections(self.ws, vault=self.vault)}["beta-fixture-net"]
        listed = C.status_for(beta, self.ws)
        self.assertEqual((listed["state"], listed["status"]), ("pending", {"connected": None, "last_ok_ts": None}))

    LATE_WRITER = ("import signal, sys, time, json\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                   "import workspace_lock as wl\nws, role, status = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])\n"
                   "wl.acquire(role, ws)\n"
                   "def stop(*a):\n    wl.release(role, ws)\n    time.sleep(0.4)\n"
                   "    status.write_text(json.dumps({'connected': True, 'last_ok_ts': 1}))\n    sys.exit(0)\n"
                   "signal.signal(signal.SIGTERM, stop)\nprint('held', flush=True)\n"
                   "while True:\n    time.sleep(0.2)\n    wl.heartbeat(role, ws)\n")

    def test_a_bridge_whose_last_status_write_follows_its_lease_release_is_waited_for(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        status = self.ws / "state" / "gateway-status.beta-fixture-net.json"
        child = subprocess.Popen([sys.executable, "-c", self.LATE_WRITER, str(ROOT / "src"), str(self.ws),
                                  C._bridge_role("beta-fixture-net"), str(status)],
                                 stdout=subprocess.PIPE, env=isolated_child_env(), text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        gone = C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault, kill=os.kill)
        self.assertEqual(gone["stopped_pid"], child.pid)
        child.wait(5)
        self.assertFalse(status.exists(), "the late status write landed before the cleanup, not after")

    def test_a_concurrent_same_name_re_add_keeps_its_new_sidecar(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        status = self.ws / "state" / "gateway-status.beta-fixture-net.json"
        status.write_text(json.dumps({"connected": True, "last_ok_ts": 1}))
        at_cleanup, readded = threading.Event(), threading.Event()
        real = C.launch_record_path

        def gated(instance, workspace=None):
            if threading.current_thread().name == "remover" and not at_cleanup.is_set():
                at_cleanup.set()
                readded.wait(2)
            return real(instance, workspace)

        def remover():
            C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault, kill=lambda *a: None)

        def re_adder():
            at_cleanup.wait(5)
            self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
            status.write_text(json.dumps({"connected": True, "last_ok_ts": 2}))  # the new bridge reports
            readded.set()
        with unittest.mock.patch.object(C, "launch_record_path", gated):
            threads = [threading.Thread(target=remover, name="remover"), threading.Thread(target=re_adder)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertTrue(status.exists(), "the remover deleted the re-added row's sidecar")
        self.assertEqual(json.loads(status.read_text())["last_ok_ts"], 2)

    def test_remove_leaves_other_rows_sidecars(self):
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        alpha = self.ws / "state" / "gateway-status.alpha-fixture-net.json"
        alpha.write_text(json.dumps({"connected": True, "last_ok_ts": 5}))
        C.remove_connection("beta-fixture-net", workspace=self.ws, vault=self.vault, kill=lambda *a: None)
        self.assertTrue(alpha.exists())


class LingeringBridge(Base):
    """A bridge that releases its lease on SIGTERM but stays alive past the stop deadline: every
    caller of stop_bridge refuses with bridge_still_running and leaves its state as it was."""

    LINGER = ("import signal, sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
              "import workspace_lock as wl\nws, role = Path(sys.argv[2]), sys.argv[3]\nwl.acquire(role, ws)\n"
              "signal.signal(signal.SIGTERM, lambda *a: wl.release(role, ws))\nprint('held', flush=True)\n"
              "while True:\n    time.sleep(0.2)\n    wl.heartbeat(role, ws)\n")
    INST = "beta-fixture-net"
    REF = "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(C, "STOP_TIMEOUT_S", 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.add()
        self.add(TOKEN_B, fetch=agents("@s:beta-fixture.net"))
        C._record_launch(self.INST, self.ws, ok=True, pid=1)
        self.status = self.ws / "state" / f"gateway-status.{self.INST}.json"
        self.status.write_text(json.dumps({"connected": True, "last_ok_ts": 1}))
        self.bridge = subprocess.Popen([sys.executable, "-c", self.LINGER, str(ROOT / "src"), str(self.ws),
                                        C._bridge_role(self.INST)], stdout=subprocess.PIPE,
                                       env=isolated_child_env(), text=True)
        self.children.append(self.bridge)
        self.assertEqual(self.bridge.stdout.readline().strip(), "held")
        self.spawned = []

    def spawn(self, argv, env, log_path):
        self.spawned.append(env["GATEWAY_INSTANCE"])
        return self.bridge_spawn()(argv, env, log_path)

    def beta(self):
        return {r["instance"]: r for r in self.rows()}.get(self.INST)

    def assert_lingering(self, ctx):
        self.assertIn("bridge_still_running", str(ctx.exception))
        self.assertIn("released its lease", str(ctx.exception))
        self.assertIsNone(self.bridge.poll(), "the old bridge is still alive")

    def test_update_token_writes_nothing_and_says_the_old_code_is_still_active(self):
        before = self.beta()
        with self.assertRaises(C.BridgeStillRunning) as ctx:
            C.update_token(self.INST, UpdateToken.NEW_B, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                           workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=os.kill,
                           fetch=agents("@s:beta-fixture.net"))
        self.assert_lingering(ctx)
        self.assertIn("was not confirmed stopped and may still be running on the old code", str(ctx.exception))
        self.assertEqual(self.vault.store[self.REF], TOKEN_B, "the vault still holds the old code")
        self.assertEqual(self.beta(), before, "the row is unchanged")
        self.assertEqual(self.spawned, [], "no second bridge")

    def test_remove_keeps_the_row_launch_record_status_and_vault_key(self):
        with self.assertRaises(C.BridgeStillRunning) as ctx:
            C.remove_connection(self.INST, workspace=self.ws, vault=self.vault, kill=os.kill)
        self.assert_lingering(ctx)
        self.assertIsNotNone(self.beta(), "the row stays")
        self.assertTrue(C.launch_record_path(self.INST, self.ws).exists(), "the launch record stays")
        self.assertTrue(self.status.exists(), "the status file stays")
        self.assertEqual(self.vault.store[self.REF], TOKEN_B, "the vault key stays")

    def test_disconnect_does_not_mark_the_row_disabled(self):
        with self.assertRaises(C.BridgeStillRunning) as ctx:
            C.disconnect(self.INST, workspace=self.ws, kill=os.kill)
        self.assert_lingering(ctx)
        self.assertFalse(self.beta()["disabled"], "disabled is never written over a live process")

    def test_reconnect_spawns_no_second_bridge(self):
        with self.assertRaises(C.BridgeStillRunning) as ctx:
            C.reconnect(self.INST, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                        vault=self.vault, spawn=self.spawn, kill=os.kill, fetch=agents("@s:beta-fixture.net"))
        self.assert_lingering(ctx)
        self.assertEqual(self.spawned, [], "no second bridge while the old one lives")


if __name__ == "__main__":
    unittest.main(verbosity=1)
