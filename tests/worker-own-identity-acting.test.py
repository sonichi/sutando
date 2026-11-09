#!/usr/bin/env python3
"""A pool worker that has its own agent acts as that agent from its own session.

The core owns the identity's lifecycle (add, rotate, retire) and its lane; the worker's session
uses it. `connections.acting_identity` is the one rule, reached by every room client through
`connection_for_room`:

- In the worker's session a room on its agent's server resolves to the worker's own row, its
  token (the vault's) and its agent. The core's session, another worker's session and a room on
  another server never do.
- A room local state says is the core's stays the core's: one where only a core lane has a task
  open, and a core lane's own owner DM.
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


def rows(**own):
    return [row("alpha-fixture.net", "alpha", ALPHA, CORE_AGENT, primary=True),
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


class TheCoresRooms(Base):
    def test_a_task_only_a_core_lane_holds_is_answered_as_the_core(self):
        self.open_task("alpha", ROOM)
        self.assertEqual(self.instance(ROOM, SEAT), "alpha")

    def test_a_task_its_own_lane_holds_is_its_own_whatever_the_core_holds(self):
        self.open_task("alpha", ROOM)
        self.open_task(OWN, ROOM)
        self.assertEqual(self.instance(ROOM, SEAT), OWN)

    def test_the_cores_owner_dm_stays_the_cores(self):
        self.owner_dm("alpha", CORE_AGENT, CORE_DM)
        self.assertEqual(self.instance(CORE_DM, SEAT), "alpha")
        self.assertEqual(self.instance(ROOM, SEAT), OWN)

    def test_a_room_id_naming_no_server_follows_the_lane_that_holds_it(self):
        self.open_task(OWN, "!bare")
        self.assertEqual(self.instance("!bare", SEAT), OWN)
        self.assertEqual(self.instance("!other", SEAT), "alpha")


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

    def test_with_a_room_the_workers_session_is_told_its_lane_has_no_file(self):
        with mock.patch.dict("os.environ", SEAT):
            rc, err = self.run_main("ag2space", "--room", ROOM)
        self.assertEqual(rc, 1)
        self.assertIn(OWN, err)


if __name__ == "__main__":
    unittest.main()
