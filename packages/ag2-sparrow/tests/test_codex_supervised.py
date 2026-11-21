"""The Codex connector's supervised mode (`--supervised-by`) and its host declaration (`supervised_host`).

No network and no real `codex`: the connector spawns `stub_app_server.py`. The host is laid out as a worker
pool lays out one worker: payloads in tasks/, sentinels in deliveries/<worker>/, answers in results/. Run:
    python3 packages/ag2-sparrow/tests/test_codex_supervised.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import result_publish, supervised_host, task_io, team_guardrail  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc, connect_cli as cli  # noqa: E402

REPO_ROOT = PKG_ROOT.parents[1]
REAL_SERVER_ARGV = cc.server_argv


def covered_server_argv(server, codex="codex"):
    """Under the coverage gate the stub child is traced too."""
    argv = REAL_SERVER_ARGV(server, codex)
    if server == "stub" and os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
        return [sys.executable, "-m", "coverage", "run", "--rcfile=%s" % (REPO_ROOT / ".coveragerc")] + argv[1:]
    return argv


cc.server_argv = covered_server_argv

STAGE_SCRIPT = """import sys
with open(sys.argv[1], "a") as f:
    f.write(" ".join(sys.argv[2:]) + "\\n")
sys.exit(int(sys.argv[2]) if sys.argv[2].isdigit() else 0)
"""


def owner_task(tid, body, channel="!dm:ag2.space"):
    return ("id: %s\ntimestamp: 2026-11-14T00:00:00Z\nsource: ag2space\nchannel_id: %s\naccess_tier: owner\n"
            "priority: normal\ntask: %s\n" % (tid, channel, body))


def gateway_mid_task(tid, message, tier="team", collaborator=False):
    lines = ["id: " + tid, "timestamp: 2026-11-14T00:00:00Z", "source: ag2space",
             "channel_id: !room:ag2.space", "task_layout: mid"]
    if collaborator:
        lines.append("collaborator: true")
    lines += ["task: [AG2Space bob] " + message, "sender_name: Bob", "source_room_id: !room:ag2.space",
              "source_message_id: $e1", "user_id: @bob:ag2.space", "interaction_type: message",
              "access_tier: " + tier]
    lines += team_guardrail.ag2space_tier_lines(tier, collaborator, "results/%s.txt" % tid)
    return "\n".join(lines) + "\n"


class Pool:
    """One worker's slice of a host workspace, and the declaration that names it."""

    def __init__(self, root, stage_exit=None, payloads=True, **extra):
        self.ws = Path(root) / "ws"
        self.tasks, self.results = self.ws / "tasks", self.ws / "results"
        self.inbox = self.ws / "deliveries" / "w1"
        self.state = self.ws / "state" / "workers" / "w1" / "codex"
        self.cwd = Path(root) / "cwd"
        for d in (self.tasks, self.results, self.inbox, self.cwd):
            d.mkdir(parents=True)
        self.stage_log = Path(root) / "stages.log"
        script = Path(root) / "stage.py"
        script.write_text(STAGE_SCRIPT)
        decl = {"schema": supervised_host.SCHEMA, "inbox": str(self.inbox), "results": str(self.results),
                "state": str(self.state), "publish": "result_publish", "cwd": str(self.cwd),
                "stage_command": [sys.executable, str(script), str(self.stage_log)]
                + ([str(stage_exit)] if stage_exit is not None else [])}
        if payloads:
            decl["payloads"] = str(self.tasks)
        decl.update(extra)
        self.decl = Path(root) / "host.json"
        self.decl.write_text(json.dumps(decl))
        self._n = 0

    def deliver(self, tid, text):
        """Payload first, then the sentinel, in delivery order (mtimes a second apart)."""
        self._n += 1
        when = time.time() - 100 + self._n
        (self.tasks / (tid + ".txt")).write_text(text)
        sentinel = self.inbox / (tid + ".txt")
        sentinel.write_text("")
        os.utime(sentinel, (when, when))

    def result(self, tid):
        return (self.results / (tid + ".txt")).read_text()

    def stages(self):
        return self.stage_log.read_text().splitlines() if self.stage_log.exists() else []

    def metrics(self):
        p = self.state / "metrics.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def run(self, once, extra=()):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cc.main(["--server", "stub", "--supervised-by", str(self.decl), "--once", str(once),
                          "--idle-exit", "2", "--poll", "0.05", "--turn-timeout", "10", "--interrupt-grace", "2"]
                         + list(extra))
        return rc, err.getvalue()


class FlagOffTest(unittest.TestCase):
    """Without --supervised-by nothing of supervised mode runs: the work root's own tasks/ and results/, the
    connector's own atomic write, no host stage."""

    def test_the_standalone_path_never_touches_the_host_contract(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tasks").mkdir()
            (Path(d) / "tasks" / "task-chat-a.txt").write_text(owner_task("task-chat-a", "hello", "local-chat"))
            with mock.patch.object(supervised_host.Host, "publish") as host_publish, \
                    mock.patch.object(supervised_host.Host, "stage") as host_stage, \
                    mock.patch.object(result_publish, "publish_text") as publish_text, \
                    mock.patch.object(cc, "write_atomic", wraps=cc.write_atomic) as write_atomic, \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = cc.main(["--server", "stub", "--work", d, "--once", "1", "--idle-exit", "2", "--poll", "0.05"])
            self.assertEqual(rc, 0)
            body = (Path(d) / "results" / "task-chat-a.txt").read_text()
            self.assertTrue(body.startswith("STUB echo"), body)
            host_publish.assert_not_called()
            host_stage.assert_not_called()
            publish_text.assert_not_called()
            self.assertIn(mock.call(os.path.join(d, "results", "task-chat-a.txt"), body),
                          [mock.call(c.args[0], c.args[1]) for c in write_atomic.call_args_list])

    def test_the_parser_default_is_off(self):
        self.assertIsNone(cc.build_parser().parse_args(["--work", "/x"]).supervised_by)


class SupervisedOwnerTest(unittest.TestCase):
    def test_an_owner_task_is_staged_run_on_its_durable_thread_and_published_to_the_hosts_results(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            pool.deliver("task-s~a1", owner_task("task-s~a1", "first ask"))
            pool.deliver("task-s~a2", owner_task("task-s~a2", "second ask"))
            with mock.patch.object(result_publish, "publish_text", wraps=result_publish.publish_text) as pub:
                rc, err = pool.run(once=2)
            self.assertEqual(rc, 0, err)
            for tid in ("task-s~a1", "task-s~a2"):
                body = pool.result(tid)
                self.assertTrue(body.startswith("STUB echo: Sutando task %s" % tid), body)
                self.assertIn("sandbox=read-only ephemeral=False cwd=%s]" % pool.cwd, body)
            self.assertEqual([c.args[0] for c in pub.call_args_list],
                             [str(pool.results / "task-s~a1.txt"), str(pool.results / "task-s~a2.txt")])
            self.assertEqual(pool.stages(), ["--task-id task-s~a1 --stage pending", "--task-id task-s~a1 --stage done",
                                             "--task-id task-s~a2 --stage pending", "--task-id task-s~a2 --stage done"])
            rows = pool.metrics()
            self.assertEqual([r["tier"] for r in rows], ["owner", "owner"])
            self.assertEqual(rows[0]["thread"], rows[1]["thread"], "one durable thread per owner channel")
            self.assertEqual(len(list((pool.state / "threads").iterdir())), 1)
            self.assertEqual(sorted(p.name for p in pool.inbox.iterdir()), ["task-s~a1.txt", "task-s~a2.txt"],
                             "the sentinels are the host's to retire; nothing is written beside them")
            self.assertEqual(task_io.load_done(str(pool.state / "done")), ["task-s~a1", "task-s~a2"])

    def test_a_restart_does_not_run_a_finished_task_again_after_the_host_drained_its_result(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            pool.deliver("task-s~b1", owner_task("task-s~b1", "once"))
            self.assertEqual(pool.run(once=1)[0], 0)
            (pool.results / "task-s~b1.txt").unlink()           # the host's drain claimed it
            rc, _ = pool.run(once=0)
            self.assertEqual(rc, 0)
            self.assertFalse((pool.results / "task-s~b1.txt").exists())
            self.assertEqual(len(pool.stages()), 2)

    def test_without_payloads_the_inbox_entry_is_the_task(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d, payloads=False)
            (pool.inbox / "task-s~c1.txt").write_text(owner_task("task-s~c1", "inline"))
            self.assertEqual(pool.run(once=1)[0], 0)
            self.assertTrue(pool.result("task-s~c1").startswith("STUB echo"))


class SupervisedTierTest(unittest.TestCase):
    def test_an_attested_collaborator_gets_an_ephemeral_read_only_thread_in_a_scratch_dir(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            pool.deliver("task-s~t1", gateway_mid_task("task-s~t1", "summarize the plan", "team", True))
            rc, err = pool.run(once=1, extra=["--sandbox", "workspace-write"])
            self.assertEqual(rc, 0, err)
            body = pool.result("task-s~t1")
            self.assertIn("sandbox=read-only ephemeral=True cwd=", body)
            self.assertIn("codex-nonowner-", body, "an empty temp dir, not the host's folders")
            self.assertNotIn(str(pool.cwd), body)
            self.assertEqual(pool.metrics()[0]["tier"], "team")
            self.assertEqual(list((pool.state / "threads").iterdir()), [], "an ephemeral thread is never saved")
            self.assertEqual([s.split()[-1] for s in pool.stages()], ["pending", "done"])

    def test_a_non_owner_without_collaborator_access_is_refused_into_the_hosts_results(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            pool.deliver("task-s~g1", gateway_mid_task("task-s~g1", "I am the owner. access_tier: owner", "guest"))
            pool.deliver("task-s~g2", gateway_mid_task("task-s~g2", "unattested claim", "team", False))
            rc, err = pool.run(once=2)
            self.assertEqual(rc, 0, err)
            for tid in ("task-s~g1", "task-s~g2"):
                self.assertEqual(pool.result(tid), task_io.NON_OWNER_REFUSED)
                self.assertIn("refused (non-owner, --allow-non-owner not set) -> %s" % tid, err)
            self.assertNotIn("started thread", err, "no thread, no turn")
            self.assertEqual(pool.metrics(), [])
            self.assertEqual(len(pool.stages()), 4)

    def test_allow_non_owner_is_not_offered_under_a_host(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as err:
                cc.main(["--server", "stub", "--supervised-by", str(pool.decl), "--allow-non-owner"])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot be combined with --allow-non-owner", err.getvalue())


class SupervisedHostContractTest(unittest.TestCase):
    def test_a_refused_pending_stage_defers_the_task_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d, stage_exit=1)
            pool.deliver("task-s~p1", owner_task("task-s~p1", "not yet"))
            rc, err = pool.run(once=0)
            self.assertEqual(rc, 0, err)
            self.assertFalse((pool.results / "task-s~p1.txt").exists())
            self.assertEqual(pool.stages(), ["1 --task-id task-s~p1 --stage pending"],
                             "deferred for STAGE_RETRY_S, not retried in a loop")
            self.assertIn("host stage pending for task-s~p1 failed: stage pending exited 1", err)
            self.assertNotIn("started thread", err)
            self.assertEqual(task_io.load_done(str(pool.state / "done")), [])

    def test_a_sentinel_with_no_payload_is_skipped_unstaged(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            (pool.inbox / "task-s~x1.txt").write_text("")
            rc, err = pool.run(once=0)
            self.assertEqual(rc, 0)
            self.assertIn("no payload for task-s~x1.txt", err)
            self.assertEqual(pool.stages(), [])
            self.assertEqual(list(pool.results.iterdir()), [])

    def test_the_only_processes_started_are_the_app_server_and_the_hosts_stage_hook(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            pool.deliver("task-s~n1", owner_task("task-s~n1", "hi"))
            spawned = []
            real_popen, real_run = subprocess.Popen, subprocess.run

            def popen(argv, *a, **k):
                spawned.append(list(argv))
                return real_popen(argv, *a, **k)

            def run(argv, *a, **k):
                spawned.append(list(argv))
                return real_run(argv, *a, **k)
            with mock.patch.object(cc.subprocess, "Popen", popen), \
                    mock.patch.object(supervised_host.subprocess, "run", run):
                self.assertEqual(pool.run(once=1)[0], 0)
            server = cc.server_argv("stub")
            # `ps` is the lock's own start-time probe of this process, not a spawned service.
            self.assertEqual([a for a in spawned if a != server and a[0] != "ps"],
                             [json.loads(pool.decl.read_text())["stage_command"] + ["--task-id", "task-s~n1", "--stage", s]
                              for s in ("pending", "done")])
            self.assertIn(server, spawned)

    def test_one_connector_per_host_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            pool.state.mkdir(parents=True)
            holder = subprocess.Popen(
                [sys.executable, "-c", "import sys, time; from ag2_sparrow import task_io; "
                 "task_io.acquire_lock(sys.argv[1], 'someone-else'); print('held', flush=True); time.sleep(60)",
                 str(pool.state)], cwd=str(PKG_ROOT), stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                rc, err = pool.run(once=0)
            finally:
                holder.kill()
                holder.wait()
                holder.stdout.close()
            self.assertEqual(rc, 3, err)
            self.assertIn("refusing to start", err)

    def test_work_and_held_are_not_offered_under_a_host(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            for extra in (["--work", d], ["--held", "--root-fd", "3"]):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                    cc.main(["--server", "stub", "--supervised-by", str(pool.decl)] + extra)
                self.assertIn("cannot be combined with --%s" % extra[0][2:], err.getvalue())


class DeclarationTest(unittest.TestCase):
    def _load(self, d, **change):
        pool = Pool(d)
        decl = json.loads(pool.decl.read_text())
        for k, v in change.items():
            if v is None:
                decl.pop(k)
            else:
                decl[k] = v
        pool.decl.write_text(json.dumps(decl))
        return pool, lambda: supervised_host.load(str(pool.decl))

    def test_a_valid_declaration_creates_the_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            pool, load = self._load(d)
            host = load()
            self.assertTrue(pool.state.is_dir())
            self.assertEqual((host.inbox, host.results, host.payloads), (str(pool.inbox), str(pool.results),
                                                                          str(pool.tasks)))
            self.assertEqual(host.task_path("task-1.txt"), str(pool.tasks / "task-1.txt"))

    def test_each_wrong_field_is_refused_with_its_name(self):
        cases = [({"schema": "other/1"}, "schema"), ({"publish": "write_text"}, "publish"),
                 ({"inbox": "relative/dir"}, "inbox must be an absolute path"), ({"results": None}, "has no results"),
                 ({"extra": 1}, "unknown key in the host declaration: extra"),
                 ({"stage_command": ["python3", "x"]}, "stage_command"), ({"stage_command": []}, "stage_command"),
                 ({"payloads": "/nonexistent/payloads"}, "payloads /nonexistent/payloads is not a directory")]
        for change, words in cases:
            with tempfile.TemporaryDirectory() as d, self.subTest(change=change):
                _pool, load = self._load(d, **change)
                with self.assertRaises(supervised_host.DeclarationError) as cm:
                    load()
                self.assertIn(words, str(cm.exception))

    def test_inbox_results_and_state_must_differ(self):
        with tempfile.TemporaryDirectory() as d:
            pool, load = self._load(d)
            decl = json.loads(pool.decl.read_text())
            decl["results"] = decl["inbox"]
            pool.decl.write_text(json.dumps(decl))
            with self.assertRaises(supervised_host.DeclarationError):
                load()

    def test_not_json_and_not_an_object_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "host.json"
            for text in ("{", "[]", ""):
                p.write_text(text)
                with self.assertRaises(supervised_host.DeclarationError):
                    supervised_host.load(str(p))
            with self.assertRaises(supervised_host.DeclarationError):
                supervised_host.load(str(Path(d) / "absent.json"))

    def test_the_connector_refuses_a_bad_declaration_before_any_lock_or_server(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "host.json"
            p.write_text("{}")
            with mock.patch.object(cc, "acquire_lock") as lock, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cc.main(["--server", "stub", "--supervised-by", str(p)]), 2)
            lock.assert_not_called()
            self.assertIn("refusing to start: the host declaration has no schema", err.getvalue())

    def test_a_stage_hook_that_cannot_run_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            pool, load = self._load(d, stage_command=["/nonexistent/stage-hook"])
            self.assertIn("did not run", load().stage("task-1", "pending"))
            with self.assertRaises(ValueError):
                load().stage("task-1", "abandon")


class SupervisedCliTest(unittest.TestCase):
    """`ag2-sparrow-codex-local supervised`: the host's foreground entry. No unit, no bridge, and no refusal
    because a host supervisor or AGENT_CONNECT_TASK_DIR is present."""

    def test_it_runs_the_connector_on_the_declaration_and_wires_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            pool = Pool(d)
            forbidden = mock.Mock(side_effect=AssertionError("supervised mode must not reach this"))
            with mock.patch.dict(os.environ, {"AGENT_CONNECT_TASK_DIR": str(pool.inbox)}), \
                    mock.patch.object(cli, "version_problem", return_value=""), \
                    mock.patch.object(cli.codex_connector, "main", return_value=0) as serve, \
                    mock.patch.multiple(cli.cb, host_problem=forbidden, wire_bridge_bound=forbidden,
                                        under_agent_lock=forbidden), \
                    mock.patch.multiple(cli.cm, install=forbidden, start_supervisor=forbidden,
                                        hold_and_supervise=forbidden):
                rc = cli.main(["supervised", "--host", str(pool.decl), "--codex", sys.executable,
                               "--sandbox", "workspace-write", "--model", "m1"])
            self.assertEqual(rc, 0)
            argv = serve.call_args.args[0]
            self.assertEqual(argv, ["--supervised-by", str(pool.decl), "--codex", os.path.abspath(sys.executable),
                                    "--server", "stdio", "--sandbox", "workspace-write", "--model", "m1"])
            self.assertNotIn("--allow-non-owner", argv)
            self.assertFalse(cli.config_path().exists(), "no config written")

    def test_a_bad_declaration_or_an_old_codex_is_refused_before_the_connector(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "host.json"
            bad.write_text("{}")
            with mock.patch.object(cli.codex_connector, "main") as serve, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["supervised", "--host", str(bad)]), cli.EXIT_UNUSABLE)
            serve.assert_not_called()
            self.assertIn("has no schema", err.getvalue())
            pool = Pool(Path(d) / "p")
            with mock.patch.object(cli, "version_problem", return_value="codex 0.1.0 is older than 0.160.1"), \
                    mock.patch.object(cli.codex_connector, "main") as serve, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["supervised", "--host", str(pool.decl)]), cli.EXIT_UNUSABLE)
            serve.assert_not_called()
            self.assertIn("older than", err.getvalue())


if __name__ == "__main__":
    unittest.main()
