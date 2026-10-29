#!/usr/bin/env python3
"""skills/pr-watch/scripts/pr_watch.py: the watch-config CLI, the event filter over fixture
payloads for each forwarded event type, the JSON stream splitter, the hook-id record (only
recorded ids are ever deleted, through a fake `gh` on PATH) and install-mod's file placement."""
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
        self.assertIn("{event}", cfg["prompt"])
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

    def test_add_keeps_a_custom_prompt(self):
        self.state.mkdir(parents=True)
        (self.state / "config.json").write_text(json.dumps({"prompt": "custom {event}"}))
        self.cli("add", "--repo", "acme/widgets", "--author", "alice")
        self.assertEqual(json.loads((self.state / "config.json").read_text())["prompt"], "custom {event}")

    def test_bad_input_is_refused(self):
        self.assertNotEqual(self.cli("add", "--repo", "widgets", "--author", "a")[0], 0)
        self.assertNotEqual(self.cli("add", "--repo", "acme/widgets")[0], 0)
        self.assertNotEqual(self.cli("add", "--repo", "acme/widgets", "--author", "bad login")[0], 0)
        self.assertFalse((self.state / "config.json").exists())

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
                         "PR_EVENT acme/widgets#7 opened author=alice head=a1b2c3d4 merged=false")
        self.assertEqual(self.line("pull_request.synchronize"),
                         "PR_EVENT acme/widgets#7 synchronize author=alice head=0badc0de merged=false")
        self.assertIn(" reopened ", self.line("pull_request.reopened"))
        self.assertIn(" ready_for_review ", self.line("pull_request.ready_for_review"))
        self.assertEqual(self.line("pull_request.closed_merged"),
                         "PR_EVENT acme/widgets#7 closed author=alice head=0badc0de merged=true")
        self.assertTrue(self.line("pull_request.closed_unmerged").endswith("merged=false"))

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
    for name in os.environ["FAKE_GH_EMIT"].split():
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
               "FAKE_GH_EMIT": " ".join(str(FIXTURES / f"{n}.json") for n in emit)}
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
        first = self.proc.stdout.readline().strip()
        second = self.proc.stdout.readline().strip()
        self.assertEqual(first, "PR_EVENT acme/widgets#7 opened author=alice head=a1b2c3d4 merged=false")
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
        note = self.proc.stdout.readline().strip()
        self.assertTrue(note.startswith("PR_WATCH_NOTE acme/widgets: another webhook forwarder"), note)
        self.stop()
        self.assertEqual(self.deletes(), [])
        self.assertNotIn("webhook forward", self.log.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
