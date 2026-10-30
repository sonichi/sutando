#!/usr/bin/env python3
"""skills/pr-watch/scripts/pr_watch.py: the watch-config CLI, the event filter over fixture
payloads for each forwarded event type, the JSON stream splitter, the hook-id record (only
recorded ids are ever deleted, through a fake `gh` on PATH), install-mod's file placement, the
settings precedence, the prompt template line, and the tracker (render, record, publish, the
hand-back to the session, and the runner's row upsert)."""
import contextlib
import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "pr_watch", str(REPO / "skills" / "pr-watch" / "scripts" / "pr_watch.py"))
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

WATCHES = [{"repo": "acme/widgets", "authors": ["alice"], "me": ["owner-me"]}]


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class _Tmp(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in pw.SETTINGS:
            os.environ.pop(key, None)
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.state = self.tmp / "state" / "pr-watch"

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = pw.main(["--state-dir", str(self.state)] + list(argv))
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 2
                err.write(str(exc.code))
        return rc, out.getvalue(), err.getvalue()


class ConfigCli(_Tmp):
    def test_add_list_remove(self):
        rc, out, _ = self.cli("add", "--repo", "acme/widgets", "--author", "alice", "--me", "owner-me")
        self.assertEqual(rc, 0)
        cfg = json.loads((self.state / "config.json").read_text())
        self.assertEqual(cfg["watches"], WATCHES)
        self.assertNotIn("prompt", cfg)
        rc, out, _ = self.cli("list")
        self.assertEqual(json.loads(out), WATCHES)
        rc, _, _ = self.cli("remove", "--repo", "ACME/widgets")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["watches"], [])

    def test_add_merges_into_existing_entry_without_duplicates(self):
        self.cli("add", "--repo", "acme/widgets", "--author", "alice")
        self.cli("add", "--repo", "acme/widgets", "--author", "Alice", "--author", "dave", "--me", "me1")
        w = json.loads((self.state / "config.json").read_text())["watches"]
        self.assertEqual(w, [{"repo": "acme/widgets", "authors": ["alice", "dave"], "me": ["me1"]}])

    def test_add_keeps_stored_settings(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        self.cli("add", "--repo", "acme/widgets", "--author", "alice")
        cfg = json.loads((self.state / "config.json").read_text())
        self.assertEqual(cfg["settings"], {"PR_WATCH_TRACKER_ROOM": "!room:server"})

    def test_bad_input_is_refused(self):
        self.assertNotEqual(self.cli("add", "--repo", "widgets", "--author", "a")[0], 0)
        self.assertNotEqual(self.cli("add", "--repo", "acme/widgets")[0], 0)
        self.assertNotEqual(self.cli("add", "--repo", "acme/widgets", "--author", "bad login")[0], 0)
        self.assertFalse((self.state / "config.json").exists())

    def test_reviewed_flag_needs_a_me_login_and_is_kept_on_merge(self):
        self.assertNotEqual(self.cli("add", "--repo", "acme/widgets", "--reviewed")[0], 0)
        self.assertNotEqual(self.cli("add", "--repo", "acme/widgets", "--me", "*", "--reviewed")[0], 0)
        self.assertFalse((self.state / "config.json").exists())
        self.assertEqual(self.cli("add", "--repo", "acme/widgets", "--me", "owner-me", "--reviewed")[0], 0)
        self.cli("add", "--repo", "ACME/widgets", "--author", "alice")
        w = json.loads((self.state / "config.json").read_text())["watches"]
        self.assertEqual(w, [{"repo": "acme/widgets", "authors": ["alice"], "me": ["owner-me"], "reviewed": True}])
        rc, out, _ = self.cli("add", "--repo", "acme/other", "--me", "owner-me", "--reviewed")
        self.assertEqual(json.loads(out)["reviewed"], True)

    def test_remove_unknown_repo_fails(self):
        self.assertEqual(self.cli("remove", "--repo", "acme/none")[0], 1)

    def test_status_reports_without_a_runner(self):
        self.cli("add", "--repo", "acme/widgets", "--author", "alice")
        rc, out, _ = self.cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("runner: not running", out)
        self.assertIn("forwarder acme/widgets: not running", out)
        self.assertIn("recorded hook ids: none", out)


class EventFilter(unittest.TestCase):
    def line(self, name, watches=WATCHES):
        return pw.classify(fixture(name), watches)

    def test_author_pull_request_actions(self):
        self.assertEqual(self.line("pull_request.opened"),
                         "PR_EVENT acme/widgets#7 opened author=alice head=a1b2c3d4 merged=false reason=author")
        self.assertEqual(self.line("pull_request.synchronize"),
                         "PR_EVENT acme/widgets#7 synchronize author=alice head=0badc0de merged=false reason=author")
        self.assertIn(" reopened ", self.line("pull_request.reopened"))
        self.assertIn(" ready_for_review ", self.line("pull_request.ready_for_review"))
        self.assertEqual(self.line("pull_request.closed_merged"),
                         "PR_EVENT acme/widgets#7 closed author=alice head=0badc0de merged=true reason=author")
        self.assertTrue(self.line("pull_request.closed_unmerged").endswith("merged=false reason=author"))

    def test_other_actions_and_authors_are_dropped(self):
        self.assertIsNone(self.line("pull_request.labeled"))
        other = [{"repo": "acme/widgets", "authors": ["zed"], "me": []}]
        self.assertIsNone(self.line("pull_request.opened", other))
        self.assertIsNone(self.line("pull_request.opened", [{"repo": "acme/other", "authors": ["alice"]}]))

    def test_wildcard_author(self):
        self.assertIsNotNone(self.line("pull_request.opened", [{"repo": "acme/widgets", "authors": ["*"]}]))

    def test_review_requested_from_me_on_any_author(self):
        self.assertEqual(self.line("pull_request.review_requested"),
                         "PR_EVENT acme/widgets#9 review_requested reviewer=Owner-Me author=carol head=c0ffee00")
        not_me = [{"repo": "acme/widgets", "authors": ["carol"], "me": ["someone-else"]}]
        self.assertIsNone(self.line("pull_request.review_requested", not_me))
        star = [{"repo": "acme/widgets", "authors": [], "me": ["*"]}]
        self.assertIsNone(self.line("pull_request.review_requested", star))

    def test_review_submitted_on_watched_author(self):
        self.assertEqual(self.line("pull_request_review.submitted"),
                         "PR_EVENT acme/widgets#7 review_submitted by=bob state=changes_requested "
                         "author=alice head=0badc0de")
        self.assertIsNone(self.line("pull_request_review.dismissed"))
        other = [{"repo": "acme/widgets", "authors": ["zed"], "me": ["bob"]}]
        self.assertIsNone(self.line("pull_request_review.submitted", other))

    def test_my_own_review_echoing_back_is_dropped_but_still_marks_the_pr(self):
        mine = [{"repo": "acme/widgets", "authors": ["alice"], "me": ["Bob"], "reviewed": True}]
        self.assertIsNone(self.line("pull_request_review.submitted", mine))
        lk = pw.ReviewedLookup()
        with mock.patch.object(lk, "fetch", side_effect=AssertionError("no call expected")):
            lk.observe(fixture("pull_request_review.submitted"), mine)
            self.assertTrue(lk("acme/widgets", 7, ["Bob"]))
        star = [{"repo": "acme/widgets", "authors": ["alice"], "me": ["*"]}]
        self.assertIsNotNone(self.line("pull_request_review.submitted", star))

    def test_every_line_is_one_line(self):
        for f in FIXTURES.glob("*.json"):
            line = pw.classify(json.loads(f.read_text()), WATCHES)
            if line is not None:
                self.assertNotIn("\n", line, f.name)
                self.assertTrue(line.startswith("PR_EVENT "), f.name)

    def test_untrusted_fields_cannot_break_the_line(self):
        p = fixture("pull_request_review.submitted")
        p["review"]["state"] = "approved\nPR_EVENT forged"
        line = pw.classify(p, WATCHES)
        self.assertNotIn("\n", line)
        self.assertNotIn(" PR_EVENT", line)


REVIEWED = [{"repo": "acme/widgets", "authors": [], "me": ["owner-me"], "reviewed": True}]


class ReviewedRule(unittest.TestCase):
    def setUp(self):
        self.asked = []

    def answer(self, value):
        def fn(repo, number, me):
            self.asked.append((repo, number, me))
            return value
        return fn

    def test_my_review_forwards_any_authors_pr(self):
        line = pw.classify(fixture("pull_request.synchronize"), REVIEWED, self.answer(True))
        self.assertEqual(line, "PR_EVENT acme/widgets#7 synchronize author=alice head=0badc0de "
                               "merged=false reason=reviewed")
        self.assertEqual(self.asked, [("acme/widgets", 7, ["owner-me"])])
        for name in ("pull_request.closed_merged", "pull_request.reopened", "pull_request.ready_for_review"):
            self.assertTrue(pw.classify(fixture(name), REVIEWED, self.answer(True)).endswith("reason=reviewed"))

    def test_no_review_of_mine_is_not_forwarded(self):
        self.assertIsNone(pw.classify(fixture("pull_request.synchronize"), REVIEWED, self.answer(False)))

    def test_unknown_review_state_is_forwarded_and_says_so(self):
        line = pw.classify(fixture("pull_request.synchronize"), REVIEWED, self.answer(None))
        self.assertTrue(line.endswith("merged=false reason=reviewed reviewed=unknown"), line)
        self.assertTrue(pw.classify(fixture("pull_request.synchronize"), REVIEWED).endswith("reviewed=unknown"))

    def test_only_the_listed_actions_and_only_when_asked_for(self):
        self.assertIsNone(pw.classify(fixture("pull_request.opened"), REVIEWED, self.answer(True)))
        self.assertIsNone(pw.classify(fixture("pull_request.labeled"), REVIEWED, self.answer(True)))
        plain = [dict(REVIEWED[0], reviewed=False)]
        self.assertIsNone(pw.classify(fixture("pull_request.synchronize"), plain, self.answer(True)))
        star = [dict(REVIEWED[0], me=["*"])]
        self.assertIsNone(pw.classify(fixture("pull_request.synchronize"), star, self.answer(True)))
        self.assertEqual(self.asked, [])

    def test_author_and_reviewed_match_yields_one_author_line_without_a_lookup(self):
        both = [dict(REVIEWED[0], authors=["alice"])]
        line = pw.classify(fixture("pull_request.synchronize"), both, self.answer(True))
        self.assertEqual(line, "PR_EVENT acme/widgets#7 synchronize author=alice head=0badc0de "
                               "merged=false reason=author")
        self.assertEqual(self.asked, [])

    def test_a_bad_pr_number_reads_as_unknown(self):
        p = fixture("pull_request.synchronize")
        p["pull_request"]["number"] = "x"
        self.assertTrue(pw.classify(p, REVIEWED, self.answer(True)).endswith("reviewed=unknown"))

    def test_my_dismissed_review_is_forwarded(self):
        p = fixture("pull_request_review.dismissed")
        p["review"]["user"]["login"] = "Owner-Me"
        self.assertEqual(pw.classify(p, REVIEWED), "PR_EVENT acme/widgets#7 review_dismissed "
                                                   "reviewer=Owner-Me author=alice head=0badc0de")
        plain_me = [{"repo": "acme/widgets", "authors": [], "me": ["owner-me"]}]
        self.assertIsNotNone(pw.classify(p, plain_me))

    def test_someone_elses_dismissed_review_is_not(self):
        self.assertIsNone(pw.classify(fixture("pull_request_review.dismissed"), REVIEWED))
        p = fixture("pull_request_review.dismissed")
        p["review"]["user"] = {}
        self.assertIsNone(pw.classify(p, REVIEWED))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ReviewedCache(unittest.TestCase):
    def lookup(self, outputs):
        clock = Clock()
        lk = pw.ReviewedLookup(ttl=600, timeout=5, clock=clock)
        calls = []

        def fake_gh(args, check=False, timeout=None):
            calls.append((args, timeout))
            out = outputs.pop(0)
            if isinstance(out, BaseException):
                raise out
            return subprocess.CompletedProcess(args, out[0], out[1], "")
        p = mock.patch.object(pw, "gh", side_effect=fake_gh)
        p.start()
        self.addCleanup(p.stop)
        return lk, clock, calls

    def test_asks_the_reviews_api_once_and_matches_any_me_login(self):
        lk, _, calls = self.lookup([(0, "bob\nOWNER-ME\n")])
        self.assertTrue(lk("acme/widgets", 7, ["owner-me"]))
        self.assertTrue(lk("ACME/widgets", 7, ["owner-me"]))
        self.assertEqual(len(calls), 1)
        args, timeout = calls[0]
        self.assertEqual(args[:2], ["api", "repos/acme/widgets/pulls/7/reviews"])
        self.assertIn("--paginate", args)
        self.assertEqual(args[-2:], ["--jq", ".[].user.login"])
        self.assertEqual(timeout, 5)

    def test_a_no_is_cached_for_the_ttl_and_a_yes_for_good(self):
        lk, clock, calls = self.lookup([(0, "bob\n"), (0, "owner-me\n")])
        self.assertFalse(lk("acme/widgets", 7, ["owner-me"]))
        clock.t += 599
        self.assertFalse(lk("acme/widgets", 7, ["owner-me"]))
        self.assertEqual(len(calls), 1)
        clock.t += 2
        self.assertTrue(lk("acme/widgets", 7, ["owner-me"]))
        clock.t += 10 ** 6
        self.assertTrue(lk("acme/widgets", 7, ["owner-me"]))
        self.assertEqual(len(calls), 2)

    def test_failures_answer_unknown_and_are_not_cached(self):
        lk, _, calls = self.lookup([(1, ""), subprocess.TimeoutExpired("gh", 5), OSError("no gh"), (0, "owner-me")])
        self.assertIsNone(lk("acme/widgets", 7, ["owner-me"]))
        self.assertIsNone(lk("acme/widgets", 7, ["owner-me"]))
        self.assertIsNone(lk("acme/widgets", 7, ["owner-me"]))
        self.assertTrue(lk("acme/widgets", 7, ["owner-me"]))
        self.assertEqual(len(calls), 4)

    def test_my_submitted_review_marks_the_pr_without_a_call(self):
        lk, _, calls = self.lookup([(0, "")])
        self.assertFalse(lk("acme/widgets", 7, ["owner-me"]))
        p = fixture("pull_request_review.submitted")
        p["review"]["user"]["login"] = "owner-me"
        lk.observe(p, REVIEWED)
        self.assertTrue(lk("acme/widgets", 7, ["owner-me"]))
        self.assertEqual(len(calls), 1)

    def test_observe_ignores_other_reviews_and_payloads(self):
        lk, _, _ = self.lookup([])
        lk.observe(fixture("pull_request_review.submitted"), REVIEWED)
        lk.observe(fixture("pull_request.synchronize"), REVIEWED)
        lk.observe(fixture("pull_request_review.submitted"), [{"repo": "acme/other", "me": ["bob"]}])
        p = fixture("pull_request_review.submitted")
        p["review"]["user"]["login"] = "owner-me"
        p["pull_request"]["number"] = None
        lk.observe(p, REVIEWED)
        self.assertEqual(lk.cache, {})

    def test_classify_with_the_real_lookup(self):
        lk, _, _ = self.lookup([(0, "owner-me")])
        line = pw.classify(fixture("pull_request.synchronize"), REVIEWED, lk)
        self.assertTrue(line.endswith("reason=reviewed"), line)


class JsonStream(unittest.TestCase):
    def test_pretty_and_compact_objects_and_partial_tail(self):
        a = json.dumps(fixture("pull_request.opened"), indent=2)
        b = json.dumps(fixture("pull_request.synchronize"))
        objs, rest = pw.split_json_stream(a + "\n" + b + "\n" + b[:40])
        self.assertEqual([o["action"] for o in objs], ["opened", "synchronize"])
        objs, rest = pw.split_json_stream(rest + b[40:] + "\n")
        self.assertEqual([o["action"] for o in objs], ["synchronize"])
        self.assertEqual(rest, "")

    def test_noise_and_malformed_objects_are_skipped(self):
        objs, rest = pw.split_json_stream('Forwarding events\n{oops\n{"action": "x"}\n')
        self.assertEqual(objs, [{"action": "x"}])
        self.assertEqual(rest, "")


FAKE_GH = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:3] == ["api", "-X", "DELETE"]:
    hook = a[3].rsplit("/", 1)[1]
    if hook in os.environ.get("FAKE_GH_FAIL", "").split():
        sys.stderr.write("HTTP 500\\n"); sys.exit(1)
    if hook in os.environ.get("FAKE_GH_GONE", "").split():
        sys.stderr.write("gh: Not Found (HTTP 404)\\n"); sys.exit(1)
    sys.exit(0)
if a[:1] == ["api"]:
    print(os.environ.get("FAKE_GH_HOOKS", "")); sys.exit(0)
sys.exit(0)
"""


class HookRecord(_Tmp):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "gh.log"
        self.env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "FAKE_GH_LOG": str(self.log)}
        self.st = pw.State(self.state)
        self.st.ensure()

    def deletes(self):
        if not self.log.exists():
            return []
        return [l.split()[-1] for l in self.log.read_text().splitlines() if l.startswith("api -X DELETE")]

    def test_only_recorded_ids_are_deleted(self):
        pw.record_hooks(self.st, "acme/widgets", [11, 12])
        pw.record_hooks(self.st, "acme/other", [21])
        with mock.patch.dict(os.environ, self.env):
            left = pw.drop_recorded(self.st, "acme/widgets")
        self.assertEqual(self.deletes(), ["repos/acme/widgets/hooks/11", "repos/acme/widgets/hooks/12"])
        self.assertEqual(left, {"acme/other": [21]})
        self.assertEqual(json.loads(self.st.hooks.read_text()), {"acme/other": [21]})

    def test_failed_delete_stays_recorded_and_404_counts_as_gone(self):
        pw.record_hooks(self.st, "acme/widgets", [11, 12, 13])
        with mock.patch.dict(os.environ, dict(self.env, FAKE_GH_FAIL="12", FAKE_GH_GONE="13")):
            left = pw.drop_recorded(self.st)
        self.assertEqual(left, {"acme/widgets": [12]})

    def test_cleanup_command_and_its_runner_guard(self):
        pw.record_hooks(self.st, "acme/widgets", [11])
        with mock.patch.object(pw, "runner_pid", return_value=4242):
            self.assertEqual(self.cli("cleanup")[0], 1)
        self.assertEqual(self.deletes(), [])
        with mock.patch.dict(os.environ, self.env):
            rc, out, _ = self.cli("cleanup")
        self.assertEqual(rc, 0)
        self.assertEqual(self.deletes(), ["repos/acme/widgets/hooks/11"])

    def test_forwarder_hook_listing_asks_for_cli_hooks_by_url(self):
        with mock.patch.dict(os.environ, dict(self.env, FAKE_GH_HOOKS="5\n3")):
            self.assertEqual(pw.forwarder_hook_ids("acme/widgets"), [3, 5])
        call = self.log.read_text().splitlines()[-1]
        self.assertIn("repos/acme/widgets/hooks", call)
        self.assertIn('.name == "cli"', call)
        self.assertIn("webhook-forwarder.github.com", call)


class InstallMod(_Tmp):
    def test_writes_mod_and_runner_outside_the_mod_folder(self):
        mods = self.tmp / "dev-mods"
        rc, out, _ = self.cli("install-mod", "--mods-dir", str(mods))
        self.assertEqual(rc, 0)
        mod = mods / "pr-watch"
        for rel in (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.ts",
                    "tests/watch.test.ts"):
            self.assertTrue((mod / rel).is_file(), rel)
        paths = (mod / "hooks" / "paths.ts").read_text()
        self.assertIn(json.dumps(str(self.state.resolve())), paths)
        runner = self.state / "pr_watch.py"
        self.assertTrue(runner.is_file())
        self.assertIn(json.dumps(str(runner.resolve())), paths)
        self.assertEqual((self.state / "manifest.json").read_text(), pw.MANIFEST.read_text())

    def test_refuses_a_state_dir_inside_the_mod(self):
        mods = self.tmp / "dev-mods"
        self.state = mods / "pr-watch" / "state"
        self.assertNotEqual(self.cli("install-mod", "--mods-dir", str(mods))[0], 0)

    def test_the_checkout_copy_resolves_through_the_workspace_loader(self):
        sys.path.insert(0, str(REPO / "src"))
        with mock.patch("workspace_default.resolve_workspace", return_value=self.tmp / "ws") as rw:
            self.assertEqual(pw.resolve_state_dir(), self.tmp / "ws" / "state" / "pr-watch")
        rw.assert_called_once_with()

    def test_the_state_copy_resolves_its_own_dir(self):
        self.cli("install-mod", "--mods-dir", str(self.tmp / "dev-mods"))
        copy = self.state / "pr_watch.py"
        spec = importlib.util.spec_from_file_location("pr_watch_copy", str(copy))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(m.resolve_state_dir(), self.state.resolve())
        self.assertEqual(m.MANIFEST, (self.state / "manifest.json").resolve())
        self.assertIn("{event}", m.setting("PR_WATCH_PROMPT"))
        # The copy has no checkout around it: a relative commons script resolves in the installer's.
        self.assertEqual((self.state / "repo_root").read_text().strip(), str(REPO.resolve()))
        self.assertEqual(m.commons_script(m.State(self.state.resolve())),
                         REPO.resolve() / "skills" / "room-commons" / "scripts" / "room_commons.py")


RUN_GH = """#!/usr/bin/env python3
import os, signal, sys, time
log = os.environ["FAKE_GH_LOG"]; hooks = os.environ["FAKE_GH_HOOKFILE"]
with open(log, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:2] == ["extension", "list"]:
    print("gh webhook  cli/gh-webhook  v0.2.0"); sys.exit(0)
if a[:3] == ["api", "-X", "DELETE"]:
    sys.exit(0)
if a[:1] == ["api"]:
    print(open(hooks).read() if os.path.exists(hooks) else ""); sys.exit(0)
if a[:2] == ["webhook", "forward"]:
    with open(hooks, "a") as fh:
        fh.write("77\\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    for name in os.environ["FAKE_GH_EMIT"].split(os.pathsep):
        sys.stdout.write(open(name).read()); sys.stdout.flush()
    while True:
        time.sleep(0.2)
sys.exit(0)
"""


class Runner(_Tmp):
    """The real `run` loop against a fake gh: events become lines, the hook it made is
    recorded and deleted on stop, and a hook it did not make is never touched."""

    def start(self, emit, preexisting=""):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(RUN_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "gh.log"
        hookfile = self.tmp / "hooks.txt"
        hookfile.write_text(preexisting)
        self.cli("add", "--repo", "acme/widgets", "--author", "alice", "--me", "owner-me")
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(self.tmp),
               "FAKE_GH_LOG": str(self.log), "FAKE_GH_HOOKFILE": str(hookfile),
               "FAKE_GH_EMIT": os.pathsep.join(str(FIXTURES / f"{n}.json") for n in emit)}
        script = REPO / "skills" / "pr-watch" / "scripts" / "pr_watch.py"
        self.proc = subprocess.Popen([sys.executable, str(script), "--state-dir", str(self.state), "run"],
                                     stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self._kill)
        watchdog = threading.Timer(60, self.proc.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=20), 0)

    def deletes(self):
        return [l.split()[-1] for l in self.log.read_text().splitlines() if l.startswith("api -X DELETE")]

    def test_events_become_lines_and_the_recorded_hook_is_deleted_on_stop(self):
        self.start(["pull_request.labeled", "pull_request.opened", "pull_request_review.submitted"])
        template = self.proc.stdout.readline().strip()
        self.assertTrue(template.startswith(pw.TEMPLATE_MARK), template)
        self.assertIn("{event}", json.loads(template[len(pw.TEMPLATE_MARK):]))
        first = self.proc.stdout.readline().strip()
        second = self.proc.stdout.readline().strip()
        self.assertEqual(first, "PR_EVENT acme/widgets#7 opened author=alice head=a1b2c3d4 merged=false reason=author")
        self.assertTrue(second.startswith("PR_EVENT acme/widgets#7 review_submitted by=bob"))
        hooks = self.state / "hooks.json"
        deadline = time.time() + 15
        while time.time() < deadline and json.loads(hooks.read_text() or "{}") != {"acme/widgets": [77]}:
            time.sleep(0.2)
        self.assertEqual(json.loads(hooks.read_text()), {"acme/widgets": [77]})
        self.stop()
        self.assertEqual(self.deletes(), ["repos/acme/widgets/hooks/77"])
        self.assertEqual(json.loads(hooks.read_text()), {})

    def test_a_hook_it_did_not_create_is_reported_and_never_deleted(self):
        self.start(["pull_request.opened"], preexisting="55\n")
        self.assertTrue(self.proc.stdout.readline().startswith(pw.TEMPLATE_MARK))
        note = self.proc.stdout.readline().strip()
        self.assertTrue(note.startswith("PR_WATCH_NOTE acme/widgets: another webhook forwarder"), note)
        self.stop()
        self.assertEqual(self.deletes(), [])
        self.assertNotIn("webhook forward", self.log.read_text())


FWD_GH = """#!/usr/bin/env python3
import os, sys, time
env = os.environ
with open(env["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:2] == ["extension", "list"]:
    print(env.get("FAKE_GH_EXT", "gh webhook  cli/gh-webhook  v0.2.0")); sys.exit(0)
if a[:3] == ["api", "-X", "DELETE"]:
    sys.exit(0)
if a[:1] == ["api"]:
    if env.get("FAKE_GH_LISTFAIL"):
        sys.stderr.write("HTTP 404\\n"); sys.exit(1)
    hf = env["FAKE_GH_HOOKFILE"]
    print(open(hf).read() if os.path.exists(hf) else ""); sys.exit(0)
if a[:2] == ["webhook", "forward"]:
    if env.get("FAKE_GH_CONFLICT"):
        sys.stderr.write("HTTP 422: Hook already exists on this repository\\n"); sys.exit(1)
    with open(env["FAKE_GH_HOOKFILE"], "a") as fh:
        fh.write("77\\n")
    for name in filter(None, env.get("FAKE_GH_EMIT", "").split(os.pathsep)):
        sys.stdout.write(open(name).read()); sys.stdout.flush()
    time.sleep(float(env.get("FAKE_GH_HOLD", "3")))
    sys.exit(0)
sys.exit(0)
"""


class InProcess(_Tmp):
    """The runner's methods and cmd_run in this process, against a fake gh on PATH."""

    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FWD_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "gh.log"
        self.hookfile = self.tmp / "hooks.txt"
        self.env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "FAKE_GH_LOG": str(self.log), "FAKE_GH_HOOKFILE": str(self.hookfile),
                    "FAKE_GH_EMIT": os.pathsep.join(str(FIXTURES / f"{n}.json") for n in
                                             ("pull_request.labeled", "pull_request.opened"))}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        self.st = pw.State(self.state)
        self.st.ensure()
        self.cli("add", "--repo", "acme/widgets", "--author", "alice")

    def calls(self, prefix):
        return [l for l in self.log.read_text().splitlines() if l.startswith(prefix)] if self.log.exists() else []

    def forward(self, runner=None):
        runner = runner or pw.Runner(self.st)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            delay = runner.forward_once("acme/widgets")
        return delay, out.getvalue().splitlines()

    def test_forward_once_emits_records_and_deletes_its_hook(self):
        delay, lines = self.forward()
        self.assertEqual(delay, pw.RESTART_DELAY_S)
        self.assertEqual(lines, ["PR_EVENT acme/widgets#7 opened author=alice head=a1b2c3d4 merged=false reason=author"])
        self.assertEqual(self.calls("api -X DELETE"), ["api -X DELETE repos/acme/widgets/hooks/77"])
        self.assertEqual(json.loads(self.st.hooks.read_text()), {})
        self.assertEqual(json.loads(self.st.forwarders.read_text()), {})

    def test_forward_once_upserts_the_row_of_each_emitted_event(self):
        self.forward()
        rows = json.loads(self.st.tracker.read_text())["rows"]
        self.assertEqual(list(rows), ["acme/widgets#7"], "the labeled event emits nothing and adds no row")
        self.assertEqual((rows["acme/widgets#7"]["state"], rows["acme/widgets#7"]["pr_head"]), ("open", "a1b2c3d4"))

    def test_forwarder_asks_for_review_events_too(self):
        self.forward()
        fwd = self.calls("webhook forward")
        self.assertEqual(len(fwd), 1)
        self.assertIn("--events=pull_request,pull_request_review", fwd[0].split())

    def test_forward_once_passes_the_review_lookup_to_the_filter(self):
        self.cli("add", "--repo", "acme/widgets", "--me", "owner-me", "--reviewed")
        cfg = json.loads(self.st.config.read_text())
        cfg["watches"][0]["authors"] = []
        self.st.config.write_text(json.dumps(cfg))
        os.environ["FAKE_GH_EMIT"] = str(FIXTURES / "pull_request.synchronize.json")
        runner = pw.Runner(self.st)
        with mock.patch.object(runner.reviewed, "fetch", return_value=True) as fetch:
            _, lines = self.forward(runner)
        self.assertEqual(lines, ["PR_EVENT acme/widgets#7 synchronize author=alice head=0badc0de "
                                 "merged=false reason=reviewed"])
        fetch.assert_called_once_with("acme/widgets", 7, ["owner-me"])

    def test_forward_once_backs_off_on_a_422(self):
        os.environ["FAKE_GH_CONFLICT"] = "1"
        delay, lines = self.forward()
        self.assertEqual(delay, pw.CONFLICT_DELAY_S)
        self.assertEqual(len(lines), 1)
        self.assertIn("refused a second forwarder", lines[0])
        self.assertIn(pw.HOOK_EXISTS, (self.state / "logs" / "acme_widgets.err").read_text())

    def test_forward_once_backs_off_when_hooks_cannot_be_listed(self):
        os.environ["FAKE_GH_LISTFAIL"] = "1"
        delay, lines = self.forward()
        self.assertEqual(delay, pw.CONFLICT_DELAY_S)
        self.assertEqual(lines, ["PR_WATCH_NOTE acme/widgets: cannot list hooks (need admin on the repo); retrying"])
        self.assertEqual(self.calls("webhook forward"), [])

    def test_cmd_run_reloads_on_sighup_and_cleans_up_on_sigterm(self):
        os.environ["FAKE_GH_HOLD"] = "30"
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in handlers.items()])
        pid = os.getpid()

        def drive():
            hooks = self.st.hooks
            deadline = time.time() + 20
            while time.time() < deadline and json.loads(hooks.read_text() if hooks.exists() else "{}") != {"acme/widgets": [77]}:
                time.sleep(0.2)
            cfg = json.loads(self.st.config.read_text())
            cfg["watches"] = []
            self.st.config.write_text(json.dumps(cfg))
            os.kill(pid, signal.SIGHUP)
            time.sleep(2.5)
            # A run.pid already gone at exit is tolerated.
            self.st.runner_pid.unlink()
            os.kill(pid, signal.SIGTERM)

        t = threading.Thread(target=drive)
        t.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pw.cmd_run(self.st)
        t.join()
        self.assertEqual(rc, 0)
        self.assertIn("PR_EVENT acme/widgets#7 opened", out.getvalue())
        templates = [l for l in out.getvalue().splitlines() if l.startswith(pw.TEMPLATE_MARK)]
        self.assertEqual(len(templates), 2, "one on start, one on SIGHUP")
        self.assertIn("api -X DELETE repos/acme/widgets/hooks/77", self.calls("api -X DELETE"))
        self.assertEqual(json.loads(self.st.hooks.read_text()), {})
        self.assertFalse(self.st.runner_pid.exists())

    def test_a_backoff_is_recorded_and_status_says_why(self):
        self.hookfile.write_text("55\n")
        runner = pw.Runner(self.st)
        before = time.time()
        delay, _ = self.forward(runner)
        self.assertEqual(delay, pw.CONFLICT_DELAY_S)
        b = json.loads(self.st.backoff.read_text())["acme/widgets"]
        self.assertEqual(b["reason"], "foreign forwarder hook 55")
        self.assertGreaterEqual(b["retry_at"], before + pw.CONFLICT_DELAY_S)
        with mock.patch.object(pw, "runner_pid", return_value=os.getpid()):
            _, out, _ = self.cli("status")
        at = time.strftime("%H:%M", time.localtime(b["retry_at"]))
        self.assertIn(f"forwarder acme/widgets: backing off: foreign forwarder hook 55, retry at {at}", out)
        with mock.patch.object(pw, "runner_pid", return_value=None):
            self.assertIn("forwarder acme/widgets: not running", self.cli("status")[1])
        self.hookfile.write_text("")
        self.forward(runner)
        self.assertEqual(json.loads(self.st.backoff.read_text()), {})

    def test_a_wake_cuts_a_backoff_short_and_a_plain_wait_does_not(self):
        runner = pw.Runner(self.st)
        gone = threading.Event()
        rounds = []

        def once(repo):
            rounds.append(time.time())
            if len(rounds) == 2:
                gone.set()
            return pw.CONFLICT_DELAY_S
        with mock.patch.object(runner, "forward_once", side_effect=once):
            t = threading.Thread(target=runner.loop, args=("acme/widgets", gone), daemon=True)
            t.start()
            time.sleep(1)
            self.assertEqual(len(rounds), 1, "still backing off without a wake")
            runner.wake()
            t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(rounds), 2)
        self.assertLess(rounds[1] - rounds[0], 5)

    def test_shutdown_wakes_a_backed_off_loop(self):
        runner = pw.Runner(self.st)
        with mock.patch.object(runner, "forward_once", return_value=pw.CONFLICT_DELAY_S):
            t = threading.Thread(target=runner.loop, args=("acme/widgets", threading.Event()), daemon=True)
            t.start()
            time.sleep(0.5)
            runner.shutdown()
            t.join(timeout=10)
        self.assertFalse(t.is_alive())

    def test_cmd_run_retries_a_backed_off_repo_on_sighup(self):
        os.environ["FAKE_GH_HOLD"] = "30"
        self.hookfile.write_text("55\n")
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in handlers.items()])
        pid = os.getpid()
        result = {}

        def drive():
            deadline = time.time() + 20
            while time.time() < deadline and "acme/widgets" not in (
                    json.loads(self.st.backoff.read_text()) if self.st.backoff.exists() else {}):
                time.sleep(0.2)
            self.hookfile.write_text("")
            sent = time.time()
            os.kill(pid, signal.SIGHUP)
            while time.time() < sent + 15 and not self.calls("webhook forward"):
                time.sleep(0.2)
            result["after"] = time.time() - sent
            os.kill(pid, signal.SIGTERM)

        t = threading.Thread(target=drive)
        t.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pw.cmd_run(self.st)
        t.join()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls("webhook forward")), 1)
        self.assertLess(result["after"], 15)
        self.assertEqual(json.loads(self.st.backoff.read_text()), {})

    def test_cmd_run_without_the_extension_exits_3(self):
        os.environ["FAKE_GH_EXT"] = "nothing installed"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pw.cmd_run(self.st), 3)
        self.assertIn("cli/gh-webhook extension is missing", out.getvalue())
        with mock.patch.object(pw.shutil, "which", return_value=None):
            self.assertFalse(pw.gh_webhook_installed())

    def test_cmd_run_exits_4_while_another_runner_holds_the_lock(self):
        import fcntl
        fh = open(self.st.lock, "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX)
        out = io.StringIO()
        with mock.patch.object(pw, "LOCK_WAIT_S", 1), contextlib.redirect_stdout(out):
            self.assertEqual(pw.cmd_run(self.st), 4)
        self.assertIn("another pr-watch runner is active", out.getvalue())
        self.assertEqual(self.calls("webhook forward"), [])

    def test_forward_once_waits_on_a_hook_it_did_not_create(self):
        self.hookfile.write_text("55\n")
        delay, lines = self.forward()
        self.assertEqual(delay, pw.CONFLICT_DELAY_S)
        self.assertEqual(lines, ["PR_WATCH_NOTE acme/widgets: another webhook forwarder already holds "
                                 "this repo (hook 55); GitHub allows one, retrying later"])
        self.assertEqual(self.calls("api -X DELETE"), [])
        self.assertEqual(self.calls("webhook forward"), [])

    def test_loop_forwards_until_the_repo_is_gone_then_drops_its_record(self):
        runner = pw.Runner(self.st)
        gone = threading.Event()
        pw.record_hooks(self.st, "acme/widgets", [91])
        rounds = []

        def once(repo):
            rounds.append(repo)
            if len(rounds) == 2:
                gone.set()
            return 0
        with mock.patch.object(runner, "forward_once", side_effect=once):
            runner.loop("acme/widgets", gone)
        self.assertEqual(rounds, ["acme/widgets", "acme/widgets"])
        self.assertEqual(self.calls("api -X DELETE"), ["api -X DELETE repos/acme/widgets/hooks/91"])

    def test_stderr_reader_logs_and_flags_the_422(self):
        runner = pw.Runner(self.st)
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stderr.write('warn\\nHTTP 422: Hook already exists\\n')"],
                                stderr=subprocess.PIPE, text=True)
        flag = threading.Event()
        path = self.state / "logs" / "x.err"
        runner._stderr(proc, path, flag)
        proc.wait()
        self.assertTrue(flag.is_set())
        self.assertEqual(path.read_text(), "warn\nHTTP 422: Hook already exists\n")

    def test_record_keeps_only_the_new_forwarder_hook(self):
        runner = pw.Runner(self.st)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.hookfile.write_text("55\n77\n")
        with mock.patch.object(pw.time, "sleep"):
            runner._record("acme/widgets", [55], proc)
        self.assertEqual(json.loads(self.st.hooks.read_text()), {"acme/widgets": [77]})

    def test_shutdown_stops_live_forwarders_and_kills_one_that_ignores_sigterm(self):
        runner = pw.Runner(self.st)
        polite = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        stubborn = subprocess.Popen([sys.executable, "-c", "import signal, sys, time\n"
                                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                     "print('ready', flush=True)\ntime.sleep(60)"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(stubborn.stdout.readline().strip(), "ready")
        self.addCleanup(stubborn.stdout.close)
        runner.procs = {"acme/a": polite, "acme/b": stubborn}
        pw.record_hooks(self.st, "acme/a", [5])
        runner.shutdown()
        self.assertTrue(runner.stop.is_set())
        self.assertEqual(polite.returncode, -signal.SIGTERM)
        self.assertEqual(stubborn.wait(timeout=5), -signal.SIGKILL)
        self.assertEqual(json.loads(self.st.hooks.read_text()), {})
        self.assertEqual(json.loads(self.st.forwarders.read_text()), {})

    def test_main_dispatches_run(self):
        with mock.patch.object(pw, "cmd_run", return_value=0) as run:
            self.assertEqual(self.cli("run")[0], 0)
        run.assert_called_once()


class Helpers(_Tmp):
    def sleeper(self, *extra):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"] + list(extra))
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p

    def test_unreadable_and_non_object_config_are_refused(self):
        self.state.mkdir(parents=True)
        (self.state / "config.json").write_text("{not json")
        rc, _, err = self.cli("list")
        self.assertEqual(rc, 2)
        self.assertIn("cannot read", err)
        (self.state / "config.json").write_text("[]")
        rc, _, err = self.cli("list")
        self.assertIn("is not a JSON object", err)

    def test_hook_listing_failures_answer_none(self):
        with mock.patch.object(pw, "gh", return_value=subprocess.CompletedProcess([], 1, "", "x")):
            self.assertIsNone(pw.forwarder_hook_ids("acme/widgets"))
        with mock.patch.object(pw, "gh", return_value=subprocess.CompletedProcess([], 0, "12 abc", "")):
            self.assertIsNone(pw.forwarder_hook_ids("acme/widgets"))

    def test_state_copy_dir_is_its_own_state_dir(self):
        fake = self.tmp / "ws" / "state" / "pr-watch" / "pr_watch.py"
        with mock.patch.object(pw, "__file__", str(fake)):
            self.assertEqual(pw.resolve_state_dir(), fake.parent.resolve())

    def test_process_helpers_and_stale_forwarders(self):
        fwd = self.sleeper("webhook", "forward")
        other = self.sleeper("unrelated")
        self.assertTrue(pw.pid_alive(fwd.pid))
        self.assertTrue(pw.is_forwarder(fwd.pid))
        self.assertFalse(pw.is_forwarder(other.pid))
        st = pw.State(self.state)
        st.ensure()
        pw.write_json(st.forwarders, {"acme/widgets": fwd.pid, "acme/other": other.pid})
        pw.kill_stale_forwarders(st)
        self.assertEqual(fwd.wait(timeout=10), -signal.SIGTERM)
        self.assertIsNone(other.poll())
        self.assertEqual(json.loads(st.forwarders.read_text()), {})
        self.assertFalse(pw.pid_alive(fwd.pid))

    def test_pid_alive_counts_a_process_it_may_not_signal(self):
        with mock.patch.object(pw.os, "kill", side_effect=PermissionError):
            self.assertTrue(pw.pid_alive(4242))

    def test_stale_forwarder_already_gone_is_skipped(self):
        st = pw.State(self.state)
        st.ensure()
        pw.write_json(st.forwarders, {"acme/widgets": 4242})
        with mock.patch.object(pw, "pid_alive", return_value=True), \
                mock.patch.object(pw, "is_forwarder", return_value=True), \
                mock.patch.object(pw.os, "kill", side_effect=ProcessLookupError) as kill:
            pw.kill_stale_forwarders(st)
        kill.assert_called_once_with(4242, signal.SIGTERM)
        self.assertEqual(json.loads(st.forwarders.read_text()), {})

    def test_runner_pid_needs_a_live_pr_watch_process(self):
        st = pw.State(self.state)
        st.ensure()
        self.assertIsNone(pw.runner_pid(st))
        p = self.sleeper()
        st.runner_pid.write_text(f"{p.pid}\n")
        self.assertIsNone(pw.runner_pid(st))
        with mock.patch.object(pw, "pid_command", return_value="python3 pr_watch.py run"):
            self.assertEqual(pw.runner_pid(st), p.pid)

    def test_add_signals_a_live_runner(self):
        p = self.sleeper()
        with mock.patch.object(pw, "runner_pid", return_value=p.pid):
            self.assertEqual(self.cli("add", "--repo", "acme/widgets", "--author", "alice")[0], 0)
        self.assertEqual(p.wait(timeout=10), -signal.SIGHUP)

    def test_status_lists_live_forwarders_and_recorded_ids(self):
        self.cli("add", "--repo", "acme/widgets", "--author", "alice")
        st = pw.State(self.state)
        fwd = self.sleeper("webhook", "forward")
        pw.write_json(st.forwarders, {"acme/widgets": fwd.pid})
        pw.write_json(st.hooks, {"acme/widgets": [11, 12]})
        rc, out, _ = self.cli("status")
        self.assertIn(f"forwarder acme/widgets: pid {fwd.pid}", out)
        self.assertIn("recorded hook ids acme/widgets: 11 12", out)

    def test_cleanup_reports_ids_it_could_not_delete(self):
        st = pw.State(self.state)
        st.ensure()
        pw.write_json(st.hooks, {"acme/widgets": [11]})
        with mock.patch.object(pw, "delete_hook", return_value=False):
            rc, out, _ = self.cli("cleanup")
        self.assertEqual(rc, 1)
        self.assertIn('could not delete: {"acme/widgets": [11]}', out)


class Settings(_Tmp):
    """CLI > env > manifest.json config > config.json, with an empty value at any layer unset."""

    def manifest(self, **config):
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps({"name": "pr-watch", "config": config}))
        patch = mock.patch.object(pw, "MANIFEST", path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_each_layer_beats_the_ones_below_it(self):
        st = pw.State(self.state)
        self.manifest(PR_WATCH_TRACKER_TITLE="")
        self.assertEqual(pw.setting_source("PR_WATCH_TRACKER_TITLE", st=st), ("", "unset"))
        self.cli("config", "set", "PR_WATCH_TRACKER_TITLE", "from-config")
        self.assertEqual(pw.setting_source("PR_WATCH_TRACKER_TITLE", st=st), ("from-config", "config.json"))
        self.manifest(PR_WATCH_TRACKER_TITLE="from-manifest")
        self.assertEqual(pw.setting_source("PR_WATCH_TRACKER_TITLE", st=st), ("from-manifest", "manifest"))
        os.environ["PR_WATCH_TRACKER_TITLE"] = "from-env"
        self.assertEqual(pw.setting_source("PR_WATCH_TRACKER_TITLE", st=st), ("from-env", "env"))
        self.assertEqual(pw.setting("PR_WATCH_TRACKER_TITLE", "from-cli", st), "from-cli")
        os.environ["PR_WATCH_TRACKER_TITLE"] = ""
        self.assertEqual(pw.setting("PR_WATCH_TRACKER_TITLE", st=st), "from-manifest")

    def test_the_shipped_manifest_declares_every_setting(self):
        cfg = json.loads(pw.MANIFEST.read_text())["config"]
        self.assertEqual(sorted(cfg), sorted(pw.SETTINGS))
        self.assertIn("{event}", cfg["PR_WATCH_PROMPT"])
        self.assertIn("{track}", cfg["PR_WATCH_PROMPT"])
        self.assertEqual(cfg["PR_WATCH_TRACKER_ROOM"], "")
        self.assertEqual(pw.commons_script(pw.State(self.state)),
                         REPO / "skills" / "room-commons" / "scripts" / "room_commons.py")

    def test_an_unknown_setting_is_refused(self):
        with self.assertRaises(SystemExit):
            pw.setting("PR_WATCH_NOPE")
        self.assertEqual(self.cli("config", "set", "PR_WATCH_NOPE", "x")[0], 2)

    def test_config_set_and_get(self):
        rc, out, _ = self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        self.assertEqual((rc, out.strip()), (0, 'PR_WATCH_TRACKER_ROOM = "!room:server"'))
        self.assertEqual(self.cli("config", "get", "PR_WATCH_TRACKER_ROOM")[1].strip(), "!room:server")
        every = json.loads(self.cli("config", "get")[1])
        self.assertEqual(every["PR_WATCH_TRACKER_ROOM"], {"value": "!room:server", "from": "config.json"})
        self.assertEqual(every["PR_WATCH_TRACKER_TITLE"]["from"], "manifest")
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "")
        self.assertEqual(self.cli("config", "get", "PR_WATCH_TRACKER_ROOM")[1].strip(), "")
        self.assertEqual(json.loads((self.state / "config.json").read_text())["settings"], {})

    def test_config_set_says_when_a_higher_layer_still_wins(self):
        os.environ["PR_WATCH_TRACKER_ROOM"] = "!env:server"
        rc, out, _ = self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        self.assertEqual(rc, 0)
        self.assertIn("the env value still wins", out)

    def test_config_set_signals_a_live_runner(self):
        with mock.patch.object(pw, "notify_runner") as notify:
            self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        notify.assert_called_once()


class TemplateLine(_Tmp):
    def emitted(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pw.Runner(pw.State(self.state)).emit_template()
        line = out.getvalue()
        self.assertTrue(line.startswith(pw.TEMPLATE_MARK) and line.endswith("\n") and line.count("\n") == 1, line)
        return json.loads(line[len(pw.TEMPLATE_MARK):])

    def test_the_runner_emits_the_resolved_template_as_one_json_line(self):
        t = self.emitted()
        self.assertTrue(t.startswith("[pr-watch] {event}\n"))
        self.assertNotIn("{track}", t)
        self.assertNotIn("track --pr", t)

    def test_the_tracker_hint_appears_only_with_a_room(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        t = self.emitted()
        self.assertIn(pw.TRACK_HINT, t)
        self.assertNotIn("{track}", t)
        for word in (pw.PUBLISH_MARK.strip(), "room.commons.pages.replace", "whole",
                     "room.commons.pages.create", "tracker-kind", "--ask"):
            self.assertIn(word, t)

    def test_env_overrides_the_template(self):
        os.environ["PR_WATCH_PROMPT"] = "custom {event}{track}"
        self.assertEqual(self.emitted(), "custom {event}")


FAKE_COMMONS = r"""
import json, os, sys
store_path, log = os.environ["FAKE_COMMONS_STORE"], os.environ["FAKE_COMMONS_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
store = json.load(open(store_path)) if os.path.exists(store_path) else {"pages": {}, "n": 0}
a, kind = sys.argv[1:], "html"
while a[0].startswith("--"):
    if a[0] == "--kind":
        kind = a[1]; a = a[2:]
    else:
        a = a[1:]
verb, a = a[0], a[1:]
if verb == os.environ.get("FAKE_COMMONS_FAIL"):
    sys.stderr.write("boom: no credential\n"); sys.exit(2)
def save():
    json.dump(store, open(store_path, "w"))
if verb == "pages":
    print(json.dumps([{"id": None, "kind": "html", "title": "Main"}] +
                     [{"id": k[5:], "kind": k, "title": v["title"]} for k, v in store["pages"].items()]))
elif verb == "page-add":
    assert a[:2] == ["--kind", "html"], a
    store["n"] += 1
    k = "html-p%d" % store["n"]
    store["pages"][k] = {"title": a[3], "text": ""}
    save(); print(json.dumps({"ok": True, "id": k[5:], "kind": k, "title": a[3], "folder": None}))
elif verb == "read":
    print(json.dumps({"chars": 0, "peers": [], "text": store["pages"][kind]["text"]}))
elif verb == "replace":
    assert a[0] == "--", a
    room, old, new = a[1:]
    text = store["pages"][kind]["text"]
    if old not in text:
        sys.stderr.write("text to replace is not in the document\n"); sys.exit(1)
    store["pages"][kind]["text"] = text.replace(old, new, 1)
    save(); print(json.dumps({"ok": True}))
"""


class Tracker(_Tmp):
    def setUp(self):
        super().setUp()
        self.script = self.tmp / "fake_commons.py"
        self.script.write_text(FAKE_COMMONS)
        self.store = self.tmp / "store.json"
        self.log = self.tmp / "commons.log"
        os.environ.update({"FAKE_COMMONS_STORE": str(self.store), "FAKE_COMMONS_LOG": str(self.log),
                           "PR_WATCH_COMMONS_SCRIPT": str(self.script)})
        self.ticks = iter(f"2026-01-01T00:00:{i:02d}Z" for i in range(60))
        patch = mock.patch.object(pw, "_now", side_effect=lambda: next(self.ticks))
        patch.start()
        self.addCleanup(patch.stop)

    def track(self, *argv):
        return self.cli("track", *argv)

    def data(self):
        return json.loads((self.state / "tracker.json").read_text())

    def verbs(self):
        if not self.log.exists():
            return []
        return [next(a for a in json.loads(l) if not a.startswith("-") and not a.startswith("html"))
                for l in self.log.read_text().splitlines()]

    def page(self, kind):
        return json.loads(self.store.read_text())["pages"][kind]["text"]

    def test_unset_room_only_records(self):
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--author", "alice", "--state", "open")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "recorded acme/widgets#7; no tracker room set (PR_WATCH_TRACKER_ROOM), "
                                      "so nothing was published")
        self.assertEqual(self.data()["rows"]["acme/widgets#7"]["author"], "alice")
        self.assertEqual(self.verbs(), [])

    def test_upsert_keeps_earlier_fields_and_stamps_updated_at(self):
        self.track("--pr", "acme/widgets#7", "--author", "alice", "--state", "open", "--head", "a1b2c3d4")
        self.track("--pr", "acme/widgets#8", "--author", "bob", "--state", "merged")
        self.track("--pr", "acme/widgets#7", "--verdict", "approved", "--event", "re-reviewed")
        rows = self.data()["rows"]
        self.assertEqual(sorted(rows), ["acme/widgets#7", "acme/widgets#8"])
        self.assertEqual(rows["acme/widgets#7"], {
            "repo": "acme/widgets", "number": 7, "link": "https://github.com/acme/widgets/pull/7",
            "author": "alice", "state": "open", "head": "a1b2c3d4", "verdict": "approved",
            "event": "re-reviewed", "updated_at": "2026-01-01T00:00:02Z"})
        self.assertEqual(rows["acme/widgets#8"]["updated_at"], "2026-01-01T00:00:01Z")

    def test_order_is_open_first_then_newest(self):
        rows = [{"repo": "a/b", "number": 1, "state": "merged", "updated_at": "2026-01-03T00:00:00Z"},
                {"repo": "a/b", "number": 2, "state": "open", "updated_at": "2026-01-01T00:00:00Z"},
                {"repo": "a/b", "number": 3, "state": "open", "updated_at": "2026-01-02T00:00:00Z"},
                {"repo": "a/b", "number": 4, "state": "closed", "updated_at": "2026-01-04T00:00:00Z"}]
        self.assertEqual([r["number"] for r in pw.tracker_order(rows)], [3, 2, 4, 1])

    def test_bad_input_is_refused(self):
        self.assertNotEqual(self.track("--pr", "acme/widgets")[0], 0)
        self.assertNotEqual(self.track("--pr", "acme/widgets#7", "--state", "draft")[0], 0)
        self.assertNotEqual(self.track("--pr", "acme/widgets#7", "--author", "<x>")[0], 0)
        self.assertFalse((self.state / "tracker.json").exists())

    def test_render_escapes_every_value_and_links_only_http(self):
        rows = [{"repo": "acme/widgets", "number": 7, "author": "<script>alert(1)</script>", "state": "open",
                 "head": "a1b2", "verdict": "approved", "event": "pushed <script>x()</script> & more",
                 "link": "javascript:alert(1)", "updated_at": "2026-01-01T00:00:00Z"},
                {"repo": "acme/widgets", "number": 8, "state": "merged", "link": "https://github.com/acme/widgets/pull/8",
                 "updated_at": "2026-01-01T00:00:00Z"}]
        out = pw.render_tracker(rows, "Title <b>", "2026-01-02T00:00:00Z")
        self.assertEqual(out.count("<script>"), 1, "only the page's own sort script")
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertIn("pushed &lt;script&gt;x()&lt;/script&gt; &amp; more", out)
        self.assertNotIn("javascript:", out)
        self.assertIn("<td>acme/widgets#7</td>", out)
        self.assertIn('<a href="https://github.com/acme/widgets/pull/8" rel="noopener noreferrer">acme/widgets#8</a>', out)
        self.assertIn("@media (prefers-color-scheme: dark)", out)
        self.assertIn('name="viewport"', out)
        self.assertIn("Generated by pr-watch; edits here are overwritten.", out)
        self.assertLess(out.index("acme/widgets#7"), out.index("acme/widgets#8"))

    def test_every_column_header_is_a_sort_button(self):
        out = pw.render_tracker([{"repo": "a/b", "number": 1}], "PR watch", "now")
        for col in pw.COLUMNS:
            self.assertIn(f'<th><button type="button">{col}</button></th>', out)
        self.assertIn("aria-sort", out)
        self.assertLess(out.index("</table>"), out.index("<script>"))

    def test_render_with_no_rows_says_so(self):
        self.assertIn("No pull requests tracked yet.", pw.render_tracker([], "PR watch", "now"))

    def test_first_publish_creates_the_page_then_later_ones_replace_it(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--author", "alice", "--state", "open")
        self.assertEqual(rc, 0, out)
        self.assertIn("tracker published to !room:server --kind html-p1", out)
        self.assertEqual(self.verbs(), ["page-add", "read", "replace"])
        self.assertEqual(json.loads(self.log.read_text().splitlines()[0]),
                         ["--json", "page-add", "--kind", "html", "!room:server", "PR watch"])
        self.assertEqual((self.data()["page_kind"], self.data()["page_room"]), ("html-p1", "!room:server"))
        self.assertIn("acme/widgets#7", self.page("html-p1"))
        self.log.unlink()
        self.track("--pr", "acme/widgets#8", "--author", "bob", "--state", "merged")
        self.assertEqual(self.verbs(), ["pages", "read", "replace"])
        text = self.page("html-p1")
        self.assertEqual(text.count("<!doctype html>"), 1)
        self.assertIn("acme/widgets#7", text)
        self.assertIn("acme/widgets#8", text)

    def test_a_removed_page_is_created_again(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        self.track("--pr", "acme/widgets#7", "--state", "open")
        store = json.loads(self.store.read_text())
        del store["pages"]["html-p1"]
        self.store.write_text(json.dumps(store))
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--state", "merged")
        self.assertIn("--kind html-p2", out)
        self.assertEqual(self.data()["page_kind"], "html-p2")

    def test_a_new_room_gets_its_own_page(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        self.track("--pr", "acme/widgets#7", "--state", "open")
        self.log.unlink()
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--room", "!other:server")
        self.assertEqual(self.verbs(), ["page-add", "read", "replace"])
        self.assertEqual(self.data()["page_room"], "!other:server")

    def test_a_failed_publish_keeps_the_row_and_exits_zero(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        os.environ["FAKE_COMMONS_FAIL"] = "page-add"
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--author", "alice", "--state", "open")
        self.assertEqual(rc, 0)
        note, line = out.strip().splitlines()
        self.assertEqual(note, "PR_WATCH_NOTE tracker publish failed: page-add exited 2: boom: no credential; "
                               "recorded acme/widgets#7, publish it with your room tools")
        self.assertTrue(line.startswith("PR_WATCH_PUBLISH room=!room:server kind=new "), line)
        self.assertIn("acme/widgets#7", self.data()["rows"])
        self.assertNotIn("page_kind", self.data())
        os.environ.pop("FAKE_COMMONS_FAIL")
        self.track("--pr", "acme/widgets#8", "--state", "open")
        self.assertIn("acme/widgets#7", self.page("html-p1"))

    def handed_back(self, out):
        line = out.strip().splitlines()[-1]
        self.assertTrue(line.startswith(pw.PUBLISH_MARK), line)
        room, kind, rest = line[len(pw.PUBLISH_MARK):].split(" ", 2)
        path_part, title_part = rest.split(" title=")
        self.assertTrue(path_part.startswith("path="))
        return {"room": room[len("room="):], "kind": kind[len("kind="):],
                "path": Path(json.loads(path_part[len("path="):])), "title": json.loads(title_part)}

    def test_a_failed_publish_hands_the_page_back_to_the_session(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        os.environ["PR_WATCH_TRACKER_TITLE"] = "My PRs"
        os.environ["FAKE_COMMONS_FAIL"] = "page-add"
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--author", "alice", "--state", "open")
        self.assertEqual(rc, 0)
        back = self.handed_back(out)
        self.assertEqual(back, {"room": "!room:server", "kind": "new", "path": (self.state / "tracker.html").resolve(),
                                "title": "My PRs"})
        page = back["path"].read_text()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("acme/widgets#7", page)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.endswith(".tmp")], [])
        rc, out, _ = self.cli("tracker-kind", "--kind", "html-ab12cd34", "--room", "!room:server")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.data()["page_kind"], self.data()["page_room"]), ("html-ab12cd34", "!room:server"))
        self.assertIn("acme/widgets#7", self.data()["rows"])
        os.environ["FAKE_COMMONS_FAIL"] = "pages"
        rc, out, _ = self.track("--pr", "acme/widgets#8", "--state", "open")
        self.assertEqual(self.handed_back(out)["kind"], "html-ab12cd34")
        self.assertIn("acme/widgets#8", (self.state / "tracker.html").read_text())

    def test_session_mode_never_runs_the_cli(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        rc, out, _ = self.cli("config", "set", "PR_WATCH_TRACKER_PUBLISH", "session")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--state", "open")
        self.assertEqual(rc, 0)
        self.assertEqual(self.verbs(), [])
        self.assertEqual(out.strip().splitlines()[0],
                         "recorded acme/widgets#7; publish the tracker page with your room tools (1 PRs)")
        self.assertEqual(self.handed_back(out)["kind"], "new")
        self.cli("tracker-kind", "--kind", "html-p9", "--room", "!room:server")
        rc, out, _ = self.track("--pr", "acme/widgets#7", "--room", "!other:server")
        self.assertEqual(self.handed_back(out)["kind"], "new", "a page kind belongs to its room")
        rc, out, _ = self.track("--pr", "acme/widgets#7")
        self.assertEqual(self.handed_back(out)["kind"], "html-p9")
        self.assertEqual(self.verbs(), [])

    def test_a_bad_publish_mode_is_refused(self):
        self.assertNotEqual(self.cli("config", "set", "PR_WATCH_TRACKER_PUBLISH", "nope")[0], 0)
        os.environ["PR_WATCH_TRACKER_PUBLISH"] = "nope"
        self.assertNotEqual(self.track("--pr", "acme/widgets#7")[0], 0)
        self.assertFalse((self.state / "tracker.json").exists())

    def test_tracker_kind_refuses_what_is_not_an_html_page_kind(self):
        for kind in ("markdown-ab12cd34", "html", "html-a b", "db:d1"):
            self.assertNotEqual(self.cli("tracker-kind", "--kind", kind, "--room", "!room:server")[0], 0, kind)
        self.assertNotEqual(self.cli("tracker-kind", "--kind", "html-p1", "--room", "!a b")[0], 0)
        self.assertFalse((self.state / "tracker.json").exists())

    def test_a_missing_commons_script_hands_the_page_back(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        os.environ["PR_WATCH_COMMONS_SCRIPT"] = str(self.tmp / "nope.py")
        rc, out, _ = self.track("--pr", "acme/widgets#7")
        self.assertEqual(self.handed_back(out)["kind"], "new")

    def test_the_runner_upsert_keeps_the_agents_verdict_and_event(self):
        st = pw.State(self.state)
        self.track("--pr", "acme/widgets#7", "--author", "alice", "--state", "open", "--head", "11111111",
                   "--verdict", "approved", "--event", "reviewed at 11111111")
        self.assertTrue(pw.upsert_from_payload(st, fixture("pull_request.closed_merged")))
        row = self.data()["rows"]["acme/widgets#7"]
        self.assertEqual(row, {
            "repo": "acme/widgets", "number": 7, "link": "https://github.com/acme/widgets/pull/7",
            "author": "alice", "state": "merged", "head": "11111111", "pr_head": "0badc0de",
            "verdict": "approved", "event": "reviewed at 11111111", "title": "x",
            "updated_at": "2026-01-01T00:00:01Z"})

    def test_a_push_past_the_reviewed_head_shows_both_heads_never_moves_the_verdicts_head(self):
        row = {"repo": "a/b", "number": 1, "head": "11111111", "pr_head": "0badc0de", "verdict": "approved"}
        out = pw.render_tracker([row], "PR watch", "now")
        self.assertIn('<code>11111111</code> <span class="stale">now <code>0badc0de</code></span>', out)
        unreviewed = pw.render_tracker([{"repo": "a/b", "number": 2, "pr_head": "0badc0de"}], "PR watch", "now")
        self.assertIn('<span class="stale">not reviewed: <code>0badc0de</code></span>', unreviewed)
        same = pw.render_tracker([{"repo": "a/b", "number": 3, "head": "0badc0de", "pr_head": "0badc0de"}], "t", "now")
        self.assertNotIn('class="stale"', same)

    def test_the_runner_upsert_creates_a_row_and_never_a_verdict(self):
        st = pw.State(self.state)
        payload = fixture("pull_request_review.submitted")
        payload["pull_request"]["html_url"] = "https://github.com/acme/widgets/pull/7"
        self.assertTrue(pw.upsert_from_payload(st, payload))
        row = self.data()["rows"]["acme/widgets#7"]
        self.assertEqual(row["state"], "open")
        self.assertNotIn("verdict", row)
        self.assertNotIn("event", row)
        bad = fixture("pull_request.opened")
        bad["pull_request"]["html_url"] = "javascript:alert(1)"
        bad["pull_request"]["user"]["login"] = "<x>"
        bad["pull_request"]["number"] = 9
        pw.upsert_from_payload(st, bad)
        row = self.data()["rows"]["acme/widgets#9"]
        self.assertEqual(row["link"], "https://github.com/acme/widgets/pull/9")
        self.assertNotIn("author", row)
        self.assertFalse(pw.upsert_from_payload(st, {"repository": {"full_name": "acme/widgets"}}))

    def test_concurrent_writers_lose_no_row(self):
        st = pw.State(self.state)
        payloads = []
        for n in range(1, 21):
            p = fixture("pull_request.opened")
            p["pull_request"]["number"] = n
            payloads.append(p)
        patch = mock.patch.object(pw, "_now", return_value="2026-01-01T00:00:00Z")
        patch.start()
        self.addCleanup(patch.stop)
        threads = [threading.Thread(target=pw.upsert_from_payload, args=(st, p)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.data()["rows"]), 20)

    def test_about_comes_from_the_title_and_track_overrides_it(self):
        st = pw.State(self.state)
        payload = fixture("pull_request.opened")
        payload["pull_request"]["title"] = "Add  <b>widgets</b>\n& more"
        pw.upsert_from_payload(st, payload)
        row = self.data()["rows"]["acme/widgets#7"]
        self.assertEqual(row["title"], "Add <b>widgets</b> & more")
        self.assertEqual(pw._about(row), "Add <b>widgets</b> & more")
        self.track("--pr", "acme/widgets#7", "--about", "Widget support")
        pw.upsert_from_payload(st, payload)
        row = self.data()["rows"]["acme/widgets#7"]
        self.assertEqual(pw._about(row), "Widget support", "a later event keeps the agent's --about")
        self.assertNotEqual(self.track("--pr", "acme/widgets#7", "--about", "x" * (pw.ABOUT_MAX + 1))[0], 0)

    def test_the_about_column_is_escaped_and_truncated_with_the_full_text_in_title(self):
        long = "<i>" + "a" * 120 + '"x"'
        out = pw.render_tracker([{"repo": "a/b", "number": 1, "title": long}], "PR watch", "now")
        self.assertNotIn("<i>", out)
        self.assertIn(f'title="{pw._esc(long)}"', out)
        shown = out.split(f'title="{pw._esc(long)}">')[1].split("</span>")[0]
        self.assertTrue(shown.endswith("\u2026"), shown)
        self.assertLessEqual(len(shown), pw.ABOUT_SHOWN + 10)
        self.assertLess(out.index("<button type=\"button\">PR</button>"), out.index(">About</button>"))
        self.assertLess(out.index(">About</button>"), out.index(">Author</button>"))

    def test_needs_you_panel_shows_each_ask_above_the_table_escaped(self):
        rows = [{"repo": "a/b", "number": 1, "state": "open", "about": "Fix <x>",
                 "link": "https://github.com/a/b/pull/1",
                 "ask": "Merge now? I recommend yes.\nOptions: <script>bad()</script> merge / wait"},
                {"repo": "a/b", "number": 2, "state": "open"}]
        out = pw.render_tracker(rows, "PR watch", "now")
        self.assertEqual(out.count("<script>"), 1, "only the page's own sort script")
        self.assertIn("Needs you (1)", out)
        panel = out[out.index('<section class="needs"'):out.index("</section>")]
        self.assertLess(out.index("</section>"), out.index("<table>"))
        self.assertIn('<a href="https://github.com/a/b/pull/1" rel="noopener noreferrer">a/b#1</a>', panel)
        self.assertIn("Fix &lt;x&gt;", panel)
        self.assertIn("Merge now? I recommend yes.\nOptions: &lt;script&gt;bad()&lt;/script&gt; merge / wait", panel)
        self.assertNotIn("a/b#2", panel)
        self.assertEqual(out.count('<tr class="asks">'), 1)
        self.assertEqual(out.count('<span class="badge">needs you</span>'), 1)
        self.assertNotIn('class="needs"', pw.render_tracker(rows[1:], "PR watch", "now"))

    def test_an_ask_is_set_cleared_and_bounded(self):
        self.track("--pr", "acme/widgets#7", "--state", "open", "--ask", "Merge? Recommend yes.")
        self.assertEqual(self.data()["rows"]["acme/widgets#7"]["ask"], "Merge? Recommend yes.")
        self.track("--pr", "acme/widgets#7", "--ask-clear")
        self.assertNotIn("ask", self.data()["rows"]["acme/widgets#7"])
        self.assertNotEqual(self.track("--pr", "acme/widgets#7", "--ask", "x" * (pw.ASK_MAX + 1))[0], 0)
        self.assertNotEqual(self.track("--pr", "acme/widgets#7", "--ask", "  ")[0], 0)
        self.assertNotEqual(self.track("--pr", "acme/widgets#7", "--ask", "a", "--ask-clear")[0], 0)

    def test_an_ask_clears_when_the_pr_merges_or_closes(self):
        st = pw.State(self.state)
        self.track("--pr", "acme/widgets#7", "--state", "open", "--ask", "Merge?")
        pw.upsert_from_payload(st, fixture("pull_request.synchronize"))
        self.assertIn("ask", self.data()["rows"]["acme/widgets#7"], "an open PR keeps its ask")
        pw.upsert_from_payload(st, fixture("pull_request.closed_merged"))
        self.assertNotIn("ask", self.data()["rows"]["acme/widgets#7"])
        self.track("--pr", "acme/widgets#8", "--state", "open", "--ask", "Close it?")
        self.track("--pr", "acme/widgets#8", "--state", "closed")
        self.assertNotIn("ask", self.data()["rows"]["acme/widgets#8"])

    def test_a_publish_that_times_out_is_a_note(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        with mock.patch.object(pw.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 60)):
            rc, out, _ = self.track("--pr", "acme/widgets#7")
        self.assertEqual(rc, 0)
        self.assertIn("PR_WATCH_NOTE tracker publish failed: page-add timed out after 60s", out)

    def test_a_missing_commons_script_is_noted_and_skipped(self):
        self.cli("config", "set", "PR_WATCH_TRACKER_ROOM", "!room:server")
        os.environ["PR_WATCH_COMMONS_SCRIPT"] = str(self.tmp / "nope.py")
        rc, out, _ = self.track("--pr", "acme/widgets#7")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("PR_WATCH_NOTE tracker publish skipped: no room-commons script"), out)
        self.assertIn("acme/widgets#7", self.data()["rows"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
