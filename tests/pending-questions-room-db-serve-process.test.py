#!/usr/bin/env python3
"""`pending_questions_room_db.py serve` run as the store runs it: a subprocess fed its one
request on stdin. Two desktop-install failures are pinned here:

1. The capability's client may re-exec the process onto an interpreter that has its
   dependencies. The request must still be on stdin when that happens, so the capability
   is loaded before stdin is read (else the re-exec reads "" and reports a JSONDecodeError).
2. A desktop install keeps the relay URL and token in the AG2 Space channel env file
   (`channels/ag2space/.env`, or the launcher-named $AG2_DEVICE_ENV), not where the
   capability looks; serve resolves that file through src/channel_env_resolve.py when the
   capability's own order finds nothing, and never mixes it with a variable already set.

No real room is touched: the capability is a fake that records what it was handed.
"""
import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_room_db.py"
sys.path[:0] = [str(REPO / "src"), str(ADAPTER.parent)]
adapter = importlib.import_module("pending_questions_room_db")
ROOM = "!ownerdm:test.invalid"
AGENT = "@agent:test.invalid"
ROWS = {"op": "rows", "schema": {"id": "pendingq", "name": "Pending questions", "props": [], "views": []}}
CRED_VARS = ("AG2_ROOM_COMMONS_URL", "REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")

# The capability: resolves only from a flag or the environment, as the real one does when
# its own relay-client.env is absent.
FAKE_CAP = textwrap.dedent('''
    import os
    URL_VARS = ("AG2_ROOM_COMMONS_URL", "REMOTE_TASK_URL")
    TOKEN_VARS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")

    class RoomDocError(Exception):
        pass

    def _first(names):
        return next((os.environ[v] for v in names if os.environ.get(v)), None)

    def resolve_url(explicit):
        found = explicit or _first(URL_VARS)
        if not found:
            raise RoomDocError("no service URL")
        return found

    def resolve_token(explicit):
        found = explicit or _first(TOKEN_VARS)
        if not found:
            raise RoomDocError("no access token")
        return found
''')

# The client: optionally re-execs once (as room_commons_deps does when this python lacks the
# deps), and records the url and token it was opened with.
FAKE_CLIENT = textwrap.dedent('''
    import json, os, sys
    from contextlib import asynccontextmanager

    if os.environ.get("FAKE_REEXEC") and not os.environ.get("FAKE_REEXECED"):
        sys.stdout.flush()
        os.execve(sys.executable, [sys.executable, *sys.argv],
                  {**os.environ, "FAKE_REEXECED": "1"})

    class Doc:
        database = {}

        def row_body(self, db, row):
            return ""

        async def settle(self, sec):
            return None

    @asynccontextmanager
    async def open_room_collab(url, room, token, kind="doc"):
        with open(os.environ["FAKE_SEEN"], "w") as f:
            json.dump({"url": url, "token": token, "reexeced": bool(os.environ.get("FAKE_REEXECED"))}, f)
        yield Doc()
''')


class ServeProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pq-serve-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.scripts = self.tmp / "skills" / "room-collab" / "scripts"
        self.scripts.mkdir(parents=True)
        (self.scripts / "room_collab.py").write_text(FAKE_CAP)
        (self.scripts / "room_collab_client.py").write_text(FAKE_CLIENT)
        self.seen = self.tmp / "seen.json"
        self.config = self.tmp / "claude-config"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in CRED_VARS and k not in ("AG2_DEVICE_ENV", "CLAUDE_HOME")}
        self.env.update({"FAKE_SEEN": str(self.seen), "CLAUDE_CONFIG_DIR": str(self.config)})

    def env_file(self, path: Path, **values) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        path.chmod(0o600)
        return path

    def serve(self, req=ROWS, **env):
        argv = [sys.executable, str(ADAPTER), "serve", "--room", ROOM, "--user-id", AGENT,
                "--skill-scripts", str(self.scripts)]
        p = subprocess.run(argv, input=json.dumps(req), capture_output=True, text=True, timeout=60,
                           env={**self.env, **env}, cwd=str(self.tmp))
        return json.loads(p.stdout.strip().splitlines()[-1]), p

    def opened(self) -> dict:
        return json.loads(self.seen.read_text())

    def test_a_reexec_by_the_capability_still_reads_the_request(self):
        reply, p = self.serve(FAKE_REEXEC="1", REMOTE_TASK_URL="https://env.test.invalid",
                              REMOTE_TASK_TOKEN="env-token")
        self.assertEqual(reply, {"ok": True, "result": []}, p.stderr)
        self.assertTrue(self.opened()["reexeced"])

    def test_the_channel_env_file_supplies_url_and_token_together(self):
        self.env_file(self.config / "channels" / "ag2space" / ".env",
                      REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        reply, p = self.serve()
        self.assertEqual(reply, {"ok": True, "result": []}, p.stderr)
        self.assertEqual(self.opened()["url"], "https://relay.test.invalid/relay")
        self.assertEqual(self.opened()["token"], "file-token")

    def test_the_launcher_named_device_env_is_resolved_too(self):
        named = self.env_file(self.tmp / "device" / ".env",
                              REMOTE_TASK_URL="https://device.test.invalid/relay", REMOTE_TASK_TOKEN="dev-token")
        reply, p = self.serve(AG2_DEVICE_ENV=str(named))
        self.assertEqual(reply, {"ok": True, "result": []}, p.stderr)
        self.assertEqual((self.opened()["url"], self.opened()["token"]),
                         ("https://device.test.invalid/relay", "dev-token"))

    def test_a_variable_already_set_wins_and_the_file_is_not_mixed_in(self):
        self.env_file(self.config / "channels" / "ag2space" / ".env",
                      REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        reply, _ = self.serve(REMOTE_TASK_URL="https://env.test.invalid", REMOTE_TASK_TOKEN="env-token")
        self.assertTrue(reply["ok"])
        self.assertEqual((self.opened()["url"], self.opened()["token"]), ("https://env.test.invalid", "env-token"))
        self.seen.unlink()
        reply, _ = self.serve(REMOTE_TASK_TOKEN="env-token")  # a token alone: no URL is taken from the file
        self.assertEqual(reply["ok"], False)
        self.assertIn("no service URL", reply["error"])
        self.assertFalse(self.seen.exists())

    def test_nothing_anywhere_is_still_the_capabilitys_own_error(self):
        reply, _ = self.serve()
        self.assertEqual(reply["ok"], False)
        self.assertIn("RoomDocError: no service URL", reply["error"])


class FakeCap:
    """The capability's resolution names, in-process: flag, then env, else RoomDocError."""
    URL_VARS = ("AG2_ROOM_COMMONS_URL", "REMOTE_TASK_URL")
    TOKEN_VARS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")

    class RoomDocError(Exception):
        pass

    def _first(self, explicit, names, what):
        found = explicit or next((os.environ[v] for v in names if os.environ.get(v)), None)
        if not found:
            raise self.RoomDocError(f"no {what}")
        return found

    def resolve_url(self, explicit):
        return self._first(explicit, self.URL_VARS, "service URL")

    def resolve_token(self, explicit):
        return self._first(explicit, self.TOKEN_VARS, "access token")


class InProcess(unittest.TestCase):
    """The same rules driven in-process, where the coverage run measures them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pq-inproc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "claude-config"
        base = {k: v for k, v in os.environ.items()
                if k not in CRED_VARS and k not in ("AG2_DEVICE_ENV", "CLAUDE_HOME")}
        base["CLAUDE_CONFIG_DIR"] = str(self.config)
        patcher = mock.patch.dict(os.environ, base, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def channel_file(self, **values):
        p = self.config / "channels" / "ag2space" / ".env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        p.chmod(0o600)

    def test_channel_credentials_reads_the_resolved_file(self):
        self.channel_file(REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token",
                          OTHER="ignored")
        self.assertEqual(adapter.channel_credentials(FakeCap()),
                         {"REMOTE_TASK_URL": "https://relay.test.invalid/relay", "REMOTE_TASK_TOKEN": "file-token"})

    def test_channel_credentials_is_empty_when_a_variable_is_set_or_nothing_resolves(self):
        self.channel_file(REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        self.assertEqual(adapter.channel_credentials(FakeCap(), {"REMOTE_TASK_TOKEN": "env"}), {})
        self.assertEqual(adapter.channel_credentials(object()), {})  # a capability naming no variables
        (self.config / "channels" / "ag2space" / ".env").unlink()
        self.assertEqual(adapter.channel_credentials(FakeCap()), {})

    def test_credentials_keep_the_capabilitys_own_answer(self):
        self.channel_file(REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        os.environ.update({"REMOTE_TASK_URL": "https://env.test.invalid", "REMOTE_TASK_TOKEN": "env-token"})
        self.assertEqual(adapter._credentials(FakeCap(), None), ("https://env.test.invalid", "env-token"))

    def test_credentials_fall_back_to_the_channel_file_as_one_source(self):
        self.channel_file(REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        self.assertEqual(adapter._credentials(FakeCap(), None), ("https://relay.test.invalid/relay", "file-token"))

    def test_a_collab_url_override_never_receives_the_channel_token(self):
        self.channel_file(REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        with self.assertRaisesRegex(FakeCap.RoomDocError, "no access token"):
            adapter._credentials(FakeCap(), "https://override.test.invalid")
        self.assertNotIn("REMOTE_TASK_TOKEN", os.environ)

    def test_credentials_never_mix_an_env_token_with_the_files_url(self):
        self.channel_file(REMOTE_TASK_URL="https://relay.test.invalid/relay", REMOTE_TASK_TOKEN="file-token")
        os.environ["REMOTE_TASK_TOKEN"] = "env-token"
        with self.assertRaisesRegex(FakeCap.RoomDocError, "no service URL"):
            adapter._credentials(FakeCap(), None)
        self.assertNotIn("REMOTE_TASK_URL", os.environ)

    def test_credentials_reraise_when_no_file_resolves(self):
        with self.assertRaisesRegex(FakeCap.RoomDocError, "no service URL"):
            adapter._credentials(FakeCap(), None)

    def _main(self, load, serve=None):
        order = []

        class Stdin(io.StringIO):
            def read(self, *a):
                order.append("stdin")
                return super().read(*a)

        def loading(scripts):
            order.append("load")
            return load(scripts)

        async def serving(args, req):
            order.append("serve")
            return (serve or (lambda r: []))(req)

        out = io.StringIO()
        argv = ["serve", "--room", ROOM, "--user-id", AGENT, "--skill-scripts", str(self.tmp)]
        with mock.patch.object(adapter, "_load_capability", loading), mock.patch.object(adapter, "_serve", serving), \
                mock.patch.object(sys, "stdin", Stdin(json.dumps(ROWS))), contextlib.redirect_stdout(out):
            rc = adapter.main(argv)
        return rc, json.loads(out.getvalue()), order

    def test_main_loads_the_capability_before_reading_stdin(self):
        rc, reply, order = self._main(lambda s: None)
        self.assertEqual((rc, reply), (0, {"ok": True, "result": []}))
        self.assertEqual(order, ["load", "stdin", "serve"])

    def test_main_reports_a_capability_exit_as_json_without_reading_stdin(self):
        def refuse(scripts):
            raise SystemExit("room-commons client needs its dependencies: no pycrdt")
        rc, reply, order = self._main(refuse)
        self.assertEqual(rc, 1)
        self.assertEqual(reply, {"ok": False, "error": "SystemExit: room-commons client needs its dependencies: no pycrdt"})
        self.assertEqual(order, ["load"])

    def test_load_capability_imports_both_modules_from_the_scripts_dir(self):
        d = self.tmp / "cap"
        d.mkdir()
        (d / "room_collab.py").write_text("MARK = 'cap'\n")
        (d / "room_collab_client.py").write_text("")
        saved = {m: sys.modules.pop(m) for m in ("room_collab", "room_collab_client") if m in sys.modules}
        self.addCleanup(sys.modules.update, saved)  # cleanups run last-first: this one runs last
        self.addCleanup(lambda: str(d) in sys.path and sys.path.remove(str(d)))
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in ("room_collab", "room_collab_client")])
        self.assertEqual(adapter._load_capability(str(d)).MARK, "cap")
        self.assertIn("room_collab_client", sys.modules)


if __name__ == "__main__":
    unittest.main()
