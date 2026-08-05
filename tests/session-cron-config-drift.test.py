#!/usr/bin/env python3
"""A cron edited AFTER registration must be REPORTED, not merely self-healed.

THE DEFECT (observed live 2026-08-04). A session cron is a snapshot of its
prompt at registration time. `pr-flag` gained `--stand` in `crons.json` four
days into a running session; the registered job kept firing the pre-edit text,
and `pr_flag.py` — which nulls `is_mine` when `--stand` is absent — returned
`is_mine: null` on all 27 PRs. The cron's own instruction said to judge from
that field. The script was correct and the config was correct; only the stale
registration was wrong.

#2653 makes /schedule-crons re-register instead of skipping, so the drift
self-heals on the next run. This guard covers the window in between, where the
only other observation point is a fire.

WHY THE ASSERTIONS ARE SHAPED THIS WAY: the pre-existing `session-crons` checks
are all COUNTS (expected vs registered, stamp vs boot). A count cannot go
positive for a prompt edit — the entry is still present, still registered, still
one row. So every test here keeps the counts SATISFIED and varies only the
prompt: if the drift check were deleted, these must fail rather than pass for
some unrelated reason. `test_drift_check_is_the_only_thing_firing` pins exactly
that by asserting the same fixture is `ok` before the edit.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
SCRIPT = REPO / "src" / "health-check.py"
SPEC = importlib.util.spec_from_file_location("health_check", SCRIPT)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)

from cron_entry_digest import digest_map, drifted, entry_digest  # noqa: E402

BOOT = 1_000_000
STAMPED = BOOT + 60


def entries(pr_flag_prompt="run pr_flag", extra=()):
    base = [
        {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
        {"name": "pr-flag", "cron": "17 * * * *", "prompt": pr_flag_prompt},
    ]
    return base + list(extra)


class ConfigDriftTest(unittest.TestCase):
    def _ws(self, root: Path, ents, stamp):
        ws = root / "workspace"
        cfg = ws / "hosts" / "test-host" / "crons.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps(ents))
        state = ws / "state"
        state.mkdir(parents=True, exist_ok=True)
        (cfg.parent / "schedule-crons-stamp.json").write_text(json.dumps(stamp))
        (state / "session-starts.log").write_text(
            json.dumps({"host": "test-host", "session_started_at": BOOT}) + "\n"
        )
        return ws

    def _check(self, ws):
        with mock.patch.object(health, "_local_host_labels", return_value={"test-host"}):
            return health.check_session_cron_registration(
                ws, host_label="test-host", runtime="claude"
            )

    def _stamp(self, ents, **kw):
        s = {"ts": STAMPED, "registered": 2, "config_total": len(ents),
             "config_digests": digest_map(ents)}
        s.update(kw)
        return s

    # --- the control: same fixture, unedited, must be OK -------------------
    def test_drift_check_is_the_only_thing_firing(self):
        """Counts satisfied and no edit => ok. If this fails, the cases below
        prove nothing, because they would be warning for another reason."""
        with tempfile.TemporaryDirectory() as td:
            ents = entries()
            ws = self._ws(Path(td), ents, self._stamp(ents))
            self.assertEqual(self._check(ws)["status"], "ok")

    # --- the defect -------------------------------------------------------
    def test_edited_prompt_warns_and_names_the_entry(self):
        with tempfile.TemporaryDirectory() as td:
            registered = entries()
            edited = entries(pr_flag_prompt="run pr_flag --stand 'Echo Act IV Pro'")
            ws = self._ws(Path(td), edited, self._stamp(registered))
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("pr-flag", check["detail"])
            self.assertIn("OLD prompt", check["detail"])

    def test_edited_schedule_warns(self):
        """A cron-expression change is a real behavioural change to a live job."""
        with tempfile.TemporaryDirectory() as td:
            registered = entries()
            edited = [dict(e, cron="45 * * * *") if e["name"] == "pr-flag" else e
                      for e in entries()]
            ws = self._ws(Path(td), edited, self._stamp(registered))
            self.assertEqual(self._check(ws)["status"], "warn")

    def test_prompt_skill_swap_warns(self):
        """Swapping prompt <-> prompt_skill changes what fires, same name."""
        with tempfile.TemporaryDirectory() as td:
            registered = entries()
            edited = [dict({k: v for k, v in e.items() if k != "prompt"},
                           prompt_skill="something-else") if e["name"] == "pr-flag" else e
                      for e in entries()]
            ws = self._ws(Path(td), edited, self._stamp(registered))
            self.assertEqual(self._check(ws)["status"], "warn")

    # --- the false positives that would make it a nag nobody opens --------
    def test_non_session_owned_edit_does_not_warn(self):
        """An edit to a launchd- or codex-owned entry is not a session-cron
        problem. Warning here would train the operator to ignore the check."""
        with tempfile.TemporaryDirectory() as td:
            extra = ({"name": "daily", "cron": "7 9 * * *", "prompt": "x", "launchd": True},
                     {"name": "cx", "cron": "1 1 * * *", "prompt": "y", "execution": "codex-task"})
            registered = entries(extra=extra)
            edited = entries(extra=(dict(extra[0], prompt="x CHANGED"),
                                    dict(extra[1], prompt="y CHANGED")))
            ws = self._ws(Path(td), edited, self._stamp(registered))
            self.assertEqual(self._check(ws)["status"], "ok")

    def test_stamp_without_digests_skips_the_check(self):
        """Backward compatibility: a stamp written before this field existed has
        no data to judge, and must not manufacture a warning from its absence."""
        with tempfile.TemporaryDirectory() as td:
            ents = entries()
            stamp = {"ts": STAMPED, "registered": 2, "config_total": len(ents)}
            ws = self._ws(Path(td), entries(pr_flag_prompt="totally different"), stamp)
            self.assertEqual(self._check(ws)["status"], "ok")

    def test_malformed_digests_field_skips_rather_than_crashes(self):
        with tempfile.TemporaryDirectory() as td:
            ents = entries()
            ws = self._ws(Path(td), ents, self._stamp(ents, config_digests="not-a-dict"))
            self.assertEqual(self._check(ws)["status"], "ok")

    def test_added_and_removed_entries_are_not_drift(self):
        """Appearing/disappearing is what the expected/registered COUNT speaks
        to; reporting it here too would double-warn on a legitimate add.

        `registered=3` deliberately: the added entry raises `expected` to 3, so
        leaving the stamp at 2 makes the pre-existing COUNT check warn and this
        case would pass for the wrong reason — which is what it did on the first
        run, and the reason this file insists on keeping counts satisfied."""
        with tempfile.TemporaryDirectory() as td:
            registered = entries()
            added = entries(extra=({"name": "brand-new", "cron": "3 * * * *", "prompt": "n"},))
            ws = self._ws(Path(td), added, self._stamp(registered, registered=3))
            self.assertEqual(self._check(ws)["status"], "ok")


class OwnershipTransitionTest(unittest.TestCase):
    """A registered entry that LEAVES the session-owned set keeps firing.

    Raised by @john-the-dev on #2654: `registered < expected` runs one way only.
    Edit a registered entry to `launchd: true` and `expected` drops while the
    session job registered under the old config stays live — counts read
    registered=2 / expected=1, and `2 < 1` is false. The digest is blind here by
    design (entry_digest ignores ownership fields so an entry that correctly
    stopped being registered is not reported as *edited*), so the surplus itself
    has to be the signal.
    """

    def _ws(self, root: Path, ents, stamp):
        ws = root / "workspace"
        cfg = ws / "hosts" / "test-host" / "crons.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps(ents))
        (ws / "state").mkdir(parents=True, exist_ok=True)
        (cfg.parent / "schedule-crons-stamp.json").write_text(json.dumps(stamp))
        (ws / "state" / "session-starts.log").write_text(
            json.dumps({"host": "test-host", "session_started_at": BOOT}) + "\n")
        return ws

    def _check(self, ws):
        with mock.patch.object(health, "_local_host_labels", return_value={"test-host"}):
            return health.check_session_cron_registration(ws, host_label="test-host", runtime="claude")

    def test_launchd_transition_warns(self):
        registered = entries()
        moved = [dict(e, launchd=True) if e["name"] == "pr-flag" else e for e in entries()]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), moved,
                          {"ts": STAMPED, "registered": 2, "config_total": 2,
                           "config_digests": digest_map(registered)})
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("never de-registered", check["detail"])

    def test_codex_task_transition_warns(self):
        registered = entries()
        moved = [dict(e, execution="codex-task") if e["name"] == "pr-flag" else e
                 for e in entries()]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), moved,
                          {"ts": STAMPED, "registered": 2, "config_total": 2,
                           "config_digests": digest_map(registered)})
            self.assertEqual(self._check(ws)["status"], "warn")

    def test_deleted_entry_whose_job_is_still_live_warns(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), entries()[:1],
                          {"ts": STAMPED, "registered": 2, "config_total": 1,
                           "config_digests": digest_map(entries())})
            self.assertEqual(self._check(ws)["status"], "warn")

    def test_the_matched_case_stays_quiet(self):
        """Control: registered == expected must NOT warn, or every case above
        is passing for the wrong reason."""
        ents = entries()
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), ents,
                          {"ts": STAMPED, "registered": 2, "config_total": 2,
                           "config_digests": digest_map(ents)})
            self.assertEqual(self._check(ws)["status"], "ok")

    def test_bootstrap_fallback_surplus_warns_and_names_the_fallback(self):
        """Step 4 registers /proactive-loop when the config lacks it, so
        registered legitimately exceeds expected by one.

        This case DELIBERATELY still warns. Subtracting one for the fallback was
        the first attempt: from counts alone a benign fallback and a single real
        transition are the same surplus, so the allowance absorbs exactly one
        transition — an amnesty rather than a filter. Caught by the sibling test
        below, which failed against that version. The message names the fallback
        so the operator can tell which they are looking at."""
        no_loop = [{"name": "pr-flag", "cron": "17 * * * *", "prompt": "run pr_flag"}]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), no_loop,
                          {"ts": STAMPED, "registered": 2, "config_total": 1,
                           "config_digests": digest_map(no_loop)})
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("bootstrap", check["detail"])

    def test_a_transition_ON_TOP_of_the_fallback_still_warns(self):
        """With the fallback armed AND an entry moved out, the surplus is 2.
        This is the case that killed the allowance approach."""
        no_loop_moved = [
            {"name": "pr-flag", "cron": "17 * * * *", "prompt": "run", "launchd": True},
            {"name": "sync", "cron": "*/30 * * * *", "prompt": "sync"},
        ]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), no_loop_moved,
                          {"ts": STAMPED, "registered": 3, "config_total": 2,
                           "config_digests": digest_map(no_loop_moved)})
            self.assertEqual(self._check(ws)["status"], "warn")


class ZeroExpectedBoundaryTest(unittest.TestCase):
    """`expected == 0` is not by itself evidence of health.

    @john-the-dev on #2654: the surplus check fixed 2→1 but `expected == 0`
    short-circuited to `ok — no session-owned schedules expected` BEFORE the
    stamp was read, so the complete 1→0 transition stayed invisible. That is the
    worst form of the failure, because every session job can be stale while
    health explicitly reports that none is expected.

    Zero-expected is healthy only when nothing was REGISTERED either, and the
    stamp is what answers that. Hence three cases, and the two negatives matter
    as much as the positive: without them the fix could be "always warn at
    zero", which would warn forever on every host that legitimately has no
    session crons.
    """

    def _ws(self, root: Path, ents, stamp=None, started_at=BOOT):
        ws = root / "workspace"
        cfg = ws / "hosts" / "test-host" / "crons.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps(ents))
        (ws / "state").mkdir(parents=True, exist_ok=True)
        if stamp is not None:
            (cfg.parent / "schedule-crons-stamp.json").write_text(json.dumps(stamp))
        (ws / "state" / "session-starts.log").write_text(
            json.dumps({"host": "test-host", "session_started_at": started_at}) + "\n")
        return ws

    def _check(self, ws):
        with mock.patch.object(health, "_local_host_labels", return_value={"test-host"}):
            return health.check_session_cron_registration(ws, host_label="test-host", runtime="claude")

    # --- the positive: the LAST session cron moves out -----------------------
    def test_one_to_zero_transition_warns(self):
        """The boundary john named. One session cron, moved to launchd; the job
        registered under the old config is still firing."""
        moved = [{"name": "pr-flag", "cron": "17 * * * *", "prompt": "run", "launchd": True}]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), moved,
                          {"ts": STAMPED, "registered": 1, "config_total": 1,
                           "config_digests": digest_map(moved)})
            check = self._check(ws)
            self.assertEqual(check["status"], "warn")
            self.assertIn("never de-registered", check["detail"])

    def test_one_to_zero_by_deletion_warns(self):
        """Same boundary, reached by deleting the entry rather than re-owning it."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), [],
                          {"ts": STAMPED, "registered": 1, "config_total": 1,
                           "config_digests": {}})
            self.assertEqual(self._check(ws)["status"], "warn")

    # --- the negatives that stop the fix becoming "always warn at zero" ------
    def test_never_had_session_crons_is_healthy(self):
        """No session-owned entries AND no stamp — the case the old
        short-circuit actually existed to protect. Must stay ok, or every host
        without session crons warns forever."""
        launchd_only = [{"name": "daily", "cron": "7 9 * * *", "prompt": "x", "launchd": True}]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), launchd_only, stamp=None)
            check = self._check(ws)
            self.assertEqual(check["status"], "ok")
            self.assertIn("no session-owned schedules expected", check["detail"])

    def test_zero_expected_with_a_stamp_registering_zero_is_healthy(self):
        """Stamped, but nothing was registered — nothing can be stale."""
        launchd_only = [{"name": "daily", "cron": "7 9 * * *", "prompt": "x", "launchd": True}]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), launchd_only,
                          {"ts": STAMPED, "registered": 0, "config_total": 1,
                           "config_digests": digest_map(launchd_only)})
            self.assertEqual(self._check(ws)["status"], "ok")

    def test_zero_expected_with_a_PREVIOUS_session_stamp_is_healthy(self):
        """Session crons die with their session, so a stamp that predates this
        launch registered nothing that is still firing. Warning here would be
        advice with no subject — there is nothing to re-register."""
        launchd_only = [{"name": "daily", "cron": "7 9 * * *", "prompt": "x", "launchd": True}]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), launchd_only,
                          {"ts": BOOT - 5000, "registered": 3, "config_total": 1,
                           "config_digests": {}})
            self.assertEqual(self._check(ws)["status"], "ok")

    def test_codex_runtime_still_short_circuits(self):
        """codex has no session CronCreate surface, so none of this applies."""
        moved = [{"name": "pr-flag", "cron": "17 * * * *", "prompt": "run", "launchd": True}]
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(Path(td), moved,
                          {"ts": STAMPED, "registered": 1, "config_total": 1,
                           "config_digests": digest_map(moved)})
            with mock.patch.object(health, "_local_host_labels", return_value={"test-host"}):
                check = health.check_session_cron_registration(
                    ws, host_label="test-host", runtime="codex")
            self.assertEqual(check["status"], "ok")


class DigestUnitTest(unittest.TestCase):
    def test_digest_is_stable_across_key_order(self):
        a = {"name": "x", "cron": "* * * * *", "prompt": "p"}
        b = {"prompt": "p", "cron": "* * * * *", "name": "x"}
        self.assertEqual(entry_digest(a), entry_digest(b))

    def test_unrelated_field_does_not_move_the_digest(self):
        """`disabled`/`launchd` change WHETHER an entry registers, which the
        count covers. Folding them in would report drift for an entry that
        correctly stopped being registered at all."""
        a = {"name": "x", "cron": "* * * * *", "prompt": "p"}
        self.assertEqual(entry_digest(a), entry_digest(dict(a, launchd=True)))

    def test_field_boundary_cannot_be_forged(self):
        """Canonical JSON, not concatenation: a prompt ending in the separator
        must not collide with the next field's value."""
        a = {"name": "x", "cron": "a", "prompt": "b"}
        b = {"name": "x", "cron": "a\",\"b", "prompt": ""}
        self.assertNotEqual(entry_digest(a), entry_digest(b))

    def test_unnamed_entries_are_skipped(self):
        self.assertEqual(digest_map([{"cron": "* * * * *", "prompt": "p"}]), {})

    def test_a_non_list_config_yields_no_digests(self):
        """`crons.json` is a list by contract, but the probe hands us whatever
        parsed. Returning {} means the drift check finds no shared names and
        stays silent — a malformed config must not be reported as drift, which
        is a different (and already-covered) failure."""
        for junk in ({"not": "a list"}, "a string", None, 7):
            with self.subTest(junk=junk):
                self.assertEqual(digest_map(junk), {})

    def test_non_dict_entries_are_skipped_without_killing_the_rest(self):
        """One malformed row must not cost the digests of its neighbours — a
        raised exception here would take out the whole probe, turning a
        cosmetic config error into a dead health check."""
        got = digest_map([
            {"name": "good", "cron": "* * * * *", "prompt": "p"},
            "junk",
            None,
            42,
            {"name": "also-good", "cron": "0 * * * *", "prompt": "q"},
        ])
        self.assertEqual(sorted(got), ["also-good", "good"])

    def test_drifted_ignores_names_present_in_only_one_map(self):
        self.assertEqual(drifted({"a": "1"}, {"b": "2"}), [])

    def test_drifted_handles_non_dict_input(self):
        self.assertEqual(drifted(None, {"a": "1"}), [])
        self.assertEqual(drifted({"a": "1"}, None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
