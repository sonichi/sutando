#!/usr/bin/env python3
"""Tests for the Claude Code task-file-injection notifier
(src/agent/claude/cli/task-notifier.sh) — the external tmux-injection
standby path, matching Codex/agy's shape.

Hermetic: a stub `tmux` on PATH stands in for the real binary. Pane content
and core-status.json are plain files the test controls directly, so
status-file/pane gating and staging verification are deterministic rather
than timing-races against a real TUI. `--event <filename>` drives one
dispatch directly (exercises has_result, idle-gating via both
core-status.json and the pane, staging-retry, submit-confirm-retry) without
needing the fswatch-driven main loop.

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
row = "❯ " if re.match(r'^❯ *$|^❯ Try "', row) else row
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
    n=$(( $(cat "{self.capture_count}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{self.capture_count}"
    # Consumed once: the pane goes BUSY on the Nth capture (a turn starting
    # between the idle gate and the paste), modeled as the footer flipping.
    if [ -f "{self.busy_on_capture_flag}" ] && [ "$n" -ge "$(cat "{self.busy_on_capture_flag}")" ]; then
      rm -f "{self.busy_on_capture_flag}"; go_busy
    fi
    # Consumed once: on the Nth capture a trust gate has REPLACED the idle pane.
    if [ -f "{self.gate_on_capture_flag}" ] && [ "$n" -ge "$(cat "{self.gate_on_capture_flag}")" ]; then
      rm -f "{self.gate_on_capture_flag}"; printf '%s\\n' "{TRUST_GATE_PANE}" > "$PANE"
    fi
    scrollback=0
    for a in "$@"; do
      [ "$a" = -S ] && scrollback=1
    done
    if [ "$scrollback" = 1 ]; then
      tail -n $(( {self.PANE_HEIGHT} + $(history_size) )) "$PANE" 2>/dev/null
    else
      tail -n {self.PANE_HEIGHT} "$PANE" 2>/dev/null
    fi
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
      *pane_id*) [ -f "{self.pane_gone_flag}" ] && exit 1; echo "%1" ;;
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
      printf 'ENTER\\n' >> "{self.sendkeys_log}"
      # Real Claude keeps the submitted prompt visible as scrollback (it
      # does not vanish from the pane) — only opt-in when a test wants to
      # prove the confirm check doesn't misread that as still-staged.
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
            "SUTANDO_NOTIFIER_CORE_READY_TIMEOUT": "5",
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

    def test_stale_running_status_falls_back_to_pane(self):
        # status.json says "running" but is stale (>90s): the notifier must
        # fall back to the pane's own idle-ready read rather than trust it.
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

    def test_fresh_running_status_blocks_dispatch(self):
        # A fresh "running" self-report is trusted outright, without
        # consulting the pane, and must not dispatch before it times out.
        self.write_task("task-fresh.txt")
        self.write_status("running", ts=time.time())
        result = self.run_event("task-fresh.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "",
                          "a fresh 'running' self-report must block dispatch")

    def test_idle_status_but_busy_pane_blocks_dispatch_until_idle(self):
        # status.json says "idle" but the pane shows an in-flight turn: the
        # pane must veto the stale-looking idle self-report.
        self.write_task("task-c.txt")
        self.write_status("idle")
        self.pane_file.write_text(BUSY_FOOTER + "\n")

        import threading
        violation = []

        def _unblock():
            time.sleep(0.6)  # several poll intervals while still busy
            if self.sendkeys_log_text() != "":
                violation.append("notifier dispatched while pane was still busy")
            self.pane_file.write_text(IDLE_FOOTER + "\n")
            for _ in range(50):
                if "ENTER" in self.sendkeys_log_text():
                    self.write_result("task-c.txt")
                    return
                time.sleep(0.1)
        t = threading.Thread(target=_unblock)
        t.start()
        result = self.run_event("task-c.txt")
        t.join(timeout=5)
        self.assertEqual(violation, [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TYPE Sutando task ready: task-c.txt", self.sendkeys_log_text())

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
        # This stub's ENTER never clears the staged marker, so deliver_prompt
        # must re-press C-m at least once after the confirm timeout elapses.
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

    def test_submit_confirms_on_busy_pane_not_on_text_vanishing(self):
        # Live-caught regression (see PR body): the submitted text stays in
        # scrollback, so confirm must key on the pane going BUSY, not on it.
        self.write_task("task-h.txt")
        self.busy_after_enter_flag.write_text("1")
        result = self.run_event("task-h.txt", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertEqual(log.count("ENTER"), 1,
                          "a pane that goes busy right after C-m must be recognized as "
                          "confirmed on the first attempt, not re-pressed")
        self.assertIn("Sutando task ready: task-h.txt", self.pane_file.read_text(),
                       "the submitted text staying in scrollback is the exact case this pins")

    def test_no_status_file_blocks_dispatch(self):
        # No self-report at all yet (e.g. before the core's first status
        # write): must not guess idle from the pane alone.
        self.status_file.unlink()
        self.write_task("task-g.txt")
        result = self.run_event("task-g.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sendkeys_log_text(), "")


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
    """A turn can start between the idle gate and the paste. The busy footer
    already sits in the pane when the notifier records its baseline, so the
    staged-vs-baseline diff and the pre-Enter equality check both pass and
    Enter lands on a live turn. Calibrated, not hard-coded: a control run
    records how many captures precede the paste (`CAPTURES@n` in the log),
    then the real run flips the footer to BUSY on that very capture."""

    def _captures_before_first_paste(self):
        self.write_task("task-cal.txt")
        # No result ever appears; a 1s completion timeout keeps the control short.
        self.run_event("task-cal.txt", timeout=12,
                       env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        m = re.search(r"CAPTURES@(\d+)", self.sendkeys_log_text())
        self.assertIsNotNone(m, "control run never pasted; cannot calibrate")
        return int(m.group(1))

    def test_a_turn_starting_right_before_the_paste_blocks_both_keystrokes(self):
        n = self._captures_before_first_paste()
        # Fresh harness state for the real run, same fake, same read order.
        self.sendkeys_log.write_text(""); self.capture_count.unlink()
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        # CAPTURES@n is the count AT the paste: capture n IS the last read
        # before it (the baseline), and that read must be the one judged.
        self.busy_on_capture_flag.write_text(str(n))
        self.write_task("task-race.txt")
        result = self.run_event("task-race.txt", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.sendkeys_log_text()
        self.assertNotIn("TYPE", log, "pasted into a pane that had just gone busy")
        self.assertNotIn("ENTER", log, "sent Enter into a live turn")
        self.assertIn("not idle-ready at the paste",
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


class MainLoopWiringTest(FakeTmuxHarness):
    """Proves the actual claim: a task file dropped on disk reaches the
    notifier via watch-tasks-stream.sh's real fswatch pipeline, unaided by
    --event. Everything else about dispatch is already covered above."""

    def _wait_for_fswatch_attach(self, timeout=10):
        # A fixed sleep guesses how long fswatch takes to attach; under load
        # that guess can be too short and flakes a real bug-free run.
        needle = str(self.tasks_dir)
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = subprocess.run(
                ["ps", "-axo", "command"], capture_output=True, text=True
            ).stdout
            if any("fswatch" in line and needle in line for line in out.splitlines()):
                return True
            time.sleep(0.1)
        return False

    def test_dropped_task_file_is_picked_up_by_the_real_watcher(self):
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env(),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-live.txt")
            deadline = time.time() + 20
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-live.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("main loop never dispatched the dropped task file:\n"
                          + self.sendkeys_log_text())
            self.write_result("task-live.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_a_queued_task_is_retried_with_no_further_wake_at_all(self):
        # A task left queued at its only wake has no other trigger once no
        # unrelated task arrives -- only the periodic self-poll can retry it.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env({"SUTANDO_NOTIFIER_RETRY_POLL_SEC": "1"}),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-p.txt")
            # Let the (failing) first wake pass, then clear the draft -- no
            # new task file is EVER written from here on.
            time.sleep(1.5)
            self.assertNotIn("TYPE", self.sendkeys_log_text(),
                              "a busy composer must not have been typed over")
            self.pane_file.write_text(IDLE_FOOTER + "\n")
            deadline = time.time() + 10
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-p.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("the periodic self-poll never retried the queued task:\n"
                          + self.sendkeys_log_text())
            self.write_result("task-p.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_claimed_task_is_never_selected_by_an_unrelated_wake(self):
        # next_pending_task must skip a task claimed must-handle, whichever
        # unrelated task's wake triggered the rescan -- see CLAIMS_DIR.
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        claims_dir = self.state_dir / "task-event-handler-claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        self.write_task("task-claimed.txt")
        (claims_dir / "task-claimed.txt").write_text("claimed\n")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env(),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                self._wait_for_fswatch_attach(),
                "fswatch never attached to the watched tasks dir",
            )
            self.write_task("task-unrelated.txt")
            deadline = time.time() + 20
            while time.time() < deadline:
                if "TYPE Sutando task ready: task-unrelated.txt" in self.sendkeys_log_text():
                    break
                time.sleep(0.2)
            else:
                self.fail("main loop never dispatched the unrelated task file:\n"
                          + self.sendkeys_log_text())
            self.assertNotIn(
                "Sutando task ready: task-claimed.txt", self.sendkeys_log_text(),
                "a claimed must-handle task must never be typed into the live core")
            self.write_result("task-unrelated.txt")
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def test_required_task_still_unclaimed_is_skipped_on_a_fresh_probe(self):
        # The pre-claim race: no CLAIMS_DIR entry yet, but a fresh probe must
        # still skip it -- direct against next_pending_task, per the note below.
        handler = self.bin / "fake-handler.sh"
        handler.write_text('''#!/bin/bash
for a in "$@"; do
  case "$a" in --task-file) next=file; continue ;; esac
  if [ "${next:-}" = file ]; then file="$a"; next=""; fi
done
case "$file" in
  */task-protected.txt) exit 4 ;;
  *) exit 3 ;;
esac
''')
        handler.chmod(0o755)
        self.write_task("task-protected.txt")
        time.sleep(0.05)
        self.write_task("task-unrelated2.txt")
        # next_pending_task is a local function, not a CLI subcommand -- run
        # the script's own definitions (above its `--event` gate) as a file.
        functions_only = []
        for line in NOTIFIER.read_text().splitlines():
            if line.startswith('if [ "${1:-}" = "--event" ]'):
                break
            functions_only.append(line)
        probe_script = NOTIFIER.parent / ".probe-test-next-pending.sh"
        probe_script.write_text("\n".join(functions_only) + "\nnext_pending_task\n")
        probe_script.chmod(0o755)
        self.addCleanup(probe_script.unlink, missing_ok=True)
        env = self._env({"SUTANDO_TASK_EVENT_HANDLER": str(handler),
                          "SUTANDO_WORKSPACE_DIR": str(self.tasks_dir.parent)})
        result = subprocess.run(["/bin/bash", str(probe_script)],
                                 env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "task-unrelated2.txt",
            "a required task with no claim file YET must still be skipped, on the "
            f"strength of a fresh probe alone; got {result.stdout!r}, stderr={result.stderr!r}")


    def test_separate_task_inbox_still_probes_the_canonical_workspace(self):
        # A separate task inbox is a supported layout; probed against the inbox's
        # parent instead of the canonical workspace, the handler says "optional".
        canonical = self.root / "workspace"
        inbox = self.root / "deliveries" / "tasks"
        inbox.mkdir(parents=True)
        seen = self.root / "handler-saw.txt"
        handler = self.bin / "fake-handler-ws.sh"
        handler.write_text(f'''#!/bin/bash
ws=""; file=""
while [ $# -gt 0 ]; do
  case "$1" in --workspace) ws="$2"; shift ;; --task-file) file="$2"; shift ;; esac
  shift
done
printf '%s\\n' "$ws" >> "{seen}"
case "$file" in
  */task-protected.txt) [ "$ws" = "{canonical}" ] && exit 4; exit 3 ;;
  *) exit 3 ;;
esac
''')
        handler.chmod(0o755)
        (inbox / "task-protected.txt").write_text("task: say OK\n")
        time.sleep(0.05)
        (inbox / "task-unrelated3.txt").write_text("task: say OK\n")
        functions_only = []
        for line in NOTIFIER.read_text().splitlines():
            if line.startswith('if [ "${1:-}" = "--event" ]'):
                break
            functions_only.append(line)
        probe_script = NOTIFIER.parent / ".probe-test-separate-inbox.sh"
        probe_script.write_text("\n".join(functions_only) + "\nnext_pending_task\n")
        probe_script.chmod(0o755)
        self.addCleanup(probe_script.unlink, missing_ok=True)
        env = self._env({"SUTANDO_TASK_EVENT_HANDLER": str(handler),
                          "SUTANDO_WORKSPACE_DIR": str(canonical),
                          "SUTANDO_TASKS_DIR": str(inbox)})
        result = subprocess.run(["/bin/bash", str(probe_script)],
                                 env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(canonical), seen.read_text(),
                      "the probe must name the canonical workspace, not the inbox's parent")
        self.assertNotIn(str(inbox.parent) + "\n", seen.read_text())
        self.assertEqual(result.stdout.strip(), "task-unrelated3.txt",
                         f"protected task leaked to the core; got {result.stdout!r}")


if __name__ == "__main__":
    unittest.main()
