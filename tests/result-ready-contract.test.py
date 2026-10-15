#!/usr/bin/env python3
"""Contract for src/delivery/readiness.py and delegation by every delivery consumer.

Readiness of a task-result file has one owner. Each consumer binds its own
results directory and keeps only provider-specific delivery.
"""
from __future__ import annotations

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery.readiness import is_ready_body, read_ready_result, ready_body_of  # noqa: E402

# Every consumer that decides "is this result ready to deliver?".
CONSUMERS = {
    "discord-bridge": REPO / "src" / "discord-bridge.py",
    "slack-bridge": REPO / "src" / "slack-bridge.py",
    "telegram-bridge": REPO / "src" / "telegram-bridge.py",
    "remote_gateway_bridge": (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                              / "remote_gateway_bridge.py"),
}


class ContractTest(unittest.TestCase):
    def _write(self, td: str, text: str | None) -> Path:
        p = Path(td) / "task-abc.txt"
        if text is not None:
            p.write_text(text)
        return p

    def test_missing_file_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, None)))

    def test_zero_byte_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, "")))

    def test_whitespace_only_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, "\n \t\n")))

    def test_directory_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "adir"
            d.mkdir()
            self.assertIsNone(read_ready_result(d))

    def test_invalid_utf8_is_not_ready(self):
        """A partial write can land mid-character; decoding must not raise."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "task-abc.txt"
            p.write_bytes(b"answer \xff\xfe")
            self.assertIsNone(read_ready_result(p))

    def test_body_is_returned_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_ready_result(self._write(td, "  hi \n")), "hi")

    def test_marker_only_body_is_ready(self):
        """[no-send] is a real body — marker handling belongs to result_markers."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_ready_result(self._write(td, "[no-send]")), "[no-send]")

    def test_reading_does_not_consume_the_file(self):
        """Not-ready must be retryable: the file survives for the next pass."""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "")
            self.assertIsNone(read_ready_result(p))
            self.assertTrue(p.exists(), "an unready result file was consumed")
            p.write_text("the answer")
            self.assertEqual(read_ready_result(p), "the answer")

    def test_ready_body_of_one_snapshot(self):
        self.assertEqual(ready_body_of(b"  answer\n"), "answer")
        for data in (b"", b" \n\t", b"answer \xff\xfe"):
            self.assertIsNone(ready_body_of(data), repr(data))

    def test_is_ready_body(self):
        for value in ("", "   ", "\n", None):
            self.assertFalse(is_ready_body(value), repr(value))
        self.assertTrue(is_ready_body("x"))


def _owner_import_sources(source: str) -> "dict[str, set[str]]":
    """Map each name bound to `read_ready_result` to the modules it is imported
    from; grouped `( ... )` imports parse the same as single-line ones."""
    found: "dict[str, set[str]]" = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "read_ready_result":
                    found.setdefault(alias.asname or alias.name, set()).add(module)
    return found


OWNER_MODULES = {"delivery.readiness", ".result_ready"}


def _called(fn: ast.AST) -> "set[str]":
    return {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
            for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, (ast.Name, ast.Attribute))}


def _functions_reading_result_bytes(source: str) -> "list[ast.FunctionDef]":
    return [n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and "identity_of" in _called(n)]


def _private_readiness(fn: ast.AST) -> "list[str]":
    """The pieces of the readiness rule: decoding, stripping, the decode error."""
    found = [c for c in _called(fn) if c in ("decode", "strip")]
    found += [n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "UnicodeDecodeError"]
    return found


class DelegationTest(unittest.TestCase):
    """No consumer may re-implement the readiness check."""

    def _assert_imports_the_owner(self, name: str, source: str) -> None:
        found = _owner_import_sources(source)
        modules = found.get("read_ready_result", set())
        self.assertTrue(modules and modules <= OWNER_MODULES,
                        f"{name}: does not import read_ready_result from the shared owner ({found})")

    def test_every_consumer_imports_the_owner(self):
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                self.assertTrue(path.exists(), f"{name}: missing at {path}")
                self._assert_imports_the_owner(name, path.read_text())

    def test_the_owner_check_accepts_grouped_and_rejects_a_missing_or_foreign_name(self):
        self._assert_imports_the_owner(
            "grouped", "from .result_ready import (identity_of,\n    read_ready_result,\n    ResultIdentity)\n")
        self._assert_imports_the_owner(
            "single", "from delivery.readiness import read_ready_result  # noqa\n")
        for label, source in (
                ("missing", "from .result_ready import (identity_of,\n    ResultIdentity)\n"),
                ("foreign", "from .other_module import (identity_of,\n    read_ready_result)\n"),
                ("renamed", "from .result_ready import read_ready_result as rr\n"),
                ("shadowed", "from .result_ready import read_ready_result\n"
                             "from .copy import read_ready_result\n")):
            with self.subTest(control=label), self.assertRaises(AssertionError):
                self._assert_imports_the_owner(label, source)

    def test_bytes_already_read_are_judged_by_the_owner(self):
        """A consumer that reads result bytes itself (one snapshot via
        identity_of) delegates readiness of those bytes; no private decode."""
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                for fn in _functions_reading_result_bytes(path.read_text()):
                    self.assertEqual(_private_readiness(fn), [],
                                     f"{name}.{fn.name}: decides readiness privately")
                    self.assertIn("ready_body_of", _called(fn), f"{name}.{fn.name}")

    def test_the_byte_pin_rejects_a_private_decode(self):
        src = ("def sweep(rfile):\n    data, gen = identity_of(rfile)\n"
               "    try:\n        raw = data.decode('utf-8').strip()\n"
               "    except UnicodeDecodeError:\n        return\n")
        (fn,) = _functions_reading_result_bytes(src)
        self.assertEqual(sorted(_private_readiness(fn)), ["UnicodeDecodeError", "decode", "strip"])
        self.assertNotIn("ready_body_of", _called(fn))

    def test_no_consumer_hand_rolls_the_result_guard(self):
        """Catches a copy reintroduced under any local variable name."""
        pat = re.compile(
            r"(\w+)\s*=\s*\w*(?:result_file|rfile)\w*\.read_text\(\)\.strip\(\)",
        )
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                hits = pat.findall(path.read_text())
                self.assertEqual(
                    hits, [],
                    f"{name}: reads a result file directly ({hits}) — readiness "
                    f"belongs to src/delivery/readiness.read_ready_result",
                )

    def test_sparrow_bundle_matches_src(self):
        pkg = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_ready.py")
        self.assertTrue(pkg.exists(), "result_ready.py not bundled into ag2-sparrow")
        self.assertEqual(
            pkg.read_text(), (REPO / "src" / "delivery" / "readiness.py").read_text(),
            "ag2-sparrow copy drifted from src/ — run tools/sync_from_src.py",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
