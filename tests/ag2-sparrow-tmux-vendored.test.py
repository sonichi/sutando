#!/usr/bin/env python3
"""The tmux probe and the guarded pane-key send are vendored into ag2-sparrow byte-identical with
their two shell scripts, take the interpreter from an explicit --python, and gain the in-command
fence: `fenced()` / `predicate()` build the one server command whose false branch orphans the ticket
(exit 1, nothing sent). Measured against a scratch tmux server on a short /tmp socket."""
import contextlib
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
sys.path.insert(0, str(REPO / "src"))
import tmux_pane_keys as src_keys  # noqa: E402
sys.path.remove(str(REPO / "src"))

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
    """The pure builders, on the vendored copy here and on the canonical src one below."""
    k = vk

    def test_argv_and_fence_argv_take_this_interpreter(self):
        self.assertEqual(self.k.argv("/s", "=c:0", "Enter")[5], sys.executable)
        self.assertEqual(self.k.fence_argv("/s", "%3", "#{==:1,1}", "x", tmux="/t")[3:6], ["/t", "--python", sys.executable])

    def test_predicate_is_pane_local_with_the_nonce_glob_and_no_pane_id_term(self):
        got = self.k.predicate(NONCE, 4242, "$7", 1792361131)
        self.assertEqual(got, "#{&&:#{==:#{pane_pid},4242},#{&&:#{==:#{session_id},$7},"
                              "#{&&:#{==:#{session_created},1792361131},"
                              "#{m:*--generation=%s --*,#{pane_start_command}}}}}" % NONCE)
        self.assertNotIn("pane_id", got, "-t selects the pane; no pane_id term")
        with_title = self.k.predicate(NONCE, 4242, "$7", 1792361131, title_token="tok_1")
        self.assertTrue(with_title.endswith("#{&&:#{m:*--generation=%s --*,#{pane_start_command}},"
                                            "#{==:#{pane_title},tok_1}}}}}" % NONCE))
        for bad in (dict(nonce="zz"), dict(nonce=NONCE + "0"), dict(session_id="7"), dict(title_token="a b"),
                    dict(title_token="")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                kw = dict(nonce=NONCE, pane_pid=1, session_id="$1", session_created=1)
                kw.update(bad)
                self.k.predicate(**kw)

    def test_fenced_is_the_if_shell_with_the_orphaning_false_branch(self):
        got = self.k.fenced("%3", "#{==:1,1}", "send-keys -t '%3' '/exit' Enter")
        self.assertEqual(got, "if-shell -F -t '%3' '#{==:1,1}' 'send-keys -t '\"'\"'%3'\"'\"' '\"'\"'/exit'\"'\"' Enter' "
                              "'run-shell '\"'\"'$ORPHAN'\"'\"''")
        orphan = "mv '/w/ticket.claimed' '/w/ticket.orphaned'"
        self.assertIn(self.k.tmux_quote("run-shell " + self.k.tmux_quote(orphan)),
                      self.k.fenced("%3", "1", "kill-session -t '$1'", orphan=orphan))


class BuilderSrc(Builder):
    k = src_keys


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class ScratchServer(unittest.TestCase):
    """A scratch server on a short socket under /tmp, created and killed here; the adapter's own
    socket is never touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pk", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sock = os.path.join(self.dir, "s")
        hold = Path(self.dir) / "hold"
        self.typed = Path(self.dir) / "typed"
        hold.write_text("#!/bin/sh\nexec cat > %s\n" % self.typed)
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
        self.ticket_files = {}

        def spy(path, ignore_errors=False, **k):
            if Path(path).name.startswith("tmux-pane-keys."):
                left.update({n: 1 for n in os.listdir(path)})
                for name in ("cmd", "out", "err", "stdout", "stderr"):
                    with contextlib.suppress(OSError):
                        self.ticket_files[name] = (Path(path) / name).read_text(errors="replace")
            return real(path, ignore_errors=ignore_errors, **k)
        err = mock.Mock()
        with mock.patch.object(vk.shutil, "rmtree", spy), open(os.devnull, "wb") as sink, \
                mock.patch.object(sys, "stdout", mock.Mock(buffer=sink)), mock.patch.object(sys, "stderr", err):
            err.buffer = sink
            rc = vk.run_guarded(self.sock, ["--tmux", "tmux", "-S", self.sock, "-t", self.pane,
                                            "--fence", predicate or self.pred, action])
        self.guard_said = "".join(str(c.args[0]) for c in err.write.call_args_list if c.args)
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
        self.wait_for_pane_start()
        rc, left = self.run_fenced("send-keys -t %s NEVER-SENT Enter" % self.pane, predicate=wrong)
        self.assertEqual(rc, vk.WITHHELD)
        self.assertEqual(left, ["cmd", "err", "out", "stderr", "stdout", "ticket.orphaned"],
                         "orphaned before cleanup; no pending, claimed or uncertain ticket")
        self.assertEqual(vk.fence_status(self.sock), "clear", "a withheld send leaves no uncertainty")
        self.assertNotIn("returned", self.ticket_files.get("out", "") + self.ticket_files.get("err", ""),
                         "the withheld branch's run-shell succeeds: a failing one prints its status")
        self.assertNotIn("did not send", self.guard_said, "withheld is the script's own exit 1, not an unconfirmed send")
        self.assertEqual(self.show("#{pane_in_mode}"), "0",
                         "nor does it leave the pane in view mode, where the next keys would go (tmux < 3.5)")
        rc, left = self.run_fenced("send-keys -t %s LANDED Enter" % self.pane)
        self.assertEqual((rc, left), (0, ["cmd", "err", "out", "stderr", "stdout", "ticket.claimed"]),
                         "the true predicate sends: claimed, then the ticket dir is removed")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "LANDED" not in self.typed_text():
            time.sleep(0.1)
        typed = self.typed_text()
        self.assertEqual(typed, "LANDED\n", "the pane received the true send and never the false one\n" +
                         (self.diagnostics() if typed != "LANDED\n" else ""))

    def wait_for_pane_start(self, seconds=5.0):
        """`exec cat > typed`: the redirect makes the file, then cat takes the pane."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.show("#{pane_current_command}") == "cat" or self.typed.exists():
                return
            time.sleep(0.05)

    def diagnostics(self):
        def tmux(*args):
            r = subprocess.run(["tmux", *args], capture_output=True, text=True)
            return (r.stdout + r.stderr).strip()
        try:
            os.kill(self.pane_pid, 0)
            alive = "alive"
        except OSError as e:
            alive = "not alive (%s)" % e
        typed = "size %d" % self.typed.stat().st_size if self.typed.exists() else "absent"
        files = "\n".join("--- %s ---\n%s" % kv for kv in sorted(self.ticket_files.items()))
        return "\n".join([
            "tmux -V: " + tmux("-V"),
            "wrapper ticket files (the server command is cmd):\n" + files,
            "list-panes -a:\n" + tmux("-S", self.sock, "list-panes", "-a", "-F",
                                       "#{pane_id} #{pane_pid} #{pane_dead} #{pane_current_command} #{pane_start_command}"),
            "capture-pane -p:\n" + tmux("-S", self.sock, "capture-pane", "-p", "-t", self.pane),
            "pane pid %d: %s" % (self.pane_pid, alive),
            "typed file %s: %s" % (self.typed, typed)])

    def typed_text(self):
        try:
            return self.typed.read_text()
        except FileNotFoundError:
            return ""

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

    def test_a_respawned_pane_with_a_reused_pid_and_the_same_command_refuses_on_the_title(self):
        """Each start of the pane's command sets a fresh title token; a respawn keeps the start
        command and the nonce, so a predicate whose pid term happens to match (a reused pid) still
        refuses on the recorded title."""
        titled = Path(self.dir) / "titled"
        titled.write_text("#!/bin/sh\nprintf '\\033]2;t%s\\007' \"$(od -An -N8 -tx1 /dev/urandom | tr -d ' \\n')\"\nexec cat\n")
        titled.chmod(0o755)
        command = "%s --generation=%s --" % (titled, NONCE)
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "r", "-x", "80", "-y", "20", command], check=True)

        def fields():
            return subprocess.run(["tmux", "-S", self.sock, "display-message", "-p", "-t", "r:0",
                                   "#{pane_pid} #{session_id} #{session_created} #{pane_title}"],
                                  capture_output=True, text=True, check=True).stdout.split()

        def holds(pred):
            return subprocess.run(["tmux", "-S", self.sock, "display-message", "-p", "-t", "r:0", pred],
                                  capture_output=True, text=True, check=True).stdout.strip()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not fields()[3].startswith("t"):
            time.sleep(0.05)
        pid, sid, created, title = fields()
        self.assertEqual(holds(vk.predicate(NONCE, int(pid), sid, int(created), title)), "1", "the live generation")
        subprocess.run(["tmux", "-S", self.sock, "respawn-pane", "-k", "-t", "r:0"], check=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and fields()[3] == title:
            time.sleep(0.05)
        pid2, _, _, title2 = fields()
        self.assertNotEqual((pid2, title2), (pid, title), "the respawn ran the command again")
        self.assertEqual(holds("#{m:*--generation=%s --*,#{pane_start_command}}" % NONCE), "1",
                         "the respawn keeps the start command and the nonce")
        self.assertEqual(holds(vk.predicate(NONCE, int(pid2), sid, int(created))), "1",
                         "without the title term a reused pid would pass")
        self.assertEqual(holds(vk.predicate(NONCE, int(pid2), sid, int(created), title)), "0",
                         "the recorded title refuses the respawned process")

    def test_the_old_trailing_space_form_fails_on_a_command_that_ends_with_the_nonce(self):
        """Measured: `--generation=<hex> --` with the `--*` glob matches the exact pane command;
        the older `--generation <hex> *` form never matches a command ending in the nonce."""
        ends = "%s --generation %s" % (Path(self.dir) / "hold", NONCE)
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "e", "-x", "80", "-y", "20", ends], check=True)
        out = subprocess.run(["tmux", "-S", self.sock, "display-message", "-p", "-t", "e:0",
                              "#{m:*--generation %s *,#{pane_start_command}}" % NONCE], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "0", "the trailing-space glob misses a command that ends with the nonce")
        self.assertEqual(self.show("#{m:*--generation=%s --*,#{pane_start_command}}" % NONCE), "1",
                         "the delimited form matches the exact pane command")


if __name__ == "__main__":
    unittest.main()
