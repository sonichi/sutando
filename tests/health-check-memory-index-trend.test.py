#!/usr/bin/env python3
"""`_index_growth_note` — the trend appended to the memory-index warning.

The probe it feeds warned "approaching the session read limit" for hours on
2026-08-04 and was read as scenery, correctly: a level that never moves carries
no information about whether the cut is a week away or an hour. These cases pin
the two things the level cannot say, and the two ways the note must stay quiet.

The over-cap case is the one that matters most. The first draft computed
`LOAD_BYTES - peak`, which goes NEGATIVE once peak exceeds the cap and printed
"came within -156 B of the cut" — so the one history that proves real loss
rendered as the calmest wording in the message. `test_over_cap_*` fails on that
draft.
"""
from __future__ import annotations
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "health-check.py"


def _hc():
    spec = importlib.util.spec_from_file_location("hc_trend_test", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# gc/fsmonitor writing into .git races TemporaryDirectory teardown (ENOTEMPTY),
# so the fixture git ignores ambient config and background maintenance.
_GIT_PIN = ["-c", "gc.auto=0", "-c", "maintenance.auto=false",
            "-c", "core.fsmonitor=false"]
_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str, env: "dict | None" = None) -> None:
    subprocess.run(["git", *_GIT_PIN, "-C", str(cwd), *args], check=True,
                   capture_output=True,
                   env={**os.environ, **_GIT_ENV, **(env or {})})


def _repo_with_sizes(tmp: Path, sizes: "list[int]", ago_h: float = 0.0) -> Path:
    """A git repo whose MEMORY.md is committed once per entry in `sizes`.

    Anchored so the LAST commit lands on `now`, one hour apart going back. The
    spans every other case asserts on are measured between commits and are
    unchanged by the anchor; what it fixes is that a frozen calendar date makes
    every fixture history arbitrarily stale in wall-clock terms, which is the
    one property `IdleHistoryDoesNotImplyALiveDeadline` needs to control.
    """
    repo = tmp / "vault"
    repo.mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    idx = repo / "MEMORY.md"
    base = int(time.time() - 3600 * ago_h) - 3600 * (len(sizes) - 1)
    for i, n in enumerate(sizes):
        idx.write_text("x" * n)
        _git(repo, "add", "MEMORY.md")
        # distinct, increasing author dates so the window spans real hours
        env_date = f"{base + 3600 * i} +0000"
        _git(repo, "commit", "-q", "-m", f"c{i}", "--date", env_date,
             env={"GIT_COMMITTER_DATE": env_date})
    return idx


class OverCapIsNamedAsLoss(unittest.TestCase):
    def test_over_cap_says_already_exceeded_and_never_a_negative(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [cap - 300, cap + 156, cap - 500])
            note = m._index_growth_note(idx, cap - 500)
        self.assertIn("ALREADY EXCEEDED", note)
        self.assertIn("156", note)
        # the sign error this test exists for
        self.assertNotIn("-156", note)
        self.assertNotIn("came within -", note)

    def test_near_but_under_cap_says_came_within(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [cap - 2000, cap - 12, cap - 900])
            note = m._index_growth_note(idx, cap - 900)
        self.assertIn("came within", note)
        self.assertNotIn("ALREADY EXCEEDED", note)
        self.assertNotIn("-", note.split("came within")[1][:12])


class FailsOpen(unittest.TestCase):
    """Fails open — but SAYS SO (#2958). A trend is a nicety and suppressing the
    level would be a regression; returning "" hid WHY there was no trend."""

    def test_not_a_git_repo_says_unavailable(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "MEMORY.md"
            idx.write_text("x" * 100)
            self.assertEqual(m._index_growth_note(idx, 100), m._TREND_UNAVAILABLE)

    def test_single_commit_is_not_a_trend(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [1000])
            self.assertEqual(m._index_growth_note(idx, 1000), m._TREND_UNAVAILABLE)

    def test_missing_file_says_unavailable(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                m._index_growth_note(Path(td) / "nope" / "MEMORY.md", 100), m._TREND_UNAVAILABLE)


class TheHelperCanActuallyFire(unittest.TestCase):
    """Control. Every assertion above is about STRING CONTENT, so a helper that
    returned "" unconditionally would satisfy the fail-open cases and give the
    suite a green half. This is the case that must produce a non-empty note, so
    the negatives above mean something."""

    def test_a_real_history_produces_a_non_empty_note(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [cap - 3000, cap + 10, cap - 800])
            note = m._index_growth_note(idx, cap - 800)
        self.assertTrue(note.strip(), "the helper produced nothing on a real history")


class RateIsMeasuredFromTheLastCompaction(unittest.TestCase):
    """Neither a compaction nor a 1-byte dip may lower the reported rate.
    Rationale and the reviewer's jitter control are in the PR body."""

    def test_growth_after_a_drop_is_not_cancelled_by_the_drop(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        # Rises, is compacted hard, then climbs steadily again — the real shape.
        sizes = [cap - 1000, cap - 900, cap - 2400, cap - 2000, cap - 1600, cap - 1200]
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), sizes)
            note = m._index_growth_note(idx, sizes[-1])
        # From the drop: +1200 B. From the oldest point: only +(-200) B, which the
        # `grew > 0` guard would suppress entirely.
        self.assertIn("+1,200 B", note)
        self.assertIn("remaining headroom", note)

    def test_a_one_byte_dip_does_not_reset_the_baseline(self):
        """The reviewer's jitter control: a 1-B decrease near the end must not
        become the baseline and hide a 900-B climb behind it."""
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        sizes = [cap - 1000, cap - 100, cap - 101, cap - 100]
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), sizes)
            note = m._index_growth_note(idx, sizes[-1])
        # Anchoring on the last decrease reports +1 B; the real climb is +900 B.
        self.assertIn("+900 B", note)
        self.assertNotIn("+1 B", note)

    def test_a_net_shrinking_history_still_reports_the_regrowth(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        # Net change oldest->newest is NEGATIVE, so the old code printed no rate at
        # all. The post-compaction climb is the number a reader needs.
        sizes = [cap - 500, cap - 3000, cap - 2500, cap - 2000]
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), sizes)
            note = m._index_growth_note(idx, sizes[-1])
        self.assertIn("+1,000 B", note)


class DefensiveBranches(unittest.TestCase):
    """The skip/except paths. They are REACHABLE — a rewritten history, a gc'd
    object, a git that is not installed — so covering them with a stub is honest
    where `pragma: no cover` would only be evading the gate."""

    class _Proc:
        def __init__(self, out="", rc=0):
            self.stdout = out
            self.returncode = rc

    def test_a_stopped_climb_is_named_so_the_deadline_is_not_read_as_live(self):
        """The max window is the WORST one, by design — a compaction must not be
        able to hide a climb. The cost is that `gain <= 0` discards every flat or
        shrinking window, so once growth STOPS the only surviving evidence is the
        old climb, and the note keeps quoting a deadline from a regime that ended.

        Measured 2026-08-17 on the live index: writes were frozen at 09:5xZ and it
        then FELL 273 B, while this note still read "+643 B over the last 10.9h,
        ~15.3h of remaining headroom" off a window starting 05:39Z. Acting on that
        urgency meant pre-empting a curation decision that was explicitly the
        owner's to make.
        """
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        # climbs 2000 B, then stops and gives some back — exactly the shape above
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(
                Path(td), [cap - 3000, cap - 2000, cap - 1000, cap - 1200, cap - 1400])
            note = m._index_growth_note(idx, cap - 1400)
        self.assertIn("of remaining headroom at that rate", note)   # the old figure survives
        self.assertIn("flat or shrinking", note)                    # and is qualified
        self.assertIn("stale", note)

    def test_a_still_climbing_history_carries_no_stale_caveat(self):
        """The control. Without it the caveat could be unconditional, which would
        mute the warning in exactly the case it is for."""
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [cap - 3000, cap - 2000, cap - 1000])
            note = m._index_growth_note(idx, cap - 1000)
        self.assertIn("of remaining headroom at that rate", note)
        self.assertNotIn("flat or shrinking", note)

    def test_unparsable_log_line_is_skipped(self):
        m = _hc()
        calls = {"n": 0}

        def fake_run(argv, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                # one malformed line (no epoch), one good line -> <2 points -> ""
                return self._Proc("deadbeef notanumber\n")
            return self._Proc("", 0)

        m.subprocess = type("S", (), {
            "run": staticmethod(fake_run),
            "SubprocessError": Exception,
        })
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "MEMORY.md"
            idx.write_text("x")
            self.assertEqual(m._index_growth_note(idx, 100), m._TREND_UNAVAILABLE)

    def test_a_truncated_batch_stream_stops_cleanly(self):
        """`--batch` output that ends mid-record (a killed git, a short pipe).
        The walker must stop rather than index past the buffer."""
        m = _hc()
        state = {"first": True}

        def fake_run(argv, **kw):
            if state["first"]:
                state["first"] = False
                return self._Proc("aaa 1000\nbbb 2000\n")
            return self._Proc(b"", 0)          # header never arrives

        m.subprocess = type("S", (), {"run": staticmethod(fake_run),
                                      "SubprocessError": Exception})
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "MEMORY.md"; idx.write_text("x")
            self.assertEqual(m._index_growth_note(idx, 100), m._TREND_UNAVAILABLE)

    def test_an_object_cat_file_reports_missing_is_skipped(self):
        """`cat-file --batch-check` does NOT fail on a bad spec — it prints
        "<spec> missing" and exits 0. So the skip has to be driven by parsing
        the line, not by a return code. An earlier revision of this test stubbed
        a non-zero exit instead; it still passed, because it hit the batch-level
        early return rather than this branch — a test can keep passing while
        silently ceasing to test the thing it names."""
        m = _hc()
        state = {"first": True}

        def fake_run(argv, **kw):
            if state["first"]:
                state["first"] = False
                return self._Proc("aaa 1000\nbbb 2000\n")
            return self._Proc(b"aaa:./MEMORY.md missing\nbbb:./MEMORY.md missing\n", 0)

        m.subprocess = type("S", (), {
            "run": staticmethod(fake_run),
            "SubprocessError": Exception,
        })
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "MEMORY.md"
            idx.write_text("x")
            # both objects skipped -> 0 points -> the unavailable marker
            self.assertEqual(m._index_growth_note(idx, 100), m._TREND_UNAVAILABLE)

    def test_a_failing_batch_says_unavailable(self):
        m = _hc()
        state = {"first": True}

        def fake_run(argv, **kw):
            if state["first"]:
                state["first"] = False
                return self._Proc("aaa 1000\nbbb 2000\n")
            return self._Proc("", 1)           # the batch itself fails

        m.subprocess = type("S", (), {
            "run": staticmethod(fake_run),
            "SubprocessError": Exception,
        })
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "MEMORY.md"
            idx.write_text("x")
            self.assertEqual(m._index_growth_note(idx, 100), m._TREND_UNAVAILABLE)

    def test_git_unavailable_is_swallowed(self):
        m = _hc()

        def boom(*a, **k):
            raise m.GitUnavailable("no git here")

        m.git_argv = boom
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "MEMORY.md"
            idx.write_text("x")
            self.assertEqual(m._index_growth_note(idx, 100), m._TREND_UNAVAILABLE)


class HistoricalBytesUseTheSAMEUnitsAsTheLimit(unittest.TestCase):
    """The blocker john-the-dev and qingyun-wu each reproduced independently.

    `effective_bytes` — the live level — comes from `_index_effective_text`,
    which strips frontmatter and whole-line HTML comments BEFORE the runtime
    measures its 25KB cap. An earlier revision of the helper measured historical
    revisions as RAW blob length, so a revision whose bulk sits inside
    `<!-- ... -->` read as far over the cap while being tiny to the runtime, and
    the note claimed "ALREADY EXCEEDED ... entries were dropped then".

    A false claim of proven loss, in a note whose whole purpose is that the
    reported number describes the artifact. This case fails on that revision.
    """

    def test_a_comment_only_over_cap_revision_is_not_reported_as_loss(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        # raw: way over cap. effective: seven bytes.
        fat = "<!--\n" + ("x" * (cap + 5000)) + "\n-->\n# live\n"
        lean = "# live\n"
        self.assertGreater(len(fat.encode()), cap)
        self.assertLess(len(m._index_effective_text(fat).encode()), 200)

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "vault"; repo.mkdir()
            _git(repo.parent, "init", "-q", str(repo))
            _git(repo, "config", "user.email", "t@e"); _git(repo, "config", "user.name", "t")
            idx = repo / "MEMORY.md"
            import os
            for i, text in enumerate((fat, lean)):
                idx.write_text(text)
                _git(repo, "add", "MEMORY.md")
                d = f"2026-08-03T{i:02d}:00:00"
                _git(repo, "commit", "-q", "-m", f"c{i}", "--date", d,
                     env={"GIT_COMMITTER_DATE": d})
            note = m._index_growth_note(idx, len(m._index_effective_text(lean).encode()))

        self.assertNotIn("ALREADY EXCEEDED", note,
                         "a comment-only revision is tiny to the runtime; claiming entries "
                         "were dropped there is a false claim of proven loss")
        self.assertNotIn("came within", note)


class StaleDeadlineGuardCanActuallyFire(unittest.TestCase):
    """The guard that says "this deadline is stale" must sample the FULL history.

    It used to take the newest point >=0.5h back — the SHORTEST qualifying
    window. But a short window with a gain is exactly what maximises gain/span
    and wins `best_rate`, so the control was reading the same burst it was
    meant to detect, and stayed silent in the one case it was written for.
    Measured live 2026-08-26: the probe quoted "+146 B over 2.0h -> 17.7h of
    remaining headroom" while the file's 224h history was net -102 B.
    """

    def test_recent_burst_on_a_flat_history_is_reported_as_stale(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            # net -110 B across the window, with a +146 B burst in the last hour
            idx = _repo_with_sizes(Path(td), [23800, 23700, 23600, 23550, 23544, 23690])
            note = m._index_growth_note(idx, 23690)
        # the max window still quotes its deadline...
        self.assertIn("of remaining headroom at that rate", note)
        # ...and the control now contradicts it, which is the whole point
        self.assertIn("deadline is stale", note)
        self.assertIn("-110", note)

    def test_sustained_growth_is_not_called_stale(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [23000, 23150, 23300, 23450, 23600, 23690])
            note = m._index_growth_note(idx, 23690)
        self.assertIn("of remaining headroom at that rate", note)
        self.assertNotIn("deadline is stale", note)




class SpentDeadlineInsideTheWindow(unittest.TestCase):
    """The quoted deadline can be outlived while the window is still open.

    Keying the guard on `hours` requires idling longer than the WHOLE history,
    which is the rare case. A near-cap file has a small `left`, so `left / rate`
    is short and elapses first — and no `now=` is injected here, so this case
    runs unmodified against either revision.
    """

    def test_caveat_fires_once_the_quoted_deadline_has_elapsed(self):
        m = _hc()
        cap = m.MEMORY_INDEX_LOAD_BYTES
        with tempfile.TemporaryDirectory() as td:
            # 23 commits an hour apart -> 22h window; newest committed 4h ago.
            # grew 440 B over 22h = 20 B/h; left 60 B -> deadline 3.0h < idle 4h.
            sizes = [cap - 500 + round(440 * i / 22) for i in range(23)]
            idx = _repo_with_sizes(Path(td), sizes, ago_h=4.0)
            note = m._index_growth_note(idx, cap - 60)
            self.assertIn("of remaining headroom at that rate", note)
            self.assertIn("closed before the deadline it implies", note,
                          "deadline ~3.0h was outlived by a 4h idle gap inside a "
                          "22h window; the note still quotes it as live")


class IdleHistoryDoesNotImplyALiveDeadline(unittest.TestCase):
    """A rate whose whole window closed yesterday is not a deadline for today.

    Every span in the note ends at the newest COMMIT, while `gain` ends at the
    LIVE size. So when the file stops being written the denominator freezes and
    the numerator does not, and a climb that finished long ago keeps being
    quoted as "the last N.Nh" with a live headroom figure hanging off it.

    #3429's control cannot catch this shape: it fires only when some window is
    flat or shrinking, and a monotonically growing history that simply STOPPED
    is positive in every window. Measured live 2026-08-29 on upstream main, the
    probe said "+5,798 B over the last 1.0h, which is ~0.3h of remaining
    headroom" against a file whose newest revision was 20.6h old and whose
    bytes had not moved since — a deadline outlived 68x over while still being
    printed as imminent, on the one message whose whole job is to say when to
    compact.
    """

    def test_an_idle_history_says_the_window_closed(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [17462, 23039, 23260])
            note = m._index_growth_note(idx, 23260,
                                        now=time.time() + 3600 * 20.6)
        self.assertIn("of remaining headroom at that rate", note)
        self.assertIn("nothing has been written since", note)
        self.assertIn("20.6h", note)

    def test_a_history_still_being_written_keeps_its_deadline(self):
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [17462, 23039, 23260])
            note = m._index_growth_note(idx, 23260)
        self.assertIn("of remaining headroom at that rate", note)
        self.assertNotIn("nothing has been written since", note)

    def test_uncommitted_growth_past_the_last_revision_is_not_called_idle(self):
        """The live file HAS grown since the newest commit, so the climb is
        current even though the history is old — the deadline stands."""
        m = _hc()
        with tempfile.TemporaryDirectory() as td:
            idx = _repo_with_sizes(Path(td), [17462, 23039, 23260])
            note = m._index_growth_note(idx, 24000,
                                        now=time.time() + 3600 * 20.6)
        self.assertNotIn("nothing has been written since", note)


if __name__ == "__main__":
    unittest.main()
