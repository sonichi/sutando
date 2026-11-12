#!/usr/bin/env python3
"""A pool worker's own agent identity: one connection row naming the worker.

- The row: a second agent on the core's own server is saved as the worker's row, never merged
  into the core's. It is never primary, keeps its own slug and vault key, and refuses an
  unverified agent, one another row runs as, or one an owner no core row serves holds. The record
  schema allows exactly that.
- Core-facing lookups never pick it: a room's connection, the unaddressed-message owner,
  set_primary/promote and move.
- The lane: `lane_worker` names the worker. The bridge host addresses every task it queues to
  that worker (TASK_ADDRESSEE, over whatever the gateway sent) and claims no proactive file.
  The pool router then hands such a task to that worker, where the same task from a core lane
  follows the room's binding.
- Rotation keeps the agent; retirement (connections and pool_manage) removes the row and the secret.
- pool_identity, the owner's command: add, show and remove, with the token read from stdin.

The vault, the gateway probe and the process spawn are fakes, from tests/connections.test.py.

Run: python3 tests/connections-worker-identity.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
_spec = importlib.util.spec_from_file_location("connections_contract", ROOT / "tests" / "connections.test.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)  # fakes the resolver, as that suite does
C = H.C
sys.path.insert(0, str(ROOT / "skills" / "worker-pool" / "scripts"))
import local_task_protocol as ltp  # noqa: E402
import pool_router as rt  # noqa: E402

W = "7c700e11" + "0" * 24
CORE_AGENT, WORKER_AGENT = "@sutando:alpha-fixture.net", "@reviewer:alpha-fixture.net"
TOKEN_W = "https://chat.alpha-fixture.net|secret-reviewer"
WORKER_INST = C.worker_instance_for("alpha-fixture.net", W)


class Base(H.Base):
    def add_core(self):
        return self.add(H.TOKEN_A, fetch=H.agents(CORE_AGENT))

    def add_worker(self, token=TOKEN_W, agent=WORKER_AGENT, *, worker=W, source="paste", fetch=None):
        preview = C.preview_connection(token, fetch=fetch or H.agents(agent))
        return C.add_worker_identity(worker, token, expected_server=preview["server"], source=source,
                                     preview=preview, workspace=self.ws, vault=self.vault)

    def record(self):
        return json.loads(C.record_path(self.ws).read_text())


class TheRow(Base):
    def test_two_agents_on_one_server_are_two_rows(self):
        core, _ = self.add_core()
        row, created = self.add_worker()
        self.assertTrue(created)
        self.assertEqual((row["worker"], row["agent_mxid"], row["instance"], row["primary"]),
                         (W, WORKER_AGENT, WORKER_INST, False))
        self.assertEqual(WORKER_INST, "alpha-fixture-net-7c700e11")
        self.assertNotEqual(row["token_ref"], core["token_ref"])
        self.assertEqual(self.vault.store[row["token_ref"]], TOKEN_W)
        self.assertEqual(self.vault.store[core["token_ref"]], H.TOKEN_A, "the core's secret is untouched")
        rows = self.rows()
        self.assertEqual([(r["instance"], r["primary"], r.get("worker")) for r in rows],
                         [("alpha-fixture-net", True, None), (WORKER_INST, False, W)])

    def test_without_a_worker_a_second_agent_still_is_the_core_row(self):
        core, _ = self.add_core()
        again, created = self.add(TOKEN_W, fetch=H.agents(WORKER_AGENT))
        self.assertFalse(created)
        self.assertEqual(again["instance"], core["instance"])
        self.assertNotIn("AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET_7C700E11", self.vault.store)

    def test_adding_the_same_worker_agent_again_is_idempotent(self):
        self.add_core()
        first, _ = self.add_worker()
        second, created = self.add_worker()
        self.assertFalse(created)
        self.assertEqual(second["instance"], first["instance"])
        self.assertEqual(len(self.rows()), 2)

    def test_an_unverified_or_shared_agent_is_refused_and_nothing_is_kept(self):
        self.add_core()
        with self.assertRaises(C.WorkerIdentityUnverified):
            self.add_worker(fetch=H.unreachable)
        with self.assertRaises(C.WorkerIdentityShared) as e:
            self.add_worker(agent=CORE_AGENT)
        self.assertIn("the core's", str(e.exception))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(sorted(self.vault.store), ["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"])
        self.add_worker()
        with self.assertRaises(C.WorkerIdentityShared) as e:
            self.add_worker(worker="b" * 32)
        self.assertIn(f"worker {W}", str(e.exception))

    def test_an_agent_another_owner_holds_is_refused_and_nothing_is_kept(self):
        self.add_core()

        def owned_by(owner):
            return lambda url, token, address=None: {"agents": [{"id": WORKER_AGENT, "owner": owner}]}
        for owner in ("@mallory:alpha-fixture.net", None):
            with self.assertRaises(C.Refusal) as e:
                self.add_worker(fetch=owned_by(owner))
            self.assertEqual(e.exception.CODE, "worker_owner_mismatch", owner)
            self.assertIn("@owner:alpha-fixture.net", str(e.exception))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(sorted(self.vault.store), ["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"])
        self.assertEqual(self.add_worker()[0]["owner_mxid"], "@owner:alpha-fixture.net")

    def test_without_a_core_row_no_owner_vouches_for_a_worker(self):
        with self.assertRaises(C.Refusal) as e:
            self.add_worker()
        self.assertEqual(e.exception.CODE, "worker_owner_mismatch")
        self.assertFalse(C.record_path(self.ws).exists() and self.rows())

    def test_the_token_source_is_recorded(self):
        self.add_core()
        row, _ = self.add_worker(source="agent_link")
        self.assertEqual(row["added_by"], "agent_link")
        with self.assertRaises(C.ConnectionsError):
            C.add_worker_identity(W, TOKEN_W, expected_server="alpha-fixture.net", source="email",
                                  workspace=self.ws, vault=self.vault)
        with self.assertRaises(C.ConnectionsError):
            C.add_connection(TOKEN_W, added_by="owner", expected_server="alpha-fixture.net", worker="Not An Id",
                             preview=C.preview_connection(TOKEN_W, fetch=H.agents(WORKER_AGENT)),
                             workspace=self.ws, vault=self.vault)


class Schema(Base):
    def rows_of(self, *rows):
        return {"schema_version": C.SCHEMA_VERSION, "connections": list(rows)}

    def row(self, **kw):
        r = {"server": "s.space", "instance": "s-space", "gateway_url": "https://chat.s.space", "token_ref": "AG2_REMOTE_TOKEN_S_SPACE",
             "primary": True, "added_by": "owner", "added_at": "2026-11-01T00:00:00Z", "verified": True,
             "server_source": "identity"}
        r.update(kw)
        return r

    def worker_row(self, worker=W, n=1, **kw):
        fields = {"instance": f"s-space-w{n}", "token_ref": f"AG2_REMOTE_TOKEN_S_SPACE_W{n}", "primary": False,
                  "worker": worker}
        fields.update(kw)
        return self.row(**fields)

    def test_a_worker_row_may_share_its_server_once_per_worker(self):
        C._validate(self.rows_of(self.row(), self.worker_row(), self.worker_row("b" * 32, 2)))
        with self.assertRaises(C.SchemaError):
            C._validate(self.rows_of(self.row(), self.worker_row(), self.worker_row(n=2)))
        with self.assertRaises(C.SchemaError):
            C._validate(self.rows_of(self.row(), self.row(instance="s-space-2", token_ref="AG2_REMOTE_TOKEN_S_SPACE_2",
                                                          primary=False)))

    def test_a_worker_row_is_named_never_primary_and_names_a_worker_id(self):
        for bad in (self.worker_row(primary=True), self.worker_row(instance=""), self.worker_row(worker="Has Space"),
                    self.worker_row(worker=7)):
            with self.assertRaises(C.SchemaError, msg=repr(bad)):
                C._validate(self.rows_of(bad))


class CoreLookups(Base):
    def setUp(self):
        super().setUp()
        self.add_core()
        self.add_worker()

    def test_a_room_operation_acts_through_the_core_row(self):
        # Put the worker's row first: order must not decide it.
        rec = self.record()
        rec["connections"].reverse()
        C.record_path(self.ws).write_text(json.dumps(rec))
        # One core row is "no preference" (None): the caller's single-connection path is the core's.
        self.assertIsNone(C.connection_for_room("!r:alpha-fixture.net", self.ws))
        self.add(H.TOKEN_B, fetch=H.agents("@sutando:beta-fixture.net"))
        self.assertEqual(C.connection_for_room("!r:alpha-fixture.net", self.ws)["instance"], "alpha-fixture-net")

    def test_the_worker_lane_never_owns_unaddressed_messages(self):
        state = self.ws / "state"
        (state / f"owner-routing.{WORKER_INST}.json").write_text(json.dumps(
            {"owner_dm": "!dm:alpha-fixture.net", "private": True, "identity": WORKER_AGENT}))
        with unittest.mock.patch.object(C, "_lane_running", lambda row, ws: True):
            owner, handed = C.unaddressed_owner(self.ws)
        self.assertEqual((owner["instance"], handed), ("alpha-fixture-net", None))
        self.assertFalse(C.owns_unaddressed(WORKER_INST, self.ws))

    def test_it_cannot_become_primary(self):
        with self.assertRaises(C.WorkerCannotBePrimary):
            C.set_primary(WORKER_INST, workspace=self.ws)
        with self.assertRaises(C.WorkerCannotBePrimary):
            C.promote(WORKER_INST, repo=self.ws / "repo", logs_dir=self.ws / "logs", workspace=self.ws,
                      vault=self.vault, spawn=lambda *a: H.DEAD_PID, kill=lambda *a: None)
        self.assertTrue(next(r for r in self.rows() if r["instance"] == "alpha-fixture-net")["primary"])

    def test_lane_worker_and_worker_identity(self):
        self.assertEqual(C.lane_worker(WORKER_INST, self.ws), W)
        self.assertEqual(C.lane_worker("alpha-fixture-net", self.ws), "")
        self.assertEqual(C.lane_worker("", self.ws), "")
        self.assertEqual(C.lane_worker("nope", self.ws), "")
        self.assertEqual(C.worker_identity(W, self.ws)["instance"], WORKER_INST)
        self.assertIsNone(C.worker_identity("b" * 32, self.ws))
        C.record_path(self.ws).write_text("{not json")
        self.assertIsNone(C.worker_identity(W, self.ws), "an unreadable record names no identity")
        self.assertEqual(C.lane_worker(WORKER_INST, self.ws), "")

    def test_its_launch_runs_as_its_own_agent_without_the_proactive_room(self):
        entry = C.plan_entry(WORKER_INST, {}, workspace=self.ws)
        env = C.launch_env(entry, {"REMOTE_PROACTIVE_ROOM": "!parent:alpha-fixture.net"}, vault=self.vault)
        self.assertEqual((env["GATEWAY_INSTANCE"], env["REMOTE_TASK_TOKEN"], env["REMOTE_PROACTIVE_ROOM"]),
                         (WORKER_INST, TOKEN_W, ""))
        self.assertEqual(env.get("AGENT_MXID"), WORKER_AGENT)


class Lifecycle(Base):
    def setUp(self):
        super().setUp()
        self.add_core()
        self.row, _ = self.add_worker()

    def rotate(self, token, agent):
        return C.update_token(WORKER_INST, token, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                              workspace=self.ws, vault=self.vault, spawn=lambda *a: H.DEAD_PID,
                              kill=lambda *a: None, fetch=H.agents(agent))

    def test_rotation_keeps_the_agent(self):
        with self.assertRaises(C.TokenAgentMismatch):
            self.rotate("https://chat.alpha-fixture.net|secret-someone", "@someone:alpha-fixture.net")
        self.assertEqual(self.vault.store[self.row["token_ref"]], TOKEN_W)
        self.rotate("https://chat.alpha-fixture.net|secret-reviewer-2", WORKER_AGENT)
        self.assertEqual(self.vault.store[self.row["token_ref"]], "https://chat.alpha-fixture.net|secret-reviewer-2")
        self.assertEqual(C.worker_identity(W, self.ws)["worker"], W)

    def test_retirement_removes_the_row_and_the_secret_only(self):
        out = C.retire_worker_identity(W, workspace=self.ws, vault=self.vault, kill=lambda *a: None)
        self.assertEqual([r["instance"] for r in out], [WORKER_INST])
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertNotIn(self.row["token_ref"], self.vault.store)
        self.assertIn("AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET", self.vault.store)
        self.assertEqual([r["instance"] for r in self.rows()], ["alpha-fixture-net"])
        self.assertEqual(C.retire_worker_identity(W, workspace=self.ws, vault=self.vault), [])

    def test_pool_manage_retire_takes_the_identity(self):
        import pool_manage
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault), \
                unittest.mock.patch.object(C, "_stop_row", lambda row, ws, kill: 0):
            self.assertEqual(pool_manage._retire_identity(self.ws, W)["result"], "removed")
            self.assertEqual(pool_manage._retire_identity(self.ws, W), {"result": "none"})
        self.assertIsNone(C.worker_identity(W, self.ws))

    def test_pool_manage_retire_reports_a_failed_retirement(self):
        import pool_manage

        def refuse(*a, **kw):
            raise C.SchemaError("record unreadable")
        with unittest.mock.patch.object(C, "retire_worker_identity", refuse):
            out = pool_manage._retire_identity(self.ws, W)
        self.assertEqual((out["result"], out["why"]), ("failed", "SchemaError: record unreadable"))


def _load_shim():
    spec = importlib.util.spec_from_file_location("rgb_worker_lane", ROOT / "src" / "remote-gateway-bridge.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["rgb_worker_lane"] = m
    spec.loader.exec_module(m)
    return m


ROOM = "!reviewer-dm:alpha-fixture.net"


class TheLane(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgb = _load_shim()

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="worker-lane-"))
        for d in ("tasks", "results", "state", "deliveries"):
            (self.tmp / d).mkdir()
        rgb = self.rgb
        self.saved = {k: getattr(rgb, k) for k in ("WS", "TASKS_DIR", "RESULTS_DIR", "ARCHIVE_RESULTS_DIR",
                                                   "TASK_ADDRESSEE", "PROACTIVE_CLAIM_GATE", "TASK_INTAKE_GATE",
                                                   "_log")}
        rgb.WS, rgb.TASKS_DIR, rgb.RESULTS_DIR = self.tmp, self.tmp / "tasks", self.tmp / "results"
        rgb.ARCHIVE_RESULTS_DIR = self.tmp / "results" / "archive"
        rgb.TASK_INTAKE_GATE = None
        rgb._log = lambda *a: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.rgb, k, v)

    def queued(self, broker_id, **fields):
        rgb = self.rgb
        tid, durable = rgb._write_task({"id": broker_id, "task": "please review", "channel_id": ROOM,
                                        "source": "ag2space", "access_tier": "owner", **fields})
        self.assertTrue(durable)
        return tid, ltp.parse_task_headers((rgb.TASKS_DIR / f"{tid}.txt").read_text())

    def test_a_core_lane_leaves_the_field_as_sent(self):
        self.assertIsNone(self.rgb.TASK_ADDRESSEE)
        _, h = self.queued("task-core1")
        self.assertIsNone(h.get("requested_worker"))
        _, h = self.queued("task-core2", requested_worker="b" * 32)
        self.assertEqual(h.get("requested_worker"), "b" * 32)

    def test_a_worker_lane_addresses_every_task_to_its_worker_and_claims_nothing(self):
        self.rgb._install_worker_lane(W)
        _, h = self.queued("task-w1")
        self.assertEqual(h.get("requested_worker"), W)
        _, h = self.queued("task-w2", requested_worker="b" * 32)
        self.assertEqual(h.get("requested_worker"), W, "the lane's own worker wins over the gateway's copy")
        self.rgb.TASK_ADDRESSEE = lambda: ""
        _, h = self.queued("task-w4")
        self.assertIsNone(h.get("requested_worker"), "no addressee on either side writes no field")
        nudge = self.tmp / "results" / "proactive-1.txt"
        nudge.write_text(f"[room: {ROOM}]\nfrom the core\n")
        self.assertFalse(self.rgb.PROACTIVE_CLAIM_GATE(nudge))

    def test_the_router_hands_the_worker_lanes_task_to_the_worker(self):
        (self.tmp / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}}, "bindings": {}}))
        tid, h = self.queued("task-core3")
        out = rt.route(self.tmp, {"id": tid, "channel_id": ROOM, "source": "ag2space",
                                  "requested_worker": h.get("requested_worker")})
        self.assertEqual(out["targets"], ["core"], "a core lane's task follows the room's binding")
        self.rgb._install_worker_lane(W)
        tid, h = self.queued("task-w3")
        out = rt.route(self.tmp, {"id": tid, "channel_id": ROOM, "source": "ag2space",
                                  "requested_worker": h.get("requested_worker")})
        self.assertEqual((out["targets"], out["delivered"]), ([W], [W]))
        self.assertTrue((self.tmp / "deliveries" / W / f"{tid}.txt").exists())


class LaneStart(Base):
    """The lane's own start decision, through the real shim: an unreadable record never makes a
    worker's lane a core lane."""

    @classmethod
    def setUpClass(cls):
        cls.rgb = _load_shim()

    def setUp(self):
        super().setUp()
        self.add_core()
        self.add_worker()
        rgb = self.rgb
        self.saved = {k: getattr(rgb, k) for k in ("WS", "GATEWAY_INSTANCE", "_log")}
        self.logged = []
        rgb.WS, rgb.GATEWAY_INSTANCE, rgb._log = self.ws, WORKER_INST, self.logged.append
        self.addCleanup(lambda: [setattr(rgb, k, v) for k, v in self.saved.items()])

    def start(self, **env):
        with unittest.mock.patch.dict("os.environ", env):
            if "SUTANDO_LANE_WORKER" not in env:
                os.environ.pop("SUTANDO_LANE_WORKER", None)
            return self.rgb._lane_worker()

    @contextlib.contextmanager
    def spoiled(self, how):
        path = C.record_path(self.ws)
        kept = path.read_text()
        if how == "missing":
            path.unlink()
        elif how == "permission":
            path.chmod(0)
        else:
            path.write_text({"corrupt": "{not json", "wrong-shape": '{"connections": 5}'}[how])
        try:
            yield
        finally:
            if path.exists():
                path.chmod(0o600)
            path.write_text(kept)

    def test_the_launch_names_the_lanes_worker(self):
        env = C.launch_env(C.plan_entry(WORKER_INST, {}, workspace=self.ws), {}, vault=self.vault)
        self.assertEqual(env.get("SUTANDO_LANE_WORKER"), W)
        env = C.launch_env(C.plan_entry("alpha-fixture-net", {}, workspace=self.ws), {}, vault=self.vault)
        self.assertEqual(env.get("SUTANDO_LANE_WORKER"), "")

    def test_an_unreadable_record_keeps_a_worker_lane_a_worker_and_says_so(self):
        for how in ("corrupt", "wrong-shape", "permission", "missing"):
            with self.subTest(how), self.spoiled(how):
                self.logged.clear()
                self.assertEqual(self.start(SUTANDO_LANE_WORKER=W), W)
                self.assertTrue(any("record" in line for line in self.logged), self.logged)

    def test_an_unreadable_record_without_the_launchers_word_refuses_to_start(self):
        for how in ("corrupt", "wrong-shape", "permission"):
            with self.subTest(how), self.spoiled(how), self.assertRaises(SystemExit):
                self.start()
        self.rgb.GATEWAY_INSTANCE = "alpha-fixture-net"
        with self.spoiled("corrupt"):
            self.assertEqual(self.start(SUTANDO_LANE_WORKER=""), "", "a lane launched as a core lane stays one")

    def test_a_launch_and_a_row_that_disagree_refuse_to_start(self):
        with self.assertRaises(SystemExit):
            self.start(SUTANDO_LANE_WORKER="")
        with self.assertRaises(SystemExit):
            self.start(SUTANDO_LANE_WORKER="b" * 32)
        self.assertEqual(self.start(SUTANDO_LANE_WORKER=W), W)
        self.assertEqual(self.start(), W)
        self.rgb.GATEWAY_INSTANCE = "alpha-fixture-net"
        self.assertEqual(self.start(SUTANDO_LANE_WORKER=""), "")

    def test_a_worker_row_another_owner_holds_refuses_to_start(self):
        rec = self.record()
        next(r for r in rec["connections"] if r.get("worker"))["owner_mxid"] = "@mallory:alpha-fixture.net"
        C.record_path(self.ws).write_text(json.dumps(rec))
        with self.assertRaises(SystemExit):
            self.start(SUTANDO_LANE_WORKER=W)
        self.assertTrue(any("not the core's owner" in line for line in self.logged), self.logged)


class OwnerCommand(Base):
    def setUp(self):
        super().setUp()
        self.add_core()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live", "label": "Reviewer"}}, "bindings": {}}))
        import pool_identity
        self.pi = pool_identity
        real = C.preview_connection

        def preview(token, **kw):
            # The gateway names the reviewer agent for its token, the core's for the core's.
            return real(token, fetch=H.agents(WORKER_AGENT if "reviewer" in token else CORE_AGENT))
        for name, fake in (("_vault", lambda v=None: self.vault), ("preview_connection", preview),
                           ("start_added", lambda row, **kw: {"ok": True})):
            p = unittest.mock.patch.object(C, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.pi.main(["--workspace", str(self.ws), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_add_show_remove_by_label_with_the_token_on_stdin(self):
        rc, out, err = self.run_cmd("add", "Reviewer", "-", "--yes", "--expected-server", "alpha-fixture.net",
                                    "--repo", str(self.ws), "--logs-dir", str(self.ws), stdin=TOKEN_W + "\n")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("secret-reviewer", out + err, "the token is never printed")
        self.assertIn(f"worker {W} runs as {WORKER_AGENT}", out)
        rc, out, _ = self.run_cmd("show", "Reviewer")
        self.assertEqual((rc, json.loads(out)["agent_mxid"]), (0, WORKER_AGENT))
        self.assertNotIn("secret", out)
        with unittest.mock.patch.object(C, "_stop_row", lambda row, ws, kill: 0):
            rc, out, _ = self.run_cmd("remove", "Reviewer")
        self.assertEqual(rc, 0)
        self.assertIsNone(C.worker_identity(W, self.ws))

    def test_a_refusal_and_an_unknown_worker(self):
        rc, _, err = self.run_cmd("add", "Reviewer", "-", "--yes", "--expected-server", "alpha-fixture.net",
                                  "--repo", str(self.ws), "--logs-dir", str(self.ws), stdin=H.TOKEN_A)
        self.assertEqual(rc, 1)
        self.assertIn("refused (worker_identity_shared)", err)
        with self.assertRaises(SystemExit):
            self.run_cmd("show", "Nobody")

    def add_args(self, *extra):
        return ("add", "Reviewer", *extra, "--repo", str(self.ws), "--logs-dir", str(self.ws))

    def test_add_asks_before_saving_and_reports_a_lane_that_did_not_start(self):
        rc, _, err = self.run_cmd(*self.add_args("-", "--yes"), stdin=TOKEN_W)
        self.assertEqual(rc, 2)
        self.assertIn("--yes needs --expected-server", err)
        with unittest.mock.patch("builtins.input", lambda prompt: "n"):
            rc, out, _ = self.run_cmd(*self.add_args(TOKEN_W))
        self.assertEqual(rc, 1)
        self.assertIn(f"agent:   {WORKER_AGENT}", out)
        self.assertIn("owner:   @owner:alpha-fixture.net", out)
        self.assertIn("token:   accepted", out)
        self.assertNotIn("secret-reviewer", out)
        self.assertIn("not saved", out)
        self.assertIsNone(C.worker_identity(W, self.ws))
        with unittest.mock.patch("getpass.getpass", lambda prompt: TOKEN_W), \
                unittest.mock.patch("builtins.input", lambda prompt: "y"), \
                unittest.mock.patch.object(C, "start_added", lambda row, **kw: {"ok": False, "reason": "no python"}):
            rc, _, err = self.run_cmd(*self.add_args())
        self.assertEqual(rc, 1)
        self.assertIn("not started: no python", err)
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], WORKER_AGENT)

    def test_preview_text_names_an_unverified_server_and_its_error(self):
        text = self.pi.preview_text({"server": "x.net", "verified": False, "authenticated": False, "error": "boom"})
        self.assertIn("x.net  (unverified)", text)
        self.assertIn("the gateway did not name one agent", text)
        self.assertIn("the gateway named no owner", text)
        self.assertIn("token:   REJECTED", text)
        self.assertIn("error:   boom", text)

    def test_show_rotate_and_remove_without_an_identity_then_rotate_with_one(self):
        rc, out, _ = self.run_cmd("show", "Reviewer")
        self.assertEqual(rc, 1)
        self.assertIn("no agent of its own", out)
        rc, _, err = self.run_cmd("rotate", "Reviewer", "-", "--repo", str(self.ws), "--logs-dir", str(self.ws))
        self.assertEqual(rc, 1)
        self.assertIn("no agent of its own to rotate", err)
        rc, out, _ = self.run_cmd("remove", "Reviewer")
        self.assertEqual(rc, 0)
        self.assertIn("has no agent of its own", out)
        self.run_cmd(*self.add_args("-", "--yes", "--expected-server", "alpha-fixture.net"), stdin=TOKEN_W)
        real = C.update_token
        new = "https://chat.alpha-fixture.net|secret-reviewer-2"

        def update(instance, token, **kw):
            return real(instance, token, vault=self.vault, spawn=lambda *a: H.DEAD_PID, kill=lambda *a: None,
                        environ={}, fetch=H.agents(WORKER_AGENT), **kw)
        with unittest.mock.patch.object(C, "update_token", update):
            rc, out, _ = self.run_cmd("rotate", "Reviewer", "-", "--repo", str(self.ws), "--logs-dir", str(self.ws),
                                      stdin=new)
        self.assertEqual(rc, 0)
        self.assertIn(f"rotated {WORKER_INST}", out)
        self.assertEqual(self.vault.store[C.worker_identity(W, self.ws)["token_ref"]], new)

    def test_an_ambiguous_label_and_an_unreadable_record(self):
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "bindings": {}, "workers": {W: {"state": "live", "label": "Reviewer"},
                                                        "b" * 32: {"state": "live", "label": "Reviewer"}}}))
        rc, _, err = self.run_cmd("show", "Reviewer")
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.startswith("pool_identity: "), err)
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live", "label": "Reviewer"}}, "bindings": {}}))
        C.record_path(self.ws).write_text("{not json")
        rc, _, err = self.run_cmd(*self.add_args("-", "--yes", "--expected-server", "alpha-fixture.net"),
                                  stdin=TOKEN_W)
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("pool_identity: "), err)


# ── one agent per server: a worker with an identity on two homeservers ──────────────────────────

BETA_CORE, BETA_WORKER = "@sutando:beta-fixture.net", "@reviewer:beta-fixture.net"
TOKEN_WB = "https://relay.beta-fixture.net:8443|secret-reviewer-b"
ALPHA_ROOM, BETA_ROOM = "!work:alpha-fixture.net", "!work:beta-fixture.net"


class TwoServers(Base):
    """The core is connected to alpha (primary) and beta; worker W holds an agent of its own on each."""

    def setUp(self):
        super().setUp()
        self.add_core()
        self.add(H.TOKEN_B, fetch=H.agents(BETA_CORE))
        self.alpha, created_a = self.add_worker()
        self.beta, created_b = self.add_worker(TOKEN_WB, BETA_WORKER)
        self.assertTrue(created_a and created_b)
        self.env = {C.SESSION_WORKER_ENV: W}

    def test_two_identities_are_two_rows_with_their_own_secrets(self):
        mine = C.worker_identities(W, self.ws)
        self.assertEqual(sorted((r["server"], r["agent_mxid"]) for r in mine),
                         [("alpha-fixture.net", WORKER_AGENT), ("beta-fixture.net", BETA_WORKER)])
        self.assertNotEqual(self.alpha["instance"], self.beta["instance"])
        self.assertEqual(self.vault.store[self.alpha["token_ref"]], TOKEN_W)
        self.assertEqual(self.vault.store[self.beta["token_ref"]], TOKEN_WB)

    def test_a_lookup_picks_by_server_and_refuses_to_guess(self):
        self.assertEqual(C.worker_identity(W, self.ws, server="alpha-fixture.net")["agent_mxid"], WORKER_AGENT)
        self.assertEqual(C.worker_identity(W, self.ws, server="Beta-Fixture.NET")["agent_mxid"], BETA_WORKER)
        self.assertIsNone(C.worker_identity(W, self.ws, server="gamma-fixture.net"))
        for lookup in (C.worker_identity, C.worker_enabled_identity):
            with self.assertRaises(C.AmbiguousWorkerIdentity) as e:
                lookup(W, self.ws)
            self.assertEqual(e.exception.CODE, "worker_identity_ambiguous")

    def test_a_room_acts_with_its_own_servers_token(self):
        for room, token, agent in ((ALPHA_ROOM, TOKEN_W, WORKER_AGENT), (BETA_ROOM, TOKEN_WB, BETA_WORKER)):
            row = C.connection_for_room(room, self.ws, self.env)
            self.assertEqual(row["agent_mxid"], agent, room)
            _, got = C.room_credential(room, workspace=self.ws, environ=self.env, vault=self.vault)
            self.assertEqual(got, token, room)
            self.assertEqual(C.room_agent_mxid(room, workspace=self.ws, environ=self.env), agent, room)

    def test_a_room_operation_with_no_room_is_refused_by_name(self):
        with self.assertRaises(C.WorkerRoomAmbiguous) as e:
            C.connection_for_room(None, self.ws, self.env)
        self.assertIsInstance(e.exception, C.NoConnectionForRoom, "every NoConnectionForRoom handler refuses it")
        self.assertIsNotNone(C.connection_for_room(None, self.ws, {}), "the core's session is unchanged")

    def test_the_mcp_credential_is_the_seats_servers(self):
        for server, secret in (("alpha-fixture.net", "secret-reviewer"), ("beta-fixture.net", "secret-reviewer-b")):
            cred = C.worker_mcp_credential(W, workspace=self.ws, vault=self.vault, environ={}, server=server)
            self.assertEqual((cred["acts_as"], cred["server"], cred["bearer"]), ("worker", server, secret))
        with self.assertRaises(C.NoWorkerCredential) as e:
            C.worker_mcp_credential(W, workspace=self.ws, vault=self.vault, environ={})
        self.assertEqual(e.exception.reason, "ambiguous_server")
        (self.ws / "state" / C.MCP_DESCRIPTOR_NAME).write_text(json.dumps({"lane": "beta-fixture-net"}))
        with unittest.mock.patch.object(C, "mcp_descriptor_path", lambda ws, *a: ws / "state" / C.MCP_DESCRIPTOR_NAME):
            cred = C.worker_mcp_credential(W, workspace=self.ws, vault=self.vault, environ={})
        self.assertEqual((cred["server"], cred["bearer"]), ("beta-fixture.net", "secret-reviewer-b"))
        argv = C.mcp_worker_credential_argv(W, Path("/repo"), workspace=self.ws, server="beta-fixture.net")
        self.assertEqual(argv[-2:], ["--server", "beta-fixture.net"])

    def test_a_single_identity_on_another_server_never_mints_on_the_seats(self):
        C.remove_worker_identity(W, server="alpha-fixture.net", workspace=self.ws, vault=self.vault,
                                 kill=lambda *a: None)
        (self.ws / "state" / C.MCP_DESCRIPTOR_NAME).write_text(json.dumps({"lane": "primary"}))
        with unittest.mock.patch.object(C, "mcp_descriptor_path", lambda ws, *a: ws / "state" / C.MCP_DESCRIPTOR_NAME):
            cred = C.worker_mcp_credential(W, workspace=self.ws, vault=self.vault, environ={})
        self.assertEqual(cred, {"worker": W, "acts_as": "core", "reason": "no_identity"},
                         "the beta token is never handed to a proxy minting on alpha")

    def test_proactive_notes_go_from_the_primarys_server_only(self):
        self.assertEqual(C.worker_home_identity(W, self.ws)["instance"], self.alpha["instance"])
        self.assertTrue(C.is_worker_home_lane(self.alpha["instance"], W, self.ws))
        self.assertFalse(C.is_worker_home_lane(self.beta["instance"], W, self.ws))
        name = C.session_proactive_name("proactive-1.txt", self.env, self.ws)
        self.assertEqual(name, f"proactive-1.by-{W}.txt")

    def test_a_note_with_no_server_to_send_from_is_refused_by_name(self):
        with unittest.mock.patch.object(C, "primary_row", lambda ws=None: None):
            self.assertIsNone(C.worker_home_identity(W, self.ws))
            self.assertFalse(C.is_worker_home_lane(self.alpha["instance"], W, self.ws))
            with self.assertRaises(C.AmbiguousWorkerIdentity):
                C.session_proactive_name("proactive-1.txt", self.env, self.ws)
        C.disconnect(self.alpha["instance"], workspace=self.ws, kill=lambda *a: None)
        self.assertEqual(C.worker_home_identity(W, self.ws)["instance"], self.beta["instance"],
                         "one serving identity left: it speaks")

    def test_a_second_agent_on_the_same_server_is_still_refused(self):
        with self.assertRaises(C.WorkerIdentityExists) as e:
            C.require_no_worker_identity(W, "alpha-fixture.net", self.ws)
        self.assertEqual(e.exception.CODE, "worker_identity_exists")
        with self.assertRaises(C.WorkerIdentityExists):
            self.add_worker("https://chat.alpha-fixture.net|secret-other", "@other:alpha-fixture.net")
        self.assertEqual(len(C.worker_identities(W, self.ws)), 2)
        self.assertNotIn("AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET_OTHER", self.vault.store)

    def test_remove_takes_one_server_and_retire_takes_both(self):
        kill = lambda *a: None  # noqa: E731
        with self.assertRaises(C.AmbiguousWorkerIdentity):
            C.remove_worker_identity(W, workspace=self.ws, vault=self.vault, kill=kill)
        gone = C.remove_worker_identity(W, server="beta-fixture.net", workspace=self.ws, vault=self.vault, kill=kill)
        self.assertEqual(gone["instance"], self.beta["instance"])
        self.assertEqual([r["agent_mxid"] for r in C.worker_identities(W, self.ws)], [WORKER_AGENT])
        self.add_worker(TOKEN_WB, BETA_WORKER)
        out = C.retire_worker_identity(W, workspace=self.ws, vault=self.vault, kill=kill)
        self.assertEqual(sorted(r["agent_mxid"] for r in out), [WORKER_AGENT, BETA_WORKER])
        self.assertEqual(C.worker_identities(W, self.ws), [])
        self.assertEqual(sorted(self.vault.store), ["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET",
                                                    "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"])

    def test_the_scripts_refuse_only_on_the_chosen_lanes_server(self):
        """provision-begin and --device-flow check the server of --lane: beta is refused while W has an
        agent there, and allowed once that one is gone, the alpha agent standing throughout."""
        import pool_device_connect
        import pool_provision
        kill = lambda *a: None  # noqa: E731
        for lane, server in (("alpha-fixture-net", "alpha-fixture.net"), ("beta-fixture-net", "beta-fixture.net")):
            with self.assertRaises(pool_provision.ProvisionError) as e:
                pool_provision.begin(W, role="reviewer", workspace=self.ws, vault=self.vault, lane=lane)
            self.assertIn(f"already has an agent of its own on {server}", str(e.exception))
            with self.assertRaises(pool_device_connect.DeviceConnectError) as e:
                pool_device_connect.connect(W, label="reviewer", workspace=self.ws, vault=self.vault, lane=lane,
                                            deliver=lambda text: None)
            self.assertIn(f"already has an agent of its own on {server}", str(e.exception))
        C.remove_worker_identity(W, server="beta-fixture.net", workspace=self.ws, vault=self.vault, kill=kill)
        out = pool_provision.begin(W, role="reviewer", workspace=self.ws, vault=self.vault, lane="beta-fixture-net")
        self.assertEqual(len(out["claim_key_sha256"]), 64, "a second server's agent may be provisioned")

        class Reached(Exception):
            pass

        def request(*a, **kw):
            raise Reached()
        with self.assertRaises(Reached):
            pool_device_connect.connect(W, label="reviewer", workspace=self.ws, vault=self.vault,
                                        lane="beta-fixture-net", request=request, deliver=lambda text: None)
        with self.assertRaises(pool_device_connect.DeviceConnectError):
            pool_device_connect.connect(W, label="reviewer", workspace=self.ws, vault=self.vault,
                                        lane="alpha-fixture-net", request=request, deliver=lambda text: None)

    def test_the_owner_command_takes_or_infers_the_server(self):
        import pool_identity
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live", "label": "Reviewer"}}, "bindings": {}}))

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = pool_identity.main(["--workspace", str(self.ws), *argv])
            return rc, out.getvalue(), err.getvalue()
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault), \
                unittest.mock.patch.object(C, "_stop_row", lambda row, ws, kill: 0):
            rc, out, _ = run("show", "Reviewer")
            self.assertEqual((rc, sorted(r["agent_mxid"] for r in json.loads(out))), (0, [WORKER_AGENT, BETA_WORKER]))
            rc, out, _ = run("show", "Reviewer", "--server", "beta-fixture.net")
            self.assertEqual((rc, json.loads(out)["agent_mxid"]), (0, BETA_WORKER))
            for verb in (("remove", "Reviewer"), ("rotate", "Reviewer", "-", "--repo", str(self.ws),
                                                  "--logs-dir", str(self.ws))):
                rc, _, err = run(*verb)
                self.assertEqual(rc, 1, verb)
                self.assertIn("refused (worker_identity_ambiguous)", err, verb)
            self.assertEqual(len(C.worker_identities(W, self.ws)), 2, "nothing was removed or rotated")
            rc, out, _ = run("remove", "Reviewer", "--server", "beta-fixture.net")
            self.assertEqual(rc, 0)
            self.assertIn(self.beta["instance"], out)
            rc, out, _ = run("show", "Reviewer")
            self.assertEqual(json.loads(out)["agent_mxid"], WORKER_AGENT, "one left: no server needed")

    def test_pool_manage_retire_takes_both(self):
        import pool_manage
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault), \
                unittest.mock.patch.object(C, "_stop_row", lambda row, ws, kill: 0):
            out = pool_manage._retire_identity(self.ws, W)
        self.assertEqual(out["result"], "removed")
        self.assertEqual(out["agent"], f"{WORKER_AGENT}, {BETA_WORKER}")
        self.assertEqual(C.worker_identities(W, self.ws), [])


if __name__ == "__main__":
    unittest.main()
