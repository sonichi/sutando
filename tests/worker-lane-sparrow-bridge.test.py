#!/usr/bin/env python3
"""One worker wiring, two bridge entry points: `src/worker_lane.py`.

- The shared module: `install` sets the worker hooks in a bridge namespace (addressee, claim and own-file
  gates, and the pre-publish router when a task-event handler is declared); `serving` answers which pool
  worker a connection row names an agent as, and why one is not served; `bridge_overlay` is the lane's dirs.
- `worker_lane.py declare|withdraw`: the declaration sparrowd runs a worker's agent bridge from.
- `sparrow_agent_bridge`: a worker declaration runs as that worker's lane does, with the hooks set before
  the package's main(); a claude-local declaration is run as before, with none; a worker declaration whose
  agent the record does not name as a servable worker is skipped with its reason.

Run: python3 tests/worker-lane-sparrow-bridge.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
_spec = importlib.util.spec_from_file_location("connections_contract_wl", ROOT / "tests" / "connections.test.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
C = H.C

import sparrow_agent_bridge as bridge
import worker_lane
from ag2_sparrow import _dirs, local_task_protocol
from ag2_sparrow.adapters.claude_code import cli

W = "7c700e11" + "0" * 24
CORE_AGENT, WORKER_AGENT = "@sutando:alpha-fixture.net", "@reviewer:alpha-fixture.net"
TOKEN_W = "https://chat.alpha-fixture.net|secret-reviewer"
SEG = bridge.agent_identity.root_segment(WORKER_AGENT)


class Base(H.Base):
    def setUp(self):
        super().setUp()
        self.add(H.TOKEN_A, fetch=H.agents(CORE_AGENT))
        preview = C.preview_connection(TOKEN_W, fetch=H.agents(WORKER_AGENT))
        self.row, _ = C.add_worker_identity(W, TOKEN_W, expected_server=preview["server"], preview=preview,
                                            workspace=self.ws, vault=self.vault)
        self.agents = self.ws / "agents"
        env = mock.patch.dict(os.environ)  # the bridge clears and refills os.environ
        env.start()
        self.addCleanup(env.stop)
        saved = (_dirs._injected, local_task_protocol._TASK_STAMPER)

        def restore():
            _dirs._injected, local_task_protocol._TASK_STAMPER = saved
        self.addCleanup(restore)

    def edit_worker_row(self, **fields):
        path = C.record_path(self.ws)
        rec = json.loads(path.read_text())
        for r in rec["connections"]:
            if r.get("worker") == W:
                r.update(fields)
        path.write_text(json.dumps(rec))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = worker_lane.main(list(argv), workspace=self.ws)
        return rc, out.getvalue() + err.getvalue()

    def declare(self):
        rc, said = self.cli("declare", "--worker", W)
        self.assertEqual(rc, 0, said)
        return self.agents / f"{SEG}.bridge" / "worker.json"

    def declare_handler(self):
        handler = self.ws / "handler.sh"
        handler.write_text("#!/bin/sh\nexit 0\n")
        handler.chmod(0o755)
        (self.ws / "state" / "task-event-handler.json").write_text(json.dumps({"handler": str(handler)}))
        return handler


class Serving(Base):
    def test_a_workers_agent_is_served_as_that_worker_on_its_rows_lane(self):
        self.assertEqual(worker_lane.serving(WORKER_AGENT, self.ws), (W, self.row["instance"], ""))

    def test_a_parked_row_is_still_served(self):
        self.edit_worker_row(parked=True)
        self.assertEqual(worker_lane.serving(WORKER_AGENT, self.ws)[0], W)

    def test_why_an_agent_is_not_served(self):
        self.assertIn("is no pool worker's own agent", worker_lane.serving(CORE_AGENT, self.ws)[2])
        self.assertIn("is no pool worker's own agent", worker_lane.serving(WORKER_AGENT + " ", self.ws)[2])
        self.edit_worker_row(owner_mxid="@someone-else:alpha-fixture.net")
        self.assertEqual(worker_lane.serving(WORKER_AGENT, self.ws)[:2], ("", ""))
        self.assertIn("not the core's owner", worker_lane.serving(WORKER_AGENT, self.ws)[2])

    def test_a_disabled_row_is_not_served(self):
        self.edit_worker_row(disabled=True)
        self.assertIn("row is disabled", worker_lane.serving(WORKER_AGENT, self.ws)[2])

    def test_two_rows_or_no_instance_are_not_served(self):
        rows = C._read(C.record_path(self.ws))
        worker = next(r for r in rows if r.get("worker"))
        twin = dict(worker, worker="b" * 32, instance="other")
        with mock.patch.object(C, "_read", return_value=rows + [twin]):
            self.assertIn("2 worker rows name agent", worker_lane.serving(WORKER_AGENT, self.ws)[2])
        bare = [dict(r, instance="") if r.get("worker") else r for r in rows]
        with mock.patch.object(C, "_read", return_value=bare):
            self.assertIn("names no lane instance", worker_lane.serving(WORKER_AGENT, self.ws)[2])

    def test_an_unreadable_record_is_unknown_not_no_worker(self):
        C.record_path(self.ws).write_text("{torn")
        with self.assertRaises(worker_lane.RecordUnreadable):
            worker_lane.serving(WORKER_AGENT, self.ws)

    def test_the_overlay_is_the_lanes_dirs_and_instance(self):
        self.assertEqual(worker_lane.bridge_overlay(self.ws, "lane-x"), {
            "AGENT_CONNECT_TASK_DIR": str(self.ws / "tasks"), "AGENT_CONNECT_RESULT_DIR": str(self.ws / "results"),
            "AGENT_CONNECT_STATE_DIR": str(self.ws / "state"), "GATEWAY_INSTANCE": "lane-x",
            "REMOTE_MEDIA_DIR": str(self.ws / "data" / "remote-media")})


class Install(Base):
    def test_install_sets_the_worker_hooks_and_the_router(self):
        handler = self.declare_handler()
        ns, logged = {}, []
        worker_lane.install(ns, W, workspace=self.ws, results_dir=self.ws / "results", log=logged.append, who="b1")
        self.assertEqual(ns["TASK_ADDRESSEE"](), W)
        self.assertIs(ns["PROACTIVE_CLAIM_GATE"], ns["PROACTIVE_OWN_FILE"])
        self.assertTrue(callable(ns["TASK_PRE_PUBLISH"]))
        self.assertIn(f"b1 runs as worker {W}'s own agent", logged[0])
        self.assertEqual(worker_lane.route_argv(self.ws, self.ws / "results"),
                         [str(handler), "--workspace", str(self.ws), "--results-dir", str(self.ws / "results"),
                          "--from-lane"])

    def test_without_a_handler_there_is_no_router(self):
        ns, logged = {}, []
        worker_lane.install(ns, W, workspace=self.ws, results_dir=self.ws / "results", log=logged.append, who="b1")
        self.assertNotIn("TASK_PRE_PUBLISH", ns)
        self.assertTrue(any("no task-event handler is declared" in line for line in logged), logged)

    def test_the_claim_gate_takes_only_its_workers_stamped_files(self):
        from proactive_routing import proactive_filename
        gate = worker_lane.claim_gate(W)
        self.assertTrue(gate(Path(proactive_filename(1, "ag2space", W))))
        self.assertFalse(gate(Path("proactive-1.txt")))

    def test_the_router_reports_a_refusal_and_runs_the_re_raise(self):
        logged = []
        route = worker_lane.router(W, ["router"], logged.append)
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 3)):
            self.assertIsNone(route("t1", Path("s"), Path("d")))
        self.assertTrue(any("t1 not delivered to it (router exit 3)" in line for line in logged), logged)
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            route("t2", Path("s"), Path("d"))()
        self.assertEqual(run.call_args.args[0], ["router", "--task-file", "d", "--renotify"])


class Declare(Base):
    def test_declare_writes_what_the_agent_bridge_loads_as_this_worker(self):
        decl = self.declare()
        body = json.loads(decl.read_text())
        self.assertEqual((body["written_by"], body["host"], body["name"]),
                         (worker_lane.WRITTEN_BY, "sparrowd", cli.worker_name(SEG)))
        loaded, why = bridge.load(decl, self.agents)
        self.assertEqual(why, "")
        self.assertEqual((loaded["worker"], loaded["instance"]), (W, self.row["instance"]))
        rc, said = self.cli("declare", "--worker", W)
        self.assertEqual(rc, 0, "declaring again rewrites its own file")
        self.assertIn(cli.vault_key(SEG), said)

    def test_withdraw_removes_only_its_own(self):
        decl = self.declare()
        self.assertEqual(self.cli("withdraw", "--worker", W), (0, f"removed: {decl}\n"))
        self.assertEqual(self.cli("withdraw", "--worker", W)[1], f"nothing declared at {decl}\n")
        decl.mkdir(parents=True)
        for action in ("declare", "withdraw"):
            rc, said = self.cli(action, "--worker", W)
            self.assertEqual(rc, 1)
            self.assertIn("is a directory", said)

    def test_declare_is_refused_for_what_is_no_servable_worker(self):
        self.assertIn("is not a worker id", self.cli("declare", "--worker", "../x")[1])
        self.assertIn("has no agent identity row", self.cli("declare", "--worker", "b" * 32)[1])
        self.edit_worker_row(disabled=True)
        rc, said = self.cli("declare", "--worker", W)
        self.assertEqual(rc, 1)
        self.assertIn("row is disabled", said)
        self.assertFalse(self.agents.exists(), "nothing written")
        with mock.patch.object(worker_lane, "serving", side_effect=worker_lane.RecordUnreadable("gone")):
            self.edit_worker_row(disabled=False)
            self.assertEqual(self.cli("declare", "--worker", W), (1, "worker_lane: declare refused: gone\n"))


    def test_the_workspace_defaults_to_the_resolved_one(self):
        import workspace_default
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker_lane.main(["declare", "--worker", W]), 0)
        self.assertTrue((self.agents / f"{SEG}.bridge" / "worker.json").is_file())


class TheAgentBridge(Base):
    def vault_get(self):
        return {cli.vault_key(SEG): TOKEN_W}.__getitem__

    def test_a_worker_declaration_runs_as_its_lane_with_the_hooks_set_before_main(self):
        handler = self.declare_handler()
        decl = self.declare()
        seen, started = {}, []

        def fake_run(module, run_name, alter_sys):
            seen.update(module=module, run_name=run_name, alter_sys=alter_sys, task=_dirs.task_dir(),
                        result=_dirs.result_dir(), state=_dirs.state_dir(),
                        instance=os.environ.get("GATEWAY_INSTANCE"), mxid=os.environ.get("AGENT_MXID"),
                        token=os.environ.get("REMOTE_TASK_TOKEN"))
            ns = {"RESULTS_DIR": self.ws / "results", "_log": lambda line: None}
            ns["main"] = lambda: started.append(dict(ns))
            return ns
        rc = bridge.main([str(decl)], workspace=self.ws, get_key=self.vault_get(), run=fake_run)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"module": bridge.BRIDGE_MODULE, "run_name": bridge.BRIDGE_MODULE, "alter_sys": False,
                                "task": self.ws / "tasks", "result": self.ws / "results", "state": self.ws / "state",
                                "instance": self.row["instance"], "mxid": WORKER_AGENT, "token": TOKEN_W})
        self.assertEqual(len(started), 1, "the package's main() runs once, after the hooks")
        ns = started[0]
        self.assertEqual(ns["TASK_ADDRESSEE"](), W)
        self.assertIs(ns["PROACTIVE_CLAIM_GATE"], ns["PROACTIVE_OWN_FILE"])
        self.assertTrue(callable(ns["TASK_PRE_PUBLISH"]))
        self.assertTrue(handler.is_file())

    def test_a_claude_local_declaration_for_the_same_agent_is_run_as_before(self):
        root = self.agents / SEG
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": WORKER_AGENT, "TOKEN_VAULT_KEY": cli.vault_key(SEG)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        decl = cli.bridge_home(root) / cli.DECLARATION
        C.record_path(self.ws).write_text("{torn")
        loaded, why = bridge.load(decl, self.agents)
        self.assertEqual(why, "", "the record is never read for it")
        self.assertNotIn("worker", loaded)
        calls = []
        rc = bridge.main([str(decl)], workspace=self.ws, get_key=self.vault_get(),
                         run=lambda module, run_name, alter_sys: calls.append((run_name, alter_sys, _dirs.task_dir().resolve())))
        self.assertEqual((rc, calls), (0, [("__main__", True, (root / "tasks").resolve())]))

    def test_a_worker_declaration_the_record_does_not_serve_is_skipped_with_its_reason(self):
        decl = self.declare()
        for fields, why in (({"disabled": True}, "row is disabled"),
                            ({"owner_mxid": "@other:alpha-fixture.net"}, "not the core's owner"),
                            ({"agent_mxid": "@moved:alpha-fixture.net"}, "is no pool worker's own agent")):
            with self.subTest(why=why):
                good = C.record_path(self.ws).read_text()
                self.edit_worker_row(**fields)
                found, skipped = bridge.declarations(self.ws, [cli.vault_key(SEG)])
                self.assertEqual(found, [])
                self.assertIn(why, " ".join(skipped))
                run = mock.Mock()
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(bridge.main([str(decl)], workspace=self.ws, get_key=self.vault_get(), run=run), 2)
                run.assert_not_called()
                self.assertIn(why, err.getvalue())
                C.record_path(self.ws).write_text(good)

    def test_an_unreadable_record_holds_the_running_bridge(self):
        self.declare()
        C.record_path(self.ws).write_text("{torn")
        held = []
        found, skipped = bridge.declarations(self.ws, [cli.vault_key(SEG)], held)
        self.assertEqual((found, held), ([], [cli.worker_name(SEG)]))
        self.assertIn("connection record unreadable", skipped[0])

    def test_the_worker_writer_declares_no_seat(self):
        decl = self.declare()
        with mock.patch.object(bridge._sources, "is_seat", return_value=True):
            self.assertIn("declares no seat's bridge", bridge.load(decl, self.agents)[1])

    def test_a_worker_declaration_is_checked_like_any_other(self):
        decl = self.declare()
        body = json.loads(decl.read_text())
        decl.write_text(json.dumps(dict(body, env=dict(body["env"], AGENT_CONNECT_TASK_DIR=str(self.ws / "tasks")))))
        self.assertIn("env is not the canonical one", bridge.load(decl, self.agents)[1])


if __name__ == "__main__":
    unittest.main()
