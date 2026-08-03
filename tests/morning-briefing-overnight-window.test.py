#!/usr/bin/env python3
"""Tests for morning-briefing.py get_overnight_discord() — the 8-hour window.

The shipped version promised "last 8 hours" in its docstring, computed
`cutoff = time.time() - 8 * 3600`, and then never applied it. The effective
window was `splitlines()[-200:]` on `logs/discord-bridge.log` — a LINE count,
not a time window, which silently shrinks as the bridge gets chattier.

Measured on the live log 2026-08-02: 9 DM lines at indices 596-6177, window
starting at 6,554, so the briefing reported ZERO overnight messages on a day
that had them. False-clean.

The log cannot be fixed in place: its `[msg] #DM` lines carry no timestamp, so
there is nothing for a cutoff to compare against. These tests pin the new
source — the bridge's own task files, which carry an ISO `timestamp:`.

test_finds_the_dm_the_line_window_hides deliberately writes BOTH a chatty log
(with a real DM line pushed past the 200-line window) AND the task file, so it
reproduces the actual production failure rather than a trivial "no log" case.
"""
import importlib.util
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"


def load_module():
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("_mb_overnight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_task(directory: Path, ident: str, ts: float, *, source="discord",
               channel="DM", tier="owner", body="hello") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"task-{ident}.txt"
    lines = [f"id: task-{ident}", f"timestamp: {iso(ts)}", f"source: {source}"]
    if channel is not None:
        lines.append(f"channel_name: {channel}")
    lines += [f"access_tier: {tier}", "priority: normal", f"task: {body}"]
    path.write_text("\n".join(lines) + "\n")
    return path


class OvernightWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.mod = load_module()
        self.mod.WORKSPACE = self.ws
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def _chatty_log_hiding_a_dm(self):
        """A DM line that IS in the log but sits outside the last 200 lines."""
        logs = self.ws / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        dm = ("  [msg] #DM @sonichi: buried by chatter "
              "(mentions: [], is_dm: True, embeds: 0)")
        filler = ["  [gateway] heartbeat ok"] * 400
        (logs / "discord-bridge.log").write_text("\n".join([dm] + filler) + "\n")

    def test_recent_owner_dm_is_found(self):
        write_task(self.ws / "tasks", "1", self.now - 3600, body="ship it")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), ["ship it"])

    def test_dm_older_than_eight_hours_is_excluded(self):
        """The whole point: a TIME window, not a line window."""
        write_task(self.ws / "tasks", "1", self.now - 9 * 3600, body="yesterday")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_finds_the_dm_the_line_window_hides(self):
        """Reproduces production: the DM is in the log but past 200 lines."""
        self._chatty_log_hiding_a_dm()
        write_task(self.ws / "tasks" / "archive", "1", self.now - 7200,
                   body="buried by chatter")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now),
                         ["buried by chatter"])

    def test_month_partitioned_archive_is_scanned(self):
        """PR #591 partitions the archive as tasks/archive/YYYY-MM/<id>.txt.

        The flat form is LEGACY. Scanning only it reproduces the exact
        false-clean this function exists to remove: on the live workspace at
        review time, 280 flat vs 178 month-partitioned files, and in an 8-hour
        window 1 owner DM found vs 2 missed.
        """
        write_task(self.ws / "tasks" / "archive" / "2026-08", "1",
                   self.now - 3600, body="month-partitioned")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now),
                         ["month-partitioned"])

    def test_flat_and_month_archives_are_merged_in_time_order(self):
        """Legacy flat entries must not be dropped when both shapes exist."""
        write_task(self.ws / "tasks" / "archive", "1", self.now - 300,
                   body="legacy-flat")
        write_task(self.ws / "tasks" / "archive" / "2026-08", "2",
                   self.now - 200, body="month-newer")
        write_task(self.ws / "tasks", "3", self.now - 100, body="live")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now),
                         ["legacy-flat", "month-newer", "live"])

    def test_archive_scan_is_one_level_deep_only(self):
        """Bounded like discord-bridge's own `archive.glob("*/<id>.txt")`.

        rglob would walk unbounded depth; a stray nested tree must not be
        pulled in.
        """
        write_task(self.ws / "tasks" / "archive" / "2026-08" / "extra", "1",
                   self.now - 60, body="too deep")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_archive_and_live_dirs_are_both_scanned(self):
        write_task(self.ws / "tasks", "2", self.now - 60, body="newest")
        write_task(self.ws / "tasks" / "archive", "1", self.now - 120, body="older")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now),
                         ["older", "newest"])

    def test_channel_messages_are_not_dms(self):
        write_task(self.ws / "tasks", "1", self.now - 60, channel="bot2bot",
                   body="channel chatter")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_non_owner_tier_is_excluded(self):
        """Replaces the old sender-name exclusion — peer bots are `team`."""
        write_task(self.ws / "tasks", "1", self.now - 60, tier="team",
                   body="peer bot")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_other_sources_are_excluded(self):
        write_task(self.ws / "tasks", "1", self.now - 60, source="telegram",
                   body="telegram")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_caps_at_five_keeping_the_newest(self):
        for i in range(8):
            write_task(self.ws / "tasks", str(i), self.now - (8 - i) * 60,
                       body=f"m{i}")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now),
                         ["m3", "m4", "m5", "m6", "m7"])

    def test_missing_dirs_return_empty(self):
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_unparseable_timestamp_is_skipped_not_fatal(self):
        good = write_task(self.ws / "tasks", "1", self.now - 60, body="good")
        bad = self.ws / "tasks" / "task-bad.txt"
        bad.write_text("id: task-bad\ntimestamp: not-a-date\nsource: discord\n"
                       "channel_name: DM\naccess_tier: owner\ntask: broken\n")
        self.assertTrue(good.exists() and bad.exists())
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), ["good"])

    def test_unreadable_entry_is_skipped_not_fatal(self):
        """A path matching task-*.txt that cannot be read must not abort the scan.

        Uses a DIRECTORY named like a task file — read_text() raises
        IsADirectoryError (an OSError) deterministically, without relying on
        chmod, which does nothing when the test runs as the file's owner.
        """
        write_task(self.ws / "tasks", "1", self.now - 60, body="good")
        (self.ws / "tasks" / "task-decoy.txt").mkdir()
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), ["good"])

    def test_dm_with_no_task_line_yields_empty_body_not_a_crash(self):
        """Header-only task file: still counted, body empty."""
        path = self.ws / "tasks" / "task-1.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"id: task-1\ntimestamp: {iso(self.now - 60)}\nsource: discord\n"
            "channel_name: DM\naccess_tier: owner\n")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [""])

    def test_body_is_truncated_for_speech(self):
        write_task(self.ws / "tasks", "1", self.now - 60, body="x" * 200)
        (only,) = self.mod.get_overnight_discord(now=self.now)
        self.assertEqual(len(only), 80)


    # ---- upper edge of the window (john-the-dev, PR #2508 @ e42cc19d) ----
    # Only the LOWER bound was enforced, so one future-dated stamp counted as
    # "overnight" in every briefing until the wall clock caught up. Testing the
    # whole boundary AXIS, not just the reported case: a fixture set containing
    # only the bug a reviewer named cannot catch the next one.

    def _month_archive(self):
        """The ACTIVE archive layout — where a processed DM really lands."""
        return self.ws / "tasks" / "archive" / "2026-08"

    def test_future_dated_task_is_excluded(self):
        write_task(self._month_archive(), "future", self.now + 365 * 86400,
                   body="future-dated")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_future_dated_task_in_flat_archive_is_excluded(self):
        write_task(self.ws / "tasks" / "archive", "flatfuture", self.now + 3600,
                   body="flat future")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_future_dated_task_in_live_tasks_dir_is_excluded(self):
        write_task(self.ws / "tasks", "livefuture", self.now + 60, body="live future")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), [])

    def test_task_written_exactly_now_is_included(self):
        """Upper bound is INCLUSIVE — a DM arriving this instant still counts."""
        write_task(self._month_archive(), "rightnow", self.now, body="right now")
        self.assertEqual(self.mod.get_overnight_discord(now=self.now), ["right now"])

    def test_task_exactly_at_cutoff_is_included(self):
        """Lower bound is INCLUSIVE — unchanged by the upper-bound fix.

        Pinned to a whole second on purpose. `write_task` serialises with `%S`,
        so a fractional `now` truncates the stamp BELOW a fractional cutoff and
        the case silently becomes "one second early" instead of "exactly at the
        edge" — the fixture format cannot express the boundary being asserted.
        """
        now = float(int(self.now))
        write_task(self._month_archive(), "atcutoff", now - 8 * 3600,
                   body="at cutoff")
        self.assertEqual(self.mod.get_overnight_discord(now=now), ["at cutoff"])

    def test_task_one_second_before_cutoff_is_excluded(self):
        """The other side of that edge, so "inclusive" is a measured claim."""
        now = float(int(self.now))
        write_task(self._month_archive(), "tooold", now - 8 * 3600 - 1,
                   body="too old")
        self.assertEqual(self.mod.get_overnight_discord(now=now), [])

    def test_future_task_does_not_displace_real_ones(self):
        """The newest-five slice is taken AFTER sorting, so an unbounded future
        stamp sorts last and evicts a genuine overnight DM from the list."""
        for i in range(5):
            write_task(self._month_archive(), "real%d" % i, self.now - (i + 1) * 600,
                       body="real%d" % i)
        write_task(self._month_archive(), "future", self.now + 999999, body="future")
        got = self.mod.get_overnight_discord(now=self.now)
        self.assertNotIn("future", got)
        self.assertEqual(len(got), 5)


if __name__ == "__main__":
    unittest.main()
