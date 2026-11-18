#!/usr/bin/env python3
"""skills/worker-pool/scripts/pool_pr_route.py: an event about one of our own PRs goes to the live
worker its newest Sutando-Task trailer was assigned to, and `handed` is printed only for a
delivered ask; everything else stays with the watcher (`local`). gh, liveness and pool_ask are
stubbed; attribution is the real store in a temp workspace."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pool_attribution  # noqa: E402
import pool_pr_route as route_mod  # noqa: E402

W = "02e4302f00844397bac09533fc398248"
ME = "212e8040d38d48b5aadab0db295dc33a"
EVENT = "PR_EVENT acme/widgets#7 review_submitted by=bob state=changes_requested author=alice head=0badc0de"
TRAILED = "fix: thing\n\nbody text\n\nSutando-Task: task-abc123\nCo-Authored-By: x"


class FakeGh:
    def __init__(self, messages=(TRAILED,), author="alice", head_repo="acme/widgets", rc=0, raises=None,
                 stdout=None):
        self.calls = []
        self.messages, self.author, self.head_repo = list(messages), author, head_repo
        self.rc, self.raises, self.stdout = rc, raises, stdout

    def __call__(self, args, timeout):
        self.calls.append(args)
        if self.raises:
            raise self.raises
        pr = {"author": {"login": self.author},
              "headRepository": {"nameWithOwner": self.head_repo} if self.head_repo else None,
              "commits": {"nodes": [{"commit": {"message": m}} for m in self.messages]}}
        out = self.stdout if self.stdout is not None else json.dumps({"data": {"repository": {"pullRequest": pr}}})
        return subprocess.CompletedProcess(args, self.rc, out, "")


def file_result(**route):
    return {"task_id": "task-x", "to": W, "route": {"delivered": [], "already": [], "skipped": [],
                                                    "fenced": [], **route}}


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ws = Path(td.name) / "ws"
        self.cache = Path(td.name) / "route"
        self.asks = []
        self.result = file_result(delivered=[W])

    def ask(self, ws, to, text):
        self.asks.append((ws, to, text))
        return self.result

    def route(self, gh=None, head="0badc0de9999", event=EVENT, authors=("alice",), **kw):
        kw.setdefault("me", ME)
        kw.setdefault("ask", self.ask)
        kw.setdefault("alive", lambda ws, wid: True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = route_mod.route(self.ws, event, "acme/widgets", 7, head, list(authors), self.cache,
                                  run=gh or FakeGh(), **kw)
        return out, err.getvalue()


class Trailer(unittest.TestCase):
    def test_only_the_trailer_block_counts(self):
        self.assertEqual(route_mod.trailer_task(TRAILED), "task-abc123")
        self.assertIsNone(route_mod.trailer_task("Sutando-Task: task-abc123"), "a subject is not a trailer")
        self.assertIsNone(route_mod.trailer_task("s\n\nSutando-Task: task-abc123\n\nlast paragraph"))
        self.assertIsNone(route_mod.trailer_task("s\n\nSutando-Task: not a task"))
        self.assertIsNone(route_mod.trailer_task(""))

    def test_trailer_paragraphs_split_by_a_blank_line_still_count(self):
        msg = "fix: thing\n\nbody text\n\nSutando-Task: task-abc123\n\nCo-Authored-By: x <y@z>"
        self.assertEqual(route_mod.trailer_task(msg), "task-abc123")
        mixed = "fix: thing\n\nSutando-Task: task-abc123\nnot a trailer line\n\nCo-Authored-By: x"
        self.assertIsNone(route_mod.trailer_task(mixed), "a paragraph with prose is body, not trailer")

    def test_the_newest_commit_with_a_trailer_wins(self):
        msgs = ["a\n\nSutando-Task: task-old1", "b\n\nSutando-Task: task-new2", "c no trailer"]
        self.assertEqual(route_mod.latest_task(msgs), "task-new2")
        self.assertIsNone(route_mod.latest_task(["a", "b\n\nSutando-Task:"]))


class Route(Base):
    def setUp(self):
        super().setUp()
        pool_attribution.record(self.ws, "task-abc123", W)

    def test_our_pr_reaches_its_live_driving_worker(self):
        gh = FakeGh()
        out, _ = self.route(gh)
        self.assertEqual(out, f"handed {W}")
        ws, to, text = self.asks[0]
        self.assertEqual((ws, to), (self.ws, W))
        self.assertTrue(text.startswith(f"[pr-watch] {EVENT}\n"))
        self.assertIn("PR you drive", text)
        self.assertEqual(gh.calls[0][:2], ["api", "graphql"])
        self.assertIn("owner=acme", gh.calls[0])
        self.assertIn("number=7", gh.calls[0])

    def test_a_sentinel_the_watcher_pass_wrote_first_counts(self):
        self.result = file_result(already=[W])
        self.assertEqual(self.route()[0], f"handed {W}")

    def test_a_fenced_or_skipped_file_route_is_local(self):
        for result in (file_result(fenced=[W]), file_result(skipped=[W]), file_result(delivered=["other"]),
                       None, {"route": None}):
            self.result = result
            out, err = self.route()
            self.assertEqual(out, "local", result)
            self.assertIn("not confirmed", err)

    def test_an_unconfirmed_room_post_is_local_and_not_sent_again(self):
        self.result = {"task_id": "task-x", "to": W, "via": "room", "status": "unconfirmed", "event_id": None}
        out, _ = self.route()
        self.assertEqual((out, len(self.asks)), ("local", 1))
        self.result = {"task_id": "task-x", "to": W, "via": "room", "status": "sent", "event_id": "$e1"}
        self.assertEqual(self.route()[0], f"handed {W}")

    def test_a_dead_driving_worker_is_local_without_an_ask(self):
        out, err = self.route(alive=lambda ws, wid: False)
        self.assertEqual((out, self.asks), ("local", []))
        self.assertIn("not alive", err)

    def test_the_watching_session_itself_is_local(self):
        out, err = self.route(me=W)
        self.assertEqual((out, self.asks), ("local", []))
        self.assertIn("this session", err)

    def test_a_failed_ask_is_local(self):
        def boom(*a):
            raise ValueError("no roster")
        out, err = self.route(ask=boom)
        self.assertEqual(out, "local")
        self.assertIn("pool_ask failed: no roster", err)

    def test_no_trailer_an_invalid_one_or_no_attribution_is_local(self):
        for msgs, why in ((["no trailer"], "no Sutando-Task trailer"),
                          (["x\n\nSutando-Task: ../../etc"], "no Sutando-Task trailer"),
                          (["x\n\nSutando-Task: task-unknown9"], "no worker attribution")):
            out, err = self.route(FakeGh(msgs), head="")
            self.assertEqual(out, "local")
            self.assertIn(why, err)
        self.assertEqual(self.asks, [])


class Ownership(Base):
    """A copied or cherry-picked trailer on someone else's PR never moves the event."""

    def setUp(self):
        super().setUp()
        pool_attribution.record(self.ws, "task-abc123", W)

    def test_an_outsiders_pr_requesting_our_review_is_local(self):
        gh = FakeGh(author="mallory")
        event = "PR_EVENT acme/widgets#7 review_requested reviewer=owner-me author=mallory head=0badc0de"
        out, err = self.route(gh, event=event)
        self.assertEqual((out, self.asks, gh.calls), ("local", [], []))
        self.assertIn("not an event about our own PR", err)

    def test_an_outsider_pushing_to_a_pr_we_reviewed_is_local(self):
        event = "PR_EVENT acme/widgets#7 synchronize author=mallory head=0badc0de merged=false reason=reviewed"
        out, _ = self.route(FakeGh(author="mallory"), event=event)
        self.assertEqual((out, self.asks), ("local", []))
        dismissed = "PR_EVENT acme/widgets#7 review_dismissed reviewer=owner-me author=mallory head=0badc0de"
        self.assertEqual(self.route(event=dismissed)[0], "local")

    def test_a_reviewed_update_to_our_own_pr_is_local(self):
        event = "PR_EVENT acme/widgets#7 synchronize author=alice head=0badc0de merged=false reason=reviewed"
        out, err = self.route(event=event)
        self.assertEqual((out, self.asks), ("local", []))
        self.assertIn("not an event about our own PR", err)

    def test_a_pr_by_someone_not_watched_or_from_a_fork_is_local(self):
        for gh in (FakeGh(author="mallory"), FakeGh(head_repo="mallory/widgets"), FakeGh(head_repo="")):
            out, err = self.route(gh, head="")
            self.assertEqual(out, "local")
            self.assertIn("not one of ours", err)
        self.assertEqual(self.asks, [])

    def test_no_concrete_author_to_check_is_local(self):
        for authors in ((), ("*",)):
            gh = FakeGh()
            self.assertEqual(self.route(gh, authors=authors)[0], "local")
            self.assertEqual(gh.calls, [])

    def test_the_author_check_ignores_case(self):
        self.assertEqual(self.route(FakeGh(author="Alice"))[0], f"handed {W}")

    def test_what_is_not_an_event_line_is_local_without_a_call(self):
        gh = FakeGh()
        for event in ("hello", "PR_EVENT x", EVENT + " title=$(rm)", "PR_EVENT " + "a" * 500):
            self.assertEqual(self.route(gh, event=event)[0], "local")
        self.assertEqual(gh.calls, [])


class Defaults(Base):
    def test_the_default_pool_ask_whoami_and_liveness_are_used(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        fake = types.ModuleType("pool_ask")
        fake.whoami = lambda: ME
        fake.ask = self.ask
        fake.who = lambda ws: [{"id": "core", "alive": None}, {"id": W, "alive": True}]
        with mock.patch.dict(sys.modules, {"pool_ask": fake}):
            out = route_mod.route(self.ws, EVENT, "acme/widgets", 7, "h1", ["alice"], self.cache, run=FakeGh())
            self.assertEqual(out, f"handed {W}")
            fake.who = lambda ws: [{"id": W, "alive": None}]
            with contextlib.redirect_stderr(io.StringIO()):
                out = route_mod.route(self.ws, EVENT, "acme/widgets", 7, "h1", ["alice"], self.cache,
                                      run=FakeGh())
            self.assertEqual(out, "local", "unknown liveness is not alive")

    def test_the_real_pool_ask_refusing_is_local(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        with mock.patch.dict(sys.modules), mock.patch.dict("os.environ", {"SUTANDO_INSTANCE_ID": ME}):
            sys.modules.pop("pool_ask", None)
            out, err = self.route(ask=None, me=None)
        self.assertEqual(out, "local")
        self.assertIn("pool_ask failed: no roster", err)


class GitHub(Base):
    def test_failures_are_local_and_not_cached(self):
        for gh in (FakeGh(rc=1), FakeGh(raises=subprocess.TimeoutExpired("gh", 10)),
                   FakeGh(raises=OSError("no gh")), FakeGh(stdout="{not json"),
                   FakeGh(stdout='{"data": {"repository": null}}')):
            out, err = self.route(gh)
            self.assertEqual(out, "local")
            self.assertIn("GitHub call", err)
        self.assertFalse((self.cache / "pr-driver.json").exists())

    def test_one_call_per_head(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        gh = FakeGh()
        self.route(gh, head="h1")
        self.route(gh, head="h1")
        self.assertEqual((len(gh.calls), len(self.asks)), (1, 2))
        self.route(gh, head="h2")
        self.assertEqual(len(gh.calls), 2)

    def test_an_entry_from_an_older_shape_is_a_miss_and_no_head_is_never_cached(self):
        self.cache.mkdir(parents=True)
        (self.cache / "pr-driver.json").write_text(json.dumps({"acme/widgets#7": {"head": "h1", "task": "t"}}))
        gh = FakeGh()
        self.route(gh, head="h1")
        self.route(gh, head="")
        self.route(gh, head="")
        self.assertEqual(len(gh.calls), 3)

    def test_a_corrupt_cache_is_rebuilt_and_the_cache_is_bounded(self):
        self.cache.mkdir(parents=True)
        (self.cache / "pr-driver.json").write_text("[1]")
        with mock.patch.object(route_mod, "CACHE_MAX", 2):
            for n in (1, 2, 3):
                route_mod.pr_facts("acme/widgets", n, "h", self.cache, FakeGh())
        data = json.loads((self.cache / "pr-driver.json").read_text())
        self.assertEqual(sorted(data), ["acme/widgets#2", "acme/widgets#3"])


class Cli(Base):
    ARGS = ["--event", EVENT, "--repo", "acme/widgets", "--number", "7", "--authors", "alice, bob,"]

    def test_main_prints_the_answer(self):
        out = io.StringIO()
        with mock.patch.object(route_mod, "route", return_value="local") as r, contextlib.redirect_stdout(out):
            rc = route_mod.main(self.ARGS + ["--head", "abc", "--cache-dir", str(self.cache),
                                             "--workspace", str(self.ws)])
        self.assertEqual((rc, out.getvalue()), (0, "local\n"))
        r.assert_called_once_with(self.ws, EVENT, "acme/widgets", 7, "abc", ["alice", "bob"], self.cache)

    def test_main_resolves_the_workspace_when_not_given(self):
        fake = types.ModuleType("workspace_default")
        fake.resolve_workspace = lambda *a, **kw: str(self.ws)
        with mock.patch.dict(sys.modules, {"workspace_default": fake}), \
                mock.patch.object(route_mod, "route", return_value="local") as r, \
                contextlib.redirect_stdout(io.StringIO()):
            route_mod.main(self.ARGS + ["--cache-dir", str(self.cache)])
        self.assertEqual(r.call_args[0][0], self.ws)

    def test_gh_runs_the_gh_binary_with_a_timeout(self):
        with mock.patch.object(route_mod.subprocess, "run") as run:
            route_mod.gh(["api", "x"], 10)
        run.assert_called_once_with(["gh", "api", "x"], capture_output=True, text=True, timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
