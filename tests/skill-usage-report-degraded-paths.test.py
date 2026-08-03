#!/usr/bin/env python3
"""Degraded-state regressions for skill-usage-report (sonichi#2180 review, round 5).

Both prior rounds tested the SUCCESSFUL lock path. The reviewer's point is that
the failures that matter here happen when something is already wrong — and in
both cases the code's degraded branch made it worse rather than contained it.

[P1-a] report-usage.py — a corrupt timestamp permanently disabled the reporter.
    `int(rec["ts"])` accepts any integer, but the `datetime.fromtimestamp()` that
    RENDERS it ran after `log.rename(pending)`. Out-of-range -> OverflowError past
    the per-record guard -> exit nonzero with the claim stranded as `.reporting`.
    The next run re-folded the same poison record and stranded it again, so valid
    usage never drained. One bad byte, permanent outage.
        repro: {"slug": "corrupt-ts", "ts": 999999999999999999999999999}

[P1-b] hooks/log-usage.py — the lock's own absence disabled the lock.
    If importing `usage_lock` failed, the fallback yielded True and the hook
    appended WITHOUT the lock, reopening the pre-rename-fd data-loss race this PR
    exists to close. "Keep working exactly as before" was the bug: before IS the
    race.

Both are tested through the SHIPPED files. P1-b blocks the import with a
meta_path finder rather than copying the tree, so the file under test is the real
one, not a copy that could drift.

EVERY assertion here is paired with a CONTROL that must come out the other way.
An earlier round of this PR shipped a test that passed because `_report` returned
at the AGENT_MXID guard long before reaching the code it claimed to cover; a
degraded-path test is exactly where that failure mode hides, because "nothing
happened" is the expected-looking outcome.

Run: python3 tests/skill-usage-report-degraded-paths.test.py
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "skill-usage-report"
HOOK = SKILL / "hooks" / "log-usage.py"

POISON = 999999999999999999999999999
VALID_TS = 1785400000


def _load_reporter():
    """Load the shipped report-usage.py, with the vault and the network stubbed.

    Stubbing is mandatory, not convenience: `_report` returns early when the
    vault has no AG2_CLOUD_TOKEN, so on a machine without one every assertion
    below would pass without executing a single line of the code under test.
    """
    sys.path.insert(0, str(SKILL))
    fake_vault = types.ModuleType("vault_intercept")
    fake_vault.get_vault_key = lambda _k: "test-token"  # noqa: ARG005
    sys.modules["vault_intercept"] = fake_vault

    spec = importlib.util.spec_from_file_location("ru", SKILL / "scripts" / "report-usage.py")
    ru = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ru)

    posted = []

    class _Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        posted.append(json.loads(req.data.decode()))
        return _Resp()

    ru.urllib.request.urlopen = _fake_urlopen
    return ru, posted


class OutOfRangeTimestamp(unittest.TestCase):
    """[P1-a] A corrupt ts must be an ordinary skipped record, not an outage."""

    def setUp(self):
        self.ru, self.posted = _load_reporter()
        self._env = __import__("os").environ
        self._prev = self._env.get("AGENT_MXID")
        self._env["AGENT_MXID"] = "@test:ag2.space"

    def tearDown(self):
        if self._prev is None:
            self._env.pop("AGENT_MXID", None)
        else:
            self._env["AGENT_MXID"] = self._prev

    def _run(self, records):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name)
        (ws / "state").mkdir()
        log = ws / "state" / "skill-usage-log.jsonl"
        pending = log.with_suffix(".jsonl.reporting")
        log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        rc = self.ru._report(ws, log, pending)
        return rc, log, pending

    def test_control_valid_ts_is_posted(self):
        """CONTROL: the harness can observe a drain — so a missing one means something."""
        rc, _log, pending = self._run([{"slug": "good-skill", "ts": VALID_TS}])
        self.assertEqual(rc, 0)
        self.assertFalse(pending.exists())
        slugs = [e["slug"] for p in self.posted for e in p.get("events", [])]
        self.assertIn("good-skill", slugs, "control failed: a VALID record did not post")

    def test_poison_ts_exits_zero_and_strands_nothing(self):
        rc, _log, pending = self._run([{"slug": "corrupt-ts", "ts": POISON}])
        self.assertEqual(rc, 0, "a corrupt timestamp must not fail the run")
        self.assertFalse(pending.exists(), ".reporting was stranded — the claim cycle is back")

    def test_poison_ts_does_not_block_later_valid_events(self):
        """The damage was never the lost record — it was the ones behind it."""
        rc, _log, pending = self._run([
            {"slug": "corrupt-ts", "ts": POISON},
            {"slug": "good-skill", "ts": VALID_TS},
        ])
        self.assertEqual(rc, 0)
        self.assertFalse(pending.exists())
        slugs = [e["slug"] for p in self.posted for e in p.get("events", [])]
        self.assertIn("good-skill", slugs, "a valid event behind a poison one never drained")
        self.assertNotIn("corrupt-ts", slugs, "the unrenderable record should be skipped")

    def test_renderable_predicate_answers_both_ways(self):
        self.assertTrue(self.ru._renderable(VALID_TS))
        self.assertFalse(self.ru._renderable(POISON))
        self.assertFalse(self.ru._renderable(-(2 ** 62)))


class LockImportFailure(unittest.TestCase):
    """[P1-b] If the lock cannot load, the hook must not write unlocked."""

    def _run_hook(self, block_usage_lock):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name)
        (ws / "state").mkdir()
        blocker = (
            "import sys\n"
            "class _B:\n"
            "    def find_module(self, name, path=None):\n"
            "        return self if name == 'usage_lock' else None\n"
            "    def load_module(self, name):\n"
            "        raise ImportError('forced: partial install')\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'usage_lock':\n"
            "            raise ImportError('forced: partial install')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _B())\n"
            if block_usage_lock else ""
        )
        code = (
            blocker
            + "import importlib.util, pathlib, sys\n"
            + f"spec = importlib.util.spec_from_file_location('hk', {str(HOOK)!r})\n"
            + "hk = importlib.util.module_from_spec(spec)\n"
            + "spec.loader.exec_module(hk)\n"
            + "print('FALLBACK=%s' % (getattr(hk._claim_lock, '__module__', '?') "
              "!= 'usage_lock'))\n"
            + f"hk.workspace = lambda: pathlib.Path({str(ws)!r})\n"
            + "print('EXIT=%s' % hk.main())\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps({"tool_name": "Skill", "tool_input": {"skill": "demo-skill"}}),
            capture_output=True, text=True,
        )
        return r, ws / "state" / "skill-usage-log.jsonl"

    def test_control_healthy_install_still_logs(self):
        """CONTROL: proves the fix is containment, not a blanket disable."""
        r, log = self._run_hook(block_usage_lock=False)
        self.assertIn("FALLBACK=False", r.stdout, f"real lock did not load: {r.stdout}{r.stderr}")
        self.assertIn("EXIT=0", r.stdout)
        self.assertTrue(log.exists(), "control failed: a HEALTHY install must still log")

    def test_import_failure_writes_nothing_and_still_exits_zero(self):
        r, log = self._run_hook(block_usage_lock=True)
        # Guard against the false positive: an earlier attempt at this repro
        # "passed" while the REAL lock was loaded, because the hook's own
        # sys.path.insert outranked the shim. Assert the degraded path is live
        # before trusting anything downstream of it.
        self.assertIn("FALLBACK=True", r.stdout,
                      f"degraded path never engaged, so this proves nothing: {r.stdout}{r.stderr}")
        self.assertIn("EXIT=0", r.stdout, "the hook must never block a tool call")
        self.assertFalse(log.exists(), "hook appended WITHOUT the lock — the race is reopened")


if __name__ == "__main__":
    unittest.main(verbosity=2)
