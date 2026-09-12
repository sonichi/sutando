#!/usr/bin/env python3
"""
src/agent/claude/cli/start-cli.sh must run src/install-claude-hooks.sh on every
Claude launch, before any tmux/CLAUDE_CONFIG_DIR work.

The gap this pins (issue #3221): the installer writes the Sutando-owned hooks
(PreCompact/SessionEnd session-handoff, Stop check-pending-tasks, every
skill-declared hook) into `<engine>/.claude/settings.json` — the tree an app
update replaces — and nothing on the boot path re-ran it. A core launched after
an update ran with no owned hooks until a human re-ran the installer AND
restarted, because Claude Code loads hooks only at session start. The launcher
is the one place every core launch passes through (startup.sh, --restart, menu
bar, the desktop supervisor) and runs before the `claude` process spawns, so
the re-registration is live in the very session it precedes.

Two properties beyond "it is called":
  * unattended: SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 — the ~/Desktop
    transcript archiver is the one owned hook whose effect leaves the workspace;
    health-check's unattended --fix leaves it to explicit opt-in, and a launch
    is unattended too.
  * a failing installer (jq missing exits 2) must not take the launch down.

Hermetic, same harness as start-cli-personal-claude-hook-wiring.test.py: the
REAL launcher source is truncated after the install-claude-hooks call, both
installers are stubbed, and the stub records what it was invoked with.

The end-to-end arm drives the REAL installer against a fixture repo whose
settings.json is in the exact shape an update leaves behind (SessionStart only)
and asserts the owned hooks are present after the launcher prefix runs — the
before/after evidence for the PR, checkable here rather than on a live host.
"""

from __future__ import annotations  # `dict | None` must not be evaluated on Python 3.9

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
PERSONAL_RE = re.compile(r'^\s*bash "\$REPO/scripts/install-personal-claude-hook\.sh"')
# The call spans two lines (`\` continuation): the env-prefixed bash line, then the `||` fallback.
HOOKS_RE = re.compile(r'^\s*SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 bash "\$REPO/src/install-claude-hooks\.sh"')


def _truncated_launcher() -> str:
    """The real launcher up to and including the install-claude-hooks call."""
    lines = LAUNCHER.read_text().splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if HOOKS_RE.match(ln)), None)
    if idx is None:
        raise AssertionError(
            "install-claude-hooks.sh call not found in src/agent/claude/cli/start-cli.sh "
            "— did it move or get removed?"
        )
    end = idx + 1
    # Include the `|| echo … >&2` continuation line so the fallback is what runs on failure.
    while end < len(lines) and lines[end - 1].rstrip().endswith("\\"):
        end += 1
    return "".join(lines[:end])


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo with spaces"
        (self.root / "src/agent/claude/cli").mkdir(parents=True)
        (self.root / "scripts").mkdir(parents=True)
        launcher = self.root / "src/agent/claude/cli/start-cli.sh"
        launcher.write_text(_truncated_launcher())
        launcher.chmod(0o755)
        shutil.copy2(REPO / "scripts/python-binary.sh", self.root / "scripts/python-binary.sh")
        shutil.copy2(REPO / "scripts/core-working-dir.sh", self.root / "scripts/core-working-dir.sh")
        shutil.copy2(REPO / "src/agent/restart-guard.sh", self.root / "src/agent/restart-guard.sh")
        # The personal-claude installer precedes the call under test; stub it to a no-op.
        personal = self.root / "scripts/install-personal-claude-hook.sh"
        personal.write_text("#!/usr/bin/env bash\nexit 0\n")
        personal.chmod(0o755)
        # A throwaway HOME: the real installer mkdirs under $HOME.
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_launcher(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE", "SUTANDO_CLAUDE_WORKING_DIR")}
        env["HOME"] = str(self.home)
        env.update(extra_env or {})
        return subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh")],
            capture_output=True, text=True, timeout=60, env=env,
        )


class StartCliClaudeHooksWiringTest(_Fixture):
    def setUp(self):
        super().setUp()
        self.marker = self.root / "installer-ran.marker"
        installer = self.root / "src/install-claude-hooks.sh"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"ran omit=${{SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE:-unset}}\" >> '{self.marker}'\n"
        )
        installer.chmod(0o755)

    def test_call_sits_after_the_personal_hook_call(self):
        """Ordering pin with a count: exactly one call, and it follows the personal-hook call."""
        text = LAUNCHER.read_text()
        lines = text.splitlines()
        personal = [i for i, ln in enumerate(lines) if PERSONAL_RE.match(ln)]
        hooks = [i for i, ln in enumerate(lines) if HOOKS_RE.match(ln)]
        self.assertEqual(len(personal), 1, personal)
        self.assertEqual(len(hooks), 1, hooks)
        self.assertLess(personal[0], hooks[0])
        self.assertEqual(text.count("install-claude-hooks.sh"), 1)

    def test_launcher_invokes_installer_once_unattended(self):
        result = self.run_launcher()
        self.assertTrue(
            self.marker.exists(),
            f"the Claude launcher did not invoke src/install-claude-hooks.sh (stderr: {result.stderr})",
        )
        self.assertEqual(self.marker.read_text(), "ran omit=1\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_failure_does_not_abort_launch(self):
        """jq missing exits 2; a jq edit failure exits 1. Neither may take the launcher down."""
        installer = self.root / "src/install-claude-hooks.sh"
        for rc in (1, 2):
            installer.write_text(f"#!/usr/bin/env bash\nexit {rc}\n")
            installer.chmod(0o755)
            result = self.run_launcher()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"claude hooks install failed (rc={rc})", result.stderr)


@unittest.skipUnless(shutil.which("jq"), "jq required by the real installer")
class StartCliRestoresOwnedHooksAfterUpdate(_Fixture):
    """The failure itself, end to end: settings.json stripped to SessionStart (what an
    update leaves) -> launcher prefix -> owned hooks present, SessionStart preserved,
    the ~/Desktop archiver NOT added."""

    def setUp(self):
        super().setUp()
        shutil.copy2(REPO / "src/install-claude-hooks.sh", self.root / "src/install-claude-hooks.sh")
        shutil.copy2(REPO / "src/skill_hooks.py", self.root / "src/skill_hooks.py")
        for name in ("session-handoff.sh", "check-pending-tasks.sh"):
            (self.root / "src" / name).write_text("#!/bin/bash\nexit 0\n")
        # One skill-declared hook, so the runtime-discovered set is exercised too.
        skill = self.root / "skills/demo-skill"
        (skill / "hooks").mkdir(parents=True)
        (skill / "hooks/demo-hook.py").write_text("#!/usr/bin/env python3\n")
        (skill / "manifest.json").write_text(json.dumps({
            "name": "demo-skill",
            "hooks": [{"event": "PreToolUse", "command": "./hooks/demo-hook.py"}],
        }))
        self.settings = self.root / ".claude/settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "echo app-owned-session-start"}]}]}}))

    def _commands(self) -> dict:
        conf = json.loads(self.settings.read_text())
        out = {}
        for event, groups in conf.get("hooks", {}).items():
            out[event] = [h["command"] for g in groups for h in g.get("hooks", [])]
        return out

    def test_owned_hooks_present_after_launch(self):
        before = self._commands()
        self.assertEqual(set(before), {"SessionStart"})
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install-claude-hooks: added=", result.stdout)
        after = self._commands()
        self.assertEqual(after["SessionStart"], before["SessionStart"])
        self.assertTrue(any("src/session-handoff.sh" in c for c in after.get("PreCompact", [])), after)
        self.assertTrue(any("src/session-handoff.sh" in c for c in after.get("SessionEnd", [])), after)
        self.assertTrue(any("src/check-pending-tasks.sh" in c for c in after.get("Stop", [])), after)
        self.assertTrue(any("demo-skill/hooks/demo-hook.py" in c for c in after.get("PreToolUse", [])), after)
        self.assertFalse(any("sutando-conversations/" in c for c in after.get("PreCompact", [])), after)
        # Idempotent: a second launch changes nothing.
        result2 = self.run_launcher()
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertIn("added=0", result2.stdout)
        self.assertEqual(self._commands(), after)

    def test_override_launch_dir_receives_the_hooks(self):
        """SUTANDO_CLAUDE_WORKING_DIR moves where the core launches from, and Claude Code reads
        project settings THERE: the engine tree's file must stay as the update left it."""
        cwd = Path(self.tmp.name) / "core cwd"
        engine_before = self.settings.read_text()
        result = self.run_launcher({"SUTANDO_CLAUDE_WORKING_DIR": str(cwd)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_text(), engine_before)
        conf = json.loads((cwd / ".claude" / "settings.json").read_text())
        cmds = {e: [h["command"] for g in v for h in g["hooks"]] for e, v in conf["hooks"].items()}
        self.assertTrue(any("src/session-handoff.sh" in c for c in cmds.get("SessionEnd", [])), cmds)
        self.assertTrue(any("src/check-pending-tasks.sh" in c for c in cmds.get("Stop", [])), cmds)
        self.assertTrue(any("demo-skill/hooks/demo-hook.py" in c for c in cmds.get("PreToolUse", [])), cmds)
        # The scripts stay anchored at the engine, not the launch dir.
        self.assertTrue(all(str(self.root) in c for c in cmds["SessionEnd"]), cmds)

    def test_refused_override_forms_install_nowhere_and_do_not_abort_the_launch(self):
        """`~user/…` and a relative path are refused by the shared resolver: the installer exits 1
        (the launcher's warning line fires), nothing is created for the mangled form, and the
        engine tree's file is untouched. `~/…` resolves under HOME."""
        engine_before = self.settings.read_text()
        for bad in ("~someoneelse/core", "relative/dir"):
            result = self.run_launcher({"SUTANDO_CLAUDE_WORKING_DIR": bad})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("absolute path or start with ~/", result.stderr, bad)
            self.assertIn("claude hooks install failed (rc=1)", result.stderr, bad)
            self.assertEqual(self.settings.read_text(), engine_before, bad)
        self.assertFalse((self.home / "someoneelse").exists())
        self.assertFalse(Path(str(self.home) + "someoneelse").exists())
        result = self.run_launcher({"SUTANDO_CLAUDE_WORKING_DIR": "~/core home"})
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = json.loads((self.home / "core home" / ".claude" / "settings.json").read_text())
        self.assertIn("SessionEnd", conf["hooks"])


class LauncherForwardsTheValidatedInterpreter(unittest.TestCase):
    """A tmux window spawned on an EXISTING server inherits the server's environment, which may
    predate SUTANDO_PY; the launcher must carry the interpreter it validated into every spawn and
    heal path. `--print-core-env` prints the real CORE_ENV_ARGS without launching anything."""

    def test_print_core_env_carries_an_executable_SUTANDO_PY(self):
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k not in ("SUTANDO_CLAUDE_WORKING_DIR",)}
            env["HOME"] = td
            env["SUTANDO_PY"] = sys.executable  # the resolver's first rung, so the value is known
            r = subprocess.run(["/bin/bash", str(LAUNCHER), "--print-core-env"],
                               capture_output=True, text=True, timeout=120, env=env, cwd=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        idx = [i for i, ln in enumerate(lines) if ln == "SUTANDO_PY=" + sys.executable]
        self.assertEqual(len(idx), 1, lines)
        self.assertEqual(lines[idx[0] - 1], "-e", lines)


class RuntimeScopingTest(unittest.TestCase):
    """install-claude-hooks.sh writes Claude Code's own settings.json: Claude-only
    policy, so it belongs at the Claude launcher and nowhere shared with Codex."""

    def test_generic_dispatcher_does_not_call_installer(self):
        self.assertNotIn("install-claude-hooks.sh", (REPO / "src/agent/start-cli.sh").read_text())

    def test_codex_launcher_does_not_call_installer(self):
        codex_launcher = REPO / "src/agent/codex/cli/start-cli.sh"
        self.assertTrue(codex_launcher.is_file())
        self.assertNotIn("install-claude-hooks.sh", codex_launcher.read_text())

    def test_startup_sh_does_not_call_installer(self):
        self.assertNotIn("install-claude-hooks.sh", (REPO / "src/startup.sh").read_text())


if __name__ == "__main__":
    unittest.main()
