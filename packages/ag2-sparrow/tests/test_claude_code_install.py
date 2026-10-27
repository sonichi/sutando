#!/usr/bin/env python3
"""claude-local install and uninstall: the unit, the root and its holder, the operation lock, and
the source rules. Shared fixtures: claude_code_fixtures.py."""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import (  # noqa: E402
    ADAPTER, alive, cli, cm, ConnectBase, covered_code, hermetic_env, hmac_policy, hold, HOLD_DRIVER, hold_mod,
    holder_lock, inbox, L, launcher, NONCE, NOT_LOADED, OP_HOLDER, oplock_free, PKG, REAL_SOCKET_PROBLEM,
    REAL_TMUX_VERSION, records, REGISTER, Runner, SERVICE_TARGET, SESSION_ID, SHOW, SHOWING, sm, stamp, SUITE_FILES,
    task_io, task_io_lock_holder, TOKEN, unhold, UNPUBLISHED, verify_py, wait_for, wait_gone,
)


class ConnectRender(ConnectBase):
    def test_unit_and_plist_carry_the_marker_and_paths_only(self):
        exec_start = str(self.bin / cli.COMMAND)
        unit = cm.render_service(L, exec_start)
        self.assertEqual(unit.split("\n")[0], cli.MARKER)
        self.assertIn(f"ExecStart={exec_start} run\n", unit)
        for line in ("Restart=always", "RestartSec=10", "RestartPreventExitStatus=75", "WantedBy=default.target",
                     "KillMode=process", "TimeoutStopSec=180", f"Environment={cm.MANAGED_VAR}=1"):
            self.assertIn(line + "\n", unit)
        self.assertNotIn("control-group", unit, "systemd signals only the launcher; the tmux session survives it")
        plist = cm.render_plist(L, exec_start, "/opt/bin:/usr/bin", "/var/log/x.log")
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER), plist[:200])
        self.assertTrue(plist.startswith("<?xml"), "the declaration stays first")
        body = plistlib.loads(plist.encode())
        self.assertEqual(body["ProgramArguments"], [exec_start, "run", "--launchd"], "the entry shim maps the exit")
        self.assertEqual((body["Label"], body["KeepAlive"], body["RunAtLoad"]), (cli.AGENT_LABEL, {"SuccessfulExit": False}, True))
        self.assertNotIn("PathState", plist, "no held marker; SuccessfulExit alone")
        self.assertEqual(body["EnvironmentVariables"], {"PATH": "/opt/bin:/usr/bin", cm.MANAGED_VAR: "1"})
        self.assertEqual((body["AbandonProcessGroup"], body["ExitTimeOut"], body["ThrottleInterval"]), (False, 180, 30),
                         "outer stop 180 s over the 120 s inner bound; the throttle stays 30")
        generic = cm.Shape(name="x", command="x", description="", cli="", min_version=(1,), version_anchor="", version_note="",
                           required_keys=(), config_keys=(), env_keep=(), kept="")
        self.assertIn("KillMode=control-group\nTimeoutStopSec=30\n", cm.render_service(generic, "/x"), "the default shape is unchanged")
        self.assertEqual(plistlib.loads(cm.render_plist(generic, "/x", "/b", "/l").encode())["KeepAlive"], True)
        for text in (unit, plist, cli.render_config({"CLAUDE": "/x/claude", "INBOX_DIR": "/i"})):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("REMOTE_TASK_TOKEN", text)
        self.assertEqual([ln for ln in unit.splitlines() if ln.startswith("Environment=")], [f"Environment={cm.MANAGED_VAR}=1"],
                         "the unit carries paths in ExecStart and the one marker, no values")
        bridge = cm.render_service(cli.BRIDGE, "/x/ag2-sparrow", {"B": 'p"q%r\\s', "A": "a b"})
        self.assertIn("ExecStart=/x/ag2-sparrow\n", bridge, "no arguments")
        self.assertIn('Environment="A=a b"\nEnvironment="B=p\\"q%%r\\\\s"\n', bridge,
                      "sorted, quoted, with systemd's escapes and specifiers neutralized")
        self.assertTrue(bridge.startswith(f"# Written by {cli.COMMAND} install;"), "the marker names who wrote it")
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER), plist[:200])
        self.assertTrue(cm.marked("<?xml?>\n" + cli.MARKER + "\nx", cli.MARKER), "a marker on the second line counts")
        self.assertFalse(cm.marked("a\nb\n" + cli.MARKER, cli.MARKER), "the marker is a leading line")


class ConnectInstall(ConnectBase):
    """Install is a quiescent replacement: stop, prove absent and free, write, start, require a fresh
    holder of this shape; anything less fails closed with the files as written, and the retry is the
    same install."""

    ACQUIRED = "the managed launcher acquired the expected root"

    def test_a_fresh_systemd_install_writes_starts_and_requires_a_fresh_holder(self):
        runner = self.runner()
        rc, out, err = self.install_with(runner)
        self.assertEqual(rc, 0, err)
        cfg_path = cli.config_path()
        self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
        cfg = cli.read_config(cfg_path)
        self.assertEqual((cfg["CLAUDE"], cfg["PLUGIN_DIR"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"], cfg["ROOT"]),
                         (str(self.claude), str(self.plugin), str(self.inbox), str(self.results), str(self.work)))
        self.assertEqual((cfg["PERMISSION_MODE"], cfg["HMAC_STRICT"], cfg["ALLOW_NON_OWNER"]), ("bypassPermissions", "1", ""),
                         "a key file alone is strict; non-owner tasks are off")
        self.assertTrue(cli.SESSION_ID_RE.match(cfg["SESSION_ID"]), "install mints the session id")
        self.assertEqual(cfg["HITL_STORE"], str(cli.hitl_store_root()), "the bridge's claude-local root is persisted")
        self.assertTrue(self.inbox.is_dir() and self.results.is_dir())
        unit = cli.service_path().read_text()
        self.assertTrue(unit.startswith(cli.MARKER))
        self.assertNotIn(TOKEN, unit + cfg_path.read_text())
        self.assertEqual(runner.verbs("systemctl"), ["show", "show", "show-environment", "daemon-reload", "enable", "restart",
                                                    "show", "show"],
                         "absent and free: no stop, then register and start")
        self.assertIn(f"{self.ACQUIRED}: {self.work} (hold pid {runner.pid()}, no live claude attached)", out,
                      "named as verify names it: hold, then its claude once attached")
        self.assertIn("linger: enabled for owner", out)
        self.assertIsNone(self.session(), "install starts no session itself; the manager does")

    def test_a_reinstall_stops_proves_free_then_replaces_and_starts(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        old = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner, False, "--permission-mode", "default")
        self.assertEqual(rc, 0)
        verbs = runner.verbs("systemctl")
        self.assertEqual(verbs[:4], ["show", "stop", "disable", "show"], "the job is stopped and proven inactive+disabled "
                         "before anything is written")
        self.assertEqual(verbs[-5:], ["daemon-reload", "enable", "restart", "show", "show"], "the shows read MainPID")
        self.assertIn("changed: ", out)
        self.assertEqual(cli.read_config(cli.config_path())["PERMISSION_MODE"], "default")
        self.assertNotEqual(runner.pid(), old, "a fresh holder, not the old session")
        self.assertIn(f"hold pid {runner.pid()}", out)

    def test_the_session_id_is_kept_across_reinstalls_and_rotated_by_new_session_only_over_a_dead_generation(self):
        """--new-session is quiescent: the id rotates only once the old generation is proven gone."""
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        first = cli.read_config(cli.config_path())["SESSION_ID"]
        self.assertEqual(self.install_with(runner)[0], 0)
        self.assertEqual(cli.read_config(cli.config_path())["SESSION_ID"], first, "kept without --new-session")
        live = {"server_pid": os.getpid(), "server_start": holder_lock.start_time(os.getpid()), "session_id": "$1",
                "session_created": 1, "pane_id": "%0", "pane_pid": 1, "start_command": "x", "nonce": "0" * 32}
        self.assertEqual(inbox.prepare_root(str(self.work)), "", "a launcher prepares the root before its record")
        records.save_supervisor(str(self.work), dict(records.empty_supervisor(), generation=live))
        rc, out, _ = self.install_with(runner, False, "--new-session")
        self.assertEqual(rc, 0)
        self.assertIn(f"kept the session id {first}: the old session (tmux server pid {os.getpid()}) is not proven gone", out)
        self.assertEqual(cli.read_config(cli.config_path())["SESSION_ID"], first)
        dead = subprocess.Popen(["sleep", "0"]); dead.wait()
        records.save_supervisor(str(self.work), dict(records.empty_supervisor(), generation=dict(live, server_pid=dead.pid,
                                                                                                 server_start="never")))
        rc, out, _ = self.install_with(runner, False, "--new-session")
        self.assertEqual(rc, 0)
        self.assertIn("rotating the session id: the old generation is proven gone", out)
        second = cli.read_config(cli.config_path())["SESSION_ID"]
        self.assertNotEqual(second, first)
        self.assertTrue(cli.SESSION_ID_RE.match(second))

    def test_an_identical_retry_performs_the_whole_apply_again(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        old = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner)
        self.assertEqual(rc, 0)
        self.assertIn("stop", runner.verbs("systemctl"))
        self.assertIn("restart", runner.verbs("systemctl"))
        self.assertNotEqual(runner.pid(), old, "no unchanged shortcut: the old session is replaced")
        self.assertIn("rewritten: ", out)
        self.assertIn(self.ACQUIRED, out)

    def test_install_on_macos_bootstraps_and_a_reinstall_boots_out_with_wait_first(self):
        runner = self.runner()
        rc, out, err = self.install_with(runner, True, "--permission-mode", "default")
        self.assertEqual(rc, 0, err)
        plist = cli.plist_path().read_text()
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER))
        self.assertEqual(plistlib.loads(plist.encode())["ProgramArguments"][1:], ["run", "--launchd"])
        self.assertNotIn(TOKEN, plist)
        self.assertEqual(cli.read_config(cli.config_path())["PERMISSION_MODE"], "default")
        self.assertEqual(runner.verbs("launchctl"), ["print", "print", "bootstrap", "print", "print"],
                         "absent: no bootout before the bootstrap; the last print reads the job's pid")
        self.assertIn("loaded: ai.ag2.sparrow.claude-local", out)
        self.assertFalse(cli.service_path().exists())
        first = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner, True, "--permission-mode", "plan")
        self.assertEqual(rc, 0)
        self.assertEqual(runner.verbs("launchctl"), ["print", "bootout", "print", "print", "bootstrap", "print", "print"])
        self.assertEqual(next(c for c in runner.calls if c[1] == "bootout"), ["launchctl", "bootout", "--wait", SERVICE_TARGET],
                         "the single service target, the only form --wait applies to")
        self.assertEqual(runner.calls[0], ["launchctl", "print", SERVICE_TARGET])
        self.assertNotEqual(runner.pid(), first)
        self.assertIn(f"hold pid {runner.pid()}", out)

    def test_an_unconfirmed_start_fails_closed_keeps_the_files_and_the_same_install_retries(self):
        silent = self.runner(root=False)                      # the manager accepts, no launcher takes the lock
        rc, out, err = self.install_with(silent)
        self.assertEqual(rc, 1)
        self.assertIn(f"the managed launcher did not acquire {self.work} within 2 s (free)", err)
        self.assertIn(f"run {cli.COMMAND} uninstall, then {cli.COMMAND} install", err)
        self.assertTrue(cli.config_path().exists() and cli.service_path().exists(), "as written; nothing rolled back")
        self.assertNotIn(self.ACQUIRED, out)
        rc, out, _ = self.install_with(self.runner())
        self.assertEqual(rc, 0, "the same install again is the retry")
        self.assertIn(self.ACQUIRED, out)

    def test_confirmation_is_a_fresh_claude_local_holder_and_never_claims_the_session_is_alive(self):
        other = self.runner(adapter="stand-in")
        rc, out, err = self.install_with(other)
        self.assertEqual(rc, 1, "a held root is not enough: the holder must be this shape's launcher")
        self.assertIn(f"did not acquire {self.work}", err)
        self.assertRegex(err, r"held by launcher pid \d+\)")
        self.assertIn("the job this operation started, was stopped", err)
        other.stop_session()
        rc, out, _ = self.install_with(self.runner())
        self.assertEqual(rc, 0)
        line = next(ln for ln in out.splitlines() if self.ACQUIRED in ln)
        for word in ("alive", "session is running", "came up", "ready"):
            self.assertNotIn(word, line, "the lock is occupancy, the record attribution; readiness is the task's")

    def test_confirmation_requires_this_installs_identity_too(self):
        other = self.runner(install="/elsewhere/site-packages/ag2_sparrow")
        rc, _, err = self.install_with(other)
        self.assertEqual(rc, 1, "same adapter, another install: not this manager's launcher")
        self.assertIn(f"did not acquire {self.work}", err)
        self.assertRegex(err, r"held by launcher pid \d+\)", "held all along, by the wrong install")

    def test_fresh_holder_refuses_the_snapshotted_holder_and_takes_a_different_one(self):
        self.configure()
        first = hold(str(self.work), "claude-local")
        try:
            mine = task_io_lock_holder(str(self.work))
            with mock.patch.object(cm, "CONFIRM_TIMEOUT_S", 0.5):
                self.assertIsNone(cm._fresh_holder(L, str(self.work), mine), "the pre-apply holder itself is not fresh")
                self.assertEqual(cm._fresh_holder(L, str(self.work), {})["pid"], first.pid, "with no snapshot it is")
        finally:
            unhold(first)
        second = hold(str(self.work), "claude-local")
        try:
            self.assertEqual(cm._fresh_holder(L, str(self.work), mine)["pid"], second.pid, "a different live holder is")
        finally:
            unhold(second)

    def test_a_failed_start_fails_closed_with_the_same_line(self):
        for table in ({("systemctl", "restart"): 1}, {("systemctl", "daemon-reload"): 1}, {("systemctl", "enable"): 1},
                      {("systemctl", "show-environment"): None}):
            rc, _, err = self.install_with(self.runner(table))
            self.assertEqual(rc, 1, table)
            self.assertIn(f"the managed launcher did not acquire {self.work}", err)
            self.assertIn("uninstall, then", err)
        rc, _, err = self.install_with(self.runner({("launchctl", "bootstrap"): 1}), True)
        self.assertEqual((rc, "bootstrap failed" in err), (1, True))

    def test_a_stop_that_is_not_confirmed_applies_nothing(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        before = cli.config_path().read_text()
        for table in ({("systemctl", "stop"): 0, ("systemctl", "show"): SHOW("loaded", "active", "enabled")},
                      {("systemctl", "show"): (1, "", "Failed to connect to bus\n")},
                      {("systemctl", "stop"): None}):
            rc, _, err = self.install_with(self.runner(table), False, "--permission-mode", "plan")
            self.assertEqual(rc, 1, table)
            self.assertIn("nothing was applied", err)
            self.assertEqual(cli.config_path().read_text(), before, "the old config stands")
        for table in ({("systemctl", "show"): (1, "", "Failed to connect to bus\n")}, {("systemctl", "show"): None}):
            rc, _, err = self.install_with(self.runner(table), False, "--permission-mode", "plan")
            self.assertEqual((rc, "is unknown; nothing was applied" in err), (1, True), table)

    def test_install_refuses_an_old_cli_before_writing_anything(self):
        (self.home / "claude-version").write_text("2.1.200")
        rc, out, err = self.install()
        self.assertEqual(rc, 2)
        self.assertIn("claude 2.1.200 is older than the 2.1.287 this adapter needs", err)
        self.assertFalse(cli.config_path().exists() or cli.service_path().exists())
        self.assertIsNone(self.session())

    def test_install_refuses_an_old_or_missing_tmux_and_an_over_long_socket_path(self):
        """tmux >= 3.2 for new-session -e; a socket path over 100 bytes fails tmux outright."""
        with mock.patch.object(launcher, "tmux_version", return_value=(3, 1)):
            rc, _, err = self.install()
        self.assertEqual((rc, "tmux 3.1 is older than the 3.2" in err), (2, True), err)
        with mock.patch.object(launcher, "tmux_version", return_value=None):
            rc, _, err = self.install()
        self.assertEqual((rc, "tmux -V did not answer" in err), (2, True))
        long_dir = "/" + "d" * 90
        self.assertIn("over the 100 the platform allows", REAL_SOCKET_PROBLEM(long_dir))
        self.assertEqual(REAL_SOCKET_PROBLEM("/tmp/cl"), "")
        with mock.patch.object(launcher, "socket_problem", return_value=REAL_SOCKET_PROBLEM(long_dir)):
            rc, _, err = self.install()
        self.assertEqual((rc, "use a shorter config dir" in err), (2, True))
        self.assertFalse(cli.config_path().exists())
        self.assertEqual(REAL_TMUX_VERSION(run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "tmux 3.6b\n", "")), (3, 6))
        self.assertIsNone(REAL_TMUX_VERSION(run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")))

    def test_ownership_refusals_are_final_and_name_the_file_to_remove_by_hand(self):
        cli.service_path().parent.mkdir(parents=True)
        cli.service_path().write_text("[Unit]\nDescription=mine\n")
        rc, out, err = self.install_with(self.runner())
        self.assertEqual(rc, 1)
        self.assertEqual(cli.service_path().read_text(), "[Unit]\nDescription=mine\n")
        self.assertFalse(cli.config_path().exists(), "nothing written before the refusal")
        self.assertIn(f"{cli.service_path()} was not written by install; remove it by hand (rm {cli.service_path()})", err)
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir", str(self.plugin), "--force"])
        self.assertEqual(e.exception.code, 2, "no --force on install")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["uninstall", "--force"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir", str(self.plugin), "--no-service"])

    def test_the_version_pin(self):
        self.assertEqual(cli.parse_version("2.1.296 (Claude Code)"), (2, 1, 296))
        self.assertIsNone(cli.parse_version("nope"))
        self.assertEqual(cli.parse_version("Update available: 2.1.310\n2.1.200 (Claude Code)\n"), (2, 1, 200),
                         "the CLI's own line, not an update notice")
        self.assertIsNone(cli.parse_version("2.1.310\n"), "no (Claude Code) line, no version")
        (self.home / "claude-version-banner").write_text("Update available: 2.1.310\n2.1.200 (Claude Code)")
        self.assertIn("claude 2.1.200 is older", cli.version_problem(str(self.claude)))
        (self.home / "claude-version-banner").unlink()
        (self.home / "claude-version-rc").write_text("1")
        self.assertIn("did not answer", cli.version_problem(str(self.claude)), "a failing --version is no version")
        (self.home / "claude-version-rc").unlink()
        self.assertEqual(cli.version_problem(str(self.claude)), "")
        (self.home / "claude-version").write_text("2.1.286")
        self.assertIn("older than the 2.1.287", cli.version_problem(str(self.claude)))
        (self.home / "claude-version").write_text("3.0.0")
        self.assertEqual(cli.version_problem(str(self.claude)), "")
        self.assertIn("did not answer", cli.version_problem(str(self.home / "no-such-claude")))


class ConnectRoot(ConnectBase):
    """The canonical root is persisted at install from --work or the lexical inbox parent, never re-derived;
    a root change is refused; the child runs under the held root inode."""

    def test_the_root_comes_from_work_or_the_lexical_inbox_parent_and_the_inbox_must_be_tasks(self):
        rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.home / "queue"))
        self.assertEqual(rc, 2)
        self.assertIn("the inbox must be <work>/tasks", err)
        self.assertFalse(cli.config_path().exists())
        runner = self.runner()
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--work", str(self.work))
        self.assertEqual(rc, 0, err)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual((cfg["ROOT"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"]),
                         (str(self.work), str(self.inbox), str(self.results)))
        self.assertEqual(cm.work_root(cfg), str(self.work))
        self.assertEqual(cm.canonical_root("", str(self.home / "w" / "sub" / ".." / "tasks")), (str(self.home / "w"), ""))

    def test_a_root_with_a_line_break_is_refused_at_install_before_any_write(self):
        for name in ("w\nx", "w\rx"):
            odd = self.home / name
            (odd / "tasks").mkdir(parents=True)
            for flags in (("--work", str(odd)), ("--inbox", str(odd / "tasks"))):
                with mock.patch.object(sm, "_run", side_effect=self.runner()) as run:
                    rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), *flags)
                self.assertEqual((rc, "line break" in err), (2, True), (flags, err))
                self.assertFalse(cli.config_path().exists(), "nothing written")
                self.assertFalse(cm.service_path(L).exists())
                self.assertEqual(run.call_count, 0, "no manager call")

    def test_a_symlinked_inbox_locks_the_persisted_root_which_a_sibling_shares(self):
        real = self.home / "elsewhere"
        (real / "tasks").mkdir(parents=True)
        link = self.work
        link.symlink_to(real)
        runner = Runner(root=str(real))                        # the stand-in locks the real root, as the launcher will
        self.runners.append(runner)
        rc, _, err = self.install_with(runner, False, "--inbox", str(link / "tasks"))
        self.assertEqual(rc, 0, err)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual(cfg["ROOT"], str(real), "the lexical parent, canonicalized once at install")
        self.assertEqual(cfg["INBOX_DIR"], str(link / "tasks"), "the inbox path itself is kept as given")
        self.runners[-1].stop_session()
        holder = hold(str(real), "codex")                      # a sibling adapter on the same root
        try:
            rc, _, err = self.hold_cmd(root=str(real))
            self.assertEqual(rc, 75, "the persisted root is what hold locks: " + err)
            self.assertIsNone(self.session())
        finally:
            unhold(holder)

    def test_a_tasks_symlink_is_refused_at_install_and_at_run(self):
        """A `tasks` that is itself a link would put the lock on this root and the session on the link's
        target: two locks for one inbox. Refused before any write, and by run through O_NOFOLLOW."""
        real = self.home / "elsewhere" / "tasks"
        real.mkdir(parents=True)
        self.work.mkdir(parents=True, exist_ok=True)
        link = self.work / "tasks"
        link.symlink_to(real)
        for extra in (("--inbox", str(link)), ("--work", str(self.work))):
            runner = self.runner()
            rc, _, err = self.install_with(runner, False, *extra)
            self.assertEqual(rc, 2, err)
            self.assertIn("is a symlink; tasks must be a real directory", err)
            self.assertFalse(cli.config_path().exists(), "nothing written")
            self.assertEqual(runner.calls, [], "the manager is never touched")
        from ag2_sparrow import task_io
        self.configure(INBOX_DIR=str(link))
        rc, _, err = self.hold_cmd()
        self.assertEqual((rc, "tasks/ under the held root is a symlink" in err), (2, True), "ERR=" + err)
        self.assertIsNone(self.session(), "no session started against the link's target")
        self.assertEqual(task_io.lock_held(str(self.work)), "free", "and the lock was released")

    def test_a_work_root_change_is_refused_with_uninstall_then_install(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        other = self.home / "other"
        before = cli.config_path().read_text()
        runner.calls.clear()
        rc, _, err = self.install_with(runner, False, "--work", str(other))
        self.assertEqual(rc, 1)
        self.assertIn(f"the work root is {self.work}, not {other}; run {cli.COMMAND} uninstall, then {cli.COMMAND} install", err)
        self.assertEqual(cli.config_path().read_text(), before)
        self.assertEqual(runner.calls, [], "refused before the manager is touched")
        self.assertFalse(other.exists(), "the proposed root is untouched: no tasks/ or results/ made")

    def test_a_config_without_a_root_is_one_line(self):
        self.configure()
        cfg = cli.config_path()
        cfg.write_text(cfg.read_text().replace("ROOT=", "WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks ROOT" in err), (2, True))


class ConnectOrphan(ConnectBase):
    """A held root with no registered job is an orphan: install refuses before any file is written."""

    def test_a_live_launcher_no_job_manages_refuses_a_reinstall_before_any_write_and_names_its_pid(self):
        """A real launcher holding the root with no registered job (a hand start, or a job the manager
        forgot) is an orphan: install probes, refuses, writes nothing, and names the launcher pid."""
        self.configure()
        from ag2_sparrow import task_io
        before = cli.config_path().read_text()
        proc, child = HoldProcess.start_hold(self)
        try:
            self.assertEqual(task_io.lock_holder_pid(str(self.work)), child, "the consumer is the fencing pid")
            shutil.rmtree(self.results)
            runner = self.runner({})
            rc, out, err = self.install_with(runner, False, "--permission-mode", "default")
            self.assertEqual(rc, 1)
            self.assertFalse(self.results.exists(), "the refusal made no directory under the root")
            self.assertIn(f"{self.work} is held by a session no job of {cli.SERVICE_NAME} manages (launcher pid {child})", err)
            self.assertIn("nothing was applied", err)
            self.assertNotIn(ConnectInstall.ACQUIRED, out)
            self.assertEqual(runner.verbs("systemctl"), ["show"], "probed, not applied")
            self.assertEqual(cli.config_path().read_text(), before, "config untouched")
            self.assertFalse(cli.service_path().exists(), "no unit written")
        finally:
            os.kill(child, signal.SIGKILL)
            proc.communicate(timeout=30)
            wait_gone(child)

    def test_a_manual_run_holding_the_root_refuses_a_reinstall_by_pid(self):
        self.configure()
        holder = hold(str(self.work))
        try:
            rc, _, err = self.install_with(self.runner({("launchctl", "print"): NOT_LOADED}), True)
            self.assertEqual(rc, 1)
            self.assertIn(f"no job of {cli.AGENT_LABEL} manages (launcher pid {holder.pid})", err)
            rc, _, err = self.install_with(self.runner({("launchctl", "print"): (1, "", "launchctl: hm\n")}), True)
            self.assertEqual((rc, "is unknown; nothing was applied" in err), (1, True), "an unknown probe applies nothing")
            rc, _, err = self.install_with(self.runner({("systemctl", "show"): SHOW("loaded", "inactive", "enabled")}))
            self.assertEqual(rc, 1, "a registered but inactive job runs no holder either: an orphan")
            self.assertIn(f"no job of {cli.SERVICE_NAME} manages (launcher pid {holder.pid})", err)
        finally:
            unhold(holder)

    def test_a_bootout_that_returns_early_leaves_the_old_holder_and_the_reinstall_reports_failure(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner, True)[0], 0)
        old, before = runner.pid(), cli.config_path().read_text()

        class Early(Runner):
            """bootout returns 0 at once and launchd forgets the job, but the old process keeps the lock."""
            def __call__(self, cmd, timeout=30):
                verb = self.verb(cmd)
                if verb == "print":
                    self.table[("launchctl", "print")] = NOT_LOADED if any(c[1] == "bootout" for c in self.calls) else 0
                if verb in ("bootout", "bootstrap"):
                    self.table[("launchctl", verb)] = 0
                return super().__call__(cmd)
        early = Early(root=None)
        rc, out, err = self.install_with(early, True, "--permission-mode", "default")
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.work} is held by launcher pid {old} after the stop; nothing was applied", err)
        self.assertNotIn(ConnectInstall.ACQUIRED, out)
        self.assertNotIn("bootstrap", early.verbs("launchctl"), "no start over a root that is not free")
        self.assertEqual(cli.config_path().read_text(), before, "nothing written")
        self.assertIsNone(runner.session.poll(), "the old session is still there, untouched")


class ConnectSerialize(ConnectBase):
    """One per-shape operation lock across install, uninstall and debug-run's check-then-acquire."""

    def test_concurrent_installs_serialize_on_the_operation_lock(self):
        other = hold(None, script=OP_HOLDER)
        try:
            rc, _, err = self.install_with(self.runner())
            self.assertEqual(rc, 1)
            self.assertIn(f"another {cli.COMMAND} operation is in progress (lock {cm.oplock_path(L)})", err)
            self.assertFalse(cli.config_path().exists(), "nothing written while the other installer holds the lock")
        finally:
            unhold(other)
        self.assertEqual(self.install_with(self.runner())[0], 0, "once it is released, the install proceeds")
        self.assertTrue(cm.oplock_path(L).exists())

    def test_uninstall_serializes_on_the_operation_lock_and_the_managed_run_never_takes_it(self):
        """The managed start does not take the install operation lock, so a bootstrapped launcher
        never deadlocks install, which keeps its own lock through confirmation."""
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        other = hold(None, script=OP_HOLDER)
        try:
            with mock.patch.object(sm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("uninstall")
            self.assertEqual((rc, "operation is in progress" in err), (1, True))
            self.assertTrue(cli.config_path().exists())
            seen = {}
            with mock.patch.object(launcher.Launcher, "serve", lambda self_: seen.update(oplock_held=not oplock_free()) or 0):
                self.assertEqual(self.main("run")[0], 0)
            self.assertTrue(seen["oplock_held"], "run ran to the launcher while another operation held the lock")
        finally:
            unhold(other)
        src = Path(launcher.__file__).read_text() + Path(cli.__file__).read_text().split("def run(")[1].split("\ndef ")[0]
        self.assertNotIn("operation_lock", src.split("def install(")[0] if "def install(" in src else src,
                         "neither the launcher nor run names the operation lock")
        with mock.patch.object(sm, "_run", side_effect=self.runner({})):
            self.assertEqual(self.main("uninstall")[0], 0)
        self.assertTrue(cm.oplock_path(L).exists(), "the mutex file is never unlinked")


class ConnectMod(unittest.TestCase):
    def test_the_mod_auto_arms_only_on_the_launchers_marker(self):
        src = REGISTER.read_text()
        self.assertIn(f"(await $.env.get('{cli.AUTOARM_VAR}')) === '1'", src)
        start = src[src.index("session.start"):src.index("on('tool.call'")]
        self.assertIn("r = await arm($, s,", start, "session.start arms from the env with no model turn")
        self.assertIn("try {", start, "a failed auto-arm never breaks the session")
        self.assertIn("AG2_INBOX_DIR", src)
        self.assertIn("AG2_RESULTS_DIR", src)

    def test_the_mod_writes_no_claim_or_progress_or_result_file_itself(self):
        """Every claim, authorize, confirm, stage, publish, park and progress mutation is one
        `ag2-sparrow-claude-local inbox <verb>` call; the Mod never writes results/ or state/ directly."""
        src = REGISTER.read_text()
        self.assertNotIn("$.fs.write", src)
        self.assertNotIn("os.replace", src)
        for verb in ("'claim'", "'interrupt'", "'authorize'", "'confirm'", "'stage'", "'publish'", "'park'", "'progress'", "'check-inode'",
                     "'envelope'"):
            self.assertIn(verb, src, verb)
        self.assertIn("'inbox'", src)
        self.assertIn("$.session.id()", src, "the session-id witness")
        self.assertIn("AG2_INBOX_INODE", src, "the inode check")
        self.assertIn("e.reason === 'clear'", src)


class ConnectUninstall(ConnectBase):
    """Teardown, config last: stop, explicit absent (systemd: inactive and disabled), the root free, the
    unit, reload with LoadState=not-found, then the config; every refusal names the recovery."""

    def test_systemd_stop_disable_absent_free_unit_reload_not_found_then_config(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        runner.calls.clear()
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("systemctl"), ["show", "stop", "disable", "show", "daemon-reload", "show"])
        self.assertFalse(cli.service_path().exists() or cli.config_path().exists())
        self.assertIn(f"stopped: {cli.SERVICE_NAME}", out)
        self.assertLess(out.index(f"removed: {cli.service_path()}"), out.index(f"removed: {cli.config_path()}"), "config last")
        self.assertEqual(self.relay.read_text(), f"REMOTE_TASK_TOKEN={TOKEN}\n", "Sparrow's token file is untouched")
        self.assertTrue((self.plugin / ".claude-plugin" / "plugin.json").exists())
        with mock.patch.object(sm, "_run", side_effect=Runner()):
            self.assertIn("nothing to remove", self.main("uninstall")[1])

    def test_a_partial_systemd_teardown_keeps_the_config_and_completes_on_retry(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        for table, why in (({("systemctl", "daemon-reload"): 1}, "daemon-reload failed"),
                           ({("systemctl", "show"): SHOW("loaded", "inactive", "disabled")}, "still known to systemd (LoadState loaded)")):
            self.configure()
            cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
            with mock.patch.object(sm, "_run", side_effect=self.runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn(why, err)
            self.assertFalse(cli.service_path().exists(), "the unit went")
            self.assertTrue(cli.config_path().exists(), "the config stays, so the rerun knows the root")
            self.assertIn("kept so a rerun knows the work root", err)
            runner = self.runner()
            with mock.patch.object(sm, "_run", side_effect=runner):
                rc, out, err = self.main("uninstall")
            self.assertEqual(rc, 0, err)
            self.assertEqual(runner.verbs("systemctl"), ["show", "daemon-reload", "show"], "the retry finishes from where it stopped")
            self.assertFalse(cli.config_path().exists())
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        with mock.patch.object(sm, "_run", side_effect=self.runner({("systemctl", "show"): SHOW("loaded", "inactive", "enabled")})):
            rc, _, err = self.main("uninstall")
        self.assertEqual((rc, "is still inactive and enabled after the stop" in err), (1, True))
        self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        with mock.patch.object(sm, "_run", side_effect=self.runner()):
            self.assertEqual(self.main("uninstall")[0], 0)

    def test_a_registered_job_without_files_is_still_torn_down(self):
        self.configure()
        runner = self.runner(registered=True)
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("systemctl"), ["show", "stop", "disable", "show", "daemon-reload", "show"],
                         "the manager is probed and the job stopped, disabled, reloaded away")
        self.assertFalse(cli.config_path().exists())
        with mock.patch.object(sm, "_run", side_effect=Runner()):
            self.assertIn("nothing to remove", self.main("uninstall")[1])
        runner = self.runner(registered=True)
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual((rc, runner.verbs("systemctl")), (0, ["show", "stop", "disable", "show", "daemon-reload", "show"]),
                         "no files at all, a registered job: still torn down")
        self.assertNotIn("nothing to remove", out)
        mac = self.runner(registered=True)
        with mock.patch.object(cm, "is_macos", return_value=True), mock.patch.object(sm, "_run", side_effect=mac):
            rc, out, _ = self.main("uninstall")
        self.assertEqual((rc, mac.calls), (0, [["launchctl", "print", SERVICE_TARGET], ["launchctl", "bootout", "--wait", SERVICE_TARGET],
                                               ["launchctl", "print", SERVICE_TARGET]]))

    def test_uninstall_waits_a_bounded_time_for_the_root_to_free_after_the_stop(self):
        runner = self.runner(stop_delay=0.6)
        self.assertEqual(self.install_with(runner)[0], 0)
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 0, "the launcher reaps within the bound: " + err)
        self.assertFalse(cli.config_path().exists())

    def test_launchd_boots_out_with_wait_and_requires_the_documented_not_found(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner, True)[0], 0)
        runner.calls.clear()
        with mock.patch.object(sm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("launchctl"), ["print", "bootout", "print"])
        self.assertEqual(runner.calls[1], ["launchctl", "bootout", "--wait", SERVICE_TARGET])
        self.assertFalse(cli.plist_path().exists() or cli.config_path().exists())
        self.assertLess(out.index(f"removed: {cli.plist_path()}"), out.index(f"removed: {cli.config_path()}"), "config last")
        plist = cli.plist_path()
        for table in ({("launchctl", "bootout"): 0, ("launchctl", "print"): 0},                 # still loaded
                      {("launchctl", "bootout"): 3, ("launchctl", "print"): (1, "", "odd\n")},  # not the documented error
                      {("launchctl", "bootout"): None, ("launchctl", "print"): None}):
            self.assertEqual(self.install_with(self.runner(), True)[0], 0)
            self.runners[-1].stop_session()
            with mock.patch.object(sm, "_run", side_effect=Runner(table)), mock.patch.object(cm, "is_macos", return_value=True):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertTrue(plist.exists() and cli.config_path().exists(), "kept after an unconfirmed stop")
            self.assertIn(f"Manual recovery: 1. stop the job by hand: launchctl bootout --wait {SERVICE_TARGET}", err)
            self.assertIn(f"3. re-run {cli.COMMAND} uninstall", err)
            with mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "bootout"): 3, ("launchctl", "print"): NOT_LOADED})), \
                    mock.patch.object(cm, "is_macos", return_value=True):
                self.assertEqual(self.main("uninstall")[0], 0, "bootout said no such process and print confirms it: absent")

    def test_a_live_holder_refuses_with_the_recovery_steps_verbatim(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        keeper = runner.session
        runner.session = None                                  # the stop verb reaches nobody: the holder stays
        try:
            with mock.patch.object(sm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn(f"{self.work} is held by launcher pid {keeper.pid}; ", err)
            self.assertIn("were kept so a rerun knows the work root", err)
            self.assertIn(f"Manual recovery: 1. stop the job by hand: systemctl --user stop {cli.SERVICE_NAME}", err)
            self.assertIn(f"2. stop what holds the work root {self.work}: kill {keeper.pid} (the launcher pid in the lock record)", err)
            self.assertIn(f"3. re-run {cli.COMMAND} uninstall", err)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        finally:
            unhold(keeper)
        with mock.patch.object(sm, "_run", side_effect=Runner({})):
            self.assertEqual(self.main("uninstall")[0], 0, "re-run once the root is free")
        self.assertFalse(cli.config_path().exists())

    def test_a_held_root_with_no_live_launcher_in_the_record_names_lsof_on_the_lock_file(self):
        """A holder between its flock and its record (or one whose record is gone) is the only way a
        held lock names no live launcher now that no child ever holds it; the recovery names lsof."""
        from ag2_sparrow import task_io
        self.configure()
        cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
        holder = hold(str(self.work), script=UNPUBLISHED)
        try:
            with mock.patch.object(sm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn("held by an unrecorded holder", err)
            self.assertNotIn("child", err)
            self.assertIn(f"lsof {task_io.lock_path(str(self.work))}", err)
            self.assertTrue(cli.config_path().exists())
        finally:
            unhold(holder)

    def test_an_unknown_stop_probe_or_a_lock_error_refuses(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        for table in ({("systemctl", "stop"): 0, ("systemctl", "show"): SHOW("loaded", "active", "enabled")},
                      {("systemctl", "stop"): None}):
            with mock.patch.object(sm, "_run", side_effect=Runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn("Manual recovery", err)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        for table in ({("systemctl", "stop"): 1, ("systemctl", "show"): (1, "", "Failed to connect to bus\n")},
                      {("systemctl", "show"): None}):
            with mock.patch.object(sm, "_run", side_effect=Runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual((rc, "state is unknown; nothing was removed" in err), (1, True), table)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        with mock.patch.object(sm, "_run", side_effect=self.runner()), mock.patch.object(cm, "lock_held", return_value="unknown"):
            rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertIn("unknown (the lock of", err)
        self.assertTrue(cli.config_path().exists(), "a lock error keeps everything")
        with mock.patch.object(sm, "_run", side_effect=self.runner({("systemctl", "stop"): 1})):
            self.assertEqual(self.main("uninstall")[0], 0, "stop failed but the unit is confirmed inactive and disabled")

    def test_files_install_did_not_write_are_kept_and_named(self):
        self.configure()
        cm._write(cli.service_path(), "[Unit]\nDescription=the owner's own unit\n", 0o644)
        rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertIn(f"was not written by install; remove it by hand (rm {cli.service_path()})", err)
        self.assertTrue(cli.service_path().exists() and cli.config_path().exists(), "nothing goes while a foreign unit may run it")
        cli.service_path().unlink()
        cli.config_path().write_text("CLAUDE=/theirs\n")
        rc, _, err = self.main("uninstall")
        self.assertEqual((rc, cli.config_path().read_text()), (1, "CLAUDE=/theirs\n"))
        self.assertIn("remove it by hand", err)


class ConnectStrict(ConnectBase):
    def strict(self, key_text=None, *extra):
        key = self.home / "task-hmac.key"
        if key_text is not None:
            key.write_text(key_text)
        return self.install_with(self.runner(), False, "--hmac-strict", *extra)

    def test_strict_mode_refuses_a_malformed_key_and_records_a_key_not_there_yet(self):
        rc, _, err = self.strict("not-a-key")
        self.assertEqual((rc, "not a 64-hex-character key" in err, "--hmac-strict needs a usable key" in err), (2, True, True))
        self.assertFalse(cli.config_path().exists())
        (self.home / "task-hmac.key").unlink()
        rc, out, err = self.strict()
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: %s missing: strict mode submits nothing until it is back" % (self.home / "task-hmac.key"), out)
        self.assertIn("honoured without a reinstall", out)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual((cfg["HMAC_STRICT"], cfg["HMAC_KEY_FILE"]), ("1", str(self.home / "task-hmac.key")), "strict, key absent")
        self.runners[-1].stop_session()
        rc, _, err = self.strict("ab" * 32)
        self.assertEqual(rc, 0, err)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual((cfg["HMAC_STRICT"], cfg["HMAC_KEY_FILE"]), ("1", str(self.home / "task-hmac.key")))
        self.runners[-1].stop_session()
        env = launcher.session_env(cfg, "/c", "/r")
        self.assertEqual(env["AG2_HMAC_KEY_FILE"], cfg["HMAC_KEY_FILE"], "the verifier's key path reaches the session")
        self.assertEqual(env.get("AG2_HMAC_STRICT"), "1", "exported for a plugin older than the envelope verb")
        self.assertNotIn("AG2_HMAC_STRICT", launcher.session_env(dict(cfg, HMAC_STRICT="0"), "/c", "/r"), "lenient: not exported")
        self.assertNotIn("AG2_HMAC_STRICT", launcher.session_env(dict(cfg, HMAC_KEY_FILE="", HMAC_STRICT=""), "/c", "/r"), "off")
        self.assertEqual(launcher.session_env(dict(cfg, HMAC_KEY_FILE="", HMAC_STRICT="1"), "/c", "/r").get("AG2_HMAC_STRICT"), "1",
                         "a strict flag with no key path exports strict: an old plugin fails closed too")
        rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox), "--hmac-strict")
        self.assertEqual((rc, "no key file" in err), (2, True))

    def test_verify_fails_the_hmac_part_on_a_missing_or_malformed_key(self):
        (self.home / "task-hmac.key").unlink()
        self.configure(HMAC_STRICT="1")
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL hmac key: strict mode:", out)
        (self.home / "task-hmac.key").write_text("short")
        self.assertIn("FAIL hmac key: strict mode:", self.main("verify")[1])
        (self.home / "task-hmac.key").write_text("ab" * 32)
        self.assertIn("PASS hmac key", self.main("verify")[1])
        self.configure(HMAC_KEY_FILE="", HMAC_STRICT="")
        self.assertIn("PASS hmac key: none (every task reads unsigned)", self.main("verify")[1])

    def test_strictness_is_derived_per_read_from_the_config_and_the_key_file_as_it_is_then(self):
        """The one owner, hmac_policy.decide, and the Mod's `inbox envelope` verb over it: key present
        and no opt-out is strict; the opt-out is lenient whatever the key; a key added after install is
        honoured on the next read with no reinstall; a key removed under strict refuses, never lenient."""
        key = self.home / "task-hmac.key"
        key.unlink()
        self.assertEqual(self.install_with(self.runner())[0], 0, "a key not there yet is recorded")
        self.runners[-1].stop_session()
        cfg = cli.read_config(cli.config_path())
        self.assertEqual(hmac_policy.mode(cfg), hmac_policy.STRICT)
        missing = "strict mode: %s missing: strict mode submits nothing until it is back" % key
        for verdict in ("verified", "unsigned", "unverifiable"):
            self.assertEqual(hmac_policy.decide(cfg, verdict), (False, missing, True), verdict)
        self.assertIn("FAIL hmac key: strict mode: %s missing" % key, self.main("verify")[1])
        key.write_text("ab" * 32)                       # added after install: strict on the next read, no reinstall
        self.assertEqual(hmac_policy.decide(cfg, "verified"), (True, "", False))
        self.assertEqual(hmac_policy.decide(cfg, "unsigned"), (False, "strict mode: envelope unsigned", False))
        self.assertEqual(hmac_policy.decide(cfg, "unverifiable"), (False, "strict mode: envelope unverifiable", False))
        self.assertEqual(hmac_policy.decide(cfg, "invalid"), (False, "envelope invalid", False))
        self.assertEqual(hmac_policy.decide(cfg, "bogus")[0], False)
        self.assertIn("PASS hmac key", self.main("verify")[1])
        key.write_text("short")                          # malformed while strict: refused with the reason, held, never lenient
        self.assertEqual(hmac_policy.decide(cfg, "verified"),
                         (False, "strict mode: %s is not a 64-hex-character key, so every envelope reads unverifiable" % key, True))
        key.unlink()                                     # removed while strict: refused with the one reason line, held
        self.assertEqual(hmac_policy.decide(cfg, "verified"), (False, missing, True))
        self.assertEqual(hmac_policy.mode(dict(cfg, HMAC_STRICT="")), hmac_policy.LENIENT, "a present empty value: the older opt-out record")
        self.assertEqual(hmac_policy.mode({k: v for k, v in cfg.items() if k != "HMAC_STRICT"}), hmac_policy.STRICT, "an absent line is no record")
        lenient = dict(cfg, HMAC_STRICT="0")             # --no-hmac-strict recorded as 0: lenient regardless of the key
        self.assertEqual(hmac_policy.mode(lenient), hmac_policy.LENIENT)
        for verdict in ("verified", "unsigned", "unverifiable"):
            self.assertEqual(hmac_policy.decide(lenient, verdict), (True, "", False), verdict)
        self.assertEqual(hmac_policy.decide(lenient, "invalid"), (False, "envelope invalid", False))
        key.write_text("ab" * 32)
        self.assertEqual(hmac_policy.decide(lenient, "unsigned"), (True, "", False))
        off = dict(cfg, HMAC_KEY_FILE="", HMAC_STRICT="")
        self.assertEqual(hmac_policy.mode(off), hmac_policy.OFF)
        self.assertEqual(hmac_policy.decide(off, "unsigned"), (True, "", False))
        self.assertEqual(hmac_policy.decide(off, "invalid"), (False, "envelope invalid", False))
        self.assertIs(cli.key_problem, hmac_policy.key_problem, "install reads the same owner")

    def test_a_strict_flag_with_no_key_path_fails_closed_never_off(self):
        """HMAC_STRICT=1 and HMAC_KEY_FILE empty (main refused this config at install; a hand edit or an
        older write can still hold it): strict, every task refused with the one reason line and held,
        verify FAIL naming the missing key; never off, never a PASS."""
        cfg = self.values(HMAC_KEY_FILE="", HMAC_STRICT="1")
        self.assertEqual(hmac_policy.mode(cfg), hmac_policy.STRICT)
        reason = "strict mode: no key file recorded (HMAC_KEY_FILE is empty): strict mode submits nothing"
        for verdict in ("verified", "unsigned", "unverifiable"):
            self.assertEqual(hmac_policy.decide(cfg, verdict), (False, reason, True), verdict)
        self.assertEqual(hmac_policy.decide(cfg, "invalid"), (False, "envelope invalid", False))
        self.configure(HMAC_KEY_FILE="", HMAC_STRICT="1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", "verified"])
        self.assertEqual((rc, json.loads(out.getvalue())),
                         (0, {"verdict": "verified", "mode": "strict", "submit": False, "reason": reason, "retry": True}))

    def test_verify_fails_the_hmac_part_on_a_strict_flag_with_no_key_path(self):
        self.configure(HMAC_KEY_FILE="", HMAC_STRICT="1")
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL hmac key: strict mode: no key file recorded (HMAC_KEY_FILE is empty)", out)
        self.assertNotIn("PASS hmac key", out)
        self.assertIn("INFO hmac mode: strict (only verified envelopes are submitted)", out)

    def test_the_envelope_verb_answers_from_the_installed_config_and_the_key_file_at_that_call(self):
        key = self.home / "task-hmac.key"
        self.configure(HMAC_STRICT="1")

        def verb(verdict, environ=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", verdict])
            return rc, json.loads(out.getvalue())

        self.assertEqual(verb("verified"), (0, {"verdict": "verified", "mode": "strict", "submit": True, "reason": "", "retry": False}))
        rc, out = verb("unsigned")
        self.assertEqual((rc, out["submit"], out["reason"], out["retry"]), (0, False, "strict mode: envelope unsigned", False))
        key.unlink()
        rc, out = verb("verified")
        self.assertEqual((rc, out["submit"], out["retry"]), (0, False, True), "held for the key's return, not retired")
        self.assertEqual(out["reason"], "strict mode: %s missing: strict mode submits nothing until it is back" % key)
        key.write_text("ab" * 32)
        self.assertEqual(verb("verified")[1]["submit"], True, "the next call sees the key back; nothing was reinstalled")
        self.configure(HMAC_STRICT="0")
        self.assertEqual(verb("unverifiable")[1], {"verdict": "unverifiable", "mode": "lenient", "submit": True, "reason": "", "retry": False})
        self.assertEqual(verb("invalid")[1]["submit"], False)
        self.assertEqual(inbox.envelope("unsigned", {inbox.CONFIG_DIR_VAR: str(cli.config_dir())})["mode"], "lenient", "the pane's config dir")
        with self.assertRaises(inbox.Parked):
            inbox.envelope("verified", {inbox.CONFIG_DIR_VAR: str(self.home / "none")})
        cli.config_path().unlink()
        rc, out = verb("verified")
        self.assertEqual((rc, out.get("retry"), "unreadable" in out.get("refused", "")), (1, True, True), "fail closed, retried")
        self.assertNotIn("envelope", inbox.PROGRESS_VERBS, "a read: it never clears or raises the progress alarm")

    def test_a_key_that_changes_between_the_verifiers_read_and_the_policys_holds_the_task_never_retires_it(self):
        """The Mod's two reads, the verifier's and the envelope verb's: a valid signed task whose key lands
        (missing -> valid) or is rotated (old -> new) between them is held for another verification, and
        that verification submits it. The verdict carries the digest of the key it was made with."""
        key = self.home / "task-hmac.key"
        self.configure(HMAC_STRICT="1")

        def verified(text):
            out = subprocess.run([sys.executable, "-c", verify_py()], input=text, capture_output=True, text=True,
                                 env=hermetic_env(AG2_HMAC_KEY_FILE=str(key)), check=True).stdout.split()
            return out[0], out[3]

        def verb(verdict, seen):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", verdict, "--key-seen", seen])
            data = json.loads(out.getvalue())
            return rc, data["submit"], data["retry"]

        k1, k2 = secrets.token_hex(32), secrets.token_hex(32)
        text = stamp("id: task-r1\nsource: ag2space\naccess_tier: owner\ntask: hi\n", k1)
        key.unlink()
        first = verified(text)
        self.assertEqual(first, ("unverifiable", "absent"))
        key.write_text(k1)                                  # the key lands between the two reads
        self.assertEqual(hmac_policy.decide(cli.read_config(cli.config_path()), *first)[1:],
                         ("the key file changed after this envelope was verified; verified again", True))
        self.assertEqual(verb(*first), (0, False, True), "held, not retired")
        again = verified(text)
        self.assertEqual(again[0], "verified")
        self.assertEqual(verb(*again), (0, True, False))

        text = stamp("id: task-r2\nsource: ag2space\naccess_tier: owner\ntask: hi\n", k2)
        first = verified(text)
        self.assertEqual(first[0], "invalid")
        key.write_text(k2)                                  # rotated between the two reads
        self.assertEqual(verb(*first), (0, False, True), "a stale invalid is held, not retired")
        again = verified(text)
        self.assertEqual(verb(*again), (0, True, False))
        self.assertEqual(verb("invalid", again[1]), (0, False, False), "invalid under the key read here stays final")

    def test_an_empty_or_incomplete_config_parks_the_envelope_verb_never_off(self):
        """A readable config short of a required key (empty, or a marker line alone) is not a config that
        says off: the verb parks (retry) exactly as a missing one does."""
        for text in ("", cm.render_config(cli.LOCAL, {}).splitlines()[0] + "\n", "CLAUDE=/x\n", "garbage\n"):
            cm._write(cli.config_path(), text, 0o600)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", "unsigned"])
            data = json.loads(out.getvalue())
            self.assertEqual((rc, data.get("retry"), data.get("submit")), (1, True, None), repr(text))
            self.assertIn("lacks", data["refused"])
            with self.assertRaises(inbox.Parked):
                inbox.envelope("unsigned", {inbox.CONFIG_DIR_VAR: str(cli.config_dir())})

    # (key path, HMAC_STRICT, key file) -> strict: verified only, others retired; held: all refused, retried;
    # lenient/off: all but invalid submitted. A config that is not complete parks (checked after the table).
    SWEEP = [
        # key path, flag,    file,        outcome
        ("present", "1",     "present",   "strict"),
        ("present", "1",     "missing",   "held"),
        ("present", "1",     "malformed", "held"),
        ("present", None,    "present",   "strict"),
        ("present", None,    "missing",   "held"),
        ("present", None,    "malformed", "held"),
        ("present", "",      "present",   "lenient"),     # the older install's --no-hmac-strict record
        ("present", "",      "missing",   "lenient"),
        ("present", "",      "malformed", "lenient"),
        ("present", "0",     "present",   "lenient"),
        ("present", "0",     "missing",   "lenient"),
        ("present", "0",     "malformed", "lenient"),
        ("absent",  "1",     "present",   "held"),
        ("absent",  "1",     "missing",   "held"),
        ("absent",  "1",     "malformed", "held"),
        ("absent",  None,    "present",   "off"),
        ("absent",  None,    "missing",   "off"),
        ("absent",  None,    "malformed", "off"),
        ("absent",  "",      "present",   "off"),
        ("absent",  "",      "missing",   "off"),
        ("absent",  "",      "malformed", "off"),
        ("absent",  "0",     "present",   "off"),
        ("absent",  "0",     "missing",   "off"),
        ("absent",  "0",     "malformed", "off"),
    ]

    def test_every_input_combination_fails_closed_unless_explicitly_lenient_or_off(self):
        key = self.home / "task-hmac.key"
        verdicts = ("verified", "unsigned", "unverifiable", "invalid")
        for key_path, flag, key_file, outcome in self.SWEEP:
            with self.subTest(key_path=key_path, flag=flag, key_file=key_file):
                if key_file == "present":
                    key.write_text("ab" * 32)
                elif key_file == "malformed":
                    key.write_text("short")
                elif key.exists():
                    key.unlink()
                values = self.values(HMAC_KEY_FILE=str(key) if key_path == "present" else "", HMAC_STRICT=flag or "")
                text = cli.render_config(values)
                if flag is None:
                    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("HMAC_STRICT=")) + "\n"
                cm._write(cli.config_path(), text, 0o600)
                cfg = cli.read_config(cli.config_path())
                self.assertEqual("HMAC_STRICT" in cfg, flag is not None)
                want_mode = {"strict": "strict", "held": "strict", "lenient": "lenient", "off": "off"}[outcome]
                self.assertEqual(hmac_policy.mode(cfg), want_mode)
                seen = hmac_policy.key_digest(str(key))
                stale = "0" * 64 if seen != "0" * 64 else hmac_policy.ABSENT
                for verdict in verdicts:
                    submit, reason, retry = hmac_policy.decide(cfg, verdict)
                    if verdict == "invalid":
                        want = (False, "envelope invalid", False)
                    elif outcome == "held":
                        want = (False, reason, True)
                        self.assertTrue(reason.startswith("strict mode: "), reason)
                        self.assertFalse(reason.startswith("strict mode: envelope "), "the key, not the verdict, is the reason")
                    elif outcome == "strict":
                        want = (True, "", False) if verdict == "verified" else (False, "strict mode: envelope %s" % verdict, False)
                    else:
                        want = (True, "", False)
                    self.assertEqual((submit, reason, retry), want, verdict)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", verdict])
                    data = json.loads(out.getvalue())
                    self.assertEqual((rc, data["mode"], data["submit"], data["reason"], data["retry"]), (0, want_mode, *want), verdict)
                    # The verdict bound to the key read here decides the same; one bound to another key is held.
                    seen_here = seen if key_path == "present" else hmac_policy.ABSENT
                    self.assertEqual(hmac_policy.decide(cfg, verdict, seen_here), want, verdict)
                    self.assertEqual(hmac_policy.decide(cfg, verdict, stale if key_path == "present" else "0" * 64),
                                     (False, "the key file changed after this envelope was verified; verified again", True), verdict)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", verdict, "--key-seen", seen_here])
                    data = json.loads(out.getvalue())
                    self.assertEqual((rc, data["submit"], data["reason"], data["retry"]), (0, *want), verdict)
                rc, out, _ = self.main("verify")
                if outcome == "held":
                    self.assertIn("FAIL hmac key: strict mode: ", out)
                elif outcome == "off":
                    self.assertIn("PASS hmac key: none", out)
                elif key_file == "present":
                    self.assertIn("PASS hmac key: %s" % key, out)
                else:
                    self.assertIn("FAIL hmac key: %s" % key, out)
        for text in ("", "garbage\n"):                   # and no complete config at all: parked, never off
            cm._write(cli.config_path(), text, 0o600)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", "unsigned"]), 1)
            self.assertEqual(json.loads(out.getvalue()).get("retry"), True)
        cli.config_path().unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", "unsigned"]), 1)
        self.assertEqual(json.loads(out.getvalue()).get("retry"), True)

    def test_the_opt_out_an_older_install_recorded_stays_lenient_through_the_verb_and_verify_warns(self):
        """7f7f56a8's install persisted `--hmac-key-file <path> --no-hmac-strict` as HMAC_STRICT= (present,
        empty: `"1" if strict else ""` through the same render_config). An unattended lenient install that
        upgrades keeps submitting unsigned and unverifiable owner tasks, with no rerun; verify says so."""
        runner = self.runner()
        self.assertEqual(self.install_with(runner, False, "--no-hmac-strict")[0], 0)
        self.runners[-1].stop_session()
        cfg = cli.read_config(cli.config_path())
        self.assertEqual(cfg["HMAC_STRICT"], "0")
        parent = cm._write(cli.config_path(), cli.render_config(dict(cfg, HMAC_STRICT="")), 0o600)   # what 7f7f56a8 wrote
        self.assertIn("\nHMAC_STRICT=''\n", cli.config_path().read_text())
        self.assertEqual(cli.read_config(cli.config_path())["HMAC_STRICT"], "")
        for verdict in ("unsigned", "unverifiable", "verified"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", verdict])
            self.assertEqual((rc, json.loads(out.getvalue())),
                             (0, {"verdict": verdict, "mode": "lenient", "submit": True, "reason": "", "retry": False}), verdict)
        (self.home / "task-hmac.key").unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["inbox", "--root", str(self.work), "envelope", "--verdict", "unverifiable"])
        self.assertEqual(json.loads(out.getvalue())["submit"], True, "lenient whatever the key file holds")
        self.assertTrue(hmac_policy.legacy_opt_out(cli.read_config(cli.config_path())))
        rc, out, _ = self.main("verify")
        self.assertIn("INFO hmac mode: not strict", out)
        self.assertIn("WARN hmac mode: legacy opt-out (HMAC_STRICT recorded empty by an older install --no-hmac-strict): not strict", out)
        self.assertNotIn("WARN hmac mode", self.main("verify")[1] if self.configure(HMAC_STRICT="0") else "", "the current record: no warning")
        self.assertNotIn("WARN hmac mode", self.main("verify")[1] if self.configure(HMAC_STRICT="1") else "")
        self.assertNotIn("WARN hmac mode", self.main("verify")[1] if self.configure(HMAC_KEY_FILE="", HMAC_STRICT="") else "", "off is not legacy")
        self.assertFalse(hmac_policy.legacy_opt_out({"HMAC_KEY_FILE": str(self.home / "task-hmac.key")}), "a missing line is no record")
        del parent

    def test_a_key_file_alone_is_strict_and_no_hmac_strict_opts_out(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        self.assertEqual(cli.read_config(cli.config_path())["HMAC_STRICT"], "1", "a key file without --no-hmac-strict")
        self.assertIn("INFO hmac mode: strict (only verified envelopes are submitted)", self.main("verify")[1])
        self.assertEqual(self.install_with(runner, False, "--no-hmac-strict")[0], 0)
        self.assertEqual(cli.read_config(cli.config_path())["HMAC_STRICT"], "0", "the opt-out is an explicit record")
        self.assertIn("INFO hmac mode: not strict", self.main("verify")[1])
        (self.home / "task-hmac.key").write_text("not-a-key")
        rc, _, err = self.install_with(runner)
        self.assertEqual((rc, "--no-hmac-strict opts out" in err), (2, True), err)
        self.assertEqual(self.install_with(runner, False, "--no-hmac-strict")[0], 0, "the soak mode stays reachable")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.install("--hmac-strict", "--no-hmac-strict")

    def test_allow_non_owner_is_persisted_off_by_default_and_shown_by_verify(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        self.assertEqual(cli.read_config(cli.config_path())["ALLOW_NON_OWNER"], "")
        self.assertIn("INFO non-owner tasks: refused (owner only)", self.main("verify")[1])
        self.assertFalse(inbox.allow_non_owner())
        self.assertEqual(self.install_with(runner, False, "--allow-non-owner")[0], 0)
        self.assertEqual(cli.read_config(cli.config_path())["ALLOW_NON_OWNER"], "1")
        self.assertIn("INFO non-owner tasks: submitted with the non-owner note (--allow-non-owner)", self.main("verify")[1])
        self.assertTrue(inbox.allow_non_owner())
        self.assertTrue(inbox.allow_non_owner({inbox.CONFIG_DIR_VAR: str(cli.config_dir())}), "the pane's config dir")
        self.assertFalse(inbox.allow_non_owner({inbox.CONFIG_DIR_VAR: str(self.home / "none")}), "unreadable reads as off")
        self.assertEqual(inbox.CONFIG_DIR_VAR, hold_mod.ENV_CONFIG_DIR)


class ConnectOwnership(ConnectBase):
    def test_an_unowned_config_is_kept_and_named_on_install_run_and_uninstall(self):
        cfg = cli.config_path()
        cfg.write_text("CLAUDE=/theirs\n")
        rc, _, err = self.install_with(self.runner())
        self.assertEqual(rc, 1)
        self.assertIn(f"{cfg} was not written by install; remove it by hand (rm {cfg})", err)
        self.assertEqual(cfg.read_text(), "CLAUDE=/theirs\n")
        self.assertFalse(cli.service_path().exists(), "nothing written before the refusal")

    def test_a_dangling_a_directory_or_an_unparsable_config_is_one_line_and_kept(self):
        cfg = cli.config_path()
        cfg.symlink_to(self.home / "gone")
        rc, _, err = self.main("run")
        self.assertEqual((rc, "dangling symlink" in err), (2, True))
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, f"remove it by hand (rm {cfg})" in err), (1, True))
        cfg.unlink()
        cfg.mkdir()
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, "is a directory, not a config; remove it yourself" in err), (1, True))
        self.assertEqual(self.main("verify")[0], 1)
        rc, _, err = self.main("uninstall")
        self.assertEqual((rc, "is a directory" in err), (1, True))
        self.assertTrue(cfg.is_dir())
        cfg.rmdir()
        good = self.configure().read_text()
        for bad in (good + "PERMISSION_MODE='unclosed\n", good.replace("INBOX_DIR=", "INBOX_WAS="),
                    good.replace(f"ROOT={self.work}", "ROOT=relative/work")):
            cfg.write_text(bad)
            scratch = self.home / "cwd"
            scratch.mkdir(exist_ok=True)
            was = os.getcwd()
            os.chdir(scratch)                                   # a relative ROOT would be made under the cwd
            try:
                rc, _, err = self.main("run")
            finally:
                os.chdir(was)
            self.assertEqual(rc, 2, bad)
            self.assertEqual(os.listdir(scratch), [], "refused by the loader, before anything under the cwd is made")
            self.assertIsNone(self.session())
            rc, _, err = self.install_with(self.runner())
            self.assertEqual(rc, 1, "a marked but malformed config is never replaced: " + err)
            self.assertIn("remove it by hand", err)
            self.assertEqual(cfg.read_text(), bad, "preserved")
            self.assertFalse(cli.service_path().exists())
            rc, _, err = self.main("uninstall")
            self.assertEqual((rc, cfg.read_text()), (1, bad), "uninstall keeps it too: " + err)

    def test_a_malformed_owned_config_fails_closed_with_an_old_root_orphan(self):
        """The persisted root is the only anchor; a config that cannot name it stops install and uninstall
        before any mutation, while a holder on the old root stays untouched."""
        from ag2_sparrow import task_io
        cfg = self.configure()
        cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
        good = cfg.read_text()
        bad = good.replace(f"ROOT={self.work}", "ROOT='unmatched")
        cfg.write_text(bad)
        holder = hold(str(self.work), "claude-local")
        try:
            other = self.home / "other"
            rc, _, err = self.install_with(self.runner(), False, "--work", str(other))
            self.assertEqual(rc, 1)
            self.assertIn("is not parsable", err)
            self.assertFalse(other.exists())
            self.assertEqual(cfg.read_text(), bad, "preserved")
            with mock.patch.object(sm, "_run", side_effect=self.runner()) as r:
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn("is not parsable", err)
            self.assertTrue(cli.service_path().exists() and cfg.exists(), "nothing removed")
            self.assertEqual(r.side_effect.calls, [], "the manager is not touched")
            self.assertEqual(task_io.lock_held(str(self.work)), "held", "the old root's holder is untouched")
        finally:
            unhold(holder)
    def test_verify_stops_cleanly_on_a_config_missing_a_required_key(self):
        self.configure(PLUGIN_DIR="")
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config", out)
        self.assertIn("lacks PLUGIN_DIR", out)
        self.assertNotIn("claude", {line.split()[1].rstrip(":") for line in out.splitlines() if line[:4] in ("PASS", "FAIL")})


class ConnectBranches(ConnectBase):
    """The small branches the command tests do not reach, called in-process."""

    def test_console_script_run_and_platform_helpers(self):
        for p in self.patches:       # the real helpers, not the fixture's stand-ins
            p.stop()
        exe = self.bin / cli.COMMAND
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        with mock.patch.object(cm.sysconfig, "get_path", return_value=str(self.bin)):
            self.assertEqual(cm.console_script(L), str(exe))
        with mock.patch.object(cm.sysconfig, "get_path", return_value=str(self.home / "nowhere")), \
                mock.patch.object(cm.shutil, "which", return_value=None):
            self.assertEqual(cm.console_script(L), "")
            self.assertIn("was not found beside", cm.render_supervisor(L)[1])
        self.assertTrue(cm._ok(sm._run(["true"])))
        self.assertIsNone(sm._run([str(self.home / "no-such-program")]))
        self.assertEqual(cm._why(None), "could not run")
        self.assertEqual(cm._why(subprocess.CompletedProcess([], 4, "", "")), "exit 4")
        self.assertEqual(cm._why(subprocess.CompletedProcess([], 1, "", "a\nlast line\n")), "last line")
        self.assertIsInstance(cm.is_macos(), bool)
        self.assertTrue(cm._launchd_target().startswith("gui/"))
        self.assertTrue(str(cm.log_path(L)).endswith("claude-local.log"))
        self.assertEqual(cli.read_config(self.write_conf("# c\nA='x y'\nB=\nbare\n")), {"A": "x y", "B": ""})
        self.assertEqual(cli.SHAPE, "claude-local")
        with mock.patch.object(cm, "is_macos", return_value=True):
            self.assertEqual(cm.job_name(L), cli.AGENT_LABEL)
            self.assertEqual(cm.stop_command(L), f"launchctl bootout --wait {SERVICE_TARGET}")
        with mock.patch.object(cm, "is_macos", return_value=False):
            self.assertEqual(cm.stop_command(L), f"systemctl --user stop {cli.SERVICE_NAME}")

    def write_conf(self, text):
        p = self.home / "some.conf"
        p.write_text(text)
        return p

    def test_probes_read_explicit_outputs_only(self):
        for got, want in ((SHOW("loaded", "active", "enabled"), "active"), (SHOW("loaded", "activating", "enabled"), "active"),
                          (SHOW("not-found", "inactive", "disabled"), "absent"), (SHOW("not-found", "failed", ""), "absent"),
                          (SHOW("loaded", "inactive", "disabled"), "registered"), (SHOW("not-found", "inactive", "enabled"), "registered"),
                          (SHOW("loaded", "inactive", "enabled"), "registered"), ((1, "", "Failed to connect to bus\n"), "unknown"),
                          (SHOW("odd", "inactive", "disabled"), "unknown"), (None, "unknown")):
            with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): got})) as r:
                self.assertEqual(cm.unit_active(L), want, got)
            self.assertEqual(r.side_effect.calls[0], ["systemctl", "--no-ask-password", "--user", "show", "-p",
                                                      "LoadState,ActiveState,UnitFileState", "--value", cli.SERVICE_NAME])
        for got, want in ((0, "active"), (NOT_LOADED, "absent"), ((113, "", "Bad request\n"), "unknown"), (None, "unknown")):
            with mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): got})) as r:
                self.assertEqual(cm.agent_loaded(L), want, got)
            self.assertEqual(r.side_effect.calls[0], ["launchctl", "print", SERVICE_TARGET])
        with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): None})):
            self.assertEqual(cm.unit_load_state(L), "unknown")
        with mock.patch.object(cm, "is_macos", return_value=True), \
                mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): 0})):
            self.assertEqual(cm.manager_state(L), "active")
        with mock.patch.object(sm, "_run", side_effect=Runner({("loginctl", "enable-linger"): 1})), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cm.linger(L)
        self.assertIn("enable-linger owner failed", out.getvalue())
    def test_a_non_plugin_dir_a_missing_console_script_and_a_failed_write(self):
        rc, _, err = self.main("install", "--plugin-dir", str(self.home))
        self.assertEqual((rc, "is not a Claude Code plugin" in err), (2, True))
        with mock.patch.object(cm, "console_script", return_value=""):
            rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, "was not found beside" in err), (1, True))
        self.assertFalse(cli.config_path().exists(), "nothing written without a unit to write")
        with mock.patch.object(cm.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cm._write(self.home / "x.conf", "text", 0o600)
        self.assertEqual([p.name for p in self.home.glob(".x.conf.*")], [], "the temp file is gone")

    def test_run_and_verify_edge_branches(self):
        rc, _, err = self.main("run")
        self.assertEqual((rc, "unreadable" in err), (2, True), "no config yet")
        rc, _, err = self.main("attach")
        self.assertEqual((rc, "unreadable" in err), (2, True))
        rc, _, err = self.main("stop", "--confirm")
        self.assertEqual((rc, "unreadable" in err), (2, True))
        rc, _, err = self.main("stop")
        self.assertEqual((rc, "stop takes --confirm" in err), (2, True))
        self.configure()
        cfg = cli.read_config(cli.config_path())
        self.assertEqual(hold_mod.claude_argv(dict(cfg, PERMISSION_MODE=""), SESSION_ID)[3:7],
                         ["--permission-mode", "bypassPermissions", "--session-id", SESSION_ID])
        self.assertNotIn("CLAUDE_CONFIG_DIR", cli.child_env(dict(cfg, CLAUDE_CONFIG_DIR=""), {"PATH": "/bin"}))
        cfg_path = cli.config_path()
        cfg_path.write_text(cfg_path.read_text().replace(SESSION_ID, "not-a-uuid"))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "malformed SESSION_ID" in err), (2, True))
        self.configure()
        self.assertIn("auth status did not run", cli.auth_problem(dict(cfg, CLAUDE=str(self.home / "none"))))
        self.assertIsNone(cli.cli_version(str(self.home / "none")))
        rc, out, _ = self.main("verify")
        self.assertIn("FAIL bridge: no bridge wrote a status under", out)
        self.assertIn(f"start the bridge with AGENT_CONNECT_TASK_DIR={self.work}/tasks", out)
        self.assertNotIn("relay token", out, "an explicit root's bridge is yours: no host relay.env is assumed")
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["nonsense"])
        self.assertEqual(e.exception.code, 2)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])


class RecordUnderHoldStart(unittest.TestCase):
    """The launcher writes its record while the pane's hold prepares the same root: hold's startup
    sweep must never take a live writer's temp (a replace with no source), only an abandoned one."""

    def test_a_record_written_while_hold_prepares_the_root_lands(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "work")
            os.mkdir(root)
            self.assertEqual(inbox.prepare_root(root), "")
            real = os.replace

            def hold_starts_mid_write(src, dst):
                self.assertEqual(inbox.prepare_root(root), "", "hold's own prepare, between the temp and the replace")
                return real(src, dst)
            with mock.patch.object(records.os, "replace", side_effect=hold_starts_mid_write):
                records.save_supervisor(root, dict(records.empty_supervisor(), agent_id="@a:x"))
            self.assertEqual(records.load_supervisor(root)["agent_id"], "@a:x")
            old = Path(root, "state", ".claude-local-supervisor.json.dead.tmp")
            old.write_text("{}")
            os.utime(old, (time.time() - 200, time.time() - 200))
            self.assertEqual(inbox.prepare_root(root), "")
            self.assertFalse(old.exists(), "an abandoned temp is still swept")


class LockHelpers(unittest.TestCase):
    """task_io: lock_held is held | free | unknown (the flock, occupancy); lock_holder is attribution."""

    def test_the_contended_retry_reads_liveness_through_holder_locks_one_reader(self):
        """A schema-2 record whose consumer is dead but whose holder lives is held: no retry. Nobody
        live (no record) is a probe's momentary flock: retried until the bound."""
        p = subprocess.Popen(["sleep", "30"])
        dead_start = task_io.start_time(p.pid)
        p.kill()
        p.wait()
        me = (os.getpid(), task_io.start_time(os.getpid()))
        rec = {"schema": 2, "adapter": "x", "pid": p.pid, "start": dead_start, "host": holder_lock._host(),
               "install_id": "i", "lock": {"dev": 1, "ino": 2}, "acquired_at": 1.0, "owner_pid": me[0],
               "owner_start": me[1], "consumer": {"pid": p.pid, "start": dead_start}, "tombstone": None}
        for prior, retried in ((holder_lock.Valid(rec), False), (holder_lock.Missing(), True)):
            calls = []
            with mock.patch.object(holder_lock, "acquire",
                                   lambda *a, **k: calls.append(1) or holder_lock.Contended(prior)), \
                    self.assertRaises(task_io.LockRefused):
                task_io.acquire_lock("/nonexistent-root", "x", retry_s=0.3)
            self.assertEqual(len(calls) > 1, retried, (prior, len(calls)))

    def test_lock_held_lock_holder_lock_path_and_lock_holder_pid(self):
        from ag2_sparrow import task_io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "work"
            self.assertEqual(task_io.lock_held(str(root)), "free", "no root")
            self.assertIsNone(task_io.lock_holder_pid(str(root)))
            self.assertEqual(task_io.lock_holder(str(root)), {})
            root.mkdir()
            self.assertEqual(task_io.lock_held(str(root)), "free", "no lock file yet")
            holder = hold(str(root), "claude-local")
            try:
                self.assertEqual(task_io.lock_held(str(root)), "held")
                rec = task_io.lock_holder(str(root))
                self.assertEqual((rec["pid"], rec["live"], rec["adapter"], rec["install_id"]),
                                 (holder.pid, True, "claude-local", holder_lock.install_id()))
                self.assertTrue(rec["start"])
                self.assertEqual(task_io.lock_holder_pid(str(root)), holder.pid)
                self.assertTrue(task_io.lock_path(str(root)).endswith("/lock"))
            finally:
                unhold(holder)
            self.assertEqual(task_io.lock_held(str(root)), "free", "released: the file stays, the flock is gone")
            self.assertIsNone(task_io.lock_holder_pid(str(root)))
            with mock.patch.object(task_io.holder_lock, "lock_dir", side_effect=PermissionError("no")):
                self.assertEqual(task_io.lock_held(str(root)), "unknown", "an error is unknown, never free")
                self.assertIsNone(task_io.lock_path(str(root)))
            with mock.patch.object(task_io.os, "open", side_effect=PermissionError("no")):
                self.assertEqual(task_io.lock_held(str(root)), "unknown")
            with mock.patch.object(task_io.holder_lock, "status", side_effect=OSError("no")):
                self.assertEqual(task_io.lock_holder(str(root)), {})

    def test_a_stale_record_under_a_free_lock_reads_free(self):
        from ag2_sparrow import task_io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "work"
            root.mkdir()
            holder = hold(str(root))
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait(timeout=30)
            holder.stdin.close()
            holder.stdout.close()
            self.assertEqual(task_io.lock_held(str(root)), "free", "the record may linger; the flock does not")
            self.assertFalse(task_io.lock_holder(str(root)).get("live", False))
            self.assertEqual(cm.holder_line(str(root)), "free")


class ConnectEnvAndPaths(ConnectBase):
    def test_auth_status_runs_under_the_same_allowlist(self):
        self.configure()
        cfg = cli.read_config(cli.config_path())
        calls = []

        def spy(argv, **kw):
            calls.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true}', "")
        self.assertEqual(cli.auth_problem(cfg, spy), "")
        self.assertEqual(calls[0], cli.child_env(cfg), "claude auth status sees the child's allowlist, not os.environ")
        for secret in ("REMOTE_TASK_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(secret, calls[0])

    def test_a_directory_or_dangling_symlink_at_the_supervisor_path_is_one_line(self):
        unit = cli.service_path()
        unit.parent.mkdir(parents=True)
        unit.mkdir()
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, "is a directory, not a supervisor file" in err), (1, True))
        self.assertFalse(cli.config_path().exists())
        self.configure()
        out = self.main("verify")[1]
        self.assertIn("FAIL supervisor", out)
        self.assertIn("is a directory", out)
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, "is a directory" in err), (1, True))
        self.assertTrue(unit.is_dir(), "left alone")
        unit.rmdir()
        unit.symlink_to(self.home / "gone")
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, f"remove it by hand (rm {unit})" in err), (1, True))
        self.assertIn("dangling symlink", self.main("verify")[1])
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, "dangling symlink" in err), (1, True))
        self.assertTrue(unit.is_symlink(), "left alone")

    def test_a_key_less_config_is_one_line_in_run_and_verify(self):
        cfg = self.configure()
        good = cfg.read_text()
        cfg.write_text(good.replace("INBOX_DIR=", "INBOX_WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks INBOX_DIR" in err), (2, True))
        self.assertNotIn("Traceback", err)
        self.assertIsNone(self.session())
        cfg.write_text(good.replace("SESSION_ID=", "SESSION_WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks SESSION_ID" in err), (2, True))
        cfg.write_text(good.replace("INBOX_DIR=", "INBOX_WAS="))
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, "lacks INBOX_DIR" in out), (1, True), out)


class ConnectOccupancy(ConnectBase):
    """`verify` reports the manager's own state beside the lock, so a free lock under a live or
    respawning job never reads bare 'free'."""

    def test_verify_reads_the_lock_beside_the_managers_state(self):
        self.configure()
        work = str(self.work)
        restarting = SHOWING(LoadState="loaded", ActiveState="activating", SubState="auto-restart", UnitFileState="enabled")
        with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): restarting})):
            out = self.main("verify")[1]
        self.assertIn(f"INFO session: {work} restarting (manager activating/auto-restart, lock free)", out)
        self.assertNotIn(f"INFO session: {work} free", out)
        self.assertIn("INFO systemd ordering is asserted from KillMode=process and not witnessed", out)
        holder = hold(work, "claude-local")
        try:
            running = SHOWING(LoadState="loaded", ActiveState="active", SubState="running", UnitFileState="enabled")
            with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): running})):
                out = self.main("verify")[1]
            self.assertIn(f"INFO session: {work} held by hold pid {holder.pid}, no live claude attached (manager active/running)", out)
            with mock.patch.object(sm, "_run", side_effect=Runner({})):
                self.assertIn(f"INFO session: {work} held by hold pid {holder.pid}, no live claude attached (manager absent)", self.main("verify")[1])
        finally:
            unhold(holder)
        with mock.patch.object(sm, "_run", side_effect=Runner({})):
            self.assertIn(f"INFO session: {work} free (no job)", self.main("verify")[1])
        stopped = SHOWING(LoadState="loaded", ActiveState="inactive", SubState="dead", UnitFileState="disabled")
        with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): stopped})):
            self.assertIn(f"INFO session: {work} free (manager inactive/dead)", self.main("verify")[1])
        with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): None})):
            self.assertIn(f"INFO session: {work} free (manager unknown)", self.main("verify")[1])
        live = (0, "ai.ag2.sparrow.claude-local = {\n\tactive count = 1\n\tpid = 4242\n\tstate = running\n}\n", "")
        with mock.patch.object(cm, "is_macos", return_value=True):
            with mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): live})):
                out = self.main("verify")[1]
            self.assertIn(f"INFO session: {work} restarting (manager pid 4242, state running, lock free)", out)
            self.assertNotIn("KillMode", out)
            self.assertIn(f"INFO launchd respawns the launcher after {cm.THROTTLE_S} s; the session in its tmux pane is not in "
                          "the launcher's process group and survives a launcher death until the next launcher adopts it", out)
            with mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): (0, "\tstate = waiting\n", "")})):
                self.assertIn(f"INFO session: {work} restarting (manager state waiting, lock free)", self.main("verify")[1])
            with mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): NOT_LOADED})):
                self.assertIn(f"INFO session: {work} free (no job)", self.main("verify")[1])
            holder = hold(work, "claude-local")
            try:
                with mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): live})):
                    self.assertIn(f"INFO session: {work} held by hold pid {holder.pid}, no live claude attached (manager pid 4242, state running)",
                                  self.main("verify")[1])
            finally:
                unhold(holder)

    def test_manager_detail_reads_explicit_outputs_only(self):
        for got, want in (((0, "", ""), "loaded"), ((1, "", "odd\n"), "unknown"), (NOT_LOADED, "absent"), (None, "unknown")):
            with mock.patch.object(cm, "is_macos", return_value=True), \
                    mock.patch.object(sm, "_run", side_effect=Runner({("launchctl", "print"): got})):
                self.assertEqual(cm.manager_detail(L), want, got)
        for got, want in ((None, "unknown"), ((1, "", "bus\n"), "unknown"), ((0, "\n\n", ""), "unknown"),
                          (SHOWING(LoadState="not-found", ActiveState="inactive", SubState="dead", UnitFileState="disabled"), "absent"),
                          (SHOWING(LoadState="loaded", ActiveState="failed", SubState="failed", UnitFileState="enabled"), "failed/failed")):
            with mock.patch.object(sm, "_run", side_effect=Runner({("systemctl", "show"): got})):
                self.assertEqual(cm.manager_detail(L), want, got)


class ConnectReadme(unittest.TestCase):
    def test_the_readme_states_the_limits_and_the_fallbacks(self):
        text = " ".join((ADAPTER / "README.md").read_text().split())
        for phrase in ("`AbandonProcessGroup` false", "`ThrottleInterval` 30", "`KillMode=process`", "`ExitTimeOut` 180",
                       "`TimeoutStopSec=180`", "`SuccessfulExit: false`", "stray", "systemd ordering", "not witnessed",
                       "`/clear`", "at-most-once", "commit-unknown", "`AG2_SPARROW_MANAGED=1`", "not a security boundary",
                       "`hold --generation=<32 hex> --`", "`hitl/requirements-claude-local`", "`hitl_cl_`",
                       "`stop --confirm`", "`attach`", "`/exit`", "Keep waiting", "`AG2_INBOX_INODE`",
                       "`ag2-sparrow-claude-local inbox`"):
            self.assertIn(phrase, text, phrase)
        for gone in ("held, launcher gone", "trampoline **parent**", "group kill", "sweeps the group", "debug-run",
                     "KillMode=control-group", "as its **direct child**", "stream-json"):
            self.assertNotIn(gone, text, gone)


class ConnectSourceRules(unittest.TestCase):
    """Source pins: no group signal or descriptor passing in the package; no lease symbol anywhere the
    design once named it; no review-round, PR or reviewer reference in the adapter's text."""

    def test_the_adapter_its_shared_modules_docs_and_tests_carry_no_review_or_person_references(self):
        pattern = re.compile("#" + "5695|" + r"\bv\d+" + r"\.\d+\b|\b[BKQR][1-9](?:-[a-e])?\b|" + "|".join(
            ("Ke" + "wei", "Qing" + "yun", "Su" + "san")))
        repo = PKG.parents[1]
        files = sorted((PKG / "ag2_sparrow" / "adapters" / "claude_code").glob("*.py"))
        files += sorted((PKG / "ag2_sparrow").glob("claude_local_*.py")) + [ADAPTER / "README.md", *SUITE_FILES]
        files += [Path(cm.__file__), Path(task_io.__file__)]
        files += [repo / "tests" / n for n in ("ag2-sparrow-claude-local-bridge.test.py", "ag2-sparrow-tmux-vendored.test.py",
                                                "ag2-sparrow-claude-local-wheel.test.py", "hitl-schema.test.py",
                                                "holder-lock.test.py")]
        found = ["%s: %s" % (path.name, m.group(0)) for path in files for m in pattern.finditer(path.read_text())]
        self.assertEqual(found, [])

    def test_no_killpg_anywhere_in_the_package_and_no_group_or_descriptor_passing_in_the_lifecycle(self):
        for path in sorted((PKG / "ag2_sparrow").rglob("*.py")):
            self.assertNotIn("killpg", path.read_text(), path)
        src = Path(cm.__file__).read_text()
        for token in ("start_new_session", "process_group", "pass_fds", "setsid", "setpgid", "os.kill(", "TRAMPOLINE"):
            self.assertNotIn(token, src, token)

    def test_no_lease_symbol_survives_in_the_module_the_adapter_the_docs_or_the_tests(self):
        words = ["child_" + "lease", "end_" + "lease", "lease" + "_used", "Owner" + "." + "leased"]
        pattern = re.compile("|".join(map(re.escape, words)) + r"|\." + "leased" + r"\b")
        repo = PKG.parents[1]
        files = [Path(cm.__file__), Path(task_io.__file__), Path(holder_lock.__file__), Path(cli.__file__),
                 ADAPTER / "README.md", *SUITE_FILES]
        files += [p for p in (repo / "src" / "holder_lock.py", repo / "tests" / "holder-lock.test.py",
                              repo / "docs" / "design-holder-lock.md") if p.exists()]
        for path in files:
            self.assertIsNone(pattern.search(path.read_text()), path)
        self.assertFalse(hasattr(holder_lock, words[0]))
        self.assertFalse(hasattr(holder_lock, words[1]))
        self.assertFalse(hasattr(task_io, "lease_fd") or hasattr(task_io, "release_after_reap"))


class HoldProcess(ConnectBase):
    """`hold` is the pane's thin parent: the root taken and published, the dead generation
    recovered under that flock, `tasks` opened O_NOFOLLOW from the held root fd and bound by inode, the
    exec shim gated on one go byte, the consumer attached before the byte is written."""

    @staticmethod
    def start_hold(case, linger=300, patch="", **extra):
        """A hold whose consumer is attached and live: (proc, consumer pid)."""
        from ag2_sparrow import task_io
        proc = case.hold_proc(patch=patch, linger=linger, **extra)
        consumer = wait_for(lambda: (task_io.holder_consumer(str(case.work)) or {}).get("live") and task_io.holder_consumer(str(case.work)), 20)
        case.assertTrue(consumer, "the consumer attached")
        case.assertTrue(wait_for(lambda: case.session() is not None, 10), "the gated child started")
        return proc, consumer["pid"]

    def test_hold_takes_the_root_attaches_the_gated_child_and_binds_the_inbox_inode(self):
        from ag2_sparrow import task_io
        self.configure()
        proc, consumer = self.start_hold(self, linger=2)
        try:
            rec = holder_lock.read_holder(str(self.work)).record
            session = self.session()
            self.assertEqual((rec["adapter"], rec["pid"], rec["consumer"]["pid"], rec["owner_pid"]),
                             ("claude-local", consumer, consumer, proc.pid), "the consumer is the fencing pid, hold the owner")
            self.assertEqual(session["pid"], consumer, "the consumer IS the claude process: the shim execs in place")
            self.assertEqual(session["ppid"], proc.pid, "claude is hold's direct child")
            st = os.stat(self.inbox)
            self.assertEqual(Path(session["cwd"]).resolve(), self.inbox.resolve())
            self.assertEqual((session["env"]["AG2_INBOX_INODE"], session["env"]["AG2_INBOX_DIR"], session["env"]["AG2_RESULTS_DIR"]),
                             ("%d:%d" % (st.st_dev, st.st_ino), ".", "../results"), "the inode from the held fd, relative task I/O")
            self.assertEqual(session["argv"][1:], ["--plugin-dir", str(self.plugin), "--permission-mode", "bypassPermissions",
                                                   "--session-id", SESSION_ID], "no transcript yet: --session-id")
            self.assertEqual(session["env"][hold_mod.ENV_SESSION_ID], SESSION_ID)
            self.assertEqual([fd for fd in session["fifos"] if fd > 2], [],
                             "no pipe beyond stdio reaches claude: the go fd is closed before the exec")
            self.assertEqual(task_io.occupancy(str(self.work)), "held")
            self.assertEqual(inbox.prepare_root(str(self.work)), "", "the work dir is prepared and fenced claude-local/1")
            self.assertEqual((self.work / "state" / "format").read_text().strip(), "claude-local/1")
        finally:
            out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("\033]2;t", out, "the pane title is set to a fresh token before the root is taken")
        self.assertTrue(re.search(r"\033\]2;t[0-9a-f]{32}\007", out))
        self.assertEqual(task_io.occupancy(str(self.work)), "free", "released after the child")

    def test_hold_resumes_by_id_once_the_transcript_exists(self):
        self.configure()
        projects = self.home / ".claude-sutando" / "projects" / "-w"
        projects.mkdir(parents=True)
        (projects / (SESSION_ID + ".jsonl")).write_text("{}\n")
        rc, _, err = self.hold_cmd(linger=0)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.session()["argv"][-2:], ["--resume", SESSION_ID])

    def test_hold_refuses_a_bad_nonce_a_missing_environment_and_a_held_root(self):
        self.configure()
        r = subprocess.run([sys.executable, "-c", covered_code(HOLD_DRIVER % (str(PKG), "", "zz"))], env=self.hold_env(),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual((r.returncode, "32 hex" in r.stderr), (2, True), r.stderr)
        env = self.hold_env()
        env.pop(hold_mod.ENV_ROOT)
        r = subprocess.run([sys.executable, "-c", covered_code(HOLD_DRIVER % (str(PKG), "", NONCE))], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, "must be set in the pane's environment" in r.stderr), (2, True), r.stderr)
        holder = hold(str(self.work), "claude-local")
        try:
            rc, _, err = self.hold_cmd()
            self.assertEqual((rc, "refusing to start" in err), (75, True), err)
            self.assertIsNone(self.session())
        finally:
            unhold(holder)
        shutil.rmtree(self.inbox)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        self.inbox.symlink_to(elsewhere)
        rc, _, err = self.hold_cmd()
        self.assertEqual((rc, "refusing to start" in err), (2, True), err)
        self.assertIsNone(self.session(), "a tasks symlink is refused before any spawn")
        from ag2_sparrow import task_io
        self.assertEqual(task_io.occupancy(str(self.work)), "free")

    def test_the_shim_execs_only_on_exactly_one_go_byte(self):
        """EOF, a short read, a read error or a wrong byte exits without exec."""
        def run_shim(feed, close=True, fd_override=None):
            r_, w_ = os.pipe()
            argv = [sys.executable, "-c", hold_mod.SHIM, str(fd_override if fd_override is not None else r_), "/bin/echo", "EXECED"]
            p = subprocess.Popen(argv, pass_fds=(r_,), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            os.close(r_)
            if feed:
                os.write(w_, feed)
            if close:
                os.close(w_)
            out, _ = p.communicate(timeout=30)
            return p.returncode, out.strip()
        self.assertEqual(run_shim(b""), (3, ""), "EOF: exit without exec")
        self.assertEqual(run_shim(b"x"), (4, ""), "a wrong byte: exit without exec")
        self.assertEqual(run_shim(hold_mod.GO), (0, "EXECED"), "the one byte: execv")
        self.assertEqual(run_shim(b"", fd_override=99)[0], 5, "a read error: exit without exec")

    def test_kill_9_hold_after_the_spawn_and_before_attach_starts_nothing_and_frees_the_root(self):
        """The shim waits on the pipe; hold's death closes it; EOF, no exec, no consumer."""
        from ag2_sparrow import task_io
        self.configure()
        marker = self.home / "paused"
        patch = ("real = task_io.attach_consumer\n"
                 "def pause(*a, **k):\n    open(%r, 'w').close(); time.sleep(60)\n"
                 "task_io.attach_consumer = pause\n" % str(marker))
        proc = self.hold_proc(patch=patch, linger=30)
        try:
            self.assertTrue(wait_for(marker.exists, 20), "hold paused between the spawn and attach_consumer")
            self.assertEqual(task_io.occupancy(str(self.work)), "held", "the owner fences the root in the window")
            self.assertIsNone(task_io.holder_consumer(str(self.work)), "no consumer yet")
            shim_pids = [int(line.split()[0]) for line in subprocess.run(["ps", "-o", "pid=,ppid=", "-ax"], capture_output=True, text=True).stdout.splitlines() if line.split()[1:] == [str(proc.pid)]]
            self.assertEqual(len(shim_pids), 1, "one gated shim is waiting")
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
            self.assertTrue(wait_gone(shim_pids[0], 10), "EOF on the pipe: the shim exited without exec")
            self.assertIsNone(self.session(), "claude never started")
            self.assertEqual(task_io.occupancy(str(self.work)), "free", "the dead owner with no consumer frees the root")
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
            proc.communicate()

    def test_kill_9_hold_after_the_record_and_before_the_go_byte_leaves_a_consumer_that_never_started(self):
        from ag2_sparrow import task_io
        self.configure()
        marker = self.home / "attached"
        patch = ("real = task_io.attach_consumer\n"
                 "def attach_then_pause(*a, **k):\n    out = real(*a, **k); open(%r, 'w').close(); time.sleep(60); return out\n"
                 "task_io.attach_consumer = attach_then_pause\n" % str(marker))
        proc = self.hold_proc(patch=patch, linger=30)
        try:
            self.assertTrue(wait_for(marker.exists, 20))
            consumer = task_io.holder_consumer(str(self.work))
            self.assertTrue(consumer and consumer["live"], "the record names the shim as the consumer")
            self.assertEqual(task_io.occupancy(str(self.work)), "held")
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
            self.assertTrue(wait_gone(consumer["pid"], 10), "the shim read EOF and exited without exec")
            self.assertIsNone(self.session())
            self.assertEqual(wait_for(lambda: task_io.occupancy(str(self.work)) == "free" and "free", 10), "free",
                             "a verified-dead consumer frees the root; while it lived the root read held")
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
            proc.communicate()

    def test_a_failed_attach_reaps_the_shim_and_hold_exits_without_a_consumer(self):
        from ag2_sparrow import task_io
        self.configure()
        patch = "task_io.attach_consumer = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('record changed'))\n"
        rc, _, err = self.hold_cmd(patch=patch, linger=30)
        self.assertEqual((rc, "no consumer: record changed" in err), (2, True), err)
        self.assertIsNone(self.session(), "the shim was reaped unstarted")
        self.assertEqual(task_io.occupancy(str(self.work)), "free")

    def test_hold_returning_beside_a_live_consumer_leaves_the_record_and_the_root_held(self):
        """The kill-timeout path: hold returns while its claude still lives; release refuses, so the
        record stays and the root never reads free beside the live session."""
        from ag2_sparrow import task_io
        self.configure()
        (self.home / "claude-ignore-term").write_text("1")
        (self.home / "claude-linger").write_text("60")
        log = self.home / "hold.err"
        with open(log, "w") as stream:      # a file: the surviving claude would keep a pipe open
            rc = subprocess.run([sys.executable, "-c", covered_code(HOLD_DRIVER % (str(PKG), "hold._supervise = lambda child, state: 9\n",
                                                                                    NONCE))],
                                env=self.hold_env(), stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                timeout=60).returncode
        err = log.read_text()
        consumer = task_io.holder_consumer(str(self.work))
        self.addCleanup(lambda: consumer and alive(consumer["pid"]) and os.kill(consumer["pid"], signal.SIGKILL))
        self.assertEqual(rc, 9, err)
        self.assertIn("root stays held", err)
        self.assertTrue(consumer and consumer["live"], "the record still names the live consumer")
        self.assertEqual(task_io.occupancy(str(self.work)), "held", "hold exited; its live claude still fences")
        with self.assertRaises(task_io.LockRefused):
            task_io.acquire_lock(str(self.work), "sibling")
        os.kill(consumer["pid"], signal.SIGKILL)
        self.assertTrue(wait_gone(consumer["pid"], 10))
        self.assertTrue(wait_for(lambda: task_io.occupancy(str(self.work)) == "free", 10))

    def test_kill_9_hold_with_a_hup_ignoring_consumer_never_shows_a_free_root_to_a_sibling_loop(self):
        """No root-free/live-consumer overlap and zero sibling acquisitions until the
        consumer exits; the loop runs through the gate windows too (it starts before hold)."""
        from ag2_sparrow import task_io
        self.configure()
        (self.home / "claude-ignore-hup").write_text("1")
        (self.home / "claude-ignore-term").write_text("1")
        loop = subprocess.Popen([sys.executable, "-c", covered_code((
            "import sys, time, json; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
            "root = %r; acquired = 0; frees = 0; seen = set(); print('looping', flush=True)\n"
            "while True:\n"
            "    line = sys.stdin.readline()\n"
            "    if line.strip() == 'stop': break\n"
            "    occ = task_io.occupancy(root); seen.add(occ)\n"
            "    if occ == 'free': frees += 1\n"
            "    try:\n        o = task_io.acquire_lock(root, 'sibling'); acquired += 1; task_io.release_lock(o)\n"
            "    except task_io.LockRefused: pass\n"
            "    print(json.dumps({'acquired': acquired, 'frees': frees, 'seen': sorted(seen)}), flush=True)\n"
        ) % (str(PKG), str(self.work)))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(loop.stdout.readline().strip(), "looping")

        def probe():
            loop.stdin.write("go\n"); loop.stdin.flush()
            return json.loads(loop.stdout.readline())
        proc = self.hold_proc(linger=60)
        try:
            consumer = wait_for(lambda: (task_io.holder_consumer(str(self.work)) or {}).get("live") and task_io.holder_consumer(str(self.work)), 20)
            self.assertTrue(consumer)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
            self.assertTrue(alive(consumer["pid"]), "the HUP-ignoring consumer outlives hold")
            for _ in range(40):
                got = probe()
                self.assertEqual((got["acquired"], got["frees"]), (0, 0), got)
                time.sleep(0.02)
            self.assertTrue(task_io.lock_held(str(self.work)) in ("held",), "sticky: the live consumer holds the root")
            os.kill(consumer["pid"], signal.SIGKILL)
            self.assertTrue(wait_gone(consumer["pid"], 10))
            got = wait_for(lambda: probe()["acquired"] and probe(), 10)
            self.assertTrue(got and got["acquired"] >= 1, "once the consumer is gone the sibling acquires")
        finally:
            loop.stdin.write("stop\n"); loop.stdin.flush()
            loop.wait(timeout=30)
            loop.stdin.close()
            loop.stdout.close()
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
            proc.communicate()

    def test_hold_recovers_the_dead_generations_claims_under_its_flock_and_leaves_a_live_one(self):
        """Recovery runs after acquire and publish, before the spawn; only claims of a
        generation that verifies dead are touched: TURN_CONFIRMED without a blob answers interrupted."""
        from ag2_sparrow import task_lifecycle as tl
        self.configure()
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        dead = subprocess.Popen(["sleep", "0"]); dead.wait()
        lc = tl.Lifecycle(str(self.work), lineage=lambda *a: True)
        for task_id, gen in (("task-dead", {"pid": dead.pid, "start": "gone"}),
                             ("task-live", {"pid": os.getpid(), "start": holder_lock.start_time(os.getpid())})):
            (self.inbox / (task_id + ".txt")).write_text("task: hi\n")
            lc.prepare(task_id, inbox.INTERRUPTED_BODY, mode=tl.LOCAL, generation=gen)
            token = lc.authorize_call(task_id, 1, text_hash="h")
            lc.confirm_turn(task_id, "t1", token, "h")
        rc, _, err = self.hold_cmd(linger=0)
        self.assertEqual(rc, 0, err)
        self.assertIn("recovered 1 claim(s) of the dead generation", err)
        self.assertEqual((self.results / "task-dead.txt").read_text(), inbox.INTERRUPTED_BODY)
        self.assertEqual(lc.claims.load("task-dead")["phase"], tl.SETTLED)
        self.assertEqual(lc.claims.load("task-live")["phase"], tl.TURN_CONFIRMED, "the live generation's claim is untouched")
        self.assertFalse((self.results / "task-live.txt").exists())


if __name__ == "__main__":
    unittest.main()
