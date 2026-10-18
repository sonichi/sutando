#!/usr/bin/env python3
"""The tmux probe and the guarded pane-key send are vendored into ag2-sparrow byte-identical with
their two shell scripts, take the interpreter from an explicit --python, and gain the in-command
fence: `fenced()` / `predicate()` build the one server command whose false branch orphans the ticket
(exit 1, nothing sent). Measured against a scratch tmux server on a short /tmp socket (#5695 K4,
v3.6 §4, v3.7 §4, v3.10, v3.13 §2 and the phase-1 delta)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG))
from ag2_sparrow import tmux_pane_keys as vk  # noqa: E402  (the vendored copy is what runs here)
from ag2_sparrow import tmux_probe as vp  # noqa: E402

VENDORED = ("tmux_probe.py", "tmux_pane_keys.py", "tmux-pane-keys.sh", "bounded-wait.sh")
NONCE = "0123456789abcdef0123456789abcdef"


class Packaging(unittest.TestCase):
    def test_every_tmux_module_and_script_is_vendored_byte_identical(self):
        sync = (PKG / "tools" / "sync_from_src.py").read_text()
        for name in VENDORED:
            with self.subTest(name=name):
                self.assertIn('"src/%s": "%s"' % (name, name), sync)
                self.assertEqual((REPO / "src" / name).read_bytes(), (PKG / "ag2_sparrow" / name).read_bytes())
        self.assertIn('"ag2_sparrow" = ["*.sh"]', (PKG / "pyproject.toml").read_text(), "the scripts ship in the wheel")

    def test_the_packaged_scripts_reference_no_repo_helper(self):
        for name in ("tmux-pane-keys.sh", "bounded-wait.sh"):
            text = (PKG / "ag2_sparrow" / name).read_text()
            for needle in ("../scripts", "sutando-config"):
                self.assertNotIn(needle, text, "%s reaches for %s" % (name, needle))

    def test_the_vendored_copy_imports_with_only_the_package_on_the_path(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import ag2_sparrow.tmux_probe, ag2_sparrow.tmux_pane_keys as k\n"
                "assert k.SCRIPT.is_file() and 'file_lock' not in sys.modules\n"
                "import tempfile, os\n"
                "s = os.path.join(tempfile.mkdtemp(dir='/tmp', prefix='pk'), 's')\n"
                "with k.prepare_guard(s).open('ab') as f: assert k.acquire_guard(s, f.fileno()) == 0\n"
                "print('OK')\n" % str(PKG))
        with tempfile.TemporaryDirectory() as cwd:
            r = subprocess.run([sys.executable, "-I", "-c", code], cwd=cwd, capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK"), r.stderr)

    def test_argv_passes_this_interpreter_explicitly(self):
        self.assertEqual(vk.argv("/s", "=c:0", "Enter")[:5], ["bash", str(vk.SCRIPT), "--tmux", "tmux", "--python"])
        self.assertEqual(vk.argv("/s", "=c:0", "Enter")[5], sys.executable)
        self.assertEqual(vk.fence_argv("/s", "%3", "#{==:1,1}", "kill-session -t '$1'", tmux="/t"),
                         ["bash", str(vk.SCRIPT), "--tmux", "/t", "--python", sys.executable, "-S", "/s", "-t", "%3",
                          "--fence", "#{==:1,1}", "kill-session -t '$1'"])


class Builder(unittest.TestCase):
    def test_predicate_is_pane_local_with_the_nonce_glob_and_no_pane_id_term(self):
        got = vk.predicate(NONCE, 4242, "$7", 1792361131)
        self.assertEqual(got, "#{&&:#{==:#{pane_pid},4242},#{&&:#{==:#{session_id},$7},"
                              "#{&&:#{==:#{session_created},1792361131},"
                              "#{m:*--generation=%s --*,#{pane_start_command}}}}}" % NONCE)
        self.assertNotIn("pane_id", got, "-t selects the pane; no pane_id term")
        with_title = vk.predicate(NONCE, 4242, "$7", 1792361131, title_token="tok_1")
        self.assertTrue(with_title.endswith("#{&&:#{m:*--generation=%s --*,#{pane_start_command}},"
                                            "#{==:#{pane_title},tok_1}}}}}" % NONCE))
        for bad in (dict(nonce="zz"), dict(nonce=NONCE + "0"), dict(session_id="7"), dict(title_token="a b"),
                    dict(title_token="")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                kw = dict(nonce=NONCE, pane_pid=1, session_id="$1", session_created=1)
                kw.update(bad)
                vk.predicate(**kw)

    def test_fenced_is_the_if_shell_with_the_orphaning_false_branch(self):
        got = vk.fenced("%3", "#{==:1,1}", "send-keys -t '%3' '/exit' Enter")
        self.assertEqual(got, "if-shell -F -t '%3' '#{==:1,1}' 'send-keys -t '\"'\"'%3'\"'\"' '\"'\"'/exit'\"'\"' Enter' "
                              "'run-shell '\"'\"'$ORPHAN; false'\"'\"''")
        orphan = "mv '/w/ticket.claimed' '/w/ticket.orphaned'"
        self.assertIn(vk.tmux_quote("run-shell " + vk.tmux_quote(orphan + "; false")),
                      vk.fenced("%3", "1", "kill-session -t '$1'", orphan=orphan))


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class ScratchServer(unittest.TestCase):
    """A scratch server on a short socket under /tmp, created and killed here; the adapter's own
    socket is never touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pk", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sock = os.path.join(self.dir, "s")
        hold = Path(self.dir) / "hold"
        hold.write_text("#!/bin/sh\nexec cat\n")
        hold.chmod(0o755)
        self.start_command = "%s --generation=%s --" % (hold, NONCE)
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "t", "-x", "80", "-y", "20",
                        self.start_command], check=True)
        self.addCleanup(subprocess.run, ["tmux", "-S", self.sock, "kill-server"], capture_output=True)
        time.sleep(0.2)
        fields = self.show("#{pane_id} #{pane_pid} #{session_id} #{session_created}").split()
        self.pane, self.pane_pid, self.session_id, self.created = fields[0], int(fields[1]), fields[2], int(fields[3])
        self.pred = vk.predicate(NONCE, self.pane_pid, self.session_id, self.created)

    def show(self, fmt):
        return subprocess.run(["tmux", "-S", self.sock, "display-message", "-p", "-t", "t:0", fmt],
                              capture_output=True, text=True, check=True).stdout.strip()

    def pane_text(self):
        return subprocess.run(["tmux", "-S", self.sock, "capture-pane", "-p", "-t", "t:0"],
                              capture_output=True, text=True).stdout

    def run_fenced(self, action, predicate=None):
        """The vendored wrapper in-process; (rc, names left in the ticket dir before cleanup)."""
        left = {}
        real = shutil.rmtree

        def spy(path, ignore_errors=False, **k):
            if Path(path).name.startswith("tmux-pane-keys."):
                left.update({n: 1 for n in os.listdir(path)})
            return real(path, ignore_errors=ignore_errors, **k)
        with mock.patch.object(vk.shutil, "rmtree", spy), open(os.devnull, "wb") as sink, \
                mock.patch.object(sys, "stdout", mock.Mock(buffer=sink)), mock.patch.object(sys, "stderr", mock.Mock(buffer=sink)):
            rc = vk.run_guarded(self.sock, ["--tmux", "tmux", "-S", self.sock, "-t", self.pane,
                                            "--fence", predicate or self.pred, action])
        return rc, sorted(left)

    def test_the_nonce_delimiter_matches_this_pane_and_refuses_a_longer_nonce(self):
        self.assertEqual(self.show(self.pred), "1", self.show("#{pane_start_command}"))
        self.assertEqual(self.show("#{m:*--generation=%s0 --*,#{pane_start_command}}" % NONCE), "0",
                         "a longer nonce refuses: the ` --` delimiter ends the match")
        self.assertEqual(self.show("#{m:*--generation=%s --*,#{pane_start_command}}" % NONCE[:-1]), "0",
                         "a shorter one too")
        self.assertEqual(self.show(vk.predicate(NONCE[:-1] + ("0" if NONCE[-1] != "0" else "1"),
                                                self.pane_pid, self.session_id, self.created)), "0")
        self.assertEqual(self.show(vk.predicate(NONCE, self.pane_pid + 1, self.session_id, self.created)), "0")

    def test_the_title_token_matches_when_set_and_refuses_otherwise(self):
        subprocess.run(["tmux", "-S", self.sock, "select-pane", "-t", "t:0", "-T", "tok-1"], check=True)
        self.assertEqual(self.show(vk.predicate(NONCE, self.pane_pid, self.session_id, self.created, "tok-1")), "1")
        self.assertEqual(self.show(vk.predicate(NONCE, self.pane_pid, self.session_id, self.created, "tok-2")), "0")
        self.assertEqual(self.show(self.pred), "1", "no token, no title term")

    def test_a_false_predicate_orphans_the_ticket_returns_1_and_sends_nothing(self):
        wrong = vk.predicate(NONCE, self.pane_pid, self.session_id, self.created + 1)
        rc, left = self.run_fenced("send-keys -t %s -l NEVER-SENT" % self.pane, predicate=wrong)
        self.assertEqual(rc, vk.WITHHELD)
        self.assertEqual(left, ["cmd", "err", "out", "stderr", "stdout", "ticket.orphaned"],
                         "orphaned before cleanup; no pending, claimed or uncertain ticket")
        time.sleep(0.2)
        self.assertNotIn("NEVER-SENT", self.pane_text())
        self.assertEqual(vk.fence_status(self.sock), "clear", "a withheld send leaves no uncertainty")
        rc, left = self.run_fenced("send-keys -t %s -l LANDED" % self.pane)
        self.assertEqual((rc, left), (0, ["cmd", "err", "out", "stderr", "stdout", "ticket.claimed"]),
                         "the true predicate sends: claimed, then the ticket dir is removed")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "LANDED" not in self.pane_text():
            time.sleep(0.1)
        self.assertIn("LANDED", self.pane_text())

    def test_the_script_builds_exactly_what_fenced_describes(self):
        seen = {}
        real = vk.subprocess.run

        def spy(args, **k):
            r = real(args, **k)
            work = Path(args[3])
            seen["cmd"] = (work / "cmd").read_text().rstrip("\n")
            seen["orphan"] = "mv %s %s" % (vk.tmux_quote(work / "ticket.claimed"), vk.tmux_quote(work / "ticket.orphaned"))
            return r
        with mock.patch.object(vk.subprocess, "run", spy):
            rc, _ = self.run_fenced("display-message -p fenced")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["cmd"], vk.fenced(self.pane, self.pred, "display-message -p fenced", orphan=seen["orphan"]))

    def test_a_server_killed_mid_command_returns_125_and_fences_the_next_send(self):
        # Measured: a graceful kill-server makes the client exit 0; only the server dying under the
        # command (SIGKILL by its own pid, never a pattern) leaves "server exited unexpectedly", rc 1.
        server = int(self.show("#{pid}"))
        killer = threading.Timer(1.0, os.kill, [server, 9])
        killer.start()
        try:
            rc, left = self.run_fenced("wait-for pk-never-signalled")   # the client blocks on the server
        finally:
            killer.join()
        self.assertEqual(rc, vk.UNCERTAIN, "claimed, then neither sent nor orphaned")
        ticket = vk._pending_ticket(vk.guard_path(self.sock))
        self.addCleanup(shutil.rmtree, ticket.parent, True)
        self.assertEqual(left, [], "the ticket dir is kept as evidence")
        self.assertTrue(ticket.with_name("ticket.claimed").exists())
        self.assertFalse(ticket.with_name("ticket.orphaned").exists())
        self.assertEqual(vk.fence_status(self.sock), "uncertain")
        self.assertEqual(vk.run_guarded(self.sock, ["-S", self.sock, "-t", self.pane, "--", "Enter"]), vk.UNCERTAIN,
                         "blocked until recovery")
        self.assertIs(vp.has_session(self.sock, "t"), False, "and the probe reads the server gone")


if __name__ == "__main__":
    unittest.main()
