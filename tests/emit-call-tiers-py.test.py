"""Tests for src/emit_call_tiers.py — parity with tests/call-tiers-emit.test.ts."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import emit_call_tiers as ect


class TestPrivateLanIpv4(unittest.TestCase):
    def test_rfc1918_ranges(self):
        for ip in ("10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.5"):
            self.assertTrue(ect.is_private_lan_ipv4(ip), ip)

    def test_excluded_ranges(self):
        for ip in ("127.0.0.1", "169.254.1.1", "100.101.1.2", "8.8.8.8",
                   "172.32.0.1", "172.15.0.1", "192.169.0.1"):
            self.assertFalse(ect.is_private_lan_ipv4(ip), ip)

    def test_malformed(self):
        for ip in ("", "1.2.3", "a.b.c.d", "10.0.0.999", "10.0.0.-1"):
            self.assertFalse(ect.is_private_lan_ipv4(ip), ip)


class TestParseIfconfig(unittest.TestCase):
    IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.76.47.3 --> 100.76.47.3 netmask 0xffffffff
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
"""

    def test_picks_private_lan_only(self):
        self.assertEqual(ect.parse_ifconfig_lan_ipv4(self.IFCONFIG), "192.168.1.23")

    def test_none_when_no_private(self):
        self.assertIsNone(ect.parse_ifconfig_lan_ipv4("lo0:\n\tinet 127.0.0.1\n"))

    def test_lowest_named_interface_wins(self):
        text = ("en5:\n\tinet 10.0.0.5 netmask 0xff000000\n"
                "en0:\n\tinet 192.168.1.9 netmask 0xffffff00\n")
        self.assertEqual(ect.parse_ifconfig_lan_ipv4(text), "192.168.1.9")


class TestParseTailnetHost(unittest.TestCase):
    def test_prefers_magicdns_and_strips_dot(self):
        s = {"Self": {"Online": True, "DNSName": "mbp.taila1a7c4.ts.net.",
                      "TailscaleIPs": ["100.1.2.3"]}}
        self.assertEqual(ect.parse_tailnet_host(s), "mbp.taila1a7c4.ts.net")

    def test_falls_back_to_100x_ip(self):
        s = {"Self": {"Online": True, "DNSName": "", "TailscaleIPs": ["fd7a::1", "100.1.2.3"]}}
        self.assertEqual(ect.parse_tailnet_host(s), "100.1.2.3")

    def test_offline_node_advertises_nothing(self):
        s = {"Self": {"Online": False, "DNSName": "mbp.ts.net."}}
        self.assertIsNone(ect.parse_tailnet_host(s))

    def test_malformed(self):
        for s in (None, [], {}, {"Self": None}, {"Self": {"DNSName": 3}}):
            self.assertIsNone(ect.parse_tailnet_host(s), repr(s))


class TestComposeTailnetUrl(unittest.TestCase):
    def test_serve_https_no_port(self):
        self.assertEqual(ect.compose_tailnet_url("h.ts.net", True), "https://h.ts.net")

    def test_no_serve_http_client_port(self):
        with patch.dict(os.environ, {"CLIENT_PORT": "9999"}):
            self.assertEqual(ect.compose_tailnet_url("h.ts.net", False), "http://h.ts.net:9999")


class TestComposeAndEmit(unittest.TestCase):
    def test_gated_off_both_unreachable(self):
        with patch.dict(os.environ, {"SUTANDO_LAN_SHARE": ""}):
            tiers = ect.compose_call_tiers()
        self.assertEqual([t["tier"] for t in tiers], ["direct-tailnet", "direct-lan"])
        self.assertTrue(all(t["url"] is None and t["reachable"] is False for t in tiers))

    def test_emit_payload_shape(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"SUTANDO_LAN_SHARE": ""}):
            dest = os.path.join(tmp, "state", "call-tiers.json")
            out = ect.emit_call_tiers(dest)
            self.assertEqual(out, dest)
            with open(dest) as f:
                payload = json.load(f)
        self.assertIn("ts", payload)
        self.assertIn("pid", payload)
        self.assertEqual(len(payload["call_tiers"]), 2)
        # shape identical to the TS emitter: {tier,label,url,reachable}
        self.assertEqual(sorted(payload["call_tiers"][0]), ["label", "reachable", "tier", "url"])


if __name__ == "__main__":
    unittest.main()
