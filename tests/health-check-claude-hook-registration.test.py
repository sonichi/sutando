#!/usr/bin/env python3
"""claude-hooks probe: the core's launch settings are the only registration.

It must warn when the launch JSON would miss an owned or skill hook, point at a
missing script, or leave transcripts on the 30-day default; when the running core's
own launch record lacks them; and when a settings file still holds a Sutando-written
copy that fires outside the core launch.

Run: python3 tests/health-check-claude-hook-registration.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import io
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import claude_argv  # noqa: E402

BUILDER = "src/agent/claude/cli/build-core-settings.mjs"
TABLE = "src/agent/claude/cli/owned-hooks.json"
OWNED = ("check-pending-tasks.sh", "turn-start.sh", "session-handoff.sh",
         "schedule-crons-session-hint.sh", "personal-claude-compact-hint.sh",
         "watcher-rearm-session-hint.sh")
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"
WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A ps that answers with the real values but, like macOS under de_DE, formats lstart in the
# locale's %c (day first, weekday last) unless LC_ALL=C.
PS_LOCALIZED = r"""#!/bin/bash
out="$(LC_ALL=C /bin/ps "$@")"
if [ "${LC_ALL:-}" != "C" ]; then
  out="$(printf '%s\n' "$out" | sed -E 's/^ *(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2}) +([0-9]+) ([0-9:]+) ([0-9]{4})/\3. \2 \5 \4 \1/')"
fi
printf '%s\n' "$out"
"""
# The tmux the probe asks for the recorded session's panes on the recorded socket.
TMUX_PANES = r"""#!/bin/bash
# list-panes of =sutando-core on $STUB_SOCKET answers $STUB_PANE_PID; any other socket or session has no server.
sock=""; prev=""; for a in "$@"; do [ "$prev" = "-S" ] && sock="$a"; prev="$a"; done
case " $* " in *" list-panes "*" -t =sutando-core "*) ;; *) echo "can't find session" >&2; exit 1 ;; esac
[ "$sock" = "${STUB_SOCKET:-}" ] || { echo "no server running on $sock" >&2; exit 1; }
echo "${STUB_PANE_PID:-}"
"""


def _load():
    spec = importlib.util.spec_from_file_location("hc_hooks_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(hc)
    except SystemExit:
        pass
    return hc


hc = _load()


# The probe is Claude-only (#5414); pin the runtime so a codex host still exercises it.
_RUNTIME_PIN = mock.patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": "claude"})


def setUpModule():
    _RUNTIME_PIN.start()


def tearDownModule():
    _RUNTIME_PIN.stop()


@unittest.skipUnless(shutil.which("node"), "node is required to run the launch-settings builder")
class Probe(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name) / "repo"
        (self.repo / BUILDER).parent.mkdir(parents=True)
        shutil.copy(REPO / BUILDER, self.repo / BUILDER)
        shutil.copy(REPO / TABLE, self.repo / TABLE)
        for name in OWNED:
            (self.repo / "src" / name).write_text("#!/bin/bash\n")
        (self.repo / "hooks").mkdir()
        (self.repo / "hooks" / "skip-ask-user-question.py").write_text("")
        skill = self.repo / "skills" / "demo"
        (skill / "hooks").mkdir(parents=True)
        (skill / "hooks" / "g.py").write_text("")
        (skill / "manifest.json").write_text(json.dumps(
            {"name": "demo", "hooks": [{"event": "PreToolUse", "command": "./hooks/g.py"}]}))
        self.ccd = Path(self._td.name) / "ccd"
        self.ccd.mkdir()
        self.ws = Path(self._td.name) / "ws"
        # A real process the record binds to: argv as a core's, so ps reports `--name sutando-core`.
        self.core = subprocess.Popen(["/bin/bash", "-c", 'exec -a "claude --name sutando-core" sleep 300'])
        self.addCleanup(self.core.kill)
        self.start = subprocess.run(["ps", "-p", str(self.core.pid), "-o", "lstart="], env=hc._C_LOCALE_ENV(),
                                    capture_output=True, text=True).stdout.strip()
        assert self.start, "ps did not report the probe process"
        # What the launcher's record writer needs: the workspace resolver and a ps on PATH.
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "sutando-config.sh").write_text(
            '#!/bin/bash\ncase "$1" in workspace) echo "%s";; *) echo "";; esac\n' % self.ws)
        self.bin = Path(self._td.name) / "bin"
        self.bin.mkdir()
        (self.bin / "ps").write_text(PS_LOCALIZED)
        (self.bin / "ps").chmod(0o755)
        (self.bin / "tmux").write_text(TMUX_PANES)
        (self.bin / "tmux").chmod(0o755)
        self.sock = str(Path(self._td.name) / "core.sock")
        settings, why = hc._core_launch_settings(self.repo)
        assert settings is not None, why
        self.record(settings)
        self._env = mock.patch.dict(os.environ, {"SUTANDO_CLAUDE_WORKING_DIR": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def record(self, settings, **fields):
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        rec = {"ts": 1, "repo": str(self.repo), "pid": self.core.pid, "start": self.start,
               "session": "sutando-core", "socket": "", "launch": "bare", "other_core_live": False,
               "settings": settings}
        rec.update(fields)
        for key, value in list(rec.items()):
            if value is self.ABSENT:
                del rec[key]
        (self.ws / "state" / "core-launch-settings.json").write_text(json.dumps(rec))

    ABSENT = object()

    def tmux_probe_env(self, pane_pid=None, socket=None):
        """The probe resolves the stand-in tmux (one socket, one pane) and finds the stand-in ps on PATH."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}",
                                                         "STUB_SOCKET": socket if socket is not None else self.sock,
                                                         "STUB_PANE_PID": str(pane_pid if pane_pid is not None else self.core.pid)}))
        stack.enter_context(mock.patch.object(hc, "_resolve_tmux_bin", return_value=str(self.bin / "tmux")))
        return stack

    def probe(self, **kw):
        kw.setdefault("workspace_dir", self.ws)
        return hc.check_claude_hook_registration(repo_dir=self.repo, config_dir=self.ccd, **kw)

    def test_ok_when_the_launch_settings_carry_everything_and_no_file_has_a_copy(self):
        out = self.probe()
        self.assertEqual(out["status"], "ok", out["detail"])
        # guard + 7 owned + 1 skill hook
        self.assertIn("9 hooks registered by the core's launch settings only", out["detail"])

    def test_not_a_checkout(self):
        os.remove(self.repo / BUILDER)
        self.assertIn("not a sutando checkout", self.probe()["detail"])

    def test_node_missing_warns(self):
        with mock.patch.object(hc.shutil, "which", return_value=None):
            out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("node not found", out["detail"])

    def test_a_failing_builder_warns_with_its_exit(self):
        (self.repo / BUILDER).write_text("process.stderr.write('boom'); process.exit(4);\n")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("exited 4", out["detail"])

    def test_a_builder_that_cannot_run_warns(self):
        with mock.patch.object(hc.subprocess, "run", side_effect=OSError("no exec")):
            out = self.probe()
        self.assertIn("build-core-settings.mjs failed", out["detail"])

    def test_a_raising_discovery_warns(self):
        import skill_hooks
        with mock.patch.object(skill_hooks, "discover", side_effect=RuntimeError("boom")):
            out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("skill-hook discovery failed", out["detail"])

    def test_a_builder_missing_events_skill_hooks_and_retention_warns_for_each(self):
        (self.repo / BUILDER).write_text(
            'process.stdout.write(JSON.stringify({hooks: {Stop: [{hooks: [{type: "command", command: "true"}]}]}}));\n')
        detail = self.probe()["detail"]
        self.assertIn("launch settings lack owned hooks: ", detail)
        self.assertIn("+3 more", detail)
        self.assertIn("skill hooks missing from launch settings: PreToolUse:", detail)
        self.assertIn("cleanupPeriodDays", detail)

    def test_one_unrelated_command_per_event_is_not_the_owned_set(self):
        echoes = {e: [{"matcher": "", "hooks": [{"type": "command", "command": "echo x"}]}]
                  for e in ("Stop", "UserPromptSubmit", "PreCompact", "SessionEnd", "SessionStart")}
        (self.repo / BUILDER).write_text(
            f"process.stdout.write(JSON.stringify({{hooks: {json.dumps(echoes)}, cleanupPeriodDays: 3650}}));\n")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("launch settings lack owned hooks", out["detail"])

    def test_no_launch_record_means_the_running_core_is_not_proven(self):
        (self.ws / "state" / "core-launch-settings.json").unlink()
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("no launch record for the running core", out["detail"])
        self.assertFalse(out["_live_core_has_launch_hooks"])

    def test_a_record_from_an_older_launch_is_not_the_builders_output(self):
        settings, _ = hc._core_launch_settings(self.repo)
        settings["hooks"].pop("SessionStart")
        self.record(settings)
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("the running core was launched without", out["detail"])
        self.assertFalse(out["_live_core_has_launch_hooks"])

    def _bound_record_fails(self, **fields):
        settings, _ = hc._core_launch_settings(self.repo)
        self.record(settings, **fields)
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertFalse(out["_live_core_has_launch_hooks"], out["detail"])
        return out["detail"]

    def test_a_record_for_a_process_that_is_gone_is_stale(self):
        self.core.kill()
        self.core.wait()
        self.assertIn("is not running — the launch record is stale", self._bound_record_fails())

    def test_a_record_whose_start_time_differs_is_stale(self):
        self.assertIn("is not the core the launch record describes", self._bound_record_fails(start="Thu Jan  1 00:00:00 2026"))

    def test_a_record_naming_another_session_is_stale(self):
        self.assertIn("is not the core the launch record describes", self._bound_record_fails(session="sutando-other"))

    def test_a_core_whose_name_only_starts_with_the_session_is_not_that_session(self):
        self.core.kill()
        self.core.wait()
        self.core = subprocess.Popen(["/bin/bash", "-c", 'exec -a "claude --name sutando-core-2" sleep 300'])
        self.addCleanup(self.core.kill)
        self.start = subprocess.run(["ps", "-p", str(self.core.pid), "-o", "lstart="], env=hc._C_LOCALE_ENV(),
                                    capture_output=True, text=True).stdout.strip()
        self.assertIn("is not the core the launch record describes", self._bound_record_fails(session="sutando-core"))
        # The probe reads the name through the one shared owner; pin the policy there.
        self.assertIs(hc.claude_argv, claude_argv)
        self.assertFalse(claude_argv.names_session("claude --name sutando-core-2", "sutando-core"))
        self.assertFalse(claude_argv.names_session("claude --name sutando-core-watcher", "sutando-core"))
        self.assertTrue(claude_argv.names_session("claude --name=sutando-core --x", "sutando-core"))
        self.assertEqual(claude_argv.session_name("claude --name 'sutando core'"), "sutando core")
        self.assertEqual(claude_argv.session_name("claude --resume"), "")
        # The boot prompt follows `--`; nothing after it names a session, and the first --name wins.
        for args in ("claude --resume abc -- --name sutando-core", "claude -- --name=sutando-core",
                     "claude --name other --name sutando-core", "claude --name", "claude --name=", "claude --name --"):
            self.assertFalse(claude_argv.names_session(args, "sutando-core"), args)
        self.assertEqual(claude_argv.session_name("claude --name sutando-core -- --name other"), "sutando-core")
        self.assertEqual(claude_argv.session_name('claude --name sutando-core -- "say --name x"'), "sutando-core")

    def test_a_record_without_a_session_or_launch_mode_is_not_proof(self):
        for fields in ({"session": self.ABSENT}, {"session": ""}, {"launch": self.ABSENT}, {"launch": "screen"},
                       {"pid": True}):
            with self.subTest(fields=fields):
                self.assertIn("not bound to a process", self._bound_record_fails(**fields))

    def test_a_tmux_record_binds_only_through_a_pane_of_its_session_on_its_socket(self):
        settings, _ = hc._core_launch_settings(self.repo)
        for pane in (self.core.pid, os.getpid()):
            with self.subTest(pane=pane):
                self.record(settings, launch="tmux", socket=self.sock)
                with self.tmux_probe_env(pane_pid=pane):
                    rec = json.loads((self.ws / "state" / "core-launch-settings.json").read_text())
                    self.assertEqual(hc._launch_record_binds_live_core(rec), "")
                    self.assertEqual(self.probe()["status"], "ok")
        for fields, why in (({"socket": str(Path(self._td.name) / "other.sock")}, "no pane of session"),
                            ({"socket": ""}, "names no tmux socket"), ({"socket": None}, "names no tmux socket"),
                            ({"socket": self.ABSENT}, "names no tmux socket")):
            with self.subTest(fields=fields), self.tmux_probe_env():
                self.assertIn(why, self._bound_record_fails(launch="tmux", **fields))
        with self.tmux_probe_env(pane_pid=1):
            self.assertIn("is not in a pane of session", self._bound_record_fails(launch="tmux", socket=self.sock))
        # Without any tmux on PATH a tmux record is not proof either.
        self.assertIn("the record is", self._bound_record_fails(launch="tmux", socket=self.sock))

    def test_the_shared_name_reader_cli_and_its_malformed_quote_fallback(self):
        cli = [sys.executable, str(REPO / "src" / "claude_argv.py")]
        env = hermetic_env(PATH="/usr/bin:/bin")
        run = lambda *a: subprocess.run([*cli, *a], env=env, capture_output=True, text=True, timeout=30)  # noqa: E731
        self.assertEqual(run("names-session", "claude --name sutando-core --x", "sutando-core").returncode, 0)
        self.assertEqual(run("names-session", "claude --name sutando-core-2", "sutando-core").returncode, 1)
        # An apostrophe in the argv (a boot prompt's) breaks shlex; the whitespace fallback still reads the name.
        malformed = "claude --name sutando-core -- don't stop"
        self.assertEqual(run("names-session", malformed, "sutando-core").returncode, 0)
        self.assertEqual(run("names-session", "claude -- --name sutando-core", "sutando-core").returncode, 1)
        self.assertEqual(claude_argv.session_name(malformed), "sutando-core")
        self.assertEqual(run("session-name", "claude --name=sutando-core").stdout.strip(), "sutando-core")
        self.assertEqual(run("session-name").returncode, 2)
        # The same entry points in-process, where coverage measures them.
        self.assertEqual(claude_argv.main(["names-session", "claude --name sutando-core", "sutando-core"]), 0)
        self.assertEqual(claude_argv.main(["names-session", "claude --name sutando-core-2", "sutando-core"]), 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(claude_argv.main(["session-name", "claude --name=sutando-core"]), 0)
        self.assertEqual(buf.getvalue().strip(), "sutando-core")
        self.assertEqual(claude_argv.main(["bogus"]), 2)
        self.assertFalse(claude_argv.names_session("claude --name sutando-core", ""))

    def test_a_tmux_that_cannot_run_or_a_ps_that_fails_mid_walk_is_not_proof(self):
        settings, _ = hc._core_launch_settings(self.repo)
        self.record(settings, launch="tmux", socket=self.sock)
        rec = json.loads((self.ws / "state" / "core-launch-settings.json").read_text())
        real_run = hc.subprocess.run

        def tmux_cannot_run(argv, **kw):
            if str(argv[0]).endswith("tmux"):
                raise OSError("tmux cannot run")
            return real_run(argv, **kw)

        def ps_walk_fails(argv, **kw):
            if argv[:3] == ["ps", "-o", "ppid="]:
                raise OSError("ps is gone")
            return real_run(argv, **kw)
        with self.tmux_probe_env(), mock.patch.object(hc.subprocess, "run", side_effect=tmux_cannot_run):
            self.assertIn("cannot list the panes", hc._launch_record_binds_live_core(rec))
            self.assertEqual(self.probe()["status"], "warn")
        with self.tmux_probe_env(pane_pid=os.getpid()), mock.patch.object(hc.subprocess, "run", side_effect=ps_walk_fails):
            self.assertIn("is not in a pane of session", hc._pid_outside_session_pane(self.core.pid, self.sock, "sutando-core"))

    def test_an_unreadable_owned_hooks_table_warns(self):
        import claude_hooks_settings as chs
        with mock.patch.object(chs, "owned_launch_records", side_effect=RuntimeError("table gone")):
            out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("owned-hooks table unreadable (table gone)", out["detail"])

    def test_a_bare_record_needs_no_tmux_and_still_needs_the_exact_start(self):
        self.assertEqual(self.probe()["status"], "ok")
        self.assertIn("is not the core the launch record describes",
                      self._bound_record_fails(start=self.start.replace(self.start[-4:], "1999")))
        # A prefix of the real start (the year cut off) is not the start: the comparison is exact.
        self.assertIn("is not the core the launch record describes", self._bound_record_fails(start=self.start[:-5]))
        self.assertIn("is not the core the launch record describes", self._bound_record_fails(start=self.start + " "))

    def _launcher_writes_record(self, locale):
        """record_core_launch_settings from session-launch.sh, run as the launcher would under `locale`."""
        settings, _ = hc._core_launch_settings(self.repo)
        script = ('REPO="$1"; SESSION=sutando-core; TMUX_SOCKET=/tmp/t.sock; PY="$2"; SETTINGS_ARGS=(--settings "$3"); '
                  '. "$4"; record_core_launch_settings "$5" bare')
        env = hermetic_env(PATH=f"{self.bin}:/usr/bin:/bin", LC_ALL=locale)
        run = subprocess.run(["/bin/bash", "-c", script, "_", str(self.repo), sys.executable, json.dumps(settings),
                              str(LAUNCHER), str(self.core.pid)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads((self.ws / "state" / "core-launch-settings.json").read_text())

    def test_the_record_binds_across_locales_because_both_sides_pin_ps_to_c(self):
        # The stand-in ps really localizes: unpinned, lstart does not start with an English weekday.
        localized = subprocess.run(["ps", "-o", "lstart=", "-p", str(self.core.pid)],
                                   env=hermetic_env(PATH=f"{self.bin}:/usr/bin:/bin", LC_ALL="de_DE.UTF-8"),
                                   capture_output=True, text=True).stdout.strip()
        self.assertTrue(localized and not localized.startswith(WEEKDAY), localized)
        for writer, reader in (("de_DE.UTF-8", "en_US.UTF-8"), ("en_US.UTF-8", "de_DE.UTF-8")):
            with self.subTest(writer=writer, reader=reader):
                rec = self._launcher_writes_record(writer)
                self.assertEqual((rec["pid"], rec["session"], rec["start"], rec["launch"]),
                                 (self.core.pid, "sutando-core", self.start, "bare"))
                self.assertTrue(rec["start"].startswith(WEEKDAY), rec["start"])
                with mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}", "LC_ALL": reader}):
                    self.assertEqual(hc._launch_record_binds_live_core(rec), "")
                    out = self.probe()
                self.assertEqual(out["status"], "ok", out["detail"])

    def test_a_record_without_a_pid_is_not_proof(self):
        settings, _ = hc._core_launch_settings(self.repo)
        (self.ws / "state" / "core-launch-settings.json").write_text(
            json.dumps({"ts": 1, "repo": str(self.repo), "settings": settings}))
        out = self.probe()
        self.assertIn("not bound to a process", out["detail"])
        self.assertFalse(out["_live_core_has_launch_hooks"])

    def test_a_core_launched_beside_an_outside_socket_core_does_not_authorize_the_sweep(self):
        self.assertIn("another same-name core was live outside the socket", self._bound_record_fails(other_core_live=True))

    def test_an_owned_script_that_is_gone_warns(self):
        os.remove(self.repo / "src" / "turn-start.sh")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn(f"missing scripts: {self.repo / 'src' / 'turn-start.sh'}", out["detail"])

    def test_copies_in_the_project_file_and_config_dir_are_reported_for_the_fix(self):
        proj = self.repo / ".claude" / "settings.json"
        proj.parent.mkdir()
        proj.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"bash '{self.repo}/src/check-pending-tasks.sh'"},
            {"type": "command", "command": "echo operator"}]}]}}))
        (self.ccd / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": f'bash "{self.repo}/src/session-handoff.sh" "${{TRANSCRIPT_PATH:-}}"'}]}]}}))
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 Sutando hook copies still in a settings file", out["detail"])
        self.assertTrue(out["_live_core_has_launch_hooks"])
        self.assertEqual(len(out["_project_leftovers"]), 2)
        self.assertNotIn("echo operator", " ".join(out["_project_leftovers"]))
        # Probing is read-only.
        self.assertIn("check-pending-tasks.sh", proj.read_text())

    def test_one_copy_is_singular(self):
        (self.ccd / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"bash '{self.repo}/src/check-pending-tasks.sh'"}]}]}}))
        self.assertIn("1 Sutando hook copy still", self.probe()["detail"])

    def test_an_unreadable_settings_file_warns_instead_of_raising(self):
        (self.ccd / "settings.json").write_text("{nope")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("could not read a settings file", out["detail"])

    def test_the_default_config_dir_comes_from_the_shared_resolver(self):
        ws = Path(self._td.name) / "ws"
        (ws / ".claude-sutando").mkdir(parents=True)
        (ws / ".claude-sutando" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"bash '{self.repo}/src/check-pending-tasks.sh'"}]}]}}))
        with mock.patch.dict(os.environ, {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws)}):
            out = hc.check_claude_hook_registration(repo_dir=self.repo, workspace_dir=self.ws)
        self.assertEqual(len(out["_project_leftovers"]), 1, out["detail"])


@unittest.skipUnless(shutil.which("node"), "node is required to run the launch-settings builder")
class LiveTree(unittest.TestCase):
    def test_this_checkout_builds_launch_settings_with_every_owned_hook(self):
        settings, why = hc._core_launch_settings(REPO)
        self.assertIsNotNone(settings, why)
        commands = " ".join(h["command"] for gs in settings["hooks"].values() for g in gs for h in g["hooks"])
        for name in OWNED:
            self.assertIn(f"/src/{name}", commands)
        self.assertEqual(settings["cleanupPeriodDays"], 3650)


class RuntimeGate(Probe):
    """#5414: a Codex core never runs Claude hooks, so their absence is not a defect."""

    def _run(self, runtime):
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": runtime}):
            return self.probe()

    def test_codex_runtime_does_not_warn_about_missing_claude_hooks(self):
        (self.ws / "state" / "core-launch-settings.json").unlink()
        out = self._run("codex")
        self.assertEqual(out["status"], "ok", out["detail"])
        self.assertIn("not applicable", out["detail"])
        self.assertNotIn("_project_leftovers", out, "--fix must not sweep under codex")
        self.assertNotIn("_live_core_has_launch_hooks", out)

    def test_control_claude_runtime_still_warns_on_the_same_repo(self):
        (self.ws / "state" / "core-launch-settings.json").unlink()
        out = self._run("claude")
        self.assertEqual(out["status"], "warn")
        self.assertIn("no launch record for the running core", out["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
