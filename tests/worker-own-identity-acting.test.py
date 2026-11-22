#!/usr/bin/env python3
"""A pool worker that has its own agent acts as that agent from its own session.

The core owns the identity's lifecycle (add, rotate, retire) and its lane; the worker's session
uses it. `connections.acting_identity` is the one rule, reached by every room client through
`connection_for_room`:

- In the worker's session a room on its agent's server resolves to the worker's own row, its
  token (the vault's) and its agent. The core's session, another worker's session and a room on
  another server never do.
- Identity comes from the task: a reply posts as the agent the task was addressed to, only in the
  task's own room; a core agent only for a task this worker holds that is still open. A task file
  that does not bind is refused. An unprompted post is the worker's own, except in a core lane's
  own owner DM. A room nothing serves is refused, never sent with the shared token.
- room_ops and task-progress send that worker's bearer, with no change of their own.
- A task's rows in the local activity log name the agent the task is answered as, so a reader
  need not assume every row is the core's.
- `channel-env.sh <a worker's lane>` says the token is in the vault, not "no channel dir".

The record, the vault and the channel home are fixtures.

Run: python3 tests/worker-own-identity-acting.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills" / "agent-room-ops"))

VAULT = {}
_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = lambda var: VAULT[var]
sys.modules["vault_intercept"] = _FAKE_VI

import _gateway
import activity_rows
import channel_env_resolve
import connections as C

_spec = importlib.util.spec_from_file_location(
    "notify_under_test", ROOT / "skills" / "task-progress" / "scripts" / "notify.py")
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)

ALPHA, BETA = "https://chat.alpha-fixture.net/relay", "https://chat.beta-fixture.net/relay"
CORE_AGENT, BETA_AGENT, OWN_AGENT = "@core:alpha-fixture.net", "@core:beta-fixture.net", "@helper:alpha-fixture.net"
W, OTHER_W = "7c700e11" + "0" * 24, "9d9d9d9d" + "0" * 24
OWN = C.worker_instance_for("alpha-fixture.net", W)
ROOM, BETA_ROOM, CORE_DM = "!dm:alpha-fixture.net", "!team:beta-fixture.net", "!core-dm:alpha-fixture.net"


def row(server, instance, gateway_url, agent, **extra):
    r = {"server": server, "instance": instance, "gateway_url": gateway_url,
         "token_ref": C.token_ref_for(instance), "primary": False, "added_by": "owner",
         "added_at": "2026-10-01T00:00:00Z", "verified": True, "server_source": "identity",
         "agent_mxid": agent}
    r.update(extra)
    return r


def rows(core=None, **own):
    return [row("alpha-fixture.net", "alpha", ALPHA, CORE_AGENT, primary=True, **(core or {})),
            row("beta-fixture.net", "beta", BETA, BETA_AGENT),
            row("alpha-fixture.net", OWN, ALPHA, OWN_AGENT, worker=W, **own)]


CORE_ENV = {C.token_ref_for("alpha"): ALPHA + "|core-secret", C.token_ref_for("beta"): BETA + "|beta-secret"}
# The name the pool launchers export into a worker's seat.
SEAT_VAR = "SUTANDO_WORKER_ID"
SEAT = {**CORE_ENV, SEAT_VAR: W}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="worker-own-identity-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ws = self.root / "workspace"
        (self.ws / "state").mkdir(parents=True)
        (self.root / "config" / "channels").mkdir(parents=True)
        VAULT.clear()
        VAULT[C.token_ref_for(OWN)] = ALPHA + "|own-secret"
        self.record(rows())
        for patcher in (mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.root / "config")}),
                        mock.patch("workspace_default.resolve_workspace", return_value=self.ws)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, rws):
        C.record_path(self.ws).write_text(json.dumps({"schema_version": C.SCHEMA_VERSION, "connections": rws}))

    def open_task(self, instance, room):
        (self.ws / "state" / f"remote-task-rooms.{instance}.json").write_text(json.dumps({"task-x": room}))

    def owner_dm(self, instance, agent, room):
        (self.ws / "state" / f"owner-routing.{instance}.json").write_text(json.dumps(
            {"owner_dm": room, "private": True, "identity": agent}))

    def instance(self, room, environ):
        r = C.connection_for_room(room, self.ws, environ)
        return None if r is None else r["instance"]


class TheWorkersSession(Base):
    def test_a_room_on_its_agents_server_is_reached_as_its_own_agent(self):
        self.assertEqual(self.instance(ROOM, SEAT), OWN)
        self.assertEqual(C.room_credential(ROOM, workspace=self.ws, environ=SEAT), (ALPHA, ALPHA + "|own-secret"))
        self.assertEqual(C.room_agent_mxid(ROOM, workspace=self.ws, environ=SEAT), OWN_AGENT)

    def test_with_no_room_it_acts_as_its_own_agent(self):
        self.assertEqual(self.instance(None, SEAT), OWN)
        self.assertEqual(self.instance(None, CORE_ENV), "alpha")

    def test_one_core_row_does_not_hide_the_workers_own(self):
        self.record([r for r in rows() if r["instance"] != "beta"])
        self.assertEqual(self.instance(ROOM, SEAT), OWN)
        self.assertIsNone(self.instance(ROOM, CORE_ENV))

    def test_a_room_on_another_server_is_the_core_rows(self):
        self.assertEqual(self.instance(BETA_ROOM, SEAT), "beta")

    def test_a_disconnected_identity_answers_through_the_cores(self):
        self.record(rows(disabled=True))
        self.assertEqual(self.instance(ROOM, SEAT), "alpha")


class NeverSomeoneElses(Base):
    def test_the_core_session_never_acts_as_a_workers_agent(self):
        self.open_task(OWN, ROOM)
        self.owner_dm(OWN, OWN_AGENT, ROOM)
        self.assertEqual(self.instance(ROOM, CORE_ENV), "alpha")
        self.assertEqual(C.room_credential(ROOM, workspace=self.ws, environ=CORE_ENV), (ALPHA, ALPHA + "|core-secret"))

    def test_another_workers_session_never_does(self):
        for seat in ({**CORE_ENV, SEAT_VAR: OTHER_W}, {**CORE_ENV, SEAT_VAR: "not a worker id"}):
            self.assertEqual(self.instance(ROOM, seat), "alpha")
        self.assertEqual(C.session_worker({SEAT_VAR: "not a worker id"}), "")

    def test_the_seat_variable_is_the_one_the_launchers_export(self):
        self.assertEqual(C.SESSION_WORKER_ENV, SEAT_VAR)
        scripts = ROOT / "skills" / "worker-pool" / "scripts"
        self.assertIn(f'"{SEAT_VAR}": worker_id', (scripts / "spawn_worker.py").read_text())
        for launcher in ("launch-worker-session.sh", "launch-codex-worker-session.sh"):
            self.assertIn(SEAT_VAR, (scripts / launcher).read_text(), launcher)


def write_task(ws, tid, room, agent="", folder="tasks"):
    path = ws / folder / f"{tid}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    recv = f"receiving_instance: {agent}\n" if agent else ""
    path.write_text(f"id: {tid}\n{recv}channel_id: {room}\nsource: ag2space\ntask: hi\n")
    return path


class IdentityComesFromTheTask(Base):
    """Which lane once handled a room never decides: its task ledger outlives every task in it."""

    def deliver(self, tid, worker=W):
        d = self.ws / "deliveries" / worker
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.txt").write_text("")

    def via(self, room, environ, task_file):
        r = C.connection_for_room(room, self.ws, environ, task_file)
        return None if r is None else r["instance"]

    def refused(self, room, task_file, environ=SEAT):
        with self.assertRaises(C.NoConnectionForRoom) as cm:
            C.connection_for_room(room, self.ws, environ, task_file)
        return str(cm.exception)

    def test_its_own_task_is_its_own_even_where_a_stale_core_entry_names_the_room(self):
        self.open_task("alpha", ROOM)
        own = write_task(self.ws, f"task-{OWN}~abc123", ROOM, OWN_AGENT)
        self.assertEqual(self.via(ROOM, SEAT, own), OWN)
        self.assertEqual(self.instance(ROOM, SEAT), OWN)

    def test_its_own_task_in_another_room_is_refused(self):
        own = write_task(self.ws, f"task-{OWN}~abc123", ROOM, OWN_AGENT)
        self.assertIn("not the task's room", self.refused(CORE_DM, own))

    def test_a_core_task_delivered_to_it_is_answered_as_the_core(self):
        core = write_task(self.ws, "task-abc123", ROOM, CORE_AGENT)
        self.deliver("task-abc123")
        self.assertEqual(self.via(ROOM, SEAT, core), "alpha")
        beta = write_task(self.ws, "task-beta~def456", BETA_ROOM, BETA_AGENT)
        self.deliver("task-beta~def456")
        self.assertEqual(self.via(BETA_ROOM, SEAT, beta), "beta")

    def test_a_core_task_is_the_cores_row_even_with_one_core_connection(self):
        self.record([r for r in rows() if r["instance"] != "beta"])
        core = write_task(self.ws, "task-abc123", ROOM, CORE_AGENT)
        self.deliver("task-abc123")
        self.assertEqual(C.room_credential(ROOM, workspace=self.ws, environ=SEAT, task_file=core),
                         (ALPHA, ALPHA + "|core-secret"))

    def test_a_forged_core_header_in_a_task_it_does_not_hold_is_refused(self):
        forged = write_task(self.ws, "task-abc123", ROOM, CORE_AGENT)
        self.assertIn("does not hold", self.refused(ROOM, forged))
        self.deliver("task-abc123", worker=OTHER_W)
        self.assertIn("does not hold", self.refused(ROOM, forged))
        scratch = write_task(self.ws, "task-abc999", ROOM, CORE_AGENT, folder="scratch")
        self.deliver("task-abc999")
        self.assertIn("does not hold", self.refused(ROOM, scratch))

    def test_a_held_core_task_posted_to_another_room_is_refused(self):
        core = write_task(self.ws, "task-abc123", ROOM, CORE_AGENT)
        self.deliver("task-abc123")
        self.assertIn("not the task's room", self.refused(CORE_DM, core))

    def test_an_answered_or_archived_core_task_is_refused(self):
        core = write_task(self.ws, "task-abc123", ROOM, CORE_AGENT)
        self.deliver("task-abc123")
        (self.ws / "results").mkdir()
        (self.ws / "results" / "task-abc123.txt").write_text("done\n")
        self.assertIn("answered", self.refused(ROOM, core))
        (self.ws / "results" / "task-abc123.txt").unlink()
        archived = self.ws / "tasks" / "archive"
        archived.mkdir()
        core.rename(archived / core.name)
        self.assertIn("unreadable", self.refused(ROOM, core))

    def test_a_missing_unreadable_or_inconsistent_task_file_is_refused(self):
        self.assertIn("unreadable", self.refused(ROOM, self.ws / "tasks" / "task-gone.txt"))
        self.assertIn("no task file", self.refused(ROOM, ""))
        lane_says_beta = write_task(self.ws, "task-beta~abc123", ROOM, CORE_AGENT)
        self.assertIn("lane runs as", self.refused(ROOM, lane_says_beta))
        nameless = write_task(self.ws, "task-abc777", ROOM)
        self.assertIn("names no receiving agent", self.refused(ROOM, nameless))
        stranger = write_task(self.ws, "task-abc888", ROOM, "@someone:alpha-fixture.net")
        self.deliver("task-abc888")
        self.assertIn("neither", self.refused(ROOM, stranger))

    def test_an_unprompted_post_is_its_own(self):
        self.open_task("alpha", ROOM)
        self.open_task("beta", ROOM)
        self.assertEqual(self.instance(ROOM, SEAT), OWN)
        self.assertEqual(self.instance("!bare", SEAT), OWN)

    def test_no_identity_for_the_room_is_refused_never_the_shared_token(self):
        with self.assertRaises(C.NoConnectionForRoom):
            C.connection_for_room("!r:gamma-fixture.net", self.ws, SEAT)
        self.record([r for r in rows() if r["instance"] != "beta"])
        with self.assertRaises(C.NoConnectionForRoom):
            C.connection_for_room(BETA_ROOM, self.ws, SEAT)

    def test_the_core_session_and_single_connection_installs_ignore_the_task_file(self):
        missing = self.ws / "tasks" / "task-gone.txt"
        self.assertEqual(self.via(ROOM, CORE_ENV, missing), "alpha")
        self.record([r for r in rows() if r["instance"] != "beta"])
        self.assertIsNone(self.via(ROOM, CORE_ENV, missing))
        C.record_path(self.ws).unlink()
        self.assertIsNone(self.via(ROOM, SEAT, missing))


class TheCoresOwnerDM(Base):
    def test_the_cores_owner_dm_stays_the_cores(self):
        self.owner_dm("alpha", CORE_AGENT, CORE_DM)
        self.assertEqual(self.instance(CORE_DM, SEAT), "alpha")
        self.assertEqual(self.instance(ROOM, SEAT), OWN)

    def test_a_core_bridge_running_under_a_launch_key_alias_keeps_its_owner_dm(self):
        self.record(rows(core={"launch_keys": [C.token_ref_for("alias")]}))
        for name in ("alpha", "alias"):
            with self.subTest(owner_dm_written_as=name):
                self.owner_dm(name, CORE_AGENT, CORE_DM)
                self.assertEqual(self.instance(CORE_DM, SEAT), "alpha")
                (self.ws / "state" / f"owner-routing.{name}.json").unlink()
        self.assertEqual(self.instance(CORE_DM, SEAT), OWN)
        self.assertEqual(C.task_agent_mxid("task-alias~abc123", self.ws), CORE_AGENT)

    def test_an_alias_record_bound_to_another_agent_or_not_private_is_no_owner_dm(self):
        self.record(rows(core={"launch_keys": [C.token_ref_for("alias")]}))
        self.owner_dm("alias", "@someone:alpha-fixture.net", CORE_DM)
        self.assertEqual(self.instance(CORE_DM, SEAT), OWN)
        (self.ws / "state" / "owner-routing.alias.json").write_text(json.dumps(
            {"owner_dm": CORE_DM, "private": False, "identity": CORE_AGENT}))
        self.assertEqual(self.instance(CORE_DM, SEAT), OWN)


class RoomClients(Base):
    """room_ops and task-progress ask the resolver with the process environment: no flag of their own."""

    def bearer(self, seat):
        with mock.patch.dict("os.environ", seat):
            base, headers = _gateway.gateway(ROOM)
            url, token = notify._gateway_config("ag2space", ROOM)
        self.assertEqual((base, url), (ALPHA, ALPHA))
        self.assertEqual(headers["Authorization"], f"Bearer {token}")
        return token

    def test_the_workers_session_sends_its_own_bearer(self):
        self.assertEqual(self.bearer(SEAT), "own-secret")

    def test_the_cores_session_sends_the_cores(self):
        self.assertEqual(self.bearer(CORE_ENV), "core-secret")

    def test_a_worker_whose_token_is_gone_is_refused_never_given_the_cores(self):
        VAULT.clear()
        with mock.patch.dict("os.environ", SEAT), self.assertRaises(_gateway.RoomConnectionError):
            _gateway.gateway(ROOM)

    def test_a_task_file_makes_room_ops_answer_as_the_agent_it_was_addressed_to(self):
        task = write_task(self.ws, "task-abc123", ROOM, CORE_AGENT)
        d = self.ws / "deliveries" / W
        d.mkdir(parents=True)
        (d / "task-abc123.txt").write_text("")
        self.addCleanup(_gateway.set_task_file, None)
        _gateway.set_task_file(str(task))
        with mock.patch.dict("os.environ", SEAT):
            self.assertEqual(_gateway.gateway(ROOM)[1]["Authorization"], "Bearer core-secret")
            self.assertEqual(notify._gateway_config("ag2space", ROOM, str(task)), (ALPHA, "core-secret"))
        _gateway.set_task_file(None)
        self.assertEqual(self.bearer(SEAT), "own-secret")

    def test_a_task_file_that_does_not_bind_is_refused_by_both_clients(self):
        self.addCleanup(_gateway.set_task_file, None)
        _gateway.set_task_file(str(self.ws / "tasks" / "task-gone.txt"))
        with mock.patch.dict("os.environ", SEAT), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(_gateway.RoomConnectionError):
                _gateway.gateway(ROOM)
            self.assertIsNone(notify._gateway_config("ag2space", ROOM, str(self.ws / "tasks" / "task-gone.txt")))

    def test_a_room_no_identity_serves_is_refused_by_both_clients(self):
        with mock.patch.dict("os.environ", SEAT), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(_gateway.RoomConnectionError):
                _gateway.gateway("!r:gamma-fixture.net")
            self.assertIsNone(notify._gateway_config("ag2space", "!r:gamma-fixture.net"))


class WhoATaskIsAnsweredAs(Base):
    def test_the_lane_that_queued_a_task_names_its_agent(self):
        for tid, agent in ((f"task-{OWN}~abc123", OWN_AGENT), (f"task-{OWN}~abc123~~2", OWN_AGENT),
                           ("task-beta~abc123", BETA_AGENT), ("task-abc123", ""), ("task-abc123~~2", ""),
                           ("task-chat-1", ""), ("task-gone~abc123", ""), ("", "")):
            self.assertEqual(C.task_agent_mxid(tid, self.ws), agent, tid)

    def test_an_unreadable_record_names_no_one(self):
        C.record_path(self.ws).write_text("{")
        self.assertEqual(C.task_agent_mxid(f"task-{OWN}~abc123", self.ws), "")

    def test_its_activity_rows_carry_that_agent(self):
        task = {"id": f"task-{OWN}~abc123", "from": "@owner:alpha-fixture.net", "text": "hi"}
        first = activity_rows.append("Thinking", kind="thinking", room=ROOM, task=task, workspace=self.ws)
        activity_rows.append("replied", kind="done", room=ROOM, task=task, done=True, workspace=self.ws)
        core = activity_rows.append("Thinking", kind="thinking", room=CORE_DM, task={"id": "task-abc123"},
                                    workspace=self.ws)
        self.assertEqual(first["agent"], OWN_AGENT)
        self.assertNotIn("agent", core)
        logged = [json.loads(l) for l in activity_rows.log_path(self.ws).read_text().splitlines()]
        self.assertEqual([r.get("agent") for r in logged], [OWN_AGENT, OWN_AGENT, None])
        summary = json.loads(activity_rows.summaries_path(self.ws).read_text().splitlines()[0])
        self.assertEqual(summary["agent"], OWN_AGENT)

    def test_a_row_is_written_when_the_agent_cannot_be_named(self):
        with mock.patch.object(C, "task_agent_mxid", side_effect=RuntimeError("no record")):
            rec = activity_rows.append("Thinking", kind="thinking", room=ROOM, task={"id": f"task-{OWN}~abc123"},
                                       workspace=self.ws)
        self.assertNotIn("agent", rec)


class ChannelEnv(Base):
    def run_main(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = channel_env_resolve.main(["channel_env_resolve.py", str(self.root / "config" / "channels"), *args])
        return rc, err.getvalue()

    def test_a_workers_lane_is_named_as_vault_held_by_instance_or_channel_dir(self):
        for name in (OWN, C.channel_dir_for(OWN)):
            rc, err = self.run_main(name)
            self.assertEqual(rc, 1)
            self.assertIn("pool worker's own lane", err)
            self.assertIn("vault", err)
            self.assertNotIn("no channel dir", err)

    def test_any_other_missing_channel_still_says_so(self):
        rc, err = self.run_main("nowhere")
        self.assertEqual(rc, 1)
        self.assertIn("no channel dir", err)

    def test_a_record_that_cannot_be_read_names_no_workers_lane(self):
        with mock.patch.object(C, "worker_lane_named", side_effect=RuntimeError("no record")):
            rc, err = self.run_main(OWN)
        self.assertEqual(rc, 1)
        self.assertIn("no channel dir", err)
        C.record_path(self.ws).write_text("{")
        self.assertFalse(C.worker_lane_named(OWN, self.ws))

    def test_with_a_room_the_workers_session_is_told_its_lane_has_no_file(self):
        with mock.patch.dict("os.environ", SEAT):
            rc, err = self.run_main("ag2space", "--room", ROOM)
        self.assertEqual(rc, 1)
        self.assertIn(OWN, err)


if __name__ == "__main__":
    unittest.main()
