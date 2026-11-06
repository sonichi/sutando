#!/usr/bin/env python3
"""AGENT_API_PORT must let a second instance bind elsewhere.

The point is not configurability for its own sake: a reviewer asked to produce a
live-path witness could otherwise only restart the owner's running service on
7843, because the port was a bare constant while the bind address already had an
override. Default MUST stay 7843 so the shipped service is unaffected.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "agent-api.py"


def _load(env_port):
    prev = os.environ.get("AGENT_API_PORT")
    if env_port is None:
        os.environ.pop("AGENT_API_PORT", None)
    else:
        os.environ["AGENT_API_PORT"] = env_port
    try:
        spec = importlib.util.spec_from_file_location("agent_api_probe", SRC)
        m = importlib.util.module_from_spec(spec)
        sys.modules["agent_api_probe"] = m
        spec.loader.exec_module(m)
        return m.PORT
    finally:
        os.environ.pop("AGENT_API_PORT", None)
        if prev is not None:
            os.environ["AGENT_API_PORT"] = prev
        sys.modules.pop("agent_api_probe", None)


class AgentApiPortOverrideTest(unittest.TestCase):
    def test_default_is_unchanged(self):
        """The shipped service must bind exactly where it always did."""
        self.assertEqual(_load(None), 7843)

    def test_env_overrides_the_port(self):
        self.assertEqual(_load("7999"), 7999)

    def test_malformed_value_refuses_rather_than_defaulting(self):
        """7843 is the live service: a typo'd witness port must not land on it."""
        with self.assertRaises(ValueError) as cm:
            _load("7999x")
        self.assertIn("not a port number", str(cm.exception))

    def test_port_is_an_int_not_a_string(self):
        """ThreadingHTTPServer takes (host, port); a str port raises at bind."""
        self.assertIsInstance(_load("7999"), int)


if __name__ == "__main__":
    unittest.main(verbosity=1)
