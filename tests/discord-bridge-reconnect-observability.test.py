#!/usr/bin/env python3
"""A RESUME leaves no trace in this bridge, so "no ready lines" was read as
"no reconnects". These counters make the two classes distinguishable.

Helpers are AST-extracted from src/discord-bridge.py (importing the module
would require discord.py), matching the other discord-bridge tests.
"""
from __future__ import annotations

import ast
import os
import re
import tempfile
import unittest
from pathlib import Path

# Isolate BEFORE anything reads config. This test AST-extracts rather than
# imports, so it never resolves channels — the isolation is unconditional anyway.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-reconnect-obs-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "src" / "discord-bridge.py"
SRC = BRIDGE.read_text()


def _load():
    tree = ast.parse(SRC)
    keep, names = [], {"_ready_count", "_resume_count", "_disconnect_count"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_reconnect_state":
            keep.append(node)
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<bridge>", "exec"), ns)
    return ns


class ReconnectObservabilityTest(unittest.TestCase):
    def test_all_three_counters_exist_and_start_at_zero(self) -> None:
        ns = _load()
        for name in ("_ready_count", "_resume_count", "_disconnect_count"):
            self.assertEqual(ns[name], 0, name)

    def test_state_line_reports_each_counter_independently(self) -> None:
        """Distinct values, so a formatter that prints one twice cannot pass."""
        ns = _load()
        ns["_ready_count"], ns["_resume_count"], ns["_disconnect_count"] = 3, 7, 5
        line = eval("_reconnect_state()", ns)
        self.assertIn("session #3", line)
        self.assertIn("resume #7", line)
        self.assertIn("disconnect #5", line)

    def test_handlers_are_registered_and_increment(self) -> None:
        """The gap was a MISSING handler, so absence is the thing to assert."""
        tree = ast.parse(SRC)
        want = {"on_ready", "on_resumed", "on_disconnect"}
        handlers = {n.name: n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name in want}
        self.assertEqual(set(handlers), want)
        # A defined-but-undecorated handler is never registered, so discord.py
        # never calls it — name presence alone cannot see that.
        for name, node in handlers.items():
            self.assertTrue(
                any(isinstance(d, ast.Attribute) and d.attr == "event"
                    and isinstance(d.value, ast.Name) and d.value.id == "client"
                    for d in node.decorator_list),
                f"{name} must carry @client.event or it is never registered")
        for name, counter in (("on_resumed", "_resume_count"),
                              ("on_disconnect", "_disconnect_count")):
            body = next(ast.unparse(n) for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            self.assertIn(f"{counter} += 1", body, f"{name} must advance {counter}")
        # discord.py re-dispatches disconnect per retry while ready/resumed fire
        # only on success, so in an outage this is the only line emitted.
        disc = next(ast.unparse(n) for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_disconnect")
        self.assertIn("print(", disc, "on_disconnect must log per event (outage liveness)")
        for name in ("on_resumed", "on_ready", "on_disconnect"):
            body = next(ast.unparse(n) for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            self.assertIn("_reconnect_state()", body, f"{name} must report the counters")

    def test_resume_line_is_greppable_apart_from_ready(self) -> None:
        """A resume that logged 'ready' would be indistinguishable again."""
        self.assertRegex(SRC, r"Discord bridge resumed: ")
        self.assertRegex(SRC, r"Discord bridge disconnected: ")
        self.assertEqual(len(re.findall(r"Discord bridge ready: ", SRC)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
