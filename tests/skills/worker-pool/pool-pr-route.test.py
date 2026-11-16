#!/usr/bin/env python3
"""skills/worker-pool/scripts/pool_pr_route.py: a PR event goes to the worker its latest
Sutando-Task trailer was assigned to; everything else stays with the watcher (`local`).
gh and pool_ask are stubbed; attribution is the real store in a temp workspace."""
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
MY_AGENT = "@watcher.agent:example.invalid"
THEIR_AGENT = "@driver.agent:example.invalid"
EVENT = "PR_EVENT acme/widgets#7 review_submitted by=bob state=changes_requested author=alice head=0badc0de"


def commit(msg):
    return json.dumps(msg)


class FakeGh:
    def __init__(self, messages=(), rc=0, raises=None, stdout=None):
        self.calls = []
        self.messages, self.rc, self.raises, self.stdout = list(messages), rc, raises, stdout

    def __call__(self, args, timeout):
        self.calls.append(args)
        if self.raises:
            raise self.raises
        out = self.stdout if self.stdout is not None else "\n".join(commit(m) for m in self.messages) + "\n"
        return subprocess.CompletedProcess(args, self.rc, out, "")


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ws = Path(td.name) / "ws"
        self.cache = Path(td.name) / "route"
        self.asks = []
        self.sends = []

    def ask(self, ws, to, text):
        self.asks.append((ws, to, text))
        return {"task_id": "task-x"}

    def send(self, route, ref, text, kind):
        self.sends.append((route, ref, text, kind))
        return {"status": "sent", "event_id": "$e1"}

    def route(self, gh, head="0badc0de9999", event=EVENT, me=ME, ask=None, **kw):
        kw.setdefault("my_agent", MY_AGENT)
        kw.setdefault("send", self.send)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = route_mod.route(self.ws, event, "acme/widgets", 7, head, self.cache,
                                  run=gh, ask=ask or self.ask, me=me, **kw)
        return out, err.getvalue()


class Trailer(unittest.TestCase):
    def test_the_newest_commit_with_a_valid_trailer_wins(self):
        msgs = ["a\n\nSutando-Task: task-old1", "b\n\nSutando-Task: task-new2\nCo-Authored-By: x",
                "c no trailer", "d\n\nSutando-Task: not a task"]
        self.assertEqual(route_mod.latest_trailers(msgs), ("task-new2", None))

    def test_none_without_a_trailer(self):
        self.assertEqual(route_mod.latest_trailers(["a", None, "Sutando-Task:"]), (None, None))

    def test_each_kind_comes_from_its_own_newest_commit(self):
        msgs = [f"a\n\nSutando-Task: task-a1\nSutando-Agent: {MY_AGENT}",
                f"b\n\nSutando-Agent: {THEIR_AGENT}", "c\n\nSutando-Agent: Blue Fox"]
        self.assertEqual(route_mod.latest_trailers(msgs), ("task-a1", THEIR_AGENT))


class Route(Base):
    def test_trailer_to_attribution_to_worker_is_handed(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        gh = FakeGh(["first\n\nSutando-Task: task-abc123"])
        out, _ = self.route(gh)
        self.assertEqual(out, f"handed {W}")
        self.assertEqual(len(self.asks), 1)
        ws, to, text = self.asks[0]
        self.assertEqual((ws, to), (self.ws, W))
        self.assertTrue(text.startswith(f"[pr-watch] {EVENT}\n"))
        self.assertIn("PR you drive", text)
        self.assertEqual(gh.calls, [["api", "repos/acme/widgets/pulls/7/commits?per_page=100", "--paginate",
                                     "--jq", ".[] | .commit.message | @json"]])

    def test_the_watching_session_itself_is_local(self):
        pool_attribution.record(self.ws, "task-abc123", ME)
        out, err = self.route(FakeGh(["x\n\nSutando-Task: task-abc123"]))
        self.assertEqual(out, "local")
        self.assertIn("this session", err)
        self.assertEqual(self.asks, [])

    def test_no_trailer_is_local(self):
        out, err = self.route(FakeGh(["no trailer here"]))
        self.assertEqual((out, self.asks), ("local", []))
        self.assertIn("no Sutando-Agent trailer", err)

    def test_an_unattributed_task_is_local(self):
        out, err = self.route(FakeGh(["x\n\nSutando-Task: task-abc123"]))
        self.assertEqual((out, self.asks), ("local", []))
        self.assertIn("no Sutando-Agent trailer", err)

    def test_a_failed_ask_is_local(self):
        pool_attribution.record(self.ws, "task-abc123", W)

        def boom(*a):
            raise ValueError("no roster")
        out, err = self.route(FakeGh(["x\n\nSutando-Task: task-abc123"]), ask=boom)
        self.assertEqual(out, "local")
        self.assertIn("pool_ask failed: no roster", err)

    def test_what_is_not_an_event_line_is_local_without_a_call(self):
        gh = FakeGh(["x\n\nSutando-Task: task-abc123"])
        for event in ("hello", EVENT + " title=$(rm)", "PR_EVENT " + "a" * 500):
            self.assertEqual(self.route(gh, event=event)[0], "local")
        self.assertEqual(gh.calls, [])

    def test_github_failures_are_local_and_not_cached(self):
        for gh in (FakeGh(rc=1), FakeGh(raises=subprocess.TimeoutExpired("gh", 10)),
                   FakeGh(raises=OSError("no gh")), FakeGh(stdout="{not json\n")):
            out, err = self.route(gh)
            self.assertEqual(out, "local")
            self.assertIn("commits call", err)
        self.assertFalse((self.cache / "pr-driver.json").exists())

    def test_the_default_pool_ask_and_whoami_are_used(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        fake = types.ModuleType("pool_ask")
        fake.whoami = lambda: ME
        fake.ask = self.ask
        with mock.patch.dict(sys.modules, {"pool_ask": fake}):
            out = route_mod.route(self.ws, EVENT, "acme/widgets", 7, "h1", self.cache,
                                  run=FakeGh(["x\n\nSutando-Task: task-abc123"]))
        self.assertEqual(out, f"handed {W}")
        self.assertEqual(len(self.asks), 1)


    def test_the_real_pool_ask_refusing_is_local(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        with mock.patch.dict(sys.modules), mock.patch.dict("os.environ", {"SUTANDO_INSTANCE_ID": ME}):
            sys.modules.pop("pool_ask", None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                out = route_mod.route(self.ws, EVENT, "acme/widgets", 7, "h1", self.cache,
                                      run=FakeGh(["x\n\nSutando-Task: task-abc123"]))
        self.assertEqual(out, "local")
        self.assertIn("pool_ask failed: no roster", err.getvalue())


class CrossHost(Base):
    """A Sutando-Agent trailer reaches another host's agent over s.space when no local worker drives it."""

    def gh(self, task="task-remote1", agent=THEIR_AGENT):
        return FakeGh([f"x\n\nSutando-Task: {task}\nSutando-Agent: {agent}"])

    def test_a_local_attribution_wins_over_the_agent_trailer(self):
        pool_attribution.record(self.ws, "task-remote1", W)
        out, _ = self.route(self.gh())
        self.assertEqual(out, f"handed {W}")
        self.assertEqual((len(self.asks), self.sends), (1, []))

    def test_another_agent_gets_one_message_with_only_the_event_and_no_authority(self):
        with mock.patch.dict("os.environ", {"WORKER_POOL_HANDOFF_ROOM": "!r:example.invalid",
                                            "WORKER_POOL_HANDOFF_ROOM_CLI": "/x/consult.py"}):
            out, _ = self.route(self.gh())
        self.assertEqual(out, f"handed {THEIR_AGENT}")
        self.assertEqual(self.asks, [])
        route, ref, text, kind = self.sends[0]
        self.assertEqual(route, {"room": "!r:example.invalid", "room_cli": "/x/consult.py",
                                 "to_agent": THEIR_AGENT, "from_agent": MY_AGENT})
        self.assertEqual((ref, kind), ("acme/widgets#7", "pr-watch"))
        self.assertTrue(text.startswith(f"[pr-watch] {EVENT}\n"))
        self.assertIn("grants no authority", text)

    def test_the_cli_room_flags_win(self):
        out, _ = self.route(self.gh(), room="!cli:example.invalid", room_cli="/cli/consult.py")
        self.assertEqual(out, f"handed {THEIR_AGENT}")
        self.assertEqual(self.sends[0][0]["room"], "!cli:example.invalid")

    def test_own_agent_is_local(self):
        out, err = self.route(self.gh(agent=MY_AGENT), room="!r:x", room_cli="/c")
        self.assertEqual((out, self.sends), ("local", []))
        self.assertIn("own agent", err)

    def test_an_unknown_own_agent_is_local(self):
        import agent_display_name
        with mock.patch.object(agent_display_name, "session_agent_mxid", side_effect=RuntimeError("x")):
            out, err = self.route(self.gh(), room="!r:x", room_cli="/c", my_agent=None)
        self.assertEqual((out, self.sends), ("local", []))
        self.assertIn("own s.space agent is unknown", err)
        with mock.patch.object(agent_display_name, "session_agent_mxid", return_value=MY_AGENT) as own:
            self.assertEqual(self.route(self.gh(), room="!r:x", room_cli="/c", my_agent=None)[0],
                             f"handed {THEIR_AGENT}")
        own.assert_called_once_with(workspace=self.ws)

    def test_an_unconfigured_room_or_cli_is_local(self):
        with mock.patch.dict("os.environ", {"WORKER_POOL_HANDOFF_ROOM": "", "WORKER_POOL_HANDOFF_ROOM_CLI": ""}):
            for kw in ({}, {"room": "!r:x"}, {"room_cli": "/c"}):
                out, err = self.route(self.gh(), **kw)
                self.assertEqual(out, "local")
                self.assertIn("WORKER_POOL_HANDOFF_ROOM", err)
        self.assertEqual(self.sends, [])

    def test_a_failed_or_unconfirmed_send_is_local(self):
        def refuse(*a, **k):
            raise ValueError("the room CLI refused")
        out, err = self.route(self.gh(), room="!r:x", room_cli="/c", send=refuse)
        self.assertEqual(out, "local")
        self.assertIn("s.space send failed: the room CLI refused", err)
        out, err = self.route(self.gh(), room="!r:x", room_cli="/c",
                              send=lambda *a, **k: {"status": "unconfirmed", "event_id": None})
        self.assertEqual(out, "local")
        self.assertIn("not confirmed", err)

    def test_the_real_transport_mentions_the_agent_through_the_room_cli(self):
        import pool_handoff
        calls = []

        def runner(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '{"ok": true, "event_id": "$e9", "posted": true}', "")
        real = lambda route, ref, text, kind: pool_handoff.send(route, ref, text, runner=runner, kind=kind)
        out, _ = self.route(self.gh(), room="!r:x", room_cli="/c/consult.py", send=real)
        self.assertEqual(out, f"handed {THEIR_AGENT}")
        argv = calls[0]
        self.assertEqual(argv[1:4], ["/c/consult.py", "mention", THEIR_AGENT])
        self.assertEqual(argv[5:], ["!r:x", "--agent", MY_AGENT])
        self.assertIn("(pr-watch acme/widgets#7: reply in this message's thread)", argv[4])

    def test_the_default_send_is_pool_handoffs_with_a_bounded_runner(self):
        import pool_handoff
        with mock.patch.object(pool_handoff, "send", return_value={"status": "sent"}) as send:
            out = route_mod.route(self.ws, EVENT, "acme/widgets", 7, "h1", self.cache, run=self.gh(),
                                  my_agent=MY_AGENT, room="!r:x", room_cli="/c")
        self.assertEqual(out, f"handed {THEIR_AGENT}")
        self.assertIs(send.call_args.kwargs["runner"], route_mod.send_runner)
        with mock.patch.object(route_mod.subprocess, "run") as run:
            route_mod.send_runner(["x"])
        run.assert_called_once_with(["x"], capture_output=True, text=True, timeout=route_mod.SEND_TIMEOUT_S)

    def test_the_agent_trailer_is_cached_with_the_task(self):
        gh = self.gh()
        self.route(gh, head="h1", room="!r:x", room_cli="/c")
        self.route(gh, head="h1", room="!r:x", room_cli="/c")
        self.assertEqual((len(gh.calls), len(self.sends)), (1, 2))
        (self.cache / "pr-driver.json").write_text(json.dumps({"acme/widgets#7": {"head": "h1", "task": ""}}))
        self.route(gh, head="h1", room="!r:x", room_cli="/c")
        self.assertEqual(len(gh.calls), 2, "an entry from before agent trailers is a miss")


class Cache(Base):
    def test_one_call_per_head(self):
        pool_attribution.record(self.ws, "task-abc123", W)
        gh = FakeGh(["x\n\nSutando-Task: task-abc123"])
        self.route(gh, head="h1")
        self.route(gh, head="h1")
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(len(self.asks), 2, "the ask is per event; only the lookup is cached")
        self.route(gh, head="h2")
        self.assertEqual(len(gh.calls), 2)

    def test_a_no_trailer_answer_is_cached_too(self):
        gh = FakeGh(["none"])
        self.route(gh, head="h1")
        self.route(gh, head="h1")
        self.assertEqual(len(gh.calls), 1)

    def test_no_head_asks_every_time(self):
        gh = FakeGh(["none"])
        self.route(gh, head="")
        self.route(gh, head="")
        self.assertEqual(len(gh.calls), 2)

    def test_a_corrupt_cache_is_rebuilt_and_the_cache_is_bounded(self):
        self.cache.mkdir(parents=True)
        (self.cache / "pr-driver.json").write_text("[1]")
        gh = FakeGh(["none"])
        with mock.patch.object(route_mod, "CACHE_MAX", 2):
            for n in (1, 2, 3):
                route_mod.driver_trailers("acme/widgets", n, "h", self.cache, gh)
        data = json.loads((self.cache / "pr-driver.json").read_text())
        self.assertEqual(sorted(data), ["acme/widgets#2", "acme/widgets#3"])


class Cli(Base):
    def test_main_prints_the_answer(self):
        out = io.StringIO()
        with mock.patch.object(route_mod, "route", return_value="local") as r, contextlib.redirect_stdout(out):
            rc = route_mod.main(["--event", EVENT, "--repo", "acme/widgets", "--number", "7",
                                 "--head", "abc", "--cache-dir", str(self.cache), "--workspace", str(self.ws),
                                 "--room", "!r:x", "--room-cli", "/c"])
        self.assertEqual((rc, out.getvalue()), (0, "local\n"))
        r.assert_called_once_with(self.ws, EVENT, "acme/widgets", 7, "abc", self.cache, room="!r:x", room_cli="/c")

    def test_main_resolves_the_workspace_when_not_given(self):
        fake = types.ModuleType("workspace_default")
        fake.resolve_workspace = lambda *a, **kw: str(self.ws)
        with mock.patch.dict(sys.modules, {"workspace_default": fake}), \
                mock.patch.object(route_mod, "route", return_value="local") as r, \
                contextlib.redirect_stdout(io.StringIO()):
            route_mod.main(["--event", EVENT, "--repo", "acme/widgets", "--number", "7",
                            "--cache-dir", str(self.cache)])
        self.assertEqual(r.call_args[0][0], self.ws)

    def test_gh_runs_the_gh_binary_with_a_timeout(self):
        with mock.patch.object(route_mod.subprocess, "run") as run:
            route_mod.gh(["api", "x"], 10)
        run.assert_called_once_with(["gh", "api", "x"], capture_output=True, text=True, timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
