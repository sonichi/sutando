#!/usr/bin/env python3
"""src/dotspace.py: which homeservers are DotSpaces, resolved per server (config, well-known,
token, none), and that an ag2.space install still gets today's literals from every caller.

Run: python3 tests/dotspace.test.py
"""
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

# Before dotspace reads the environment: no outer identity or credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import dotspace as ds  # noqa: E402


def _offline(url, timeout):
    raise OSError("tests never fetch a well-known document; inject a fetch")


_REAL_FETCH = ds.default_fetch
ds.default_fetch = _offline  # the default path must stay hermetic here

NONE = {"instances": []}  # a config that knows no DotSpace: well-known and token decide


def block(**over):
    b = {"operator": "ag2", "gateway_url": "https://chat.new.space",
         "cloud_origin": "https://sutando.new.space/", "mcp_url": "https://mcp.new.space/mcp"}
    b.update(over)
    return json.dumps({"m.homeserver": {"base_url": "https://new.space"}, ds.WELL_KNOWN_KEY: b})


def _load(name, rel):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fetcher:
    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.calls = body, exc, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.exc:
            raise self.exc
        return self.body


class TestParsing(unittest.TestCase):
    def test_server_of_mxid_splits_at_the_first_colon(self):
        self.assertEqual(ds.server_of_mxid("@u:ag2.space"), "ag2.space")
        self.assertEqual(ds.server_of_mxid("@u:ag2.space:8448"), "ag2.space:8448")
        self.assertEqual(ds.server_of_mxid("@u:[::1]:8448"), "[::1]:8448")
        self.assertEqual(ds.server_of_mxid("!room:ag2.space"), "")
        self.assertEqual(ds.server_of_mxid("@nocolon"), "")
        self.assertEqual(ds.server_of_mxid(""), "")

    def test_server_of_matrix_id_takes_every_sigil(self):
        self.assertEqual(ds.server_of_matrix_id("!r:s.space"), "s.space")
        self.assertEqual(ds.server_of_matrix_id("#alias:dev.s.space"), "dev.s.space")
        self.assertEqual(ds.server_of_matrix_id("$event:ag2.space"), "")
        self.assertEqual(ds.server_of_matrix_id("ag2.space"), "")

    def test_server_of_gateway_url(self):
        self.assertEqual(ds.server_of_gateway_url("https://chat.ag2.space/api/v1"), "chat.ag2.space")
        self.assertEqual(ds.server_of_gateway_url("https://Chat.S.Space:8443"), "chat.s.space")
        self.assertEqual(ds.server_of_gateway_url("not a url"), "")
        self.assertEqual(ds.server_of_gateway_url(""), "")

    def test_gateway_url_of_token(self):
        self.assertEqual(ds.gateway_url_of_token("https://chat.s.space|secret"), "https://chat.s.space")
        self.assertEqual(ds.gateway_url_of_token("https://chat.s.space%7Csecret"), "https://chat.s.space")
        self.assertEqual(ds.gateway_url_of_token("bare-secret|x"), "")
        self.assertEqual(ds.gateway_url_of_token("https://no-separator"), "")


class TestConfig(unittest.TestCase):
    def test_built_in_default_is_todays_four_instances(self):
        servers = [e["server"] for e in ds.DEFAULT_INSTANCES]
        self.assertEqual(servers, ["ag2.space", "dev.ag2.space", "s.space", "dev.s.space"])
        d = ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH, config={})
        self.assertEqual((d.server, d.operator, d.gateway_host, d.link_via, d.cloud_origin,
                          d.prefer_ipv4, d.source),
                         ("ag2.space", "ag2", "chat.ag2.space", "ag2.space",
                          "https://sutando.ag2.space", True, "config"))
        self.assertTrue(d.is_dotspace)

    def test_gateway_host_and_subdomains_match_their_instance(self):
        for query, server in [("chat.ag2.space", "ag2.space"), ("dev-chat.s.space", "dev.s.space"),
                              ("x.dev.ag2.space", "dev.ag2.space"), ("chat.dev.s.space", "dev.s.space"),
                              ("ag2.space:8448", "ag2.space"), ("AG2.SPACE", "ag2.space"),
                              ("@u:s.space", "s.space"), ("!r:dev.ag2.space", "dev.ag2.space"),
                              ("https://chat.s.space/x", "s.space")]:
            self.assertEqual(ds.dotspace_for(query, fetch=ds.NO_FETCH).server, server, query)

    def test_configured_section_replaces_the_default(self):
        cfg = {"instances": [{"server": "x.space", "gateway_host": "gw.x.space",
                              "cloud_origin": "https://c.x.space", "prefer_ipv4": False}]}
        d = ds.dotspace_for("gw.x.space", fetch=ds.NO_FETCH, config=cfg)
        self.assertEqual((d.server, d.cloud_origin, d.prefer_ipv4, d.source),
                         ("x.space", "https://c.x.space", False, "config"))
        self.assertFalse(ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH, config=cfg).is_dotspace)
        self.assertEqual(ds.trusted_cloud_hosts(cfg), frozenset({"c.x.space", ds.LEGACY_CLOUD_HOST}))

    def test_loaded_config_section_is_read_through_the_canonical_loader(self):
        import sutando_config
        cfg = {"core": {"dotspace": {"instances": [{"server": "y.space"}]}}}
        with mock.patch.object(sutando_config, "load_config", return_value=cfg):
            self.assertEqual(ds.dotspace_for("y.space", fetch=ds.NO_FETCH).source, "config")
            self.assertFalse(ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH).is_dotspace)
        with mock.patch.object(sutando_config, "load_config", side_effect=RuntimeError("boom")):
            self.assertTrue(ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH).is_dotspace)

    def test_plain_server_is_not_a_dotspace(self):
        d = ds.dotspace_for("matrix.org", fetch=ds.NO_FETCH)
        self.assertEqual(d, ds.DotSpace(server="matrix.org", link_via="matrix.org"))
        self.assertEqual((d.operator, d.cloud_origin, d.prefer_ipv4, d.source), ("", "", False, "none"))
        self.assertFalse(ds.is_dotspace("matrix.org", fetch=ds.NO_FETCH))
        self.assertEqual(ds.link_via_for("!r:matrix.org", fetch=ds.NO_FETCH), "matrix.org")
        self.assertEqual(ds.dotspace_for("", fetch=ds.NO_FETCH).server, "")
        self.assertEqual(ds.dotspace_for("[::1]:8448", fetch=ds.NO_FETCH).server, "::1")


class TestWellKnown(unittest.TestCase):
    def setUp(self):
        ds.reset_cache()

    def test_declaration_present(self):
        f = Fetcher(block())
        d = ds.dotspace_for("new.space", fetch=f, config=NONE)
        self.assertEqual(f.calls, [("https://new.space/.well-known/matrix/client", ds.WELL_KNOWN_TIMEOUT_S)])
        self.assertEqual((d.server, d.operator, d.gateway_host, d.link_via, d.cloud_origin,
                          d.mcp_url, d.prefer_ipv4, d.source),
                         ("new.space", "ag2", "chat.new.space", "new.space", "https://sutando.new.space",
                          "https://mcp.new.space/mcp", True, "well-known"))

    def test_gateway_host_serving_the_block_names_its_server(self):
        d = ds.dotspace_for("chat.new.space", fetch=Fetcher(block(server="new.space", link_via="new.space")),
                            config=NONE)
        self.assertEqual((d.server, d.link_via, d.source), ("new.space", "new.space", "well-known"))

    def test_absent_malformed_foreign_and_failing_fall_through(self):
        cases = {
            "absent": Fetcher(json.dumps({"m.homeserver": {"base_url": "https://new.space"}})),
            "malformed": Fetcher("{not json"),
            "not-ag2": Fetcher(block(operator="someone-else")),
            "claims-another-server": Fetcher(block(server="ag2.space")),
            "timeout": Fetcher(exc=TimeoutError("slow")),
            "oserror": Fetcher(exc=OSError("down")),
            "empty": Fetcher(None),
        }
        for name, f in cases.items():
            ds.reset_cache()
            d = ds.dotspace_for("new.space", fetch=f, config=NONE)
            self.assertEqual(d.source, "none", name)
            self.assertFalse(d.is_dotspace, name)

    def test_fallthrough_reaches_config_and_token(self):
        cfg = {"instances": [{"server": "new.space", "cloud_origin": "https://c.new.space"}]}
        d = ds.dotspace_for("new.space", fetch=Fetcher(exc=TimeoutError()), config=cfg)
        self.assertEqual((d.source, d.cloud_origin), ("config", "https://c.new.space"))
        env = {"REMOTE_TASK_TOKEN": "https://gw.example|secret"}
        d = ds.dotspace_for("gw.example", fetch=Fetcher("{}"), config=NONE, env=env)
        self.assertEqual((d.source, d.operator, d.prefer_ipv4, d.gateway_host), ("token", "ag2", True, "gw.example"))
        self.assertFalse(ds.dotspace_for("other.example", fetch=Fetcher("{}"), config=NONE, env=env).is_dotspace)

    def test_config_wins_without_a_fetch_and_results_are_cached(self):
        f = Fetcher(block())
        ds.dotspace_for("ag2.space", fetch=f)
        self.assertEqual(f.calls, [], "a configured instance costs no network round trip")
        ds.dotspace_for("new.space", fetch=f, config=NONE)
        ds.dotspace_for("new.space", fetch=f, config=NONE)
        self.assertEqual(len(f.calls), 1, "one fetch per host, then the cache answers")
        self.assertEqual(ds.dotspace_for("new.space", fetch=ds.NO_FETCH, config=NONE).source, "well-known")
        ds.reset_cache()
        self.assertEqual(ds.dotspace_for("new.space", fetch=ds.NO_FETCH, config=NONE).source, "none")

    def test_parse_well_known_server_claim_rule(self):
        self.assertIsNone(ds.parse_well_known(block(server="evil.example"), "new.space"))
        self.assertEqual(ds.parse_well_known(block(server="new.space"), "chat.new.space")["server"], "new.space")
        self.assertIsNone(ds.parse_well_known("[]", "new.space"))
        self.assertIsNone(ds.parse_well_known(json.dumps({ds.WELL_KNOWN_KEY: "str"}), "new.space"))
        # A parent claim: dotted, and the fetched host exactly one label below it.
        self.assertIsNone(ds.parse_well_known(block(server="space", gateway_url="", cloud_origin="", mcp_url=""),
                                              "attacker.space"))
        self.assertIsNone(ds.parse_well_known(block(server="new.space"), "a.b.new.space"))
        self.assertIsNotNone(ds.parse_well_known(block(server="new.space"), "b.new.space"))

    def test_block_hosts_must_sit_under_the_claimed_server(self):
        ok = ds.parse_well_known(block(), "new.space")
        self.assertEqual((ok["gateway_host"], ok["cloud_origin"]), ("chat.new.space", "https://sutando.new.space"))
        for field, value in [("gateway_url", "https://chat.other.space"), ("link_via", "other.space"),
                             ("cloud_origin", "https://sutando.ag2.space"), ("mcp_url", "https://mcp.evil.example/x")]:
            self.assertIsNone(ds.parse_well_known(block(**{field: value}), "new.space"), field)
        self.assertIsNotNone(ds.parse_well_known(block(cloud_origin="", mcp_url=""), "new.space"))

    def test_fetch_refuses_redirects_and_runs_inside_the_gateway_context(self):
        import urllib.error
        h = ds._NoRedirect()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            h.redirect_request(mock.Mock(full_url="https://new.space/.well-known/matrix/client"), None, 302,
                               "Found", {}, "https://elsewhere.example/")
        cm.exception.close()
        seen = []

        class Opener:
            def open(self, req, timeout):
                seen.append((req.full_url, timeout))
                raise OSError("offline")
        with mock.patch.object(ds.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(OSError):
                _REAL_FETCH("https://new.space/.well-known/matrix/client", 1.5)
        self.assertEqual(seen, [("https://new.space/.well-known/matrix/client", 1.5)])
        self.assertIsNotNone(ds._gateway_call("https://x").__enter__)

    def test_concurrent_callers_share_one_fetch_and_one_answer(self):
        import threading
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow(url, timeout):
            calls.append(url)
            started.set()
            release.wait(5)
            return block()
        results = []
        t1 = threading.Thread(target=lambda: results.append(ds.dotspace_for("new.space", fetch=slow, config=NONE)))
        t1.start()
        self.assertTrue(started.wait(5))
        t2 = threading.Thread(target=lambda: results.append(ds.dotspace_for("new.space", fetch=slow, config=NONE)))
        t2.start()
        release.set()
        t1.join(5); t2.join(5)
        self.assertEqual(len(calls), 1, "the second caller waited for the first fetch")
        self.assertEqual([r.source for r in results], ["well-known", "well-known"])

    def test_a_lookup_made_during_a_fetch_never_fetches(self):
        inner = []

        def reentrant(url, timeout):
            inner.append(ds.dotspace_for("new.space", fetch=Fetcher(block()), config=NONE).source)
            return block()
        self.assertEqual(ds.dotspace_for("new.space", fetch=reentrant, config=NONE).source, "well-known")
        self.assertEqual(inner, ["none"])


class TestPerServer(unittest.TestCase):
    """One install, two DotSpaces at once: each server resolves on its own."""

    def test_two_servers_resolve_independently_in_one_process(self):
        a = ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH)
        b = ds.dotspace_for("s.space", fetch=ds.NO_FETCH)
        self.assertEqual((a.link_via, a.cloud_origin), ("ag2.space", "https://sutando.ag2.space"))
        self.assertEqual((b.link_via, b.cloud_origin), ("s.space", "https://sutando.s.space"))
        self.assertEqual(ds.link_via_for("!r:ag2.space", fetch=ds.NO_FETCH), "ag2.space")
        self.assertEqual(ds.link_via_for("!r:s.space", fetch=ds.NO_FETCH), "s.space")
        self.assertEqual(ds.cloud_origin_for("@a:ag2.space", fetch=ds.NO_FETCH), "https://sutando.ag2.space")
        self.assertEqual(ds.cloud_origin_for("@a:s.space", fetch=ds.NO_FETCH), "https://sutando.s.space")
        import ag2space_links as links
        self.assertEqual(links.room_link("!r:ag2.space"), "https://matrix.to/#/!r:ag2.space?via=ag2.space")
        self.assertEqual(links.room_link("!r:s.space", "$e"), "https://matrix.to/#/!r:s.space/$e?via=s.space")
        self.assertEqual(links.room_link("!r:matrix.org"), "https://matrix.to/#/!r:matrix.org?via=matrix.org")
        self.assertEqual(links.room_link("!r:s.space", via=""), "https://matrix.to/#/!r:s.space")
        self.assertEqual(links.room_link("!r:s.space", via="x.y"), "https://matrix.to/#/!r:s.space?via=x.y")

    def test_current_server_reads_identity_then_token_then_url(self):
        self.assertEqual(ds.current_server({"AGENT_MXID": "@a:s.space", "REMOTE_TASK_TOKEN": "https://chat.ag2.space|s"}),
                         "s.space")
        self.assertEqual(ds.current_server({"AG2SPACE_USER_ID": "@a:dev.s.space"}), "dev.s.space")
        self.assertEqual(ds.current_server({"REMOTE_TASK_TOKEN": "https://chat.s.space|s"}), "chat.s.space")
        self.assertEqual(ds.current_server({"REMOTE_TASK_URL": "https://dev-chat.ag2.space"}), "dev-chat.ag2.space")
        self.assertEqual(ds.current_server({"REMOTE_TASK_TOKEN": "bare-secret"}), "")
        self.assertEqual(ds.current_server({}), "")

    def test_install_defaults_follow_the_install_server(self):
        self.assertEqual(ds.default_cloud_origin({"AGENT_MXID": "@a:s.space"}), "https://sutando.s.space")
        self.assertEqual(ds.default_link_via({"AGENT_MXID": "@a:s.space"}), "s.space")
        self.assertEqual(ds.default_cloud_origin({"REMOTE_TASK_TOKEN": "https://chat.s.space|x"}),
                         "https://sutando.s.space")
        # An unknown install falls back to the legacy instance, never to "".
        self.assertEqual(ds.default_cloud_origin({}), "https://sutando.ag2.space")
        self.assertEqual(ds.default_link_via({}), "ag2.space")
        self.assertEqual(ds.default_cloud_origin({"AGENT_MXID": "@a:matrix.org"}), "https://sutando.ag2.space")

    def test_install_defaults_never_fetch(self):
        f = Fetcher(block())
        with mock.patch.object(ds, "default_fetch", f):
            ds.reset_cache()
            ds.default_cloud_origin({"AGENT_MXID": "@a:new.space"})
            ds.default_link_via({"AGENT_MXID": "@a:new.space"})
            import ag2space_links as links
            links.room_link("!r:new.space")
        self.assertEqual(f.calls, [])


class TestAg2SpaceInstallUnchanged(unittest.TestCase):
    """An ag2.space install sees exactly the literals it saw before the resolver."""

    def test_callers_reproduce_todays_literals(self):
        env = {"AGENT_MXID": "@a:ag2.space"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(ds.current_server(), "ag2.space")
            self.assertEqual(ds.default_cloud_origin(), "https://sutando.ag2.space")
            self.assertTrue({"sutando.ag2.ai", "sutando.ag2.space"} <= ds.trusted_cloud_hosts())
            import ag2space_links as links
            self.assertEqual(links.DEFAULT_VIA, "ag2.space")
            self.assertEqual(links.room_link("!r:ag2.space"), "https://matrix.to/#/!r:ag2.space?via=ag2.space")
            self.assertEqual(links.room_link("noserver"), "https://matrix.to/#/noserver?via=ag2.space")
            cloud_auth = importlib.reload(importlib.import_module("cloud_auth"))
            self.assertEqual(cloud_auth.DEFAULT_CLOUD_ORIGIN, "https://sutando.ag2.space")
            self.assertEqual(cloud_auth.TRUSTED_API_HOSTS & {"sutando.ag2.ai", "sutando.ag2.space"},
                             {"sutando.ag2.ai", "sutando.ag2.space"})
            self.assertEqual(cloud_auth.normalize_base("https://sutando.ag2.ai"), "https://sutando.ag2.space")
            self.assertEqual(cloud_auth.RETIRED_CLOUD_ORIGINS, ("https://sutando.ag2.ai",))
            self.assertEqual(_load("rh_ds", "src/runtime-health.py")._STATION_GATEWAY_HOST, "sutando.ag2.space")
            ru = _load("report_usage_ds", "skills/skill-usage-report/scripts/report-usage.py")
            self.assertEqual(ru.CLOUD_FALLBACK, "https://sutando.ag2.space")
            self.assertEqual(ru.CLOUD, "https://sutando.ag2.space")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(links.DEFAULT_VIA, "ag2.space", "no identity at all: the legacy via")

    def test_s_space_install_moves_every_default(self):
        with mock.patch.dict("os.environ", {"AGENT_MXID": "@a:s.space"}, clear=True):
            import ag2space_links as links
            self.assertEqual(links.DEFAULT_VIA, "s.space")
            cloud_auth = importlib.reload(importlib.import_module("cloud_auth"))
            self.assertEqual(cloud_auth.DEFAULT_CLOUD_ORIGIN, "https://sutando.s.space")
            self.assertIn("sutando.s.space", cloud_auth.TRUSTED_API_HOSTS)
            self.assertIn("sutando.ag2.space", cloud_auth.TRUSTED_API_HOSTS)
        importlib.reload(importlib.import_module("cloud_auth"))

    def test_bridge_ipv4_preference_asks_the_resolver_offline(self):
        src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()
        self.assertIn("_prefer_v4_for(host)", src)
        self.assertNotIn('"ag2.space" in str(host)', src)
        self.assertTrue(ds.is_dotspace("chat.ag2.space", fetch=ds.NO_FETCH))
        self.assertTrue(ds.is_dotspace("chat.s.space", fetch=ds.NO_FETCH))
        self.assertFalse(ds.is_dotspace("api.anthropic.com", fetch=ds.NO_FETCH))

    def test_cron_notify_derives_via_from_the_linked_room(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cron_notify_ds", REPO / "skills/schedule-crons/cron-notify.py")
        cn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cn)
        self.assertEqual(cn.deep_link("!r:s.space", "$e"), "https://matrix.to/#/!r:s.space/$e?via=s.space")
        self.assertEqual(cn.deep_link("!r:ag2.space"), "https://matrix.to/#/!r:ag2.space?via=ag2.space")
        self.assertEqual(cn.deep_link("!r:ag2.space", via=""), "https://matrix.to/#/!r:ag2.space")
        self.assertIn("?via=s.space", cn.format_ping("c", "s", "!r:s.space"))

    def test_cron_notify_via_flag_defaults_to_the_linked_rooms_server(self):
        import contextlib
        import io
        import tempfile
        cn = _load("cron_notify_cli_ds", "skills/schedule-crons/cron-notify.py")
        with tempfile.TemporaryDirectory() as td:
            base = ["--cron", "c", "--summary", "broke", "--kind", "error", "--room", "!dest:ag2.space",
                    "--state-file", str(Path(td) / "s.json"), "--dry-run"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cn.main(base + ["--link-room", "!detail:s.space", "--event-id", "$e"]), 0)
            self.assertIn("https://matrix.to/#/!detail:s.space/$e?via=s.space", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cn.main(base), 0)
            self.assertIn("!dest:ag2.space?via=ag2.space", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cn.main(base + ["--via", ""]), 0)
            self.assertIn("!dest:ag2.space\n", out.getvalue() + "\n")
            self.assertNotIn("?via=", out.getvalue())

    def test_bridge_prefers_v4_only_for_a_dotspace_asking_for_it(self):
        src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()
        self.assertIn("_prefer_v4_for(host)", src)
        self.assertIn("_dotspace_for(name, fetch=_NO_FETCH)", src)
        self.assertIn("d.operator and d.prefer_ipv4", src)
        self.assertIn('host.decode("ascii", "replace") if isinstance(host, bytes)', src)
        cfg = {"instances": [{"server": "v6.space", "prefer_ipv4": False}, {"server": "v4.space"}]}
        self.assertFalse(ds.dotspace_for("v6.space", fetch=ds.NO_FETCH, config=cfg).prefer_ipv4)
        self.assertTrue(ds.dotspace_for("v4.space", fetch=ds.NO_FETCH, config=cfg).prefer_ipv4)


if __name__ == "__main__":
    unittest.main()
