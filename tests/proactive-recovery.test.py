#!/usr/bin/env python3
"""Contract tests for shared proactive-result crash recovery."""

from __future__ import annotations

import ast
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_recovery import recover_orphan_sending_files  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(f"{name}: {detail}")


with tempfile.TemporaryDirectory(prefix="sutando-proactive-recovery-") as tmp:
    results = Path(tmp) / "results"
    check("missing results directory is a no-op", recover_orphan_sending_files(results) == 0)

    results.mkdir()
    orphan = results / "proactive-one.sending"
    orphan.write_text("queued message")
    check("one orphan is recovered", recover_orphan_sending_files(results) == 1)
    check("recovery preserves content", (results / "proactive-one.txt").read_text() == "queued message")
    check("recovery is idempotent", recover_orphan_sending_files(results) == 0)

    ignored = results / "task-one.sending"
    ignored.write_text("not proactive")
    check("non-proactive claims are ignored", recover_orphan_sending_files(results) == 0 and ignored.exists())

    collision = results / "proactive-collision.sending"
    collision.write_text("older claim")
    target = results / "proactive-collision.txt"
    target.write_text("newer result")
    check("collision is not overwritten", recover_orphan_sending_files(results) == 0)
    check("collision preserves both files", collision.exists() and target.read_text() == "newer result")

    collision.unlink()
    target.unlink()
    raced = results / "proactive-raced.sending"
    raced.write_text("race")
    with mock.patch.object(os, "link", side_effect=FileNotFoundError):
        check("lost recovery race is harmless", recover_orphan_sending_files(results) == 0)

    output = io.StringIO()
    with mock.patch.object(os, "link", side_effect=OSError("disk unavailable")):
        with contextlib.redirect_stdout(output):
            recovered = recover_orphan_sending_files(results)
    check("per-file failure does not block startup", recovered == 0)
    check(
        "per-file failure is visible",
        "failed to recover proactive-raced.sending: disk unavailable" in output.getvalue(),
    )


for adapter in ("discord-bridge.py", "slack-bridge.py", "telegram-bridge.py"):
    path = REPO / "src" / adapter
    source = path.read_text()
    tree = ast.parse(source)
    wrapper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_recover_orphan_sending_files"
    )
    return_node = wrapper.body[-1]
    delegates = (
        isinstance(return_node, ast.Return)
        and isinstance(return_node.value, ast.Call)
        and isinstance(return_node.value.func, ast.Name)
        and return_node.value.func.id == "recover_orphan_sending_files"
    )
    # Match the NAME, not the import line's text: a multi-name or parenthesised
    # import is the same delegation and a substring test calls it a regression.
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "proactive_recovery"
        for alias in node.names
    }
    # discord delegates through the 5b fence, whose recover() calls the shared
    # sweep (pinned here at source + behaviorally in the fence suite).
    if adapter == "discord-bridge.py":
        fence_src = (REPO / "src" / "proactive_claim_fence.py").read_text()
        fence_delegates = (
            ".recover()" in source
            and "recover_orphan_sending_files" in fence_src
        )
        check(f"{adapter} imports shared recovery", fence_delegates)
        check(f"{adapter} wrapper delegates to shared recovery",
              delegates or fence_delegates)
    else:
        check(f"{adapter} imports shared recovery",
              "recover_orphan_sending_files" in imported)
        check(f"{adapter} wrapper delegates to shared recovery", delegates)


if failures:
    print(f"\nFAIL — {len(failures)} check(s)")
    for failure in failures:
        print(f"  {failure}")
    raise SystemExit(1)
print("\nPASS — proactive recovery contract")
