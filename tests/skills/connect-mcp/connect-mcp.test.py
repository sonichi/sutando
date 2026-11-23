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
    """`tmux` on the private server and `claude mcp get`, in memory. Any other call fails the test."""

    def __init__(self, status=NEEDS_AUTH, prints=f"Open this URL to sign in:\n{LINK}\nPaste the redirect URL: "):
        self.calls: list[list] = []
        self.status = status
        self.prints = prints
        self.panes: dict[str, str] = {}
        self.typed: list[tuple[str, str]] = []

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["claude", "mcp", "get"]:
            return (0, self.status) if self.status else (1, f'No MCP server found with name: "{argv[3]}"')
        assert argv[:3] == ["tmux", "-L", cm.TMUX_SERVER], f"not the private tmux server: {argv[:4]}"
        verb, rest = argv[3], argv[4:]
        target = rest[rest.index("-t") + 1].lstrip("=").rstrip(":") if "-t" in rest else ""
        if verb == "new-session":
            self.panes[rest[rest.index("-s") + 1]] = self.prints
            return 0, ""
        if verb == "has-session":
            return (0 if target in self.panes else 1), ""
        if verb == "capture-pane":
            return (0, self.panes[target]) if target in self.panes else (1, "can't find session")
        if verb == "kill-session":
            return (0 if self.panes.pop(target, None) is not None else 1), ""
        if verb == "send-keys":
            if target not in self.panes:
                return 1, ""
            self.typed.append((target, rest[-1]))
            return 0, ""
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
                code, out = self.cli(["start", f"--task=task-abc", "--", name])
                self.assertEqual((code, out["error"]), (2, "invalid_arguments"))
        self.assertEqual(self.host.calls, [])

    def test_a_login_that_prints_no_link_is_ended_and_leaves_no_wait(self):
        self.host.prints = "This server does not support OAuth.\n"
        code, out = self.start()
        self.assertEqual((code, out["error"]), (2, "no_link"))
        self.assertEqual(len(self.host.tmux("kill-session")), 1)
        self.assertEqual(self.host.panes, {})
        self.assertEqual(cm.store(self.ws).list_markers(), [])
        self.assertEqual(self.spawned, [])

    def test_a_link_that_does_not_return_to_this_machine_is_not_a_link(self):
        for redirect in ("https%3A%2F%2Fevil.example%2Fcallback", "http%3A%2F%2Fevil.example%3A51234%2Fcallback",
                         "http%3A%2F%2Flocalhost%3A51234%2Fother"):
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


class WaiterTests(Base):
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
        self.assertEqual(self.host.typed, [(self.session, CALLBACK), (self.session, "Enter")])
        [typed, _enter] = self.host.tmux("send-keys")
        self.assertEqual(typed[-2:], ["-l", CALLBACK])
        self.assertNotIn(CODE, self.stdout)
        self.assertNotIn(CODE, self.everything_written())
        self.assertNotIn(STATE, self.everything_written())

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
        self.assertEqual(self.host.tmux("send-keys"), [])
        self.assertEqual(self.host.typed, [])

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
        self.assertEqual(self.host.typed, [])
        self.assertNotIn(CODE, self.everything_written())

    def test_a_wait_that_is_not_pending_takes_no_paste(self):
        code, out = self.paste(CALLBACK, wait_id="1789000000000-0000abcd")
        self.assertEqual((code, out["reason"]), (1, "no_pending_wait"))
        cm.store(self.ws).claim(self.wait_id, "connected", NOW)
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "no_pending_wait"))
        self.assertEqual(self.host.typed, [])

    def test_a_paste_after_the_links_time_is_refused(self):
        self.clock.t += cm.LOGIN_S
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "expired"))
        self.assertEqual(self.host.typed, [])

    def test_a_login_that_is_gone_takes_no_paste(self):
        self.host.panes.clear()
        code, out = self.paste(CALLBACK)
        self.assertEqual((code, out["reason"]), (1, "login_gone"))
        self.assertEqual(self.host.tmux("send-keys"), [])

    def test_the_address_is_never_an_argument(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            self.cli(["paste", self.wait_id, "--task", "task-paste", CALLBACK])
        self.assertEqual(self.host.typed, [])


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
