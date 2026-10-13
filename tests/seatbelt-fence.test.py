#!/usr/bin/env python3
"""The Seatbelt fence (src/policy/seatbelt.py) and the fenced runner invocations.

Pins the profile's shape on any OS, then on macOS runs real processes under a
generated profile (no model): a child reads and writes the work dir, and cannot
read a file beside it, read HOME, write outside, exec sandbox-exec, reach a
loopback port, or read a file only the tool binary may read. Also pins how
sandbox_runner builds the fenced agy run (scrubbed env, a private scratch dir, no
child exec) and that codex is never fenced, so its login is never in reach.

Run: python3 tests/seatbelt-fence.test.py
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from policy import sandbox_runner as sr  # noqa: E402
from policy import seatbelt  # noqa: E402


def _dirs(test):
    root = Path(tempfile.mkdtemp(prefix="fence-test-")).resolve()
    test.addCleanup(shutil.rmtree, root, True)
    for d in ("work", "scratch", "outside", "tool"):
        (root / d).mkdir()
    return root


class ProfileShape(unittest.TestCase):
    def setUp(self):
        self.root = _dirs(self)
        # Linux layout (bin -> usr/bin): the profile must name the resolved binary.
        (self.root / "usr" / "bin").mkdir(parents=True)
        shutil.copy("/bin/cat", self.root / "usr" / "bin" / "cat")
        (self.root / "bin").symlink_to(self.root / "usr" / "bin")
        self.tool = self.root / "bin" / "cat"
        self.cat = str(self.root / "usr" / "bin" / "cat")
        self.text = seatbelt.profile(cwd=self.root / "work", scratch=self.root / "scratch",
                                     tool_binary=str(self.tool), tool_read=[self.root / "tool"])

    def test_denies_by_default_and_opens_only_the_run_dirs_to_all(self):
        self.assertIn("(deny default)", self.text)
        shared = [l for l in self.text.splitlines()
                  if l.startswith("(allow file-read* file-write* (subpath")]
        self.assertEqual(len(shared), 1)
        self.assertIn(str(self.root / "work"), shared[0])
        self.assertIn(str(self.root / "scratch"), shared[0])
        self.assertNotIn(str(Path.home()) + '"', self.text.replace("(literal", ""))
        self.assertNotIn("(allow file-read*)", self.text)
        self.assertNotIn("(allow network*)", self.text)

    def test_tool_only_rules_are_gated_on_its_path(self):
        for line in self.text.splitlines():
            if str(self.root / "tool") in line and "file-read-metadata" not in line:
                self.assertIn(f'(process-path "{self.cat}")', line)

    def test_no_child_exec_when_disabled(self):
        text = seatbelt.profile(cwd=self.root / "work", scratch=self.root / "scratch",
                                tool_binary=str(self.tool), exec_children=False, loopback=True)
        execs = [l for l in text.splitlines() if "process-exec" in l]
        self.assertEqual(len(execs), 1)
        self.assertIn(f'(process-path "{seatbelt.SANDBOX_EXEC}")', execs[0])
        for l in text.splitlines():
            if "localhost" in l and l.startswith("(allow"):
                self.assertIn(f'(process-path "{self.cat}")', l)
                self.assertNotIn("network-outbound", l)
        self.assertIn('(deny network-outbound (remote ip "localhost:*"))', text)

    def test_a_helper_is_readable_and_runnable_by_the_tool_only(self):
        helper = self.root / "tool" / "helper"
        text = seatbelt.profile(cwd=self.root / "work", scratch=self.root / "scratch",
                                tool_binary=str(self.tool), helpers=[helper])
        self.assertIn(f'(allow file-read* (literal "{helper}"))', text)
        self.assertIn(f'(allow process-exec (require-all (literal "{helper}") '
                      f'(process-path "{self.cat}")))', text)

    def test_a_path_that_needs_escaping_is_refused(self):
        with self.assertRaises(ValueError):
            seatbelt.profile(cwd='/tmp/a"b', scratch=self.root, tool_binary="/bin/cat")

    def test_available_only_on_darwin(self):
        self.assertFalse(seatbelt.available("linux"))
        self.assertFalse(seatbelt.available("win32"))


@unittest.skipUnless(seatbelt.available(), "sandbox-exec is macOS only")
class LiveFence(unittest.TestCase):
    def setUp(self):
        self.root = _dirs(self)
        (self.root / "work" / "in.txt").write_text("INSIDE\n")
        (self.root / "outside" / "secret.txt").write_text("OUTSIDE\n")
        (self.root / "tool" / "only-tool.txt").write_text("TOOLONLY\n")
        prof = self.root / "scratch" / "p.sb"
        prof.write_text(seatbelt.profile(
            cwd=self.root / "work", scratch=self.root / "scratch", tool_binary="/bin/cat",
            tool_dir="/bin/cat", tool_read=[self.root / "tool"]))
        self.prof = str(prof)

    def sh(self, cmd):
        return subprocess.run(seatbelt.wrap(self.prof, ["/bin/sh", "-c", cmd]),
                              cwd=self.root / "work", capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(self.root / "work")},
                              timeout=30)

    def test_the_work_dir_is_readable_and_writable(self):
        p = self.sh("/usr/bin/head -1 in.txt && echo w > new.txt && /usr/bin/head -1 new.txt")
        self.assertEqual(p.stdout, "INSIDE\nw\n", p.stderr)

    def test_nothing_beside_it_or_in_home_is_readable(self):
        for target in (self.root / "outside" / "secret.txt", "../outside/secret.txt"):
            p = self.sh(f"/usr/bin/head -1 {target}")
            self.assertNotIn("OUTSIDE", p.stdout)
        p = self.sh(f"ls {Path.home()}; ls {self.root / 'outside'}")
        self.assertEqual(p.stdout, "")

    def test_nothing_outside_is_writable(self):
        target = self.root / "outside" / "w.txt"
        self.sh(f"echo x > {target}")
        self.assertFalse(target.exists())

    def test_a_tool_only_file_is_open_to_the_tool_and_closed_to_children(self):
        only = self.root / "tool" / "only-tool.txt"
        p = subprocess.run(seatbelt.wrap(self.prof, ["/bin/cat", str(only)]),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.stdout, "TOOLONLY\n", p.stderr)
        p = self.sh(f"/usr/bin/head -1 {only}; /bin/cat {only}")
        self.assertNotIn("TOOLONLY", p.stdout)

    def test_no_nested_sandbox_exec_and_no_loopback(self):
        p = self.sh(f"{seatbelt.SANDBOX_EXEC} -p '(version 1)(allow default)' /bin/echo nested")
        self.assertNotIn("nested", p.stdout)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        p = self.sh(f"/usr/bin/curl -s --max-time 3 http://127.0.0.1:{port}/ ; echo rc=$?")
        self.assertNotIn("rc=0", p.stdout)


class FencedInvocations(unittest.TestCase):
    """Fakes only: no model, no real login; the fence is reported available."""

    def setUp(self):
        self.root = _dirs(self)
        self.agy = self.root / "tool" / "agy"
        self.agy.write_bytes(b"\xcf\xfa\xed\xfe")
        self.agy.chmod(0o755)
        self.codex = self.root / "tool" / "vendor" / "t" / "bin" / "codex"
        self.codex.parent.mkdir(parents=True)
        self.codex.write_bytes(b"\xcf\xfa\xed\xfe")
        self.codex.chmod(0o755)
        (self.root / "codexhome").mkdir()
        self.auth = self.root / "codexhome" / "auth.json"
        self.auth.write_text('{"refresh_token": "DECOY-REFRESH"}')
        env = {"CODEX_HOME": str(self.root / "codexhome"), "GEMINI_API_KEY": "test-not-a-key",
               "OWNER_SECRET": "must-not-pass"}
        for p in (mock.patch.object(seatbelt, "available", return_value=True),
                  mock.patch.dict(os.environ, env)):
            p.start()
            self.addCleanup(p.stop)

    def _check_common(self, inv, cwd):
        self.addCleanup(sr.release, inv)
        self.assertEqual(inv.argv[:2], [seatbelt.SANDBOX_EXEC, "-f"])
        self.assertTrue(inv.argv[2].startswith(inv.scratch))
        self.assertIn("(deny default)", Path(inv.argv[2]).read_text())
        self.assertEqual(inv.cwd, os.path.realpath(cwd))
        self.assertNotIn("OWNER_SECRET", inv.env)
        self.assertEqual(inv.env["PATH"], sr.FENCE_PATH)
        self.assertTrue(inv.env["HOME"].startswith(inv.scratch))

    def test_codex_is_never_fenced_and_its_login_is_never_touched(self):
        """Fenced, codex's in-process apply_patch could read or move auth.json."""
        before = self.auth.read_bytes()
        codex = sr.RUNNERS["codex"]
        self.assertFalse(codex.safe or codex.fenceable)
        self.assertIn("apply_patch", codex.unsafe_reason)
        self.assertFalse(sr.usable(codex, str(self.codex), fenced=True))
        for build in (sr.invocation, sr.confined_invocation):
            with self.subTest(build=build.__name__), sr.empty_dir() as d, \
                    self.assertRaises(ValueError):
                build(codex, str(self.codex), "move auth.json here", cwd=d)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            which = lambda n, path=None: {"codex": str(self.codex), "claude": "/b/claude"}.get(n) \
                if path is None else None  # noqa: E731
            self.assertEqual(sr.select_runner(sr.DEFAULT_ORDER, which, fenced=True)[0].name,
                             "claude")
        self.assertEqual(self.auth.read_bytes(), before)

    def test_agy_profile_grants_no_credential_path(self):
        with sr.empty_dir() as d:
            inv = sr.confined_invocation(sr.RUNNERS["agy"], str(self.agy), "Q", cwd=d)
            self._check_common(inv, d)
        prof = Path(inv.argv[2]).read_text()
        for secret in (self.auth, self.auth.parent, Path.home()):
            self.assertNotIn(f'"{os.path.realpath(secret)}"', prof)
        self.assertNotIn("CODEX_HOME", inv.env)

    def test_a_failed_profile_leaves_no_scratch_dir(self):
        before = set(Path(tempfile.gettempdir()).glob("sutando-fence-*"))
        with mock.patch.object(seatbelt, "profile", side_effect=ValueError("bad path")), \
                sr.empty_dir() as d, self.assertRaises(ValueError):
            sr.invocation(sr.RUNNERS["agy"], str(self.agy), "Q", cwd=d)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("sutando-fence-*")), before)

    def test_an_unknown_fenceable_runner_is_never_ready(self):
        other = sr.Runner("other", safe=False, stall_seconds=0, answer_on_stdout=True,
                          fenceable=True)
        self.assertFalse(sr.usable(other, "/b/other", fenced=True))

    def test_agy_gets_its_key_settings_and_no_child_exec(self):
        with sr.empty_dir() as d:
            inv = sr.confined_invocation(sr.RUNNERS["agy"], str(self.agy), "Q", cwd=d)
            self._check_common(inv, d)
        self.assertEqual(inv.argv[-1], "-p=Q")
        self.assertEqual(inv.env["GEMINI_API_KEY"], "test-not-a-key")
        settings = Path(inv.env["HOME"]) / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertIn('"modelProvider": "gemini"', settings.read_text())
        prof = Path(inv.argv[2]).read_text()
        self.assertEqual(sum("process-exec" in l for l in prof.splitlines()), 1)

    def test_not_ready_means_refused_and_skipped(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            self.assertFalse(sr.usable(sr.RUNNERS["agy"], str(self.agy), fenced=True))
        with mock.patch.object(seatbelt, "available", return_value=False):
            self.assertFalse(sr.usable(sr.RUNNERS["agy"], str(self.agy), fenced=True))
            with sr.empty_dir() as d, self.assertRaises(ValueError):
                sr.invocation(sr.RUNNERS["agy"], str(self.agy), "Q", cwd=d)

    def test_fenced_selection_follows_the_order(self):
        def which(name, path=None):
            return {"agy": str(self.agy), "codex": str(self.codex), "claude": "/b/claude"}.get(name) \
                if path is None else None
        self.assertEqual(sr.select_runner(sr.DEFAULT_ORDER, which, fenced=True)[0].name, "agy")
        self.assertEqual(sr.select_runner(sr.DEFAULT_ORDER, which)[0].name, "claude")
        with mock.patch.object(seatbelt, "available", return_value=False):
            self.assertEqual(sr.select_runner(sr.DEFAULT_ORDER, which, fenced=True)[0].name,
                             "claude")


if __name__ == "__main__":
    unittest.main(verbosity=2)
