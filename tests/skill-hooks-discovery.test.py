#!/usr/bin/env python3
"""A skill declares its own hook; core discovers, never names one."""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from skill_hooks import discover, python_runner_prefix, LEGACY_SEP
import os


class SkillHookDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)

    def _skill(self, name, manifest, hook_body="#!/usr/bin/env python3\n"):
        d = self.repo / "skills" / name
        (d / "hooks").mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(manifest))
        if hook_body is not None:
            (d / "hooks" / "g.py").write_text(hook_body)
        return d

    def test_a_declared_present_hook_is_discovered(self):
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        rows = discover(self.repo)
        self.assertEqual(len(rows), 1)
        event, token, cmd, legacy = rows[0]
        self.assertEqual(event, "PreToolUse")
        self.assertEqual(token, "g.py")
        q = shlex.quote(str(self.repo.resolve() / "skills/demo/hooks/g.py"))
        # The interpreter is resolved at EVENT time by the one owner of that policy,
        # scripts/python-binary.sh; nothing in the command names an interpreter itself.
        self.assertEqual(cmd, f'[ -f {q} ] || exit 0; {python_runner_prefix(self.repo)}exec "$_py" {q}')
        self.assertIn("scripts/python-binary.sh", cmd)
        self.assertIn("resolve_python", cmd)
        # Every shape an earlier installer wrote, so the sweep can match and replace them
        # without re-deriving them from `cmd`.
        shapes = legacy.split(LEGACY_SEP)
        self.assertEqual(shapes[:2], [f"python3 {q}", f"[ -f {q} ] || exit 0; exec python3 {q}"])
        self.assertTrue(all('"${SUTANDO_PY:-' in x for x in shapes[2:]), shapes)

    def test_prior_command_survives_a_repo_path_containing_exec_and_pipe(self):
        """`${CMD#*exec }` splits at the first `exec ` — inside the path, not the
        keyword — so the derived shape matched nothing. Emitting it is immune."""
        self._td.cleanup()
        self._td = tempfile.TemporaryDirectory(prefix="exec repo|x ")
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        _event, _token, cmd, legacy = discover(self.repo)[0]
        prior = legacy.split(LEGACY_SEP)[0]
        self.assertEqual(prior, f"python3 {shlex.quote(str(self.repo.resolve() / 'skills/demo/hooks/g.py'))}")
        # What the installer used to compute. It is wrong here, and that is the bug.
        self.assertNotEqual(cmd.split("exec ", 1)[1], prior,
                            "fixture must actually exercise the bad derivation")

    def _resolver_beside(self):
        """The command sources <repo>/scripts/python-binary.sh; the fixture needs the real one."""
        (self.repo / "scripts").mkdir(exist_ok=True)
        shutil.copy2(Path(__file__).resolve().parent.parent / "scripts/python-binary.sh",
                     self.repo / "scripts/python-binary.sh")

    def test_a_stale_SUTANDO_PY_falls_to_the_bundled_interpreter_not_rc_126(self):
        """The unvalidated `${SUTANDO_PY:-…}` expansion exec'd a nonexistent override (126). The
        resolver checks executability, so a stale override falls through to the bundled python."""
        self._resolver_beside()
        bundled = self.repo.parent / "runtime" / "python" / "bin"
        bundled.mkdir(parents=True, exist_ok=True)
        (bundled / "python3").unlink(missing_ok=True)
        os.symlink(sys.executable, bundled / "python3")
        self.addCleanup(lambda: (bundled / "python3").unlink(missing_ok=True))
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]}, hook_body="print('HOOK_EXECUTED')")
        cmd = discover(self.repo)[0][2]
        stub_dir = self.repo / "bin"; stub_dir.mkdir()
        (stub_dir / "python3").write_text("#!/bin/bash\nexit 79\n"); (stub_dir / "python3").chmod(0o755)
        env = dict(os.environ, PATH=f"{stub_dir}:{os.environ.get('PATH', '')}",
                   SUTANDO_PY=str(self.repo / "no" / "such" / "python3"))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "HOOK_EXECUTED"), r.stderr)

    def test_no_runnable_interpreter_fails_open_like_the_existence_guard(self):
        """No SUTANDO_PY, no bundled python, and the resolver refusing PATH: the hook must exit 0
        (the tool is not blocked) rather than exec an empty string."""
        self._resolver_beside()
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]}, hook_body="print('HOOK_EXECUTED')")
        cmd = discover(self.repo)[0][2]
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PY"}
        env["PATH"] = str(self.repo / "empty-bin")  # no python3 anywhere
        (self.repo / "empty-bin").mkdir()
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)

    def test_the_hook_runs_under_the_configured_interpreter_not_the_PATH_one(self):
        self._resolver_beside()
        """The finding: a registered command that execs bare `python3` fails at every event on a
        host whose PATH python is broken while SUTANDO_PY is configured. Fire the exact stored
        command in that environment."""
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]},
            hook_body="import sys; print('HOOK_EXECUTED'); sys.exit(0)")
        cmd = discover(self.repo)[0][2]
        stub_dir = self.repo / "bin"; stub_dir.mkdir()
        log = self.repo / "stub.log"
        (stub_dir / "python3").write_text(f"#!/bin/bash\necho invoked >> '{log}'\nexit 79\n")
        (stub_dir / "python3").chmod(0o755)
        env = dict(os.environ, PATH=f"{stub_dir}:{os.environ.get('PATH', '')}", SUTANDO_PY=sys.executable)
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "HOOK_EXECUTED"), r.stderr)
        self.assertFalse(log.exists(), "the broken PATH python was invoked")
        # Control: the same command with no SUTANDO_PY and no bundled interpreter falls to PATH,
        # where the stub is — which is what the finding measured at the previous head.
        env.pop("SUTANDO_PY")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 79)
        self.assertTrue(log.exists())

    def test_a_bundled_interpreter_beside_the_engine_is_the_default_runner(self):
        """Second rung of the policy: with no SUTANDO_PY (a tmux window spawned on a server that
        never had one) the command runs the bundled python, never PATH."""
        self._resolver_beside()
        bundled = self.repo.parent / "runtime" / "python" / "bin"
        bundled.mkdir(parents=True, exist_ok=True)
        (bundled / "python3").unlink(missing_ok=True)
        os.symlink(sys.executable, bundled / "python3")
        self.addCleanup(lambda: (bundled / "python3").unlink(missing_ok=True))
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]},
            hook_body="print('HOOK_EXECUTED')")
        cmd = discover(self.repo)[0][2]
        stub_dir = self.repo / "bin"; stub_dir.mkdir()
        (stub_dir / "python3").write_text("#!/bin/bash\nexit 79\n"); (stub_dir / "python3").chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PY"}
        env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "HOOK_EXECUTED"), r.stderr)

    def test_a_vanished_script_allows_the_tool_instead_of_blocking_it(self):
        """The registration outlives the file, and a hook that cannot start blocks
        the tool it gates, so an absent script must exit 0."""
        self._resolver_beside()
        d = self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]}, hook_body="import sys; sys.exit(2)")
        cmd = discover(self.repo)[0][2]
        self.assertEqual(subprocess.run(cmd, shell=True).returncode, 2, "present guard must still block")
        (d / "hooks" / "g.py").unlink()
        self.assertEqual(subprocess.run(cmd, shell=True).returncode, 0, "absent guard must fail OPEN")

    def test_a_shell_hook_execs_bash_directly_and_carries_its_legacy_shapes(self):
        """A `.sh` hook needs no interpreter policy: it runs under bash, guarded like a .py hook,
        and its legacy shapes are the runner-first and guarded-bare forms only."""
        d = self.repo / "skills" / "demo"
        (d / "hooks").mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"name": "demo", "hooks": [
            {"event": "Stop", "command": "./hooks/g.sh"}]}))
        (d / "hooks" / "g.sh").write_text("#!/bin/bash\necho SH_HOOK_EXECUTED\n")
        _event, token, cmd, legacy = discover(self.repo)[0]
        q = shlex.quote(str(self.repo.resolve() / "skills/demo/hooks/g.sh"))
        self.assertEqual(token, "g.sh")
        self.assertEqual(cmd, f"[ -f {q} ] || exit 0; exec bash {q}")
        self.assertEqual(legacy.split(LEGACY_SEP), [f"bash {q}", f"[ -f {q} ] || exit 0; exec bash {q}"])
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "SH_HOOK_EXECUTED"), r.stderr)
        (d / "hooks" / "g.sh").unlink()
        self.assertEqual(subprocess.run(cmd, shell=True).returncode, 0, "absent guard must fail OPEN")

    def test_discovery_refuses_a_command_outside_the_declaring_skill(self):
        """A manifest must not be able to point core at a host executable."""
        for cmd in ("/bin/sh", "../../../bin/sh"):
            self._skill("demo", {"name": "demo", "hooks": [
                {"event": "PreToolUse", "command": cmd}]})
            self.assertEqual(discover(self.repo), [], f"registered {cmd!r}")
            shutil.rmtree(self.repo / "skills" / "demo")

    def test_the_command_is_absolute_so_it_is_portable(self):
        """The whole point: no host-specific path is written by hand."""
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        cmd = discover(self.repo)[0][2]
        self.assertIn(str(self.repo), cmd)

    def test_a_skill_with_no_hooks_key_contributes_nothing(self):
        self._skill("demo", {"name": "demo", "tools": "./tools.ts"})
        self.assertEqual(discover(self.repo), [])

    def test_a_disabled_skill_is_skipped(self):
        self._skill("demo", {"name": "demo", "enabled": False, "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        self.assertEqual(discover(self.repo), [])

    def test_a_declared_but_ABSENT_hook_is_not_registered(self):
        """Registering a path that does not exist arms nothing and reads as armed."""
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/missing.py"}]})
        self.assertEqual(discover(self.repo), [])

    def test_a_broken_manifest_does_not_abort_discovery_of_the_others(self):
        self._skill("broken", {"name": "broken"})
        (self.repo / "skills" / "broken" / "manifest.json").write_text("{not json")
        self._skill("good", {"name": "good", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        self.assertEqual([r[1] for r in discover(self.repo)], ["g.py"])

    def test_malformed_hook_entries_are_skipped_not_raised(self):
        self._skill("demo", {"name": "demo", "hooks": [
            "a string", {"event": "PreToolUse"}, {"command": "./hooks/g.py"},
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        self.assertEqual(len(discover(self.repo)), 1)

    def test_hooks_as_a_non_list_is_skipped(self):
        self._skill("demo", {"name": "demo", "hooks": {"event": "PreToolUse"}})
        self.assertEqual(discover(self.repo), [])


class ManifestLintAcceptsAndValidatesHooks(unittest.TestCase):
    """Accepting `hooks` without validating it would be worse than rejecting it:
    discovery silently drops what it cannot find, so a bad entry reads as armed."""

    REPO = Path(__file__).resolve().parent.parent

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.skill = Path(self._td.name) / "demo"
        (self.skill / "hooks").mkdir(parents=True)
        (self.skill / "hooks" / "g.py").write_text("#!/usr/bin/env python3\n")
        self.base = {
            "name": "demo", "version": "1.0.0", "owner": "github:sonichi/sutando",
            "stability": "experimental",
        }

    @staticmethod
    def _linter():
        """In-process import: a subprocess runs uninstrumented, so a CLI-only
        test leaves the rules it exercises reading as 0% covered."""
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "scripts" / "lint-skill.py"
        spec = importlib.util.spec_from_file_location("lint_skill", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _lint(self, hooks="__omit__"):
        """(rc, text) with rc mirroring the CLI: non-zero when errors exist."""
        m = dict(self.base)
        if hooks != "__omit__":
            m["hooks"] = hooks
        (self.skill / "manifest.json").write_text(json.dumps(m))
        errors, warnings = self._linter()._lint_manifest(self.skill)
        return (1 if errors else 0), "\n".join(errors + warnings)

    def test_lint_rejects_a_command_that_resolves_outside_the_skill(self):
        """`skill_dir / "/bin/sh"` is `/bin/sh` — escaping needs no `..`."""
        for cmd in ("/bin/sh", "../../../bin/sh"):
            rc, text = self._lint([{"event": "PreToolUse", "command": cmd}])
            self.assertEqual(rc, 1, f"lint accepted {cmd!r}")
            self.assertIn("inside the skill dir", text)

    def test_the_CLI_exit_code_still_tracks_errors(self):
        """One subprocess case so the in-process shortcut cannot drift."""
        (self.skill / "manifest.json").write_text(json.dumps(
            {**self.base, "hooks": [{"event": "PreToolUse", "command": "./hooks/nope.py"}]}))
        bad = subprocess.run(
            [sys.executable, str(self.REPO / "scripts" / "lint-skill.py"), str(self.skill)],
            capture_output=True, text=True)
        (self.skill / "manifest.json").write_text(json.dumps(
            {**self.base, "hooks": [{"event": "PreToolUse", "command": "./hooks/g.py"}]}))
        good = subprocess.run(
            [sys.executable, str(self.REPO / "scripts" / "lint-skill.py"), str(self.skill)],
            capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)

    def test_CONTROL_no_hooks_key_is_clean(self):
        """Without a passing baseline the failure cases below prove nothing."""
        rc, out = self._lint()
        self.assertEqual(rc, 0, out)

    def test_CONTROL_a_valid_hook_is_clean_and_not_an_unknown_field(self):
        rc, out = self._lint([{"event": "PreToolUse", "command": "./hooks/g.py"}])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("unknown manifest field", out)

    def test_a_command_that_does_not_exist_is_an_error(self):
        rc, out = self._lint([{"event": "PreToolUse", "command": "./hooks/nope.py"}])
        self.assertNotEqual(rc, 0)
        self.assertIn("does not exist", out)

    def test_a_command_escaping_the_skill_dir_is_an_error(self):
        rc, out = self._lint([{"event": "PreToolUse", "command": "../../../etc/passwd"}])
        self.assertNotEqual(rc, 0)
        self.assertIn("must resolve inside the skill dir", out)

    def test_a_missing_event_is_an_error(self):
        rc, out = self._lint([{"command": "./hooks/g.py"}])
        self.assertNotEqual(rc, 0)
        self.assertIn("missing 'event'", out)

    def test_a_missing_command_is_an_error(self):
        rc, out = self._lint([{"event": "PreToolUse"}])
        self.assertNotEqual(rc, 0)
        self.assertIn("missing 'command'", out)

    def test_hooks_must_be_a_list(self):
        rc, out = self._lint({"event": "PreToolUse"})
        self.assertNotEqual(rc, 0)
        self.assertIn("must be a list", out)

    def test_an_entry_must_be_an_object(self):
        rc, out = self._lint(["a string"])
        self.assertNotEqual(rc, 0)
        self.assertIn("must be an object", out)


class HealthProbeReportsWhenDiscoveryBreaks(unittest.TestCase):
    def test_a_raising_discovery_warns_rather_than_aborting_every_later_probe(self):
        """The probe runs inside run_all_checks; an exception here would kill the
        checks after it, and a silent pass would verify only the static hooks."""
        import importlib.util
        repo = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("hc", repo / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(hc)
        except SystemExit:
            pass
        import skill_hooks
        with mock.patch.object(skill_hooks, "discover", side_effect=RuntimeError("boom")):
            row = hc.check_claude_hook_registration(repo_dir=repo)
        self.assertEqual(row["status"], "warn")
        self.assertIn("skill-hook discovery failed", row["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
