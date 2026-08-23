#!/usr/bin/env python3
"""The identity ratchet (B slice 2): src/ may not grow new identity mints.

Two one-way gates over EVERY .py under src/ (recursive), pinned to today's
shipped sites:

R-A  Wall-clock task minting. The census showed a replayed provider event
     becomes a NEW task wherever the id comes from the clock; new code must
     use ag2_sparrow.identity.ingress_task_id. Detection is AST-based, so a
     mint is caught regardless of quote style or nesting: an f-string whose
     leading literal starts with "task-" and interpolates a value, a
     "task-..."​.format(...) call, and "task-..." + expr / "task-..." % expr.
     Sites are pinned per (file, enclosing function) so a removal elsewhere
     in the file cannot cancel a new mint. Above a pin is a new site (red);
     below means a site was strangled — lower the pin in the same change.

R-B  delivery_id constructor exclusivity: any src/ file (recursive) that
     names delivery_id must either predate this ratchet (pinned below) or
     import the canonical constructors from ag2_sparrow.identity.

Positive controls at the bottom run the SAME scanners against hostile
fixtures (nested files, single quotes, .format, concatenation) so a silent
scanner regression reds this suite, not slice 3.

Run: python3 tests/sparrow-identity-ratchet.test.py   (stdlib only)
"""
from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Shipped mint sites at the ratchet's introduction: (relpath, function) -> n.
TASK_MINT_PIN = {
    ("agent-api.py", "handle_twilio_voice"): 1,
    ("agent-api.py", "handle_twilio_sms"): 1,
    ("agent-api.py", "handle_twilio_transcription"): 1,
    ("agent-api.py", "do_POST"): 1,
    ("cron-runner.py", "emit_task"): 2,
    ("discord-bridge.py", "_dedup_recover"): 1,
    ("discord-bridge.py", "_handle_discord_message"): 1,
    ("discord-bridge.py", "poll_results"): 1,
    ("github-webhook.py", "do_POST"): 1,
    ("health-check.py", "emit_task_for_failures"): 2,
    ("obsidian-mirror.py", "_task_id_from_path"): 1,
    ("remote-gateway-bridge.test.py", "main"): 4,
    ("slack-bridge.py", "_dedup_recover"): 1,
    ("slack-bridge.py", "_write_task"): 1,
    ("telegram-bridge.py", "_dedup_recover"): 1,
    ("telegram-bridge.py", "main"): 1,
}

# Files that referenced delivery_id before the canonical package existed.
DELIVERY_ID_LEGACY_FILES = {
    "slack-bridge.py",
    "slack_proactive_receipts.py",
}
_CANONICAL_IMPORT = re.compile(r"from\s+ag2_sparrow\.identity\s+import|"
                               r"from\s+ag2_sparrow\s+import\s+identity")


def _is_task_literal(node) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith("task-"))


def scan_task_mints(root: Path) -> dict:
    """(relpath, enclosing function) -> count of task-mint construction sites
    across every .py under root, regardless of quote style or spelling."""
    counts = {}

    def scan_file(py: Path) -> None:
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except SyntaxError:
            counts[(rel, "<unparseable>")] = 1
            return
        stack = ["<module>"]

        def record():
            key = (rel, stack[-1])
            counts[key] = counts.get(key, 0) + 1

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                stack.append(n.name)
                self.generic_visit(n)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_JoinedStr(self, n):
                if (n.values and _is_task_literal(n.values[0])
                        and any(isinstance(v, ast.FormattedValue)
                                for v in n.values)):
                    record()
                self.generic_visit(n)

            def visit_Call(self, n):
                f = n.func
                if (isinstance(f, ast.Attribute) and f.attr == "format"
                        and _is_task_literal(f.value)):
                    record()
                self.generic_visit(n)

            def visit_BinOp(self, n):
                if (isinstance(n.op, (ast.Add, ast.Mod))
                        and _is_task_literal(n.left)
                        and not isinstance(n.right, ast.Constant)):
                    record()
                self.generic_visit(n)

        V().visit(tree)

    for py in sorted(root.rglob("*.py")):
        scan_file(py)
    return counts


def scan_delivery_id_files(root: Path) -> dict:
    """relpath -> has canonical import, for every .py under root (recursive)
    whose text names delivery_id."""
    out = {}
    for py in sorted(root.rglob("*.py")):
        text = py.read_text(errors="replace")
        if "delivery_id" in text:
            out[py.relative_to(root).as_posix()] = bool(
                _CANONICAL_IMPORT.search(text))
    return out


class WallClockTaskMintRatchet(unittest.TestCase):
    def test_no_new_mint_sites_and_pins_track_removals(self):
        counts = scan_task_mints(SRC)
        for key, n in sorted(counts.items()):
            pinned = TASK_MINT_PIN.get(key, 0)
            self.assertLessEqual(
                n, pinned,
                f"src/{key[0]} ({key[1]}) has {n} task-mint site(s), pin is "
                f"{pinned}. New task identities must come from "
                f"ag2_sparrow.identity.ingress_task_id (injective, "
                f"replay-stable), not the wall clock.")
            self.assertGreaterEqual(
                n, pinned,
                f"src/{key[0]} ({key[1]}) dropped below its pin "
                f"({n} < {pinned}) — a mint site was strangled. Lower "
                f"TASK_MINT_PIN in this test in the same change so the "
                f"ratchet records the progress.")
        for key in sorted(TASK_MINT_PIN):
            self.assertIn(key, counts,
                          f"pinned site src/{key[0]} ({key[1]}) has no mint "
                          f"sites left — remove its TASK_MINT_PIN entry.")


class DeliveryIdConstructorExclusivity(unittest.TestCase):
    def test_new_delivery_id_users_import_the_canonical_package(self):
        for rel, has_import in sorted(scan_delivery_id_files(SRC).items()):
            if rel in DELIVERY_ID_LEGACY_FILES:
                continue
            self.assertTrue(
                has_import,
                f"src/{rel} names delivery_id but does not import "
                f"ag2_sparrow.identity — the canonical constructors are the "
                f"only legal source of a delivery identity (freeze doc R1/R3).")


class ScannerPositiveControls(unittest.TestCase):
    """The scanners must catch the shapes the ratchet exists to catch."""

    def _scan_fixture(self, relpath: str, source: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / relpath
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(source)
            return scan_task_mints(Path(tmp))

    def test_catches_quote_styles_nesting_and_spellings(self):
        cases = {
            "top-level double-quoted": ("a.py", 'x = f"task-{ts}"\n'),
            "top-level single-quoted": ("a.py", "x = f'task-{ts}'\n"),
            "nested file": ("runtime-api/a.py",
                            'def f():\n    return f"task-{ts}"\n'),
            "str.format": ("a.py", 'x = "task-{}".format(ts)\n'),
            "concatenation": ("a.py", 'x = "task-" + str(ts)\n'),
            "percent-format": ("a.py", 'x = "task-%s" % ts\n'),
        }
        for name, (rel, src) in cases.items():
            self.assertEqual(sum(self._scan_fixture(rel, src).values()), 1,
                             f"scanner missed the {name} mint shape")

    def test_ignores_non_mint_task_strings(self):
        self.assertEqual(
            self._scan_fixture("a.py", 'x = "task-static"\ny = f"task-lit"\n'),
            {})

    def test_delivery_gate_sees_nested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "runtime-api" / "b.py"
            f.parent.mkdir(parents=True)
            f.write_text("delivery_id = compute()\n")
            self.assertEqual(scan_delivery_id_files(Path(tmp)),
                             {"runtime-api/b.py": False})


if __name__ == "__main__":
    unittest.main()
