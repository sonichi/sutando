#!/usr/bin/env python3
"""Progress notifies carry the same worker stamp as results.

An unstamped notify renders with no attribution at all client-side, which
read as "the stripe disappeared" to the owner — so the stamp on this path is
load-bearing, not cosmetic.

Run: python3 tests/task-progress-notify-stamp.test.py
"""
import contextlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "task-progress" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import notify  # noqa: E402

ROOM = "!r:ag2.space"
# A room v12 id body: unpadded base64url of 32 bytes (43 chars), synthetic.
_V12 = "Synth3tic_v12-RoomId0123456789abcdefABCDEFg"
assert len(_V12) == 43
_GW_ENV = {"REMOTE_TASK_URL": "https://gw.example", "REMOTE_TASK_TOKEN": "tok"}


class NotifyWorkerStampTests(unittest.TestCase):
    def _payload(self, extra_env, **kw):
        sent = []

        def fake_post(url, payload, headers):
            sent.append({"url": url, "payload": payload})
            return True

        env = {**_GW_ENV, **extra_env}
        with mock.patch.object(notify, "_post", fake_post), \
                mock.patch.dict(os.environ, env, clear=False):
            for k in ("SUTANDO_WORKER_ID", "SUTANDO_CORE_ID"):
                if k not in extra_env:
                    os.environ.pop(k, None)
            ok = notify.send_remote_gateway("local-ag2space", ROOM, "on it", **kw)
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        return sent[0]["payload"]

    def test_worker_id_env_stamps_the_message(self):
        p = self._payload({"SUTANDO_WORKER_ID": "core-7"})
        self.assertEqual(p["extra_content"], {"space.ag2.worker": {"id": "core-7"}})

    def test_core_id_env_derives_the_stamp(self):
        p = self._payload({"SUTANDO_CORE_ID": "3", "SUTANDO_WORKER_ID": ""})
        self.assertEqual(p["extra_content"], {"space.ag2.worker": {"id": "worker-3"}})

    def test_no_worker_env_sends_no_stamp(self):
        p = self._payload({})
        self.assertNotIn("extra_content", p)
        self.assertEqual(p["body"], "on it")


class NotifyThreadRootTests(unittest.TestCase):
    def _send(self, argv):
        sent = []

        def fake_post(url, payload, headers):
            sent.append(payload)
            return True

        with mock.patch.object(notify, "_post", fake_post), \
                mock.patch.dict(os.environ, _GW_ENV, clear=False), \
                mock.patch.object(sys, "argv", ["notify.py", "--source", "local-ag2space",
                                                "--channel-id", ROOM, "--message", "on it", *argv]):
            for k in ("SUTANDO_WORKER_ID", "SUTANDO_CORE_ID", "SUTANDO_WORKER_SEAT"):
                os.environ.pop(k, None)
            rc = notify.main()
        return rc, sent

    def test_thread_root_flag_threads_the_message(self):
        rc, sent = self._send(["--thread-root", "$root123"])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it",
                                 "thread_root": "$root123"}])

    def test_no_flag_payload_is_unchanged(self):
        rc, sent = self._send([])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_empty_thread_root_posts_unthreaded(self):
        rc, sent = self._send(["--thread-root", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_malformed_thread_root_is_refused_without_posting(self):
        for bad in ("root123", "   ", "$"):
            rc, sent = self._send(["--thread-root", bad])
            self.assertEqual(rc, 1, repr(bad))
            self.assertEqual(sent, [], repr(bad))


class EventIdParityTests(unittest.TestCase):
    """notify.py keeps its own copy of the event-id check (agent-room-ops is optional),
    so both copies must give the same answer for every non-empty input."""

    CASES = ("$ok", " $ok ", "$", "root", "   ", "e$vt", "\t$x\n")

    def test_notify_and_relations_agree(self):
        sys.path.insert(0, str(_SCRIPTS.parents[1] / "agent-room-ops"))
        import relations  # noqa: E402
        for value in self.CASES:
            try:
                expected = relations._event_id(value, "thread_root")
            except relations.RelationError:
                expected = None
            sent, err = [], io.StringIO()
            with mock.patch.object(notify, "_post", lambda url, payload, headers: sent.append(payload) or True), \
                    mock.patch.dict(os.environ, _GW_ENV, clear=False), \
                    contextlib.redirect_stderr(err):
                ok = notify.send_remote_gateway("local-ag2space", ROOM, "x", thread_root=value)
            if expected is None:
                self.assertFalse(ok, repr(value))
                self.assertEqual(sent, [], repr(value))
                self.assertIn(repr(value), err.getvalue(), repr(value))
            else:
                self.assertTrue(ok, repr(value))
                self.assertEqual(sent[0]["thread_root"], expected, repr(value))

class NotifyTaskFileDeriveTests(unittest.TestCase):
    """--task-file derives source/channel/thread from the task file's own headers;
    an explicit flag, even an empty one, wins over what the file carries."""

    def _task(self, body):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(body)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _send(self, argv, env=None):
        sent = []
        # No real channels/<source>/.env may decide routability.
        cfg = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, cfg)

        def fake_post(url, payload, headers):
            sent.append(payload)
            return True

        with mock.patch.object(notify, "_post", fake_post), \
                mock.patch.object(notify, "_token", lambda source, var: "tok"), \
                mock.patch.dict(os.environ, {**_GW_ENV, "CLAUDE_CONFIG_DIR": cfg, **(env or {})},
                                clear=False), \
                mock.patch.object(sys, "argv", ["notify.py", "--message", "on it", *argv]):
            for k in ("SUTANDO_WORKER_ID", "SUTANDO_CORE_ID", "SUTANDO_WORKER_SEAT"):
                os.environ.pop(k, None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = notify.main()
        return rc, sent, err.getvalue()

    def test_task_mid_file_derives_source_channel_and_thread_root(self):
        # Headers both before AND after `task:` (ag2space DM envelope shape).
        f = self._task("id: task-x\nsource: local-ag2space\nchannel_id: !r:ag2.space\n"
                       "task: some owner message\nthread_root: $root123\n")
        rc, sent, _ = self._send(["--task-file", f])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it",
                                 "thread_root": "$root123"}])

    def test_slack_task_derives_reply_thread_ts_into_thread_ts(self):
        # The Slack bridge's header is reply_thread_ts, not thread_ts.
        f = self._task("id: task-s\nsource: slack\nchannel_id: C0SLACK\n"
                       "reply_thread_ts: 1700.0001\ntask: hi\n")
        rc, sent, _ = self._send(["--task-file", f])
        self.assertEqual(rc, 0)
        self.assertEqual(sent[0]["channel"], "C0SLACK")
        self.assertEqual(sent[0]["thread_ts"], "1700.0001")

    def test_explicit_flag_overrides_what_the_task_file_carries(self):
        f = self._task("source: local-ag2space\nchannel_id: !r:ag2.space\n"
                       "task: x\nthread_root: $fromfile\n")
        rc, sent, _ = self._send(["--task-file", f, "--thread-root", "$explicit"])
        self.assertEqual(rc, 0)
        self.assertEqual(sent[0]["thread_root"], "$explicit")

    def test_empty_explicit_thread_root_opts_out_of_the_file_thread(self):
        f = self._task("source: local-ag2space\nchannel_id: !r:ag2.space\n"
                       "task: x\nthread_root: $fromfile\n")
        rc, sent, _ = self._send(["--task-file", f, "--thread-root", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_no_thread_root_threads_under_source_message_id_not_reply_to_event(self):
        # reply_to_event is the post the sender quoted (often someone else's);
        # the asking message is source_message_id.
        f = self._task("source: local-ag2space\nchannel_id: !r:ag2.space\n"
                       "source_message_id: $ask\nreply_to_event: $other\ntask: x\n")
        rc, sent, _ = self._send(["--task-file", f])
        self.assertEqual(rc, 0)
        self.assertEqual(sent[0]["thread_root"], "$ask")

    def test_thread_root_wins_over_source_message_id(self):
        f = self._task("source: local-ag2space\nchannel_id: !r:ag2.space\n"
                       "source_message_id: $ask\nreply_to_event: $other\ntask: x\n"
                       "thread_root: $root\n")
        rc, sent, _ = self._send(["--task-file", f])
        self.assertEqual(rc, 0)
        self.assertEqual(sent[0]["thread_root"], "$root")

    def test_missing_task_file_falls_back_to_explicit_flags_without_crashing(self):
        missing = os.path.join(tempfile.gettempdir(), "notify-does-not-exist-xyz.txt")
        rc, sent, err = self._send(["--task-file", missing,
                                    "--source", "local-ag2space", "--channel-id", ROOM])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])
        self.assertIn("unreadable", err)

    def test_undecodable_task_file_fails_open_to_explicit_flags(self):
        f = self._task("")
        with open(f, "wb") as fh:
            fh.write(b"source: local-ag2space\n\xff\xfe task: x\n")
        rc, sent, err = self._send(["--task-file", f,
                                    "--source", "local-ag2space", "--channel-id", ROOM])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])
        self.assertIn("unreadable", err)

    def test_local_voice_task_sends_nothing_to_the_gateway(self):
        # The shape src/task-bridge.ts writes for an undocked voice task.
        f = self._task("id: task-1\nsource: voice\ninteraction_type: realtime_audio\n"
                       "media_form: live_stream\nchannel_id: local-voice\ntask: private ask\n")
        rc, sent, err = self._send(["--task-file", f])
        self.assertEqual(rc, notify.NO_ROUTE_EXIT)
        self.assertEqual(sent, [])
        self.assertIn("no delivery path", err)

    def test_docked_voice_task_with_a_real_room_still_posts(self):
        f = self._task("id: task-2\nsource: voice\ninteraction_type: realtime_audio\n"
                       f"channel_id: {ROOM}\ntask: room ask\n")
        rc, sent, _ = self._send(["--task-file", f])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_unknown_source_without_a_room_sends_nothing(self):
        f = self._task("id: task-u\nsource: some-local-producer\nchannel_id: local-x\ntask: x\n")
        rc, sent, err = self._send(["--task-file", f])
        self.assertEqual(rc, notify.NO_ROUTE_EXIT)
        self.assertEqual(sent, [])
        self.assertIn("no delivery path", err)

    def test_chat_task_has_no_bridge_and_sends_nothing(self):
        f = self._task("id: task-chat-1\nsource: chat\nchannel_id: local-chat\n"
                       "access_tier: owner\ntask: do a thing\n")
        rc, sent, err = self._send(["--task-file", f])
        self.assertEqual(rc, notify.NO_ROUTE_EXIT)
        self.assertEqual(sent, [])
        self.assertIn("no delivery path", err)

    def test_parser_unavailable_falls_back_to_explicit_flags(self):
        # sys.modules[name] = None makes `from local_task_protocol import ...` raise ImportError.
        f = self._task("source: local-ag2space\nchannel_id: !r:ag2.space\ntask: x\n")
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"local_task_protocol": None}), \
                contextlib.redirect_stderr(err):
            derived = notify._derive_from_task_file(f)
        self.assertEqual(derived, {})
        self.assertIn("local_task_protocol unavailable", err.getvalue())

    def test_task_file_with_source_but_no_channel_refuses_cleanly(self):
        f = self._task("id: task-v\nsource: slack\ntask: x\n")
        rc, sent, err = self._send(["--task-file", f])
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])
        self.assertIn("--channel-id (or --chat-id) is required", err)

    def test_neither_task_file_nor_source_refuses_cleanly(self):
        rc, sent, err = self._send([])
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])
        self.assertIn("--source is required", err)


class SharedRouteDelegationTests(unittest.TestCase):
    """notify.py applies src/progress_route.py's verdict; a private copy fails here."""

    def test_binds_the_shared_route_by_identity(self):
        import progress_route
        self.assertIs(notify._delivery_route, progress_route.delivery_route)
        self.assertIs(notify._no_route_message, progress_route.no_route_message)
        self.assertEqual(notify.NO_ROUTE_EXIT, progress_route.NO_ROUTE_EXIT)

    def test_unimportable_route_fails_closed(self):
        with mock.patch.dict(sys.modules, {"progress_route": None}):
            route, _, code = notify._load_progress_route()
        self.assertIsNone(route("slack", "C1"))
        self.assertNotEqual(code, 0)


class NotifyDeliveryRouteTests(unittest.TestCase):
    """A built-in source sends; any other source, known or not, sends iff its channel
    is a valid room id. Everything else sends nothing in either config mode."""

    LOCAL = {
        "local-voice": "id: task-1\nsource: voice\ninteraction_type: realtime_audio\n"
                       "channel_id: local-voice\ntask: private ask\n",
        "onboarding-wizard": "id: task-claude-import-1\nsource: chat\ninteraction_type: message\n"
                             "channel_id: onboarding-wizard\nuser_id: onboarding-wizard\n"
                             "access_tier: owner\npriority: low\ntask: Run the import\n",
        "runtime-api": "id: task-rtapi-1\ntimestamp: 2026-10-01T00:00:00Z\ntask: private ask\n"
                       "source: runtime-api\nchannel_id: runtime-api\nuser_id: u\n"
                       "access_tier: owner\npriority: normal\n",
        "some-new-writer": "id: task-n\nsource: some-new-writer\nchannel_id: some-new-writer\n"
                           "task: private ask\n",
    }
    BRIDGES = {
        "docked-room": ("gateway", f"id: task-2\nsource: voice\nchannel_id: {ROOM}\ntask: room ask\n"),
        "ag2space": ("gateway", f"id: task-a\nsource: ag2space\nchannel_id: {ROOM}\ntask: x\n"),
        "local-ag2space": ("gateway", f"id: task-l\nsource: local-ag2space\nchannel_id: {ROOM}\ntask: x\n"),
        "slack": ("slack", "id: task-s\nsource: slack\nchannel_id: C0SLACK\ntask: x\n"),
        "discord": ("discord", "id: task-d\nsource: discord\nchannel_id: 1234567890\ntask: x\n"),
        "telegram": ("telegram", "id: task-t\nsource: telegram\nchat_id: 42\ntask: x\n"),
    }
    SOURCES = ("voice", "chat", "runtime-api", "some-new-writer", "ag2space", "local-ag2space")

    def _run(self, body, mode):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg)
        task = os.path.join(cfg, "task.txt")
        with open(task, "w") as fh:
            fh.write(body)
        env = {"CLAUDE_CONFIG_DIR": cfg}
        if mode == "per-source-env":
            for name in self.SOURCES:
                os.makedirs(os.path.join(cfg, "channels", name))
                with open(os.path.join(cfg, "channels", name, ".env"), "w") as fh:
                    fh.write("REMOTE_TASK_URL=https://gw.example\nREMOTE_TASK_TOKEN=tok\n")
        else:
            env.update(_GW_ENV)
        calls = []

        def rec(kind):
            return lambda *a, **k: calls.append(kind) or True

        with mock.patch.object(notify, "_post", rec("http")), \
                mock.patch.object(notify, "send_slack", rec("slack")), \
                mock.patch.object(notify, "send_discord", rec("discord")), \
                mock.patch.object(notify, "send_telegram", rec("telegram")), \
                mock.patch.object(notify, "send_remote_gateway", rec("gateway")), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", ["notify.py", "--message", "on it",
                                                "--task-file", task]):
            for k in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
                if mode == "per-source-env":
                    os.environ.pop(k, None)
            with contextlib.redirect_stderr(io.StringIO()):
                rc = notify.main()
        return rc, calls

    def test_local_and_unknown_writers_send_nothing_in_either_config_mode(self):
        for mode in ("per-source-env", "global-gateway"):
            for name, body in self.LOCAL.items():
                with self.subTest(mode=mode, shape=name):
                    rc, calls = self._run(body, mode)
                    self.assertEqual(rc, notify.NO_ROUTE_EXIT)
                    self.assertEqual(calls, [])

    def test_runtime_api_task_sends_nothing(self):
        for mode in ("per-source-env", "global-gateway"):
            rc, calls = self._run(self.LOCAL["runtime-api"], mode)
            self.assertEqual((rc, calls), (notify.NO_ROUTE_EXIT, []), mode)

    def test_unknown_source_with_a_room_id_sends(self):
        # Accepted trade-off: routing keys on the room id, not the source label.
        body = f"id: task-n\nsource: some-new-writer\nchannel_id: {ROOM}\ntask: x\n"
        for mode in ("per-source-env", "global-gateway"):
            self.assertEqual(self._run(body, mode), (0, ["gateway"]), mode)

    def test_unknown_source_with_a_non_room_channel_sends_nothing(self):
        for mode in ("per-source-env", "global-gateway"):
            rc, calls = self._run(self.LOCAL["some-new-writer"], mode)
            self.assertEqual((rc, calls), (notify.NO_ROUTE_EXIT, []), mode)

    def test_malformed_room_ids_send_nothing(self):
        # A gateway room id is strictly `!opaque:server`; anything looser is local.
        for channel in ("!foo", "!", "!room", "!room:", "!a b", "!room:server extra", "!:server",
                        "x!room:server", "!" + _V12[:-1], "!" + _V12 + "x", "!" + _V12[:-1] + " ",
                        "!" + _V12[:20] + "+" + _V12[21:]):
            for mode in ("per-source-env", "global-gateway"):
                with self.subTest(channel=channel, mode=mode):
                    body = f"id: task-m\nsource: ag2space\nchannel_id: {channel}\ntask: x\n"
                    rc, calls = self._run(body, mode)
                    self.assertEqual((rc, calls), (notify.NO_ROUTE_EXIT, []))
            with self.subTest(channel=channel, verdict=True):
                self.assertIsNone(notify._delivery_route("ag2space", channel))

    def test_a_trailing_newline_is_not_a_room_id(self):
        for channel in ("!r:ag2.space\n", "!" + _V12 + "\n"):
            with self.subTest(channel=channel):
                self.assertIsNone(notify._delivery_route("ag2space", channel))

    def test_room_v12_ids_without_a_server_send(self):
        # Synthetic: same length and charset as a v12 room id, not a real one.
        for channel in ("!" + _V12, "!x:ag2.space"):
            for mode in ("per-source-env", "global-gateway"):
                with self.subTest(channel=channel, mode=mode):
                    body = f"id: task-v\nsource: ag2space\nchannel_id: {channel}\ntask: x\n"
                    self.assertEqual(self._run(body, mode), (0, ["gateway"]))

    def test_each_bridge_task_sends_exactly_once(self):
        for mode in ("per-source-env", "global-gateway"):
            for name, (sender, body) in self.BRIDGES.items():
                with self.subTest(mode=mode, shape=name):
                    rc, calls = self._run(body, mode)
                    self.assertEqual(rc, 0)
                    self.assertEqual(calls, [sender])


if __name__ == "__main__":
    unittest.main(verbosity=1)
