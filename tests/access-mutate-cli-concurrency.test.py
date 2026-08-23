#!/usr/bin/env python3
"""Concurrency regression: the skill-callable CLI vs. a bridge-side writer
(#3318 blocker 1, qingyun-wu review).

Before this fix, the `/discord:access` skill's `group append`/`group rm-allow`
subcommands were a freehand Read/Write-tool edit — a separate OS process with
NO coordination against `access_store.mutate_access_file`, the lock every
other writer (tier-map seeding, thread-engage seeding, pairing) shares. An
interleaving where the skill read a stale snapshot after a concurrent bridge
write landed silently dropped the bridge's update when the skill wrote back.

This test drives the REAL production skill-callable path — a genuine
`subprocess.run(["python3", "scripts/access-mutate.py", ...])`, not a copied
recipe of its logic — racing a genuine `access_store.mutate_access_file` call
(what a bridge writer, e.g. thread-engage seeding, actually calls) against
the SAME file, and asserts both updates survive. The bridge-side mutator
sleeps while holding the lock so the CLI subprocess's own
`mutate_access_file` call is forced to block on the real `fcntl.flock` (cross-
process, not merely GIL-serialized) rather than racing for who reads first.

Run: python3 tests/access-mutate-cli-concurrency.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "access-mutate.py"
sys.path.insert(0, str(REPO / "src"))

from access_store import mutate_access_file  # noqa: E402


def _cli_command(args):
    """Wrap the CLI subprocess invocation under coverage when the caller
    (coverage-gate.sh) asks for it, so the CLI's own lines aren't reported
    as unexercised despite this test genuinely driving them."""
    command = [sys.executable]
    if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
        command += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]
    return command + [str(CLI)] + [str(a) for a in args]


def _slow_bridge_mutator(data):
    """Mirrors a real bridge writer's shape (e.g. thread-engage seeding) —
    holds the lock long enough to force the CLI subprocess to genuinely
    block on it, not just race for who reads the file first."""
    time.sleep(0.3)
    groups = data.setdefault("groups", {})
    groups["thread-bridge"] = {"requireMention": False, "allowFrom": ["sender-y"]}
    return data, True


class TestSkillCliAndBridgeWriterBothSurvive(unittest.TestCase):
    def test_racing_cli_group_append_and_bridge_mutate_both_persist(self):
        with tempfile.TemporaryDirectory() as d:
            ccd = Path(d) / "ccd"
            access_file = ccd / "channels" / "discord" / "access.json"
            access_file.parent.mkdir(parents=True, exist_ok=True)
            access_file.write_text(json.dumps({
                "dmPolicy": "pairing",
                "allowFrom": ["owner-1"],
                "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1"]}},
            }))

            env = dict(os.environ)
            env["CLAUDE_CONFIG_DIR"] = str(ccd)

            results = {}

            def _run_bridge_writer():
                results["bridge"] = mutate_access_file(access_file, _slow_bridge_mutator)

            def _run_cli():
                results["cli"] = subprocess.run(
                    _cli_command(["group-append", "thread-1", "newmember"]),
                    capture_output=True, text=True, env=env, timeout=10,
                )

            t_bridge = threading.Thread(target=_run_bridge_writer)
            t_cli = threading.Thread(target=_run_cli)
            # Start the bridge writer first so it holds the lock while the
            # CLI subprocess starts and genuinely overlaps with it.
            t_bridge.start()
            time.sleep(0.05)
            t_cli.start()
            t_bridge.join(timeout=10)
            t_cli.join(timeout=10)

            self.assertFalse(t_bridge.is_alive(), "bridge writer did not finish — deadlock?")
            self.assertFalse(t_cli.is_alive(), "CLI subprocess did not finish — deadlock?")
            self.assertTrue(results.get("bridge"), "bridge-side mutation reported failure")

            cli_proc = results["cli"]
            self.assertEqual(
                cli_proc.returncode, 0,
                f"CLI subprocess failed: stdout={cli_proc.stdout!r} stderr={cli_proc.stderr!r}",
            )
            cli_result = json.loads(cli_proc.stdout.strip())
            self.assertTrue(cli_result.get("ok"), f"CLI reported failure: {cli_result}")
            self.assertEqual(cli_result.get("added"), ["newmember"])

            final = json.loads(access_file.read_text())
            self.assertEqual(
                final.get("groups", {}).get("thread-bridge"),
                {"requireMention": False, "allowFrom": ["sender-y"]},
                "the bridge-side thread seed was lost — the CLI's write clobbered it",
            )
            self.assertEqual(
                final.get("groups", {}).get("thread-1", {}).get("allowFrom"),
                ["owner-1", "newmember"],
                "the CLI's group-append was lost — the bridge write clobbered it",
            )
            # Nothing pre-existing should have been dropped either.
            self.assertEqual(final.get("allowFrom"), ["owner-1"])
            self.assertEqual(final.get("dmPolicy"), "pairing")


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    try:
        import coverage

        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    sys.exit(0 if _r.result.wasSuccessful() else 1)
