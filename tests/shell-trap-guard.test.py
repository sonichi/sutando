#!/usr/bin/env python3
"""hooks/shell-trap-guard.py: macOS/zsh shell traps are blocked or warned before a Bash
call runs; the rule set follows the host; hits are logged under the resolved workspace;
the core launcher registers the hook.

Run:  python3 tests/shell-trap-guard.test.py
"""
import importlib.util
import json
import os
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
        self.assertFires("/usr/bin/timeout 3 ls", "timeout", stg.BLOCK)

    def test_timeout_negatives(self):
        self.assertSilent("gtimeout 5 curl -s x")
        self.assertSilent("curl --connect-timeout 5 x")
        self.assertSilent('python3 -c "import subprocess; subprocess.run([\'ls\'], timeout=5)"')
        self.assertSilent('echo "timeout 5 means nothing here"')
        self.assertSilent("cat <<'EOF' > f.py\ntimeout 5 x\nEOF")
        self.assertSilent("grep -n timeout file.py")
        self.assertSilent("ls  # timeout 5 x")

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

    def test_disabled_rule_does_not_fire(self):
        self.assertEqual(stg.analyze("timeout 5 ls", {"pgrep_self"}), [])


class HostRules(unittest.TestCase):
    def test_override_names_the_rules(self):
        with mock.patch.dict(os.environ, {"SHELL_TRAP_GUARD_RULES": "timeout, pgrep_self,bogus"}):
            self.assertEqual(stg.host_rules(), {"timeout", "pgrep_self"})

    def test_gnu_host_gets_no_bsd_rules(self):
        """A Linux host has GNU sed/base64 and coreutils timeout: blocking them would be wrong."""
        env = {k: v for k, v in os.environ.items() if k != "SHELL_TRAP_GUARD_RULES"}
        env["SHELL"] = "/bin/bash"
        which = {"timeout": "/usr/bin/timeout", "sed": "/usr/bin/sed", "base64": "/usr/bin/base64"}.get
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(stg.sys, "platform", "linux"), \
                mock.patch.object(stg.shutil, "which", which):
            self.assertEqual(stg.host_rules(), {"pgrep_self"})

    def test_stock_macos_zsh_gets_every_rule(self):
        env = {k: v for k, v in os.environ.items() if k != "SHELL_TRAP_GUARD_RULES"}
        env["SHELL"] = "/bin/zsh"
        which = {"sed": "/usr/bin/sed", "base64": "/usr/bin/base64"}.get
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(stg.sys, "platform", "darwin"), \
                mock.patch.object(stg.shutil, "which", which):
            self.assertEqual(stg.host_rules(), ALL)

    def test_gnu_tools_first_on_macos_path_are_not_blocked(self):
        env = {k: v for k, v in os.environ.items() if k != "SHELL_TRAP_GUARD_RULES"}
        which = {"timeout": "/opt/homebrew/opt/coreutils/libexec/gnubin/timeout",
                 "sed": "/opt/homebrew/opt/gnu-sed/libexec/gnubin/sed",
                 "base64": "/opt/homebrew/opt/coreutils/libexec/gnubin/base64"}.get
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(stg.sys, "platform", "darwin"), \
                mock.patch.object(stg.shutil, "which", which):
            self.assertFalse(stg.host_rules() & {"timeout", "sed_inplace", "base64_file"})


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
