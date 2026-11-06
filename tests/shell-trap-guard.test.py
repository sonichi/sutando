#!/usr/bin/env python3
"""hooks/shell-trap-guard.py: macOS/zsh shell traps are blocked or warned before a Bash
call runs; the rule set follows the host; hits are logged under the resolved workspace;
the core launcher registers the hook.

Run:  python3 tests/shell-trap-guard.test.py
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "shell-trap-guard.py"
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

_spec = importlib.util.spec_from_file_location("shell_trap_guard", HOOK)
stg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stg)

ALL = set(stg.ALL_RULES)


def rules(cmd):
    return {(r, a) for r, a, _ in stg.analyze(cmd, ALL)}


class Rules(unittest.TestCase):
    def assertFires(self, cmd, rule, action):
        self.assertIn((rule, action), rules(cmd), cmd)

    def assertSilent(self, cmd):
        self.assertEqual(rules(cmd), set(), cmd)

    def test_for_unquoted_positive(self):
        self.assertFires('files=$(ls); for f in $files; do echo "$f"; done', "for_unquoted_var", stg.WARN)
        self.assertFires('X=`git ls-files`\nfor f in ${X}; do :; done', "for_unquoted_var", stg.WARN)

    def test_for_unquoted_negatives(self):
        self.assertSilent('for x in "${arr[@]}"; do echo $x; done')
        self.assertSilent('files=$(ls); for f in ${=files}; do echo $f; done')
        self.assertSilent('files=$(ls); for f in ${(f)files}; do echo $f; done')
        self.assertSilent('LIST="a b"; for f in $LIST; do echo $f; done')  # not command output
        self.assertSilent('for f in $(ls); do echo $f; done')
        self.assertSilent("files=$(ls); echo 'for f in $files; do'")

    def test_timeout_positive(self):
        self.assertFires("timeout 5 curl -s x", "timeout", stg.BLOCK)
        self.assertFires("cd /tmp && timeout 10 make", "timeout", stg.BLOCK)
        self.assertFires("out=$(timeout 3 ls)", "timeout", stg.BLOCK)
        self.assertFires("FOO=1 timeout 3 ls", "timeout", stg.BLOCK)

    def test_timeout_negatives(self):
        self.assertSilent("gtimeout 5 curl -s x")
        self.assertSilent("curl --connect-timeout 5 x")
        self.assertSilent('python3 -c "import subprocess; subprocess.run([\'ls\'], timeout=5)"')
        self.assertSilent('echo "timeout 5 means nothing here"')
        self.assertSilent("cat <<'EOF' > f.py\ntimeout 5 x\nEOF")
        self.assertSilent("grep -n timeout file.py")
        self.assertSilent("ls  # timeout 5 x")

    def test_timeout_guarded_by_a_probe_is_allowed(self):
        self.assertSilent("command -v timeout >/dev/null && timeout 5 ls")
        self.assertSilent("if command -v timeout >/dev/null; then timeout 5 ls; else ls; fi")
        self.assertSilent("type timeout && timeout 5 ls")
        self.assertSilent("which timeout >/dev/null 2>&1 && timeout 5 ls || ls")
        self.assertFires("command -v gtimeout && timeout 5 ls", "timeout", stg.BLOCK)
        self.assertFires("command timeout 5 ls", "timeout", stg.BLOCK)

    def test_command_by_path_is_checked_against_that_path(self):
        with tempfile.TemporaryDirectory() as d:
            bsd, gnu = Path(d, "bsd"), Path(d, "gnubin")
            bsd.mkdir(), gnu.mkdir()
            for tool in ("sed", "base64", "timeout"):
                (gnu / tool).write_text("")
            for tool in ("sed", "base64"):
                (bsd / tool).write_text("")
            (gnu / "link-sed").mkdir()
            os.symlink(bsd / "sed", gnu / "link-sed" / "sed")
            g, b, q = (shlex.quote(str(x)) for x in (gnu, bsd, d))
            with mock.patch.object(stg, "BSD_DIR", str(bsd)):
                self.assertSilent(f"{g}/timeout 5 ls")
                self.assertSilent(f"{b}/timeout 5 ls")  # no BSD timeout exists
                self.assertSilent(f"{g}/sed -i 's/a/b/' f")
                self.assertSilent(f"{g}/base64 f")
                self.assertSilent(f"{q}/missing/sed -i 's/a/b/' f")
                self.assertFires(f"{b}/sed -i 's/a/b/' f", "sed_inplace", stg.BLOCK)
                self.assertFires(f"{b}/base64 f", "base64_file", stg.BLOCK)
                self.assertFires(f"{g}/link-sed/sed -i 's/a/b/' f", "sed_inplace", stg.BLOCK)
                self.assertFires("sed -i 's/a/b/' f", "sed_inplace", stg.BLOCK)  # bare names: PATH gating

    def test_path_fixtures_survive_a_dir_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="a b ") as d:
            bsd = Path(d, "bsd")
            bsd.mkdir()
            (bsd / "sed").write_text("")
            with mock.patch.object(stg, "BSD_DIR", str(bsd)):
                self.assertFires(f"{shlex.quote(str(bsd))}/sed -i 's/a/b/' f", "sed_inplace", stg.BLOCK)

    def test_gnubin_full_path_on_this_host(self):
        self.assertSilent("/opt/homebrew/opt/coreutils/libexec/gnubin/timeout 5 ls")
        self.assertSilent("/nonexistent/dir/sed -i 's/a/b/' f")

    @unittest.skipUnless(sys.platform == "darwin" and os.path.isfile("/usr/bin/sed"), "BSD sed")
    def test_full_path_bsd_sed_is_blocked_on_macos(self):
        self.assertFires("/usr/bin/sed -i 's/a/b/' f", "sed_inplace", stg.BLOCK)
        self.assertSilent("/usr/bin/timeout 3 ls")

    def test_pgrep_positive(self):
        self.assertFires("pgrep -f voice-agent", "pgrep_self", stg.WARN)
        self.assertFires("pgrep -fl 'node src/x.js'", "pgrep_self", stg.WARN)
        # an unrelated `grep -v` elsewhere in a compound command must not exempt it
        self.assertFires("ls | grep -v '/guards/' | head; pgrep -f probe >/dev/null || true", "pgrep_self", stg.WARN)
        self.assertFires("pgrep -f probe; ps aux | grep -v grep", "pgrep_self", stg.WARN)
        self.assertFires("pgrep -f probe & ps aux | grep -v grep", "pgrep_self", stg.WARN)

    def test_pgrep_negatives(self):
        self.assertSilent("pgrep -f '[v]oice-agent'")
        self.assertSilent("pgrep -f voice-agent | grep -v $$")
        self.assertSilent("pgrep -fl voice-agent | grep -v pgrep")
        self.assertSilent("pgrep -fl voice-agent 2>&1 | grep -v pgrep")
        self.assertSilent("pgrep -fl voice-agent |& grep -v pgrep")
        self.assertSilent("pgrep node")
        self.assertSilent("pgrep -x node")

    def test_sed_positive(self):
        self.assertFires("sed -i 's/a/b/' file", "sed_inplace", stg.BLOCK)
        self.assertFires("sed -i -e 's/a/b/' file", "sed_inplace", stg.BLOCK)
        self.assertFires("sed --in-place 's/a/b/' file", "sed_inplace", stg.BLOCK)
        self.assertFires("sed -Ei 's/a+/b/' file", "sed_inplace", stg.BLOCK)

    def test_sed_negatives(self):
        self.assertSilent("sed -i '' 's/a/b/' file")
        self.assertSilent('sed -i "" "s/a/b/" file')
        self.assertSilent("sed -i .bak 's/a/b/' file")
        self.assertSilent("sed -i.bak 's/a/b/' file")
        self.assertSilent("sed -n '1,5p' file")
        self.assertSilent("gsed -i 's/a/b/' file")
        self.assertSilent("echo 'sed -i s/a/b/ f'")

    def test_base64_positive(self):
        self.assertFires("base64 image.png", "base64_file", stg.BLOCK)
        self.assertFires("x=$(base64 /tmp/a.bin)", "base64_file", stg.BLOCK)
        self.assertFires("base64 image.png 2>/dev/null", "base64_file", stg.BLOCK)

    def test_base64_negatives(self):
        self.assertSilent("base64 -i f")
        self.assertSilent("base64 < f")
        self.assertSilent("base64 <f")
        self.assertSilent("base64 -i f -o out.txt")
        self.assertSilent("cat f | base64")
        self.assertSilent("echo hi | base64 -d")
        self.assertSilent("echo hi | base64 2>/dev/null")
        self.assertSilent("echo hi | base64 > out.txt")
        self.assertSilent("echo hi | base64 2>&1")
        self.assertSilent("base64 --input=f")

    def test_plain_commands_silent(self):
        for c in ["ls -la", "git status", "gh pr view 12 --json state", "python3 -m unittest -q",
                  "cd '/tmp/Application Support' && ls", "echo 'unterminated", "make 2>&1 | tail -5",
                  "sleep 1 &"]:
            self.assertSilent(c)

    def test_env_prefix_and_ampersand_redirect(self):
        self.assertFires("env -i FOO=1 timeout 5 ls", "timeout", stg.BLOCK)
        self.assertSilent("ls &>/dev/null")
        self.assertFires("pgrep -f probe &>/dev/null", "pgrep_self", stg.WARN)

    def test_base64_option_arguments(self):
        self.assertFires("base64 -b 76 f", "base64_file", stg.BLOCK)
        self.assertSilent("base64 -o out.txt -i f")

    def test_unresolvable_path_is_not_bsd(self):
        with mock.patch.object(stg.os.path, "realpath", side_effect=OSError):
            self.assertSilent("/usr/bin/sed -i 's/a/b/' f")

    def test_disabled_rule_does_not_fire(self):
        self.assertEqual(stg.analyze("timeout 5 ls", {"pgrep_self"}), [])


def no_shell_probe_env():
    """os.environ with no rule override and no shell-PATH cache location, so no probe runs."""
    drop = ("SHELL_TRAP_GUARD_RULES", "SHELL_TRAP_GUARD_SHELL_PATH_CACHE", "SUTANDO_REPO_ROOT")
    return {k: v for k, v in os.environ.items() if k not in drop}


class HostRules(unittest.TestCase):
    def test_override_names_the_rules(self):
        with mock.patch.dict(os.environ, {"SHELL_TRAP_GUARD_RULES": "timeout, pgrep_self,bogus"}):
            self.assertEqual(stg.host_rules(), {"timeout", "pgrep_self"})

    def test_gnu_host_gets_no_bsd_rules(self):
        """A Linux host has GNU sed/base64 and coreutils timeout: blocking them would be wrong."""
        env = no_shell_probe_env()
        env["SHELL"] = "/bin/bash"
        which = {"timeout": "/usr/bin/timeout", "sed": "/usr/bin/sed", "base64": "/usr/bin/base64"}.get
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(stg.sys, "platform", "linux"), \
                mock.patch.object(stg.shutil, "which", which):
            self.assertEqual(stg.host_rules(), {"pgrep_self"})

    def test_stock_macos_zsh_gets_every_rule(self):
        env = no_shell_probe_env()
        env["SHELL"] = "/bin/zsh"
        which = {"sed": "/usr/bin/sed", "base64": "/usr/bin/base64"}.get
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(stg.sys, "platform", "darwin"), \
                mock.patch.object(stg.shutil, "which", which):
            self.assertEqual(stg.host_rules(), ALL)

    def test_gnu_tools_first_on_macos_path_are_not_blocked(self):
        env = no_shell_probe_env()
        which = {"timeout": "/opt/homebrew/opt/coreutils/libexec/gnubin/timeout",
                 "sed": "/opt/homebrew/opt/gnu-sed/libexec/gnubin/sed",
                 "base64": "/opt/homebrew/opt/coreutils/libexec/gnubin/base64"}.get
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(stg.sys, "platform", "darwin"), \
                mock.patch.object(stg.shutil, "which", which):
            self.assertFalse(stg.host_rules() & {"timeout", "sed_inplace", "base64_file"})


class ShellPath(unittest.TestCase):
    """`timeout` on the user's shell PATH only (e.g. gnubin added in .zshrc) is not blocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="stg shell ")
        self.tmp = Path(self._tmp.name)
        self.empty = self.tmp / "empty"
        self.gnubin = self.tmp / "gnu bin"
        self.empty.mkdir(), self.gnubin.mkdir()
        tool = self.gnubin / "timeout"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        self.calls = self.tmp / "calls"
        self.cache = self.tmp / "cache.json"
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / ".zshrc").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def fake_shell(self, body):
        (self.tmp / "bin").mkdir(exist_ok=True)
        sh = self.tmp / "bin" / "zsh"
        sh.write_text("#!/bin/sh\necho x >> %s\n%s\n" % (shlex.quote(str(self.calls)), body))
        sh.chmod(0o755)
        return sh

    def reporting_shell(self):
        return self.fake_shell("echo 'rc noise'; printf '\\n%s%%s\\n' %s" % (
            stg.PATH_MARK, shlex.quote("/usr/bin:%s:~/rel" % self.gnubin)))

    def spawns(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def env(self, shell, cache=True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SHELL_TRAP_GUARD_RULES", "SHELL_TRAP_GUARD_SHELL_PATH_CACHE", "SUTANDO_REPO_ROOT")}
        env.update(SHELL=str(shell), PATH=str(self.empty), HOME=str(self.home))
        if cache:
            env["SHELL_TRAP_GUARD_SHELL_PATH_CACHE"] = str(self.cache)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_timeout_only_on_shell_path_is_allowed(self):
        with self.env(self.reporting_shell()):
            self.assertNotIn("timeout", stg.host_rules())
            self.assertEqual(stg.analyze("timeout 5 ls"), [])
        self.assertEqual(json.loads(self.cache.read_text())["path"], "/usr/bin:%s" % self.gnubin)

    def test_cache_hit_does_not_respawn(self):
        with self.env(self.reporting_shell()):
            for _ in range(3):
                self.assertNotIn("timeout", stg.host_rules())
        self.assertEqual(self.spawns(), 1)

    def test_expired_cache_reprobes(self):
        with self.env(self.reporting_shell()), mock.patch.object(stg, "SHELL_PATH_TTL", 0):
            stg.host_rules()
            stg.host_rules()
        self.assertEqual(self.spawns(), 2)

    def test_commands_without_timeout_never_probe(self):
        with self.env(self.reporting_shell()):
            stg.analyze("ls -la")
        self.assertEqual(self.spawns(), 0)

    def test_failed_probe_falls_back_and_is_cached(self):
        for body in ("exit 1", "echo 'no marker'", "printf '\\n%srelative:~/x\\n'" % stg.PATH_MARK):
            self.cache.unlink(missing_ok=True)
            with self.subTest(body=body), self.env(self.fake_shell(body)):
                self.assertIn("timeout", stg.host_rules())
                n = self.spawns()
                self.assertIn("timeout", stg.host_rules())
                self.assertEqual(self.spawns(), n)

    def test_hung_probe_times_out_and_falls_back(self):
        with self.env(self.fake_shell("/bin/sleep 30")), mock.patch.object(stg, "SHELL_PROBE_TIMEOUT", 0.3):
            t0 = time.monotonic()
            self.assertIn("timeout", stg.host_rules())
            self.assertLess(time.monotonic() - t0, 5)

    def test_rc_change_invalidates_the_cache(self):
        with self.env(self.reporting_shell()):
            stg.host_rules()
            os.utime(self.home / ".zshrc", ns=(1, 1))
            stg.host_rules()
        self.assertEqual(self.spawns(), 2)

    def test_missing_shell_falls_back(self):
        with self.env(self.tmp / "no-such-shell"):
            self.assertIn("timeout", stg.host_rules())
        self.assertIsNone(json.loads(self.cache.read_text())["path"])

    def test_unwritable_cache_still_answers(self):
        self.cache.mkdir()
        with self.env(self.reporting_shell()):
            self.assertNotIn("timeout", stg.host_rules())

    def test_lookup_error_falls_back(self):
        with self.env(self.reporting_shell()), mock.patch.object(stg, "user_shell_path", side_effect=RuntimeError):
            self.assertIn("timeout", stg.host_rules())

    def test_cache_under_the_configured_workspace(self):
        ws = self.tmp / "ws"
        with self.env(self.reporting_shell(), cache=False), \
                mock.patch.dict(os.environ, {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws)}):
            self.assertNotIn("timeout", stg.host_rules(["--repo", str(REPO)]))
        self.assertTrue((ws / "state" / "guards" / "shell_path_cache.json").is_file())

    def test_clean_path(self):
        self.assertEqual(stg._clean_path("/a:rel:~/b::/c"), "/a:/c")
        self.assertIsNone(stg._clean_path("rel:~/b"))
        self.assertIsNone(stg._clean_path("/a\0/b"))
        self.assertIsNone(stg._clean_path("/" * 70000))

    def test_no_cache_location_skips_the_probe(self):
        with self.env(self.reporting_shell(), cache=False):
            self.assertIn("timeout", stg.host_rules())
        self.assertEqual(self.spawns(), 0)

    def test_hook_allows_timeout_found_on_shell_path(self):
        env = {k: v for k, v in os.environ.items() if k != "SHELL_TRAP_GUARD_RULES"}
        env.update(SHELL=str(self.reporting_shell()), PATH=str(self.empty),
                   SHELL_TRAP_GUARD_SHELL_PATH_CACHE=str(self.cache), SHELL_TRAP_GUARD_LOG=str(self.tmp / "log"))
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "timeout 5 ls"}})
        p = subprocess.run(PYBASE + [str(HOOK)], input=payload, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual((p.returncode, p.stderr), (0, ""))


class Hook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "hits.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, payload, env_extra=None, argv=()):
        env = dict(os.environ, SHELL_TRAP_GUARD_LOG=str(self.log), SHELL_TRAP_GUARD_RULES=",".join(sorted(ALL)))
        env.pop("SHELL_TRAP_GUARD_DISABLE", None)
        env.update(env_extra or {})
        env = {k: v for k, v in env.items() if v is not None}
        p = subprocess.run(PYBASE + [str(HOOK), *argv], input=payload, capture_output=True, text=True,
                           env=env, timeout=30)
        return p, (self.log.read_text() if self.log.exists() else "")

    def payload(self, cmd):
        return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})

    def test_block_exit2_and_log(self):
        p, log = self.run_hook(self.payload("timeout 5 ls"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("Bash tool's `timeout` parameter", p.stderr)
        self.assertIn("subprocess.run", p.stderr)
        rec = json.loads(log.splitlines()[0])
        self.assertEqual((rec["rule"], rec["action"], rec["command"]), ("timeout", "blocked", "timeout 5 ls"))

    def test_logged_command_keeps_2000_chars(self):
        cmd = "timeout 5 ls " + "x" * 3000
        _, log = self.run_hook(self.payload(cmd))
        self.assertEqual(json.loads(log.splitlines()[0])["command"], cmd[:2000])

    def test_logged_command_redacts_secrets(self):
        cmd = ('timeout 5 curl -H "Authorization: Bearer sk-live-abc123" --token tok999 '
               "--password=pw777 x && GH_TOKEN=ghp_secret1 API_KEY='k3y' password=hunter2 ls")
        p, log = self.run_hook(self.payload(cmd))
        self.assertEqual(p.returncode, 2)
        logged = json.loads(log.splitlines()[0])["command"]
        for secret in ("sk-live-abc123", "tok999", "pw777", "ghp_secret1", "k3y", "hunter2"):
            self.assertNotIn(secret, logged)
        self.assertIn("timeout 5 curl", logged)
        self.assertIn("[REDACTED]", logged)

    def test_unimportable_scanner_fails_open_in_process(self):
        spec = importlib.util.spec_from_file_location("shell_trap_guard_noscan", HOOK)
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"_shell_scan": None}):
            spec.loader.exec_module(mod)
        self.assertIsNone(mod._shell_scan)
        self.assertEqual(mod.decide(self.payload("timeout 5 ls")), (0, "", ""))

    def test_missing_or_broken_scanner_fails_open(self):
        for scanner in (None, "raise ImportError('boom')\n", "def (:\n"):
            with tempfile.TemporaryDirectory() as d:
                hook = Path(d, "shell-trap-guard.py")
                hook.write_text(HOOK.read_text())
                if scanner is not None:
                    Path(d, "_shell_scan.py").write_text(scanner)
                env = dict(os.environ, SHELL_TRAP_GUARD_RULES=",".join(sorted(ALL)))
                p = subprocess.run([sys.executable, str(hook)], input=self.payload("timeout 5 ls"),
                                   capture_output=True, text=True, env=env, timeout=30)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), scanner)

    def test_warn_json(self):
        p, log = self.run_hook(self.payload("pgrep -f foo"))
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("pgrep", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn('"warned"', log)

    def test_clean_silent(self):
        p, log = self.run_hook(self.payload("ls"))
        self.assertEqual((p.returncode, p.stdout, p.stderr, log), (0, "", "", ""))

    def test_non_bash_tool_is_ignored(self):
        p, _ = self.run_hook(json.dumps({"tool_name": "Read", "tool_input": {"command": "timeout 5 ls"}}))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_garbage_input_exits_0(self):
        for bad in ["", "not json", "[]", '{"tool_input": 5}']:
            p, _ = self.run_hook(bad)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), bad)

    def test_disable(self):
        p, _ = self.run_hook(self.payload("timeout 5 ls"), {"SHELL_TRAP_GUARD_DISABLE": "1"})
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_default_log_is_under_the_resolved_workspace(self):
        ws = self.tmp / "ws"
        env = {"SHELL_TRAP_GUARD_LOG": None, "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws)}
        p, _ = self.run_hook(self.payload("timeout 5 ls"), env, argv=("--repo", str(REPO)))
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("SUTANDO_WORKSPACE", p.stderr)
        rec = json.loads((ws / "state" / "guards" / "shell_trap_hits.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["rule"], "timeout")

    def test_unconfigured_repo_still_decides_and_logs_nothing(self):
        env = {"SHELL_TRAP_GUARD_LOG": None, "SUTANDO_REPO_ROOT": None}
        p, _ = self.run_hook(self.payload("timeout 5 ls"), env)
        self.assertEqual(p.returncode, 2)

    def test_fast(self):
        cmd = "; ".join(["sed -i '' s/a/b/ f", "for x in \"${a[@]}\"; do :; done"] * 200)
        t0 = time.perf_counter()
        stg.analyze(cmd, ALL)
        self.assertLess(time.perf_counter() - t0, 0.25)


class Registration(unittest.TestCase):
    def test_the_launcher_registers_the_hook(self):
        launch = (REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh").read_text()
        self.assertIn('"$REPO/hooks/shell-trap-guard.py"', launch)

    def test_the_builder_registers_it_on_bash_with_the_repo(self):
        builder = REPO / "src" / "agent" / "claude" / "cli" / "build-core-settings.mjs"
        hook = "/x/y z/hooks/shell-trap-guard.py"
        out = subprocess.run(["node", str(builder), "/x/hooks/skip-ask-user-question.py", "", "", "", "", "", hook],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        blk = [b for b in json.loads(out)["hooks"]["PreToolUse"]
               if any("shell-trap-guard" in h["command"] for h in b["hooks"])]
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["matcher"], "Bash")
        words = subprocess.run(["/bin/bash", "-c", "printf '%s\\n' " + blk[0]["hooks"][0]["command"]],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(words, ["python3", hook, "--repo", "/x/y z"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
