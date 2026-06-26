#!/usr/bin/env python3
"""Regression guards: hostname-resolution resilience (2026-06-22).

Two related fragilities found when a node's `hostname` is a DHCP-assigned
name (e.g. `Chis-MBP.hsd1.wa.comcast.net`) that is unstable and not
DNS-resolvable:

1. `agent-api.py` did `socket.gethostbyname(socket.gethostname())` at startup
   for an informational log line. An unresolvable hostname raises `gaierror`
   (an `OSError`) and CRASHED agent-api on boot. Fix: `_resolve_local_ip()`
   catches `OSError` and falls back to loopback.

2. `core_heartbeat._hostname()` used the raw `socket.gethostname()` for the
   `<label>.alive` filename, diverging from `util_paths._host_label()` (which
   honors `$SUTANDO_HOST_LABEL`). On a DHCP-drifting host that produced TWO
   divergent `.alive` files and ignored the per-host label pin. Fix: delegate
   to `_host_label()`.

Run: python3 tests/hostname-resolution-resilience.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations
import importlib.util
import os
import re
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_core_heartbeat():
    spec = importlib.util.spec_from_file_location(
        "core_heartbeat", ROOT / "src" / "core_heartbeat.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CoreHeartbeatHostnameTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SUTANDO_HOST_LABEL", None)

    def tearDown(self):
        os.environ.pop("SUTANDO_HOST_LABEL", None)

    def test_hostname_honors_label_pin(self):
        os.environ["SUTANDO_HOST_LABEL"] = "Chis-MacBook-Pro"
        ch = _load_core_heartbeat()
        self.assertEqual(
            ch._hostname(), "Chis-MacBook-Pro",
            "_hostname() must honor $SUTANDO_HOST_LABEL (via _host_label), "
            "so the .alive label survives DHCP hostname drift",
        )

    def test_hostname_fallback_matches_short_hostname(self):
        # No label set → must equal the domain-stripped short hostname,
        # i.e. byte-identical to the pre-fix behavior.
        ch = _load_core_heartbeat()
        self.assertEqual(ch._hostname(), socket.gethostname().split(".")[0])


class AgentApiGethostbynameGuardTests(unittest.TestCase):
    SRC = (ROOT / "src" / "agent-api.py").read_text()

    def test_gethostbyname_is_wrapped(self):
        self.assertIn("_resolve_local_ip", self.SRC,
                      "the local-IP resolution must go through the guarded helper")
        self.assertIn("except OSError", self.SRC,
                      "gethostbyname must be wrapped so an unresolvable hostname "
                      "can't crash startup")
        self.assertIn('return "127.0.0.1"', self.SRC,
                      "must fall back to loopback on resolution failure")

    def test_no_bare_crashing_call_remains(self):
        # The exact bare call that crashed must not be reintroduced at module top level.
        self.assertNotRegex(
            self.SRC,
            r"\n    local_ip = socket\.gethostbyname\(socket\.gethostname\(\)\)",
            "the bare unguarded gethostbyname call must not return",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
