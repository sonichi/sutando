#!/usr/bin/env python3
"""The gemini adapter: Gemini CLI (or Antigravity) woken over ACP per task, and its install/verify/run.

The wake is exercised against tests/gemini_stub_acp.py, which speaks the wire Gemini CLI 0.59 was measured
speaking: sign-in methods listed at every initialize, -32000 from session/new when not signed in, permission
requests without rawInput, and (modelling the relaunch race lost) no answer without GEMINI_CLI_NO_RELAUNCH=true.

Run:
    python3 packages/ag2-sparrow/tests/test_gemini_adapter.py
"""
import argparse
import atexit
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import acp_host, connect_common as cm, holder_lock, task_io  # noqa: E402

# The work-root lock lives under the passwd home, not HOME: keep it in a temp dir for this suite.
LOCK_HOME = tempfile.mkdtemp(prefix="gemini-lock-home-")
holder_lock._home = lambda: LOCK_HOME
atexit.register(shutil.rmtree, LOCK_HOME, True)
from ag2_sparrow.adapters.gemini import cli, gemini_connector as gc  # noqa: E402
from ag2_sparrow.result_markers import parse_markers  # noqa: E402

STUB = str(Path(__file__).resolve().parent / "gemini_stub_acp.py")


def task_text(tid, body, tier="owner", channel="!room:s.space", event_type=None, collaborator=False):
    lines = ["id: " + tid, "timestamp: 2026-11-14T00:00:00Z", "source: ag2space", "channel_id: " + channel,
             "source_room_id: " + channel, "source_message_id: $m-" + tid, "user_id: @alice:s.space",
             "sender_name: Alice", "access_tier: " + tier]
    if collaborator:
        lines.append("collaborator: true")
    if event_type:
        lines.append("event_type: " + event_type)
    return "\n".join(lines + ["task: " + body]) + "\n"


class Work:
    """A work root, a Session directory and the stub's HOME (its sign-in, sessions and log)."""

    def __init__(self, d, signed_in=True):
        self.root = Path(d) / "work"
        for sub in ("tasks", "results", "state"):
            (self.root / sub).mkdir(parents=True)
        self.cwd = Path(d) / "cwd"
        self.cwd.mkdir()
        self.home = Path(d) / "home"
        self.home.mkdir()
        if signed_in:
            (self.home / "signed-in").write_text("")

    def task(self, tid, body, **kw):
        (self.root / "tasks" / (tid + ".txt")).write_text(task_text(tid, body, **kw), encoding="utf-8")

    def result(self, tid):
        p = self.root / "results" / (tid + ".txt")
        return p.read_text(encoding="utf-8") if p.exists() else None

    def records(self):
        log = self.home / "stub.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()] if log.exists() else []

    def methods(self):
        return [r.get("method") for r in self.records() if r.get("method")]

    def args(self, *extra, env=None):
        a = gc.build_parser().parse_args(["--work", str(self.root), "--cwd", str(self.cwd), "--acp-command",
                                          shlex.join([sys.executable, STUB]), "--once", "1", "--poll", "0.05", *extra])
        gc._prepare(a)
        gc._paths(a, str(self.root))
        a.child_env = gc.agent_env(isolated_child_env(HOME=str(self.home))) if env is None else env
        return a

    def serve(self, n=1, *extra, env=None):
        a = self.args(*extra, env=env)
        a.once = n
        with contextlib.redirect_stderr(io.StringIO()):
            gc.Connector(a).serve()
        return a

    def metrics(self):
        p = self.root / "state" / "metrics.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()]


class ConnectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = Work(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_task_wakes_gemini_once_and_its_final_text_is_the_reply(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "hello from gemini-stub\n")
        methods = self.w.methods()
        self.assertEqual([m for m in methods if m in ("initialize", "session/new", "session/prompt")],
                         ["initialize", "session/new", "session/prompt"])
        init = next(r for r in self.w.records() if r.get("method") == "initialize")
        self.assertEqual(init["params"]["clientInfo"]["name"], "ag2-sparrow-gemini")

    def test_the_agent_always_runs_with_no_relaunch(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        self.assertEqual(self.w.records()[0]["no_relaunch"], "true")
        self.assertEqual(gc.agent_env({"PATH": "/bin"}), {"PATH": "/bin", "GEMINI_CLI_NO_RELAUNCH": "true"})
        self.assertEqual(gc.agent_env({"GEMINI_CLI_NO_RELAUNCH": "false"})["GEMINI_CLI_NO_RELAUNCH"], "true")
        a = gc.build_parser().parse_args(["--work", "w"])
        gc._prepare(a)
        self.assertEqual(a.child_env["GEMINI_CLI_NO_RELAUNCH"], "true")

    def test_without_no_relaunch_gemini_never_answers_and_the_message_is_told(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve(1, "--rpc-timeout", "1", "--interrupt-grace", "1",
                     env=isolated_child_env(HOME=str(self.w.home)))
        self.assertIn("Gemini did not finish this in time (initialize: no response in 1.0s)", self.w.result("task-1"))

    def test_signed_in_while_sign_in_methods_are_listed_needs_no_authenticate(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        self.assertNotIn("authenticate", self.w.methods())
        self.assertEqual(self.w.metrics()[0]["status"], "completed")

    def test_not_signed_in_is_said_for_a_message_and_posts_nothing_for_an_event(self):
        (self.w.home / "signed-in").unlink()
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.task("task-2", "say hi", event_type="room.joined")
        os.utime(self.w.root / "tasks" / "task-2.txt", (2e9, 2e9))
        self.w.serve(2)
        said = self.w.result("task-1")
        self.assertIn("Gemini is not signed in on this computer", said)
        self.assertIn("run `gemini` once in a terminal and sign in", said)
        self.assertNotIn("session/prompt", self.w.methods())
        self.assertTrue(self.w.result("task-2").startswith("[no-send] Gemini handled the event without posting"))

    def test_no_reply_posts_nothing(self):
        self.w.task("task-1", "STUB:NOREPLY thanks!")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "[no-send] Gemini chose not to post\n")
        self.assertEqual(parse_markers(self.w.result("task-1")).actions[0].kind, "skip")

    def test_delivery_markers_in_the_final_text_never_act(self):
        self.w.task("task-1", "STUB:MARKER hi")
        self.w.serve()
        said = self.w.result("task-1")
        self.assertIn("hello", said)
        self.assertEqual(parse_markers(said).actions, [])

    def test_the_owner_may_edit_only_under_the_working_directory_and_no_command_is_granted(self):
        self.w.task("task-1", "STUB:EDIT go")
        self.w.task("task-2", "STUB:SHELL go", channel="!b:s.space")
        os.utime(self.w.root / "tasks" / "task-2.txt", (2e9, 2e9))
        self.w.serve(2)
        perms = [r for r in self.w.records() if "permission" in r]
        self.assertEqual(perms, [{"permission": "WriteFile notes.txt", "allowed": True},
                                 {"permission": "WriteFile /etc/stub-outside", "allowed": False},
                                 {"permission": "rm -rf ~ [current working directory]", "allowed": False}])
        reasons = [d["reason"] for m in self.w.metrics() for d in m["permissions"]]
        self.assertEqual(reasons, ["under the working directory", "outside the working directory",
                                   "no command is granted"])

    def test_a_collaborator_may_not_touch_files(self):
        self.w.task("task-1", "STUB:EDIT go", tier="team", collaborator=True)
        self.w.serve()
        perms = [r for r in self.w.records() if "permission" in r]
        self.assertEqual([p["allowed"] for p in perms], [False, False])
        prompt = next(r["text"] for r in self.w.records() if r.get("method") == "session/prompt")
        self.assertIn("You may not read or change files.", prompt)

    def test_a_guest_is_refused_without_waking_gemini(self):
        self.w.task("task-1", "STUB:TEXT hi", tier="guest")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), task_io.NON_OWNER_REFUSED)
        self.assertEqual(self.w.records(), [])

    def test_a_room_keeps_its_session_and_replayed_history_is_not_the_answer(self):
        self.w.task("task-1", "STUB:TEXT one")
        self.w.serve()
        self.w.task("task-2", "STUB:TEXT two")
        self.w.serve()
        self.assertIn("session/load", self.w.methods())
        self.assertEqual(self.w.result("task-2"), "hello from gemini-stub\n")
        rec = acp_host.SessionStore(self.w.root / "state" / gc.SESSIONS_NAME).get(("!room:s.space", "owner"))
        self.assertEqual((rec["session_id"], rec["turns"]), ("s-1", 2))

    def test_message_and_event_prompts_say_how_to_answer(self):
        msg = gc.wake_prompt("task-1", {"access_tier": "owner"}, "hi", [], "/w")
        self.assertIn("what Sparrow posts as the reply", msg)
        self.assertIn("exactly NO_REPLY", msg)
        self.assertIn("You may read and edit files under /w.", msg)
        ev = gc.wake_prompt("task-1", {"access_tier": "owner", "event_type": "room.joined"}, "hi", ["op line"], "/w")
        self.assertIn("platform event room.joined", ev)
        self.assertIn("post in your final message only what the body asks", ev)
        self.assertIn("op line", ev)

    def test_failures_are_said_for_a_message_and_quiet_for_an_event(self):
        c = gc.Connector(self.w.args())
        msg, ev = {"access_tier": "owner"}, {"access_tier": "owner", "event_type": "room.joined"}
        self.assertEqual(c.answer_body(msg, "completed", "", "end_turn"), "Gemini finished without an answer.\n")
        self.assertTrue(c.answer_body(ev, "completed", "", "end_turn").startswith("[no-send]"))
        self.assertEqual(c.answer_body(ev, "completed", "hello room", "end_turn"), "hello room\n")
        self.assertIn("Gemini stopped before answering (gone)", c.answer_body(msg, "gone", "", "gone"))
        self.assertIn("Partial reply:\npart", c.answer_body(msg, "failed", "part", "refusal"))
        self.assertEqual(parse_markers(c.answer_body(msg, "failed", "", "[file: /etc/passwd]")).actions, [])
        failed = c.answer_body(ev, "failed", "", "[no-send]x")
        self.assertEqual([a.kind for a in parse_markers(failed).actions], ["skip"])

    def test_antigravity_is_a_runtime_with_its_own_command_and_name(self):
        a = gc.build_parser().parse_args(["--work", str(self.w.root), "--runtime", "antigravity"])
        gc._prepare(a)
        gc._paths(a, str(self.w.root))
        self.assertEqual(a.acp_argv, ["agy", "acp"])
        self.assertEqual(gc.Connector(a).name, "Antigravity")
        self.assertIn("Antigravity is not signed in", gc.not_signed_in("antigravity"))
        self.assertIn("run `agy` once", gc.not_signed_in("antigravity"))

    def test_a_task_whose_publication_fails_is_retried_without_stopping_the_others(self):
        self.w.task("task-1", "STUB:TEXT hi")
        a = self.w.args()
        a.once, a.idle_exit = 0, 0.3
        with mock.patch.object(gc, "publish", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(io.StringIO()):
            gc.Connector(a).serve()
        self.assertIsNone(self.w.result("task-1"))
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "hello from gemini-stub\n")


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = Work(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *extra):
        argv = ["--work", str(self.w.root), "--cwd", str(self.w.cwd), "--acp-command",
                shlex.join([sys.executable, STUB]), "--poll", "0.01", *extra]
        with contextlib.redirect_stderr(io.StringIO()):
            return gc.main(argv)

    def test_main_takes_the_lock_and_serves(self):
        self.w.task("task-1", "STUB:TEXT hi")
        with mock.patch.dict(os.environ, {"HOME": str(self.w.home)}):
            self.assertEqual(self.main("--once", "1"), 0)
        self.assertEqual(self.w.result("task-1"), "hello from gemini-stub\n")

    def test_main_refuses_a_held_root_and_an_empty_command(self):
        owner = task_io.acquire_lock(str(self.w.root), "other")
        try:
            self.assertNotEqual(self.main("--once", "1"), 0)
        finally:
            task_io.release_lock(owner)
        with self.assertRaises(SystemExit):
            gc._prepare(gc.build_parser().parse_args(["--work", "w", "--acp-command", " "]))

    def test_held_starts_only_when_the_holder_names_this_process(self):
        me = {"pid": os.getpid(), "live": True, "start": task_io.start_time(os.getpid())}
        for consumer, want in ((me, 0), (None, 2)):
            fd = os.open(str(self.w.root), os.O_RDONLY)
            here = os.getcwd()
            try:
                with mock.patch.object(gc, "holder_consumer", lambda w: consumer), \
                        mock.patch.object(gc, "HELD_WAIT_S", 0.1), mock.patch.object(gc, "serve_locked", lambda a: 0):
                    self.assertEqual(self.main("--held", "--root-fd", str(fd), "--once", "1"), want)
            finally:
                os.chdir(here)
                os.close(fd)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            gc.main(["--work", "w", "--held"])


def quiet(fn, *a):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return fn(*a), out.getvalue()


class Patches(contextlib.ExitStack):
    def set(self, obj, **attrs):
        for k, v in attrs.items():
            self.enter_context(mock.patch.object(obj, k, v))
        return self


class InstallVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.home.mkdir()
        (self.home / "signed-in").write_text("")
        self.bin = t / "bin"
        self.bin.mkdir()
        self.gemini = self.bin / "gemini"
        self.gemini.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(STUB)))
        self.gemini.chmod(0o755)
        self.work = t / "work"
        (self.work / "tasks").mkdir(parents=True)
        (self.work / "results").mkdir()
        self.cwd = t / "cwd"
        self.cwd.mkdir()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "%s:%s" % (self.bin, os.environ["PATH"])})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def install(self, *extra):
        seen = {}

        def fake_install(shape, values, root, **kw):
            seen.update(shape=shape, values=values, root=root)
            return 0
        out = io.StringIO()
        with mock.patch.object(cm, "install", fake_install), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = cli.main(["install", "--work", str(self.work), *extra])
        return rc, seen, out.getvalue()

    def test_install_checks_the_sign_in_and_records_the_runtime_and_command(self):
        rc, seen, out = self.install()
        self.assertEqual(rc, 0, out)
        v = seen["values"]
        self.assertEqual(shlex.split(v["ACP_COMMAND"]), [str(self.gemini), "--acp"])
        self.assertEqual(v["RUNTIME"], "gemini")
        self.assertEqual(v["CWD"], str(Path(seen["root"]) / "workspace"))
        self.assertEqual(v["INBOX_DIR"], str(Path(seen["root"]) / "tasks"))
        self.assertEqual(seen["shape"].name, "gemini-local")
        self.assertIn("session/new", [json.loads(x).get("method") for x in (self.home / "stub.log").read_text().splitlines()])
        rc, seen, out = self.install("--cwd", str(self.cwd), "--session-store", str(self.cwd / "s.json"))
        self.assertEqual((seen["values"]["CWD"], seen["values"]["SESSION_STORE"]),
                         (str(self.cwd.resolve()), str((self.cwd / "s.json").resolve())))

    def test_install_refuses_when_gemini_is_not_signed_in(self):
        (self.home / "signed-in").unlink()
        rc, seen, out = self.install()
        self.assertEqual((rc, seen), (cm.EXIT_UNUSABLE, {}))
        self.assertIn("Gemini is not signed in on this computer (session/new: Gemini API key is missing", out)

    def test_install_refuses_without_gemini_on_path_or_with_a_bad_cwd(self):
        rc, seen, out = self.install("--acp-command", "no-such-gemini --acp")
        self.assertEqual((rc, seen), (cm.EXIT_UNUSABLE, {}))
        self.assertIn("not on PATH; install Gemini", out)
        rc, seen, out = self.install("--runtime", "antigravity")
        self.assertIn("agy is not on PATH; install Antigravity", out)
        rc, seen, out = self.install("--cwd", str(self.cwd / "missing"))
        self.assertEqual((rc, seen), (cm.EXIT_UNUSABLE, {}))

    def test_a_pasted_token_install_checks_the_sign_in_once_before_the_token_is_written(self):
        seen = {}
        args = argparse.Namespace(work=None, token_env_file=None, token_stdin=True, agent="@a:x", key=None,
                                  single=False, runtime="gemini", acp_command=None, cwd=None, session_store=None,
                                  allow_non_owner=False, argv=[], parser=None)
        with Patches() as p:
            p.set(cli.cb, install_token_stdin=lambda a, shape, pre, inst, cmd, **k: (seen.update(pre=pre()), 0)[1],
                  host_problem=lambda c: "")
            self.assertEqual(cli.install(args), 0)
        self.assertEqual(seen["pre"], "")
        self.assertTrue(args.signed_in, "the install that follows does not start Gemini again")
        with mock.patch.object(cli, "probe", side_effect=AssertionError("probed twice")):
            self.assertEqual(cli.signed_in_problem(args, "x"), "")
        (self.home / "signed-in").unlink()
        args.signed_in = False
        with Patches() as p:
            p.set(cli.cb, install_token_stdin=lambda a, shape, pre, inst, cmd, **k: (seen.update(pre=pre()), 1)[1],
                  host_problem=lambda c: "")
            cli.install(args)
        self.assertIn("not signed in", seen["pre"])

    def test_the_connector_runs_held_with_the_recorded_settings_and_a_narrow_env(self):
        cfg = {"RUNTIME": "antigravity", "ACP_COMMAND": "%s --acp" % self.gemini, "CWD": str(self.cwd),
               "SESSION_STORE": "/s.json", "ALLOW_NON_OWNER": "1"}
        argv = " ".join(cli.connector_argv(cfg, str(self.work), 7))
        for part in ("-m %s" % gc.__name__, "--held --root-fd 7", "--runtime antigravity",
                     "--acp-command %s --acp" % self.gemini, "--session-store /s.json", "--allow-non-owner"):
            self.assertIn(part, argv)
        env = cli.child_env(cfg, {"PATH": "/usr/bin", "HOME": "/h", "GEMINI_API_KEY": "x", "SECRET_TOKEN": "y"})
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.bin))
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("GEMINI_API_KEY", env, "a key reaches Gemini from ~/.gemini/.env, not the unit")

    def write_config(self):
        values = {"RUNTIME": "gemini", "ACP_COMMAND": shlex.join([str(self.gemini), "--acp"]), "CWD": str(self.cwd),
                  "INBOX_DIR": str(self.work / "tasks"), "RESULTS_DIR": str(self.work / "results"),
                  "ROOT": str(self.work)}
        path = cli.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cm.render_config(cli.LOCAL, values))
        path.chmod(0o600)

    def verify(self, *extra):
        return quiet(cli.main, ["verify", *extra])

    def test_verify_reports_the_sign_in_and_each_part(self):
        self.write_config()
        rc, out = self.verify()
        self.assertIn("PASS acp command:", out)
        self.assertIn("PASS sign-in: gemini-cli 0.59.0, signed in, protocol 1, session resume yes", out)
        self.assertIn("PASS session cwd:", out)
        self.assertIn("INFO runtime: Gemini", out)
        self.assertEqual(rc, 1, "no unit is installed in this test, so the tail fails")
        (self.home / "signed-in").unlink()
        rc, out = self.verify()
        self.assertIn("FAIL sign-in: Gemini is not signed in on this computer", out)
        rc, out = self.verify("--no-probe")
        self.assertNotIn("sign-in", out)

    def test_verify_fails_when_gemini_does_not_start_or_answer(self):
        self.write_config()
        self.gemini.write_text("#!/bin/sh\nexit 3\n")
        rc, out = self.verify()
        self.assertIn("FAIL sign-in:", out)
        self.assertIn("did not answer", out)
        said, why = cli.probe({"ACP_COMMAND": "/x --acp"}, factory=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
        self.assertIn("did not start", why)
        self.gemini.unlink()
        rc, out = self.verify()
        self.assertIn("FAIL acp command:", out)

    def test_run_is_the_managers_and_hands_the_connector_to_the_holder_sequence(self):
        self.assertEqual(quiet(cli.main, ["run"])[0], cm.EXIT_UNUSABLE, "refused without the unit's marker")
        got = {}
        with Patches() as p:
            p.set(cm, job_from_env=lambda shape: (shape, ""), managed_problem=lambda shape: "",
                  run_launchd=lambda argv, popen: (got.update(launchd=argv), 0)[1])
            self.assertEqual(cli.main(["run", "--launchd"]), 0)
        self.assertIn(cli.__name__, " ".join(map(str, got["launchd"])))
        cfg = {"RUNTIME": "gemini", "ACP_COMMAND": "%s --acp" % self.gemini, "CWD": str(self.cwd),
               "ROOT": str(self.work)}

        class Op:
            def __enter__(self):
                return self

            def release(self):
                got["released"] = True

        def hold(shape, root, state, prepare, spawn, supervise, kill, *a, **k):
            ctx, _ = prepare(argparse.Namespace(root_fd=5), root)
            got["child"] = spawn(ctx)
            return supervise("child")
        with Patches() as p:
            p.set(cli, selected=lambda a: (cli.LOCAL, ""), load_config=lambda local=None: (cfg, ""))
            p.set(cli.task_io, make_dirs=lambda owner, dirs: got.update(dirs=dirs), root_path=lambda owner, root: root)
            p.set(cm, operation_lock=lambda local: Op(), hold_and_supervise=hold,
                  supervise_child=lambda child, state, *a: cm.EXIT_HELD, retired=lambda state, rc: rc)
            rc = quiet(cli.run, argparse.Namespace(debug=True, launchd=False),
                       lambda argv, **kw: (argv, kw["pass_fds"]))[0]
        self.assertEqual(rc, cm.EXIT_CHILD_HELD, "the connector's own 75 is reported as 1")
        self.assertTrue(got["released"])
        self.assertIn("workspace", got["dirs"], "the default Session directory is made under the held root")
        argv, fds = got["child"]
        self.assertEqual((argv[argv.index("--root-fd") + 1], fds), ("5", (5,)))

    def test_verify_and_uninstall_delegate_and_refuse_without_a_job(self):
        with Patches() as p:
            p.set(cli, selected=lambda a: (None, "no job"))
            self.assertEqual(quiet(cli.verify, argparse.Namespace())[0], cm.EXIT_UNUSABLE)
            self.assertEqual(quiet(cli.uninstall, argparse.Namespace())[0], cm.EXIT_UNUSABLE)
        seen = {}
        with Patches() as p:
            p.set(cli.cb, unwire_bridge=lambda cfg, cmd, bridge: seen.setdefault("unwired", cmd))
            p.set(cm, uninstall=lambda local, before_free, token_file: before_free({}) and 0)
            self.assertEqual(quiet(cli.main, ["uninstall"])[0], 0)
        self.assertEqual(seen["unwired"], cli.COMMAND)


if __name__ == "__main__":
    unittest.main(verbosity=2)
