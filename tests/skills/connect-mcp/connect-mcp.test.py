#!/usr/bin/env python3
"""The MCP sign-in card (skills/connect-mcp/scripts/connect_mcp.py).

- `start` acts only for the owner's own DM task: it starts the login on a private tmux server, reads
  the link, records a wait that holds no link, state or code, and prints a card with one url button.
- The waiter asks Claude Code for the server's status only once the login looks finished, writes one
  resume task, and after the login's time is up marks the wait so one more link may be offered.
- `paste` reads the address from STDIN only, types it into the login only when it is this pending
  wait's callback, and leaves the address in no output and no file.
- `card` puts the outcome under the sign-in card's id; `rearm` restarts only dead waiters.

`claude` and `tmux` are one fake that records every call and refuses any it does not know; no test
starts a login, a tmux server or a process, or writes outside its temporary workspace.

Run: python3 tests/skills/connect-mcp/connect-mcp.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "connect_mcp", ROOT / "skills" / "connect-mcp" / "scripts" / "connect_mcp.py")
cm = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(cm)
ltp = cm.wait_store.ltp
tmux_pane_keys = cm.tmux_pane_keys
ac = cm.ac

ROOM = "!dm:example.org"
OWNER = "@owner:example.org"
NOW = 1_789_000_000.0
PORT = 51234
STATE = "st4te-abc"
CODE = "s3cret-c0de-xyz"
LINK = (f"https://auth.example.com/authorize?response_type=code&client_id=c1&state={STATE}"
        f"&redirect_uri=http%3A%2F%2Flocalhost%3A{PORT}%2Fcallback&code_challenge=abc")
CALLBACK = f"http://localhost:{PORT}/callback?code={CODE}&state={STATE}"
NEEDS_AUTH = "luma:\n  Scope: User config\n  Status: ! Needs authentication\n  Type: http\n"
CONNECTED = "luma:\n  Scope: User config\n  Status: ✔ Connected\n  Type: http\n"


class FakeHost:
    """`tmux` on each login's own socket, the guarded key sender and `claude mcp get`, in memory.
    Any other call, a bare send-keys included, fails the test."""

    def __init__(self, status=NEEDS_AUTH, prints=f"Open this URL to sign in:\n{LINK}\nPaste the redirect URL: "):
        self.calls: list[list] = []
        self.status = status
        self.prints = prints
        self.panes: dict[str, str] = {}
        self.sockets: dict[str, str] = {}
        self.sends: list[tuple[str, str, str]] = []
        self.buffers: dict[tuple[str, str], str] = {}
        self.send_code = 0
        self.new_session_code = 0

    def __call__(self, argv, timeout, stdin_text=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["claude", "mcp", "get"]:
            return (0, self.status) if self.status else (1, f'No MCP server found with name: "{argv[3]}"')
        if argv[:2] == ["bash", str(tmux_pane_keys.SCRIPT)]:
            socket, target = argv[argv.index("-S") + 1], argv[argv.index("-t") + 1]
            session = target.lstrip("=").rstrip(":")
            assert self.sockets.get(session) == socket, "the paste goes to the login's own tmux server"
            predicate, action = argv[argv.index("--fence") + 1:argv.index("--fence") + 3]
            assert predicate == "#{==:#{pane_in_mode},0}" and action.startswith("paste-buffer -d -b "), action
            if self.send_code == 0:
                self.sends.append((socket, session, self.buffers.pop((socket, action.split()[3]))))
            return self.send_code, "tmux-pane-keys: output the caller must not repeat"
        assert argv[:2] == ["tmux", "-S"], f"not a tmux call on an explicit socket: {argv[:3]}"
        socket, verb, rest = argv[2], argv[3], argv[4:]
        assert cm.SOCKET_DIR_PREFIX in socket, f"not a login's own socket: {socket}"
        target = rest[rest.index("-t") + 1].lstrip("=").rstrip(":") if "-t" in rest else ""
        if verb == "new-session":
            if self.new_session_code:
                return self.new_session_code, "no server"
            session = rest[rest.index("-s") + 1]
            self.panes[session], self.sockets[session] = self.prints, socket
            return 0, ""
        if verb == "load-buffer":
            assert rest[-1] == "-" and stdin_text is not None, "a buffer is loaded from stdin only"
            self.buffers[(socket, rest[rest.index("-b") + 1])] = stdin_text
            return 0, ""
        if verb == "delete-buffer":
            return (0 if self.buffers.pop((socket, rest[rest.index("-b") + 1]), None) is not None else 1), ""
        if verb == "kill-server":
            gone = [n for n, sock in self.sockets.items() if sock == socket]
            for name in gone:
                self.panes.pop(name, None)
                self.sockets.pop(name)
            return (0 if gone else 1), ""
        if target and target in self.panes and self.sockets[target] != socket:
            return 1, "no server running"
        if verb == "has-session":
            return (0 if target in self.panes else 1), ""
        if verb == "capture-pane":
            return (0, self.panes[target]) if target in self.panes else (1, "can't find session")
        raise AssertionError(f"unexpected tmux call: {argv}")

    def of(self, *head):
        return [c for c in self.calls if c[:len(head)] == list(head)]

    def tmux(self, verb):
        return [c for c in self.calls if c[:1] == ["tmux"] and c[3] == verb]


class Clock:
    def __init__(self, start=NOW):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def dm_text(task="task-abc", **fields):
    """A gateway task file: id, source and room above the body, the rest below it."""
    head = {"id": task, "source": "ag2space", "channel_id": ROOM}
    tail = {"room_member_count": "2", "channel_kind": "dm", "source_message_id": "$evt1", "user_id": OWNER,
            "interaction_type": "message", "access_tier": "owner"}
    for key, value in fields.items():
        (head if key in head or key == "collaborator" else tail)[key] = value
    lines = [f"{k}: {v}" for k, v in head.items() if v is not None]
    lines.append("task: what events do I have on luma")
    lines += [f"{k}: {v}" for k, v in tail.items() if v is not None]
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        self.host = FakeHost()
        self.clock = Clock()
        self.spawned: list[str] = []
        self.task()
        sockets = mock.patch.object(cm, "socket_path", lambda wait_id: str(
            self.ws.parent / f"{self.ws.name}-sock" / f"{cm.SOCKET_DIR_PREFIX}{wait_id}" / "s"))
        sockets.start()
        self.addCleanup(sockets.stop)
        self.addCleanup(lambda: shutil.rmtree(self.ws.parent / f"{self.ws.name}-sock", ignore_errors=True))

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, task="task-abc", **fields):
        (self.ws / "tasks" / f"{task}.txt").write_text(dm_text(task, **fields))

    def spawn(self, ws, wait_id):
        self.spawned.append(wait_id)
        return 4242

    def cli(self, argv, stdin=None, spawn=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cm.main(["--workspace", str(self.ws), *argv], run=self.host, spawn=spawn or self.spawn,
                           now=self.clock.now, sleep=self.clock.sleep,
                           stdin=io.StringIO(stdin) if stdin is not None else io.StringIO(""))
        self.stdout = out.getvalue()
        return code, json.loads(self.stdout)

    def start(self, name="luma", task="task-abc", request="what events do I have on luma"):
        return self.cli(["start", name, "--task", task, "--request", request])

    def waiter(self, wait_id):
        return cm.run_waiter(self.ws, wait_id, self.host, self.clock.now, self.clock.sleep)

    def new_tasks(self):
        return sorted(p.name for p in (self.ws / "tasks").iterdir() if p.name.startswith("task-connect-mcp-"))

    def everything_written(self):
        return "".join(p.read_text(errors="replace") for p in self.ws.rglob("*") if p.is_file())


class StartTests(Base):
    def test_the_owner_dm_gets_a_card_with_one_link_button(self):
        code, out = self.start()
        self.assertEqual(code, 0)
        [new] = self.host.tmux("new-session")
        self.assertEqual(new[-5:], ["claude", "mcp", "login", "luma", "--no-browser"])
        marker = cm.store(self.ws).read_marker(out["wait_id"])
        self.assertEqual(new[:3], ["tmux", "-S", marker["socket"]])
        self.assertEqual(Path(marker["socket"]).parent.name, cm.SOCKET_DIR_PREFIX + out["wait_id"])
        self.assertEqual(Path(marker["socket"]).parent.stat().st_mode & 0o777, 0o700)
        cwd = Path(new[new.index("-c") + 1]).resolve()
        self.assertNotIn(ROOT.resolve(), [cwd, *cwd.parents], "the login starts outside the checkout")
        card = out["message"]["extra_content"][ac.CARD_KEY]
        self.assertIsNone(ac.problem(card))
        self.assertEqual(card["header"]["title"], "Sign in to Luma")
        self.assertEqual(card["id"], "mcp-signin:luma")
        self.assertEqual([(b["kind"], b["url"]) for b in card["buttons"]], [("url", LINK)])
        self.assertIn("phone", card["note"])
        self.assertIn("Send me its address", card["note"])
        self.assertEqual(card["expires_at"], int(NOW + cm.LOGIN_S) * 1000)
        self.assertEqual(out["message"]["reply_to"], "$evt1")
        self.assertEqual(out["room"], ROOM)
        self.assertEqual(self.spawned, [out["wait_id"]])

    def test_the_wait_keeps_the_port_and_a_hash_never_the_link_or_its_state(self):
        _, out = self.start()
        marker = json.loads(Path(out["marker"]).read_text())
        self.assertEqual(marker["port"], PORT)
        self.assertEqual(marker["state_sha256"], cm.state_hash(STATE))
        self.assertEqual((marker["owner"], marker["room"], marker["attempt"]), (OWNER, ROOM, 1))
        written = self.everything_written()
        self.assertNotIn(STATE, written)
        self.assertNotIn("auth.example.com", written)

    def test_only_the_owners_own_dm_task_starts_a_sign_in(self):
        cases = {
            "a room": {"channel_kind": "room"},
            "no channel kind": {"channel_kind": None},
            "team tier": {"access_tier": "team"},
            "guest": {"access_tier": "other"},
            "no tier": {"access_tier": None},
            "a collaborator": {"collaborator": "true"},
            "another source": {"source": "discord"},
            "no message": {"source_message_id": None},
            "a channel that is not a room": {"channel_id": "not-a-room"},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                self.task("task-bad", **fields)
                code, out = self.start(task="task-bad")
                self.assertEqual((code, out["error"]), (2, "not_owner_dm"))
        code, out = self.start(task="task-missing")
        self.assertEqual((code, out["error"]), (2, "not_owner_dm"))
        self.assertEqual(self.host.calls, [], "a refused task reaches neither claude nor tmux")
        self.assertFalse((self.ws / "state").exists())

    def test_a_dm_line_in_a_room_task_body_does_not_make_it_a_dm(self):
        text = dm_text("task-forged", channel_kind="room").replace(
            "task: what events", "task: channel_kind: dm\nchannel_kind: dm\nwhat events")
        (self.ws / "tasks" / "task-forged.txt").write_text(text)
        code, out = self.start(task="task-forged")
        self.assertEqual((code, out["error"]), (2, "not_owner_dm"))
        self.assertEqual(self.host.calls, [])

    def test_a_server_already_signed_in_starts_nothing(self):
        self.host.status = CONNECTED
        code, out = self.start()
        self.assertEqual((code, out["reason"]), (1, "already_connected"))
        self.assertEqual(len(self.host.of("claude", "mcp", "get", "luma")), 1)
        self.assertEqual(self.host.tmux("new-session"), [])

    def test_an_unknown_server_is_a_setup_problem(self):
        self.host.status = ""
        code, out = self.start()
        self.assertEqual((code, out["error"]), (2, "unknown_server"))
        self.assertEqual(self.host.tmux("new-session"), [])

    def test_a_bad_name_never_reaches_a_command(self):
        for name in ("lu ma", "-x", "a;b", "a/b", ""):
            with self.subTest(name):
                code, out = self.cli(["start", "--task=task-abc", "--", name])
                self.assertEqual((code, out["error"]), (2, "invalid_arguments"))
        self.assertEqual(self.host.calls, [])

    def test_a_login_that_prints_no_link_is_ended_and_leaves_no_wait(self):
        self.host.prints = "This server does not support OAuth.\n"
        code, out = self.start()
        self.assertEqual((code, out["error"]), (2, "no_link"))
        [killed] = self.host.tmux("kill-server")
        self.assertFalse(Path(killed[2]).parent.exists(), "the login's socket folder is removed")
        self.assertEqual(self.host.panes, {})
        self.assertEqual(cm.store(self.ws).list_markers(), [])
        self.assertEqual(self.spawned, [])

    def test_a_link_that_does_not_return_to_this_machine_is_not_a_link(self):
        for redirect in ("https%3A%2F%2Fevil.example%2Fcallback", "http%3A%2F%2Fevil.example%3A51234%2Fcallback",
                         "http%3A%2F%2Flocalhost%3A51234%2Fother", "http%3A%2F%2Flocalhost%2Fcallback",
                         "http%3A%2F%2Flocalhost%3A99999999%2Fcallback"):
            with self.subTest(redirect):
                self.host.prints = f"https://auth.example.com/authorize?state=s&redirect_uri={redirect}\n"
                code, out = self.start()
                self.assertEqual((code, out["error"]), (2, "no_link"))

    def test_asking_again_while_the_link_is_live_shows_the_same_sign_in(self):
        _, first = self.start()
        self.task("task-two")
        code, again = self.start(task="task-two")
        self.assertEqual(code, 0)
        self.assertTrue(again["reused"])
        self.assertEqual(again["wait_id"], first["wait_id"])
        self.assertEqual(again["message"]["extra_content"][ac.CARD_KEY]["buttons"][0]["url"], LINK)
        self.assertEqual(len(self.host.tmux("new-session")), 1)
        self.assertEqual(len(cm.store(self.ws).list_markers()), 1)

    def test_an_old_login_past_its_time_is_replaced(self):
        _, first = self.start()
        self.clock.t += cm.LOGIN_S + 1
        code, second = self.start()
        self.assertEqual(code, 0)
        self.assertNotEqual(second["wait_id"], first["wait_id"])
        self.assertEqual(list(self.host.panes), [cm.session_name(second["wait_id"])])
        claimed = json.loads(cm.store(self.ws).claimed_path(first["wait_id"]).read_text())
        self.assertEqual(claimed["claimed_by"], "superseded")

    def test_the_default_runner_is_looked_up_when_called(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((list(argv), kwargs.get("cwd")))
            return mock.Mock(returncode=1, stdout="")

        out = io.StringIO()
        with mock.patch.object(cm.subprocess, "run", fake_run), contextlib.redirect_stdout(out):
            code = cm.main(["--workspace", str(self.ws), "start", "luma", "--task", "task-abc"], spawn=self.spawn)
        self.assertEqual((code, json.loads(out.getvalue())["error"]), (2, "unknown_server"))
        self.assertEqual([argv for argv, _ in seen], [["claude", "mcp", "get", "luma"]])
        cwd = Path(seen[0][1]).resolve()
        self.assertNotIn(ROOT.resolve(), [cwd, *cwd.parents])


class StartEdgeTests(Base):
    def test_a_link_without_a_state_is_not_a_link(self):
        self.host.prints = f"https://auth.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A{PORT}%2Fcallback\n"
        code, out = self.start()
        self.assertEqual((code, out["error"]), (2, "no_link"))
        self.assertEqual(cm.store(self.ws).list_markers(), [])

    def test_a_task_id_that_is_not_one_reads_no_file(self):
        for task in ("../task-abc", "abc", "task-a b"):
            with self.subTest(task):
                code, out = self.cli(["start", "luma", "--task", task])
                self.assertEqual((code, out["error"]), (2, "invalid_arguments"))
        self.assertEqual(self.host.calls, [])

    def test_the_request_is_read_from_stdin_or_a_file_and_kept_short(self):
        code, out = self.cli(["start", "luma", "--task", "task-abc", "--request-file", "-"],
                             stdin="what is   on\nluma " + "x" * 2000)
        marker = cm.store(self.ws).read_marker(out["wait_id"])
        self.assertEqual(code, 0)
        self.assertTrue(marker["request"].startswith("what is on luma x"))
        self.assertEqual(len(marker["request"]), cm.MAX_REQUEST_CHARS)
        asked = self.ws / "asked.txt"
        asked.write_text("my next luma event\n")
        code, out = self.cli(["start", "other", "--task", "task-abc", "--request-file", str(asked)])
        self.assertEqual(cm.store(self.ws).read_marker(out["wait_id"])["request"], "my next luma event")

    def test_a_socket_folder_that_cannot_be_made_starts_no_login(self):
        blocker = self.ws / "blocker"
        blocker.write_text("")
        with mock.patch.object(cm, "socket_path", lambda wait_id: str(blocker / f"sutando-mcp-{wait_id}" / "s")):
            code, out = self.start()
        self.assertEqual((code, out["error"]), (2, "login_not_started"))
        self.assertEqual(self.host.of("tmux"), [])
        self.assertEqual(cm.store(self.ws).list_markers(), [])

    def test_a_tmux_that_will_not_start_leaves_no_wait_and_no_folder(self):
        self.host.new_session_code = 1
        code, out = self.start()
        self.assertEqual((code, out["error"]), (2, "login_not_started"))
        [new] = self.host.tmux("new-session")
        self.assertFalse(Path(new[2]).parent.exists())
        self.assertEqual(cm.store(self.ws).list_markers(), [])
        self.assertEqual(self.spawned, [])

    def test_a_wait_that_cannot_be_written_ends_the_login_it_started(self):
        with mock.patch.object(cm.wait_store.WaitStore, "write_marker", side_effect=OSError("read-only")):
            code, out = self.start()
        self.assertEqual((code, out["error"], out["detail"]), (2, "not_written", "OSError"))
        self.assertEqual(self.host.panes, {})
        self.assertEqual(len(self.host.tmux("kill-server")), 1)
        self.assertEqual(self.spawned, [])
        self.assertNotIn("auth.example.com", self.stdout)

    def test_a_waiter_that_cannot_start_still_leaves_the_card_and_the_wait(self):
        def no_spawn(ws, wait_id):
            raise OSError("no more processes")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out = self.cli(["start", "luma", "--task", "task-abc"], spawn=no_spawn)
        self.assertEqual((code, out["waiter_pid"]), (0, None))
        self.assertIsNotNone(out["message"])
        self.assertIsNotNone(cm.store(self.ws).read_marker(out["wait_id"]))
        self.assertIn("could not start the waiter", err.getvalue())

    def test_text_for_a_command_goes_to_its_stdin_and_nothing_else_does(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(kwargs)
            return mock.Mock(returncode=0, stdout="ok")

        with mock.patch.object(cm.subprocess, "run", fake_run):
            self.assertEqual(cm.run_command(["tmux", "x"], 1.0, "secret\n"), (0, "ok"))
            cm.run_command(["tmux", "x"], 1.0)
        self.assertEqual((seen[0]["input"], "stdin" in seen[0]), ("secret\n", False))
        self.assertEqual((seen[1]["stdin"], "input" in seen[1]), (cm.subprocess.DEVNULL, False))

    def test_a_command_that_cannot_run_or_hangs_reads_as_a_failure(self):
        for error in (OSError("no such file"), cm.subprocess.TimeoutExpired("claude", 1)):
            with self.subTest(type(error).__name__), mock.patch.object(cm.subprocess, "run", side_effect=error):
                self.assertEqual(cm.run_command(["claude", "mcp", "get", "luma"], 1.0), (127, ""))

    def test_the_workspace_comes_from_the_configured_one_and_falls_back_to_the_checkout(self):
        _, out = self.start()
        fake = mock.Mock(resolve_workspace=lambda: str(self.ws))
        with mock.patch.dict(sys.modules, {"sutando_config": fake}):
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown):
                code = cm.main(["status"], run=self.host, spawn=self.spawn, now=self.clock.now)
        self.assertEqual(code, 0)
        self.assertEqual([p["wait_id"] for p in json.loads(shown.getvalue())["pending"]], [out["wait_id"]])
        broken = mock.Mock(resolve_workspace=mock.Mock(side_effect=RuntimeError("unreadable config")))
        with mock.patch.dict(sys.modules, {"sutando_config": broken}):
            self.assertEqual(cm._workspace(), cm.REPO_ROOT / "workspace")


class WaiterTests(Base):
    def waiter_cli(self, wait_id):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cm.main(["--workspace", str(self.ws), "waiter", wait_id], run=self.host, spawn=self.spawn,
                           now=self.clock.now, sleep=self.clock.sleep)
        return code, out.getvalue()

    def test_the_waiter_command_prints_its_outcome_line(self):
        _, out = self.start()
        code, line = self.waiter_cli(out["wait_id"])
        self.assertEqual(code, 0)
        self.assertTrue(line.strip().endswith(f"waiter {out['wait_id']}: timeout"), line)
        self.assertEqual(len(self.new_tasks()), 1)

    def test_the_waiter_command_refuses_what_is_not_a_wait_id(self):
        code, line = self.waiter_cli("../../etc")
        self.assertEqual((code, json.loads(line)["error"]), (2, "invalid_arguments"))
        self.assertEqual(self.host.calls, [])
        self.assertFalse((self.ws / "state").exists())

    def stopped_after_the_claim(self, outcome="connected"):
        """The files a waiter leaves when it stops between claiming the wait and writing its task."""
        _, out = self.start()
        self.assertIsNotNone(cm.store(self.ws).claim(out["wait_id"], outcome, self.clock.now()))
        return out["wait_id"]

    def test_a_waiter_stopped_after_the_claim_is_made_good_by_rearm_once(self):
        wait_id = self.stopped_after_the_claim()
        self.assertEqual(self.new_tasks(), [])
        code, out = self.cli(["rearm"])
        self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "task"}])
        self.assertEqual(self.new_tasks(), [f"task-connect-mcp-{wait_id}.txt"])
        body = ltp.parse_task_headers((self.ws / "tasks" / self.new_tasks()[0]).read_text()).body
        self.assertIn("Luma is now signed in", body)
        first = (self.ws / "tasks" / self.new_tasks()[0]).stat().st_mtime_ns
        code, again = self.cli(["rearm"])
        self.assertEqual(again["recovered"], [])
        self.assertEqual(self.new_tasks(), [f"task-connect-mcp-{wait_id}.txt"])
        self.assertEqual((self.ws / "tasks" / self.new_tasks()[0]).stat().st_mtime_ns, first)

    def test_a_waiter_stopped_after_the_task_and_before_its_stamp_writes_no_second_task(self):
        wait_id = self.stopped_after_the_claim("timeout")
        record = json.loads(cm.store(self.ws).claimed_path(wait_id).read_text())
        path = cm.write_resume_task(self.ws, record, "timeout", self.clock.now())
        written = path.read_bytes()
        code, out = self.cli(["rearm"])
        self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "found"}])
        self.assertEqual(path.read_bytes(), written)
        archive = self.ws / "tasks" / "archive"
        archive.mkdir()
        path.rename(archive / path.name)
        code, out = self.cli(["rearm"])
        self.assertEqual((out["recovered"], self.new_tasks()), ([], []))

    def test_a_task_the_core_already_archived_is_not_written_again(self):
        wait_id = self.stopped_after_the_claim()
        record = json.loads(cm.store(self.ws).claimed_path(wait_id).read_text())
        path = cm.write_resume_task(self.ws, record, "connected", self.clock.now())
        archive = self.ws / "tasks" / "archive"
        archive.mkdir()
        path.rename(archive / path.name)
        code, out = self.cli(["rearm"])
        self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "found"}])
        self.assertEqual(self.new_tasks(), [])

    def test_a_restarted_waiter_publishes_the_claim_it_finds(self):
        wait_id = self.stopped_after_the_claim()
        self.assertEqual(self.waiter(wait_id), "recovered")
        self.assertEqual(self.new_tasks(), [f"task-connect-mcp-{wait_id}.txt"])
        self.assertEqual(self.waiter(wait_id), "claimed_elsewhere")
        code, out = self.cli(["rearm"])
        self.assertEqual((out["recovered"], len(self.new_tasks())), ([], 1))

    def test_a_finished_waiter_leaves_nothing_for_rearm_to_recover(self):
        _, out = self.start()
        self.assertEqual(self.waiter(out["wait_id"]), "timeout")
        record = json.loads(cm.store(self.ws).claimed_path(out["wait_id"]).read_text())
        self.assertEqual(record["published"], "task")
        code, again = self.cli(["rearm"])
        self.assertEqual((again["recovered"], len(self.new_tasks())), ([], 1))

    def test_a_claim_that_says_nothing_stays_silent_on_rearm(self):
        for outcome in ("superseded", "invalid", "expired"):
            with self.subTest(outcome):
                self.stopped_after_the_claim(outcome)
                code, out = self.cli(["rearm"])
                self.assertEqual((out["recovered"], self.new_tasks()), ([], []))

    def test_a_claim_found_long_after_gets_no_late_task(self):
        wait_id = self.stopped_after_the_claim()
        self.clock.t += cm.STALE_GRACE_S
        code, out = self.cli(["rearm"])
        self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "stale"}])
        self.assertEqual(self.new_tasks(), [])

    def test_a_claim_whose_waiter_is_alive_is_left_to_it(self):
        wait_id = self.stopped_after_the_claim()
        held = cm.store(self.ws).acquire_lock(wait_id)
        try:
            code, out = self.cli(["rearm"])
        finally:
            held.close()
        self.assertEqual((out["recovered"], self.new_tasks()), ([], []))

    def test_a_recovery_that_cannot_write_tries_again_next_time(self):
        wait_id = self.stopped_after_the_claim()
        with mock.patch.object(ltp, "write_task_file", side_effect=OSError("disk full")):
            code, out = self.cli(["rearm"])
        self.assertEqual((out["recovered"], self.new_tasks()), ([], []))
        code, out = self.cli(["rearm"])
        self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "task"}])

    def stopped_after_taking(self, outcome="connected", staged=True):
        """The files a waiter leaves when it stops after taking the wait, before its outcome is placed."""
        _, out = self.start()
        real, seen = os.rename, []

        class Stopped(BaseException):
            pass

        def rename(src, dst):
            seen.append(dst)
            if len(seen) == 2:
                raise Stopped()
            real(src, dst)

        with contextlib.ExitStack() as stack:
            if not staged:
                stack.enter_context(mock.patch.object(cm.wait_store.WaitStore, "write_json",
                                                      side_effect=OSError("disk full")))
            stack.enter_context(mock.patch.object(os, "rename", rename))
            with self.assertRaises(Stopped):
                cm.store(self.ws).claim(out["wait_id"], outcome, self.clock.now())
        self.assertFalse(cm.store(self.ws).claimed_path(out["wait_id"]).exists())
        return out["wait_id"]

    def resume_body(self, wait_id):
        return ltp.parse_task_headers((self.ws / "tasks" / f"task-connect-mcp-{wait_id}.txt").read_text()).body

    def test_a_waiter_stopped_before_its_outcome_was_placed_resumes_once_with_that_outcome(self):
        wait_id = self.stopped_after_taking("connected")
        code, out = self.cli(["rearm"])
        self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "task"}])
        self.assertIn("Luma is now signed in", self.resume_body(wait_id))
        code, again = self.cli(["rearm"])
        self.assertEqual((again["recovered"], len(self.new_tasks())), ([], 1))
        self.assertEqual(self.waiter(wait_id), "claimed_elsewhere")
        self.assertEqual(len(self.new_tasks()), 1)

    def test_a_restarted_waiter_finishes_a_claim_stopped_before_its_outcome_was_placed(self):
        wait_id = self.stopped_after_taking("timeout")
        self.assertEqual(self.waiter(wait_id), "recovered")
        self.assertIn("expired", self.resume_body(wait_id))
        code, out = self.cli(["rearm"])
        self.assertEqual((out["recovered"], len(self.new_tasks())), ([], 1))

    def test_a_claim_with_no_outcome_is_kept_then_closed_as_expired_never_as_signed_in(self):
        wait_id = self.stopped_after_taking("connected", staged=False)
        since = self.clock.now()
        err = io.StringIO()
        with mock.patch.object(cm.wait_store.WaitStore, "claimed_since", lambda store, w: since), \
                contextlib.redirect_stderr(err):
            code, out = self.cli(["rearm"])
            self.assertEqual((out["recovered"], self.new_tasks()), ([], []))
            self.assertTrue(cm.store(self.ws).claimed_path(wait_id).exists(), "kept, not dropped")
            self.clock.t += cm.wait_store.UNSTAMPED_SETTLE_S
            code, out = self.cli(["rearm"])
            self.assertEqual(out["recovered"], [{"wait_id": wait_id, "published": "task"}])
            code, out = self.cli(["rearm"])
        self.assertIn("names no outcome yet", err.getvalue())
        self.assertEqual((out["recovered"], len(self.new_tasks())), ([], 1))
        self.assertIn("expired", self.resume_body(wait_id))
        self.assertNotIn("is now signed in", self.resume_body(wait_id))
        code, card = self.cli(["card", wait_id])
        self.assertEqual((code, card["outcome"]), (0, "timeout"))

    def test_a_wait_file_that_is_not_a_wait_is_set_aside_without_a_task(self):
        _, out = self.start()
        waits = cm.store(self.ws)
        waits.marker_path(out["wait_id"]).write_text(json.dumps({"wait_id": out["wait_id"], "name": "luma"}))
        self.assertEqual(self.waiter(out["wait_id"]), "invalid")
        self.assertEqual(json.loads(waits.claimed_path(out["wait_id"]).read_text())["name"], "luma")
        self.assertEqual(self.new_tasks(), [])

    def test_a_waiter_that_loses_the_claim_writes_nothing_and_ends_nothing(self):
        _, out = self.start()
        self.host.status = CONNECTED
        self.host.panes[cm.session_name(out["wait_id"])] += "\nAuthenticated\n"
        with mock.patch.object(cm.wait_store.WaitStore, "claim", return_value=None):
            self.assertEqual(self.waiter(out["wait_id"]), "claimed_elsewhere")
        self.assertEqual(self.new_tasks(), [])
        self.assertEqual(self.host.tmux("kill-server"), [])

    def test_a_wait_spoiled_before_the_claim_gets_no_task(self):
        _, out = self.start()
        self.host.panes.clear()
        spoiled = {"wait_id": out["wait_id"], "invalid": True}
        with mock.patch.object(cm.wait_store.WaitStore, "claim", return_value=spoiled):
            self.assertEqual(self.waiter(out["wait_id"]), "invalid")
        self.assertEqual(self.new_tasks(), [])

    def test_a_task_that_cannot_be_written_nor_the_wait_put_back_is_reported(self):
        _, out = self.start()
        self.host.panes.clear()
        err = io.StringIO()
        with mock.patch.object(ltp, "write_task_file", side_effect=OSError("disk full")), \
                mock.patch.object(cm.wait_store.WaitStore, "release", return_value=False), \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.waiter(out["wait_id"]), "write_failed")
        self.assertIn("not written", err.getvalue())
        self.assertEqual(self.new_tasks(), [])

    def test_a_finished_login_writes_one_resume_task_and_ends_the_session(self):
        _, out = self.start()
        wait_id = out["wait_id"]
        session = cm.session_name(wait_id)
        self.host.panes[session] += '\nAuthenticated with "luma". Its tools are now available.\n'
        self.host.status = CONNECTED
        self.assertEqual(self.waiter(wait_id), "connected")
        self.assertEqual(self.new_tasks(), [f"task-connect-mcp-{wait_id}.txt"])
        text = (self.ws / "tasks" / self.new_tasks()[0]).read_text()
        parsed = ltp.parse_task_headers(text)
        self.assertEqual(parsed.headers["source"], cm.RESUME_SOURCE)
        self.assertEqual(parsed.headers["channel_id"], ROOM)
        self.assertEqual(parsed.headers["user_id"], OWNER)
        self.assertEqual(parsed.headers["access_tier"], "owner")
        self.assertIn("Luma is now signed in", parsed.body)
        self.assertIn("what events do I have on luma", parsed.body)
        self.assertIn(f"connect_mcp.py card {wait_id}", parsed.body)
        self.assertNotIn("auth.example.com", text)
        self.assertNotIn(session, self.host.panes)
        self.assertFalse(Path(cm.socket_path(wait_id)).parent.exists())
        self.assertEqual(self.waiter(wait_id), "claimed_elsewhere")
        self.assertEqual(len(self.new_tasks()), 1)

    def test_a_login_that_exited_signed_in_counts_as_connected(self):
        _, out = self.start()
        self.host.panes.clear()
        self.host.status = CONNECTED
        self.assertEqual(self.waiter(out["wait_id"]), "connected")

    def test_claude_is_not_asked_while_the_login_is_still_waiting(self):
        _, out = self.start()
        asked = len(self.host.of("claude", "mcp", "get"))
        self.assertEqual(self.waiter(out["wait_id"]), "timeout")
        self.assertGreater(len(self.host.tmux("capture-pane")), 100, "the pane was polled until the deadline")
        self.assertEqual(len(self.host.of("claude", "mcp", "get")) - asked, 1)

    def test_time_up_marks_the_wait_and_offers_one_more_link(self):
        _, out = self.start()
        self.assertEqual(self.waiter(out["wait_id"]), "timeout")
        self.assertEqual(self.host.panes, {})
        body = ltp.parse_task_headers((self.ws / "tasks" / self.new_tasks()[0]).read_text()).body
        self.assertIn("expired", body)
        self.assertIn("try again", body)
        code, card = self.cli(["card", out["wait_id"]])
        self.assertEqual((code, card["outcome"], card["card_id"]), (0, "timeout", "mcp-signin:luma"))
        self.assertEqual(card["message"]["extra_content"][ac.CARD_KEY]["state"], "expired")
        self.assertIn("try again", card["message"]["body"])

        self.task("task-again")
        code, second = self.start(task="task-again")
        self.assertEqual((code, second["attempt"]), (0, 2))
        self.assertEqual(self.waiter(second["wait_id"]), "timeout")
        body = ltp.parse_task_headers(
            (self.ws / "tasks" / f"task-connect-mcp-{second['wait_id']}.txt").read_text()).body
        self.assertIn("no further link", body)
        self.assertNotIn("try again", body)
        _, card = self.cli(["card", second["wait_id"]])
        self.assertNotIn("try again", card["message"]["body"])

    def test_a_wait_found_long_after_its_time_ends_without_a_task(self):
        _, out = self.start()
        self.clock.t += cm.LOGIN_S + cm.STALE_GRACE_S + 1
        self.assertEqual(self.waiter(out["wait_id"]), "expired")
        self.assertEqual(self.new_tasks(), [])
        self.assertEqual(self.host.panes, {})

    def test_a_second_waiter_stands_down(self):
        _, out = self.start()
        held = cm.store(self.ws).acquire_lock(out["wait_id"])
        try:
            self.assertEqual(self.waiter(out["wait_id"]), "busy")
        finally:
            held.close()

    def test_a_task_that_cannot_be_written_releases_the_wait(self):
        _, out = self.start()
        self.host.panes.clear()
        self.host.status = CONNECTED
        attempts = []

        def flaky(*a, **k):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("disk full")
            return real(*a, **k)

        real = ltp.write_task_file
        with mock.patch.object(ltp, "write_task_file", side_effect=flaky), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.waiter(out["wait_id"]), "connected")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.new_tasks()), 1)


class PasteTests(Base):
    def setUp(self):
        super().setUp()
        _, out = self.start()
        self.wait_id = out["wait_id"]
        self.session = cm.session_name(self.wait_id)
        self.task("task-paste")

    def paste(self, text, task="task-paste", wait_id=None):
        return self.cli(["paste", wait_id or self.wait_id, "--task", task], stdin=text)

    def test_the_callback_is_typed_into_the_login_and_kept_nowhere(self):
        code, out = self.paste(CALLBACK + "\n")
        self.assertEqual((code, out["status"]), (0, "sent"))
        socket = cm.store(self.ws).read_marker(self.wait_id)["socket"]
        self.assertEqual(self.host.sends, [(socket, self.session, CALLBACK + "\n")])
        [loaded] = self.host.tmux("load-buffer")
        [guarded] = self.host.of("bash")
        self.assertEqual(guarded, tmux_pane_keys.fence_argv(
            socket, f"={self.session}:", "#{==:#{pane_in_mode},0}",
            f"paste-buffer -d -b {loaded[loaded.index('-b') + 1]} -t '={self.session}:'"))
        self.assertEqual(self.host.buffers, {}, "the paste removed its buffer")
        for argv in self.host.calls:
            self.assertFalse(any(CODE in str(a) or STATE in str(a) for a in argv), "the address is no argument")
        self.assertNotIn(CODE, self.stdout)
        self.assertNotIn(CODE, self.everything_written())
        self.assertNotIn(STATE, self.everything_written())

    def test_each_attempt_pastes_from_a_buffer_of_its_own_name(self):
        self.host.send_code = 1
        self.paste(CALLBACK)
        self.paste(CALLBACK)
        names = [c[c.index("-b") + 1] for c in self.host.tmux("load-buffer")]
        self.assertEqual(len(set(names)), 2)
        self.assertEqual([c[c.index("-b") + 1] for c in self.host.tmux("delete-buffer")], names)
        self.assertEqual(self.host.buffers, {})

    def test_a_busy_return_leaves_the_send_in_flight_untouched(self):
        socket = cm.store(self.ws).read_marker(self.wait_id)["socket"]
        work = Path(socket).parent / "tmux-pane-keys.other"
        guard = tmux_pane_keys.guard_path(socket)
        for folder in (work, guard):
            folder.mkdir(parents=True)
        (work / "cmd").write_text("paste-buffer -d -b cb-other -t 'x'\n")
        (work / "ticket").write_text("")
        (guard / "pending.json").write_text(json.dumps({"ticket": str(work / "ticket")}))
        self.host.buffers[(socket, "cb-other")] = "another attempt's address\n"
        before = {p: p.read_bytes() for p in Path(socket).parent.rglob("*") if p.is_file()}
        self.host.send_code = tmux_pane_keys.BUSY
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["status"], out["reason"]), (1, "refused", "busy"))
        self.assertEqual({p: p.read_bytes() for p in Path(socket).parent.rglob("*") if p.is_file()}, before)
        self.assertEqual(self.host.buffers, {(socket, "cb-other"): "another attempt's address\n"})
        [own] = self.host.tmux("delete-buffer")
        self.assertNotEqual(own[own.index("-b") + 1], "cb-other")

    def test_a_buffer_that_cannot_be_loaded_types_nothing(self):
        real = self.host.__call__

        def no_buffers(argv, timeout, stdin_text=None):
            if "load-buffer" in argv:
                self.host.calls.append(list(argv))
                return 1, ""
            return real(argv, timeout, stdin_text)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cm.main(["--workspace", str(self.ws), "paste", self.wait_id, "--task", "task-paste"],
                           run=no_buffers, now=self.clock.now, stdin=io.StringIO(CALLBACK))
        self.assertEqual((code, json.loads(out.getvalue())["reason"]), (1, "not_typed"))
        self.assertEqual(self.host.of("bash"), [])

    def test_a_send_that_proved_nothing_was_typed_is_refused_not_sent(self):
        for code, reason in ((1, "not_typed"), (124, "not_typed"), (tmux_pane_keys.BUSY, "busy")):
            with self.subTest(code):
                self.host.send_code = code
                rc, out = self.paste(CALLBACK)
                self.assertEqual((rc, out["status"], out["reason"]), (1, "refused", reason))
                self.assertNotIn("tmux-pane-keys", self.stdout)

    def test_an_uncertain_send_is_reported_as_uncertain_and_not_sent_again(self):
        for code in (tmux_pane_keys.UNCERTAIN, 127, 2):
            with self.subTest(code):
                self.host.calls.clear()
                self.host.send_code = code
                rc, out = self.paste(CALLBACK)
                self.assertEqual((rc, out["status"], out["reason"]), (1, "uncertain", "uncertain"))
                self.assertEqual(len(self.host.of("bash")), 1, "one attempt, never a retry")
                self.assertEqual(self.host.buffers, {}, "the unpasted buffer is dropped")
                self.assertNotIn(CODE, self.stdout)
        self.assertIsNotNone(cm.store(self.ws).read_marker(self.wait_id), "the wait stays for the waiter")

    def test_an_address_that_is_not_this_waits_callback_is_not_typed(self):
        cases = {
            "another port": CALLBACK.replace(str(PORT), "51235"),
            "another host": CALLBACK.replace("localhost", "evil.example"),
            "a host that only starts with localhost": CALLBACK.replace("localhost", "localhost.evil.example"),
            "userinfo": CALLBACK.replace("localhost", "localhost@evil.example"),
            "https": CALLBACK.replace("http://", "https://"),
            "another path": CALLBACK.replace("/callback", "/callback/x"),
            "another flow's state": CALLBACK.replace(STATE, "other-state"),
            "no state": f"http://localhost:{PORT}/callback?code={CODE}",
            "no code": f"http://localhost:{PORT}/callback?state={STATE}",
            "two lines": CALLBACK + "\nrm -rf x",
            "a space": CALLBACK + " ; ls",
            "a trailing semicolon": CALLBACK + ";",
            "a control character": CALLBACK + "\x1b[A",
            "not an address": "done",
            "empty": "",
            "too long": CALLBACK + "&x=" + "a" * cm.MAX_PASTE_CHARS,
        }
        for name, text in cases.items():
            with self.subTest(name):
                code, out = self.paste(text)
                self.assertEqual((code, out["status"]), (1, "refused"))
                self.assertNotIn(CODE, self.stdout)
        self.assertEqual(self.host.of("bash"), [])
        self.assertEqual(self.host.sends, [])

    def test_an_address_whose_port_cannot_be_read_is_not_an_address(self):
        for text in (f"http://localhost:port/callback?code={CODE}&state={STATE}",
                     f"http://[localhost/callback?code={CODE}&state={STATE}"):
            with self.subTest(text[:24]):
                code, out = self.paste(text)
                self.assertEqual((code, out["reason"]), (1, "not_an_address"))
                self.assertNotIn(CODE, self.stdout)
        self.assertEqual(self.host.of("bash"), [])

    def test_a_paste_for_what_is_not_a_wait_id_is_a_setup_problem(self):
        code, out = self.paste(CALLBACK, wait_id="../x")
        self.assertEqual((code, out["error"]), (2, "invalid_arguments"))
        self.assertEqual(self.host.of("bash"), [])

    def test_a_wait_file_that_is_not_a_wait_takes_no_paste(self):
        cm.store(self.ws).marker_path(self.wait_id).write_text("{}")
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "no_pending_wait"))
        self.assertEqual(self.host.of("bash"), [])

    def test_the_loopback_address_is_the_same_listener(self):
        code, out = self.paste(CALLBACK.replace("localhost", "127.0.0.1"))
        self.assertEqual((code, out["status"]), (0, "sent"))

    def test_an_error_callback_is_passed_on_too(self):
        code, _ = self.paste(f"http://localhost:{PORT}/callback?error=access_denied&state={STATE}")
        self.assertEqual(code, 0)

    def test_only_the_owners_dm_task_may_paste(self):
        for name, fields in {"a room": {"channel_kind": "room"}, "team tier": {"access_tier": "team"},
                             "a collaborator": {"collaborator": "true"}}.items():
            with self.subTest(name):
                self.task("task-bad", **fields)
                code, out = self.paste(CALLBACK, task="task-bad")
                self.assertEqual((code, out["error"]), (2, "not_owner_dm"))
        self.task("task-other-dm", channel_id="!other:example.org")
        code, out = self.paste(CALLBACK, task="task-other-dm")
        self.assertEqual((code, out["reason"]), (1, "no_pending_wait"))
        self.assertEqual(self.host.sends, [])
        self.assertNotIn(CODE, self.everything_written())

    def test_a_wait_that_is_not_pending_takes_no_paste(self):
        code, out = self.paste(CALLBACK, wait_id="1789000000000-0000abcd")
        self.assertEqual((code, out["reason"]), (1, "no_pending_wait"))
        cm.store(self.ws).claim(self.wait_id, "connected", NOW)
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "no_pending_wait"))
        self.assertEqual(self.host.sends, [])

    def test_a_paste_after_the_links_time_is_refused(self):
        self.clock.t += cm.LOGIN_S
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "expired"))
        self.assertEqual(self.host.sends, [])

    def test_a_login_that_is_gone_takes_no_paste(self):
        self.host.panes.clear()
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "login_gone"))
        self.assertEqual(self.host.of("bash"), [])

    def test_the_address_is_never_an_argument(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            self.cli(["paste", self.wait_id, "--task", "task-paste", CALLBACK])
        self.assertEqual(self.host.sends, [])


class SocketPathTests(unittest.TestCase):
    def test_a_socket_sits_in_its_waits_own_folder_under_the_temporary_directory(self):
        with mock.patch.object(cm.tempfile, "gettempdir", return_value="/short/tmp"):
            path = cm.socket_path("1789000000000-0000abcd")
        self.assertEqual(path, "/short/tmp/sutando-mcp-1789000000000-0000abcd/s")
        self.assertTrue(cm.own_socket(path, "1789000000000-0000abcd"))
        self.assertFalse(cm.own_socket(path, "1789000000001-0000abcd"))

    def test_a_temporary_directory_too_long_for_a_socket_is_not_used(self):
        with mock.patch.object(cm.tempfile, "gettempdir", return_value="/" + "x" * 90):
            path = cm.socket_path("1789000000000-0000abcd")
        self.assertLessEqual(len(path), cm.MAX_SOCKET_BYTES)
        self.assertTrue(cm.own_socket(path, "1789000000000-0000abcd"))

    def test_a_wait_naming_another_folder_as_its_socket_is_not_valid(self):
        marker = {"wait_id": "1789000000000-0000abcd", "name": "luma", "room": ROOM, "reply_to": "$e",
                  "task": "task-abc", "owner": OWNER, "session": "mcp-x", "port": PORT, "state_sha256": "ab",
                  "attempt": 1, "deadline": NOW, "socket": "/short/tmp/sutando-mcp-1789000000000-0000abcd/s"}
        self.assertTrue(cm._valid_marker(marker, marker["wait_id"]))
        self.assertFalse(cm._valid_marker({**marker, "socket": "/some/other/tmux.sock"}, marker["wait_id"]))


class CardAndRearmTests(Base):
    def test_connected_is_a_done_card_under_the_sign_in_cards_id(self):
        _, out = self.start()
        self.host.panes.clear()
        self.host.status = CONNECTED
        self.waiter(out["wait_id"])
        code, card = self.cli(["card", out["wait_id"]])
        panel = card["message"]["extra_content"][ac.CARD_KEY]
        self.assertEqual((code, card["outcome"]), (0, "connected"))
        self.assertEqual(panel["id"], out["card_id"])
        self.assertEqual((panel["state"], panel["header"]["status"]["label"]), ("done", "Connected"))
        self.assertIsNone(ac.problem(panel))
        self.assertNotIn("buttons", panel)

    def test_a_pending_or_unknown_wait_has_no_outcome_card(self):
        _, out = self.start()
        for wait_id in (out["wait_id"], "1789000000000-0000abcd"):
            code, card = self.cli(["card", wait_id])
            self.assertEqual((code, card["message"]), (1, None))

    def test_a_wait_that_ended_without_a_word_has_no_card(self):
        _, out = self.start()
        cm.store(self.ws).claim(out["wait_id"], "superseded", NOW)
        code, card = self.cli(["card", out["wait_id"]])
        self.assertEqual((code, card["outcome"], card["message"]), (1, None, None))
        code, bad = self.cli(["card", "../x"])
        self.assertEqual((code, bad["error"]), (2, "invalid_arguments"))

    def test_rearm_starts_only_the_waiters_that_are_gone(self):
        _, first = self.start()
        _, second = self.start(name="other")
        self.spawned.clear()
        held = cm.store(self.ws).acquire_lock(first["wait_id"])
        try:
            code, out = self.cli(["rearm"])
        finally:
            held.close()
        self.assertEqual(code, 0)
        self.assertEqual(out["running"], [first["wait_id"]])
        self.assertEqual([r["wait_id"] for r in out["rearmed"]], [second["wait_id"]])
        self.assertEqual(self.spawned, [second["wait_id"]])

    def test_status_lists_the_rooms_pending_sign_ins(self):
        _, out = self.start()
        code, status = self.cli(["status", "--room", ROOM])
        self.assertEqual(code, 0)
        self.assertEqual([p["wait_id"] for p in status["pending"]], [out["wait_id"]])
        _, elsewhere = self.cli(["status", "--room", "!other:example.org"])
        self.assertEqual(elsewhere["pending"], [])

    def test_the_real_spawn_goes_through_the_shared_store(self):
        with mock.patch.object(cm.wait_store.subprocess, "Popen") as popen:
            popen.return_value.pid = 77
            self.assertEqual(cm.spawn_waiter(self.ws, "1789000000000-0000abcd"), 77)
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[1:], [str(cm.SCRIPT_PATH), "--workspace", str(self.ws), "waiter",
                                    "1789000000000-0000abcd"])
        self.assertTrue(kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
