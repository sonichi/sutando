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

EventDispatchTests lives in claude-task-notifier-gate.test.py and
claude-task-notifier-submit.test.py; they load the harness from this file.
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
        # Standby watchers call setsid(); killpg of the notifier cannot reach them,
        # and SIGKILL skips its EXIT trap. Reap before the tmp tree is removed.
        self.addCleanup(self._stop_fixture_watchers)
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
        # One line per has-session probe: the caller's pid and what the marker and results
        # dirs held at that moment; after a paste the notifier probes only once has_result missed.
        self.session_probe_log = self.root / "has-session.log"
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
        # Holds N: a `-l` paste longer than N bytes lands as only its bytes after N,
        # as one tmux write past the CLI's input limit does on a real pane.
        self.cut_paste_over_flag = self.root / "cut-paste-over.flag"
        # Holds N: the Nth and every later `-l` paste is dropped (a chunk the CLI never
        # took), never consumed; paste_count numbers them from 1.
        self.drop_paste_from_flag = self.root / "drop-paste-from.flag"
        self.paste_count = self.root / "paste-count.txt"
        # Window rows (29, a pool worker's): above 29 the box fits and the view flag lapses;
        # resize-window logs `RESIZE -y N enters=<ENTERs so far>`; the flags: exit 1, pane unchanged.
        self.window_rows = self.root / "window-rows.txt"
        self.resize_log = self.root / "resize.log"
        # The window-local window-size option (empty = inherited); resize-window sets it
        # to manual as real tmux does, set-window-option rewrites it and logs `WINOPT`.
        self.window_size_opt = self.root / "window-size-opt.txt"
        self.grow_fails_flag = self.root / "grow-fails.flag"
        self.split_pane_flag = self.root / "split-pane.flag"
        # Holds K: the box shows only its last K rows (needs WRAP_COLS); "K@N" shows K
        # rows from row N (0-based): the box cut by the screen bottom on a short pane.
        self.composer_view_rows_flag = self.root / "composer-view-rows.flag"
        self.view_py = self.root / "view.py"
        self.view_py.write_text(
            "import sys, re\n"
            "k, _, n = sys.argv[1].partition('@'); k = int(k); n = int(n or -1); rows = sys.stdin.read().split('\\n')\n"
            "last = max((i for i, r in enumerate(rows) if r.startswith('\u276f')), default=-1)\n"
            "if last >= 0:\n"
            "    j = last + 1\n"
            "    while j < len(rows) and rows[j].startswith('  ') and '\u23f5\u23f5' not in rows[j] and not re.match(r'^[\\s\u2500-\u257f-]+$', rows[j]): j += 1\n"
            "    box = rows[last:j]\n"
            "    if len(box) > k:\n"
            "        keep = box[-k:] if n < 0 else box[n:n + k]; keep[0] = '\u276f ' + keep[0].strip()\n"
            "        rows[last:j] = keep\n"
            "        if n >= 0: rows[last + k:] = []  # cut by the screen: the frame is off screen too\n"
            "print('\\n'.join(rows))\n")
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
. "{REPO}/tests/lib/tmux-fake-unwrap.sh"
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
# The box's own continuation rows (everything down to the frame) belong to the
# text: a later chunk re-wraps all of it.
end = idx + 1
while wrap > 0 and end < len(lines) and lines[end].strip() and "⏵⏵" not in lines[end] \\
        and not lines[end].startswith("❯") and not re.match(r"^[\\s─-╿]+$", lines[end]):
    row += lines[end].strip() if style == "word" else lines[end]; end += 1
new = row + text
if wrap <= 0:
    rows = [new]
elif style == "word":
    # The real input box: wrap at word boundaries, continuation rows indented two spaces.
    rows = textwrap.wrap(new, width=wrap, subsequent_indent="  ", break_long_words=True,
                         break_on_hyphens=False)
else:
    rows = [new[i:i + wrap] for i in range(0, len(new), wrap)]
lines[idx:end] = rows
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
# -N <n> BSpace: removes the last n chars of the composer's own typed text
# (never the glyph), re-wrapping exactly as append_typed does.
backspace_composer() {{
  python3 - "$PANE" "$1" {self.WRAP_COLS} "{self.WRAP_STYLE}" <<'PYEOF'
import re, sys, textwrap
path, n, wrap, style = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
lines = open(path).read().split("\\n")
if lines and lines[-1] == "": lines.pop()
idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("❯")), None)
if idx is None:
    sys.exit(0)
row = lines[idx]
end = idx + 1
while wrap > 0 and end < len(lines) and lines[end].strip() and "⏵⏵" not in lines[end] \\
        and not lines[end].startswith("❯") and not re.match(r"^[\\s─-╿]+$", lines[end]):
    row += lines[end].strip() if style == "word" else lines[end]; end += 1
content = row[2:] if row.startswith("❯ ") else row
new_content = content[:-n] if 0 < n < len(content) else ""
new = "❯ " + new_content
if wrap <= 0:
    rows = [new]
elif style == "word":
    rows = textwrap.wrap(new, width=wrap, subsequent_indent="  ", break_long_words=True,
                         break_on_hyphens=False) or ["❯ "]
else:
    rows = [new[i:i + wrap] for i in range(0, len(new), wrap)] or ["❯ "]
lines[idx:end] = rows
open(path, "w").write("\\n".join(lines) + "\\n")
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
    printf 'PROBE pid=%s markers=%s results=%s\n' "$PPID" "$(ls "{self.inflight_dir}" 2>/dev/null | tr '\n' ',')" "$(ls "{self.results_dir}" 2>/dev/null | tr '\n' ',')" >> "{self.session_probe_log}"
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
    grown=0; [ "$(cat "{self.window_rows}" 2>/dev/null || echo 29)" -gt 29 ] && grown=1
    [ -f "{self.split_pane_flag}" ] && grown=0
    if [ -f "{self.composer_view_rows_flag}" ] && [ "$grown" = 0 ]; then
      out="$(printf '%s\\n' "$out" | python3 "{self.view_py}" "$(cat "{self.composer_view_rows_flag}")")"
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
      *window_height*) cat "{self.window_rows}" 2>/dev/null || echo 29 ;;

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
      # Real tmux: a trailing ';' is its command separator and is lost; a trailing '\\;' lands as ';'.
      case "$text" in *'\\;') text="${{text%\\\\;}};" ;; *';') text="${{text%;}}" ;; esac
      if [ -f "{self.cut_paste_over_flag}" ] && [ "${{#text}}" -gt "$(cat "{self.cut_paste_over_flag}")" ]; then
        text="${{text:$(cat "{self.cut_paste_over_flag}")}}"
      fi
      pn=$(( $(cat "{self.paste_count}" 2>/dev/null || echo 0) + 1 )); echo "$pn" > "{self.paste_count}"
      if [ -f "{self.swallow_always_flag}" ]; then
        :
      elif [ -f "{self.drop_paste_from_flag}" ] && [ "$pn" -ge "$(cat "{self.drop_paste_from_flag}")" ]; then
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
    elif [ "${{1:-}}" = -N ] && [ "${{3:-}}" = BSpace ]; then
      printf 'BSPACE %s\\n' "$2" >> "{self.sendkeys_log}"
      backspace_composer "$2"
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
  resize-window)
    [ -f "{self.grow_fails_flag}" ] && exit 1
    rows=""; while [ $# -gt 0 ]; do [ "$1" = -y ] && rows="$2"; shift; done
    printf 'RESIZE -y %s enters=%s\\n' "$rows" "$({{ grep -c '^ENTER' "{self.sendkeys_log}" || true; }} 2>/dev/null)" >> "{self.resize_log}"
    echo "$rows" > "{self.window_rows}"
    echo manual > "{self.window_size_opt}"
    exit 0
    ;;
  show-window-options)
    cat "{self.window_size_opt}" 2>/dev/null
    exit 0
    ;;
  set-window-option)
    unset_opt=0; val=""; while [ $# -gt 0 ]; do case "$1" in -u) unset_opt=1 ;; -t) shift ;; window-size) val="${{2:-}}" ;; esac; shift; done
    if [ "$unset_opt" = 1 ]; then : > "{self.window_size_opt}"; echo "WINOPT unset" >> "{self.resize_log}"; else echo "$val" > "{self.window_size_opt}"; echo "WINOPT $val" >> "{self.resize_log}"; fi
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


    def _strays(self):
        """Pids of watch-tasks-stream / fswatch still naming this fixture."""
        needle = str(self.root)
        me = os.getpid()
        found = []
        for pattern in ("watch-tasks-stream", "fswatch"):
            out = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True,
            ).stdout
            for token in out.split():
                if not token.isdigit():
                    continue
                pid = int(token)
                if pid == me or pid in found:
                    continue
                # -ww: macOS `ps -o command=` truncates and would hide the inbox path.
                cmd = subprocess.run(
                    ["ps", "-p", str(pid), "-ww", "-o", "command="],
                    capture_output=True, text=True,
                ).stdout
                if needle in cmd:
                    found.append(pid)
        return found

    def _kill_strays(self, grace=5.0):
        """Stop every watcher this fixture started; returns any that survive."""
        deadline = time.time() + grace
        while time.time() < deadline:
            pids = self._strays()
            if not pids:
                return []
            for pid in pids:
                for killer in (os.killpg, os.kill):
                    try:
                        killer(pid, signal.SIGTERM)
                        break
                    except (ProcessLookupError, PermissionError):
                        continue
            time.sleep(0.3)
        for pid in self._strays():
            for killer in (os.killpg, os.kill):
                try:
                    killer(pid, signal.SIGKILL)
                    break
                except (ProcessLookupError, PermissionError):
                    continue
        time.sleep(0.3)
        return self._strays()

    def _stop_fixture_watchers(self):
        left = self._kill_strays()
        if left:
            raise AssertionError(
                "fixture watcher(s) survived cleanup: %s" % (left,)
            )

    def _env(self, extra=None):
        env = dict(os.environ)
        # A pool worker's shell routes its inbox through these; inherited, they reclassify
        # the fixture's tasks (a deliveries inbox skips the worker-held check).
        for k in ("SUTANDO_INBOX_KIND", "SUTANDO_INBOX_RESOLVER", "SUTANDO_INBOX_RESOLVER_TIMEOUT",
                  "SUTANDO_INSTANCE", "SUTANDO_INSTANCE_ID", "SUTANDO_POOL_DELIVERY_SCRIPT",
                  "SUTANDO_WATCHER_BEAT", "SUTANDO_WATCHER_TRANSITION_HOOK"):
            env.pop(k, None)
        env.update({
            "PATH": f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}",
            # The notifier derives state/ from the workspace and its queue from TMPDIR;
            # inherited values would put both outside the fixture (the live core's, in a core shell).
            "SUTANDO_WORKSPACE_DIR": str(self.tasks_dir.parent),
            "TMPDIR": str(self.root),
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

    def expected_prompt(self, name):
        """The line the notifier types for `name`. ONE definition, pinned to the
        producer by test_the_prompt_names_the_standby_and_the_rearm_command."""
        return (f"Sutando task ready: {name}. Read {self.tasks_dir}/{name}, follow CLAUDE.md, "
                f"complete the task, and write the result to {self.results_dir}/{name}. "
                f"Delivered by the standby: no session-role watcher holds {self.tasks_dir}. "
                f'Re-arm yours via the Monitor tool: bash "{REPO}/src/watch-tasks-stream.sh" '
                f'"{self.tasks_dir}" --role session --inbox "{self.tasks_dir}"')

    def no_result_polls(self, pid, name):
        """has-session probes by pid that found name's marker present and its result absent."""
        n = 0
        for line in (self.session_probe_log.read_text() if self.session_probe_log.exists() else "").splitlines():
            m = re.match(r"PROBE pid=(\d+) markers=(\S*) results=(\S*)$", line)
            if m and int(m.group(1)) == pid and name in m.group(2).split(",") and name not in m.group(3).split(","):
                n += 1
        return n

    def sendkeys_log_text(self):
        return self.sendkeys_log.read_text()


class StandbyReminderTests(FakeTmuxHarness):
    """Every notifier delivery is a standby delivery, so the pane text says so,
    names the re-arm command, and the log records the delivery as the standby's."""

    def test_the_prompt_names_the_standby_and_the_rearm_command(self):
        self.pane_file.write_text(IDLE_FOOTER + "\n")
        self.write_task("task-sb.txt")
        self.run_event("task-sb.txt", timeout=15)
        # The paste goes out in chunks; what they reassemble to is the line typed.
        typed = "".join(l[5:] for l in self.sendkeys_log_text().splitlines() if l.startswith("TYPE "))
        # Equality, not containment: this is what pins expected_prompt() — which the
        # inflight suite stages into its composer — to what the notifier really types.
        self.assertEqual(self.expected_prompt('task-sb.txt'), typed)
        self.assertIn(f"Delivered by the standby: no session-role watcher holds {self.tasks_dir}", typed)
        # The script path is quoted: a desktop install lives under "Application Support".
        self.assertIn(f'Re-arm yours via the Monitor tool: bash "{REPO}/src/watch-tasks-stream.sh" "{self.tasks_dir}" --role session --inbox "{self.tasks_dir}"', typed)
        import shlex
        rearm = typed.split("Re-arm yours via the Monitor tool: ", 1)[1]
        self.assertEqual(shlex.split(rearm)[1], f"{REPO}/src/watch-tasks-stream.sh", "the script path survives shell parsing as ONE word")
        log = (self.logs_dir / "claude-task-notifier.log").read_text()
        self.assertIn(f"delivering task-sb.txt as the standby: no session-role watcher holds {self.tasks_dir}", log)



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


class ComposerBlockPathTests(unittest.TestCase):
    """In-process, so the path owner is measured by coverage, not only via argv."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("util_paths", REPO / "src/util_paths.py")
        self.up = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.up)
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_the_default_instance_keeps_the_bare_name(self):
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID",
                  "AGENT_ID", "SUTANDO_INSTANCE"):
            if k in os.environ:
                self.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
        p = self.up.composer_block_path(self.d)
        self.assertEqual(p, self.d / "task-notifier-composer-block")

    def test_two_instances_get_two_files(self):
        a = self.up.composer_block_path(self.d, instance="w1", agent="@a:x")
        b = self.up.composer_block_path(self.d, instance="w2", agent="@a:x")
        self.assertNotEqual(a, b)
        self.assertEqual({a.parent, b.parent}, {self.d})


if __name__ == "__main__":
    unittest.main()
