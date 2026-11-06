#!/usr/bin/env python3
"""`install --agent <id> --token-stdin` on claude-local and codex-local, under systemd and launchd: the relay
token pasted on stdin (asked for without echo on a terminal) in either accepted form, written to the agent's
seat by the seat's own writer (0600, through a temp file and a rename), then installed as --token-env-file
<that seat>. A malformed token, a seat file that is a link or a FIFO, and the other adapter already serving the
agent are refused with nothing written, the last before stdin is read. The token never reaches output, a
unit, a config, or a shell trace. Driven in-process against a fake manager; no real manager, claude, codex
or relay."""
from __future__ import annotations

import contextlib
import http.server
import importlib
import threading
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402
from _helpers.subprocess_coverage import covered_code  # noqa: E402

isolate_os_environ()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_code_fixtures as fx  # noqa: E402
import test_bridge_exclusive as bx  # noqa: E402

cb, cm, sm = fx.cb, fx.cm, fx.sm
claude_cli, codex_cli = fx.cli, importlib.import_module("ag2_sparrow.adapters.codex.connect_cli")
CL, CX = bx.CL, bx.CX
CLIS = {CL.name: claude_cli, CX.name: codex_cli}
BEARER = "pasted-bearer-QQQQ7777"
TOKEN = f"{fx.RELAY}|{BEARER}"
FORMS = {"bare": TOKEN, "remote_task_token": f"REMOTE_TASK_TOKEN={TOKEN}",
         "ag2_remote_token_single_quoted": f"AG2_REMOTE_TOKEN='{TOKEN}'",
         "export_double_quoted": f'export REMOTE_TASK_TOKEN="{TOKEN}"'}
MALFORMED = {"empty": "", "bare secret": BEARER, "no secret": f"{fx.RELAY}|", "no separator": fx.RELAY,
             "plain http": f"http://relay.backend.example/relay|{BEARER}", "other key": f"OTHER_TOKEN={TOKEN}",
             "space in secret": f"{fx.RELAY}|{BEARER} extra", "url with query": f"{fx.RELAY}?x=1|{BEARER}"}


class Relay:
    """The relay's GET /v1/whoami: `agent` for the pasted bearer, else 401; `status` or `down` override it."""

    def __init__(self, agent=fx.AGENT, status=200, down=False):
        self.agent, self.status, self.down, self.calls = agent, status, down, []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, req.get_method(), req.get_header("Authorization"), timeout))
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        status = self.status if req.get_header("Authorization") == f"Bearer {BEARER}" else 401
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(b'{"error":"unauthorized"}'))
        return fx._Resp(200, json.dumps({"agent_mxid": self.agent} if self.agent else {}).encode())


def relay_opener(relay):
    """The fake relay as the opener a request with a bearer builds: urllib's own primitive, stood in for."""
    return mock.patch.object(urllib.request, "build_opener", return_value=mock.Mock(open=mock.Mock(side_effect=relay)))


class Tty(io.StringIO):
    readline = None     # a terminal's line is read through getpass, never echoed by a plain read

    def isatty(self):
        return True


class TokenStdin(fx.AgentRootBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(self.home / "host-state")
        self.relay.unlink()     # a fresh host: no device env, nothing but the pasted token
        self.codex = self.bin / "codex"
        self.codex.write_text(bx.FAKE_CODEX)
        self.codex.chmod(0o755)
        self.codex_home = self.home / ".codex-agent"
        self.codex_home.mkdir()
        self.mac = False
        mac = mock.patch.object(cm, "is_macos", side_effect=lambda: self.mac)
        mac.start()
        self.addCleanup(mac.stop)

    def seat(self):
        return cb.bridge_home(self.root) / "channels" / "ag2space"

    def argv(self, name, *extra, agent=fx.AGENT):
        named = ["--agent", agent] if agent else []
        if name == CL.name:
            return ["install", *named, "--token-stdin", "--plugin-dir", str(self.plugin), "--claude-config-dir",
                    str(self.home / ".claude-sutando"), "--hmac-key-file", str(self.home / "task-hmac.key"), *extra]
        return ["install", *named, "--token-stdin", "--codex", str(self.codex), "--codex-home", str(self.codex_home),
                *extra]

    def run_cli(self, name, argv, jobs, mac, stdin, relay=None):
        out, err = io.StringIO(), io.StringIO()
        self.mac = mac
        self.relay_fake = Relay() if relay is None else relay
        relayed = relay_opener(self.relay_fake) if self.relay_fake \
            else contextlib.nullcontext()     # False: the real no-redirect opener, against a real server
        with mock.patch.object(sm, "_run", side_effect=jobs), mock.patch.object(sys, "stdin", stdin), relayed, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = CLIS[name].main(argv)
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def paste(self, name, line, mac, jobs=None, *extra, agent=fx.AGENT, relay=None):
        stdin = io.StringIO(line + "\n")
        jobs = jobs or bx.Jobs(self.root, self.runners)
        return (*self.run_cli(name, self.argv(name, *extra, agent=agent), jobs, mac, stdin, relay), stdin, jobs)

    def snapshot(self):
        files = {}
        for d in (cb.bridge_home(self.root), self.root):
            files.update({f"{d}:{k}": v for k, v in fx.tree_bytes(d).items() if not k.endswith(cm.SEAT_LOCK)})
        for shape in (CL, CX):
            for path in (cm.config_path(shape), cm.supervisor_path(shape), cm.supervisor_path(cb.bridge_shape(shape))):
                files[str(path)] = path.read_bytes() if path.exists() else None
        return files

    def assert_installed(self, name, rc, out, err, jobs, mac):
        self.assertEqual(rc, 0, out + err)
        seat = self.seat()
        self.assertEqual(sorted(p.name for p in seat.iterdir() if p.name != cm.SEAT_LOCK), [".env", "token"])
        for f in (".env", "token"):
            self.assertEqual(stat.S_IMODE((seat / f).stat().st_mode), 0o600, f)
        self.assertEqual(stat.S_IMODE(seat.stat().st_mode), 0o700)
        self.assertEqual((seat / "token").read_text(), f"{fx.RELAY}|{BEARER}\n")
        values = dict(ln.split("=", 1) for ln in (seat / ".env").read_text().splitlines())
        self.assertEqual((values["AGENT_MXID"], values["REMOTE_TASK_TOKEN_FILE"]), (fx.AGENT, str(seat / "token")))
        cfg = cm.read_config(cm.config_path({CL.name: CL, CX.name: CX}[name]))
        self.assertEqual((cfg["ROOT"], cfg["TOKEN_ENV_FILE"], cfg["AGENT_ID"]), (str(self.root), str(seat / ".env"), fx.AGENT))
        self.assertTrue(jobs.bridges[name].active, "the agent's bridge runs from the seat")
        unit = cm.supervisor_path(bx.BRIDGES[name])
        self.assertEqual(unit.suffix, ".plist" if mac else ".service")
        self.assertIn(f"seat written: {fx.AGENT}", out)
        for text in (out, err, unit.read_text(), cm.config_path({CL.name: CL, CX.name: CX}[name]).read_text(),
                     (seat / ".env").read_text()):
            self.assertNotIn(BEARER, text)

    def each(self):
        for name in (CL.name, CX.name):
            for mac in (False, True):
                yield name, mac

    def fresh(self, name, mac):
        """A clean host for the next adapter and manager: no root, seat, config or unit of either adapter."""
        for r in self.runners:
            r.reap()
        self.runners.clear()
        shutil.rmtree(self.agents, ignore_errors=True)
        shutil.rmtree(cm.config_root(), ignore_errors=True)
        for m in (False, True):
            self.mac = m
            for shape in (CL, CX):
                for path in (cm.supervisor_path(shape), cm.supervisor_path(cb.bridge_shape(shape))):
                    path.unlink(missing_ok=True)
        self.mac = mac

    def test_piped_stdin_in_each_accepted_form_is_written_to_the_seat_and_installed(self):
        for name, mac in self.each():
            for form, line in FORMS.items():
                with self.subTest(name=name, mac=mac, form=form):
                    self.fresh(name, mac)
                    rc, out, err, stdin, jobs = self.paste(name, line, mac)
                    self.assert_installed(name, rc, out, err, jobs, mac)
                    self.assertEqual(stdin.tell(), len(line) + 1, "one line, read whole")

    def test_a_terminal_is_asked_without_echo(self):
        for name, mac in self.each():
            with self.subTest(name=name, mac=mac):
                self.fresh(name, mac)
                jobs = bx.Jobs(self.root, self.runners)
                with mock.patch.object(cm.getpass, "getpass", return_value=FORMS["ag2_remote_token_single_quoted"]) as gp:
                    rc, out, err = self.run_cli(name, self.argv(name), jobs, mac, Tty())
                gp.assert_called_once_with(cm.TOKEN_PROMPT)
                self.assert_installed(name, rc, out, err, jobs, mac)

    def test_a_malformed_token_is_refused_with_nothing_written(self):
        for name, mac in self.each():
            self.fresh(name, mac)
            for why, line in MALFORMED.items():
                with self.subTest(name=name, mac=mac, why=why):
                    before = self.snapshot()
                    rc, out, err, _stdin, jobs = self.paste(name, line, mac)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn("nothing was written", err)
                    self.assertEqual(self.snapshot(), before, "no seat, config or unit")
                    self.assertFalse(any(b.started() for b in jobs.bridges.values()))
                    self.assertNotIn("seat written", out)
                    if line:
                        self.assertNotIn(BEARER, out + err)

    def test_a_link_or_fifo_seat_file_is_refused_and_never_followed(self):
        target = Path(tempfile.mkdtemp(dir=self.home)) / "elsewhere"
        for name, mac in self.each():
            for kind, fname in (("symlink", "token"), ("symlink", ".env"), ("fifo", "token"), ("fifo", ".env")):
                with self.subTest(name=name, mac=mac, kind=kind, file=fname):
                    self.fresh(name, mac)
                    self.seat().mkdir(parents=True)
                    if kind == "symlink":
                        target.write_text("untouched\n")
                        (self.seat() / fname).symlink_to(target)
                    else:
                        os.mkfifo(self.seat() / fname)
                    before = self.snapshot()
                    rc, out, err, stdin, jobs = self.paste(name, TOKEN, mac)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn("cannot be written", err)
                    self.assertEqual(stdin.tell(), 0, "refused before the token is read")
                    if kind == "symlink":
                        self.assertTrue((self.seat() / fname).is_symlink())
                        self.assertEqual(target.read_text(), "untouched\n", "the link is never followed")
                    else:
                        self.assertTrue(stat.S_ISFIFO(os.lstat(self.seat() / fname).st_mode))
                    self.assertEqual(self.snapshot(), before)
                    self.assertFalse(cm.config_path({CL.name: CL, CX.name: CX}[name]).exists())

    def test_an_existing_seat_is_replaced_through_a_rename_never_rewritten_in_place(self):
        for name, mac in self.each():
            with self.subTest(name=name, mac=mac):
                self.fresh(name, mac)
                fx.seed_seat(cb.bridge_home(self.root), "old-bearer-RRRR8888")
                inodes = {f: os.lstat(self.seat() / f).st_ino for f in ("token", ".env")}
                held = {f: open(self.seat() / f, "rb") for f in ("token", ".env")}
                try:
                    replaced = []
                    real = os.replace
                    with mock.patch.object(os, "replace", side_effect=lambda *a, **kw: replaced.append(a[1]) or real(*a, **kw)):
                        rc, out, err, _stdin, jobs = self.paste(name, TOKEN, mac)
                    self.assert_installed(name, rc, out, err, jobs, mac)
                    self.assertTrue({"token", ".env"} <= set(replaced), replaced)
                    for f in ("token", ".env"):
                        self.assertNotEqual(os.lstat(self.seat() / f).st_ino, inodes[f], f"{f} was renamed over")
                    self.assertIn(b"old-bearer-RRRR8888", held["token"].read(), "a reader of the old file saw no torn write")
                    self.assertEqual([p for p in self.seat().iterdir() if p.name.startswith(".token.") or
                                      p.name.startswith("..env.")], [], "no temp file is left")
                finally:
                    for fh in held.values():
                        fh.close()

    def test_the_other_runtime_serving_the_agent_is_refused_before_stdin_is_read(self):
        for first, second in ((CL.name, CX.name), (CX.name, CL.name)):
            for mac in (False, True):
                with self.subTest(first=first, second=second, mac=mac):
                    self.fresh(first, mac)
                    jobs = bx.Jobs(self.root, self.runners)
                    rc, out, err, _stdin, _ = self.paste(first, TOKEN, mac, jobs)
                    self.assertEqual(rc, 0, out + err)
                    jobs.launchers[first].stop_session()
                    before = self.snapshot()
                    rc, out, err, stdin, _ = self.paste(second, "x" + TOKEN, mac, jobs)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertEqual(stdin.tell(), 0, "stdin is never read")
                    self.assertIn(f"already has a bridge, run by {first}", err)
                    self.assertIn(f"run {bx.COMMANDS[first]} uninstall --agent {fx.AGENT} first", err)
                    self.assertIn("the token was not read", err)
                    self.assertFalse(jobs.bridges[second].started())
                    self.assertTrue(jobs.bridges[first].active, "the first bridge is left running")
                    self.assertEqual(self.snapshot(), before)

    def test_a_running_job_is_refused_before_stdin_is_read(self):
        for name, mac in self.each():
            with self.subTest(name=name, mac=mac):
                self.fresh(name, mac)
                jobs = bx.Jobs(self.root, self.runners)
                self.assertEqual(self.paste(name, TOKEN, mac, jobs)[0], 0)
                rc, out, err, stdin, _ = self.paste(name, TOKEN, mac, jobs)
                self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                self.assertEqual(stdin.tell(), 0)
                self.assertIn("stop it first", err)
                self.assertIn("then install --token-stdin again; the token was not read", err)

    def test_options_that_name_another_credential_or_root_are_refused_before_stdin(self):
        for name, mac in self.each():
            cases = [("--token-env-file", str(self.home / "x.env")), ("--work", str(self.home / "w"))]
            if name == CL.name:
                cases += [("--inbox", str(self.home / "w" / "tasks")), ("--bridge-state-dir", str(self.home / "s"))]
            for flag, value in cases:
                with self.subTest(name=name, mac=mac, flag=flag):
                    self.fresh(name, mac)
                    rc, out, err, stdin, _ = self.paste(name, TOKEN, mac, None, flag, value)
                    self.assertNotEqual(rc, 0, out + err)
                    self.assertEqual(stdin.tell(), 0)
                    self.assertIn("not allowed with argument --token-stdin" if flag == "--token-env-file"
                                  else f"cannot be combined with {flag}", err)
                    self.assertFalse(self.seat().exists())

    def test_no_agent_or_a_malformed_one_is_refused_before_stdin(self):
        for name, mac in self.each():
            for agent, why in (("", "--token-stdin needs --agent"), ("not-an-id", "is not a Matrix ID")):
                with self.subTest(name=name, mac=mac, agent=agent):
                    self.fresh(name, mac)
                    rc, out, err, stdin, _ = self.paste(name, TOKEN, mac, agent=agent)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(why, err)
                    self.assertEqual(stdin.tell(), 0)
                    self.assertFalse(self.seat().exists())

    def test_a_failed_install_after_the_seat_names_the_rerun_without_a_second_paste(self):
        for name, mac in self.each():
            with self.subTest(name=name, mac=mac):
                self.fresh(name, mac)
                adapter = CLIS[name]
                with mock.patch.object(adapter.cm, "install", return_value=cm.EXIT_UNUSABLE):
                    rc, out, err, _stdin, _ = self.paste(name, TOKEN, mac)
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn(f"the token is in the seat {self.seat() / '.env'}; once the line above is fixed, run "
                              f"{adapter.COMMAND} install", err)
                self.assertIn(f"--token-env-file {self.seat() / '.env'} (no need to paste it again)", err)
                self.assertNotIn("--token-stdin", err.split("run ", 1)[-1])
                self.assertNotIn(BEARER, out + err)


    def test_the_relay_must_name_exactly_this_agent_for_the_token(self):
        other = "@other.agent:ag2.space"
        cases = (("mismatch", dict(agent=other), f"the token is for {other}, not {fx.AGENT}: paste {fx.AGENT}'s own "
                  f"token, or run the command with --agent {other}"),
                 ("401", dict(status=401), "the relay rejected the token (HTTP 401)"),
                 ("403", dict(status=403), "the relay rejected the token (HTTP 403)"),
                 ("500", dict(status=500), "the relay did not confirm the token (HTTP 500)"),
                 ("no agent", dict(agent=""), "the relay's answer names no agent for the token"),
                 ("unreachable", dict(down=True), "the relay could not be reached to check the token"))
        for name, mac in self.each():
            for why, kw, said in cases:
                with self.subTest(name=name, mac=mac, why=why):
                    relay = Relay(**kw)
                    self.fresh(name, mac)
                    before = self.snapshot()
                    rc, out, err, _stdin, jobs = self.paste(name, TOKEN, mac, relay=relay)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(said, err)
                    self.assertIn("nothing was written", err)
                    self.assertEqual(self.snapshot(), before, "no seat, config or unit")
                    self.assertFalse(self.seat().exists())
                    self.assertFalse(any(b.started() for b in jobs.bridges.values()))
                    self.assertNotIn(BEARER, out + err)
                    self.assertEqual(len(relay.calls), 1)

    def test_a_relay_reflecting_the_bearer_never_gets_it_printed(self):
        reflected = (f"@{BEARER}:example.test", f"@{BEARER.lower()}:example.test", f"@x:{BEARER.lower()}.example")
        for name, mac in self.each():
            for agent in reflected:
                with self.subTest(name=name, mac=mac, agent=agent):
                    self.fresh(name, mac)
                    before = self.snapshot()
                    rc, out, err, _stdin, _jobs = self.paste(name, TOKEN, mac, relay=Relay(agent=agent))
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"the relay named a different agent than {fx.AGENT} for the token", err)
                    self.assertNotIn(BEARER.lower(), (out + err).lower())
                    self.assertNotIn("QQQQ7777".lower(), (out + err).lower())
                    self.assertEqual(self.snapshot(), before)

    def test_an_id_that_is_not_a_plain_matrix_id_is_never_echoed(self):
        for name, mac in self.each():
            for agent in ("@Other.Agent:ag2.space", "@other:ag2.space/see-https://evil.example", "@o:h\x1b[2Jx"):
                with self.subTest(name=name, mac=mac, agent=agent):
                    self.fresh(name, mac)
                    rc, out, err, _stdin, _jobs = self.paste(name, TOKEN, mac, relay=Relay(agent=agent))
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"the relay named a different agent than {fx.AGENT} for the token", err)
                    self.assertNotIn(agent, out + err)
                    self.assertFalse(self.seat().exists())

    def test_a_redirect_is_refused_before_the_credential_leaves_for_its_target(self):
        seen = []

        def server(handler):
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
            return srv.server_address[1]

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append(dict(self.headers))
                body = json.dumps({"agent_mxid": fx.AGENT}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        target = server(Target)

        for code, where in ((302, f"http://localhost:{target}/v1/whoami"), (307, f"http://localhost:{target}/v1/whoami"),
                            (301, "/v1/whoami-elsewhere")):
            class Redirect(http.server.BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    self.send_response(code)
                    self.send_header("Location", where)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

                def log_message(self, *a):
                    pass
            relay = server(Redirect)
            for name, mac in self.each():
                with self.subTest(code=code, name=name, mac=mac):
                    self.fresh(name, mac)
                    before = self.snapshot()
                    rc, out, err, _stdin, jobs = self.paste(name, f"http://127.0.0.1:{relay}|{BEARER}", mac, relay=False)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"the relay redirected (HTTP {code}); not following with a credential; nothing was "
                                  "written", err)
                    self.assertEqual(seen, [], "the redirect's target never receives a request, nor the bearer")
                    self.assertEqual(self.snapshot(), before)
                    self.assertFalse(any(b.started() for b in jobs.bridges.values()))
                    self.assertNotIn(BEARER, out + err)
        self.fresh(CL.name, False)
        rc, out, err, _stdin, jobs = self.paste(CL.name, f"http://127.0.0.1:{target}|{BEARER}", False, relay=False)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([h.get("Authorization") for h in seen], [f"Bearer {BEARER}"], "the control: a direct relay")

    def test_the_whoami_check_is_one_short_authenticated_get_before_the_write(self):
        for name, mac in self.each():
            with self.subTest(name=name, mac=mac):
                self.fresh(name, mac)
                rc, out, err, _stdin, jobs = self.paste(name, FORMS["remote_task_token"], mac)
                self.assert_installed(name, rc, out, err, jobs, mac)
                self.assertEqual(self.relay_fake.calls, [(fx.RELAY + "/v1/whoami", "GET", f"Bearer {BEARER}",
                                                          cm.WHOAMI_TIMEOUT_S)])
                self.assertLessEqual(cm.WHOAMI_TIMEOUT_S, 5)

    def test_a_busy_agent_lock_after_the_read_writes_nothing(self):
        for name, mac in self.each():
            with self.subTest(name=name, mac=mac):
                self.fresh(name, mac)
                before = self.snapshot()
                with mock.patch.object(cb, "_seat_under_lock") as under, mock.patch.object(
                        cm, "agent_lock", return_value=mock.Mock(__enter__=mock.Mock(side_effect=cm.Busy("agent busy")))):
                    rc, out, err, _stdin, _ = self.paste(name, TOKEN, mac)
                under.assert_not_called()
                self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                self.assertIn("agent busy; nothing was written: run the same command once it is done", err)
                self.assertEqual(self.snapshot(), before)


class ReadToken(unittest.TestCase):
    """cm.read_token's edges: no stdin, a failed read, an interrupted prompt and an over-long line are refusals."""

    def test_no_or_unreadable_stdin_or_an_aborted_prompt_is_no_token(self):
        broken = mock.Mock(isatty=mock.Mock(return_value=False), readline=mock.Mock(side_effect=OSError(5, "EIO")))
        eof = mock.Mock(side_effect=EOFError)
        for why, stdin, prompt in (("none", None, None), ("closed", broken, None), ("ctrl-d", Tty(), eof)):
            with self.subTest(why), mock.patch.object(sys, "stdin", None):
                self.assertEqual(cm.read_token(stdin, prompt),
                                 (None, "no token was given: paste it when asked, or pipe one line into --token-stdin"))

    def test_an_over_long_line_is_refused_without_its_value(self):
        line = TOKEN + "x" * cm.TOKEN_LINE_MAX
        got, why = cm.read_token(io.StringIO(line + "\n"))
        self.assertIsNone(got)
        self.assertIn(f"longer than {cm.TOKEN_LINE_MAX} characters", why)
        self.assertNotIn(BEARER, why)


XTRACE_DRIVER = "--xtrace-driver"


DRIVER_CODE = ("import runpy, sys; sys.argv = [%r, %r, *sys.argv[1:]]; "
               "runpy.run_path(sys.argv[0], run_name='__main__')")


def xtrace_driver(name: str, mac: str) -> int:
    """In a child under `bash -x`: a TokenStdin host, then the adapter's install reading the real stdin."""
    case = TokenStdin("test_a_terminal_is_asked_without_echo")
    case.setUp()
    try:
        case.fresh(name, mac == "1")
        jobs = bx.Jobs(case.root, case.runners)
        case.mac = mac == "1"
        with mock.patch.object(sm, "_run", side_effect=jobs), \
                relay_opener(Relay()):
            rc = CLIS[name].main(case.argv(name))
        token = (case.seat() / "token").read_text() if (case.seat() / "token").exists() else ""
        print("SEAT-OK" if token == f"{fx.RELAY}|{BEARER}\n" else "SEAT-MISSING", file=sys.stderr)
        return rc
    finally:
        case.doCleanups()


class Xtrace(unittest.TestCase):
    """Under `set -x` (SHELLOPTS=xtrace, and a BASH_ENV that sets it again) the token never reaches a trace: it
    travels only through stdin, never argv, and the CLI never prints it."""

    def test_the_token_never_appears_in_a_shell_trace(self):
        bash = "/bin/bash" if os.path.exists("/bin/bash") else "bash"
        for name in (CL.name, CX.name):
            for mac in ("0", "1"):
                for form in ("bare", "ag2_remote_token_single_quoted"):
                    with self.subTest(name=name, mac=mac, form=form), tempfile.TemporaryDirectory() as tmp:
                        tok = Path(tmp) / "pasted"
                        tok.write_text(FORMS[form] + "\n")
                        bash_env = Path(tmp) / "bash_env"
                        bash_env.write_text("set -x\n")
                        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "SHELLOPTS": "xtrace", "BASH_ENV": str(bash_env),
                               "PS4": "+TRACE "}
                        script = 'set -x; "$1" -c "$2" "$3" "$4" < "$5"'
                        code = covered_code(DRIVER_CODE % (str(Path(__file__).resolve()), XTRACE_DRIVER))
                        p = subprocess.run([bash, "-x", "-c", script, "bash", sys.executable, code, name, mac, str(tok)],
                                           env=env, capture_output=True, text=True, timeout=300)
                        self.assertIn("+TRACE", p.stderr, "the shell traced")
                        self.assertIn("SEAT-OK", p.stderr, p.stdout + p.stderr)
                        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                        for text in (p.stdout, p.stderr):
                            self.assertNotIn(BEARER, text)
                            self.assertNotIn(TOKEN, text)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == XTRACE_DRIVER:
        sys.exit(xtrace_driver(sys.argv[2], sys.argv[3]))
    unittest.main()
