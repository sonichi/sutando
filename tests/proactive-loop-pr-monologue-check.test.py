#!/usr/bin/env python3
"""Contract for the PR monologue guard: refuse to post into a thread that is only me."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "skills" / "proactive-loop" / "scripts" / "pr-monologue-check.py"
spec = importlib.util.spec_from_file_location("pr_monologue_check", MOD)
g = importlib.util.module_from_spec(spec)
sys.modules["pr_monologue_check"] = g
spec.loader.exec_module(g)

ME = "me"
REPO = "sonichi/sutando"


def c(ts, login):
    return {"created_at": ts, "user": {"login": login}}


def r(ts, login):
    return {"submitted_at": ts, "user": {"login": login}}


def T(n):
    return f"2026-09-0{n}T00:00:00Z"


class TestTrailingRun(unittest.TestCase):
    def test_all_mine_counts_every_one(self):
        ev = g.merge_events([c(T(1), ME), c(T(2), ME), c(T(3), ME)], [])
        self.assertEqual(g.trailing_run(ev, ME)[0], 3)

    def test_someone_else_last_gives_zero(self):
        ev = g.merge_events([c(T(1), ME), c(T(2), ME), c(T(3), "peer")], [])
        self.assertEqual(g.trailing_run(ev, ME)[0], 0)

    def test_only_the_trailing_run_counts(self):
        ev = g.merge_events([c(T(1), ME), c(T(2), "peer"), c(T(3), ME)], [])
        self.assertEqual(g.trailing_run(ev, ME)[0], 1)

    def test_empty_timeline(self):
        self.assertEqual(g.trailing_run([], ME), (0, 0.0))

    def test_span_measures_the_run_not_the_thread(self):
        ev = g.merge_events([c(T(1), "peer"), c(T(3), ME), c(T(5), ME)], [])
        run, span = g.trailing_run(ev, ME)
        self.assertEqual(run, 2)
        self.assertAlmostEqual(span, 2.0, places=3)


class TestSurfaces(unittest.TestCase):
    def test_a_review_is_engagement_and_breaks_the_run(self):
        # A thread answered only by a review must not read as silence.
        ev = g.merge_events([c(T(1), ME), c(T(2), ME)], [r(T(3), "peer")])
        self.assertEqual(g.trailing_run(ev, ME)[0], 0)

    def test_my_own_review_extends_the_run(self):
        ev = g.merge_events([c(T(1), ME), c(T(2), ME)], [r(T(3), ME)])
        self.assertEqual(g.trailing_run(ev, ME)[0], 3)

    def test_events_interleave_by_timestamp_across_surfaces(self):
        ev = g.merge_events([c(T(1), ME), c(T(3), ME)], [r(T(2), "peer")])
        self.assertEqual([e["login"] for e in ev], [ME, "peer", ME])


class TestBots(unittest.TestCase):
    def test_a_bot_comment_does_not_count_as_a_reply(self):
        # Measured live: github-actions[bot] reset a real run of 2 to 0 on #2406.
        ev = g.merge_events([c(T(1), ME), c(T(2), ME), c(T(3), "github-actions[bot]")], [])
        self.assertEqual(g.trailing_run(ev, ME)[0], 2)

    def test_count_bots_reproduces_the_false_safe(self):
        ev = g.merge_events(
            [c(T(1), ME), c(T(2), ME), c(T(3), "github-actions[bot]")], [], keep_bots=True)
        self.assertEqual(g.trailing_run(ev, ME)[0], 0)

    def test_is_bot_only_matches_the_suffix(self):
        self.assertTrue(g.is_bot("dependabot[bot]"))
        self.assertFalse(g.is_bot("randombet"))
        self.assertFalse(g.is_bot("open-mac-bot"))


class TestMain(unittest.TestCase):
    def _with_fetch(self, comments, reviews, argv):
        real = g.fetch
        g.fetch = lambda repo, number: (comments, reviews)
        try:
            return g.main(argv)
        finally:
            g.fetch = real

    def test_refuses_at_the_threshold(self):
        ev = [c(T(1), ME), c(T(2), ME), c(T(3), ME)]
        self.assertEqual(self._with_fetch(ev, [], ["1", "--me", ME, "--repo", REPO]), 1)

    def test_allows_below_the_threshold(self):
        ev = [c(T(1), ME), c(T(2), ME)]
        self.assertEqual(self._with_fetch(ev, [], ["1", "--me", ME, "--repo", REPO]), 0)

    def test_threshold_is_configurable(self):
        ev = [c(T(1), ME), c(T(2), ME)]
        self.assertEqual(self._with_fetch(ev, [], ["1", "--me", ME, "--threshold", "2", "--repo", REPO]), 1)

    def test_empty_thread_is_safe(self):
        self.assertEqual(self._with_fetch([], [], ["1", "--me", ME, "--repo", REPO]), 0)

    def test_every_verdict_names_the_repo_it_measured(self):
        """A bare `#1` cannot be told apart from the same number in another repo,
        so a cross-repo run reads an unrelated (usually empty) thread and can only
        ever say "safe" — a gate that cannot refuse."""
        import io
        from contextlib import redirect_stdout
        for argv, want in (
                (["https://github.com/url-form/repo/pull/1", "--me", ME], "url-form/repo"),
                (["1", "--me", ME, "--repo", "other/repo"], "other/repo"),
        ):
            # All three verdict branches, including REFUSE — the branch the tool
            # exists for, and the one a wrong repo silently makes unreachable.
            for events in ([], [c(T(1), ME)], [c(T(1), ME), c(T(2), ME), c(T(3), ME)]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self._with_fetch(events, [], argv)
                self.assertIn(f"{want}#1", buf.getvalue())

    def test_the_repo_reaches_fetch(self):
        seen = []
        real = g.fetch
        g.fetch = lambda repo, number: (seen.append(repo), ([], []))[1]
        try:
            g.main(["https://github.com/url-form/repo/pull/1", "--me", ME])
            g.main(["1", "--me", ME, "--repo", "other/repo"])
        finally:
            g.fetch = real
        self.assertEqual(seen, ["url-form/repo", "other/repo"])

    def test_a_fetch_failure_is_cannot_answer_not_a_green_light(self):
        real = g.fetch

        def boom(repo, number):
            raise RuntimeError("injected: gh api failed")

        g.fetch = boom
        try:
            self.assertEqual(g.main(["1", "--me", ME, "--repo", REPO]), 2)
        finally:
            g.fetch = real

    def test_a_nonsense_threshold_refuses_rather_than_guessing(self):
        self.assertEqual(self._with_fetch([], [], ["1", "--me", ME, "--threshold", "0", "--repo", REPO]), 2)


class TestFetchLayer(unittest.TestCase):
    """The gh layer the other tests inject around. Its failure path is the one that
    decides between REFUSE and a false 'safe', so it needs its own coverage."""

    def _fake_run(self, rc, out="", err=""):
        class R:
            returncode, stdout, stderr = rc, out, err

        calls = []

        def run(args, **kw):
            calls.append(args)
            return R()

        return run, calls

    def test_gh_json_parses_a_successful_response(self):
        run, calls = self._fake_run(0, '[{"x": 1}]')
        real = g.subprocess.run
        g.subprocess.run = run
        try:
            self.assertEqual(g._gh_json("repos/o/r/issues/1/comments"), [{"x": 1}])
        finally:
            g.subprocess.run = real
        self.assertEqual(calls[0][:2], ["gh", "api"])

    def test_gh_json_raises_naming_the_path_when_gh_fails(self):
        run, _ = self._fake_run(1, "", "HTTP 404: Not Found")
        real = g.subprocess.run
        g.subprocess.run = run
        try:
            with self.assertRaises(RuntimeError) as ctx:
                g._gh_json("repos/o/r/pulls/9/reviews")
            self.assertIn("pulls/9/reviews", str(ctx.exception))
        finally:
            g.subprocess.run = real

    def test_fetch_reads_both_surfaces(self):
        run, calls = self._fake_run(0, "[]")
        real = g.subprocess.run
        g.subprocess.run = run
        try:
            comments, reviews = g.fetch("o/r", 7)
        finally:
            g.subprocess.run = real
        self.assertEqual((comments, reviews), ([], []))
        # Search the whole argv, not a fixed index — a new flag must not silently
        # shift what this assertion is reading.
        paths = [a for c in calls for a in c]
        self.assertTrue(any("issues/7/comments" in p for p in paths), paths)
        self.assertTrue(any("pulls/7/reviews" in p for p in paths), paths)

    def test_a_gh_failure_reaches_main_as_cannot_answer(self):
        # End to end through the real fetch: a failing gh must exit 2, not 0.
        run, _ = self._fake_run(1, "", "boom")
        real = g.subprocess.run
        g.subprocess.run = run
        try:
            self.assertEqual(g.main(["1", "--me", ME, "--repo", REPO]), 2)
        finally:
            g.subprocess.run = real


class TestPagination(unittest.TestCase):
    """>100 records on either surface. GitHub returns the OLDEST page first, so an
    unpaginated read computes the trailing run from a stale end and fails OPEN."""

    def test_gh_json_requests_every_page(self):
        seen = []

        class R:
            returncode, stdout, stderr = 0, "[]", ""

        def run(args, **kw):
            seen.append(args)
            return R()

        real = g.subprocess.run
        g.subprocess.run = run
        try:
            g._gh_json("repos/o/r/issues/1/comments")
        finally:
            g.subprocess.run = real
        self.assertIn("--paginate", seen[0],
                      "unpaginated: only the OLDEST 100 records are read")

    def _long_thread(self, tail):
        """147 peer events, then `tail` — more than one page on either surface."""
        ev = [c(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z", "peer") for i in range(147)]
        return ev + tail

    def test_over_100_records_with_my_three_newest_refuses(self):
        tail = [c("2026-09-01T00:00:00Z", ME), c("2026-09-02T00:00:00Z", ME),
                c("2026-09-03T00:00:00Z", ME)]
        ev = self._long_thread(tail)
        self.assertGreater(len(ev), 100)
        run, _ = g.trailing_run(g.merge_events(ev, []), ME)
        self.assertEqual(run, 3)

    def test_over_100_records_with_a_peer_newest_is_safe(self):
        # The discriminating twin: same 150 records, one different last event.
        tail = [c("2026-09-01T00:00:00Z", ME), c("2026-09-02T00:00:00Z", ME),
                c("2026-09-03T00:00:00Z", "peer")]
        ev = self._long_thread(tail)
        self.assertGreater(len(ev), 100)
        run, _ = g.trailing_run(g.merge_events(ev, []), ME)
        self.assertEqual(run, 0)

    def test_the_stale_prefix_a_truncated_read_would_see_says_safe(self):
        # Fail-OPEN, not merely incomplete: the first 100 records end in peer
        # traffic, so a truncated read clears the post the full thread refuses.
        tail = [c("2026-09-01T00:00:00Z", ME), c("2026-09-02T00:00:00Z", ME),
                c("2026-09-03T00:00:00Z", ME)]
        ev = self._long_thread(tail)
        full, _ = g.trailing_run(g.merge_events(ev, []), ME)
        truncated, _ = g.trailing_run(g.merge_events(ev[:100], []), ME)
        self.assertEqual((full, truncated), (3, 0))


class RepoMustBeNamed(unittest.TestCase):
    """A bare number plus a defaulted repo is the shape that cannot refuse.

    Naming the assumption in the output was necessary but not sufficient: on
    2026-09-10 a run omitted --repo, printed a 404 that contained the wrong repo,
    and the post went out anyway. A reader who has the repo in front of him and
    posts regardless is not helped by being shown it again, so the default is gone.
    """

    def test_a_bare_number_without_repo_cannot_answer(self):
        self.assertEqual(g.main(["1", "--me", ME]), 2)

    def test_the_refusal_says_what_to_pass(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            g.main(["1", "--me", ME])
        err = buf.getvalue()
        self.assertIn("--repo", err)
        self.assertIn("URL", err)

    def test_a_full_url_supplies_the_repo(self):
        seen = []
        real = g.fetch
        g.fetch = lambda repo, number: (seen.append((repo, number)), ([], []))[1]
        try:
            g.main(["https://github.com/o/r/pull/42", "--me", ME])
        finally:
            g.fetch = real
        self.assertEqual(seen, [("o/r", 42)])

    def test_a_url_disagreeing_with_repo_refuses_rather_than_picking(self):
        self.assertEqual(
            g.main(["https://github.com/o/r/pull/1", "--me", ME, "--repo", "other/repo"]), 2)

    def test_a_url_agreeing_with_repo_is_fine(self):
        real = g.fetch
        g.fetch = lambda repo, number: ([], [])
        try:
            rc = g.main(["https://github.com/o/r/pull/1", "--me", ME, "--repo", "o/r"])
        finally:
            g.fetch = real
        self.assertEqual(rc, 0)

    def test_a_non_number_non_url_cannot_answer(self):
        self.assertEqual(g.main(["not-a-pr", "--me", ME, "--repo", REPO]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
