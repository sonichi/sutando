#!/usr/bin/env python3
"""Tests for the Claude Code task-file-injection notifier
(src/agent/claude/cli/task-notifier.sh) — the external tmux-injection
standby path, matching Codex/agy's shape.

Hermetic: a stub `tmux` on PATH stands in for the real binary. Pane content
is a plain file the test controls directly, so pane gating and staging
verification are deterministic rather than timing-races against a real TUI.
core-status.json is written too, only to prove it is never read. `--event
<filename>` drives one dispatch directly (exercises has_result, the pane
gate, staging-retry, submit-confirm-retry) without needing the fswatch-driven
main loop.

core_pane_is_idle_ready() delegates gate/idle-footer classification to the
REAL src/core-input-watch.py (not a stub) — that module already owns Claude's
pane-state patterns for the M0-M4 core supervisor, so this suite pins the
delegation rather than re-deriving the pattern list.

One additional test runs the real main loop against real fswatch to prove
the watch-tasks-stream.sh wiring itself (a task file appearing on disk
reaches the notifier and gets dispatched) — skipped if fswatch is absent.

Does NOT drive a real Claude Code session — that was verified by hand
against the real binary (see the PR body for the pane-text transcript this
suite's IDLE/BUSY markers are drawn from).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(os.environ.get(
    "SUTANDO_TEST_REPO", Path(__file__).resolve().parents[1]
)).resolve()

NOTIFIER = REPO / "src/agent/claude/cli/task-notifier.sh"

# Drawn from a live capture against real Claude Code v2.1.261 (see PR body).
# Leading "❯ " is the real composer line, empty = no unsent draft.
IDLE_FOOTER = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
BUSY_FOOTER = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents"
# The status row alone -- what a pane's LAST line becomes when a turn starts.
BUSY_STATUS = BUSY_FOOTER.split("\n", 1)[1]
# Same footer, but the composer carries an unsent owner draft.
DRAFT_FOOTER = "❯ owner draft\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
# A second, rotating hint row below the real footer -- distinct from the
# single trailing row every other fixture in this file models.
TIP_FOOTER = IDLE_FOOTER + "\nTip: Use /btw to send feedback"
TRUST_GATE_PANE = "\n".join([
    " Quick safety check: Is this a project you created or one you trust?",
    " ❯ No, exit",
    "   Yes, I trust this folder",
    " Enter to confirm · Esc to cancel",
])


class FakeTmuxHarness(unittest.TestCase):
    """Base: builds a stub `tmux` + isolated workspace for one test."""

    # Lines a non-`-S` capture-pane returns (the viewport height); a subclass
    # narrows this to put the marker above it. `-S -N` returns N history rows + the viewport.
    PANE_HEIGHT = 500
    # The PANE's own #{history_limit} (fixed at creation); a subclass narrows it
    # below CAPTURE_SCROLLBACK_LINES to make IT the real bound.
    HISTORY_LIMIT = 500
    # What `show-options -g history-limit` reports -- the global default, which real
    # tmux lets drift away from an existing pane's limit. None = same as the pane.
    GLOBAL_HISTORY_LIMIT = None
    # Columns at which a `-l` paste wraps onto new rows (0 = one row), like a real pane;
    # "chars" cuts anywhere, "word" is the input box's own word wrap + 2-space indent.
    WRAP_COLS = 0
    WRAP_STYLE = "chars"
    # Physical pane width for capture-pane: a row longer than this is returned as
    # several rows unless -J joins them, as a real pane soft-wraps. 0 = no wrap.
    CAPTURE_COLS = 0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.tasks_dir = self.root / "workspace" / "tasks"
        self.results_dir = self.root / "workspace" / "results"
        self.state_dir = self.root / "workspace" / "state"
        self.logs_dir = self.root / "workspace" / "logs"
        for d in (self.tasks_dir, self.results_dir, self.state_dir, self.logs_dir):
            d.mkdir(parents=True)
        self.pane_file = self.root / "pane.txt"
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        # Non-empty = the CLI is showing this ghost text in an empty composer.
        self.ghost_file = self.root / "ghost.txt"
        self.status_file = self.state_dir / "core-status.json"
        self.write_status("idle")
        self.session_flag = self.root / "session.flag"
        self.session_flag.write_text("up")
        self.sendkeys_log = self.root / "send-keys.log"
        self.sendkeys_log.write_text("")
        self.swallow_flag = self.root / "swallow-next-paste.flag"
        # NEVER consumed, unlike swallow_flag -- every attempt is dropped.
        self.swallow_always_flag = self.root / "swallow-always.flag"
        self.busy_after_enter_flag = self.root / "busy-after-enter.flag"
        # The CLI swallowed our C-m: the prompt stays staged, no new composer row.
        self.swallow_enter_flag = self.root / "swallow-enter.flag"
        self.owner_types_after_enter_flag = self.root / "owner-types-after-enter.flag"
        # Consumed with a swallowed paste: an unrelated line appears instead,
        # modeling an interleaved owner keystroke landing where ours didn't.
        self.concurrent_draft_flag = self.root / "concurrent-draft.flag"
        # Consumed once, NOT swallowed: owner text lands on the SAME composer
        # line as our paste (unlike the separate-line flag above).
        self.interleaved_owner_flag = self.root / "interleaved-owner.flag"
        # Every capture-pane increments this; the paste logs `CAPTURES@<n>`, so a
        # test can aim a state flip at "the read before the paste" without hard-coding order.
        self.capture_count = self.root / "capture-count.txt"
        # Holds N: on the Nth capture the footer flips to BUSY (consumed once).
        self.busy_on_capture_flag = self.root / "busy-on-capture.flag"
        # Holds N: on the Nth capture a trust gate replaces the pane (consumed once).
        self.gate_on_capture_flag = self.root / "gate-on-capture.flag"
        # Holds a row of owner text that lands under our paste (consumed once).
        self.extra_owner_row_flag = self.root / "extra-owner-row.flag"
        # The pane's #{pane_pid}: the core incarnation an in-flight marker is keyed to.
        self.pane_pid_file = self.root / "pane-pid.txt"
        self.inflight_dir = self.state_dir / "task-notifier-inflight"
        # Holds a pid: on the next ENTER the pane pid flips to it (a restart racing
        # the submit), consumed once.
        self.pid_after_enter_flag = self.root / "pid-after-enter.flag"
        # The NEXT capture-pane fails (rc 1, no output), consumed once; the
        # after-Enter variant arms it at the moment C-m lands.
        self.fail_next_capture_flag = self.root / "fail-next-capture.flag"
        self.fail_capture_after_enter_flag = self.root / "fail-capture-after-enter.flag"

        # The core pane is gone (its window may live on with a replacement).
        self.pane_gone_flag = self.root / "pane-gone.flag"
        self._write_fake_tmux()

    def write_status(self, status, ts=None):
        self.status_file.write_text(json.dumps({
            "status": status,
            "ts": ts if ts is not None else time.time(),
        }))

    def _write_fake_tmux(self):
        # -l pastes append to pane.txt (simulating composer echo) unless
        # swallow-next-paste.flag is set (consumed once) — simulates a drop.
        script = self.bin / "tmux"
        glob_limit = self.HISTORY_LIMIT if self.GLOBAL_HISTORY_LIMIT is None else self.GLOBAL_HISTORY_LIMIT
        script.write_text(f'''#!/bin/bash
[ "${{1:-}}" = -S ] && shift 2
cmd="$1"; shift
PANE="{self.pane_file}"
# Typed rows land ABOVE the status footer, as in a real pane; the footer is
# always the last row. A text is wrapped at WRAP_COLS like a real terminal.
# Text lands ON the bottommost composer row (a real pane types at the cursor,
# replacing the CLI's hint on an empty row); wrapped rows follow that row, and
# whatever renders below it (a box border, the status footer) stays below.
append_typed() {{
  python3 - "$PANE" "$1" {self.WRAP_COLS} "{self.WRAP_STYLE}" <<'PYEOF'
import re, sys, textwrap
path, text, wrap, style = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
lines = open(path).read().split("\\n")
if lines and lines[-1] == "": lines.pop()
idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("❯")), None)
if idx is None:
    lines.append("❯ "); idx = len(lines) - 1
row = lines[idx]
row = "❯ " if re.match(r'^❯ *$|^❯ Try "|^❯ Press up to edit queued messages', row) else row
new = row + text
if wrap <= 0:
    rows = [new]
elif style == "word":
    # The real input box: wrap at word boundaries, continuation rows indented two spaces.
    rows = textwrap.wrap(new, width=wrap, subsequent_indent="  ", break_long_words=True,
                         break_on_hyphens=False)
else:
    rows = [new[i:i + wrap] for i in range(0, len(new), wrap)]
lines[idx:idx + 1] = rows
open(path, "w").write("\\n".join(lines) + "\\n")
PYEOF
}}
go_busy() {{ sed -i '' -e '$d' "$PANE"; printf '%s\\n' "{BUSY_STATUS}" >> "$PANE"; }}
# An owner CONTINUATION row: its own line under the composer text, above
# whatever structural rows (box rule, status footer) trail the composer.
append_owner_row() {{
  python3 - "$PANE" "$1" <<'PYEOF'
import re, sys
path, text = sys.argv[1], sys.argv[2]
lines = open(path).read().split("\\n")
if lines and lines[-1] == "": lines.pop()
tail = []
if lines and "bypass permissions on" in lines[-1]: tail.insert(0, lines.pop())
while lines and re.match(r"^[\\s─-╿]+$", lines[-1]): tail.insert(0, lines.pop())
lines.append(text)
open(path, "w").write("\\n".join(lines + tail) + "\\n")
PYEOF
}}
total_rows() {{ grep -c '' "$PANE" 2>/dev/null || echo 0; }}
history_size() {{
  local t; t="$(total_rows)"; local h=$(( t - {self.PANE_HEIGHT} ))
  [ "$h" -lt 0 ] && h=0; [ "$h" -gt {self.HISTORY_LIMIT} ] && h={self.HISTORY_LIMIT}
  echo "$h"
}}
case "$cmd" in
  has-session)
    [ -f "{self.session_flag}" ] && exit 0
    exit 1
    ;;
  capture-pane)
    # A vanished pane cannot be captured; the window it was in may live on.
    [ -f "{self.pane_gone_flag}" ] && exit 1
    n=$(( $(cat "{self.capture_count}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{self.capture_count}"
    if [ -f "{self.fail_next_capture_flag}" ]; then rm -f "{self.fail_next_capture_flag}"; exit 1; fi
    # Consumed once: the pane goes BUSY on the Nth capture (a turn starting
    # between the idle gate and the paste), modeled as the footer flipping.
    if [ -f "{self.busy_on_capture_flag}" ] && [ "$n" -ge "$(cat "{self.busy_on_capture_flag}")" ]; then
      rm -f "{self.busy_on_capture_flag}"; go_busy
    fi
    # Consumed once: on the Nth capture a trust gate has REPLACED the idle pane.
    if [ -f "{self.gate_on_capture_flag}" ] && [ "$n" -ge "$(cat "{self.gate_on_capture_flag}")" ]; then
      rm -f "{self.gate_on_capture_flag}"; printf '%s\\n' "{TRUST_GATE_PANE}" > "$PANE"
    fi
    scrollback=0; esc=0; join=0
    for a in "$@"; do
      [ "$a" = -S ] && scrollback=1
      [ "$a" = -e ] && esc=1
      [ "$a" = -J ] && join=1
    done
    if [ "$scrollback" = 1 ]; then
      out="$(tail -n $(( {self.PANE_HEIGHT} + $(history_size) )) "$PANE" 2>/dev/null)"
    else
      out="$(tail -n {self.PANE_HEIGHT} "$PANE" 2>/dev/null)"
    fi
    # Ghost text renders only into an EMPTY composer and vanishes on the first
    # typed character; a plain capture loses its dimming, -e keeps it.
    if [ -s "{self.ghost_file}" ]; then
      g="$(cat "{self.ghost_file}")"
      if [ "$esc" = 1 ]; then
        out="$(printf '%s\\n' "$out" | LC_ALL=C sed "s/^❯ $/❯ $(printf '\\033')[2m${{g}}$(printf '\\033')[0m/")"
      else
        out="$(printf '%s\\n' "$out" | LC_ALL=C sed "s/^❯ $/❯ ${{g}}/")"
      fi
    fi
    if [ {self.CAPTURE_COLS} -gt 0 ] && [ "$join" = 0 ]; then
      out="$(printf '%s\\n' "$out" | fold -w {self.CAPTURE_COLS})"
    fi
    printf '%s\\n' "$out"
    exit 0
    ;;
  show-options)
    printf 'history-limit %s\\n' {glob_limit}
    exit 0
    ;;
  display-message)
    # -p -t TARGET '#{{history_limit}}' | '#{{history_size}}'
    case "$*" in
      *history_limit*) echo {self.HISTORY_LIMIT} ;;
      *history_size*) history_size ;;
      *pane_pid*) cat "{self.pane_pid_file}" 2>/dev/null || echo 4242 ;;

      *pane_id*)
        if [ -f "{self.pane_gone_flag}" ]; then echo ""; exit 0; fi
        case "$*" in *"-t %"*) echo "$*" | sed -n 's/.*-t \\(%[0-9][0-9]*\\).*/\\1/p' ;; *) echo "%1" ;; esac ;;
      *) echo "" ;;
    esac
    exit 0
    ;;
  send-keys)
    # args: -t TARGET [-l -- TEXT | C-m]; the target is recorded so a test can pin it
    printf 'TARGET %s\\n' "$2" >> "{self.sendkeys_log}"
    shift 2  # -t TARGET
    if [ "${{1:-}}" = -l ]; then
      shift 2  # -l --
      text="$1"
      printf 'CAPTURES@%s\\nTYPE %s\\n' "$(cat "{self.capture_count}" 2>/dev/null || echo 0)" "$text" >> "{self.sendkeys_log}"
      if [ -f "{self.swallow_always_flag}" ]; then
        :
      elif [ -f "{self.swallow_flag}" ]; then
        rm -f "{self.swallow_flag}"
        if [ -f "{self.concurrent_draft_flag}" ]; then
          rm -f "{self.concurrent_draft_flag}"
          append_typed "owner is typing something else"
        fi
      elif [ -f "{self.interleaved_owner_flag}" ]; then
        rm -f "{self.interleaved_owner_flag}"
        append_typed "$text OWNERTEXT"
      else
        append_typed "$text"
        # Consumed once: an owner row lands on its own line under our paste.
        if [ -f "{self.extra_owner_row_flag}" ]; then
          append_owner_row "$(cat "{self.extra_owner_row_flag}")"; rm -f "{self.extra_owner_row_flag}"
        fi
      fi
    else
      printf 'ENTER markers=%s\\n' "$(ls "{self.inflight_dir}" 2>/dev/null | grep -vc '^\\.')" >> "{self.sendkeys_log}"
      if [ -f "{self.pid_after_enter_flag}" ]; then
        cat "{self.pid_after_enter_flag}" > "{self.pane_pid_file}"; rm -f "{self.pid_after_enter_flag}"
      fi
      if [ -f "{self.fail_capture_after_enter_flag}" ]; then
        rm -f "{self.fail_capture_after_enter_flag}"; touch "{self.fail_next_capture_flag}"
      fi
      # Real Claude keeps the submitted prompt visible as scrollback and opens
      # a fresh empty composer row under it; a swallowed Enter changes nothing.
      if [ ! -f "{self.swallow_enter_flag}" ]; then
        python3 - "$PANE" <<'PYEOF'
import sys
path = sys.argv[1]
lines = open(path).read().split("\\n")
if lines and lines[-1] == "": lines.pop()
lines.insert(max(len(lines) - 1, 0), "❯ ")
open(path, "w").write("\\n".join(lines) + "\\n")
PYEOF
      fi
      if [ -f "{self.busy_after_enter_flag}" ]; then
        go_busy
      fi
      # Simulates the owner typing something new right after our C-m --
      # not busy, and not our own staged prompt either.
      if [ -f "{self.owner_types_after_enter_flag}" ]; then
        rm -f "{self.owner_types_after_enter_flag}"
        append_typed "owner is typing something else"
      fi
    fi
    exit 0
    ;;
  new-session|kill-session|setenv)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
''')
        script.chmod(0o755)

    def _env(self, extra=None):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}",
            "SUTANDO_TMUX_SOCKET": str(self.root / "fake.sock"),
            "SUTANDO_TMUX_SESSION": "sutando-core-test",
            "SUTANDO_TASKS_DIR": str(self.tasks_dir),
            "SUTANDO_RESULTS_DIR": str(self.results_dir),
            "SUTANDO_CORE_STATUS_FILE": str(self.status_file),
            "SUTANDO_NOTIFIER_POLL_INTERVAL": "0.1",
            "SUTANDO_NOTIFIER_CORE_READY_TIMEOUT": "3",
            "SUTANDO_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT": "1",
            "SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "8",
        })
        if extra:
            env.update(extra)
        return env

    def write_task(self, name, body="task: say OK\n"):
        (self.tasks_dir / name).write_text(body)

    def write_result(self, name, body="OK\n"):
        (self.results_dir / name).write_text(body)

    def run_event(self, filename, env_extra=None, timeout=15):
        return subprocess.run(
            ["/bin/bash", str(NOTIFIER), "--event", filename],
            env=self._env(env_extra),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def sendkeys_log_text(self):
        return self.sendkeys_log.read_text()


class EventDispatchTests(FakeTmuxHarness):
    """--event <filename>: exercises submit_task/deliver_prompt directly."""

    def test_existing_result_is_never_dispatched(self):
        self.write_task("task-a.txt")
        self.write_result("task-a.txt")
        result = self.run_event("task-a.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a task with an existing result must never be typed into the pane")

    def test_empty_live_placeholder_with_no_ready_result_anywhere_is_still_dispatched(self):
        # `[ -f results/<f> ]` treated an empty file as delivered regardless
        # of content; the shared module's READY walk rejects whitespace-only.
        self.write_task("task-c.txt")
        (self.results_dir / "task-c.txt").write_text("")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-c.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-c.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-c.txt", self.sendkeys_log_text(),
                       "an empty placeholder with nothing ready behind it must not block dispatch")

    def test_pending_task_is_typed_and_submitted(self):
        self.write_task("task-b.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-b.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-b.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-b.txt", log)
        self.assertIn("ENTER", log)
        pane = self.pane_file.read_text()
        self.assertIn("Sutando task ready: task-b.txt", pane)
        self.assertIn("follow CLAUDE.md", log)

    def test_a_stale_running_self_report_does_not_block_an_idle_pane(self):
        # The status file is never read; a stale "running" is as irrelevant as a fresh one.
        self.write_task("task-stale.txt")
        self.write_status("running", ts=time.time() - 200)
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-stale.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-stale.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-stale.txt", self.sendkeys_log_text())

    def test_a_fresh_running_self_report_does_not_block_an_idle_pane(self):
        # The status file is the core's own report; a killed turn leaves it
        # "running" while the pane shows the idle prompt. The pane decides.
        self.write_task("task-fresh.txt")
        self.write_status("running", ts=time.time())
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-fresh.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-fresh.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-fresh.txt", self.sendkeys_log_text(),
                      "a self-reported 'running' must not outrank an idle pane")

    def test_a_running_turn_still_receives_the_task(self):
        # The pane shows an in-flight turn. The Monitor tool's notification
        # never waited for it, and neither does this: the line queues behind it.
        self.write_task("task-c.txt")
        self.pane_file.write_text(BUSY_FOOTER + "\n")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-c.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-c.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-c.txt", log,
                      "a running turn is not a gate; the line must be typed")
        self.assertIn("ENTER", log, "and submitted, so the CLI queues it")

    def test_trust_gate_on_stale_status_blocks_dispatch(self):
        # Pins the delegation to the REAL core-input-watch.py: a stale status
        # plus a pane stuck on the folder-trust gate must not read as idle.
        self.write_task("task-gate.txt")
        self.write_status("running", ts=time.time() - 200)
        self.pane_file.write_text(TRUST_GATE_PANE + "\n")
        result = self.run_event("task-gate.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a folder-trust gate must never be typed over")

    def test_trust_gate_on_fresh_idle_status_blocks_dispatch(self):
        # A fresh (non-stale) idle status alone must not satisfy dispatch --
        # a trust-gate pane is "not busy" too (no in-flight turn to interrupt).
        self.write_task("task-gate2.txt")
        self.write_status("idle")
        self.pane_file.write_text(TRUST_GATE_PANE + "\n")
        result = self.run_event("task-gate2.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a folder-trust gate must never be typed over, even under a fresh idle status")

    def test_stale_same_task_marker_plus_swallowed_paste_is_not_mistaken_for_staged(self):
        # A prior episode's prompt in OLDER scrollback (above the current
        # composer) plus this episode's paste swallowed must not read staged.
        self.pane_file.write_text("Sutando task ready: task-i.txt\n" + IDLE_FOOTER + "\n")
        self.write_task("task-i.txt")
        self.swallow_flag.write_text("1")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-i.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-i.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        type_calls = self.sendkeys_log_text().count("TYPE Sutando task ready: task-i.txt")
        self.assertEqual(type_calls, 2,
                          "a stale marker from a prior episode must not be read as this "
                          "episode's own staged paste -- the swallowed retype must still fire")

    def test_stale_marker_plus_concurrent_owner_draft_is_not_mistaken_for_staged(self):
        # Same stale marker, but the swallowed paste is masked by an
        # UNRELATED pane change instead of leaving the tail byte-identical.
        self.pane_file.write_text("Sutando task ready: task-k.txt\n" + IDLE_FOOTER + "\n")
        self.write_task("task-k.txt")
        self.swallow_flag.write_text("1")
        self.concurrent_draft_flag.write_text("1")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-k.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-k.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("TYPE Sutando task ready: task-k.txt"), 1)
        self.assertNotIn("ENTER", log,
                         "a stale marker plus an unrelated concurrent pane change must not "
                         "satisfy staging")
        # The owner's draft now occupies the composer: the retype must refuse
        # rather than type over it (failing closed beats a second paste).
        self.assertIn("composer not empty",
                      (self.logs_dir / "claude-task-notifier.log").read_text())

    def test_composer_draft_blocks_typing(self):
        # An unsent owner draft in the composer must never be typed over,
        # even though the pane is otherwise idle-ready (no gate signature).
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        self.write_task("task-m.txt")
        result = self.run_event("task-m.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("TYPE", self.sendkeys_log_text(),
                          "an unsent owner draft in the composer must never be typed over")
        self.assertFalse((self.results_dir / "task-m.txt").exists(),
                          "a task blocked on a draft composer must stay queued, not consumed")

    def test_ghost_text_suggestion_is_not_a_draft(self):
        # The CLI's suggested reply is dim ghost text in the EMPTY composer; a plain
        # capture shows it as typed, and every re-pick would stall on it.
        self.ghost_file.write_text("yes")
        self.write_task("task-g.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-g.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-g.txt")
        t.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("composer not empty", result.stderr,
                         "ghost text must not read as an unsent draft")
        self.assertIn("TYPE", self.sendkeys_log_text(),
                      "the paste must proceed over ghost text")
        self.assertIn("ENTER", self.sendkeys_log_text(),
                      "the prompt must stage and submit once the ghost text is gone")

    def test_pane_change_after_enter_blocks_a_second_press(self):
        # After the first C-m, the pane changing to something other than
        # busy (e.g. the owner typing) must never get a second C-m.
        self.write_task("task-j.txt")
        self.owner_types_after_enter_flag.write_text("1")
        result = self.run_event("task-j.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text().count("ENTER"), 1,
                          "a pane that changed to something other than our own prompt "
                          "must not receive a second C-m")

    def test_dropped_paste_is_retyped(self):
        self.write_task("task-d.txt")
        self.swallow_flag.write_text("1")

        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-d.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-d.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        type_calls = self.sendkeys_log_text().count("TYPE Sutando task ready: task-d.txt")
        self.assertEqual(type_calls, 2,
                          "a paste that never staged must be retyped exactly once more")

    def test_owner_text_interleaved_with_our_paste_is_not_mistaken_for_staged(self):
        # Owner text alongside our marker used to satisfy a substring check.
        # Retyping never clears the composer, so a real mix can't self-heal.
        self.write_task("task-o.txt")
        self.interleaved_owner_flag.write_text("1")
        result = self.run_event("task-o.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("TYPE Sutando task ready: task-o.txt"), 1,
                          "the mix now occupies the composer; a retype would paste over owner text")
        self.assertNotIn("ENTER", log,
                          "Enter must never fire on a composer mixing our prompt with owner text")
        self.assertIn("composer not empty",
                      (self.logs_dir / "claude-task-notifier.log").read_text())

    def test_never_staged_returns_fast_instead_of_waiting_the_full_timeout(self):
        # Enter never sent -> give up immediately, never wait out
        # COMPLETION_TIMEOUT (8s here) for a result that can't ever appear.
        self.write_task("task-n.txt")
        self.swallow_always_flag.write_text("1")
        started = time.time()
        result = self.run_event("task-n.txt", timeout=15)
        elapsed = time.time() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ENTER", self.sendkeys_log_text(),
                          "Enter must never fire when staging never succeeded")
        self.assertLess(elapsed, 4,
                         f"took {elapsed:.1f}s -- a never-submitted prompt must not wait out "
                         "the completion timeout")

    def test_unconfirmed_submit_is_re_pressed(self):
        # A swallowed C-m leaves the prompt staged in the composer, so
        # deliver_prompt must re-press at least once after the confirm timeout.
        self.swallow_enter_flag.write_text("1")
        self.write_task("task-e.txt")

        import threading
        def _finish():
            for _ in range(80):
                if self.sendkeys_log_text().count("ENTER") >= 2:
                    self.write_result("task-e.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-e.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.sendkeys_log_text().count("ENTER"), 2,
                                 "an unconfirmed submit must be re-pressed")

    def test_no_session_drops_without_hanging(self):
        self.session_flag.unlink()
        self.write_task("task-f.txt")
        result = self.run_event("task-f.txt", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "")

    def test_submit_confirms_when_the_prompt_leaves_the_composer(self):
        # The submitted text stays in scrollback; what confirms is the fresh
        # empty composer under it, not the pane going busy.
        self.write_task("task-h.txt")
        self.busy_after_enter_flag.write_text("1")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-h.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-h.txt", timeout=15)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("ENTER"), 1,
                          "a prompt that left the composer is confirmed on the first attempt")
        self.assertIn("Sutando task ready: task-h.txt", self.pane_file.read_text(),
                       "the submitted text staying in scrollback is the exact case this pins")

    def test_a_novel_prompt_under_an_old_idle_footer_is_not_typed_into(self):
        # An unforeseen confirmation shares the window with a stale idle footer and
        # a blank bottom composer; the pane read alone must refuse.
        self.status_file.unlink()
        self.write_task("task-novel.txt")
        self.pane_file.write_text("\n".join([
            "❯", "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
            "Overwrite the existing config file?", "Enter to confirm · Esc to cancel", "❯", ""]))
        result = self.run_event("task-novel.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                         "a live prompt must never receive the task as its answer")

    def test_an_abnormal_banner_holds_the_task(self):
        # Parked on an API error: the one state a running turn is not. Hold.
        self.write_task("task-abn.txt")
        self.pane_file.write_text("API Error: 529 Overloaded\n" + IDLE_FOOTER + "\n")
        result = self.run_event("task-abn.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                         "an abnormal pane must not be typed into")
        self.assertIn("did not become healthy", result.stderr)

    def test_the_clis_connection_error_retry_banner_holds_the_task(self):
        # The retry family, under the CLI's own result prefix: nothing is being served.
        self.write_task("task-retry.txt")
        self.pane_file.write_text("  ⎿  Connection error. Retrying in 2 seconds…\n" + IDLE_FOOTER + "\n")
        result = self.run_event("task-retry.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                         "a retrying pane must not be typed into")

    def test_prose_about_a_connection_error_on_screen_does_not_hold(self):
        # The banner families are line-anchored; a transcript discussing errors is not one.
        self.write_task("task-prose.txt")
        self.pane_file.write_text("⏺ I once saw a Connection error. Retrying was the fix.\n" + IDLE_FOOTER + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-prose.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-prose.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-prose.txt", self.sendkeys_log_text())

    def test_prose_under_the_tool_result_prefix_does_not_hold(self):
        # `⎿` is also the ordinary tool-result prefix; a whole-line grammar tells the banner from it.
        self.write_task("task-prose2.txt")
        self.pane_file.write_text("  ⎿  Connection error. Retrying was the fix.\n" + IDLE_FOOTER + "\n")
        t = self._finish_on("task-prose2.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-prose2.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-prose2.txt", self.sendkeys_log_text())

    def test_prose_naming_an_api_error_or_a_retry_does_not_hold(self):
        for name, line in (("task-p4.txt", "⏺ API Error handling is covered by tests."),
                           ("task-p5.txt", "  ⎿  Connection error. The fix was retrying")):
            self.write_task(name)
            self.pane_file.write_text(line + "\n" + IDLE_FOOTER + "\n")
            t = self._finish_on(name, lambda log, n=name: f"TYPE Sutando task ready: {n}" in log and "ENTER" in log)
            result = self.run_event(name)
            t.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"TYPE Sutando task ready: {name}", self.sendkeys_log_text(), line)

    def test_a_wrapped_sentence_starting_with_a_retry_word_does_not_hold(self):
        self.write_task("task-prose3.txt")
        self.pane_file.write_text("⏺ I verified the docs that say\n  Connection error handling is covered by tests.\n" + IDLE_FOOTER + "\n")
        t = self._finish_on("task-prose3.txt", lambda log: "ENTER" in log)
        result = self.run_event("task-prose3.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-prose3.txt", self.sendkeys_log_text())

    def _finish_on(self, name, predicate):
        import threading
        def _run():
            for _ in range(100):
                if predicate(self.sendkeys_log_text()):
                    self.write_result(name)
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_run); t.start()
        return t

    def test_the_queued_messages_composer_is_not_a_draft(self):
        # A line already queued behind the turn leaves this hint in the composer;
        # the next task must still go in, on top of the queue.
        self.write_task("task-q.txt")
        self.pane_file.write_text("❯ Press up to edit queued messages\n" + BUSY_STATUS + "\n")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-q.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-q.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-q.txt", self.sendkeys_log_text())

    def test_a_busy_footer_alone_does_not_confirm_a_submit(self):
        # Busy is trivially true once a turn runs, so it proves nothing about our
        # line: a swallowed C-m on a busy pane must still be re-pressed.
        self.write_task("task-h2.txt")
        self.swallow_enter_flag.write_text("1")
        self.busy_after_enter_flag.write_text("1")
        import threading
        def _finish():
            for _ in range(80):
                if self.sendkeys_log_text().count("ENTER") >= 2:
                    self.write_result("task-h2.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-h2.txt", timeout=15)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.sendkeys_log_text().count("ENTER"), 2,
                                "a busy footer must not stand in for the prompt leaving the composer")

    def test_no_status_file_does_not_block_an_idle_pane(self):
        # A fresh install or a core that never wrote its status has no file;
        # the pane alone shows whether a task can be typed.
        self.status_file.unlink()
        self.write_task("task-g.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-g.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-g.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-g.txt", self.sendkeys_log_text(),
                      "a missing status file must not hold a task on an idle pane")


class SmallViewportTests(FakeTmuxHarness):
    """A 3-row pane: only the composer and footer are on screen. What scrolled
    off is history, whatever the scrollback capture still retains of it."""

    PANE_HEIGHT = 3

    def test_a_stale_error_high_in_scrollback_does_not_hold(self):
        # The error was hours ago; the pane has moved on to an idle prompt. Deliver.
        self.write_task("task-old.txt")
        history = "\n".join(f"⏺ line {i}" for i in range(6))
        self.pane_file.write_text("API Error: 529 Overloaded\n" + history + "\n" + IDLE_FOOTER + "\n")


class TargetTests(FakeTmuxHarness):
    """Every capture and keystroke goes to the declared target, and a vanished
    pane ends the wait at once rather than at the ready timeout."""

    def _deliver(self, env):
        self.write_task("task-t.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-t.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish); t.start()
        result = self.run_event("task-t.txt", env_extra=env)
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_notifier_addresses_the_declared_window(self):
        self._deliver({"SUTANDO_TMUX_WINDOW": "3"})
        log = self.sendkeys_log_text()
        self.assertIn("TARGET sutando-core-test:3", log)
        self.assertNotIn("TARGET sutando-core-test:0", log, "a keystroke went to window 0")

    def test_the_notifier_addresses_the_declared_pane_over_the_window(self):
        self._deliver({"SUTANDO_TMUX_WINDOW": "3", "SUTANDO_TMUX_PANE": "%7"})
        log = self.sendkeys_log_text()
        self.assertIn("TARGET %7", log)
        self.assertNotIn("TARGET sutando-core-test", log, "a keystroke went to a window instead of the pane")

    def test_a_vanished_pane_ends_the_wait_at_once(self):
        # The session (and even the window) may live on; the pane is what matters.
        # Real tmux answers a dead pane with rc 0 and a BLANK id, which the fake models.
        self.pane_gone_flag.write_text("1")
        self.pane_file.write_text(BUSY_FOOTER + "\n")
        self.write_task("task-gone.txt")
        started = time.time()
        result = self.run_event("task-gone.txt", env_extra={"SUTANDO_TMUX_PANE": "%7"}, timeout=8)
        elapsed = time.time() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("TYPE", self.sendkeys_log_text())
        self.assertLess(elapsed, 3, f"waited {elapsed:.1f}s for a pane that no longer exists")


class TallComposerScrollbackTests(FakeTmuxHarness):
    """Regression for a real head-e540f676a review finding (qingyun-wu's
    Codex, 2026-09-18): a prompt taller than the pane's own height scrolls
    its leading composer marker into scrollback, where a plain
    `capture-pane -p` (no -S) never sees it again -- the periodic retry then
    finds a permanently nonempty composer and stalls forever. PANE_HEIGHT=2
    makes the fake tmux's non-`-S` reads a 2-line viewport, exactly wide
    enough for the untyped idle footer and no more, so appending even one
    typed line pushes the marker out of view unless capture_tail() asks for
    scrollback."""

    PANE_HEIGHT = 2
    # Wrap the ~200-char prompt onto 25+ rows so a restored `| tail -20`
    # after the capture also loses the marker -- the second cap must be dead too.
    WRAP_COLS = 8

    def test_marker_scrolled_off_pane_top_is_still_found_via_scrollback(self):
        self.write_task("task-tall.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-tall.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-tall.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn(
            "ENTER", log,
            "staging must succeed even though the marker line sits above "
            "the fake pane's 2-line viewport -- capture_tail() must ask for "
            "scrollback (-S), not just the visible screen",
        )


class HistoryLimitBoundTests(FakeTmuxHarness):
    """Regression for a real review finding on PR #4307 (rui / yixuan-ag2,
    2026-09-18): CAPTURE_SCROLLBACK_LINES only helps when IT is the binding
    constraint. When tmux's own history-limit is smaller, `-S` can never
    return more than that no matter how high the env var is raised, and
    that miss must be a distinguishable, loud condition -- not the generic
    never-staged message. HISTORY_LIMIT=2 caps `-S` itself at the untyped
    footer's own line count, so even scrollback can't recover the marker
    once a line is typed."""

    PANE_HEIGHT = 2
    HISTORY_LIMIT = 2
    WRAP_COLS = 8
    # The global option drifts above an existing pane's limit in real tmux; a
    # detector keyed on it reads 2000, sees a 2-row capture, and stays silent.
    GLOBAL_HISTORY_LIMIT = 2000

    def test_marker_past_historys_own_limit_is_reported_distinctly(self):
        self.write_task("task-past-limit.txt")
        result = self.run_event("task-past-limit.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ENTER", self.sendkeys_log_text(),
                          "must fail closed -- tmux truly has no more history to give")
        log_text = (self.logs_dir / "claude-task-notifier.log").read_text()
        self.assertIn("may exceed the capture window", log_text,
                      "the warning must key on the PANE's #{history_limit}, not the global")
        self.assertIn("history-limit", log_text)
        self.assertIn("#{history_size}=2", log_text)


class BusyBeforePasteTests(FakeTmuxHarness):
    """A turn can start between the health gate and the paste, and a gate can
    replace the idle prompt there. The baseline read is the one judged: a turn
    is not a hold (the line queues), a gate is. Calibrated, not hard-coded: a
    control run records how many captures precede the paste (`CAPTURES@n` in
    the log), then the real run flips the pane on that very capture."""

    def _captures_before_first_paste(self):
        self.write_task("task-cal.txt")
        # No result ever appears; a 1s completion timeout keeps the control short.
        self.run_event("task-cal.txt", timeout=12,
                       env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        m = re.search(r"CAPTURES@(\d+)", self.sendkeys_log_text())
        self.assertIsNotNone(m, "control run never pasted; cannot calibrate")
        return int(m.group(1))

    def test_a_turn_starting_right_before_the_paste_still_gets_the_line(self):
        n = self._captures_before_first_paste()
        # Fresh harness state for the real run, same fake, same read order.
        self.sendkeys_log.write_text(""); self.capture_count.unlink()
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        # CAPTURES@n is the count AT the paste: capture n IS the last read
        # before it (the baseline). A turn starting there is not a gate.
        self.busy_on_capture_flag.write_text(str(n))
        self.write_task("task-race.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-race.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-race.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE", log, "a turn that just started must not hold the line")
        self.assertIn("ENTER", log, "the line queues behind the turn")
        self.assertNotIn("not healthy at the paste",
                         (self.logs_dir / "claude-task-notifier.log").read_text())

    def test_a_gate_replacing_idle_on_the_baseline_read_blocks_the_paste(self):
        # Not-busy is not idle: a trust gate has no "esc to interrupt" and
        # would pass a busy-only check, then receive the task as its answer.
        n = self._captures_before_first_paste()
        self.sendkeys_log.write_text(""); self.capture_count.unlink()
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        self.gate_on_capture_flag.write_text(str(n))
        self.write_task("task-gate-race.txt")
        result = self.run_event("task-gate-race.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("TYPE", self.sendkeys_log_text(), "pasted the task into a trust gate")


class OwnerRowResemblingUiTextTests(FakeTmuxHarness):
    """An owner continuation row that happens to match a gate signature
    ("permission to ...") lands under our paste. It is typed text: the
    composer must compare UNEQUAL to the bare prompt and nothing may be
    submitted -- a parser that drops rows for resembling UI text would
    read the mix as exactly our prompt and press Enter over it."""

    def test_mixed_composer_with_gate_like_owner_row_is_never_submitted(self):
        self.extra_owner_row_flag.write_text("permission to continue")
        self.write_task("task-mix.txt")
        result = self.run_event("task-mix.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-mix.txt", log)
        self.assertNotIn("ENTER", log,
                         "the owner's row was discarded and the mix passed as our prompt")
        self.assertIn("permission to continue", self.pane_file.read_text(),
                      "fixture precondition: the owner row is really in the pane")


class OwnerRowReadingForAgentsTests(FakeTmuxHarness):
    """The footer strip must not re-classify what it exposes: once the real
    status row is gone, an owner row reading `for agents` is trailing and
    matches the idle regex -- it is typed text and the mix must be refused."""

    def test_owner_continuation_row_matching_the_footer_words_is_never_submitted(self):
        self.extra_owner_row_flag.write_text("for agents")
        self.write_task("task-agents.txt")
        result = self.run_event("task-agents.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-agents.txt", log)
        self.assertNotIn("ENTER", log, "the owner's `for agents` row was stripped as a footer")
        self.assertIn("for agents\n", self.pane_file.read_text(),
                      "fixture precondition: the owner row is really in the pane")


class TwoRowFooterTests(FakeTmuxHarness):
    """A live incident, not a hypothetical: a real Claude Code footer can carry
    a second, rotating "Tip: ..." row below its idle-footer row. The old
    composer_text() (core-input-watch.py's _composer_text) popped only one
    trailing non-border row, so the tip leaked into staged text and every
    delivery refused with "composer holds <task>'s prompt with other text" for
    47 minutes until a human cleared the composer by hand."""

    def test_a_tip_row_below_the_real_footer_does_not_block_staging(self):
        self.pane_file.write_text(TIP_FOOTER + "\n")
        self.write_task("task-tip.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-tip.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-tip.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-tip.txt", log)
        self.assertIn("ENTER", log, "a tip row below the real footer must not be read as an unsent draft")


class PlaceholderComposerTests(FakeTmuxHarness):
    """Found by the isolated live witness, not by any fake: Claude Code
    v2.1.275 renders a hint in the EMPTY composer, and a plain capture loses
    the dimming that distinguishes it from typed text. Read as a draft, the
    notifier refused every task on a genuinely idle pane."""

    LIVE_PANE = (
        "                                            ● high · /effort\n"
        "────────────────────────────────────────────────────────────────\n"
        '❯ Try "refactor <filepath>"\n'
        "────────────────────────────────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n")

    def test_hint_in_an_empty_composer_does_not_block_dispatch(self):
        self.pane_file.write_text(self.LIVE_PANE)
        self.write_task("task-hint.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-hint.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-hint.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertIn("TYPE Sutando task ready: task-hint.txt", log)
        self.assertIn("ENTER", log, "the CLI's own hint text was read as an owner draft")


class LiveWordWrapTests(FakeTmuxHarness):
    """Found by the live witness: the input box word-wraps at the pane width
    and indents continuation rows by two spaces, so the dewrapped capture
    reads `task,  and write` where the prompt says `task, and write`. Exact
    equality then fails every time on a 120-column pane and the notifier
    refuses its own successful paste."""

    WRAP_COLS = 120
    WRAP_STYLE = "word"

    def test_word_wrapped_paste_still_stages_and_submits(self):
        self.pane_file.write_text(PlaceholderComposerTests.LIVE_PANE)
        self.write_task("task-wrap.txt")
        import threading
        def _finish():
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-wrap.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_finish)
        t.start()
        result = self.run_event("task-wrap.txt")
        t.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        pane = self.pane_file.read_text()
        self.assertRegex(pane, r"\n  \S", "fixture precondition: an indented continuation row exists")
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("TYPE Sutando task ready: task-wrap.txt"), 1)
        self.assertIn("ENTER", log, "a word-wrapped paste must compare equal to the prompt")

    def test_owner_text_still_breaks_whitespace_insensitive_equality(self):
        self.interleaved_owner_flag.write_text("1")
        self.write_task("task-wrapmix.txt")
        result = self.run_event("task-wrapmix.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ENTER", self.sendkeys_log_text())


if __name__ == "__main__":
    unittest.main()
