#!/usr/bin/env python3
"""claude_hooks_settings: sweep only what a Sutando installer wrote, touch nothing else."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import claude_hooks_settings as chs  # noqa: E402

TP = '"$TRANSCRIPT_PATH"'


def _settings(**events) -> dict:
    return {"model": "keep-me", "hooks": {
        ev: [{"matcher": "", "hooks": [{"type": "command", "command": c} for c in cmds]}]
        for ev, cmds in events.items()}}


def _commands(settings: dict, event: str) -> list[str]:
    return [h["command"] for g in settings["hooks"].get(event, []) for h in g["hooks"]]


class Fixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        # A space and an apostrophe: the installer's shq() escaping must be reproduced exactly.
        self.repo = Path(self._td.name) / "my repo's"
        (self.repo / "src").mkdir(parents=True)
        for name in ("check-pending-tasks.sh", "session-handoff.sh", "turn-start.sh"):
            (self.repo / "src" / name).write_text("#!/bin/bash\n")
        self.owned = chs.emitted_records(self.repo)
        self.q = lambda name: chs._shq(str(self.repo / "src" / name))
        self.dq = lambda name: f'"{self.repo / "src" / name}"'

    def tearDown(self):
        self._td.cleanup()


class EmittedRecords(Fixture):
    def test_every_historical_installer_form_is_recognized(self):
        want = {
            ("Stop", "", f"bash {self.q('check-pending-tasks.sh')}"),
            ("UserPromptSubmit", "", f"bash {self.q('turn-start.sh')}"),
            ("PreCompact", "", f"bash {self.q('session-handoff.sh')} {TP}"),
            ("SessionEnd", "", f"bash {self.q('session-handoff.sh')} {TP}"),
            ("SessionEnd", "", f'bash {self.dq("session-handoff.sh")} "${{TRANSCRIPT_PATH:-}}"'),
            ("PreCompact", "", f'bash {self.q("archive-transcript.sh")} "$HOME/Desktop/sutando-conversations/"'),
            ("PreCompact", "", chs._DESKTOP_ARCHIVE_CP),
            ("Stop", "", "bash $HOME/Desktop/sutando/src/check-pending-tasks.sh"),
            ("Stop", "", chs._WATCHER_KILL_STOP),
            ("SessionEnd", "", f"bash $HOME/Desktop/sutando/src/session-handoff.sh {TP}"),
            ("SessionStart", "", f"bash {self.dq('schedule-crons-session-hint.sh')}"),
            ("SessionStart", "compact", f"bash {self.dq('personal-claude-compact-hint.sh')}"),
            ("SessionStart", "compact|resume", f"bash {self.dq('watcher-rearm-session-hint.sh')}"),
        }
        self.assertEqual(want - self.owned, set())

    def test_the_shq_form_matches_the_builder_output_byte_for_byte(self):
        import subprocess
        out = subprocess.run(
            ["node", str(REPO / "src/agent/claude/cli/build-core-settings.mjs"), "/g.py",
             "--owned-hooks", str(self.repo)], capture_output=True, text=True, check=True).stdout
        built = {r for r in chs.records_in(json.loads(out)) if r[0] != "PreToolUse"}
        self.assertEqual(built, chs.owned_launch_records(self.repo))
        # Hints were written double-quoted by their installers, so only the core four overlap.
        core = {r for r in built if r[0] != "SessionStart"}
        self.assertEqual(core - self.owned, set())

    def test_skill_hooks_are_recognized_in_both_emitted_forms(self):
        skill = self.repo / "skills" / "demo"
        (skill / "hooks").mkdir(parents=True)
        (skill / "hooks" / "g.py").write_text("")
        (skill / "manifest.json").write_text(json.dumps(
            {"name": "demo", "hooks": [{"event": "Stop", "command": "./hooks/g.py"}]}))
        owned = chs.emitted_records(self.repo)
        import shlex
        q = shlex.quote(str(skill.resolve() / "hooks" / "g.py"))
        self.assertIn(("Stop", "", f"python3 {q}"), owned)
        self.assertIn(("Stop", "", f"[ -f {q} ] || exit 0; exec python3 {q}"), owned)


class Sweep(Fixture):
    def test_removes_exact_copies_and_keeps_everything_else(self):
        operator = Path(self._td.name) / "operator" / "$CUSTOM_ROOT" / "src"
        operator.mkdir(parents=True)
        (operator / "session-handoff.sh").write_text("#!/bin/bash\n")
        keep = [
            # An escaped literal `$` inside double quotes names a real, executable script.
            f'bash "{operator.parent.parent}/\\$CUSTOM_ROOT/src/session-handoff.sh" {TP}',
            f"bash -x {self.q('session-handoff.sh')} {TP}",
            f"bash {self.q('session-handoff.sh')} {TP} --operator-flag",
            "bash $CUSTOM_ROOT/src/session-handoff.sh \"$TRANSCRIPT_PATH\"",
            "echo unrelated",
        ]
        s = _settings(PreCompact=[f"bash {self.q('session-handoff.sh')} {TP}", *keep],
                      Stop=[f"bash {self.q('check-pending-tasks.sh')}"],
                      # The right command under the wrong event is not something we wrote.
                      Notification=[f"bash {self.q('check-pending-tasks.sh')}"])
        removed = chs.sweep(s, self.owned)
        self.assertEqual(sorted(e for e, _ in removed), ["PreCompact", "Stop"])
        self.assertEqual(_commands(s, "PreCompact"), keep)
        self.assertEqual(s["hooks"]["Stop"], [], "an emptied group is dropped")
        self.assertEqual(_commands(s, "Notification"), [f"bash {self.q('check-pending-tasks.sh')}"])
        self.assertEqual(s["model"], "keep-me")

    def test_a_moved_checkouts_exact_record_is_removed_and_nothing_else_dead(self):
        gone = "/nonexistent-clone/src"
        s = {"hooks": {"SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": c} for c in (
            f'bash "{gone}/personal-claude-compact-hint.sh"',
            f'bash "{gone}/someone-elses-hint.sh"',
            'bash "$HOME/gone/src/watcher-rearm-session-hint.sh"',
            "bash src/turn-start.sh",
        )]}]}}
        removed = chs.sweep(s, self.owned)
        self.assertEqual([c for _e, c in removed], [f'bash "{gone}/personal-claude-compact-hint.sh"'])
        self.assertEqual(len(_commands(s, "SessionStart")), 3)

    def test_an_exact_command_under_an_operator_matcher_is_kept(self):
        cmd = f"bash {self.q('check-pending-tasks.sh')}"
        s = {"hooks": {"Stop": [{"matcher": "operator-scope", "hooks": [{"type": "command", "command": cmd}]}]}}
        self.assertEqual(chs.sweep(s, self.owned), [])
        self.assertEqual(_commands(s, "Stop"), [cmd])

    def test_a_dead_path_is_not_ownership_by_basename(self):
        keep = [
            "bash /offline/operator/session-handoff.sh --operator-flag",
            f'bash "/nonexistent-clone/src/session-handoff.sh" {TP} --operator-flag',
        ]
        s = _settings(Notification=keep, PreCompact=list(keep))
        self.assertEqual(chs.sweep(s, self.owned), [])
        self.assertEqual(_commands(s, "Notification"), keep)
        self.assertEqual(_commands(s, "PreCompact"), keep)

    def test_the_retired_watcher_kill_stop_hook_is_removed(self):
        s = _settings(Stop=[chs._WATCHER_KILL_STOP, "echo operator"])
        self.assertEqual(chs.sweep(s, self.owned), [("Stop", chs._WATCHER_KILL_STOP)])
        self.assertEqual(_commands(s, "Stop"), ["echo operator"])

    def test_malformed_shapes_are_left_alone(self):
        s = {"hooks": {"Stop": [
            "not-a-dict",
            {"matcher": "", "hooks": "not-a-list"},
            {"matcher": "", "hooks": []},
            {"matcher": "", "hooks": ["str", {"type": "command"}]},
        ], "PreCompact": "not-a-list"}}
        before = json.dumps(s)
        self.assertEqual(chs.sweep(s, self.owned), [])
        self.assertEqual(json.dumps(s), before)
        self.assertEqual(chs.sweep({"hooks": []}, self.owned), [])
        self.assertEqual(chs.sweep({}, self.owned), [])


class SweepFile(Fixture):
    def _write(self, data) -> Path:
        p = Path(self._td.name) / "cfg" / "settings.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data))
        return p

    def test_missing_file_is_a_noop_and_is_never_created(self):
        p = Path(self._td.name) / "absent" / "settings.json"
        self.assertEqual(chs.sweep_file(p, self.owned), [])
        self.assertFalse(p.exists())

    def test_dry_run_reports_without_writing(self):
        p = self._write(_settings(Stop=[f"bash {self.q('check-pending-tasks.sh')}"]))
        before = p.read_bytes()
        self.assertEqual(len(chs.sweep_file(p, self.owned, dry_run=True)), 1)
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(len(chs.sweep_file(p, self.owned)), 1)
        self.assertEqual(json.loads(p.read_text())["hooks"]["Stop"], [])
        self.assertEqual([x.name for x in p.parent.iterdir()], ["settings.json"], "no temp file left")
        self.assertEqual(chs.sweep_file(p, self.owned), [], "idempotent")

    def test_a_rewrite_keeps_the_files_mode(self):
        p = self._write(_settings(Stop=[f"bash {self.q('check-pending-tasks.sh')}"]))
        os.chmod(p, 0o600)
        old = os.umask(0o022)
        try:
            self.assertEqual(len(chs.sweep_file(p, self.owned)), 1)
        finally:
            os.umask(old)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_a_non_object_file_raises(self):
        with self.assertRaises(ValueError):
            chs.sweep_file(self._write("[1, 2]"), self.owned)


class Targets(Fixture):
    def test_working_dir_adds_its_own_project_file_once(self):
        base = self.repo / ".claude" / "settings.json"
        with mock.patch.dict(os.environ, {"SUTANDO_CLAUDE_WORKING_DIR": ""}):
            self.assertEqual(chs.default_targets(self.repo), [base])
        with mock.patch.dict(os.environ, {"SUTANDO_CLAUDE_WORKING_DIR": str(self.repo)}):
            self.assertEqual(chs.default_targets(self.repo), [base])
        other = Path(self._td.name) / "wd"
        with mock.patch.dict(os.environ, {"SUTANDO_CLAUDE_WORKING_DIR": str(other)}):
            self.assertEqual(chs.default_targets(self.repo), [base, other / ".claude" / "settings.json"])


class Main(Fixture):
    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"SUTANDO_CLAUDE_WORKING_DIR": ""}):
            rc = chs.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_sweeps_the_project_file_and_an_extra_settings_file(self):
        proj = self.repo / ".claude" / "settings.json"
        proj.parent.mkdir()
        proj.write_text(json.dumps(_settings(Stop=[f"bash {self.q('check-pending-tasks.sh')}", "echo mine"])))
        ccd = Path(self._td.name) / "ccd.json"
        ccd.write_text(json.dumps(_settings(
            SessionEnd=[f'bash {self.dq("session-handoff.sh")} "${{TRANSCRIPT_PATH:-}}"'])))
        rc, out, _ = self._run("sweep", "--repo", str(self.repo), "--settings", str(ccd), "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would remove Stop", out)
        self.assertIn("2 owned entries found", out)
        rc, out, _ = self._run("sweep", "--repo", str(self.repo), "--settings", str(ccd))
        self.assertEqual(rc, 0)
        self.assertIn("2 owned entries removed", out)
        self.assertEqual(_commands(json.loads(proj.read_text()), "Stop"), ["echo mine"])
        rc, out, _ = self._run("sweep", "--repo", str(self.repo), "--settings", str(ccd))
        self.assertIn("0 owned entries removed", out)

    def test_an_unreadable_file_is_reported_left_untouched_and_fails(self):
        bad = Path(self._td.name) / "bad.json"
        bad.write_text("{not json")
        rc, out, err = self._run("sweep", "--repo", str(self.repo), "--settings", str(bad))
        self.assertEqual(rc, 1)
        self.assertIn("left untouched", err)
        self.assertEqual(bad.read_text(), "{not json")
        self.assertIn("0 owned entries removed", out)

    def test_singular_wording(self):
        p = Path(self._td.name) / "one.json"
        p.write_text(json.dumps(_settings(Stop=[f"bash {self.q('check-pending-tasks.sh')}"])))
        _rc, out, _ = self._run("sweep", "--repo", str(self.repo), "--settings", str(p))
        self.assertIn("1 owned entry removed", out)


class CoreConfig(Fixture):
    def test_resolves_under_the_repo_workspace_and_is_swept(self):
        env = {"SUTANDO_CLAUDE_WORKING_DIR": "", "SUTANDO_TEST_MODE": "1",
               "SUTANDO_WORKSPACE": str(Path(self._td.name) / "ws")}
        with mock.patch.dict(os.environ, env):
            ccd = chs.core_config_settings(self.repo)
            self.assertEqual(ccd, (Path(self._td.name) / "ws").resolve() / ".claude-sutando" / "settings.json")
            ccd.parent.mkdir(parents=True)
            ccd.write_text(json.dumps(_settings(
                SessionEnd=[f'bash {self.dq("session-handoff.sh")} "${{TRANSCRIPT_PATH:-}}"'])))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(chs.main(["sweep", "--repo", str(self.repo), "--no-core-config"]), 0)
            self.assertIn("0 owned entries removed", out.getvalue())
            with contextlib.redirect_stdout(out):
                self.assertEqual(chs.main(["sweep", "--repo", str(self.repo)]), 0)
            self.assertEqual(json.loads(ccd.read_text())["hooks"]["SessionEnd"], [])

    def test_an_unresolvable_config_dir_is_skipped(self):
        with mock.patch.dict(sys.modules, {"sutando_config": None}):
            self.assertIsNone(chs.core_config_settings(self.repo))


class LaunchCheck(Fixture):
    def _built(self) -> str:
        import subprocess
        return subprocess.run(
            ["node", str(REPO / "src/agent/claude/cli/build-core-settings.mjs"), "/g.py",
             "--owned-hooks", str(self.repo)], capture_output=True, text=True, check=True).stdout

    def test_the_builders_output_passes_and_a_partial_or_foreign_one_fails(self):
        built = self._built()
        self.assertEqual(chs.launch_check(self.repo, built), [])
        partial = json.loads(built)
        partial["hooks"]["SessionStart"] = partial["hooks"]["SessionStart"][:1]
        self.assertEqual(len(chs.launch_check(self.repo, json.dumps(partial))), 2)
        echoes = {"hooks": {e: [{"matcher": "", "hooks": [{"type": "command", "command": "echo"}]}]
                            for e in ("Stop", "UserPromptSubmit", "PreCompact", "SessionEnd", "SessionStart")}}
        self.assertEqual(len(chs.launch_check(self.repo, json.dumps(echoes))), 7)
        with self.assertRaises(ValueError):
            chs.launch_check(self.repo, "")

    def test_the_cli_exits_nonzero_unless_everything_is_carried(self):
        for text, rc in ((self._built(), 0), ("{}", 1), ("not json", 1)):
            with mock.patch.object(sys, "stdin", io.StringIO(text)), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(chs.main(["launch-check", "--repo", str(self.repo)]), rc, text[:20])


class ScriptPathOf(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(chs.script_path_of("bash /x/y.sh"), "/x/y.sh")
        self.assertEqual(chs.script_path_of("python3 '/x/y z.py' --flag"), "/x/y z.py")
        self.assertIsNone(chs.script_path_of("bash"))
        self.assertIsNone(chs.script_path_of(""))
        self.assertIsNone(chs.script_path_of("-x -y"))
        self.assertEqual(chs.script_path_of("/x/run --a"), "/x/run")
        self.assertEqual(chs.script_path_of('bash "/x/unterminated'), '"/x/unterminated')


if __name__ == "__main__":
    unittest.main(verbosity=2)
