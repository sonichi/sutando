#!/usr/bin/env python3
"""Regression test: check_dynamic_loop_freshness must make a dead dynamic loop LOUD.

The gap it covers: a `loop: "dynamic"` entry self-paces via ScheduleWakeup, so
it is not a cron job (absent from CronList) and not an OS process (invisible to
pgrep). Every other liveness probe in health-check.py keys off one of those two,
so a dynamic loop that stops re-arming pages NOBODY — the exact way the
inbox-score loop died 2026-07-21 and owner-comm sweeps lapsed for days.

Run: python3 tests/health-check-dynamic-loop-freshness.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_dynloop_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestDynamicLoopFreshness(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        self.hc.WORKSPACE_DIR = self.ws

    def tearDown(self):
        self._tmp.cleanup()

    def _crons(self, entries: list[dict]) -> None:
        host = self.hc._host_label()
        d = self.ws / "hosts" / host
        d.mkdir(parents=True, exist_ok=True)
        (d / "crons.json").write_text(json.dumps(
            [{"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"}]
            + entries
        ))

    def _declare(self, name: str = "inbox-score") -> None:
        self._crons([{"name": name, "prompt_skill": name, "loop": "dynamic",
                      "loop_hint": "~10m active, ~40m quiet"}])

    def _stamp(self, name: str = "inbox-score", *, age_s: float = 0.0,
               next_delay_s=2400, ts="auto", mtime_age_s: float | None = None,
               raw: str | None = None) -> Path:
        p = self.ws / "state" / f"dynamic-loop-{name}.alive"
        if raw is not None:
            p.write_text(raw)
        else:
            payload: dict = {}
            if ts == "auto":
                payload["ts"] = time.time() - age_s
            elif ts is not None:
                payload["ts"] = ts
            if next_delay_s is not None:
                payload["next_delay_s"] = next_delay_s
            p.write_text(json.dumps(payload))
        # mtime is set INDEPENDENTLY of ts on purpose — the two disagreeing is
        # the whole point of several tests below.
        stamp_age = age_s if mtime_age_s is None else mtime_age_s
        if stamp_age:
            t = time.time() - stamp_age
            os.utime(p, (t, t))
        return p

    def _one(self):
        out = self.hc.check_dynamic_loop_freshness()
        self.assertEqual(len(out), 1, f"expected exactly one check, got {out!r}")
        return out[0]

    # --- lane awareness -------------------------------------------------

    def test_host_declaring_no_dynamic_loop_emits_NOTHING(self):
        # The comm-sweep lesson, mirrored: a permanent warn on a host with
        # nothing to monitor is how a health output gets ignored, which would
        # take this probe's real alarms down with it. No declaration => no row.
        self._crons([{"name": "pr-flag", "cron": "17 * * * *", "prompt": "..."}])
        self.assertEqual(self.hc.check_dynamic_loop_freshness(), [])

    def test_no_crons_file_at_all_emits_nothing(self):
        self.assertEqual(self.hc.check_dynamic_loop_freshness(), [])

    def test_declared_but_never_stamped_warns(self):
        # Launched but not re-arming. This must NOT be silent: it is the state a
        # loop lands in when its very first re-arm fails.
        self._declare()
        out = self._one()
        self.assertEqual(out["name"], "dynamic-loop:inbox-score")
        self.assertEqual(out["status"], "warn")
        self.assertIn("never stamped", out["detail"])

    def test_stalled_stays_down_even_when_the_declaration_is_REMOVED(self):
        # THE DANGEROUS DIRECTION, tested by actually removing the declaration.
        #
        # An earlier revision of this test did NOT do that — it left the entry in
        # crons.json and only asserted _host_dynamic_loops wasn't called twice,
        # then claimed removal-proofness in the docstring. bassilkhilo-ag2
        # reproduced the real behaviour on #2692: enumeration came from crons.json
        # alone, so deleting the entry dropped a stalled loop out of the output
        # entirely. A test whose own comment concedes it isn't exercising the
        # claim is not covering it.
        self._declare()
        self._stamp(age_s=99_999, next_delay_s=2400)
        self._crons([])  # entry deleted; the sentinel on disk is untouched
        out = self._one()
        self.assertEqual(out["name"], "dynamic-loop:inbox-score")
        self.assertEqual(out["status"], "down",
                         "an undeclared loop was dropped — deleting a crons.json entry "
                         "must not be able to silence a stall that is still real")

    def test_config_is_consulted_once_not_per_loop(self):
        # The narrower guarantee the old test actually held, kept on its own so
        # the removal case above tests removal and nothing else.
        self._declare()
        self._stamp(age_s=99_999, next_delay_s=2400)
        with mock.patch.object(self.hc, "_host_dynamic_loops",
                               wraps=self.hc._host_dynamic_loops) as spy:
            self._one()
        self.assertEqual(spy.call_count, 1)

    def test_a_stray_but_FRESH_sentinel_does_not_manufacture_an_alarm(self):
        # The other half of the widened axis. Enumerating sentinels off disk must
        # not turn every leftover file into a permanent warn — that is the same
        # ignored-output failure the lane-awareness rule exists to prevent. An
        # undeclared loop that is still re-arming is simply healthy.
        self._crons([])
        self._stamp(age_s=60, next_delay_s=2400)
        self.assertEqual(self._one()["status"], "ok")

    # --- the sentinel carries its own threshold -------------------------

    def test_fresh_is_ok(self):
        self._declare()
        self._stamp(age_s=60, next_delay_s=2400)
        out = self._one()
        self.assertEqual(out["status"], "ok")
        self.assertIn("cadence 40m", out["detail"])

    def test_past_its_own_rearm_deadline_warns(self):
        self._declare()
        self._stamp(age_s=2400 + 300, next_delay_s=2400)  # > delay+120, < 2*delay+300
        out = self._one()
        self.assertEqual(out["status"], "warn")
        self.assertIn("re-arm deadline", out["detail"])

    def test_double_its_own_cadence_is_down(self):
        self._declare()
        self._stamp(age_s=2 * 2400 + 600, next_delay_s=2400)
        out = self._one()
        self.assertEqual(out["status"], "down")
        self.assertIn("stopped re-arming", out["detail"])

    def test_thresholds_FOLLOW_the_sentinel_not_a_hardcoded_cadence(self):
        # The load-bearing property. A self-pacing loop changes its own cadence
        # (~10m busy, ~40m quiet), so a fixed threshold either alarms falsely on
        # the slow end or goes blind on the fast end. Same age, opposite verdict,
        # decided ONLY by the next_delay_s the loop itself stamped.
        self._declare()
        self._stamp(age_s=1800, next_delay_s=2400)
        self.assertEqual(self._one()["status"], "ok")
        self._stamp(age_s=1800, next_delay_s=300)
        self.assertEqual(self._one()["status"], "down")

    # --- honest clock ---------------------------------------------------

    def test_ancient_ts_wins_over_a_FRESH_mtime(self):
        # state/dynamic-loop-*.alive is NOT vault-excluded (unlike
        # state/cores/*.alive, which is excluded precisely so a synced mtime
        # cannot fake liveness). A sync can therefore hand this file a fresh
        # mtime while the loop has been dead for an hour. The self-reported ts
        # is the honest clock.
        self._declare()
        self._stamp(age_s=4000, next_delay_s=600, mtime_age_s=0)
        out = self._one()
        self.assertEqual(out["status"], "down",
                         "a fresh mtime masked a dead loop — probe is reading mtime, not ts")

    def test_unparseable_payload_falls_back_to_mtime_and_SAYS_so(self):
        self._declare()
        self._stamp(raw="{not json", mtime_age_s=60)
        out = self._one()
        self.assertEqual(out["status"], "ok")
        self.assertIn("fell back to mtime", out["detail"])

    def test_malformed_next_delay_assumes_a_conservative_default_and_SAYS_so(self):
        # A corrupt cadence must not manufacture an alarm; 1800s is the slow end
        # of /loop's documented fallback range. The caveat has to reach the
        # detail, or the reader cannot tell a measured verdict from an assumed one.
        self._declare()
        self._stamp(age_s=60, next_delay_s="soon")
        out = self._one()
        self.assertEqual(out["status"], "ok")
        self.assertIn("no usable `next_delay_s`", out["detail"])
        self.assertIn("assumed 30m", out["detail"])

    def test_boolean_next_delay_is_rejected_not_read_as_one_second(self):
        # bool is an int in Python: a `true` read naively becomes a 1-second
        # cadence, and every subsequent check pages. Fails closed to the default.
        self._declare()
        self._stamp(age_s=60, next_delay_s=True)
        self.assertEqual(self._one()["status"], "ok")

    def test_read_failure_warns_not_crashes(self):
        # A sentinel that exists() but whose read/stat raises (races, a broken
        # mount) must degrade to warn, never propagate an OSError that would
        # crash the whole health check.
        class _ReadFails:
            def exists(self):
                return True

            def read_text(self):
                raise OSError("simulated read failure")

            def stat(self):
                raise OSError("simulated stat failure")

        self._declare()
        with mock.patch.object(self.hc, "status_read_path", return_value=_ReadFails()):
            out = self._one()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])

    # --- enumeration ----------------------------------------------------

    def test_each_declared_loop_gets_its_own_row(self):
        self._crons([
            {"name": "inbox-score", "prompt_skill": "inbox-score", "loop": "dynamic"},
            {"name": "watch-feeds", "prompt_skill": "watch-feeds", "loop": "dynamic"},
        ])
        self._stamp("inbox-score", age_s=60, next_delay_s=2400)
        out = self.hc.check_dynamic_loop_freshness()
        by_name = {c["name"]: c for c in out}
        self.assertEqual(set(by_name), {"dynamic-loop:inbox-score", "dynamic-loop:watch-feeds"})
        self.assertEqual(by_name["dynamic-loop:inbox-score"]["status"], "ok")
        self.assertEqual(by_name["dynamic-loop:watch-feeds"]["status"], "warn")

    def test_run_all_checks_emits_the_dynamic_loop_check(self):
        # Reachability guard (mirrors PR #1898): the probe is useless if it is
        # defined but never wired into run_all_checks(). Exercise the real call
        # site — with a loop declared, so an unwired probe cannot pass by
        # emitting nothing.
        self._declare()
        self._stamp(age_s=60, next_delay_s=2400)
        try:
            checks = self.hc.run_all_checks()
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"run_all_checks() raised: {e!r}")
        names = [c.get("name") for c in checks if isinstance(c, dict)]
        self.assertIn("dynamic-loop:inbox-score", names,
                      "run_all_checks() emitted no dynamic-loop check (branch unreachable)")

    def test_run_all_checks_sees_an_UNDECLARED_stalled_loop(self):
        # Reachability for the union branch specifically. The unit-level check
        # above can pass while run_all_checks() still filters by declaration, and
        # run_all_checks() is the only path the operator ever actually sees — so
        # the dangerous direction has to be proven at the real call site too.
        self._crons([])
        self._stamp(age_s=99_999, next_delay_s=2400)
        checks = self.hc.run_all_checks()
        row = next((c for c in checks
                    if isinstance(c, dict) and c.get("name") == "dynamic-loop:inbox-score"), None)
        self.assertIsNotNone(row, "a stalled loop vanished from run_all_checks() once its "
                                  "crons.json entry was deleted")
        self.assertEqual(row["status"], "down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
