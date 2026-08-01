#!/usr/bin/env python3
"""Tests for the session-cron registration divergence guard (session-crons check).

The failure it detects is silent: CronCreate registrations are session-only, so
a core boot where /schedule-crons never completed leaves crons.json intact on
disk with zero live crons (peer instance observed 2/18 registered, 2026-07-23).
The guard compares the /schedule-crons completion stamp against the heartbeat's
started_at — stamp AGE alone is deliberately unused (long sessions would
false-warn).
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "health-check.py"
SPEC = importlib.util.spec_from_file_location("health_check", SCRIPT)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)

SESSION_ENTRIES = [
    {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
    {"name": "digest", "cron": "2 6 * * *", "prompt": "run"},
    {"name": "daily", "cron": "7 9 * * *", "prompt": "x", "launchd": True},  # not session-owned
    {"name": "codexjob", "cron": "1 1 * * *", "prompt": "y", "execution": "codex-task"},  # not session-owned
]


class SessionCronStampTest(unittest.TestCase):
    def _workspace(self, root: Path, entries, stamp=None, started_at=None) -> Path:
        workspace = root / "workspace"
        config = workspace / "hosts" / "test-host" / "crons.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(entries))
        state = workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        if stamp is not None:
            (config.parent / "schedule-crons-stamp.json").write_text(json.dumps(stamp))
        if started_at is not None:
            cores = state / "cores"
            cores.mkdir(exist_ok=True)
            (cores / "test-host.alive").write_text(json.dumps({"started_at": started_at}))
        return workspace

    def _check(self, workspace, **kw):
        return health.check_session_cron_registration(
            workspace, host_label="test-host", runtime=kw.pop("runtime", "claude"), **kw
        )

    def test_no_stamp_warns(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(Path(td), SESSION_ENTRIES)
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("never stamped", check["detail"])

    def test_other_hosts_stamp_does_not_satisfy_this_host(self):
        """A newer same-count foreign stamp cannot hide this host's missing run."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES, started_at=5000.0
            )
            foreign = ws / "hosts" / "other-host"
            foreign.mkdir()
            (foreign / "schedule-crons-stamp.json").write_text(
                json.dumps({"ts": 7000.0, "registered": 2})
            )
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("never stamped", check["detail"])

    def test_stamp_predating_boot_warns(self):
        """The Michael failure: core rebooted, /schedule-crons never re-ran."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES,
                stamp={"ts": 1000.0, "registered": 2},
                started_at=5000.0,
            )
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("predates this core boot", check["detail"])

    def test_fresh_stamp_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES,
                stamp={"ts": 6000.0, "registered": 2},
                started_at=5000.0,
            )
            check = self._check(ws)
            self.assertEqual(check["status"], "ok")

    def test_partial_registration_warns(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES,
                stamp={"ts": 6000.0, "registered": 1},
                started_at=5000.0,
            )
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("1/2", check["detail"])

    def test_codex_runtime_skips(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(Path(td), SESSION_ENTRIES)
            check = self._check(ws, runtime="codex")
            self.assertEqual(check["status"], "ok")

    def test_only_nonsession_entries_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(Path(td), [
                {"name": "daily", "cron": "7 9 * * *", "prompt": "x", "launchd": True},
                {"name": "codex", "cron": "8 9 * * *", "execution": "codex-task"},
                {"name": "dynamic", "cron": "9 9 * * *", "loop": "dynamic"},
                {"name": "disabled", "prompt": "no cron expression"},
                "malformed entry",
            ])
            check = self._check(ws)
            self.assertEqual(check["status"], "ok")

    def test_no_heartbeat_fresh_stamp_ok(self):
        """No .alive anchor → stamp presence + counts still validate."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES, stamp={"ts": 6000.0, "registered": 2}
            )
            check = self._check(ws)
            self.assertEqual(check["status"], "ok")

    def test_missing_config_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "workspace"
            (ws / "state").mkdir(parents=True)
            check = self._check(ws)
            self.assertEqual(check["status"], "ok")

    def test_invalid_config_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._workspace(root, SESSION_ENTRIES)
            config = ws / "hosts" / "test-host" / "crons.json"
            config.write_text("{")
            self.assertEqual(self._check(ws)["status"], "warn")
            config.write_text(json.dumps({"not": "a list"}))
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("not a list", check["detail"])

    def test_unreadable_stamp_warns(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(Path(td), SESSION_ENTRIES)
            (ws / "hosts" / "test-host" / "schedule-crons-stamp.json").mkdir()
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("stamp unreadable", check["detail"])

    def test_malformed_stamp_shapes_warn(self):
        cases = [
            ([], "expected an object"),
            ({"registered": 2}, "numeric ts"),
            ({"ts": "6000", "registered": 2}, "numeric ts"),
            ({"ts": 6000}, "registered count"),
            ({"ts": 6000, "registered": -1}, "registered count"),
            ({"ts": 6000, "registered": True}, "registered count"),
        ]
        for stamp, detail in cases:
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as td:
                ws = self._workspace(Path(td), SESSION_ENTRIES, stamp=stamp)
                check = self._check(ws)
                self.assertEqual(check["status"], "warn")
                self.assertIn(detail, check["detail"])

    def test_malformed_heartbeat_shape_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES, stamp={"ts": 6000.0, "registered": 2}
            )
            cores = ws / "state" / "cores"
            cores.mkdir(exist_ok=True)
            (cores / "test-host.alive").write_text(json.dumps([]))
            self.assertEqual(self._check(ws)["status"], "ok")

class ParkedCronNotExpectedTest(unittest.TestCase):
    """A cron parked on an impossible date must not count toward `expected`.

    Observed on Chis-Mac-mini 2026-08-01: crons.json carries
    `wire-newsroom-nightly-DISABLED-...` at `0 0 31 2 *` (February 31st).
    /schedule-crons registers the other 8 and cannot register that one, so the
    stamp reads 8/9 and the guard warned on every health-check run, forever.
    It can only be cleared by re-enabling the parked job (owner's call alone) or
    by deleting the disabled record — so the guard was permanently crying wolf
    while existing to catch a SILENT failure.
    """

    def _check(self, workspace):
        return health.check_session_cron_registration(
            workspace, host_label="test-host", runtime="claude"
        )

    def _workspace(self, root: Path, entries, registered: int) -> Path:
        workspace = root / "workspace"
        config = workspace / "hosts" / "test-host" / "crons.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(entries))
        (workspace / "state").mkdir(parents=True, exist_ok=True)
        (config.parent / "schedule-crons-stamp.json").write_text(
            json.dumps({"ts": 2000, "registered": registered, "config_total": len(entries)})
        )
        return workspace

    def test_parked_entry_is_not_expected(self):
        """The real host shape: 1 live + 1 parked, 1 registered -> ok, not 1/2."""
        entries = [
            {"name": "main-loop", "cron": "*/3 * * * *", "prompt_skill": "proactive-loop"},
            {"name": "wire-nightly-DISABLED", "cron": "0 0 31 2 *", "prompt": "x"},
        ]
        with tempfile.TemporaryDirectory() as td:
            res = self._check(self._workspace(Path(td), entries, registered=1))
        self.assertEqual(res["status"], "ok", res["detail"])
        self.assertIn("1 session cron", res["detail"])

    def test_a_genuinely_missing_cron_still_warns(self):
        """The guard must keep its teeth — a real shortfall still warns."""
        entries = [
            {"name": "main-loop", "cron": "*/3 * * * *", "prompt": "x"},
            {"name": "digest", "cron": "2 6 * * *", "prompt": "y"},
            {"name": "parked", "cron": "0 0 31 2 *", "prompt": "z"},
        ]
        with tempfile.TemporaryDirectory() as td:
            res = self._check(self._workspace(Path(td), entries, registered=1))
        self.assertEqual(res["status"], "warn")
        self.assertIn("1/2", res["detail"])  # parked one excluded from BOTH sides

    def test_never_fires_predicate(self):
        never = ["0 0 31 2 *", "0 0 30 2 *", "0 0 31 4,6 *", "0 0 31 2 ?"]
        for e in never:
            self.assertTrue(health._cron_can_never_fire(e), e)
        fires = [
            "*/3 * * * *", "57 6 * * *", "0 0 29 2 *",     # Feb 29 exists in leap years
            "0 0 31 1 *", "0 0 31 2 MON",                   # day-of-week ORs it back in
            "0 0 31 2 1", "0 0 * * *", "13 3 * * 1",
        ]
        for e in fires:
            self.assertFalse(health._cron_can_never_fire(e), e)

    def test_unparseable_is_treated_as_schedulable(self):
        """A parser gap must never invent a never-fires verdict."""
        for e in ["0 0 31 FEB *", "not a cron", "0 0 31 2", "0 0 L 2 *", "0 0 31 2 * *", "0 0 /0 2 *"]:
            self.assertFalse(health._cron_can_never_fire(e), e)


if __name__ == "__main__":
    unittest.main(verbosity=1)
