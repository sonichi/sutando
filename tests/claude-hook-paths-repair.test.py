#!/usr/bin/env python3
"""A moved install's hooks are re-pointed at the current checkout, and nothing else changes.

Fixtures build an "old" install path that no longer exists and a "new" checkout that
does; nothing here reads or writes a live settings file.

Run: python3 tests/claude-hook-paths-repair.test.py
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import claude_hooks_settings as chs  # noqa: E402

SESSION_LAUNCH = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"
START_CLI = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"

SCRIPTS = ["src/check-pending-tasks.sh", "src/session-handoff.sh", "src/turn-start.sh",
           "src/archive-transcript.sh", "src/personal-claude-compact-hint.sh",
           "skills/demo/hooks/pre-tool.sh"]


def sq(path):
    return "'" + path.replace("'", "'\\''") + "'"


def group(*cmds, matcher=""):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": c} for c in cmds]}


def owned(root):
    """The hook commands the installers write, rooted at ``root``."""
    guarded = f"{shlex.quote(root + '/skills/demo/hooks/pre-tool.sh')}"
    return {
        "Stop": f"bash {sq(root + '/src/check-pending-tasks.sh')}",
        "PreCompact": f"bash {sq(root + '/src/session-handoff.sh')} \"$TRANSCRIPT_PATH\"",
        "UserPromptSubmit": f"bash {sq(root + '/src/turn-start.sh')}",
        "SessionStart": f'bash "{root}/src/personal-claude-compact-hint.sh"',
        "PreToolUse": f"[ -f {guarded} ] || exit 0; exec bash {guarded}",
    }


def archiver(root):
    return f"bash {sq(root + '/src/archive-transcript.sh')} \"$HOME/Desktop/sutando-conversations/\""


class RepairHookPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.new = self.tmp / "Application Support" / "it's new" / "engine" / "sutando"
        for rel in SCRIPTS:
            (self.new / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.new / rel).write_text("#!/bin/bash\n")
        self.old = str(self.tmp / "Old Place" / "it's old" / "engine" / "sutando")
        self.other = self.tmp / "users-tool.sh"
        self.other.write_text("#!/bin/bash\n")
        self.settings = self.tmp / "work" / ".claude" / "settings.json"
        self.settings.parent.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data, indent=4):
        # A formatting the repair does not itself produce, so a whole-file rewrite shows.
        text = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
        self.settings.write_text(text, encoding="utf-8")
        return text

    def moved_settings(self, with_archiver):
        old = owned(self.old)
        hooks = {event: [group(cmd)] for event, cmd in old.items()}
        hooks["Stop"][0]["hooks"].append({"type": "command", "command": f'bash "{self.other}"'})
        hooks["Stop"].append(group('bash "/nowhere/at/all/mine.sh"'))
        hooks["Notification"] = [group("say done")]
        if with_archiver:
            hooks["PreCompact"].insert(0, group(archiver(self.old)))
        return {"permissions": {"allow": ["Bash(ls:*)"]}, "env": {"NOTE": "café"},
                "hooks": hooks, "model": "x"}

    def test_moved_hooks_point_at_the_current_checkout(self):
        before = self.write(self.moved_settings(with_archiver=False))
        changed = chs.repair_paths(self.settings, self.new)
        after = json.loads(self.settings.read_text())
        want = owned(str(self.new))
        for event, cmd in want.items():
            self.assertIn(cmd, [h["command"] for g in after["hooks"][event] for h in g["hooks"]], event)
        self.assertEqual(len(changed), len(want))
        self.assertNotIn(self.old, self.settings.read_text())
        # Every other byte is the original: only the old command strings were swapped.
        expected = before
        for event, old_cmd in owned(self.old).items():
            expected = expected.replace(json.dumps(old_cmd, ensure_ascii=False),
                                        json.dumps(want[event], ensure_ascii=False))
        self.assertEqual(self.settings.read_text(), expected)

    def test_every_repointed_command_runs(self):
        self.write(self.moved_settings(with_archiver=False))
        chs.repair_paths(self.settings, self.new)
        after = json.loads(self.settings.read_text())
        for event in owned(self.old):
            for h in after["hooks"][event][0]["hooks"]:
                r = subprocess.run(["bash", "-c", h["command"]], capture_output=True, text=True,
                                   env={"PATH": "/usr/bin:/bin", "TRANSCRIPT_PATH": "/dev/null"})
                self.assertEqual(r.returncode, 0, f"{event}: {r.stderr}")

    def test_an_archiver_that_was_off_stays_off(self):
        self.write(self.moved_settings(with_archiver=False))
        chs.repair_paths(self.settings, self.new)
        self.assertNotIn("archive-transcript.sh", self.settings.read_text())
        self.assertNotIn("sutando-conversations", self.settings.read_text())

    def test_an_archiver_that_was_on_is_repointed_not_duplicated(self):
        self.write(self.moved_settings(with_archiver=True))
        chs.repair_paths(self.settings, self.new)
        cmds = [h["command"] for g in json.loads(self.settings.read_text())["hooks"]["PreCompact"]
                for h in g["hooks"]]
        self.assertEqual([c for c in cmds if "archive-transcript.sh" in c], [archiver(str(self.new))])

    def test_rerun_is_a_no_op(self):
        self.write(self.moved_settings(with_archiver=True))
        chs.repair_paths(self.settings, self.new)
        text, stat = self.settings.read_bytes(), self.settings.stat()
        self.assertEqual(chs.repair_paths(self.settings, self.new), [])
        self.assertEqual(self.settings.read_bytes(), text)
        self.assertEqual(self.settings.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(self.settings.stat().st_ino, stat.st_ino)

    def test_a_healthy_file_is_never_rewritten(self):
        text = self.write({"hooks": {e: [group(c)] for e, c in owned(str(self.new)).items()}}, indent=3)
        ino = self.settings.stat().st_ino
        self.assertEqual(chs.repair_paths(self.settings, self.new), [])
        self.assertEqual(self.settings.read_text(), text)
        self.assertEqual(self.settings.stat().st_ino, ino)

    def test_hooks_that_are_not_a_moved_install_are_left_alone(self):
        live_elsewhere = self.tmp / "other-clone"
        (live_elsewhere / "src").mkdir(parents=True)
        (live_elsewhere / "src" / "check-pending-tasks.sh").write_text("#!/bin/bash\n")
        two_roots = (f"bash {sq(self.old + '/src/turn-start.sh')} "
                     f"{sq(str(self.tmp / 'gone2' / 'src' / 'turn-start.sh'))}")
        unknown = f"bash {sq(self.old + '/src/not-in-this-checkout.sh')}"
        keep = {"hooks": {"Stop": [group(
            f"bash {sq(str(live_elsewhere / 'src' / 'check-pending-tasks.sh'))}",
            two_roots, unknown, "bash -c 'echo hi'",
            'bash "$HOME/old/src/check-pending-tasks.sh"')]}}
        text = self.write(keep)
        self.assertEqual(chs.repair_paths(self.settings, self.new), [])
        self.assertEqual(self.settings.read_text(), text)

    def test_a_live_checkout_missing_one_script_is_not_repointed(self):
        live = self.tmp / "live-checkout"
        (live / "src").mkdir(parents=True)
        (live / "src" / "turn-start.sh").write_text("#!/bin/bash\n")
        cmd = f"bash {sq(str(live / 'src' / 'check-pending-tasks.sh'))}"
        text = self.write({"hooks": {"Stop": [group(cmd)]}})
        self.assertIsNone(chs.repoint_command(cmd, str(self.new)))
        self.assertEqual(chs.repair_paths(self.settings, self.new), [])
        self.assertEqual(self.settings.read_text(), text)

    def test_a_root_on_an_unmounted_volume_is_repaired(self):
        root = str(self.tmp / "Volumes" / "external disk" / "sutando")
        self.assertFalse(os.path.exists(root))
        self.write({"hooks": {"Stop": [group(owned(root)["Stop"])]}})
        self.assertEqual(len(chs.repair_paths(self.settings, self.new)), 1)
        self.assertEqual(json.loads(self.settings.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"],
                         owned(str(self.new))["Stop"])

    def test_an_unquoted_path_is_not_rewritten_into_a_broken_one(self):
        # The new root has a space; splicing it into an unquoted token would split it.
        self.assertNotIn(" ", str(self.tmp))
        cmd = f"bash {self.tmp}/gone/src/check-pending-tasks.sh"
        self.assertIsNone(chs.repoint_command(cmd, str(self.new)))
        plain = self.tmp / "plain"
        (plain / "src").mkdir(parents=True)
        (plain / "src" / "check-pending-tasks.sh").write_text("")
        old = "/no/such/root"
        self.assertEqual(chs.repoint_command(f"bash {old}/src/check-pending-tasks.sh", str(plain)),
                         f"bash {plain}/src/check-pending-tasks.sh")

    def test_missing_and_malformed_files(self):
        self.assertEqual(chs.repair_paths(self.tmp / "absent.json", self.new), [])
        self.settings.write_text("[]")
        with self.assertRaises(ValueError):
            chs.repair_paths(self.settings, self.new)
        self.settings.write_text(json.dumps({"hooks": {"Stop": 7, "X": [7, {"hooks": [7, {"command": 1}]}]}}))
        self.assertEqual(chs.repair_paths(self.settings, self.new), [])

    def test_cli(self):
        self.write(self.moved_settings(with_archiver=False))
        args = [sys.executable, str(REPO / "src" / "claude_hooks_settings.py"), "repair-paths",
                "--repo", str(self.new), "--settings", str(self.settings),
                "--settings", str(self.tmp / "absent.json")]
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count("re-pointed"), len(owned(self.old)))
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        bad = self.tmp / "bad.json"
        bad.write_text("[]")
        r = subprocess.run(args[:-1] + [str(bad)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not an object", r.stderr)

    def test_unparseable_and_shapeless_inputs_are_left_alone(self):
        self.assertIsNone(chs.repoint_command(f'bash "{self.old}/src/turn-start.sh', str(self.new)))
        self.assertEqual(chs.repoint_moved({"hooks": [1]}, str(self.new)), [])

    def test_an_unusual_encoding_falls_back_to_a_full_dump(self):
        old_cmd = owned(self.old)["Stop"]
        self.settings.write_text('{"hooks": {"Stop": [{"hooks": [{"command": %s}]}]}}'
                                 % json.dumps(old_cmd).replace("/", "\\/"))
        self.assertEqual(len(chs.repair_paths(self.settings, self.new)), 1)
        self.assertEqual(json.loads(self.settings.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"],
                         owned(str(self.new))["Stop"])

    def test_a_failed_write_leaves_the_file_and_no_temp_behind(self):
        from unittest import mock
        text = self.write(self.moved_settings(with_archiver=False))
        with mock.patch.object(chs.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                chs.repair_paths(self.settings, self.new)
        self.assertEqual(self.settings.read_text(), text)
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()), ["settings.json"])

    def test_main_in_process(self):
        import contextlib
        import io
        self.write(self.moved_settings(with_archiver=False))
        bad = self.tmp / "bad.json"
        bad.write_text("{")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chs.main(["repair-paths", "--repo", str(self.new),
                           "--settings", str(bad), "--settings", str(self.settings)])
        self.assertEqual(rc, 1)
        self.assertIn("hook path repair", err.getvalue())
        self.assertEqual(out.getvalue().count("re-pointed"), len(owned(self.old)))


class LauncherWiring(unittest.TestCase):
    def test_start_cli_repairs_before_installing_and_after_the_pure_probe(self):
        src = START_CLI.read_text()
        repair = src.index("\nrepair_claude_hook_paths\n")
        self.assertLess(src.index('"--print-core-env"'), repair)
        self.assertLess(repair, src.index("\ninstall_claude_personal_hook\n"))

    def test_the_launcher_function_repairs_both_project_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            new = tmp / "new root" / "sutando"
            for rel in SCRIPTS:
                (new / rel).parent.mkdir(parents=True, exist_ok=True)
                (new / rel).write_text("#!/bin/bash\n")
            (new / "src" / "claude_hooks_settings.py").write_text(
                (REPO / "src" / "claude_hooks_settings.py").read_text())
            work = tmp / "stable-work"
            old = str(tmp / "old root" / "sutando")
            stop = {"hooks": {"Stop": [group(owned(old)["Stop"])]}}
            for d, name in ((new, "settings.json"), (work, "settings.local.json")):
                (d / ".claude").mkdir(parents=True)
                (d / ".claude" / name).write_text(json.dumps(stop))
            script = (f'. {shlex.quote(str(SESSION_LAUNCH))}; REPO={shlex.quote(str(new))}; '
                      f'PY={shlex.quote(sys.executable)}; repair_claude_hook_paths')
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(tmp),
                                    "SUTANDO_CLAUDE_WORKING_DIR": str(work)})
            self.assertEqual(r.returncode, 0, r.stderr)
            for path in (new / ".claude" / "settings.json", work / ".claude" / "settings.local.json"):
                data = json.loads(path.read_text())
                self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"],
                                 owned(str(new))["Stop"], path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
