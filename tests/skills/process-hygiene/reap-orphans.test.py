#!/usr/bin/env python3
"""skills/process-hygiene/scripts/reap_orphans.py: what is a candidate, and how one is stopped.

Two layers. The rule tests drive classify/plan/stop over fabricated process
tables, a stub cwd reader and recording signal functions, so every rule and
both platforms' cwd readers are exercised on any host. The real-process tests
start actual orphans under a fake scratchpad root (a temp dir passed as
--tmp-base) and check what is stopped and what is left; their signal functions
refuse any pid this test did not start, so nothing else on the host can be hit.

Run: python3 tests/skills/process-hygiene/reap-orphans.test.py
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills" / "process-hygiene" / "scripts" / "reap_orphans.py"

spec = importlib.util.spec_from_file_location("reap_orphans", str(SCRIPT))
ro = importlib.util.module_from_spec(spec)
sys.modules["reap_orphans"] = ro   # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(ro)

UID = 4242
ROOT = "/fake/claude-4242/proj/sess/scratchpad"
LSTART = "Sat Oct 18 01:00:00 2026"


def P(pid, ppid=1, pgid=None, uid=UID, stat="S", age=10_000, lstart=LSTART, command="python3 fixture.py"):
    return ro.Proc(pid=pid, ppid=ppid, pgid=pid if pgid is None else pgid, uid=uid, stat=stat,
                   age_s=age, lstart=lstart, command=command)


class Recorder:
    """Signal functions and a clock for stop(); the process table it serves can change per call."""

    def __init__(self, tables, cwds):
        self.tables = list(tables)
        self.cwds = cwds
        self.signals = []
        self.now = 0.0
        self.lines = []
        self.refuse_group = False

    def snapshot(self):
        table = self.tables[0] if len(self.tables) == 1 else self.tables.pop(0)
        return None if table is None else dict(table)

    def kill(self, pid, sig):
        self.signals.append(("pid", pid, sig))

    def killpg(self, pgid, sig):
        if self.refuse_group:
            raise PermissionError("EPERM")
        self.signals.append(("group", pgid, sig))

    def sleep(self, s):
        self.now += s

    def ctx(self, **kw):
        base = dict(uid=UID, min_age_s=7200, grace_s=1.0, roots=[ROOT], snapshot=self.snapshot,
                    read_cwd=lambda pid: self.cwds.get(pid), log=self.lines.append, kill=self.kill,
                    killpg=self.killpg, sleep=self.sleep, clock=lambda: self.now,
                    owner_state=lambda root: ro.GONE, listening=lambda pid: False)
        base.update(kw)
        return ro.Context(**base)


def cand(proc, cwd=ROOT):
    return ro.Candidate(proc, cwd)


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.manifest = self.tmp / "manifest.json"
        self.manifest.write_text(json.dumps({"config": {"K": "7200"}}))

    def test_precedence_cli_env_manifest(self):
        self.assertEqual(ro.setting(5, "K", {"K": "9"}, self.manifest), 5)
        self.assertEqual(ro.setting(None, "K", {"K": "9"}, self.manifest), 9)
        self.assertEqual(ro.setting(None, "K", {}, self.manifest), 7200)

    def test_unusable_values_are_usage_errors(self):
        with self.assertRaises(ValueError):
            ro.setting(None, "MISSING", {}, self.manifest)
        with self.assertRaises(ValueError):
            ro.setting(-1, "K", {}, self.manifest)
        with self.assertRaises(ValueError):
            ro.setting(None, "K", {"K": "soon"}, self.manifest)
        self.manifest.write_text("not json")
        self.assertIsNone(ro.manifest_config("K", self.manifest))

    def test_shipped_manifest_declares_both_keys(self):
        self.assertEqual(ro.manifest_config(ro.MIN_AGE_KEY), "7200")
        self.assertEqual(ro.manifest_config(ro.GRACE_KEY), "5")


class Parsing(unittest.TestCase):
    def test_etime(self):
        self.assertEqual(ro.parse_etime("05"), 5)
        self.assertEqual(ro.parse_etime("01:05"), 65)
        self.assertEqual(ro.parse_etime("02:01:05"), 7265)
        self.assertEqual(ro.parse_etime("8-02:01:05"), 8 * 86400 + 7265)
        self.assertIsNone(ro.parse_etime("x-1"))
        self.assertIsNone(ro.parse_etime("1:2:3:4"))

    def test_ps_rows(self):
        text = ("  12     1    12   501 S     2-00:00:01 Sat Oct  4 01:02:03 2026     bash -c sleep 1\n"
                "  13    12    12   501 Z     00:01 Sat Oct 18 01:02:03 2026\n"
                "garbage\n"
                "  x     1     1   501 S 00:01 Sat Oct 18 01:02:03 2026 cmd\n")
        procs = ro.parse_ps(text)
        self.assertEqual(sorted(procs), [12, 13])
        self.assertEqual(procs[12].lstart, "Sat Oct 4 01:02:03 2026")
        self.assertEqual(procs[12].age_s, 2 * 86400 + 1)
        self.assertEqual(procs[12].command, "bash -c sleep 1")
        self.assertTrue(procs[13].zombie)
        self.assertEqual(procs[13].command, "")

    def test_snapshot_failure_is_none_never_empty(self):
        fail = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "boom"))
        self.assertIsNone(ro.snapshot(run=fail))
        empty = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.assertIsNone(ro.snapshot(run=empty))
        self.assertIsNone(ro.snapshot(run=mock.Mock(side_effect=OSError("no ps"))))
        self.assertIn("LC_ALL", fail.call_args.kwargs["env"])

    def test_cwd_reader_linux(self):
        self.assertEqual(ro.read_cwd(7, "linux", readlink=lambda p: "/tmp" if p == "/proc/7/cwd" else ""),
                         os.path.realpath("/tmp"))
        self.assertIsNone(ro.read_cwd(7, "linux", readlink=mock.Mock(side_effect=OSError)))
        self.assertIsNone(ro.read_cwd(7, "linux", readlink=lambda p: "/gone (deleted)"))

    def test_cwd_reader_macos(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "p7\nfcwd\nn/tmp\n", ""))
        self.assertEqual(ro.read_cwd(7, "darwin", run=run), os.path.realpath("/tmp"))
        self.assertEqual(run.call_args.args[0], ["lsof", "-a", "-p", "7", "-d", "cwd", "-Fn"])
        none = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
        self.assertIsNone(ro.read_cwd(7, "darwin", run=none))
        self.assertIsNone(ro.read_cwd(7, "darwin", run=mock.Mock(side_effect=OSError)))

    def test_launchd_pids(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, "PID\tStatus\tLabel\n123\t0\tcom.a\n-\t0\tcom.b\n\n456\t-9\tcom.c\n", ""))
        self.assertEqual(ro.launchd_pids("darwin", run), {123, 456})
        self.assertEqual(ro.launchd_pids("linux", run), set())
        self.assertEqual(ro.launchd_pids("darwin", mock.Mock(side_effect=OSError)), set())

    def test_roots_and_inside(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        me = os.getuid()
        good = base / f"claude-{me}" / "proj" / "sess" / "scratchpad"
        good.mkdir(parents=True)
        (base / f"claude-{me}" / "proj" / "other").mkdir()
        (base / f"claude-{me + 1}" / "proj" / "sess" / "scratchpad").mkdir(parents=True)
        self.assertEqual(ro.scratchpad_roots(me, [str(base), str(base)]), [os.path.realpath(good)])
        self.assertEqual(ro.scratchpad_roots(me + 1, [str(base)]), [], "a claude-<uid> of another owner")
        root = os.path.realpath(good)
        self.assertTrue(ro.inside(root, [root]))
        self.assertTrue(ro.inside(root + "/a/b", [root]))
        self.assertFalse(ro.inside(root + "2/a", [root]))
        self.assertFalse(ro.inside(str(base), [root]))


class SymlinkedRoots(unittest.TestCase):
    """A symlink below claude-<uid> must never make its target a root, nor a Spotlight target."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.me = os.getuid()
        self.top = self.base / f"claude-{self.me}"
        self.good = self.top / "proj" / "sess" / "scratchpad"
        self.good.mkdir(parents=True)
        self.outside = self.base / "repo"
        (self.outside / "sess" / "scratchpad").mkdir(parents=True)

    def roots(self):
        return ro.scratchpad_roots(self.me, [str(self.base)])

    def test_a_symlinked_scratchpad_is_skipped(self):
        (self.top / "proj" / "linked").mkdir()
        (self.top / "proj" / "linked" / "scratchpad").symlink_to(self.outside)
        self.assertEqual(self.roots(), [str(self.good)])

    def test_a_scratchpad_symlinked_to_a_plain_dir_inside_claude_uid_is_skipped(self):
        data = self.top / "proj" / "sess" / "data"
        data.mkdir()
        (self.top / "proj" / "other").mkdir()
        (self.top / "proj" / "other" / "scratchpad").symlink_to(data)
        self.assertEqual(self.roots(), [str(self.good)], "a symlink must not make a non-scratchpad dir a root")

    def test_a_symlinked_session_or_project_dir_is_skipped(self):
        (self.top / "proj" / "other").symlink_to(self.outside / "sess")
        (self.top / "elsewhere").symlink_to(self.outside)
        self.assertEqual(self.roots(), [str(self.good)])

    def test_a_symlinked_claude_uid_dir_is_skipped(self):
        shutil.rmtree(self.top)
        (self.outside / "proj" / "sess" / "scratchpad").mkdir(parents=True)
        self.top.symlink_to(self.outside)
        self.assertEqual(self.roots(), [])

    def test_spotlight_never_writes_outside_claude_uid(self):
        (self.top / "proj" / "linked").mkdir()
        (self.top / "proj" / "linked" / "scratchpad").symlink_to(self.outside / "sess" / "scratchpad")
        ro.ensure_never_index(self.roots(), lambda line: None)
        written = sorted(str(p) for p in self.base.rglob(ro.NEVER_INDEX))
        self.assertEqual(written, sorted([str(self.good / ro.NEVER_INDEX), str(self.top / ro.NEVER_INDEX)]))


class Rules(unittest.TestCase):
    def ctx(self, cwds=None, **kw):
        return Recorder([{}], cwds or {}).ctx(**kw)

    def reason(self, proc, cwd=ROOT, **kw):
        return ro.classify(proc, self.ctx({proc.pid: cwd}, **kw))[0]

    def test_a_candidate_passes_every_rule(self):
        self.assertIsNone(self.reason(P(10)))
        self.assertIsNone(self.reason(P(10), cwd=ROOT + "/deep/dir"))

    def test_each_rule_alone_excludes(self):
        self.assertEqual(self.reason(P(10, uid=UID + 1)), "another user")
        self.assertEqual(self.reason(P(10, ppid=999)), "has a live parent")
        self.assertEqual(self.reason(P(10, age=7199)), "younger than the minimum age")
        self.assertEqual(self.reason(P(10, age=None)), "younger than the minimum age")
        self.assertEqual(self.reason(P(10, stat="Z")), "zombie")
        self.assertEqual(self.reason(P(10), cwd="/fake/claude-4242/proj/sess/scratchpad-not"),
                         "cwd outside every scratchpad")
        self.assertEqual(self.reason(P(10), cwd=None), "cwd unreadable")
        self.assertEqual(self.reason(P(10), protected_pids={10}), "this run, an ancestor, or a launchd job")

    def test_orphan_parent_is_injectable_for_subreaper_hosts(self):
        self.assertIsNone(self.reason(P(10, ppid=77), orphan_ppid=77))

    def test_a_live_or_unknown_owning_session_spares_the_orphan(self):
        roots = []
        for state, want in ((ro.ALIVE, "owning session alive"), (ro.UNKNOWN, "owning session unknown"),
                            (ro.GONE, None)):
            ctx = self.ctx({10: ROOT + "/sub"}, owner_state=lambda root, st=state: roots.append(root) or st)
            self.assertEqual(ro.classify(P(10), ctx)[0], want)
        self.assertEqual(roots, [ROOT] * 3, "the owner is judged by the scratchpad root, not the cwd")

    def test_never_touch_list(self):
        for command in ("tmux -S /tmp/x.sock new-session -d", "/opt/homebrew/bin/tmux attach", "claude --resume",
                        "-claude", "node /x/node_modules/@anthropic-ai/claude-code/cli.js",
                        "bash -c run sutando-core", "python3 worker.py --name sutando-worker-ab",
                        "bash /w/relay/x/run-bridge.sh in", "python3 src/remote-gateway-bridge.py",
                        "python3 -m remote_gateway_bridge", "/Applications/Foo.app/Contents/MacOS/Foo",
                        "/Applications/Foo Bar.app/Contents/MacOS/Foo Bar --flag",
                        os.path.expanduser("~") + "/Applications/Own.app/Contents/MacOS/Own",
                        "/x/ms-playwright/chrome-headless-shell --headless",
                        "bash src/watch-tasks-stream.sh /w/tasks --role session",
                        "bash src/agent/claude/cli/claude-task-notifier.sh", "node src/voice-agent.ts",
                        "python3 src/chat-bridge.py", "python3 /x/relay/example-bridge.py --once",
                        "python3 src/core_heartbeat.py", "node /x/@modelcontextprotocol/server-fs/dist/index.js",
                        "npx -y chrome-devtools-mcp@1 --browserUrl http://127.0.0.1:9222",
                        "python3 -m my_mcp_server", "node mcp-server.js", "sutando-station serve",
                        "/x/Google Chrome Helper --type=gpu", "/x/Chromium.app/Contents/MacOS/Chromium",
                        "/usr/bin/chromium-browser --headless", "google-chrome --headless", "launchd"):
            with self.subTest(command=command):
                self.assertTrue(self.reason(P(10, command=command)).startswith("never-touch"), command)
        for command in ("python3 /tmp/x/fake-chrome --remote-debugging-port=1", "bash -c tmuxless loop",
                        "node --test tests/a.test.mjs", "swift -interpret x.swift", "fswatch -0 /tmp/x",
                        "/opt/homebrew/Frameworks/Python.framework/Resources/Python.app/Contents/MacOS/Python -u a.py",
                        "/Applications/Xcode.app/Contents/Developer/Toolchains/X/usr/bin/swift-frontend -interpret a",
                        "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/"
                        "Resources/Python.app/Contents/MacOS/Python -c x",
                        "/tmp/x/Applications/Fake.app/Contents/MacOS/Fake"):
            with self.subTest(command=command):
                self.assertIsNone(self.reason(P(10, command=command)), command)

    def test_cwd_is_read_only_for_otherwise_eligible_processes(self):
        reads = []
        ctx = Recorder([{}], {}).ctx(read_cwd=lambda pid: reads.append(pid) or ROOT)
        for proc in (P(1, uid=0), P(2, ppid=5), P(3, age=1), P(4, command="tmux")):
            ro.classify(proc, ctx)
        self.assertEqual(reads, [])

    def test_ancestors_stop_at_init_and_cycles(self):
        procs = {5: P(5, ppid=4), 4: P(4, ppid=1), 1: P(1, ppid=0), 8: P(8, ppid=9), 9: P(9, ppid=8)}
        self.assertEqual(ro.ancestors(5, procs), {5, 4})
        self.assertEqual(ro.ancestors(8, procs), {8, 9})


class OwnerState(unittest.TestCase):
    """owner_state over a fabricated Claude config dir; ps is stubbed by start_of."""
    START = "Fri Oct 2 02:46:19 2026"

    def setUp(self):
        self.cfg = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cfg, True)
        (self.cfg / "sessions").mkdir()
        self.transcript = self.cfg / "projects" / "proj" / "sess.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("{}\n")
        os.utime(self.transcript, (1000, 1000))
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = str(tmp / "claude-1" / "proj" / "sess" / "scratchpad")
        os.makedirs(self.root)
        self.born = ro.created_at(os.path.dirname(self.root))

    def state(self, start_of=lambda pid: None, now=lambda: 1000 + 10_000, configs=None):
        return ro.owner_state(self.root, configs or [str(self.cfg)], 7200, start_of=start_of, now=now)

    def marker(self, pid=4321, session="sess", start="Fri Oct  2 02:46:19 2026", **extra):
        body = dict(pid=pid, sessionId=session, procStart=start, **extra)
        (self.cfg / "sessions" / f"{pid}.json").write_text(json.dumps(body))

    def test_a_running_marker_pid_with_its_start_time_is_alive(self):
        self.marker()
        self.assertEqual(self.state(start_of=lambda pid: self.START if pid == 4321 else None), ro.ALIVE)

    def test_a_marker_whose_pid_is_gone_or_reused_is_not_alive(self):
        self.marker()
        self.assertEqual(self.state(start_of=lambda pid: None), ro.GONE)
        self.assertEqual(self.state(start_of=lambda pid: "Sat Oct 3 00:00:00 2026"), ro.GONE)

    def test_another_sessions_marker_does_not_count(self):
        self.marker(session="other", cwd="/elsewhere")
        self.assertEqual(self.state(start_of=lambda pid: self.START), ro.GONE)

    def utc(self, epoch):
        return time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(epoch))

    def test_a_live_process_in_the_project_that_predates_the_scratchpad_may_own_it_after_clear(self):
        # /clear: same pid and procStart, a new sessionId, nothing pointing back at the old one
        start = self.utc(self.born - 60)
        self.marker(session="after-clear", cwd="proj", start=start)
        self.assertEqual(self.state(start_of=lambda pid: start), ro.UNKNOWN)

    def test_a_process_started_after_the_scratchpad_existed_cannot_own_it(self):
        start = self.utc(self.born + 10 * ro.CLEAR_SLACK_S)
        self.marker(session="later", cwd="proj", start=start)
        self.assertEqual(self.state(start_of=lambda pid: start), ro.GONE)

    def test_only_the_same_project_and_a_live_pid_count(self):
        start = self.utc(self.born - 60)
        self.marker(session="other-project", cwd="/elsewhere", start=start)
        self.assertEqual(self.state(start_of=lambda pid: start), ro.GONE)
        self.marker(session="after-clear", cwd="proj", start=start)
        self.assertEqual(self.state(start_of=lambda pid: None), ro.GONE, "a dead marker pid owns nothing")

    def test_a_same_project_marker_without_start_or_cwd_is_unknown(self):
        (self.cfg / "sessions" / "9.json").write_text(json.dumps({"sessionId": "x", "cwd": "proj"}))
        self.assertEqual(self.state(), ro.UNKNOWN)
        (self.cfg / "sessions" / "9.json").write_text(json.dumps({"pid": 9, "sessionId": "x",
                                                                  "procStart": self.utc(self.born - 60)}))
        self.assertEqual(self.state(start_of=lambda pid: self.utc(self.born - 60)), ro.UNKNOWN,
                         "a marker with no cwd may be in any project")

    def test_slug_and_epoch_helpers(self):
        self.assertEqual(ro.project_slug("/a/b.c_d/-e"), "-a-b-c-d--e")
        self.assertEqual(ro.utc_epoch("Thu Jan  1 00:01:40 1970"), 100.0)
        self.assertIsNone(ro.utc_epoch("not a time"))

    def test_a_recent_transcript_is_alive(self):
        self.assertEqual(self.state(now=lambda: 1000 + 60), ro.ALIVE)

    def test_no_transcript_anywhere_is_unknown(self):
        self.transcript.unlink()
        self.assertEqual(self.state(), ro.UNKNOWN)

    def test_no_sessions_dir_is_unknown(self):
        (self.cfg / "sessions").rmdir()
        self.assertEqual(self.state(), ro.UNKNOWN)

    def test_unreadable_records_are_unknown(self):
        (self.cfg / "sessions" / "9.json").write_text("{not json")
        self.assertEqual(self.state(), ro.UNKNOWN)
        (self.cfg / "sessions" / "9.json").write_text(json.dumps({"sessionId": "sess"}))
        self.assertEqual(self.state(), ro.UNKNOWN)
        (self.cfg / "sessions" / "9.json").unlink()
        os.chmod(self.cfg / "sessions", 0)
        self.addCleanup(os.chmod, self.cfg / "sessions", 0o700)
        if os.access(self.cfg / "sessions", os.R_OK):
            self.skipTest("running as a user that can read a mode-000 directory")
        self.assertEqual(self.state(), ro.UNKNOWN)

    def refuse(self, suffix):
        real = os.stat

        def fake(path, *a, **kw):
            if str(path).endswith(suffix):
                raise PermissionError("EACCES")
            return real(path, *a, **kw)
        return mock.patch.object(ro.os, "stat", side_effect=fake)

    def test_unstattable_transcript_is_unknown(self):
        with self.refuse(".jsonl"):
            self.assertEqual(self.state(), ro.UNKNOWN)

    def test_a_refused_sessions_probe_is_unknown_even_when_another_config_dir_reads_gone(self):
        live = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, live, True)
        (live / "sessions").mkdir()
        (live / "sessions" / "77.json").write_text(json.dumps({"pid": 77, "sessionId": "sess",
                                                               "procStart": self.START}))
        real = os.stat

        def fake(path, *a, **kw):
            if str(path) == str(live / "sessions"):
                raise PermissionError("EACCES")
            return real(path, *a, **kw)
        with mock.patch.object(ro.os, "stat", side_effect=fake):
            state = ro.owner_state(self.root, [str(live), str(self.cfg)], 7200,
                                   start_of=lambda pid: self.START, now=lambda: 1000 + 10_000)
        self.assertEqual(state, ro.UNKNOWN, "a sessions/ dir that cannot be probed is not an absent one")

    def test_a_sessions_dir_probe_that_raises_is_unknown(self):
        self.marker()
        with self.refuse("sessions"):
            self.assertEqual(self.state(start_of=lambda pid: self.START), ro.UNKNOWN,
                             "a raising directory probe must read unknown on every Python version")
        with mock.patch.object(ro.os, "listdir", side_effect=PermissionError("EACCES")):
            self.assertEqual(self.state(), ro.UNKNOWN)
        with mock.patch("builtins.open", side_effect=PermissionError("EACCES")):
            self.assertEqual(self.state(), ro.UNKNOWN)

    def test_a_path_that_is_not_a_scratchpad_is_unknown(self):
        self.assertEqual(ro.owner_state("/t/x", [str(self.cfg)], 7200), ro.UNKNOWN)

    def test_the_marker_found_in_any_config_dir_counts(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        (other / "sessions").mkdir()
        (other / "sessions" / "77.json").write_text(json.dumps({"pid": 77, "sessionId": "sess",
                                                                "procStart": self.START}))
        self.assertEqual(self.state(start_of=lambda pid: self.START, configs=[str(self.cfg), str(other)]),
                         ro.ALIVE)

    def test_utc_start_reads_ps_in_utc(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Fri Oct  2 02:46:19 2026\n", ""))
        self.assertEqual(ro.utc_start(5, run=run), self.START)
        self.assertEqual(run.call_args.kwargs["env"]["TZ"], "UTC")
        self.assertIsNone(ro.utc_start(5, run=mock.Mock(side_effect=OSError)))
        self.assertIsNone(ro.utc_start(5, run=mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))))
        self.assertEqual(ro.utc_start(os.getpid()), ro.utc_start(os.getpid()))

    def test_default_config_dirs_are_existing_realpaths(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}):
            dirs = ro.default_config_dirs()
        self.assertIn(os.path.realpath(self.cfg), dirs)
        self.assertTrue(all(os.path.isdir(d) for d in dirs))
        self.assertEqual(len(dirs), len(set(dirs)))


class Servers(unittest.TestCase):
    def test_lsof_reader(self):
        cp = subprocess.CompletedProcess
        hit = mock.Mock(return_value=cp([], 0, "p7\nf3\nnlocalhost:5393\n", ""))
        self.assertTrue(ro.listening(7, "darwin", run=hit))
        argv = hit.call_args.args[0]
        self.assertIn("-sTCP:LISTEN", argv)
        self.assertIn("-iUDP", argv)
        self.assertIs(ro.listening(7, "darwin", run=mock.Mock(return_value=cp([], 1, "", ""))), False)
        self.assertIsNone(ro.listening(7, "darwin", run=mock.Mock(return_value=cp([], 1, "", "lsof: oops"))))
        self.assertIsNone(ro.listening(7, "darwin", run=mock.Mock(return_value=cp([], 2, "", ""))))
        self.assertIsNone(ro.listening(7, "darwin", run=mock.Mock(side_effect=OSError)))
        self.assertIs(ro.listening(7, "linux", linux=lambda pid: True), True)

    def test_proc_reader(self):
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

        def row(state, inode):
            return f"   0: 0100007F:1F90 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 501 0 {inode}\n"

        def reader(tables):
            def read(path):
                name = path.rsplit("/", 1)[1]
                if name not in tables:
                    raise FileNotFoundError(path)
                return header + tables[name]
            return read

        fds = dict(listdir=lambda p: ["3", "4"],
                   readlink=lambda p: "socket:[555]" if p.endswith("/3") else "/dev/null")
        self.assertTrue(ro._proc_listens(9, read=reader({"tcp": row("0A", 555)}), **fds))
        self.assertFalse(ro._proc_listens(9, read=reader({"tcp": row("01", 555)}), **fds),
                         "an established connection is not a server")
        self.assertTrue(ro._proc_listens(9, read=reader({"udp6": row("07", 555)}), **fds))
        self.assertFalse(ro._proc_listens(9, read=reader({"tcp": row("0A", 999)}), **fds))
        self.assertFalse(ro._proc_listens(9, read=reader({}), listdir=lambda p: ["1"],
                                          readlink=lambda p: "/dev/null"))
        self.assertIsNone(ro._proc_listens(9, listdir=mock.Mock(side_effect=PermissionError)))

    def test_a_listening_or_unreadable_member_spares_the_whole_group(self):
        procs = {20: P(20, pgid=20), 21: P(21, pgid=20), 30: P(30, pgid=30), 40: P(40, pgid=40),
                 41: P(41, ppid=40, pgid=40, age=5)}
        cands = {20: cand(procs[20]), 21: cand(procs[21]), 30: cand(procs[30]), 40: cand(procs[40])}
        states = {20: False, 21: True, 30: False, 40: False, 41: None}
        rec = Recorder([{}], {})
        kept = ro.spare_servers(cands, procs, rec.ctx(listening=states.get))
        self.assertEqual(sorted(kept), [30], "a group with a server, or an unreadable member, must be spared")
        self.assertEqual(len([line for line in rec.lines if line.startswith("spared-server")]), 2)


class Planning(unittest.TestCase):
    def test_whole_group_only_when_every_member_is_a_candidate(self):
        procs = {20: P(20, pgid=20), 21: P(21, pgid=20), 30: P(30, pgid=30), 31: P(31, ppid=30, pgid=30)}
        cands = {20: cand(procs[20]), 21: cand(procs[21]), 30: cand(procs[30])}
        targets = ro.plan(cands, procs, Recorder([{}], {}).ctx())
        self.assertEqual([(t.kind, t.ident, sorted(t.members)) for t in targets],
                         [("group", 20, [20, 21]), ("pid", 30, [30])])

    def test_never_the_own_group_or_init(self):
        procs = {20: P(20, pgid=7), 40: P(40, pgid=1)}
        cands = {20: cand(procs[20]), 40: cand(procs[40])}
        targets = ro.plan(cands, procs, Recorder([{}], {}).ctx(own_pgid=7))
        self.assertEqual([(t.kind, t.ident) for t in targets], [("pid", 40), ("pid", 20)])

    def test_zombie_members_do_not_block_a_group(self):
        procs = {20: P(20, pgid=20), 21: P(21, pgid=20, stat="Z")}
        targets = ro.plan({20: cand(procs[20])}, procs, Recorder([{}], {}).ctx())
        self.assertEqual([(t.kind, t.ident) for t in targets], [("group", 20)])


class Stopping(unittest.TestCase):
    def stop(self, target, tables, cwds=None, **kw):
        rec = Recorder(tables, cwds if cwds is not None else {pid: ROOT for pid in target.members})
        for k, v in kw.items():
            setattr(rec, k, v)
        outcome = ro.stop(target, rec.ctx())
        return outcome, rec

    def target(self, *procs, kind="pid"):
        ident = procs[0].pgid if kind == "group" else procs[0].pid
        return ro.Target(kind, ident, {p.pid: cand(p) for p in procs})

    def test_terminated_after_sigterm(self):
        p = P(50)
        outcome, rec = self.stop(self.target(p), [{50: p}, {}, {}])
        self.assertEqual(outcome, "terminated")
        self.assertEqual(rec.signals, [("pid", 50, signal.SIGTERM)])

    def test_pid_reuse_is_never_signalled(self):
        p = P(50)
        for changed in (P(50, lstart="Sun Oct 19 02:00:00 2026"), P(50, command="sleep 999")):
            outcome, rec = self.stop(self.target(p), [{50: changed}])
            self.assertEqual((outcome, rec.signals), ("aborted", []))
        outcome, rec = self.stop(self.target(p), [{50: p}], cwds={50: "/elsewhere"})
        self.assertEqual((outcome, rec.signals), ("aborted", []))
        outcome, rec = self.stop(self.target(p), [{}])
        self.assertEqual((outcome, rec.signals), ("aborted", []), "a vanished pid must not be signalled")
        outcome, rec = self.stop(self.target(p), [None])
        self.assertEqual((outcome, rec.signals), ("aborted", []), "ps failure is unknown, never safe")

    def test_term_ignoring_survivor_gets_kill_after_the_grace(self):
        p = P(50)
        outcome, rec = self.stop(self.target(p), [{50: p}])
        self.assertEqual(outcome, "killed")
        self.assertEqual(rec.signals, [("pid", 50, signal.SIGTERM), ("pid", 50, signal.SIGKILL)])
        self.assertGreaterEqual(rec.now, 1.0, "SIGKILL came before the grace elapsed")
        self.assertTrue(any(line.startswith("sigkill ") for line in rec.lines))

    def test_survivor_that_changed_during_the_grace_is_not_killed(self):
        p = P(50)
        swapped = P(50, command="something else")
        outcome, rec = self.stop(self.target(p), [{50: p}] + [{50: p}] * 3 + [{50: swapped}])
        self.assertEqual(outcome, "aborted")
        self.assertEqual(rec.signals, [("pid", 50, signal.SIGTERM)])

    def test_group_is_signalled_as_a_group(self):
        a, b = P(60, pgid=60), P(61, pgid=60)
        outcome, rec = self.stop(self.target(a, b, kind="group"), [{60: a, 61: b}, {61: b}, {}])
        self.assertEqual(outcome, "terminated")
        self.assertEqual(rec.signals, [("group", 60, signal.SIGTERM)])

    def test_group_that_gained_a_member_falls_back_to_its_unchanged_members(self):
        a, b = P(60, pgid=60), P(61, pgid=60)
        stranger = P(62, ppid=61, pgid=60, command="not a candidate")
        everyone, without_a = {60: a, 61: b, 62: stranger}, {61: b, 62: stranger}
        tables = [everyone, everyone, without_a, without_a, without_a, {62: stranger}]
        outcome, rec = self.stop(self.target(a, b, kind="group"), tables)
        self.assertEqual(outcome, "terminated")
        self.assertEqual(rec.signals, [("pid", 60, signal.SIGTERM), ("pid", 61, signal.SIGTERM)])
        self.assertFalse(any(s[0] == "group" for s in rec.signals))

    def test_group_whose_members_all_left_is_aborted(self):
        a = P(60, pgid=60)
        outcome, rec = self.stop(self.target(a, kind="group"), [{}])
        self.assertEqual((outcome, rec.signals), ("aborted", []))

    def test_group_refusing_killpg_signals_each_checked_member(self):
        a, b = P(60, pgid=60), P(61, pgid=60)
        outcome, rec = self.stop(self.target(a, b, kind="group"), [{60: a, 61: b}, {}], refuse_group=True)
        self.assertEqual(outcome, "terminated")
        self.assertEqual(rec.signals, [("pid", 60, signal.SIGTERM), ("pid", 61, signal.SIGTERM)])

    def test_permission_denied_is_reported_not_retried(self):
        p = P(50)
        rec = Recorder([{50: p}], {50: ROOT})
        ctx = rec.ctx(kill=mock.Mock(side_effect=PermissionError("EPERM")))
        self.assertEqual(ro.stop(self.target(p), ctx), "aborted")
        self.assertTrue(any(line.startswith("signal-refused") for line in rec.lines))
        a, b = P(60, pgid=60), P(61, pgid=60)
        rec = Recorder([{60: a, 61: b}], {60: ROOT, 61: ROOT})
        rec.refuse_group = True
        ctx = rec.ctx(kill=mock.Mock(side_effect=[ProcessLookupError(), PermissionError("EPERM")]))
        self.assertEqual(ro.stop(self.target(a, b, kind="group"), ctx), "aborted")

    def test_a_target_already_gone_at_signal_time_counts_as_signalled(self):
        p = P(50)
        rec = Recorder([{50: p}, {}], {50: ROOT})
        ctx = rec.ctx(kill=mock.Mock(side_effect=ProcessLookupError()))
        self.assertEqual(ro.stop(self.target(p), ctx), "terminated")


class Spotlight(unittest.TestCase):
    def test_markers_are_idempotent_and_never_truncate(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        root = base / f"claude-{UID}" / "proj" / "sess" / "scratchpad"
        root.mkdir(parents=True)
        existing = base / f"claude-{UID}" / ro.NEVER_INDEX
        existing.write_text("kept")
        lines = []
        created = ro.ensure_never_index([str(root)], lines.append)
        self.assertEqual(created, [str(root / ro.NEVER_INDEX)])
        self.assertEqual(existing.read_text(), "kept")
        self.assertEqual(ro.ensure_never_index([str(root)], lines.append), [])
        self.assertEqual(len([line for line in lines if line.startswith("spotlight-marker ")]), 1)
        missing = base / "nope" / "a" / "b" / "scratchpad"
        self.assertEqual(ro.ensure_never_index([str(missing)], lines.append), [])
        self.assertTrue(any(line.startswith("spotlight-marker-failed") for line in lines))


def start_time(pid):
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env=isolated_child_env(LC_ALL="C")).stdout
    return " ".join(out.split())


def ppid_of(pid):
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) if out else None


def alive(pid):
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


@unittest.skipUnless(os.name == "posix" and shutil.which("ps") and
                     (sys.platform.startswith("linux") or shutil.which("lsof")), "needs ps and a cwd reader")
class RealOrphans(unittest.TestCase):
    MIN_AGE = 4   # the young orphan must stay under it for the whole scan

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="reap-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / f"claude-{os.getuid()}" / "proj" / "sess" / "scratchpad"
        self.root.mkdir(parents=True)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.mine = {}
        self.addCleanup(self.kill_mine)
        self.log = self.base / "hygiene.log"
        self.orphan_ppid = None
        self.cfg = self.base / "cfg"
        (self.cfg / "sessions").mkdir(parents=True)
        self.transcript = self.cfg / "projects" / "proj" / "sess.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("{}\n")
        old = time.time() - 10 * 86400
        os.utime(self.transcript, (old, old))   # an ended session, unless a test says otherwise

    def kill_mine(self):
        """Only this test's pids, verified by start time, and the children they started."""
        table = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
        children = {}
        for line in table.splitlines():
            pid, ppid = (int(x) for x in line.split())
            children.setdefault(ppid, []).append(pid)
        for pid, lstart in self.mine.items():
            if lstart and start_time(pid) == lstart:
                for victim in children.get(pid, []) + [pid]:
                    try:
                        os.kill(victim, signal.SIGKILL)
                    except OSError:
                        pass

    def orphan(self, cwd, *argv):
        """Start argv re-parented away from this test: its launching shell exits at once."""
        out = subprocess.run(["bash", "-c", 'cd "$1" || exit 1; shift; "$@" </dev/null >/dev/null 2>&1 & echo $!',
                              "_", str(cwd), *argv], capture_output=True, text=True, start_new_session=True,
                             check=True)
        pid = int(out.stdout.strip())
        self.mine[pid] = start_time(pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ppid = ppid_of(pid)
            if ppid is not None and ppid != os.getpid():
                self.orphan_ppid = self.orphan_ppid or ppid
                return pid
            time.sleep(0.02)
        self.fail(f"{pid} was never re-parented")

    def named(self, name):
        link = self.bin / name
        if not link.exists():
            link.symlink_to(shutil.which("sleep"))
        return str(link)

    def args(self, *extra):
        return ro.parse_args(["--tmp-base", str(self.base), "--min-age", str(self.MIN_AGE), "--grace", "1",
                              "--log", str(self.log), "--claude-config-dir", str(self.cfg), *extra])

    def guarded(self, args):
        ctx = ro.build_context(args, ro.file_logger(args.log), orphan_ppid=self.orphan_ppid or 1)

        def kill(pid, sig):
            assert pid in self.mine, f"refusing to signal {pid}: not started by this test"
            os.kill(pid, sig)

        def killpg(pgid, sig):
            procs = ctx.snapshot() or {}
            members = {p.pid for p in procs.values() if p.pgid == pgid}
            assert members and members <= set(self.mine), f"refusing group {pgid}: {members}"
            os.killpg(pgid, sig)

        ctx.kill, ctx.killpg = kill, killpg
        return ctx

    def run_reaper(self, *extra):
        args = self.args(*extra)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            counts = ro.run(args, self.guarded(args))
        return counts, out.getvalue()

    def wait_gone(self, pid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and alive(pid):
            time.sleep(0.05)
        return not alive(pid)

    def test_only_the_old_orphan_in_a_scratchpad_is_stopped(self):
        old = self.orphan(self.root, "sleep", "600")
        outside = self.orphan(self.outside, "sleep", "601")
        tmux = self.orphan(self.root, self.named("tmux"), "602")
        claude = self.orphan(self.root, self.named("claude"), "603")
        parented = subprocess.Popen(["sleep", "604"], cwd=str(self.root))
        self.mine[parented.pid] = start_time(parented.pid)
        self.addCleanup(lambda: (parented.kill(), parented.wait()))
        time.sleep(self.MIN_AGE + 1.5)
        young = self.orphan(self.root, "sleep", "605")

        counts, out = self.run_reaper("--reap")

        self.assertTrue(self.wait_gone(old), "the old scratchpad orphan survived")
        for pid, what in ((outside, "outside the root"), (tmux, "tmux"), (claude, "claude"),
                          (parented.pid, "a live parent"), (young, "a young orphan")):
            self.assertTrue(alive(pid), f"{what} was stopped")
        self.assertEqual((counts["candidates"], counts["terminated"]), (1, 1), out)
        log = self.log.read_text()
        self.assertIn("sigterm ", log)
        self.assertIn("summary mode=reap", log)

    def test_report_mode_stops_nothing(self):
        old = self.orphan(self.root, "sleep", "610")
        time.sleep(self.MIN_AGE + 1.5)
        counts, out = self.run_reaper()
        self.assertEqual(counts["candidates"], 1)
        self.assertIn("would stop group=", out)
        self.assertIn(str(old), out)
        time.sleep(0.5)
        self.assertTrue(alive(old), "report mode stopped a process")
        self.assertNotIn("sigterm", self.log.read_text())

    def test_term_ignoring_orphan_is_killed_after_the_grace(self):
        ready = self.base / "ready"
        code = ("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({str(ready)!r}, 'w').close()\ntime.sleep(620)\n")
        pid = self.orphan(self.root, sys.executable, "-c", code)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(self.MIN_AGE + 1.5)
        counts, out = self.run_reaper("--reap")
        self.assertTrue(self.wait_gone(pid), "the TERM-ignoring orphan survived")
        self.assertEqual(counts["killed"], 1, out)
        self.assertIn("sigkill ", self.log.read_text())

    def test_a_process_that_changes_between_scan_and_signal_is_left_alone(self):
        for change, trigger_body in (("exec", "exec sleep 630"), ("cwd", f'cd "{self.outside}"; sleep 631')):
            with self.subTest(change=change):
                # A blocking read starts no child: a polling loop's short-lived children could
                # vanish mid-scan, read as an unreadable group member, and spare the orphan.
                trig = self.base / f"trig-{change}"
                os.mkfifo(trig)
                script = f'read -r _ < "{trig}"; {trigger_body}; :'
                pid = self.orphan(self.root, "bash", "-c", script)
                time.sleep(self.MIN_AGE + 1.5)
                args = self.args("--reap")
                ctx = self.guarded(args)
                procs = ctx.snapshot()
                targets = ro.plan(ro.find_candidates(procs, ctx), procs, ctx)
                mine = [t for t in targets if pid in t.members]
                self.assertEqual(len(mine), 1, "the orphan was not a candidate; the case proves nothing")
                fd = os.open(trig, os.O_WRONLY | os.O_NONBLOCK)   # ENXIO, not a hang, if no reader
                os.write(fd, b"go\n")
                os.close(fd)
                deadline = time.monotonic() + 10
                before = mine[0].members[pid]
                while time.monotonic() < deadline:
                    procs_now = ctx.snapshot()
                    if change == "exec" and procs_now[pid].command != before.proc.command:
                        break
                    if change == "cwd" and ctx.read_cwd(pid) != before.cwd:
                        break
                    time.sleep(0.05)
                self.assertEqual(ro.stop(mine[0], ctx), "aborted")
                time.sleep(0.3)
                self.assertTrue(alive(pid), f"a process whose {change} changed was signalled")

    def test_a_live_owning_session_keeps_its_orphan_until_it_ends(self):
        owner = subprocess.Popen(["sleep", "650"])          # stands in for the session's claude process
        self.mine[owner.pid] = start_time(owner.pid)
        self.addCleanup(lambda: (owner.kill(), owner.wait()))
        (self.cfg / "sessions" / f"{owner.pid}.json").write_text(json.dumps(
            {"pid": owner.pid, "sessionId": "sess", "procStart": ro.utc_start(owner.pid)}))
        orphan = self.orphan(self.root, "sleep", "651")
        time.sleep(self.MIN_AGE + 1.5)

        counts, out = self.run_reaper("--reap")
        self.assertEqual(counts["candidates"], 0, out)
        time.sleep(0.3)
        self.assertTrue(alive(orphan), "a live session's orphaned child was stopped")

        owner.kill()
        owner.wait()                                        # the marker stays behind, as after a crash
        counts, out = self.run_reaper("--reap")
        self.assertEqual(counts["terminated"], 1, out)
        self.assertTrue(self.wait_gone(orphan), "an ended session's orphan survived")

    def test_an_unreadable_owner_spares_the_orphan(self):
        self.transcript.unlink()                            # no record of the session anywhere
        orphan = self.orphan(self.root, "sleep", "652")
        time.sleep(self.MIN_AGE + 1.5)
        counts, out = self.run_reaper("--reap")
        self.assertEqual(counts["candidates"], 0, out)
        time.sleep(0.3)
        self.assertTrue(alive(orphan), "an orphan whose owner could not be read was stopped")

    def test_a_server_of_an_ended_session_survives(self):
        code = ("import socket, time\ns = socket.socket()\ns.bind(('127.0.0.1', 0))\ns.listen()\n"
                "time.sleep(660)\n")
        server = self.orphan(self.root, sys.executable, "-c", code)
        plain = self.orphan(self.root, "sleep", "661")
        deadline = time.monotonic() + 10
        while ro.listening(server) is not True and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIs(ro.listening(server), True, "the fixture never listened; the case proves nothing")
        time.sleep(self.MIN_AGE + 1.5)
        counts, out = self.run_reaper("--reap")
        self.assertTrue(self.wait_gone(plain), "the plain orphan beside it should still be reaped")
        self.assertEqual(counts["terminated"], 1, out)
        time.sleep(0.3)
        self.assertTrue(alive(server), "a listening server was stopped")
        self.assertIn("spared-server", self.log.read_text())

    def test_main_reports_and_exits_zero(self):
        self.orphan(self.root, "sleep", "640")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ro.main(["--tmp-base", str(self.base), "--min-age", "0", "--grace", "1", "--log", str(self.log),
                          "--claude-config-dir", str(self.cfg), "--spotlight"], orphan_ppid=self.orphan_ppid or 1)
        self.assertEqual(rc, 0)
        self.assertIn("mode=report", out.getvalue())
        self.assertTrue((self.root / ro.NEVER_INDEX).exists())


class DeveloperOnly(unittest.TestCase):
    """A data pin: the skill ships no schedule, because new installs copy the example crons."""

    def test_no_example_cron_and_the_doc_says_developer_only(self):
        crons = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
        self.assertFalse([e for e in crons if "process-hygiene" in json.dumps(e)])
        doc = (REPO / "skills" / "process-hygiene" / "SKILL.md").read_text()
        self.assertIn("Developer-only, not scheduled", doc)
        self.assertIn("reap_orphans.py --reap --spotlight", doc)
        manifest = json.loads((REPO / "skills" / "process-hygiene" / "manifest.json").read_text())
        self.assertTrue(manifest["description"].startswith("Developer-only, not scheduled."))


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "h.log"

    def test_usage_errors_are_non_zero(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ro.main(["--min-age", "-5", "--log", str(self.log)]), 2)
            with self.assertRaises(SystemExit) as cm:
                ro.main(["--bogus"])
        self.assertEqual(cm.exception.code, 2)

    def test_reap_refuses_a_minimum_age_under_the_floor(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(ro, "run") as run:
            self.assertEqual(ro.main(["--reap", "--min-age", "599", "--tmp-base", str(self.tmp),
                                      "--log", str(self.log)]), 2)
            run.assert_not_called()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ro.main(["--reap", "--min-age", "600", "--tmp-base", str(self.tmp),
                                          "--log", str(self.log)]), 0)
            run.assert_called_once()
            self.assertEqual(ro.main(["--min-age", "5", "--tmp-base", str(self.tmp), "--log", str(self.log)]), 0,
                             "report mode has no floor")
        self.assertIn("at least 600", err.getvalue())

    def test_a_second_run_while_one_holds_the_lock_does_nothing(self):
        held = ro.run_lock(self.tmp / "process-hygiene.lock")
        self.assertIsNotNone(held)
        self.addCleanup(held.close)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(ro, "run") as run:
            self.assertEqual(ro.main(["--tmp-base", str(self.tmp), "--log", str(self.log)]), 0)
        run.assert_not_called()
        self.assertIn("another run holds the lock", out.getvalue())
        held.close()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(ro, "run") as run:
            ro.main(["--tmp-base", str(self.tmp), "--log", str(self.log)])
        run.assert_called_once()

    def test_a_failing_run_still_exits_zero_and_logs(self):
        out = io.StringIO()
        with mock.patch.object(ro, "run", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(out):
            self.assertEqual(ro.main(["--tmp-base", str(self.tmp), "--log", str(self.log)]), 0)
        self.assertIn("run-failed", self.log.read_text())

    def test_unreadable_process_table_stops_nothing(self):
        rec = Recorder([None], {})
        args = argparse.Namespace(reap=True, spotlight=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            counts = ro.run(args, rec.ctx())
        self.assertEqual((counts["candidates"], rec.signals), (0, []))
        self.assertIn("ps-unavailable", rec.lines[0])

    def test_default_log_lives_in_the_resolved_workspace(self):
        with mock.patch.dict(sys.modules):
            fake = type(sys)("workspace_default")
            fake.resolve_workspace = lambda migrate=True: self.tmp / "ws"
            sys.modules["workspace_default"] = fake
            self.assertEqual(ro.default_log_path(), self.tmp / "ws" / "logs" / ro.LOG_NAME)

    def test_unwritable_log_is_reported_not_fatal(self):
        err = io.StringIO()
        blocker = self.tmp / "file"
        blocker.write_text("")
        with contextlib.redirect_stderr(err):
            ro.file_logger(blocker / "sub" / "x.log")("line")
        self.assertIn("cannot write", err.getvalue())


if __name__ == "__main__":
    unittest.main()
