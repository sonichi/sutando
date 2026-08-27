"""check_onboarding_status: the core-side reader of the desktop checklist's
agent surface (onboarding v2, ag2space-cinny-desktop#165 S4).

Covers: absent file → None; todo rows → warn naming them; all-satisfied → ok;
unreadable → warn (never raises).
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)


class OnboardingStatusCheckTest(unittest.TestCase):
    def _with_workspace(self, payload):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name)
        if payload is not None:
            (ws / "state").mkdir()
            f = ws / "state" / "onboarding-status.json"
            if isinstance(payload, str):
                f.write_text(payload)
            else:
                f.write_text(json.dumps(payload))
        orig = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = ws
        self.addCleanup(lambda: setattr(hc, "WORKSPACE_DIR", orig))
        return ws

    def _core_heartbeat(self, ws, *, fresh: bool):
        """Write this host's core heartbeat; fresh=False makes it look dead."""
        import os
        import time
        d = ws / "state" / "cores"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{hc._host_label()}.alive"
        f.write_text(json.dumps({"ts": int(time.time())}))
        if not fresh:
            old = time.time() - 3600
            os.utime(f, (old, old))
        return f

    def test_stale_core_row_reported_as_stale_mirror_not_setup_gap(self):
        """The mirror said 'core not running' while the core's own heartbeat was
        19s old — the probe reported a 9h-old third-party claim as current fact."""
        ws = self._with_workspace(
            {"updated_at": 1, "rows": {"core": {"state": "todo",
                                                "detail": "core not running"}}}
        )
        self._core_heartbeat(ws, fresh=True)
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("stale", out["detail"].lower())
        # The false claim must NOT survive as user-facing incompleteness.
        self.assertNotIn("setup incomplete", out["detail"])

    def test_detail_less_core_row_is_not_assumed_to_be_the_down_state(self):
        """Fail safe: a todo core row with no detail says nothing about WHY, so a
        heartbeat cannot refute it — report the gap rather than suppress it."""
        ws = self._with_workspace({"updated_at": 1, "rows": {"core": {"state": "todo"}}})
        self._core_heartbeat(ws, fresh=True)
        out = hc.check_onboarding_status()
        self.assertIn("setup incomplete", out["detail"])

    def test_fresh_heartbeat_does_not_hide_a_signin_gap_on_a_running_core(self):
        """Control for the over-broad first cut: the writer emits TWO core todo
        details, and a heartbeat refutes only 'not running'."""
        ws = self._with_workspace(
            {"updated_at": 1, "rows": {"core": {
                "state": "todo", "detail": "core running, Claude sign-in required",
                "claude_authed": False}}}
        )
        self._core_heartbeat(ws, fresh=True)
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("setup incomplete", out["detail"])
        self.assertIn("sign-in", out["detail"])
        self.assertNotIn("stale", out["detail"].lower())

    def test_core_row_still_reported_when_heartbeat_is_dead(self):
        """Mutation guard: with no live heartbeat the row is a REAL gap, so the
        stale-mirror branch must not swallow it."""
        ws = self._with_workspace(
            {"updated_at": 1, "rows": {"core": {"state": "todo",
                                                "detail": "core not running"}}}
        )
        self._core_heartbeat(ws, fresh=False)
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("setup incomplete", out["detail"])
        self.assertIn("core", out["detail"])

    def test_a_peer_hosts_heartbeat_does_not_silence_a_local_gap(self):
        """The reason this uses _fresh_local_core_record() and not the fleet-wide
        resolver: that one globs every synced state/cores/*.alive and takes the
        freshest, which the workspace contract permits to be another machine's.
        """
        import time
        ws = self._with_workspace(
            {"updated_at": 1, "rows": {"core": {"state": "todo",
                                                "detail": "core not running"}}}
        )
        d = ws / "state" / "cores"
        d.mkdir(parents=True, exist_ok=True)
        peer = d / "some-peer-host.alive"
        peer.write_text(json.dumps({"ts": int(time.time())}))
        self.assertFalse((d / f"{hc._host_label()}.alive").exists(),
                         "premise: THIS host has no heartbeat, only the peer does")

        out = hc.check_onboarding_status()

        self.assertEqual(out["status"], "warn",
                         f"a peer's heartbeat must not clear a local gap: {out!r}")
        self.assertIn("setup incomplete", out["detail"])
        self.assertIn("core", out["detail"])

    def test_other_todo_rows_survive_a_stale_core_row(self):
        ws = self._with_workspace(
            {"updated_at": 1, "rows": {"core": {"state": "todo",
                                                "detail": "core not running"},
                                       "gateway": {"state": "todo"}}}
        )
        self._core_heartbeat(ws, fresh=True)
        out = hc.check_onboarding_status()
        self.assertIn("stale", out["detail"].lower())
        self.assertIn("gateway", out["detail"])

    def test_none_when_file_absent(self):
        self._with_workspace(None)
        self.assertIsNone(hc.check_onboarding_status())

    def test_warn_names_todo_rows(self):
        self._with_workspace(
            {
                "updated_at": 0,
                "rows": {
                    "core": {"state": "done"},
                    "gateway": {"state": "todo"},
                    "voice_creds": {"state": "optional"},
                },
            }
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("gateway", out["detail"])
        self.assertNotIn("core,", out["detail"])

    def test_ok_when_no_todo(self):
        self._with_workspace(
            {"updated_at": 0, "rows": {"core": {"state": "done"}, "accessibility": {"state": "optional"}}}
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "ok")

    def test_warn_on_list_payload(self):
        # Codex P1: a top-level list must degrade to 'unreadable', not raise.
        self._with_workspace("[]")
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])

    def test_warn_on_list_rows(self):
        # Codex P1: rows as a list (frontend bug) must also degrade cleanly.
        self._with_workspace({"updated_at": 0, "rows": []})
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])

    def test_ok_with_null_updated_at(self):
        self._with_workspace({"updated_at": None, "rows": {"core": {"state": "done"}}})
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "ok")

    def test_warn_on_unreadable(self):
        self._with_workspace("{not json")
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])


    def test_a_non_string_detail_warns_rather_than_raising(self):
        """The Console (a separate repo) writes this file, which is why every
        other shape here is guarded. `AttributeError` is not in the except tuple
        and the call site has ZERO enclosing try blocks, so a raise here loses
        every other check's result — one bad optional field becomes an outage."""
        for bad in (3, {"a": 1}, ["x"], True):
            self._with_workspace(
                {"updated_at": 0, "rows": {"gateway": {"state": "todo", "detail": bad}}}
            )
            out = hc.check_onboarding_status()
            self.assertEqual(out["status"], "warn", f"detail={bad!r}: {out}")
            self.assertIn("gateway", out["detail"])

    def test_a_verbose_detail_is_clamped(self):
        """One chatty row must not dominate the line the owner scans."""
        self._with_workspace(
            {"updated_at": 0, "rows": {"gateway": {"state": "todo", "detail": "z" * 400}}}
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertNotIn("z" * 200, out["detail"])
        self.assertIn("z" * 120, out["detail"])

    def test_a_todo_row_carries_its_own_detail(self):
        """`gateway` alone cannot distinguish "not running" from a reconnect —
        the writer populates `detail` to say which, and it was being dropped."""
        self._with_workspace(
            {
                "updated_at": 0,
                "rows": {
                    "gateway": {"state": "todo",
                                "detail": "gateway process up, relay not connected"},
                    "core": {"state": "todo"},
                },
            }
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("gateway (gateway process up, relay not connected)", out["detail"])
        # A row with no detail still renders as the bare name.
        self.assertIn("core", out["detail"])
        self.assertNotIn("core (", out["detail"])

    # --- age rendering: an unknown timestamp must not read as a number -------

    def test_a_missing_updated_at_is_unknown_not_the_whole_epoch(self):
        """`int(None or 0)` made the age the unix time itself — the warn line
        read "as of 1786962010s ago" (~56 years) as though it were measured."""
        for payload in ({"rows": {"gateway": {"state": "todo"}}},
                        {"updated_at": None, "rows": {"gateway": {"state": "todo"}}},
                        {"updated_at": 0, "rows": {"gateway": {"state": "todo"}}}):
            self._with_workspace(payload)
            out = hc.check_onboarding_status()
            self.assertEqual(out["status"], "warn", out)
            self.assertIn("age unknown", out["detail"], payload)
            self.assertNotIn("s ago", out["detail"], payload)

    def test_the_ok_line_carries_the_same_guard(self):
        """The all-satisfied line rendered it too, and that is the line a reader
        is least likely to question — it says everything is fine."""
        self._with_workspace({"updated_at": 0, "rows": {"gateway": {"state": "done"}}})
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("age unknown", out["detail"])

    def test_a_real_timestamp_still_renders_as_seconds(self):
        """Control: without this, returning "age unknown" unconditionally passes
        both tests above while destroying the age the probe exists to report."""
        import time
        for state, want_status in (("todo", "warn"), ("done", "ok")):
            self._with_workspace({"updated_at": int(time.time()) - 42,
                                  "rows": {"gateway": {"state": state}}})
            out = hc.check_onboarding_status()
            self.assertEqual(out["status"], want_status, out)
            self.assertIn("s ago", out["detail"])
            self.assertNotIn("age unknown", out["detail"])

    def test_an_unusable_updated_at_TYPE_still_reads_as_unreadable(self):
        """Pins what this change does NOT touch: a non-numeric updated_at keeps
        raising into the existing handler rather than degrading to unknown."""
        self._with_workspace({"updated_at": {"a": 1},
                              "rows": {"gateway": {"state": "todo"}}})
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
