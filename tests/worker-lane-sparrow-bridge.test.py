#!/usr/bin/env python3
"""One worker wiring, two bridge entry points: `src/worker_lane.py`.

- The shared module: `install` sets the worker hooks in a bridge namespace (addressee, claim and own-file
  gates, and the pre-publish router when a task-event handler is declared); `serving` answers which pool
  worker a connection row names an agent as, and why one is not served; `bridge_overlay` is the lane's dirs.
- `worker_lane.py declare|withdraw`: the declaration sparrowd runs a worker's agent bridge from. It names
  no vault key: the bridge reads the row's own token, as the lane does.
- The lane keeper does not start a lane whose worker has a valid declared bridge, starts it again once the
  declaration is withdrawn, and the row stays enabled throughout, so the worker keeps its own identity.
  `connections.py stop <instance>` stops the running lane once.
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
            rc = worker_lane.main(list(argv), workspace=self.ws, vault=self.vault)
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

    def test_a_parked_row_is_not_served(self):
        self.edit_worker_row(parked=True)
        self.assertIn("row is parked", worker_lane.serving(WORKER_AGENT, self.ws)[2])

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

    def test_the_overlay_is_the_lanes_dirs_and_the_lanes_own_identity_env(self):
        lane = self.row["instance"]
        overlay = worker_lane.bridge_overlay(self.ws, lane)
        self.assertEqual({k: overlay[k] for k in ("AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR",
                                                   "AGENT_CONNECT_STATE_DIR", "REMOTE_MEDIA_DIR")},
                         {"AGENT_CONNECT_TASK_DIR": str(self.ws / "tasks"), "AGENT_CONNECT_RESULT_DIR": str(self.ws / "results"),
                          "AGENT_CONNECT_STATE_DIR": str(self.ws / "state"),
                          "REMOTE_MEDIA_DIR": str(self.ws / "data" / "remote-media")})
        launched = C.launch_env(C.plan_entry(lane, {}, workspace=self.ws), {}, vault=self.vault)
        lane_env = C.lane_identity_env(C.plan_entry(lane, workspace=self.ws))
        self.assertEqual({k: overlay[k] for k in lane_env}, {k: launched[k] for k in lane_env},
                         "the same identity env the lane is launched with")
        self.assertEqual((overlay["GATEWAY_INSTANCE"], overlay[C.LANE_WORKER_ENV]), (lane, W))
        self.edit_worker_row(parked=True)
        with self.assertRaises(worker_lane.NotServable):
            worker_lane.bridge_overlay(self.ws, lane)


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
        self.assertEqual(body["token_vault_key"], "", "no second copy of the token is named")
        rc, said = self.cli("declare", "--worker", W)
        self.assertEqual(rc, 0, "declaring again rewrites its own file")
        self.assertIn("with the token its connection row names", said)
        self.assertNotIn("vault set", said)

    def test_withdraw_removes_only_its_own(self):
        decl = self.declare()
        self.assertEqual(self.cli("withdraw", "--worker", W),
                         (0, f"removed: {decl} (the lane keeper starts the worker's lane on its next pass)\n"))
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

    def test_withdraw_works_whatever_the_row_now_says(self):
        decl = self.declare()
        self.edit_worker_row(disabled=True)
        self.assertEqual(self.cli("withdraw", "--worker", W)[0], 0)
        self.assertFalse(decl.exists())

    def test_the_workspace_defaults_to_the_resolved_one(self):
        import workspace_default
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker_lane.main(["declare", "--worker", W], vault=self.vault), 0)
        self.assertTrue((self.agents / f"{SEG}.bridge" / "worker.json").is_file())


class TheAgentBridge(Base):
    def cli_without_token(self):
        saved = dict(self.vault.store)
        self.vault.store.clear()
        try:
            rc, said = self.cli("declare", "--worker", W)
        finally:
            self.vault.store.update(saved)
        self.assertEqual(rc, 1, "declare refuses a worker whose row token cannot be read")
        return said

    def vault_get(self):
        return {cli.vault_key(SEG): TOKEN_W}.__getitem__

    def no_vault_key(self, key):
        self.fail(f"a worker's bridge read the vault key {key}")

    def runs(self, decl, run_path, **kw):
        import workspace_default
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=kw.pop("resolved", self.ws)):
            return bridge.main([str(decl)], workspace=self.ws, get_key=self.no_vault_key, vault=self.vault,
                               run=kw.pop("run", mock.Mock(side_effect=AssertionError("the bare package ran"))),
                               run_path=run_path, **kw)

    def test_a_worker_declaration_runs_the_lanes_own_loader_as_its_lane(self):
        decl = self.declare()
        seen = {}

        def fake_run_path(path, run_name):
            seen.update(path=path, run_name=run_name, task=_dirs.task_dir(), result=_dirs.result_dir(),
                        state=_dirs.state_dir(), env={k: os.environ.get(k) for k in (
                            "GATEWAY_INSTANCE", "AGENT_MXID", "REMOTE_TASK_TOKEN", C.LANE_WORKER_ENV,
                            "SUTANDO_SUPERVISED", "REMOTE_PROACTIVE_ROOM")})
        self.assertEqual(self.runs(decl, fake_run_path), 0)
        self.assertEqual(seen, {"path": str(ROOT / "src" / "remote-gateway-bridge.py"), "run_name": "__main__",
                                "task": self.ws / "tasks", "result": self.ws / "results", "state": self.ws / "state",
                                "env": {"GATEWAY_INSTANCE": self.row["instance"], "AGENT_MXID": WORKER_AGENT,
                                        "REMOTE_TASK_TOKEN": TOKEN_W, C.LANE_WORKER_ENV: W,
                                        "SUTANDO_SUPERVISED": "1", "REMOTE_PROACTIVE_ROOM": ""}})

    def test_a_worker_bridge_whose_lane_left_the_plan_or_whose_workspace_differs_never_runs(self):
        decl = self.declare()
        loader = mock.Mock()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.runs(decl, loader, resolved=self.ws / "elsewhere"), 1)
        self.assertIn("resolves another workspace", err.getvalue())
        with mock.patch.object(worker_lane, "bridge_overlay", side_effect=worker_lane.NotServable("lane gone")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.runs(decl, loader), 1)
        self.assertIn("lane gone", err.getvalue())
        loader.assert_not_called()

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
                    self.assertEqual(bridge.main([str(decl)], workspace=self.ws, get_key=self.no_vault_key, run=run,
                                                 vault=self.vault), 2)
                run.assert_not_called()
                self.assertIn(why, err.getvalue())
                C.record_path(self.ws).write_text(good)

    def test_the_rows_token_is_required_and_no_vault_key_is_listed(self):
        decl = self.declare()
        self.assertIn("has no token for its gateway", self.cli_without_token())
        found, skipped = bridge.declarations(self.ws, [])
        self.assertEqual(([p for p, _ in found], skipped), ([decl], []), "listed with no vault key at all")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=self.no_vault_key,
                                         vault=self.vault), 0)
        self.assertIn(f"worker {W}'s token, from its connection row", out.getvalue())
        self.vault.store.clear()
        run = mock.Mock()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(bridge.main([str(decl)], workspace=self.ws, get_key=self.no_vault_key, run=run,
                                         vault=self.vault), 1)
        run.assert_not_called()
        self.assertIn("has no token for its gateway", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=self.no_vault_key,
                                         vault=self.vault), 1)
        self.assertIn(f"WILL NOT START: {cli.worker_name(SEG)}: ", out.getvalue())

    def test_the_credential_needs_an_identity_row(self):
        self.assertEqual(worker_lane.credential("b" * 32, self.ws, {}, self.vault),
                         ("", f"worker {'b' * 32} has no agent identity row"))

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


class TheSupervisedRevision(Base):
    def revision(self):
        spec = importlib.util.spec_from_file_location("sparrowd_wl", ROOT / "src" / "sparrowd.py")
        sd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sd)
        specs, skipped = sd._agent_bridge_specs(self.ws, vault_keys=[])
        self.assertEqual((len(specs), skipped), (1, []))
        return specs[0].revision

    def test_a_record_only_replacement_is_a_changed_bridge_and_an_unchanged_row_is_not(self):
        decl = self.declare()
        raw = decl.read_bytes()
        first = self.revision()
        self.assertEqual(self.revision(), first, "an unchanged row keeps the revision")
        for fields in ({"worker": "b" * 32}, {"instance": "alpha-fixture-net-replaced"},
                       {"token_ref": "AG2_REMOTE_TOKEN_REPLACED"}):
            with self.subTest(fields=fields):
                good = C.record_path(self.ws).read_text()
                self.edit_worker_row(**fields)
                self.assertEqual(decl.read_bytes(), raw, "the declaration is untouched")
                self.assertNotEqual(self.revision(), first, "sparrowd restarts the bridge on the new mapping")
                C.record_path(self.ws).write_text(good)
        self.assertEqual(self.revision(), first)

    def test_a_claude_local_declarations_revision_is_its_bytes_alone(self):
        import hashlib
        root = self.agents / SEG
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": WORKER_AGENT, "TOKEN_VAULT_KEY": cli.vault_key(SEG)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        decl = cli.bridge_home(root) / cli.DECLARATION
        self.assertEqual(bridge.load(decl, self.agents)[0]["digest"], hashlib.sha256(decl.read_bytes()).hexdigest())


# Every hook the lane loader sets, worker wiring included: the declared bridge must end with the same set.
HOOKS = ("TASK_INTAKE_GATE", "PROACTIVE_ROUTE_GATE", "PROACTIVE_DISPATCH_GATE", "PROACTIVE_ACK_GATE", "LANE_SERVER",
         "PROACTIVE_ROOM_GATE", "OWNER_DM_UPGRADED_NOTIFY", "TASK_STATUS", "TASK_STEP_PUBLICATION_ENABLED",
         "STOP_INTERRUPT", "OWNS_UNADDRESSED", "PROACTIVE_ROOM_KEEP", "STAND_DOWN_GATE", "PROACTIVE_CLAIM_GATE",
         "PROACTIVE_OWN_FILE", "TASK_ADDRESSEE", "TASK_PRE_PUBLISH", "_SUTANDO_WORKER_LABEL_APPLIER",
         "_LANE_WORKER", "GATEWAY_INSTANCE", "TASKS_DIR", "RESULTS_DIR", "VOICE_ROOM_VERIFIER")
WIRED = ("TASK_INTAKE_GATE", "PROACTIVE_ROUTE_GATE", "PROACTIVE_DISPATCH_GATE", "LANE_SERVER", "PROACTIVE_ROOM_GATE",
         "OWNER_DM_UPGRADED_NOTIFY", "TASK_STATUS", "OWNS_UNADDRESSED", "PROACTIVE_CLAIM_GATE", "PROACTIVE_OWN_FILE",
         "TASK_ADDRESSEE", "TASK_PRE_PUBLISH", "VOICE_ROOM_VERIFIER")


def _shape(value):
    if value is None or isinstance(value, (str, bool, int, Path)):
        return value
    return getattr(value, "__qualname__", type(value).__qualname__)


class TheHookParity(Base):
    """The lane's launch and the declared bridge both run the real loader: one hook set, every gate wired."""

    def loaded(self, run):
        import runpy
        import workspace_default
        from ag2_sparrow import send_allowlist
        self.addCleanup(setattr, send_allowlist, "_EXTRA_ROOTS", list(send_allowlist._EXTRA_ROOTS))
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ns = run(lambda path, run_name: runpy.run_path(path, run_name="hook_parity_probe"))
        self.assertIn(str(self.ws / "notes"), send_allowlist._EXTRA_ROOTS, "the loader's extra send roots")
        return ns

    def test_the_lane_and_the_declared_bridge_install_the_same_hooks(self):
        self.declare_handler()
        lane = self.row["instance"]
        env = C.launch_env(C.plan_entry(lane, {}, workspace=self.ws), {}, vault=self.vault)

        def as_lane(run_path):  # the lane's launch keys over this process's own (hermetic) environment
            with mock.patch.dict(os.environ, env):
                return run_path(str(ROOT / "src" / "remote-gateway-bridge.py"), "__main__")
        lane_ns = self.loaded(as_lane)
        decl = self.declare()

        def as_bridge(run_path):
            out = {}
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: self.fail(k), vault=self.vault,
                             run=lambda *a, **k: self.fail("the bare package ran"),
                             run_path=lambda path, run_name: out.update(run_path(path, run_name)))
            self.assertEqual(rc, 0)
            return out
        bridge_ns = self.loaded(as_bridge)
        self.assertEqual({k: _shape(bridge_ns[k]) for k in HOOKS}, {k: _shape(lane_ns[k]) for k in HOOKS})
        for ns in (lane_ns, bridge_ns):
            self.assertEqual([k for k in WIRED if ns[k] is None], [], "every gate wired, none left at None")
            self.assertEqual((ns["TASK_ADDRESSEE"](), ns["_LANE_WORKER"], ns["GATEWAY_INSTANCE"]), (W, W, lane))
            self.assertEqual(ns["TASKS_DIR"], self.ws / "tasks")


class TheLaneKeeper(Base):
    def keep(self, now=1000.0):
        started, said = [], []
        C.keep_lanes_up(repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                        vault=self.vault, spawn=self.bridge_spawn(lambda argv, env, log: started.append(
                            env["GATEWAY_INSTANCE"])), log=said.append, now=now)
        self.said = said
        return started

    def test_a_declared_workers_lane_waits_out_the_grace_then_starts_and_withdraw_restores_it(self):
        lane = self.row["instance"]
        self.declare()
        self.assertTrue(worker_lane.declared_bridge(W, self.ws))
        self.assertNotIn(lane, self.keep(now=1000.0), "the declared bridge's turn to take the lease")
        self.assertNotIn(lane, self.keep(now=1000.0 + C.DECLARED_BRIDGE_GRACE_S - 1))
        self.assertIsNotNone(C.worker_enabled_identity(W, self.ws), "the row stays enabled")
        self.assertEqual(C.session_proactive_name("proactive-1.txt", {"SUTANDO_WORKER_ID": W}, self.ws),
                         f"proactive-1.by-{W}.txt", "its session still stamps its own files")
        self.assertEqual(self.keep(now=1000.0 + C.DECLARED_BRIDGE_GRACE_S), [lane],
                         "no bridge took the lease: the lane serves the worker rather than nothing")
        self.assertTrue(any("its declared bridge took no lease" in line for line in self.said), self.said)

    def test_a_failed_start_after_the_grace_backs_off_without_a_new_grace(self):
        lane = self.row["instance"]
        self.declare()
        tried = []

        def boom(argv, env, log_path):
            tried.append(env["GATEWAY_INSTANCE"])
            raise OSError("no python")

        def keep(now):
            C.keep_lanes_up(repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                            vault=self.vault, spawn=boom, log=lambda line: None, now=now)
        grace = C.DECLARED_BRIDGE_GRACE_S
        keep(1000.0)
        keep(1000.0 + grace)
        self.assertEqual(tried.count(lane), 1, "the fallback start, once the grace is out")
        first = C.lane_retry_wait(1)
        keep(1000.0 + grace + first - 1)
        self.assertEqual(tried.count(lane), 1, "inside the backoff: no try")
        keep(1000.0 + grace + first)
        self.assertEqual(tried.count(lane), 2, "the backoff's next try, with no fresh grace")
        state = json.loads(C.lane_keeper_state_path(self.ws).read_text())[lane]
        self.assertEqual((state["failures"], state["declared_unheld_since"]), (2, 1000.0))
        keep(1000.0 + grace + first + C.lane_retry_wait(2))
        self.assertEqual(tried.count(lane), 3, "the wait doubles, as for any lane")

    def test_a_declared_bridge_holding_the_lease_keeps_the_lane_down(self):
        from _helpers import bridge_lease
        lane = self.row["instance"]
        self.declare()
        holder = bridge_lease.start(self.ws, lane, wait=True)
        self.children.append(holder)
        self.assertNotIn(lane, self.keep(now=1000.0 + 10 * C.DECLARED_BRIDGE_GRACE_S))
        bridge_lease.stop(holder)
        self.children.remove(holder)
        self.assertNotIn(lane, self.keep(now=5000.0), "a fresh grace once the holder is gone")
        self.assertEqual(self.cli("withdraw", "--worker", W)[0], 0)
        self.assertFalse(worker_lane.declared_bridge(W, self.ws))
        self.assertEqual(self.keep(now=5001.0), [lane], "withdrawn: the lane at once")

    def test_an_invalid_or_unanswerable_declaration_keeps_the_lane(self):
        lane = self.row["instance"]
        decl = self.declare()
        body = json.loads(decl.read_text())
        decl.write_text(json.dumps(dict(body, name="bridge-other-00000000")))
        self.assertFalse(worker_lane.declared_bridge(W, self.ws))
        decl.write_text(json.dumps(body))
        with mock.patch.object(worker_lane, "declared_bridge", side_effect=OSError("unreadable")):
            self.assertIn(lane, self.keep())
        self.assertFalse(worker_lane.declared_bridge("../x", self.ws))

    def test_stop_stops_the_lane_once_and_leaves_the_row(self):
        spec = importlib.util.spec_from_file_location("connections_cli_wl", ROOT / "scripts" / "connections.py")
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)
        lane = self.row["instance"]
        for pid, said in ((4242, f"stopped {lane} (pid 4242)"), (0, f"{lane} was not running")):
            with mock.patch.object(cli_mod.connections, "stop_bridge", return_value=pid) as stop, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli_mod.main(["--workspace", str(self.ws), "stop", lane]), 0)
            stop.assert_called_once_with(lane, workspace=self.ws)
            self.assertEqual(out.getvalue().strip(), said)
        self.assertIsNotNone(C.worker_enabled_identity(W, self.ws))


if __name__ == "__main__":
    unittest.main()
