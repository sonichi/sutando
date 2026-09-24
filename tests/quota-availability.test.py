#!/usr/bin/env python3
"""`src/quota_availability.py`: one authority for whether the proxy's quota
record may speak for a seat.

Pinned here: the skill's module re-exports THIS policy (no second copy), the
record reader's shapes, the seat-routing probe's tri-state through its
injectable runners, and the one decision the delivery gate relies on --
`provider_allows_now` is True only for routed AND fresh AND accepted, and
False for every other shape, because silence never overrides a limit banner.

Run: python3 tests/quota-availability.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import quota_availability as qa  # noqa: E402

NOW = 1_790_000_000.0
PROXY = "http://localhost:7846"
SEAT = "sutando-worker-abc"


def _iso(epoch: float, millis: bool = True) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if millis else dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(status="allowed", available=True, age_s=30.0, **extra) -> dict:
    d = {"available": available, "last_checked": _iso(NOW - age_s),
         "headers": {"anthropic-ratelimit-unified-status": status,
                     "anthropic-ratelimit-unified-7d-reset": "1790499600"}}
    d.update(extra)
    return d


def _R(rc: int, out: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _seat(base_url=PROXY, session=SEAT, pids=("6648",), extra_argv=""):
    """Runners that describe one seat: `list-panes` names `pids`, and each pid's
    argv carries `--name <session>` plus the env pairs `ps eww` prints."""
    env = f"ANTHROPIC_BASE_URL={base_url} " if base_url is not None else ""
    argv = f"{env}HOME=/x claude --name {session} {extra_argv}".strip()
    return (lambda sock, *a: _R(0, "\n".join(pids) + "\n"),
            lambda pid: _R(0, argv))


class TestTheSkillReExportsThisPolicy(unittest.TestCase):
    def test_the_skill_module_hands_out_the_same_functions(self):
        path = ROOT / "skills" / "quota-tracker" / "scripts" / "quota_availability.py"
        spec = importlib.util.spec_from_file_location("skill_quota_availability", path)
        skill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(skill)
        for name in ("points_at_credential_proxy", "resolve_available", "availability_decision"):
            self.assertIs(getattr(skill, name), getattr(qa, name), name)
        self.assertEqual(skill.PROXY_PORT, qa.PROXY_PORT)


class TestPolicyUnchanged(unittest.TestCase):
    """The moved functions decide exactly what they decided in the skill."""

    def test_points_at_credential_proxy(self):
        self.assertTrue(qa.points_at_credential_proxy(PROXY))
        self.assertTrue(qa.points_at_credential_proxy("localhost:7846"))
        self.assertFalse(qa.points_at_credential_proxy("https://api.anthropic.com"))
        self.assertFalse(qa.points_at_credential_proxy("http://localhost:7847"))
        self.assertFalse(qa.points_at_credential_proxy(None))
        self.assertFalse(qa.points_at_credential_proxy(""))

    def test_resolve_available_rejected_beats_the_flag_and_the_flag_beats_the_status(self):
        self.assertFalse(qa.resolve_available("rejected", True))
        self.assertTrue(qa.resolve_available("allowed_warning", True))
        self.assertFalse(qa.resolve_available("allowed", False))
        self.assertTrue(qa.resolve_available("allowed", None))
        self.assertFalse(qa.resolve_available("allowed_warning", None))

    def test_availability_decision_names_the_first_reason_that_fails(self):
        rec = _record()
        self.assertEqual(qa.availability_decision(rec, base_url=PROXY, stale=False)["unavailable_reason"], None)
        self.assertEqual(qa.availability_decision(rec, base_url=None, stale=False)["unavailable_reason"], "not-routed")
        self.assertEqual(qa.availability_decision(rec, base_url=PROXY, stale=True)["unavailable_reason"], "stale")
        self.assertEqual(qa.availability_decision(_record("rejected", False), base_url=PROXY, stale=False)["unavailable_reason"], "rejected")


class RecordFixture(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.path = self.ws / "state" / "quota-state.json"

    def write(self, data) -> None:
        self.path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


class TestReadQuotaRecord(RecordFixture):
    def test_a_record_reads_its_payload_and_age(self):
        self.write(_record(age_s=30))
        rec = qa.read_quota_record(self.ws, now=NOW)
        self.assertEqual(rec.payload["headers"]["anthropic-ratelimit-unified-status"], "allowed")
        self.assertAlmostEqual(rec.age_s, 30, delta=0.01)
        self.assertTrue(rec.fresh())

    def test_last_checked_is_read_in_both_shapes_the_proxy_writes(self):
        self.write({"headers": {}, "last_checked": _iso(NOW - 45, millis=True)})
        self.assertAlmostEqual(qa.read_quota_record(self.ws, now=NOW).age_s, 45, delta=0.01)
        self.write({"headers": {}, "last_checked": _iso(NOW - 45, millis=False)})
        self.assertAlmostEqual(qa.read_quota_record(self.ws, now=NOW).age_s, 45, delta=0.01)

    def test_no_or_unparseable_timestamp_falls_back_to_the_file_mtime(self):
        for data in ({"headers": {}}, {"headers": {}, "last_checked": "yesterday-ish"}):
            self.write(data)
            os.utime(self.path, (NOW - 100, NOW - 100))
            self.assertAlmostEqual(qa.read_quota_record(self.ws, now=NOW).age_s, 100, delta=0.01)

    def test_absent_and_malformed_records_read_as_none_not_as_an_error(self):
        self.assertIsNone(qa.read_quota_record(self.ws, now=NOW))
        self.write("{not json")
        self.assertIsNone(qa.read_quota_record(self.ws, now=NOW))
        self.write("[1, 2, 3]")
        self.assertIsNone(qa.read_quota_record(self.ws, now=NOW))

    def test_a_record_dated_in_the_future_is_not_fresh(self):
        self.write({"headers": {}, "last_checked": _iso(NOW + 300)})
        self.assertFalse(qa.read_quota_record(self.ws, now=NOW).fresh())

    def test_exactly_at_the_freshness_bound_still_counts(self):
        self.write({"headers": {}, "last_checked": _iso(NOW - qa.FRESH_SEC)})
        self.assertTrue(qa.read_quota_record(self.ws, now=NOW).fresh())
        self.write({"headers": {}, "last_checked": _iso(NOW - qa.FRESH_SEC - 1)})
        self.assertFalse(qa.read_quota_record(self.ws, now=NOW).fresh())

    def test_default_freshness_is_minutes_not_hours(self):
        # health-check's six-hour horizon asks "is the proxy wired"; this asks "is
        # the limit lifted NOW", and a limit can begin at any moment in between.
        self.assertLessEqual(qa.FRESH_SEC, 15 * 60)


class TestSeatEnvBaseUrl(unittest.TestCase):
    """The probe health-check used to own, now shared: tri-state, never a guess."""

    def test_a_routed_seat_reports_its_base_url(self):
        tm, ps = _seat(PROXY)
        env = qa.seat_env_base_url("/tmp/t.sock", SEAT, tm, ps)
        self.assertEqual((env.observed, env.base_url), (True, PROXY))

    def test_a_seat_without_the_variable_is_observed_and_unrouted(self):
        tm, ps = _seat(base_url=None)
        env = qa.seat_env_base_url("/tmp/t.sock", SEAT, tm, ps)
        self.assertEqual((env.observed, env.base_url), (True, None))

    def test_the_equals_spelling_of_name_is_recognised(self):
        tm = lambda sock, *a: _R(0, "6648\n")  # noqa: E731
        ps = lambda pid: _R(0, f"ANTHROPIC_BASE_URL={PROXY} claude --name={SEAT}")  # noqa: E731
        self.assertEqual(qa.seat_env_base_url("/tmp/t.sock", SEAT, tm, ps).base_url, PROXY)

    def test_unobserved_when_nothing_can_vouch(self):
        cases = {
            "no socket": (None, *_seat()),
            "tmux failed": ("/tmp/t.sock", lambda s, *a: _R(1, ""), _seat()[1]),
            "no panes": ("/tmp/t.sock", lambda s, *a: _R(0, ""), _seat()[1]),
            "ps raised": ("/tmp/t.sock", _seat()[0], lambda pid: (_ for _ in ()).throw(OSError("boom"))),
            "another session's process": ("/tmp/t.sock", *_seat(session="somebody-else")),
            "two matching panes": ("/tmp/t.sock", *_seat(pids=("1", "2"))),
            "no env pairs at all": ("/tmp/t.sock", _seat()[0], lambda pid: _R(0, f"claude --name {SEAT}")),
        }
        for name, (sock, tm, ps) in cases.items():
            env = qa.seat_env_base_url(sock, SEAT, tm, ps)
            self.assertFalse(env.observed, name)
            self.assertIsNone(env.base_url, name)

    def test_a_sibling_pane_that_is_not_the_seat_is_skipped_not_counted(self):
        # The gateway window lives in the same session; only the `--name <session>` process counts.
        def tm(sock, *a):
            return _R(0, "10\n20\n")

        def ps(pid):
            if pid == "20":
                return _R(0, f"ANTHROPIC_BASE_URL={PROXY} claude --name {SEAT}")
            return _R(0, "node gateway-bridge.js")

        self.assertEqual(qa.seat_env_base_url("/tmp/t.sock", SEAT, tm, ps).base_url, PROXY)


class TestProviderAllowsNow(RecordFixture):
    """The delivery gate's one question, answered fail-closed."""

    def _allows(self, seat=None, session=SEAT, **kw):
        tm, ps = seat if seat is not None else _seat(PROXY)
        return qa.provider_allows_now(self.ws, "/tmp/t.sock", session, now=NOW,
                                      tmux_runner=tm, ps_runner=ps, **kw)

    def test_true_only_for_routed_fresh_and_accepted(self):
        self.write(_record())
        self.assertTrue(self._allows())

    def test_an_allowed_warning_with_the_flag_set_is_still_accepted(self):
        # The one shape the two old readers split on: the authority decides, once.
        self.write(_record("allowed_warning", True))
        self.assertTrue(self._allows())

    def test_false_when_the_seat_is_unrouted(self):
        self.write(_record())
        self.assertFalse(self._allows(seat=_seat(base_url=None)), "no variable")
        self.assertFalse(self._allows(seat=_seat(base_url="https://api.anthropic.com")), "direct")

    def test_false_when_the_seat_cannot_be_observed(self):
        self.write(_record())
        self.assertFalse(self._allows(seat=(lambda s, *a: _R(1, ""), _seat()[1])))
        self.assertFalse(self._allows(seat=_seat(pids=("1", "2"))))

    def test_false_without_a_session_to_vouch_for(self):
        self.write(_record())
        self.assertFalse(self._allows(session=None))
        self.assertFalse(self._allows(session=""))

    def test_false_for_every_record_shape_that_does_not_vouch(self):
        cases = {
            "stale": _record(age_s=3600),
            "rejected": _record("rejected", False),
            "flag false": _record("allowed", False),
            "silent": {"last_checked": _iso(NOW - 30), "headers": {}},
        }
        for name, data in cases.items():
            self.write(data)
            self.assertFalse(self._allows(), name)
        self.path.unlink()
        self.assertFalse(self._allows(), "absent")
        self.write("{broken")
        self.assertFalse(self._allows(), "malformed")

    def test_the_freshness_window_is_a_parameter(self):
        self.write(_record(age_s=3000))
        self.assertFalse(self._allows())
        self.assertTrue(self._allows(fresh_sec=3600))

    def test_the_probe_is_not_run_when_the_record_is_missing(self):
        # No record means False already; a slow tmux must not delay that verdict.
        called = []
        tm = lambda s, *a: called.append(1) or _R(0, "6648\n")  # noqa: E731
        self.assertFalse(qa.provider_allows_now(self.ws, "/tmp/t.sock", SEAT, now=NOW,
                                                tmux_runner=tm, ps_runner=_seat()[1]))
        self.assertEqual(called, [])



class TestProbeRefreshesRecord(RecordFixture):
    """The probe exists for one case: routed seat, record too old to vouch. It
    sends one cheapest-model request through the proxy so the proxy rewrites the
    record, and the verdict is read from the record afterwards, never from the
    probe's exit code."""

    def _runner(self, calls, write=None, raise_=None):
        def run(argv, **kw):
            calls.append((argv, kw))
            # The marker must already be claimed when the request goes out.
            self.assertTrue((self.ws / "state" / qa.PROBE_MARK).exists())
            if write is not None:
                self.write(write)
            if raise_ is not None:
                raise raise_
            return _R(0, "ok")
        return run

    def test_only_toward_the_proxy(self):
        calls = []
        for url in (None, "", "https://api.anthropic.com", "http://localhost:7847"):
            self.assertFalse(qa.probe_refreshes_record(self.ws, url, now=NOW, runner=self._runner(calls)), url)
        self.assertEqual(calls, [])

    def test_one_cheapest_model_request_through_the_proxy(self):
        calls = []
        self.assertTrue(qa.probe_refreshes_record(self.ws, PROXY, now=NOW, runner=self._runner(calls)))
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertEqual(argv, ["claude", "-p", "ok", "--model", qa.PROBE_MODEL])
        self.assertEqual(kw["env"]["ANTHROPIC_BASE_URL"], PROXY)
        self.assertLessEqual(kw["timeout"], 90)

    def test_at_most_one_probe_per_window_host_wide(self):
        calls = []
        self.assertTrue(qa.probe_refreshes_record(self.ws, PROXY, now=NOW, runner=self._runner(calls)))
        self.assertFalse(qa.probe_refreshes_record(self.ws, PROXY, now=NOW + 5, runner=self._runner(calls)))
        self.assertFalse(qa.probe_refreshes_record(self.ws, PROXY, now=NOW + qa.FRESH_SEC - 1, runner=self._runner(calls)))
        self.assertTrue(qa.probe_refreshes_record(self.ws, PROXY, now=NOW + qa.FRESH_SEC, runner=self._runner(calls)))
        self.assertEqual(len(calls), 2)

    def test_a_failing_probe_still_counts_as_sent(self):
        # The limit refusing the request is the answer the record will now carry.
        calls = []
        self.assertTrue(qa.probe_refreshes_record(self.ws, PROXY, now=NOW,
                                                  runner=self._runner(calls, raise_=OSError("boom"))))
        self.assertEqual(len(calls), 1)


class TestProviderAllowsNowWithProbe(RecordFixture):
    def _allows(self, runner, probe=True, now=NOW, seat=None):
        tm, ps = seat if seat is not None else _seat(PROXY)
        return qa.provider_allows_now(self.ws, "/tmp/t.sock", SEAT, now=now, tmux_runner=tm,
                                      ps_runner=ps, probe=probe, probe_runner=runner)

    def _proxy_that_writes(self, calls, record, rc=0):
        def run(argv, **kw):
            calls.append(argv)
            if record is not None:
                self.write(record)
            return _R(rc, "ok" if rc == 0 else "rate limited")
        return run

    def test_a_probe_that_failed_while_the_record_refreshed_to_allowed_releases(self):
        # The verdict is the record's, never the probe's exit code.
        self.write(_record(age_s=3600))
        calls = []
        self.assertTrue(self._allows(self._proxy_that_writes(calls, _record(age_s=0), rc=1)))
        self.assertEqual(len(calls), 1)

    def test_a_probe_that_succeeded_while_the_record_stayed_stale_holds(self):
        self.write(_record(age_s=3600))
        calls = []
        self.assertFalse(self._allows(self._proxy_that_writes(calls, None, rc=0)))
        self.assertEqual(len(calls), 1)

    def test_off_by_default_never_probes(self):
        calls = []
        self.assertFalse(qa.provider_allows_now(self.ws, "/tmp/t.sock", SEAT, now=NOW,
                                                tmux_runner=_seat()[0], ps_runner=_seat()[1],
                                                probe_runner=self._proxy_that_writes(calls, _record())))
        self.assertEqual(calls, [])

    def test_a_stale_record_on_a_routed_seat_is_refreshed_and_re_read(self):
        self.write(_record(age_s=3600))
        calls = []
        self.assertTrue(self._allows(self._proxy_that_writes(calls, _record(age_s=0))))
        self.assertEqual(len(calls), 1)

    def test_an_absent_record_on_a_routed_seat_is_probed_for(self):
        calls = []
        self.assertTrue(self._allows(self._proxy_that_writes(calls, _record(age_s=0))))
        self.assertEqual(len(calls), 1)

    def test_a_probe_the_limit_refuses_leaves_the_hold(self):
        self.write(_record(age_s=3600))
        calls = []
        self.assertFalse(self._allows(self._proxy_that_writes(calls, _record("rejected", False, age_s=0))))
        self.assertEqual(len(calls), 1)

    def test_a_probe_that_refreshes_nothing_leaves_the_hold(self):
        self.write(_record(age_s=3600))
        calls = []
        self.assertFalse(self._allows(self._proxy_that_writes(calls, None)))
        self.assertEqual(len(calls), 1)

    def test_a_fresh_record_is_never_probed(self):
        self.write(_record(age_s=30))
        calls = []
        self.assertTrue(self._allows(self._proxy_that_writes(calls, None)))
        self.assertEqual(calls, [])

    def test_an_unrouted_or_unobserved_seat_is_never_probed(self):
        self.write(_record(age_s=3600))
        calls = []
        self.assertFalse(self._allows(self._proxy_that_writes(calls, _record(age_s=0)), seat=_seat(base_url=None)))
        self.assertFalse(self._allows(self._proxy_that_writes(calls, _record(age_s=0)),
                                      seat=(lambda s, *a: _R(1, ""), _seat()[1])))
        self.assertEqual(calls, [])

    def test_the_second_notifier_in_the_window_does_not_probe_again(self):
        self.write(_record(age_s=3600))
        calls = []
        self.assertFalse(self._allows(self._proxy_that_writes(calls, None)))
        self.assertFalse(self._allows(self._proxy_that_writes(calls, None), now=NOW + 30))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
