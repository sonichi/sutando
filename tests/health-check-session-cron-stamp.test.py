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
from unittest import mock

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
    def _workspace(self, root: Path, entries, stamp=None, started_at=None,
                   alive_started_at=None) -> Path:
        """`started_at` is the SESSION LAUNCH boundary and is written where the
        probe now reads it: `state/session-starts.log`.

        `alive_started_at` writes the heartbeat's `.alive` field INSTEAD, which
        the probe must no longer consult — see
        `test_alive_started_at_alone_does_not_warn`. Before this split the helper
        wrote only `.alive`, so every case here silently exercised the wrong
        source and the probe's real-world false alarm was unreachable in tests.
        """
        workspace = root / "workspace"
        config = workspace / "hosts" / "test-host" / "crons.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(entries))
        state = workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        if stamp is not None:
            (config.parent / "schedule-crons-stamp.json").write_text(json.dumps(stamp))
        if started_at is not None:
            (state / "session-starts.log").write_text(
                json.dumps({"host": "test-host", "session_started_at": started_at}) + "\n"
            )
        if alive_started_at is not None:
            cores = state / "cores"
            cores.mkdir(exist_ok=True)
            (cores / "test-host.alive").write_text(
                json.dumps({"started_at": alive_started_at})
            )
        return workspace

    def _check(self, workspace, **kw):
        # `_last_core_launch_at` skips launch records belonging to another host,
        # so the fixture's label has to read as local or every boundary is None.
        with mock.patch.object(health, "_local_host_labels",
                               return_value={"test-host"}):
            return self._check_unpatched(workspace, **kw)

    def _check_unpatched(self, workspace, **kw):
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
            self.assertIn("predates this session's launch", check["detail"])

    def test_alive_started_at_alone_does_not_warn(self):
        """THE regression. `.alive.started_at` is the HEARTBEAT writer's age,
        not the session's — both launch paths retain an existing heartbeat
        process, so restarting it under a live session moved that field hours
        forward while every cron stayed registered. Reading it as the boot
        boundary reported all of them gone.

        Observed on Chis-Mac-mini 2026-08-04 with all 9 expected crons live:
        core pid 30961 up since 11:32:30, heartbeat pid 72981 restarted at
        16:13:56, `.alive.started_at` = 16:13:56, stamp written 11:37:21 ->
        "stamp predates this core boot (16595s older)".

        Same field and same mistake as #2446, which established
        `_last_core_launch_at` as the boundary for exactly this reason.
        """
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES,
                stamp={"ts": 1000.0, "registered": 2},
                alive_started_at=5000.0,   # heartbeat restarted AFTER the stamp
            )                              # and NO session-starts.log
            check = self._check(ws)
            self.assertNotEqual(
                check["status"], "warn",
                f"a heartbeat restart must not read as a lost session: {check['detail']}")
            self.assertNotIn("predates", check["detail"])

    def test_session_launch_still_warns_when_both_are_present(self):
        """Control for the case above: with a real launch record present, a
        genuinely pre-launch stamp must still warn. Without this, the fix could
        have disabled the check rather than corrected its input."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._workspace(
                Path(td), SESSION_ENTRIES,
                stamp={"ts": 1000.0, "registered": 2},
                started_at=5000.0,        # the authoritative boundary
                alive_started_at=200.0,   # older heartbeat: must not soften it
            )
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("predates this session's launch", check["detail"])

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
            {"name": "wire-newsroom-nightly-DISABLED-2026-06-09", "cron": "0 0 31 2 *", "prompt": "x"},
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
            {"name": "parked-DISABLED", "cron": "0 0 31 2 *", "prompt": "z"},
        ]
        with tempfile.TemporaryDirectory() as td:
            res = self._check(self._workspace(Path(td), entries, registered=1))
        self.assertEqual(res["status"], "warn")
        self.assertIn("1/2", res["detail"])  # parked one excluded from BOTH sides

    def test_active_impossible_schedule_still_warns(self):
        """qingyun-wu #2498: an impossible date with NO disabled marker is a typo.

        Someone means "the 31st, monthly" and writes February. CronCreate omits
        it; if `expected` dropped it too, health-check would read green forever
        — the silent miss this guard exists to catch.
        """
        entries = [
            {"name": "main-loop", "cron": "*/3 * * * *", "prompt": "x"},
            {"name": "monthly-report", "cron": "0 0 31 2 *", "prompt": "y"},
        ]
        with tempfile.TemporaryDirectory() as td:
            res = self._check(self._workspace(Path(td), entries, registered=1))
        self.assertEqual(res["status"], "warn", res["detail"])
        self.assertIn("1/2", res["detail"])

    def test_parked_signal_matrix(self):
        """Exclusion needs BOTH a parked marker AND an unregistrable date.

        Axis, not just the reviewer's case: {DISABLED-name, disabled:true field,
        no marker} x {impossible cron, valid cron}.
        """
        live = {"name": "main-loop", "cron": "*/3 * * * *", "prompt": "x"}
        IMPOSSIBLE, VALID = "0 0 31 2 *", "0 4 * * *"
        cases = [
            ({"name": "wire-DISABLED-2026", "cron": IMPOSSIBLE}, True,  "parked name + impossible"),
            ({"name": "parked", "disabled": True, "cron": IMPOSSIBLE}, True,  "disabled field + impossible"),
            ({"name": "monthly-report", "cron": IMPOSSIBLE},      False, "NO marker + impossible = typo"),
            ({"name": "wire-DISABLED-2026", "cron": VALID},       False, "parked name + registrable"),
            ({"name": "parked", "disabled": True, "cron": VALID}, False, "disabled field + registrable"),
            ({"name": "ordinary", "cron": VALID},                 False, "plain active entry"),
        ]
        for entry, excluded, label in cases:
            entry = {**entry, "prompt": "y"}
            with tempfile.TemporaryDirectory() as td:
                ws = self._workspace(Path(td), [live, entry], registered=1)
                res = self._check(ws)
            if excluded:
                self.assertEqual(res["status"], "ok", f"{label}: {res['detail']}")
            else:
                self.assertEqual(res["status"], "warn", f"{label}: {res['detail']}")
                self.assertIn("1/2", res["detail"], label)

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

    def test_cron_field_expansion_branches(self):
        """Every branch of the field expander, incl. the ones a bare `31 2 *` skips.

        Ranges, valid steps, empty list segments and out-of-range bounds were all
        unexercised — CI measured the diff at 83.7% (missing 736, 742, 746-749,
        755). Each case below names the branch it drives.
        """
        # range branch + valid step, and genuinely impossible (Feb has <30 days)
        self.assertTrue(health._cron_can_never_fire("0 0 30-31 2 *"))   # range, both > Feb max
        self.assertTrue(health._cron_can_never_fire("0 0 30-31/1 2 *")) # + valid step
        # range that reaches a real day -> schedulable
        self.assertFalse(health._cron_can_never_fire("0 0 28-31 2 *"))  # 28 and 29 exist
        self.assertFalse(health._cron_can_never_fire("0 0 1-31/5 2 *")) # step lands on 1,6,...
        # unparseable / malformed -> schedulable, never a never-fires verdict
        for expr, why in [
            ("0 0 31, 2 *",    "empty list segment"),
            ("0 0 31/0 2 *",   "step below 1"),
            ("0 0 31/x 2 *",   "non-numeric step"),
            ("0 0 a-b 2 *",    "non-numeric range bounds"),
            ("0 0 40 2 *",     "day-of-month above the field max"),
            ("0 0 20-10 2 *",  "inverted range"),
            ("0 0 0 2 *",      "day-of-month below the field min"),
        ]:
            self.assertFalse(health._cron_can_never_fire(expr), why)

    def test_unparseable_is_treated_as_schedulable(self):
        """A parser gap must never invent a never-fires verdict."""
        for e in ["0 0 31 FEB *", "not a cron", "0 0 31 2", "0 0 L 2 *", "0 0 31 2 * *", "0 0 /0 2 *"]:
            self.assertFalse(health._cron_can_never_fire(e), e)


if __name__ == "__main__":
    unittest.main(verbosity=1)
