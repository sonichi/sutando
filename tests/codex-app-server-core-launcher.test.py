#!/usr/bin/env python3
"""The Codex core launcher's SUTANDO_CODEX_APP_SERVER switch, in the fixture of
tests/codex-core-launcher.test.py (which runs with the switch at 0): turned off the launcher
issues the tmux calls it issued before the switch existed; on (the default) the core session
holds the seat and no helper."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = "src/agent/codex/cli/start-cli.sh"
SEAT_DIR = "src/agent/codex/app-server"
BEGIN, END = "# >>> codex-app-server-seat", "# <<< codex-app-server-seat"

_spec = importlib.util.spec_from_file_location("codex_core_launcher_fixture", REPO / "tests" / "codex-core-launcher.test.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)

# A tmux that keeps the core session's state in files: absent, a TUI, a seat, or a session
# whose core window is gone. Every call is logged as the launcher made it.
SEAT_TMUX = r'''#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_LOG"
[ "${1:-}" = -S ] && shift 2
S="$TMUX_STATE"
seat_ready() {  # the seat's ready file, as seat.py writes it once its app-server and thread are up
  [ -n "${FAKE_SEAT_INIT_FAILS:-}" ] && return 0
  local prev=""
  for a in "$@"; do [ "$prev" = --host ] && echo $$ > "${a%/*}/ready"; prev="$a"; done
}
case "${1:-}" in
  new-session)
    if [ "${4:-}" = sutando-core ]; then
      kind=codex
      for a in "$@"; do case "$a" in */codex-seat) kind=seat ;; esac; done
      echo "$kind" > "$S.core"
      [ "$kind" = seat ] && seat_ready "$@"
      for a in "$@"; do [ "$a" = "#{session_id}" ] && echo '$7'; done
    fi
    [ "${4:-}" = sutando-core-watcher ] && touch "$S.watcher"
    exit 0 ;;
  new-window)
    echo seat > "$S.core"; seat_ready "$@"
    for a in "$@"; do [ "$a" = "#{window_id} #{window_index}" ] && { echo "@9 0"; exit 0; }; done
    echo 0; exit 0 ;;
  list-sessions)
    [ -n "${FAKE_INSPECT_FAILS:-}" ] && exit 1
    [ -f "$S.core" ] && echo '$7'; exit 0 ;;
  has-session)
    case "${3:-}" in =sutando-core|'$7') [ -f "$S.core" ] && exit 0 ;; esac
    [ "${3:-}" = =sutando-core-watcher ] && [ -f "$S.watcher" ] && exit 0
    exit 1 ;;
  kill-session)
    [ -n "${FAKE_KILL_FAILS:-}" ] && exit 1
    case "${3:-}" in =sutando-core|'$7') rm -f "$S.core" ;; esac
    [ "${3:-}" = =sutando-core-watcher ] && rm -f "$S.watcher"
    exit 0 ;;
  kill-window) [ -n "${FAKE_KILL_FAILS:-}" ] && exit 1; rm -f "$S.core"; exit 0 ;;
  list-panes)
    [ -f "$S.core" ] || exit 1
    kind="$(cat "$S.core")"
    [ "$kind" = gone ] && exit 0
    cmd=codex
    [ "$kind" = seat ] && cmd=/engine/src/agent/codex/app-server/codex-seat
    for a in "$@"; do case "$a" in *'|'*) echo "@0|0|main|$cmd --arg"; exit 0 ;; esac; done
    echo "@0 0 $cmd --arg"
    exit 0 ;;
  list-windows)
    for a in "$@"; do [ "$a" = "#{window_id}" ] && { [ -n "${FAKE_INSPECT_FAILS:-}" ] && exit 1; [ -f "$S.core" ] && echo @9; exit 0; }; done
    echo 0; exit 0 ;;
  show-environment) echo "SUTANDO_CORE_RUNTIME=codex"; exit 0 ;;
  display-message)
    for a in "$@"; do [ "$a" = "#{window_id}" ] && { echo @9; exit 0; }; done
    echo 123; exit 0 ;;
esac
exit 0
'''


def strip_seat_blocks(text: str) -> str:
    """The launcher as it reads without the switch: every marked block removed, markers included."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == BEGIN:
            skip = True
        elif line.strip() == END:
            skip = False
        elif not skip:
            out.append(line)
    return "".join(out)


class Fixture:
    """One temp engine checkout with the real launcher, built by the core-launcher suite's setUp."""

    def __init__(self, case: unittest.TestCase, seat_tmux: bool = False):
        self.h = _fixture.CodexCoreLauncherTests("run_launcher")
        self.h.setUp()
        case.addCleanup(self.h.tearDown)
        self.root, self.tmp = self.h.root, Path(self.h.tmp.name)
        dest = self.root / SEAT_DIR
        dest.mkdir(parents=True)
        for name in ("codex-seat", "seat-launch.sh"):
            shutil.copy2(REPO / SEAT_DIR / name, dest / name)
        (dest / "seat.py").symlink_to(REPO / SEAT_DIR / "seat.py")
        self.workspace = self.root / "workspace"
        self.state = self.tmp / "tmux-killed"
        if seat_tmux:
            (self.h.bin / "tmux").write_text(SEAT_TMUX)
            (self.h.bin / "tmux").chmod(0o755)

    def core(self, kind: str) -> None:
        Path(f"{self.state}.core").write_text(kind + "\n")

    def run(self, *args, **env):
        return self.h.run_launcher(*args, env_extra=env, launcher=LAUNCHER)

    def calls(self) -> str:
        """The tmux calls, made comparable across two fixtures: the temp dir is replaced, and so is
        the notifier version's file checksum, which covers the fixture's own paths."""
        calls = self.h._tmux_calls().replace(str(self.tmp), "<TMP>")
        return re.sub(r"(SUTANDO_NOTIFIER_VERSION=)\d+-\d+-", r"\1<FILES>-", calls)

    def declaration(self) -> Path:
        return self.workspace / "state" / "codex-seat" / "sutando-core" / "host.json"


class FlagOff(unittest.TestCase):
    """Off (0, or any value but 1), the launcher's tmux calls are those of the launcher without
    the switch. The fixture runs with 0."""

    def compare(self, *args, **env):
        current = Fixture(self)
        before = Fixture(self)
        launcher = before.root / LAUNCHER
        stripped = strip_seat_blocks(launcher.read_text())
        self.assertNotIn("seat", stripped.lower(), "the stripped launcher is the launcher without the switch")
        launcher.write_text(stripped)
        self.assertNotEqual((current.root / LAUNCHER).read_text(), stripped)
        got, want = current.run(*args, **env), before.run(*args, **env)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((got.returncode, got.stdout.replace(str(current.tmp), "<TMP>")),
                         (want.returncode, want.stdout.replace(str(before.tmp), "<TMP>")))
        self.assertEqual(current.calls(), before.calls())
        self.assertFalse(current.declaration().exists())
        return current.calls()

    def test_a_fresh_launch_creates_the_same_sessions_with_the_same_commands(self):
        calls = self.compare(SUTANDO_CORE_MODEL="gpt-test")
        self.assertIn("new-session -d -s sutando-core ", calls)
        self.assertIn("codex -C", calls)
        self.assertIn("new-session -d -s sutando-core-watcher", calls)
        self.assertNotIn("codex-seat", calls)

    def test_any_value_but_one_is_off(self):
        for value in ("0", "false", "no"):
            with self.subTest(value=value):
                self.assertNotIn("codex-seat", self.compare(SUTANDO_CODEX_APP_SERVER=value))

    def test_a_restart_issues_the_same_calls(self):
        calls = self.compare("--restart", TMUX_ACTIVE_RUNTIME="codex")
        self.assertIn("kill-session -t =sutando-core-watcher", calls)
        self.assertIn("new-session -d -s sutando-core ", calls)

    def test_an_already_running_core_issues_the_same_calls(self):
        self.assertNotIn("new-session -d -s sutando-core ", self.compare(TMUX_ACTIVE_RUNTIME="codex"))


class FlagOn(unittest.TestCase):
    ON = {"SUTANDO_CODEX_APP_SERVER": "1"}

    def _switched(self, f, model):
        state = f.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "model-switch.json").write_text(json.dumps({"model": model, "runtime": "codex", "accepted": True}))

    def test_the_cores_own_switch_is_its_launch_model_when_none_is_set(self):
        f = Fixture(self, seat_tmux=True)
        self._switched(f, "gpt-switched")
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertIn("--model gpt-switched", line)

    def test_a_launch_model_set_for_this_launch_wins_over_the_switch_record(self):
        f = Fixture(self, seat_tmux=True)
        self._switched(f, "gpt-switched")
        result = f.run(SUTANDO_CORE_MODEL="gpt-explicit", **self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertIn("--model gpt-explicit", line)
        self.assertNotIn("gpt-switched", line)

    def test_a_pending_pick_is_never_the_seats_launch_model(self):
        # The seat starts on the last model Codex took and tries the pick on a turn (seat.py).
        f = Fixture(self, seat_tmux=True)
        state = f.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "model-switch.json").write_text(json.dumps(
            {"model": "gpt-pending", "runtime": "codex", "status": "pending", "working": "gpt-good", "rev": 4}))
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertIn("--model gpt-good", line)
        self.assertNotIn("gpt-pending", line)
        self.assertNotIn("SUTANDO_CORE_MODEL", line, "a model read from the record is not pinned in the session")

    def test_a_pending_pick_with_no_working_model_starts_the_seat_on_codexs_default(self):
        f = Fixture(self, seat_tmux=True)
        state = f.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "model-switch.json").write_text(json.dumps(
            {"model": "gpt-pending", "runtime": "codex", "status": "pending", "rev": 1}))
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertNotIn("--model", line)

    def test_an_explicit_launch_model_carries_the_revision_it_outranks(self):
        f = Fixture(self, seat_tmux=True)
        state = f.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "model-switch.json").write_text(json.dumps(
            {"model": "gpt-pending", "runtime": "codex", "status": "pending", "working": "gpt-good", "rev": 4}))
        result = f.run(SUTANDO_CORE_MODEL="gpt-explicit", **self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertIn("--model gpt-explicit", line)
        self.assertIn("-e SUTANDO_CORE_MODEL_REV=4", line)

    def test_an_inherited_revision_is_dropped_when_the_model_comes_from_the_record(self):
        f = Fixture(self, seat_tmux=True)
        state = f.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "model-switch.json").write_text(json.dumps(
            {"model": "gpt-a", "runtime": "codex", "status": "accepted", "rev": 2}))
        result = f.run(SUTANDO_CORE_MODEL_REV="9", **self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertIn("--model gpt-a", line)
        self.assertNotIn("SUTANDO_CORE_MODEL_REV", line)

    def test_a_refused_pick_is_not_the_launch_model(self):
        f = Fixture(self, seat_tmux=True)
        state = f.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "model-switch.json").write_text(json.dumps(
            {"model": "gpt-bad", "runtime": "codex", "status": "rejected", "working": "gpt-good", "rev": 2}))
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = [ln for ln in f.calls().splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertIn("--model gpt-good", line)
        self.assertNotIn("gpt-bad", line)

    def test_a_claude_cores_record_is_not_a_codex_launch_model(self):
        f = Fixture(self, seat_tmux=True)
        (f.workspace / "state").mkdir(parents=True, exist_ok=True)
        (f.workspace / "state" / "model-switch.json").write_text(json.dumps({"model": "opus", "accepted": True}))
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--model", f.calls())

    def test_a_fresh_launch_starts_the_seat_in_the_core_session_and_no_helper_session(self):
        f = Fixture(self, seat_tmux=True)
        result = f.run(SUTANDO_CORE_MODEL="gpt-test", SUTANDO_STOP_INTERRUPT="1", **self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = f.calls()
        launch = [ln for ln in calls.splitlines() if ln.startswith("-S <TMP>/tmux.sock new-session")]
        self.assertEqual(len(launch), 1, calls)
        line = launch[0]
        self.assertIn("new-session -d -s sutando-core -e SUTANDO_CORE_SESSION=1 -e SUTANDO_CORE_RUNTIME=codex", line)
        self.assertRegex(line, r" \S+/src/agent/codex/app-server/codex-seat --host \S+/state/codex-seat/sutando-core/host\.json ")
        self.assertIn(f"--workspace {f.workspace.resolve()} --seat sutando-core --codex {f.h.bin}/codex ".replace(str(f.tmp), "<TMP>"), line)
        self.assertIn('-c web_search="live" -c check_for_update_on_startup=false -c project_doc_max_bytes=131072 '
                      "--model gpt-test --watcher", line)
        for env in ("-e SUTANDO_CODEX_SEAT=1", "-e SUTANDO_STANDBY_MODE=type", "-e SUTANDO_STOP_INTERRUPT=1",
                    "-e SUTANDO_TMUX_SESSION=sutando-core", "-e SUTANDO_CORE_MODEL=gpt-test", "-e CODEX_HOME="):
            self.assertIn(env, line)
        self.assertNotIn("READY", calls, "no bootstrap prompt is typed into a seat")
        self.assertNotIn("--ask-for-approval", line)
        for helper in ("sutando-core-watcher", "sutando-core-observer"):
            self.assertNotIn(f"new-session -d -s {helper}", calls)
        decl = json.loads(f.declaration().read_text())
        self.assertEqual(decl["schema"], "ag2-sparrow.supervised-host/1")
        self.assertEqual(decl["inbox"], str(f.workspace.resolve() / "tasks"))
        self.assertEqual(decl["results"], str(f.workspace.resolve() / "results"))
        self.assertEqual(decl["cwd"], str(f.root.resolve()))
        self.assertNotIn("payloads", decl)
        self.assertNotIn("stage_command", decl)
        self.assertIn("Started sutando-core detached", result.stdout)

    def test_the_watcher_child_can_be_turned_off(self):
        f = Fixture(self, seat_tmux=True)
        self.assertEqual(f.run(SUTANDO_SEAT_WATCHER="0", **self.ON).returncode, 0)
        self.assertNotIn("--watcher", f.calls())

    def test_a_running_seat_is_reused_and_a_notifier_left_by_a_tui_core_is_ended(self):
        f = Fixture(self, seat_tmux=True)
        f.core("seat")
        Path(f"{f.state}.watcher").touch()
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = f.calls()
        self.assertIn("sutando-core already running (codex).", result.stdout)
        self.assertNotIn("new-session", calls)
        self.assertNotIn("new-window", calls)
        self.assertIn("kill-session -t =sutando-core-watcher", calls)

    def test_a_running_tui_core_is_not_taken_over_without_a_restart(self):
        f = Fixture(self, seat_tmux=True)
        f.core("codex")
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("not the Codex app-server seat", result.stderr)
        self.assertIn("--restart", result.stderr)
        self.assertNotIn("new-window", f.calls())
        self.assertNotIn("new-session", f.calls())

    def test_a_session_that_lost_its_seat_window_is_healed_with_a_seat(self):
        f = Fixture(self, seat_tmux=True)
        f.core("gone")
        result = f.run(**self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("healing the core window", result.stderr)
        window = [ln for ln in f.calls().splitlines() if " new-window " in ln]
        self.assertEqual(len(window), 1)
        self.assertRegex(window[0], r"-e SUTANDO_CODEX_SEAT=1 .* \S+/codex-seat --host ")
        self.assertNotIn("new-session", f.calls())

    def test_restart_moves_a_tui_core_onto_the_seat(self):
        f = Fixture(self, seat_tmux=True)
        f.core("codex")
        result = f.run("--restart", **self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = f.calls()
        self.assertIn("kill-window -t @0", calls)
        self.assertLess(calls.index("kill-window -t @0"), calls.index("new-session -d -s sutando-core "))
        self.assertRegex(calls, r"new-session -d -s sutando-core .*codex-seat --host ")

    def test_restart_with_the_flag_removed_replaces_the_seat_with_the_tui_and_its_helpers(self):
        f = Fixture(self, seat_tmux=True)
        self.assertEqual(f.run(**self.ON).returncode, 0)
        self.assertTrue(f.declaration().exists())
        f.h.log.unlink()
        result = f.run("--restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = f.calls()
        self.assertIn("kill-window -t @0", calls)
        launch = re.findall(r"new-session -d -s sutando-core .*", calls)
        self.assertEqual(len(launch), 1)
        self.assertIn(" codex -C ", launch[0])
        self.assertNotIn("codex-seat", launch[0])
        self.assertIn("new-session -d -s sutando-core-watcher", calls)
        self.assertFalse(f.declaration().exists(), "the seat's declaration goes with it")

    def test_restart_of_a_seat_with_the_flag_still_on_starts_a_fresh_seat(self):
        f = Fixture(self, seat_tmux=True)
        self.assertEqual(f.run(**self.ON).returncode, 0)
        f.h.log.unlink()
        result = f.run("--restart", **self.ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kill-window -t @0", f.calls())
        self.assertRegex(f.calls(), r"new-session -d -s sutando-core .*codex-seat --host ")
        self.assertTrue(f.declaration().exists())



class PersistedChoice(unittest.TestCase):
    """`core.codex_app_server` (what `sutando-config.sh codex-app-server-select` writes without a
    desktop) decides every launch that does not set the variable (on when nothing is chosen); a
    set variable overrides it. An empty variable is unset."""

    UNSET = {"SUTANDO_CODEX_APP_SERVER": ""}

    def test_with_nothing_chosen_the_core_is_a_seat(self):
        f = Fixture(self, seat_tmux=True)
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(f.calls(), r"new-session -d -s sutando-core .*-e SUTANDO_CODEX_SEAT=1 .*codex-seat --host ")
        self.assertNotIn("new-session -d -s sutando-core-watcher", f.calls())

    def test_chosen_off_the_core_is_the_tui(self):
        f = Fixture(self, seat_tmux=True)
        self.choose(f, False)
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex-seat", f.calls())
        self.assertIn("new-session -d -s sutando-core-watcher", f.calls())
        self.assertFalse(f.declaration().exists())

    def choose(self, f: Fixture, on: bool) -> None:
        (f.workspace / "sutando.config.local.json").write_text(json.dumps({"core": {"codex_app_server": on}}))

    def test_a_launch_without_the_variable_follows_the_persisted_choice(self):
        f = Fixture(self, seat_tmux=True)
        self.choose(f, True)
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(f.calls(), r"new-session -d -s sutando-core .*-e SUTANDO_CODEX_SEAT=1 .*codex-seat --host ")
        self.assertTrue(f.declaration().exists())

    def test_a_set_variable_overrides_the_persisted_choice(self):
        f = Fixture(self, seat_tmux=True)
        self.choose(f, True)
        result = f.run(SUTANDO_CODEX_APP_SERVER="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex-seat", f.calls())
        self.assertIn("new-session -d -s sutando-core-watcher", f.calls())

    def test_a_seat_whose_app_server_never_comes_up_leaves_intake_shut(self):
        f = Fixture(self, seat_tmux=True)
        sentinel = f.workspace / "state" / "shutdown.sentinel"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"reason": "test", "ts": 0}\n')
        result = f.run(FAKE_SEAT_INIT_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertRegex(f.calls(), r"new-session -d -s sutando-core .*codex-seat --host ")
        self.assertIn("did not report ready", result.stderr)
        self.assertTrue(sentinel.exists(), "intake is not reopened before the seat is ready")
        self.assertIn("kill-session -t $7", f.calls(), "the exact seat started is torn down, by its id")
        self.assertNotIn("kill-session -t =sutando-core", f.calls())
        # The next launch therefore starts a seat and checks readiness again, not a fast return.
        f.h.log.unlink()
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("already running", result.stdout)
        self.assertRegex(f.calls(), r"new-session -d -s sutando-core .*codex-seat --host ")
        self.assertFalse(sentinel.exists())

    def test_a_healed_seat_window_that_never_comes_up_is_removed_and_intake_stays_shut(self):
        f = Fixture(self, seat_tmux=True)
        f.core("gone")
        sentinel = f.workspace / "state" / "shutdown.sentinel"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"reason": "test", "ts": 0}\n')
        result = f.run(FAKE_SEAT_INIT_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("new-window", f.calls())
        self.assertIn("kill-window -t @9", f.calls(), "the healed window is stopped by its id")
        self.assertTrue(sentinel.exists())

    def held_launch(self):
        """A seat that never reports ready and cannot be stopped: the launch holds intake for it."""
        f = Fixture(self, seat_tmux=True)
        sentinel = f.workspace / "state" / "shutdown.sentinel"
        result = f.run(FAKE_SEAT_INIT_FAILS="1", FAKE_KILL_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not stop the unready Codex app-server seat (session $7: alive)", result.stderr)
        self.assertTrue(sentinel.exists(), "this launch shut intake itself")
        held = f.declaration().parent / "intake-held"
        self.assertTrue(held.read_text().strip(), "the token of the stop this launch wrote")
        return f, sentinel, held

    def test_a_seat_that_cannot_be_stopped_fails_the_launch_loudly_with_intake_shut(self):
        f, sentinel, held = self.held_launch()
        # The surviving seat is not accepted by the next launch until it reports ready...
        result = f.run(FAKE_SEAT_INIT_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not ready — not accepting it", result.stderr)
        self.assertTrue(sentinel.exists())
        # ...and once it is, the stop this launcher wrote is cleared.
        (f.declaration().parent / "ready").write_text("1\n")
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already running", result.stdout)
        self.assertFalse(sentinel.exists())
        self.assertFalse(held.exists())

    def test_a_newer_drain_survives_the_ready_reattach(self):
        f, sentinel, held = self.held_launch()
        sentinel.write_text('{"reason": "drain", "ts": 1, "gen": "newer"}\n')  # someone else's stop since
        (f.declaration().parent / "ready").write_text("1\n")
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"newer"', sentinel.read_text(), "only the stop this launch wrote is cleared")
        self.assertIn("a newer stop holds intake", result.stderr)
        self.assertFalse(held.exists(), "a newer drain is a normal 'not mine': the marker is done")

    def test_a_reattach_whose_clear_cannot_run_keeps_the_marker_and_fails(self):
        f, sentinel, held = self.held_launch()
        (f.declaration().parent / "ready").write_text("1\n")
        lock = sentinel.with_name("shutdown.sentinel.lock")
        lock.unlink(missing_ok=True)
        lock.mkdir()  # the compare-and-clear cannot take its lock: an operational error
        result = f.run(**self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not be reopened", result.stderr)
        self.assertTrue(held.exists(), "the recovery marker is kept for the next try")
        self.assertTrue(sentinel.exists())
        lock.rmdir()
        self.assertEqual(f.run(**self.UNSET).returncode, 0)
        self.assertFalse(sentinel.exists())
        self.assertFalse(held.exists())

    def test_an_unreadable_marker_is_kept_and_the_reattach_fails(self):
        f, sentinel, held = self.held_launch()
        (f.declaration().parent / "ready").write_text("1\n")
        held.chmod(0)
        self.addCleanup(lambda: held.exists() and held.chmod(0o600))
        result = f.run(**self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not read", result.stderr)
        self.assertTrue(held.exists())
        self.assertTrue(sentinel.exists())

    def test_a_stop_that_cannot_be_written_is_reported_not_taken_for_already_held(self):
        f = Fixture(self, seat_tmux=True)
        lock = f.workspace / "state" / "shutdown.sentinel.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()  # mark-if-absent cannot take its lock
        result = f.run(FAKE_SEAT_INIT_FAILS="1", FAKE_KILL_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not shut intake for the surviving seat", result.stderr)
        self.assertTrue((f.declaration().parent / "intake-held").exists(), "a later launch still knows")

    def test_a_stop_already_in_place_is_never_cleared_by_the_reattach(self):
        f = Fixture(self, seat_tmux=True)
        sentinel = f.workspace / "state" / "shutdown.sentinel"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"reason": "drain", "ts": 0}\n')
        result = f.run(FAKE_SEAT_INIT_FAILS="1", FAKE_KILL_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotIn("could not shut intake", result.stderr, "already held is a success, not a failure")
        held = f.declaration().parent / "intake-held"
        self.assertEqual(held.read_text().strip(), "", "not this launch's stop")
        (f.declaration().parent / "ready").write_text("1\n")
        self.assertEqual(f.run(**self.UNSET).returncode, 0)
        self.assertTrue(sentinel.exists())

    def test_a_seat_whose_state_cannot_be_read_after_the_kill_is_unknown_not_gone(self):
        f = Fixture(self, seat_tmux=True)
        (f.tmp / "tmux.sock").unlink(missing_ok=True)
        import socket as _s
        sock = _s.socket(_s.AF_UNIX)
        sock.bind(str(f.tmp / "tmux.sock"))
        self.addCleanup(sock.close)
        result = f.run(FAKE_SEAT_INIT_FAILS="1", FAKE_INSPECT_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("(session $7: unknown)", result.stderr)

    def test_a_ready_seat_found_running_is_accepted_without_touching_intake(self):
        f = Fixture(self, seat_tmux=True)
        self.assertEqual(f.run(**self.UNSET).returncode, 0)
        sentinel = f.workspace / "state" / "shutdown.sentinel"
        sentinel.write_text('{"reason": "drain", "ts": 0}\n')
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already running", result.stdout)
        self.assertTrue(sentinel.exists(), "a shutdown someone else set is not this launcher's to lift")

    def test_a_healed_window_that_cannot_be_stopped_fails_loudly(self):
        f = Fixture(self, seat_tmux=True)
        f.core("gone")
        result = f.run(FAKE_SEAT_INIT_FAILS="1", FAKE_KILL_FAILS="1", SUTANDO_SEAT_READY_TIMEOUT="1", **self.UNSET)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not stop the unready Codex app-server seat (window @9: alive)", result.stderr)

    def test_a_ready_seat_reopens_intake(self):
        f = Fixture(self, seat_tmux=True)
        sentinel = f.workspace / "state" / "shutdown.sentinel"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"reason": "test", "ts": 0}\n')
        result = f.run(**self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())

    def test_an_inherited_export_of_the_resolved_mode_does_not_override_the_choice(self):
        # What a running seat's children inherit is no override: the saved Off decides.
        f = Fixture(self, seat_tmux=True)
        self.choose(f, False)
        result = f.run(CODEX_SEAT_ON="1", SUTANDO_CODEX_SEAT="1", **self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex-seat", f.calls())

    def test_the_seat_session_is_marked_without_the_override_variable(self):
        f = Fixture(self, seat_tmux=True)
        self.assertEqual(f.run(**self.UNSET).returncode, 0)
        line, = re.findall(r"new-session -d -s sutando-core .*", f.calls())
        self.assertIn("-e SUTANDO_CODEX_SEAT=1", line)
        self.assertNotIn("SUTANDO_CODEX_APP_SERVER=", line, "workers spawned by the seat resolve the saved choice")

    def test_restart_after_turning_it_off_replaces_the_seat_with_the_tui(self):
        f = Fixture(self, seat_tmux=True)
        self.choose(f, True)
        self.assertEqual(f.run(**self.UNSET).returncode, 0)
        self.assertTrue(f.declaration().exists())
        self.choose(f, False)
        f.h.log.unlink()
        result = f.run("--restart", **self.UNSET)
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = re.findall(r"new-session -d -s sutando-core .*", f.calls())
        self.assertEqual(len(launch), 1)
        self.assertNotIn("codex-seat", launch[0])
        self.assertFalse(f.declaration().exists())

if __name__ == "__main__":
    unittest.main()
