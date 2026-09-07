#!/usr/bin/env python3
"""check-pending-questions must not claim delivery it did not achieve.

2026-07-21: the cron printed "Notified: 16 pending questions" on a host where no
bridge was draining results/proactive-*.txt. The DM never reached the owner; only
a local macOS notification did. A notifier that reports success regardless of
outcome is how a blocked decision sits unseen for a day.
"""
import importlib.util
import time
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(results_dir):
    spec = importlib.util.spec_from_file_location("cpq", REPO / "src" / "check-pending-questions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RESULTS_DIR = Path(results_dir)
    return m


def _notification_body(applescript):
    """The notification text inside `display notification "..." with title ...`.

    The captured argv is the whole AppleScript; asserting on it measures a
    constant ~44 chars of wrapper as if it were the body macOS truncates.
    """
    m = re.search(r'display notification "(.*)" with title', applescript, re.S)
    return m.group(1) if m else applescript


class _Capture:
    """Records the AppleScript body notify_macos hands to osascript."""

    def __init__(self, sink):
        self.sink = sink

    def run(self, argv, **kw):
        self.sink.append(argv[-1])
        return type("R", (), {"returncode": 0})()


class TestUndrainedDetection(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp(prefix="cpq-"))
        self.m = _load(self.d)

    def _write(self, name, age_s):
        p = self.d / name
        p.write_text("x")
        t = time.time() - age_s
        import os
        os.utime(p, (t, t))
        return p

    def test_a_fresh_file_is_not_undrained(self):
        self._write("proactive-pending-q-abc.txt", 5)
        self.assertEqual(self.m.undrained_proactive_files(), [])

    def test_b_old_file_is_undrained(self):
        self._write("proactive-pending-q-abc.txt", self.m.UNDRAINED_AGE_S + 60)
        self.assertEqual(self.m.undrained_proactive_files(), ["proactive-pending-q-abc.txt"])

    def test_c_only_proactive_files_count(self):
        self._write("task-123.txt", self.m.UNDRAINED_AGE_S + 60)
        self.assertEqual(self.m.undrained_proactive_files(), [],
                         "a stale task result is a different thing entirely")

    def test_d_glob_raising_oserror_is_swallowed(self):
        """The outer handler. A missing dir does NOT raise from glob() — it
        yields nothing — so the earlier version of this test passed without ever
        reaching the except it was named for."""
        class Boom:
            def glob(self, _pat):
                raise OSError("boom")
        self.m.RESULTS_DIR = Boom()
        self.assertEqual(self.m.undrained_proactive_files(), [])

    def test_d2_stat_raising_oserror_skips_that_file(self):
        """The inner handler: one unstattable entry must not lose the others."""
        good = self._write("proactive-good.txt", self.m.UNDRAINED_AGE_S + 60)

        class Bad:
            name = "proactive-bad.txt"
            def stat(self):
                raise OSError("nope")

        class Dir:
            def glob(self, _pat):
                return [Bad(), good]
        self.m.RESULTS_DIR = Dir()
        self.assertEqual(self.m.undrained_proactive_files(), ["proactive-good.txt"])

    def test_e_notify_macos_reports_failure(self):
        import subprocess
        real = subprocess.run
        try:
            subprocess.run = lambda *a, **k: type("R", (), {"returncode": 1})()
            self.assertFalse(self.m.notify_macos(1, ["t"]), "a failed osascript must not read as delivered")
            subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0})()
            self.assertTrue(self.m.notify_macos(1, ["t"]))
            subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
            self.assertFalse(
                self.m.notify_macos(1, ["t"]),
                "a host without osascript must degrade to a failed optional path",
            )
        finally:
            subprocess.run = real



class TestNotifySummary(unittest.TestCase):
    """The summary line is the claim. It must not assert delivery that failed."""

    def setUp(self):
        import tempfile
        self.m = _load(tempfile.mkdtemp(prefix="cpq-sum-"))

    def test_a_all_paths_healthy(self):
        s, w = self.m.notify_summary(3, True, True, [])
        self.assertIn("macos=ok", s)
        self.assertIn("voice=ok", s)
        self.assertIn("proactive-file=written", s)
        self.assertIsNone(w, "no warning when nothing is undrained")

    def test_b_macos_failure_is_not_reported_as_ok(self):
        s, _ = self.m.notify_summary(3, False, True, [])
        self.assertIn("macos=FAILED", s)
        self.assertNotIn("macos=ok", s)

    def test_c_voice_offline_says_skipped_not_ok(self):
        s, _ = self.m.notify_summary(3, True, False, [])
        self.assertIn("voice=skipped(not connected)", s)

    def test_d_undrained_produces_an_explicit_warning(self):
        s, w = self.m.notify_summary(16, True, False, ["proactive-pending-q-old.txt"])
        self.assertIn("UNDRAINED", s)
        self.assertIsNotNone(w)
        self.assertIn("NOT reaching the owner", w)
        self.assertIn("proactive-pending-q-old.txt", w)

    def test_e_count_is_carried(self):
        s, _ = self.m.notify_summary(16, True, True, [])
        self.assertIn("16 pending questions", s)


class TestDeliver(unittest.TestCase):
    """deliver() must report per-path truth, not a blanket success."""

    def setUp(self):
        import tempfile
        self.m = _load(tempfile.mkdtemp(prefix="cpq-del-"))
        self.m.notify_discord_dm = lambda q: None
        self.m.notify_voice = lambda q: None

    def test_a_voice_connected_reports_ok(self):
        self.m.notify_macos = lambda c, t: True
        self.m.voice_client_connected = lambda: True
        s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("voice=ok", s)

    def test_b_voice_offline_reports_skipped(self):
        self.m.notify_macos = lambda c, t: True
        self.m.voice_client_connected = lambda: False
        s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("voice=skipped", s)

    def test_c_macos_failure_surfaces(self):
        self.m.notify_macos = lambda c, t: False
        self.m.voice_client_connected = lambda: False
        s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("macos=FAILED", s)

    def test_d_undrained_backlog_produces_warning(self):
        self.m.notify_macos = lambda c, t: True
        self.m.voice_client_connected = lambda: False
        self.m.undrained_proactive_files = lambda: ["proactive-old.txt"]
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("UNDRAINED", s)
        self.assertIn("NOT reaching the owner", err.getvalue(),
                      "the warning must reach stderr, not just be returned")


class TestReviewFindings(unittest.TestCase):
    """Both cases john-the-dev flagged: the suite passed without exercising either."""

    def setUp(self):
        import tempfile
        self.d = __import__("pathlib").Path(tempfile.mkdtemp(prefix="cpq-rev-"))
        self.m = _load(self.d)

    def _age(self, name, secs):
        import os
        p = self.d / name
        p.write_text("x")
        t = __import__("time").time() - secs
        os.utime(p, (t, t))
        return p

    # --- finding 1: cooldown must not be stamped when delivery raises ---
    def test_a_cooldown_not_stamped_when_delivery_raises(self):
        import tempfile
        import pathlib
        stamp = pathlib.Path(tempfile.mkdtemp()) / "last-notify"
        self.m.LAST_NOTIFY_FILE = stamp
        self.m.deliver = lambda *a, **k: (_ for _ in ()).throw(OSError("delivery blew up"))
        self.m.get_waiting_questions = lambda: [{"title": "q"}]
        self.m.should_notify = lambda *a, **k: True
        with self.assertRaises(OSError):
            self.m.main()
        self.assertFalse(stamp.exists(),
                         "a failed delivery must NOT put the next hour on cooldown")

    def test_a2_cooldown_IS_stamped_when_delivery_succeeds(self):
        """The positive half. Testing only the failure case let the stamp be
        deleted outright without any test failing — which would notify on every
        run forever. A guard needs both directions or it pins nothing."""
        import tempfile
        import pathlib
        stamp = pathlib.Path(tempfile.mkdtemp()) / "last-notify"
        self.m.LAST_NOTIFY_FILE = stamp
        self.m.deliver = lambda *a, **k: "Notified: 1 pending questions [ok]"
        self.m.get_waiting_questions = lambda: [{"title": "q"}]
        self.m.should_notify = lambda *a, **k: True
        self.m.main()
        self.assertTrue(stamp.exists(), "a successful delivery MUST set the cooldown")
        # The marker carries "<epoch> <content-key>" as of 2026-08-01: the cooldown
        # gates on the SET rather than only the clock, so the key must persist next
        # to the timestamp. Assert BOTH — if a later change drops the key the file
        # still parses as an int, and hourly re-notification of an unchanged queue
        # would come back silently.
        ts, _, key = stamp.read_text().partition(" ")
        self.assertGreater(int(ts), 0)
        self.assertTrue(key.strip(),
                        "the content key must be stamped alongside the timestamp")

    # --- finding 2: only THIS notifier's files are evidence ---
    def test_b_unrelated_stale_proactive_file_is_ignored(self):
        self._age("proactive-schedule-alert-cron.txt", self.m.UNDRAINED_AGE_S + 60)
        self._age("proactive-morning-brief.txt", self.m.UNDRAINED_AGE_S + 60)
        self.assertEqual(self.m.undrained_proactive_files(), [],
                         "another producer's stale file must not diagnose OUR path as dead")

    def test_c_our_own_stale_file_is_still_detected(self):
        self._age(f"{self.m.PROACTIVE_PREFIX}abc.txt", self.m.UNDRAINED_AGE_S + 60)
        self.assertEqual(self.m.undrained_proactive_files(),
                         [f"{self.m.PROACTIVE_PREFIX}abc.txt"])

    # --- finding 3: the body is bounded by TITLE COUNT and title LENGTH ---
    def test_f_notify_body_sends_slugs_not_whole_titles(self):
        """A title carries its ask; three of them overran the macOS body."""
        sent = []
        self.m.subprocess = _Capture(sent)
        # A real no-comma title from another host leads: the shape this notifier
        # sees is not guaranteed to be `slug, date`, so the cap must be the bound.
        titles = ["[2026-08-17 14:4x ET] relay/ never carried by vault — 30 handoff "
                  "notes exist only on this machine " + ("y" * 60)]
        titles += [f"slug-{i}, 2026-08-18 — " + ("x" * 90) for i in range(25)]
        self.m.notify_macos(26, titles)
        body = sent[0]
        self.assertLess(len(body), 160, f"body must stay short, got {len(body)}: {body}")
        # The comma-less title is bounded by the cap, not by a delimiter it lacks.
        self.assertIn("[2026-08-17 14:4x ET] relay/ never carri", body, "comma-less title still identifies")
        self.assertNotIn("yyyyyyyyyy", body, "a title without a comma must still be capped")
        for i in range(2):
            self.assertIn(f"slug-{i}", body, "the remaining names identify the queue")
        self.assertNotIn("xxxxxxxxxx", body, "no title prose may ride in the body")
        self.assertIn("(+23 more)", body, "the remainder must be counted, not dropped silently")

    def test_f2_cap_bounds_every_measured_title_vocabulary(self):
        """Three comma-less shapes measured on a second host (44 of 61 titles
        there carry no comma). The cap must bound each, delimiter or not."""
        for lead in (
            "[2026-08-07 03:4x UTC] approve #2701/#2702 — blocked on " + ("z" * 50),
            "2026-08-17 07:1x — `sutando-app` stale is real but NOT a fault " + ("z" * 50),
            "[2026-07-30 14:10] ngrok (phone tunnel) is DOWN and conflicts " + ("z" * 50),
        ):
            with self.subTest(lead=lead[:26]):
                sent = []
                self.m.subprocess = _Capture(sent)
                titles = [lead] + [f"slug-{i}, 2026-08-18 — " + ("x" * 90)
                                   for i in range(25)]
                self.m.notify_macos(26, titles)
                body = sent[0]
                self.assertLess(len(body), 160, f"body must stay short, got {len(body)}: {body}")
                self.assertNotIn("zzzzzzzzzz", body,
                                 "no comma exists in this shape; only the cap can bound it")
                self.assertIn("(+23 more)", body, "the remainder must still be counted")

    def test_f3_body_stays_bounded_as_the_count_widens(self):
        """Per-name caps leave the total arithmetic: the count and the `(+N more)`
        both widen with the queue. The assembled body is what must be bounded."""
        for count in (26, 126, 1226, 12226):
            with self.subTest(count=count):
                sent = []
                self.m.subprocess = _Capture(sent)
                titles = [("q" * 60) + f"-{i}" for i in range(count)]
                self.m.notify_macos(count, titles)
                # The NOTIFICATION body, not the AppleScript around it: macOS
                # truncates the former, and the wrapper is a constant ~44 chars.
                body = _notification_body(sent[0])
                self.assertLess(len(body), self.m.BODY_MAX,
                                f"count={count} must not overrun the bound, got {len(body)}: {body}")
                self.assertIn(f"(+{count - 3} more)", body,
                              "the remainder must survive the cap — it is the honest part")

    def test_f4_worst_case_names_stay_bounded(self):
        """Three maximal 40-char names — the shape the other fixtures never make."""
        sent = []
        self.m.subprocess = _Capture(sent)
        self.m.notify_macos(26, [("z" * 90) + f"-{i}" for i in range(26)])
        body = _notification_body(sent[0])
        self.assertLess(len(body), self.m.BODY_MAX,
                        f"three maximal names must still fit, got {len(body)}: {body}")

    def test_f6_blank_names_are_dropped_not_joined_as_bare_commas(self):
        """An empty or whitespace-only title contributes no name, so the join
        cannot emit `, ,` with nothing between the separators."""
        sent = []
        self.m.subprocess = _Capture(sent)
        self.m.notify_macos(5, ["", "   ", "real-slug, 2026-08-19 — the ask"])
        body = _notification_body(sent[0])
        self.assertNotRegex(body, r",\s*,", f"blank names must not render as bare commas: {body}")
        self.assertNotRegex(body, r":\s*,", f"body must not open on a separator: {body}")
        self.assertIn("real-slug", body, "the surviving name still identifies the queue")
        # Dropping two names moves them into the remainder rather than losing them.
        self.assertIn("(+4 more)", body, "dropped blanks must be counted, not vanish")

    def test_f7_all_blank_names_leave_no_double_space(self):
        """Every candidate blank: the join is empty and `head` ends in a space, so
        without stripping it the body renders `N pending questions:  (+N more)`."""
        sent = []
        self.m.subprocess = _Capture(sent)
        self.m.notify_macos(9, ["", "   ", ","])
        body = _notification_body(sent[0])
        self.assertNotIn("  ", body, f"empty join must not leave a double space: {body}")
        self.assertIn("(+9 more)", body, "the overflow must still account for every question")

    def test_f5_no_room_for_names_drops_them_rather_than_slicing_backwards(self):
        """A budget too small for the prefix must yield no names, not a negative
        slice: `joined[:room - 1]` at room==0 is `[:-1]`, which silently eats a char."""
        sent = []
        self.m.subprocess = _Capture(sent)
        original = self.m.BODY_MAX
        try:
            self.m.BODY_MAX = 20            # smaller than the prefix + overflow alone
            self.m.notify_macos(26, [("q" * 60) + f"-{i}" for i in range(26)])
        finally:
            self.m.BODY_MAX = original
        body = _notification_body(sent[0])
        self.assertNotIn("qqq", body, "no name may survive a budget that cannot hold one")
        self.assertIn("(+23 more)", body, "the count and overflow are never dropped")

    def test_d_written_name_matches_the_scanned_prefix(self):
        """Writer and detector must agree, or the check silently never fires."""
        src = (REPO / "src" / "check-pending-questions.py").read_text()
        self.assertIn('RESULTS_DIR / f"{PROACTIVE_PREFIX}', src)
        self.assertIn('RESULTS_DIR.glob(f"{PROACTIVE_PREFIX}', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
