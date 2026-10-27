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

    def test_registered_instances_enumerates_the_registry_once_each_without_fetching(self):
        defaults = ds.registered_instances()
        self.assertEqual([d.server for d in defaults], [e["server"] for e in ds.DEFAULT_INSTANCES])
        self.assertTrue(all(d.is_dotspace and d.source == "config" for d in defaults))
        cfg = {"instances": [{"server": "x.space", "gateway_host": "gw.x.space"},
                             {"server": "X.space"}, {"server": ""}, {"server": "y.space"}, "junk"]}
        listed = ds.registered_instances(cfg)
        self.assertEqual([(d.server, d.gateway_host) for d in listed], [("x.space", "gw.x.space"), ("y.space", "")],
                         "a configured list replaces the default, one entry per server, malformed ones dropped")
        self.assertEqual(ds.registered_instances({"instances": []}), [])

    def test_configured_section_replaces_the_default(self):
        cfg = {"instances": [{"server": "x.space", "gateway_host": "gw.x.space",
                              "cloud_origin": "https://c.x.space", "prefer_ipv4": False}]}
        d = ds.dotspace_for("gw.x.space", fetch=ds.NO_FETCH, config=cfg)
        self.assertEqual((d.server, d.cloud_origin, d.prefer_ipv4, d.source),
                         ("x.space", "https://c.x.space", False, "config"))
        self.assertFalse(ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH, config=cfg).is_dotspace)
        self.assertEqual(ds.trusted_cloud_hosts(cfg), frozenset({"c.x.space", "sutando.ag2.space", ds.LEGACY_CLOUD_HOST}))
        self.assertFalse(ds.dotspace_for("not-ag2.example", fetch=Fetcher(block(server="not-ag2.example",
                         gateway_url="https://gw.not-ag2.example", cloud_origin="", mcp_url="")), config=cfg).is_dotspace)

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

    def test_a_plain_server_links_via_itself_with_its_port(self):
        d = ds.dotspace_for("!r:localhost:8448", fetch=ds.NO_FETCH)
        self.assertEqual((d.server, d.link_via, d.source), ("localhost", "localhost:8448", "none"))
        self.assertEqual(ds.link_via_for("!r:localhost:8448", fetch=ds.NO_FETCH), "localhost:8448")
        self.assertEqual(ds.dotspace_for("[::1]:8448", fetch=ds.NO_FETCH).link_via, "[::1]:8448")
        self.assertEqual(ds.dotspace_for("Matrix.ORG.", fetch=ds.NO_FETCH).link_via, "matrix.org")
        import ag2space_links as links
        self.assertEqual(links.room_link("!r:localhost:8448"), "https://matrix.to/#/!r:localhost:8448?via=localhost:8448")

    def test_prefer_ipv4_is_parsed_strictly(self):
        for value, want in [(True, True), (False, False), ("true", True), ("False", False), (" TRUE ", True),
                            ("yes", True), ("0", True), (0, True), (1, True), (None, True), ([], True)]:
            cfg = {"instances": [{"server": "p.space", "prefer_ipv4": value}]}
            self.assertEqual(ds.dotspace_for("p.space", fetch=ds.NO_FETCH, config=cfg).prefer_ipv4, want, repr(value))
        self.assertTrue(ds.dotspace_for("p.space", fetch=ds.NO_FETCH, config={"instances": [{"server": "p.space"}]}).prefer_ipv4)
        for value, want in [(False, False), ("false", False), ("maybe", True), (0, True)]:
            self.assertEqual(ds.parse_well_known(block(prefer_ipv4=value), "new.space")["prefer_ipv4"], want, repr(value))

    def test_a_name_that_could_carry_a_path_userinfo_or_space_is_no_server(self):
        """Nothing is fetched for it and it never reaches a `?via=`: a room id is attacker-chosen."""
        import ag2space_links as links
        for bad in ("!a:127.0.0.1/admin?x=", "!a:evil.com@169.254.169.254", "!a:evil.com#frag",
                    "!a:two words", "chat.ag2.space/x", "@u:ag2.space/../y"):
            f = Fetcher(block())
            ds.reset_cache()
            d = ds.dotspace_for(bad, fetch=f, config=NONE)
            self.assertEqual(f.calls, [], bad)
            self.assertEqual(d, ds.DotSpace(server=""), bad)
            self.assertFalse(ds.is_dotspace(bad, fetch=f), bad)
            self.assertEqual(ds.link_via_for(bad, fetch=f), "", bad)
            self.assertEqual(f.calls, [], bad)
            if bad.startswith(("!", "@")):
                self.assertEqual(links.room_link(bad), f"https://matrix.to/#/{bad}", bad)
        self.assertEqual(ds.dotspace_for("https://evil.com@169.254.169.254/", fetch=ds.NO_FETCH).server,
                         "169.254.169.254", "a URL's userinfo is parsed off, not a host")

    def test_only_server_name_characters_pass(self):
        """An allowlist, not a denylist: `& ; " % _` and a scheme never reach a fetch or a `?via=`."""
        for bad in ("!a:evil.com&x=1", "!a:evil.com;x", '!a:ev"il.com', "!a:evil.com%2fadmin",
                    "!a:ag2_space", "!a:-ag2.space", "!a:ag2.space:", "!a:ag2.space:99999x", "!a:[::1"):
            f = Fetcher(block())
            ds.reset_cache()
            self.assertEqual(ds.dotspace_for(bad, fetch=f, config=NONE), ds.DotSpace(server=""), bad)
            self.assertEqual(f.calls, [], bad)
        for good, via in (("localhost:8448", "localhost:8448"), ("[::1]:8448", "[::1]:8448"),
                          ("127.0.0.1", "127.0.0.1"), ("a-b.example", "a-b.example")):
            d = ds.dotspace_for(good, fetch=ds.NO_FETCH, config=NONE)
            self.assertEqual(d.link_via or d.server, via, good)

    def test_port_range_and_ipv6_literal_are_checked(self):
        for bad in ("!a:ag2.space:65536", "!a:ag2.space:0", "!a:[:::]", "!a:[1.2.3.4]", "!a:[::1]:70000"):
            ds.reset_cache()
            self.assertEqual(ds.dotspace_for(bad, fetch=ds.NO_FETCH, config=NONE), ds.DotSpace(server=""), bad)
        self.assertTrue(ds.is_server_name("[::ffff:1.2.3.4]:8448"))
        self.assertTrue(ds.is_server_name("ag2.space:65535"))
        self.assertFalse(ds.is_server_name(""))

    def test_well_known_values_pass_the_same_allowlist_or_the_block_is_refused(self):
        """A registered operator's block must not smuggle a query, a second host or a userinfo
        trick into `?via=`, the relay URL, the Cloud origin or the MCP URL."""
        for field, value in [("link_via", "evil&x=1.new.space"), ("link_via", "a via.new.space"),
                             ("gateway_url", "https://g&h.new.space"), ("gateway_url", "https://u@chat.new.space"),
                             ("mcp_url", "https://evil.com\\@m.new.space/mcp"), ("mcp_url", "ftp://mcp.new.space/x"),
                             ("cloud_origin", "https://u:p@sutando.new.space"), ("cloud_origin", "https://sutando.new.space:0")]:
            self.assertIsNone(ds.parse_well_known(block(**{field: value}), "new.space"), (field, value))
        ok = ds.parse_well_known(block(link_via="chat.new.space:8448", gateway_url="https://chat.new.space:8448/relay"), "new.space")
        self.assertEqual((ok["link_via"], ok["gateway_host"]), ("chat.new.space:8448", "chat.new.space"),
                         "a declared via port stays, as a typed server's does")
        # mcp_url is dialled: https only, no query or fragment; the display-only fields keep http.
        for value in ("http://mcp.new.space/mcp", "https://mcp.new.space/mcp?x=1", "https://mcp.new.space/mcp#f"):
            self.assertIsNone(ds.parse_well_known(block(mcp_url=value), "new.space"), value)
        self.assertIsNotNone(ds.parse_well_known(block(gateway_url="http://chat.new.space/relay?x=1"), "new.space"))

    def test_control_characters_are_refused_before_the_url_is_parsed(self):
        """urlsplit silently drops tab, CR and LF; a value that needs that to parse is refused as
        written, and nothing with a control character or a space is ever stored."""
        for field, value in [("mcp_url", "https://m.ne\tw.space/mcp"), ("mcp_url", "https://mcp.new.space/mcp\r\nHost: evil.com"),
                             ("cloud_origin", "https://sut\nando.new.space"), ("gateway_url", "https://chat.new.space/re lay"),
                             ("gateway_url", "https://chat.new.space/\x00"), ("mcp_url", "https://mcp.new.space/\x7f")]:
            self.assertIsNone(ds.parse_well_known(block(**{field: value}), "new.space"), (field, value))
        ok = ds.parse_well_known(block(cloud_origin="https://Sutando.new.space/api/"), "new.space")
        self.assertEqual(ok["cloud_origin"], "https://sutando.new.space", "an origin is scheme and host only")
        self.assertIsNone(ds.parse_well_known(block(cloud_origin="http://sutando.new.space"), "new.space"),
                          "the Cloud origin is dialled with a bearer: https only")

    def test_a_declared_via_port_survives_every_resolution_path(self):
        cfg = {"instances": [{"server": "x.space", "link_via": "via.x.space:8448"}]}
        ds.reset_cache()
        self.assertEqual(ds.link_via_for("!r:x.space", fetch=ds.NO_FETCH, config=cfg), "via.x.space:8448")
        ds.reset_cache()
        self.assertEqual(ds.dotspace_for("x.space", fetch=ds.NO_FETCH, config={"instances": [{"server": "x.space", "link_via": "bad&via"}]}).link_via,
                         "x.space", "a config via that is not a server name falls back to the server")
        ds.reset_cache()
        d = ds.dotspace_for("new.space", fetch=Fetcher(block(server="new.space", link_via="via.new.space:8448")), config=NONE)
        self.assertEqual((d.source, d.link_via), ("well-known-unverified", "via.new.space:8448"))

    def test_shorthand_ipv4_and_hex_labels_are_never_fetched(self):
        for name in ("127.1", "0x7f.1", "127.0.0.01", "10.1", "!r:0x7f000001", "1.2.3"):
            f = Fetcher(block())
            ds.reset_cache()
            ds.dotspace_for(name, fetch=f, config=NONE)
            self.assertEqual(f.calls, [], name)
        for field, value in [("mcp_url", "https://mcp.new.space/mcp?"), ("mcp_url", "https://mcp.new.space/mcp#"),
                             ("gateway_url", "https://chat.new.space/re\u00a0lay"), ("mcp_url", "https://mcp.new.space/\u2028"),
                             ("cloud_origin", "https://sutando.new.space/\x85")]:
            self.assertIsNone(ds.parse_well_known(block(**{field: value}), "new.space"), (field, value))

    def test_ip_literals_and_single_labels_are_never_fetched(self):
        for name in ("[::1]:8448", "127.0.0.1", "localhost:8448", "!r:10.0.0.1", "@u:[fe80::1]"):
            f = Fetcher(block())
            ds.reset_cache()
            d = ds.dotspace_for(name, fetch=f, config=NONE)
            self.assertEqual(f.calls, [], name)
            self.assertEqual(d.source, "none", name)
        for bad in ("!a:ag2.space:08448", "!a:ag2.space:0"):
            self.assertEqual(ds.dotspace_for(bad, fetch=ds.NO_FETCH, config=NONE), ds.DotSpace(server=""), bad)
        # Through the registered path: the refused block fills nothing, the config entry stands.
        cfg = {"instances": [{"server": "reg.space"}]}
        ds.reset_cache()
        d = ds.dotspace_for("chat.reg.space", fetch=Fetcher(block(server="reg.space", link_via="evil&x=1.reg.space")), config=cfg)
        self.assertEqual((d.source, d.link_via, d.gateway_host), ("config", "reg.space", ""))

    def test_an_unregistered_parent_claim_is_refused(self):
        """x.co.uk's well-known may not declare co.uk; the same claim stands once co.uk is registered."""
        claim = block(server="co.uk", gateway_url="https://chat.co.uk", cloud_origin="", mcp_url="")
        f = Fetcher(claim)
        ds.reset_cache()
        d = ds.dotspace_for("x.co.uk", fetch=f, config=NONE)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual((d.server, d.source, d.gateway_host), ("x.co.uk", "none", ""))
        ds.reset_cache()
        d = ds.dotspace_for("x.co.uk", fetch=Fetcher(claim), config={"instances": [{"server": "co.uk"}]})
        self.assertEqual((d.server, d.source, d.gateway_host), ("co.uk", "config", "chat.co.uk"))
        ds.reset_cache()
        own = ds.dotspace_for("new.space", fetch=Fetcher(block(server="new.space")), config=NONE)
        self.assertEqual(own.source, "well-known-unverified", "a host declaring itself still counts")


class TestWellKnown(unittest.TestCase):
    def setUp(self):
        ds.reset_cache()

    def test_declaration_alone_never_makes_a_dotspace(self):
        """HTTPS proves control of a domain, not AG2 operation: an unregistered server with an
        `operator: ag2` block is discovered, not trusted."""
        f = Fetcher(block())
        d = ds.dotspace_for("new.space", fetch=f, config=NONE)
        self.assertEqual(f.calls, [("https://new.space/.well-known/matrix/client", ds.WELL_KNOWN_TIMEOUT_S)])
        self.assertEqual((d.server, d.operator, d.gateway_host, d.link_via, d.cloud_origin,
                          d.mcp_url, d.prefer_ipv4, d.source),
                         ("new.space", "", "chat.new.space", "new.space", "", "https://mcp.new.space/mcp",
                          False, "well-known-unverified"))
        self.assertFalse(d.is_dotspace)
        self.assertEqual(ds.cloud_origin_for("new.space", fetch=f, config=NONE), "")

    def test_registered_server_gains_capabilities_from_its_block(self):
        cfg = {"instances": [{"server": "new.space", "prefer_ipv4": False}]}
        ds.reset_cache()
        d = ds.dotspace_for("new.space", fetch=Fetcher(block(link_via="via.new.space")), config=cfg)
        self.assertEqual((d.operator, d.source, d.gateway_host, d.mcp_url, d.link_via, d.prefer_ipv4, d.cloud_origin),
                         ("ag2", "config", "chat.new.space", "https://mcp.new.space/mcp", "via.new.space", False, ""),
                         "block fills gateway/mcp/link_via; config's prefer_ipv4 and the absent cloud_origin stand")
        ds.reset_cache()
        full = {"instances": [{"server": "new.space", "gateway_host": "gw.new.space", "link_via": "new.space"}]}
        d = ds.dotspace_for("new.space", fetch=Fetcher(block()), config=full)
        self.assertEqual((d.gateway_host, d.link_via, d.mcp_url), ("gw.new.space", "new.space", "https://mcp.new.space/mcp"))
        ds.reset_cache()
        d = ds.dotspace_for("new.space", fetch=Fetcher(block(server="other.space")), config=cfg)
        self.assertEqual(d.gateway_host, "", "a block for another server fills nothing")

    def test_an_unregistered_gateway_host_may_not_name_its_server(self):
        """The same shape as x.co.uk claiming co.uk: unregistered, the host is a plain server; once
        the server is registered the gateway host resolves to it, and a token for it still does."""
        claim = block(server="new.space", link_via="new.space")
        d = ds.dotspace_for("chat.new.space", fetch=Fetcher(claim), config=NONE)
        self.assertEqual((d.server, d.link_via, d.source), ("chat.new.space", "chat.new.space", "none"))
        ds.reset_cache()
        d = ds.dotspace_for("chat.new.space", fetch=Fetcher(claim), config={"instances": [{"server": "new.space"}]})
        self.assertEqual((d.server, d.gateway_host, d.source), ("new.space", "chat.new.space", "config"))
        ds.reset_cache()
        d = ds.dotspace_for("chat.new.space", fetch=Fetcher(claim), config=NONE, own_gateway="https://chat.new.space")
        self.assertEqual((d.server, d.source), ("chat.new.space", "token"))

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
        own = "https://gw.example"
        d = ds.dotspace_for("gw.example", fetch=Fetcher("{}"), config=NONE, own_gateway=own)
        self.assertEqual((d.source, d.operator, d.prefer_ipv4, d.gateway_host), ("token", "ag2", True, "gw.example"))
        self.assertFalse(ds.dotspace_for("other.example", fetch=Fetcher("{}"), config=NONE, own_gateway=own).is_dotspace)
        self.assertFalse(ds.dotspace_for("gw.example", fetch=Fetcher("{}"), config=NONE).is_dotspace)

    def test_registered_lookup_without_fetch_costs_nothing_and_results_are_cached(self):
        f = Fetcher(block())
        self.assertEqual(ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH).source, "config")
        self.assertEqual(f.calls, [])
        ds.dotspace_for("new.space", fetch=f, config=NONE)
        ds.dotspace_for("new.space", fetch=f, config=NONE)
        self.assertEqual(len(f.calls), 1, "one fetch per host, then the cache answers")
        self.assertEqual(ds.dotspace_for("new.space", fetch=ds.NO_FETCH, config=NONE).source, "well-known-unverified")
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
        self.assertEqual([r.source for r in results], ["well-known-unverified"] * 2)

    def test_a_lookup_made_during_a_fetch_never_fetches(self):
        inner = []

        def reentrant(url, timeout):
            inner.append(ds.dotspace_for("new.space", fetch=Fetcher(block()), config=NONE).source)
            return block()
        self.assertEqual(ds.dotspace_for("new.space", fetch=reentrant, config=NONE).source, "well-known-unverified")
        self.assertEqual(inner, ["none"])


class TestPerServer(unittest.TestCase):
    """One install, two DotSpaces at once: each server resolves on its own."""

    def test_two_servers_resolve_independently_in_one_process(self):
        a = ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH)
        b = ds.dotspace_for("s.space", fetch=ds.NO_FETCH)
        self.assertEqual((a.link_via, a.cloud_origin), ("ag2.space", "https://sutando.ag2.space"))
        self.assertEqual((b.link_via, b.cloud_origin, b.is_dotspace), ("s.space", "", True))
        opt_in = {"instances": [{"server": "s.space", "cloud_origin": "https://sutando.s.space"}]}
        self.assertEqual(ds.cloud_origin_for("s.space", fetch=ds.NO_FETCH, config=opt_in), "https://sutando.s.space")
        self.assertEqual(ds.trusted_cloud_hosts(opt_in), frozenset({"sutando.s.space", "sutando.ag2.space", ds.LEGACY_CLOUD_HOST}))
        self.assertEqual(ds.link_via_for("!r:ag2.space", fetch=ds.NO_FETCH), "ag2.space")
        self.assertEqual(ds.link_via_for("!r:s.space", fetch=ds.NO_FETCH), "s.space")
        self.assertEqual(ds.cloud_origin_for("@a:ag2.space", fetch=ds.NO_FETCH), "https://sutando.ag2.space")
        self.assertEqual(ds.cloud_origin_for("@a:s.space", fetch=ds.NO_FETCH), "")
        self.assertEqual(ds.trusted_cloud_hosts(),
                         frozenset({"sutando.ag2.space", "dev-sutando.ag2.space", ds.LEGACY_CLOUD_HOST}))
        import ag2space_links as links
        self.assertEqual(links.room_link("!r:ag2.space"), "https://matrix.to/#/!r:ag2.space?via=ag2.space")
        self.assertEqual(links.room_link("!r:s.space", "$e"), "https://matrix.to/#/!r:s.space/$e?via=s.space")
        self.assertEqual(links.room_link("!r:matrix.org"), "https://matrix.to/#/!r:matrix.org?via=matrix.org")
        self.assertEqual(links.room_link("!r:s.space", via=""), "https://matrix.to/#/!r:s.space")
        self.assertEqual(links.room_link("!r:s.space", via="x.y"), "https://matrix.to/#/!r:s.space?via=x.y")

    def test_current_server_reads_identity_then_the_callers_gateway_never_token_variables(self):
        self.assertEqual(ds.current_server({"AGENT_MXID": "@a:s.space"}, own_gateway="https://chat.ag2.space"),
                         "s.space")
        self.assertEqual(ds.current_server({"AG2SPACE_USER_ID": "@a:dev.s.space"}), "dev.s.space")
        self.assertEqual(ds.current_server({}, own_gateway="https://chat.s.space"), "chat.s.space")
        self.assertEqual(ds.current_server({"REMOTE_TASK_TOKEN": "https://chat.s.space|s"}), "")
        self.assertEqual(ds.current_server({"REMOTE_TASK_URL": "https://dev-chat.ag2.space"}), "")
        self.assertEqual(ds.current_server({}), "")

    def test_install_defaults_follow_the_install_server(self):
        # Cloud is undecided for s.space (no cloud_origin): the production origin, as today.
        self.assertEqual(ds.default_cloud_origin({"AGENT_MXID": "@a:s.space"}), "https://sutando.ag2.space")
        self.assertEqual(ds.default_link_via({"AGENT_MXID": "@a:s.space"}), "s.space")
        self.assertEqual(ds.default_cloud_origin({"REMOTE_TASK_TOKEN": "https://chat.s.space|x"}),
                         "https://sutando.ag2.space")
        self.assertEqual(ds.default_cloud_origin({"AGENT_MXID": "@a:dev.ag2.space"}), "https://dev-sutando.ag2.space")
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
            self.assertEqual(ds.trusted_cloud_hosts(),
                             frozenset({"sutando.ag2.ai", "sutando.ag2.space", "dev-sutando.ag2.space"}))
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
            self.assertEqual(cloud_auth.DEFAULT_CLOUD_ORIGIN, "https://sutando.ag2.space",
                             "no per-instance Cloud for s.space: the production origin, as today")
            self.assertNotIn("sutando.s.space", cloud_auth.TRUSTED_API_HOSTS)
            self.assertIn("sutando.ag2.space", cloud_auth.TRUSTED_API_HOSTS)
        importlib.reload(importlib.import_module("cloud_auth"))

    def test_credentials_stay_bound_to_their_origin_on_every_instance(self):
        """A production credential (retired-origin or bare pre-scoping key) is never paired with
        another instance's Cloud, and the retired origin never normalises to one."""
        import cloud_auth
        prod, retired = "https://sutando.ag2.space", "https://sutando.ag2.ai"
        store = {cloud_auth.origin_vault_key(retired): "sutk_retired", "AG2_CLOUD_TOKEN": "sutk_bare"}
        cases = [({"AGENT_MXID": "@a:s.space"}, "https://sutando.s.space"),
                 ({"AGENT_MXID": "@a:dev.ag2.space"}, "https://dev-sutando.ag2.space")]
        for env, other in cases:
            with mock.patch.dict("os.environ", {**env, "AG2_CLOUD_ORIGIN": other}, clear=True):
                ca = importlib.reload(cloud_auth)
                self.assertEqual(ca.PRODUCTION_CLOUD_ORIGIN, prod)
                self.assertEqual(ca.normalize_base(retired), prod, env)
                self.assertNotEqual(ca.normalize_base(retired), other)
                found = list(ca.keychain_candidates(store.get))
                self.assertEqual(found, [(prod, "sutk_bare")], env)
                self.assertNotIn(other, [o for o, _ in found])
                scoped = {**store, ca.origin_vault_key(other): "sutk_other"}
                self.assertEqual(list(ca.keychain_candidates(scoped.get)), [(other, "sutk_other"), (prod, "sutk_bare")])
        with mock.patch.dict("os.environ", {"AGENT_MXID": "@a:ag2.space"}, clear=True):
            ca = importlib.reload(cloud_auth)
            self.assertEqual(list(ca.keychain_candidates(store.get)), [(prod, "sutk_retired"), (prod, "sutk_bare")])
        importlib.reload(cloud_auth)

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
        # The shipped hook itself: v4 first for a DotSpace host asking for it, untouched otherwise.
        import socket
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import remote_gateway_bridge as rgb
        self.addCleanup(setattr, socket, "getaddrinfo", rgb._getaddrinfo_prefer_v4._ag2_orig_getaddrinfo)
        self.assertTrue(rgb._prefer_v4_for("chat.ag2.space"))
        self.assertTrue(rgb._prefer_v4_for(b"chat.s.space"))
        self.assertFalse(rgb._prefer_v4_for("api.anthropic.com"))
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with mock.patch.object(rgb, "_resolve_bounded", return_value=[v6, v4]), \
                mock.patch.object(rgb, "_PREFER_V4", True):
            self.assertEqual(rgb._getaddrinfo_prefer_v4("chat.ag2.space", 443), [v4])
            self.assertEqual(rgb._getaddrinfo_prefer_v4("api.anthropic.com", 443), [v6, v4])


class SharedCloudAgreement(unittest.TestCase):
    """An instance that declares no Cloud uses the shared production Cloud, and the bearer
    allowlist names that host: the fallback and the allowlist never disagree."""

    MINIMAL = {"instances": [{"server": "s.space", "gateway_host": "chat.s.space"}]}
    # The legacy server registered, Cloud unset: the instances list must not empty the fallback.
    LEGACY_NO_CLOUD = {"instances": [{"server": "ag2.space", "gateway_host": "chat.ag2.space"}]}

    def test_the_shared_production_cloud_is_a_constant_not_an_instances_entry(self):
        self.assertEqual(ds.PRODUCTION_CLOUD_ORIGIN, "https://sutando.ag2.space")
        self.assertEqual(ds.legacy_dotspace(self.LEGACY_NO_CLOUD).cloud_origin, "")
        for env in ({"AGENT_MXID": "@a:ag2.space"}, {"AGENT_MXID": "@a:s.space"}, {}):
            self.assertEqual(ds.default_cloud_origin(env, config=self.LEGACY_NO_CLOUD), ds.PRODUCTION_CLOUD_ORIGIN, env)
        self.assertEqual(ds.trusted_cloud_hosts(self.LEGACY_NO_CLOUD), frozenset({"sutando.ag2.space", ds.LEGACY_CLOUD_HOST}))
        self.assertIn("sutando.ag2.space", ds.trusted_cloud_hosts({"instances": []}))
        # A legacy entry with its own Cloud still wins the fallback, and both hosts are allowlisted.
        moved = {"instances": [{"server": "ag2.space", "cloud_origin": "https://cloud.ag2.space"}]}
        self.assertEqual(ds.default_cloud_origin({"AGENT_MXID": "@a:s.space"}, config=moved), "https://cloud.ag2.space")
        self.assertEqual(ds.trusted_cloud_hosts(moved), frozenset({"cloud.ag2.space", "sutando.ag2.space", ds.LEGACY_CLOUD_HOST}))
        import cloud_auth
        self.assertIs(cloud_auth.PRODUCTION_CLOUD_ORIGIN, ds.PRODUCTION_CLOUD_ORIGIN)

    def test_only_s_space_registered_and_no_cloud_uses_the_shared_production_cloud(self):
        env = {"AGENT_MXID": "@a:s.space"}
        origin = ds.default_cloud_origin(env, config=self.MINIMAL)
        self.assertEqual(origin, "https://sutando.ag2.space")
        self.assertIn(ds.server_of_gateway_url(origin), ds.trusted_cloud_hosts(self.MINIMAL))
        self.assertEqual(ds.cloud_origin_for("s.space", fetch=ds.NO_FETCH, config=self.MINIMAL), "")

    def test_the_fallback_host_is_always_allowlisted(self):
        for cfg in (self.MINIMAL, {"instances": []}, None,
                    {"instances": [{"server": "s.space", "cloud_origin": "https://sutando.s.space"}]},
                    {"instances": [{"server": "ag2.space", "gateway_host": "chat.ag2.space"}]}):
            for env in ({"AGENT_MXID": "@a:s.space"}, {"AGENT_MXID": "@a:ag2.space"}, {}):
                origin = ds.default_cloud_origin(env, config=cfg)
                self.assertTrue(origin, (cfg, env))
                self.assertIn(ds.server_of_gateway_url(origin), ds.trusted_cloud_hosts(cfg), (cfg, env))


class TestFetchAndHelpers(unittest.TestCase):
    """The real fetch and the helpers the hot paths bypass: each branch runs at least once."""

    def setUp(self):
        ds.reset_cache()

    def test_url_parsing_failures_and_the_no_fetch_stub(self):
        self.assertEqual(ds.server_of_gateway_url("https://[::1"), "", "urlsplit's ValueError reads as no host")
        self.assertIsNone(ds.NO_FETCH("https://new.space/.well-known/matrix/client", 1.5))

    def test_the_fetch_runs_inside_the_packages_gateway_context_when_present(self):
        import contextlib
        import types
        seen = []
        fake = types.ModuleType("gateway_instance")

        @contextlib.contextmanager
        def gateway_call(url):
            seen.append(url)
            yield
        fake.gateway_call = gateway_call
        with mock.patch.dict(sys.modules, {"gateway_instance": fake}):
            with ds._gateway_call("https://x.example/w"):
                pass
        self.assertEqual(seen, ["https://x.example/w"])

    def test_an_absent_config_loader_means_the_built_in_default(self):
        with mock.patch.dict(sys.modules, {"sutando_config": None}):
            self.assertIsNone(ds._loaded_section())
            self.assertTrue(ds.dotspace_for("ag2.space", fetch=ds.NO_FETCH).is_dotspace)

    def test_a_waiter_on_the_fetch_slot_reads_the_answer_the_fetch_cached(self):
        import threading
        import time
        real, entering, results = threading.Lock(), threading.Event(), []

        class Slot:
            def __enter__(self):
                entering.set()
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)
        f = Fetcher(block())
        with mock.patch.object(ds, "_fetch_slot", Slot()):
            real.acquire()  # a fetch for new.space is in flight
            t = threading.Thread(target=lambda: results.append(ds.dotspace_for("new.space", fetch=f, config=NONE)))
            t.start()
            self.assertTrue(entering.wait(5), "the waiter missed the cache and reached the slot")
            with ds._cache_lock:  # the in-flight fetch lands its answer, then releases the slot
                ds._cache["new.space"] = (time.monotonic() + 60, ds.parse_well_known(block(), "new.space"))
            real.release()
            t.join(5)
        self.assertEqual(f.calls, [], "the waiter never fetched again")
        self.assertEqual([r.source for r in results], ["well-known-unverified"])

    def test_default_fetch_reads_200_drops_other_statuses_and_refuses_a_redirect(self):
        """Against a real local server, so dropping `_NoRedirect()` would fail here: the 302's
        target is never requested and the redirecting answer is no document."""
        import http.server
        import threading
        import urllib.error
        requested = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                elif self.path == "/empty":
                    self.send_response(204)
                    self.end_headers()
                else:
                    body = block().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            text = _REAL_FETCH(base + "/.well-known/matrix/client", 5)
            self.assertEqual(ds.parse_well_known(text, "new.space")["server"], "new.space")
            self.assertIsNone(_REAL_FETCH(base + "/empty", 5))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                _REAL_FETCH(base + "/redirect", 5)
            cm.exception.close()
            self.assertEqual(cm.exception.code, 302)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(requested, ["/.well-known/matrix/client", "/empty", "/redirect"])
        self.assertNotIn("/target", requested)


if __name__ == "__main__":
    unittest.main()
