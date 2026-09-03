#!/usr/bin/env python3
"""Contract tests for scripts/pool-runtime-drive.sh + its two adapters.

The library is the single owner of per-runtime session driving: which pane text
counts as busy, as a menu, as a staged input and as an idle prompt; what the
pool entry says; and which key submits it. Both adapters bind it —
pool-worker-wrapper.sh for the in-session sweep and kick-pool.sh for the watchdog.

The defect being pinned is a guard that failed OPEN. kick-pool's staged-input
test was "if the pane shows a prompt WITH content, skip" — so any pane it could
not parse fell through to typing Claude's slash command. A codex follower's
prompt marker is U+203A, so the guard silently vanished and the watchdog
appended `/proactive-loop-pool pass` into a box that already held one. The
recognition is now positive: type only on a recognized idle prompt.

Pane fixtures are captured from live TUIs, not invented — Claude Code renders
its prompt separator as U+00A0 and codex renders its idle placeholder in SGR
dim (ESC[2m), which is the only thing distinguishing it from staged text.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "pool-runtime-drive.sh"
KICK = REPO / "scripts" / "kick-pool.sh"
WRAPPER = REPO / "scripts" / "pool-worker-wrapper.sh"

NBSP = " "
ESC = "\x1b"

CLAUDE_IDLE = f"⏺ standing by.\n{'─' * 20}\n❯{NBSP}\n{'─' * 20}\n  ⏵⏵ bypass permissions on\n"
CLAUDE_STAGED = CLAUDE_IDLE.replace(f"❯{NBSP}\n", f"❯{NBSP}draft a reply to Chi\n")
CLAUDE_STAGED_NUDGE = CLAUDE_IDLE.replace(
    f"❯{NBSP}\n", f"❯{NBSP}/proactive-loop-pool\n")
CLAUDE_BUSY = "✻ Cogitating… (12s · esc to interrupt)\n" + CLAUDE_IDLE
CLAUDE_MENU = "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel\n"

CODEX_IDLE = (f"{ESC}[1m›{ESC}[0m {ESC}[2mImprove documentation in @filename{ESC}[0m\n"
              "  gpt-5.6-sol xhigh · /repo\n")
CODEX_STAGED = f"{ESC}[1m›{ESC}[0m review the open PRs\n  gpt-5.6-sol xhigh · /repo\n"
CODEX_STAGED_NUDGE = (f"{ESC}[1m›{ESC}[0m Sutando pool mode. You are worker-4. Do not read\n"
                      "  gpt-5.6-sol xhigh · /repo\n")
CODEX_BUSY = (f"{ESC}[1m{ESC}[38;2;128;128;128m•{ESC}[0m Working "
              f"{ESC}[2m(5s • esc to interrupt) · /stop to close{ESC}[0m\n") + CODEX_IDLE
CODEX_MENU = ("  ✨ Update available! 0.147.0 -> 0.149.0\n"
              "› 1. Update now (runs `npm install -g @openai/codex`)\n"
              "  2. Skip\n\n  Press enter to continue\n")

# Records every tmux argv, one arg per line, '--' between calls. Serves pane2
# on the second capture so the post-menu re-read can differ from the first.
STUB_TMUX = """#!/bin/bash
D="$STUB_DIR"
for a in "$@"; do printf '%s\\n' "$a" >> "$D/argv"; done
printf '@@ENDCALL@@\\n' >> "$D/argv"
if [ "$1" = "capture-pane" ]; then
  if [ -e "$D/served" ] && [ -f "$D/pane2" ]; then cat "$D/pane2"; else cat "$D/pane"; fi
  touch "$D/served"
fi
exit 0
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
                       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sutando.worker-4</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>POOL_CLAUDE_BIN</key><string>/nonexistent/claude</string>
{runtime_keys}  </dict>
</dict>
</plist>
"""


def write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


class DriveHarness(unittest.TestCase):
    """Drives the library through a stub tmux and reads back the argv."""

    def drive(self, script: str, td: Path, *, pane="", pane2=None):
        stub = td / "stub-tmux"
        write_exec(stub, STUB_TMUX)
        (td / "pane").write_text(pane)
        if pane2 is not None:
            (td / "pane2").write_text(pane2)
        body = (f'set -u\n. "{LIB}"\n'
                f'tmux_fn() {{ "{stub}" "$@"; }}\n' + script + '\necho "RC=$?"\n')
        env = dict(os.environ, STUB_DIR=str(td))
        r = subprocess.run(["bash", "-c", body], env=env,
                           capture_output=True, text=True, timeout=60)
        argv_file = td / "argv"
        calls, cur = [], []
        if argv_file.exists():
            for line in argv_file.read_text().split("\n"):
                if line == "@@ENDCALL@@":
                    calls.append(cur)
                    cur = []
                else:
                    cur.append(line)
        return r, calls

    def sends(self, calls):
        return [c for c in calls if len(c) > 1 and c[0] == "send-keys"]


class RuntimeResolutionTest(DriveHarness):
    def resolve(self, td: Path, body: str):
        p = td / "core.plist"
        p.write_text(body)
        r = subprocess.run(
            ["bash", "-c", f'set -u\n. "{LIB}"\npool_runtime_from_plist "{p}"'],
            capture_output=True, text=True, timeout=30)
        return r

    def test_declared_runtime_is_read_from_the_plist(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.resolve(Path(t), PLIST.format(
                runtime_keys="    <key>POOL_RUNTIME</key><string>codex</string>\n"
                             "    <key>POOL_RUNTIME_BIN</key><string>/nonexistent/codex</string>\n"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "codex")

    def test_multiline_plist_form_resolves_too(self):
        # plistlib writes key and value on separate lines; the installer's
        # here-doc writes them on one. Both are installed plists.
        with tempfile.TemporaryDirectory() as t:
            r = self.resolve(Path(t), PLIST.format(
                runtime_keys="    <key>POOL_RUNTIME</key>\n    <string>codex</string>\n"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "codex")

    def test_absent_key_is_claude_because_only_claude_predates_the_dimension(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.resolve(Path(t), PLIST.format(runtime_keys=""))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "claude")

    def test_runtime_bin_key_is_not_mistaken_for_the_runtime(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.resolve(Path(t), PLIST.format(
                runtime_keys="    <key>POOL_RUNTIME_BIN</key><string>/nonexistent/codex</string>\n"))
            self.assertEqual(r.stdout, "claude")

    def test_unknown_runtime_is_unresolvable_not_claude(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.resolve(Path(t), PLIST.format(
                runtime_keys="    <key>POOL_RUNTIME</key><string>gemini</string>\n"))
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout, "")

    def test_missing_or_unparseable_file_is_unresolvable(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r = subprocess.run(
                ["bash", "-c",
                 f'set -u\n. "{LIB}"\npool_runtime_from_plist "{td}/absent.plist"'],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout, "")
            r = self.resolve(td, "bplist00\x00\x01binary garbage")
            self.assertEqual(r.returncode, 1,
                             "an unreadable plist must not read as 'absent key'")


class NudgeTextTest(unittest.TestCase):
    def text(self, runtime, worker="worker-4"):
        return subprocess.run(
            ["bash", "-c",
             f'set -u\n. "{LIB}"\npool_drive_nudge_text {runtime} {worker}'],
            capture_output=True, text=True, timeout=30)

    def test_claude_entry_is_the_slash_command(self):
        r = self.text("claude")
        self.assertEqual(r.stdout, "/proactive-loop-pool pass")

    def test_codex_entry_points_at_codex_md_and_names_the_core(self):
        r = self.text("codex")
        self.assertIn("skills/proactive-loop-pool/CODEX.md", r.stdout)
        self.assertIn("worker-4", r.stdout)
        self.assertNotIn("/proactive-loop-pool pass", r.stdout,
                         "codex has no slash-command surface")

    def test_unknown_runtime_yields_nothing(self):
        r = self.text("gemini")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")


class ClaudeKickTest(DriveHarness):
    def kick(self, td, pane, pane2=None):
        return self.drive('pool_drive_kick claude worker-1 tmux_fn worker-1', td,
                          pane=pane, pane2=pane2)

    def test_idle_pane_types_the_slash_command_and_enter(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CLAUDE_IDLE)
            self.assertIn("RC=0", r.stdout, r.stderr)
            self.assertEqual(
                self.sends(calls),
                [["send-keys", "-t", "worker-1", "/proactive-loop-pool pass", "Enter"]])
            self.assertNotIn("-e", calls[0],
                             "claude capture must stay byte-identical (no SGR)")

    def test_busy_pane_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CLAUDE_BUSY)
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [])
            self.assertIn("BUSY", r.stdout)

    def test_menu_pane_escapes_then_re_reads(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CLAUDE_MENU, pane2=CLAUDE_IDLE)
            self.assertIn("RC=0", r.stdout)
            self.assertEqual(
                self.sends(calls),
                [["send-keys", "-t", "worker-1", "Escape"],
                 ["send-keys", "-t", "worker-1", "/proactive-loop-pool pass", "Enter"]])

    def test_staged_pool_entry_is_submitted_not_retyped(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CLAUDE_STAGED_NUDGE)
            self.assertIn("RC=0", r.stdout)
            self.assertEqual(self.sends(calls),
                             [["send-keys", "-t", "worker-1", "Enter"]])

    def test_other_staged_input_is_never_appended_to(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CLAUDE_STAGED)
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [],
                             "typing here appends to text a caller staged")
            self.assertIn("HAS STAGED INPUT", r.stdout)

    def test_unrecognized_pane_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            # No prompt marker at all: a wedged, garbled or foreign TUI.
            r, calls = self.kick(td, "some other program\n$ \n")
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [],
                             "an unrecognized pane must never be typed into")

    def test_empty_pane_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, "")
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [])


class CodexKickTest(DriveHarness):
    def kick(self, td, pane, pane2=None):
        return self.drive('pool_drive_kick codex worker-4 tmux_fn worker-4', td,
                          pane=pane, pane2=pane2)

    def test_idle_pane_types_the_codex_entry_literally_then_ctrl_m(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CODEX_IDLE)
            self.assertIn("RC=0", r.stdout, r.stderr)
            sends = self.sends(calls)
            self.assertEqual(len(sends), 2, sends)
            self.assertEqual(sends[0][:5],
                             ["send-keys", "-t", "worker-4", "-l", "--"])
            self.assertIn("skills/proactive-loop-pool/CODEX.md", sends[0][5])
            self.assertNotIn("/proactive-loop-pool pass", sends[0][5],
                             "claude's slash command must never reach codex")
            self.assertEqual(sends[1], ["send-keys", "-t", "worker-4", "C-m"])
            self.assertIn("-e", calls[0],
                          "codex needs SGR codes to tell placeholder from input")

    def test_idle_is_recognized_whatever_sgr_prefix_codex_uses(self):
        # Live codex emits ESC[0;1m once it has replied, not the ESC[1m a fresh
        # pane shows; pinning one spelling defers a codex core's ONLY sweep.
        for prefix in ("[1m", "[0;1m", "[22;1m"):
            pane = (f"{ESC}{prefix}›{ESC}[0m "
                    f"{ESC}[2mImprove documentation in @filename{ESC}[0m\n"
                    "  gpt-5.6-sol xhigh · /repo\n")
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as t:
                r, calls = self.kick(Path(t), pane)
                self.assertIn("RC=0", r.stdout,
                              f"{prefix} idle prompt must drive: {r.stderr}")
                self.assertEqual(len(self.sends(calls)), 2)

    def test_busy_pane_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CODEX_BUSY)
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [])

    def test_staged_input_is_not_appended_to(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CODEX_STAGED)
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [],
                             "this is exactly the accumulation that wedged worker-4")

    def test_staged_pool_entry_is_submitted(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CODEX_STAGED_NUDGE)
            self.assertIn("RC=0", r.stdout)
            self.assertEqual(self.sends(calls),
                             [["send-keys", "-t", "worker-4", "C-m"]])

    def test_startup_menu_is_skipped_without_pressing_enter(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.kick(td, CODEX_MENU)
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [],
                             "Enter here selects 'Update now' and runs npm")


class UnresolvableRuntimeTest(DriveHarness):
    def test_empty_runtime_types_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.drive('pool_drive_kick "" worker-9 tmux_fn worker-9', td,
                                  pane=CLAUDE_IDLE)
            self.assertIn("RC=2", r.stdout)
            self.assertEqual(self.sends(calls), [],
                             "an unresolvable runtime must not inherit claude's text")
            self.assertIn("UNRESOLVED RUNTIME", r.stdout)

    def test_unknown_runtime_types_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.drive('pool_drive_kick gemini worker-9 tmux_fn worker-9', td,
                                  pane=CLAUDE_IDLE)
            self.assertIn("RC=2", r.stdout)
            self.assertEqual(self.sends(calls), [])


class WrapperNudgeTest(DriveHarness):
    """pool_drive_nudge is the wrapper's sweep: claude unguarded, codex guarded."""

    def test_claude_nudge_is_unconditional_because_its_input_is_a_queue(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.drive('pool_drive_nudge claude worker-1 tmux_fn worker-1', td,
                                  pane=CLAUDE_BUSY)
            self.assertIn("RC=0", r.stdout)
            self.assertEqual(
                self.sends(calls),
                [["send-keys", "-t", "worker-1", "/proactive-loop-pool pass", "Enter"]])

    def test_codex_nudge_defers_while_a_turn_is_running(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.drive('pool_drive_nudge codex worker-4 tmux_fn worker-4', td,
                                  pane=CODEX_BUSY)
            self.assertIn("RC=1", r.stdout)
            self.assertEqual(self.sends(calls), [],
                             "codex interleaves keystrokes into a running turn")


KICK_STUB_TMUX = """#!/bin/bash
D="$STUB_DIR"
for a in "$@"; do printf '%s\\n' "$a" >> "$D/argv"; done
printf '@@ENDCALL@@\\n' >> "$D/argv"
sess=""; prev=""
for a in "$@"; do [ "$prev" = "-t" ] && sess="$a"; prev="$a"; done
case "$1" in
  list-sessions) cat "$D/sessions";;
  capture-pane) [ -f "$D/pane-$sess" ] && cat "$D/pane-$sess";;
esac
exit 0
"""


class KickPoolWiringTest(DriveHarness):
    """The watchdog resolves each session's runtime and drives it accordingly."""

    def run_kick(self, td: Path, cores):
        """cores: {index: (runtime-or-None, pane)}."""
        home = td / "home"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        stub = td / "stub-tmux"
        write_exec(stub, KICK_STUB_TMUX)
        (td / "sessions").write_text(
            "".join(f"worker-{i}\n" for i in cores))
        for i, (runtime, pane) in cores.items():
            keys = ("" if runtime is None
                    else f"    <key>POOL_RUNTIME</key><string>{runtime}</string>\n")
            (agents / f"com.sutando.worker-{i}.plist").write_text(
                PLIST.format(runtime_keys=keys))
            (td / f"pane-worker-{i}").write_text(pane)
        env = dict(os.environ, HOME=str(home), STUB_DIR=str(td),
                   TMUX_BIN=str(stub))
        env.pop("SUTANDO_POOL_SOCKET", None)
        r = subprocess.run(["bash", str(KICK)], env=env,
                           capture_output=True, text=True, timeout=120)
        calls, cur = [], []
        for line in (td / "argv").read_text().split("\n"):
            if line == "@@ENDCALL@@":
                calls.append(cur)
                cur = []
            else:
                cur.append(line)
        return r, calls

    def test_each_session_is_driven_with_its_own_runtimes_entry(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.run_kick(td, {
                1: ("claude", CLAUDE_IDLE),
                4: ("codex", CODEX_IDLE),
            })
            sends = self.sends(calls)
            self.assertEqual(
                [c for c in sends if c[2] == "worker-1"],
                [["send-keys", "-t", "worker-1", "/proactive-loop-pool pass", "Enter"]],
                r.stdout + r.stderr)
            four = [c for c in sends if c[2] == "worker-4"]
            self.assertEqual(len(four), 2, four)
            self.assertIn("skills/proactive-loop-pool/CODEX.md", four[0][5])
            self.assertEqual(four[1][3], "C-m")
            self.assertEqual(r.returncode, 0)
            self.assertIn("kicked 2", r.stdout)

    def test_a_core_whose_plist_names_an_unknown_runtime_is_left_alone(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.run_kick(td, {5: ("gemini", CLAUDE_IDLE)})
            self.assertEqual(self.sends(calls), [],
                             "an unresolvable runtime must not be typed into")
            self.assertEqual(r.returncode, 1)
            self.assertIn("UNRESOLVED RUNTIME", r.stdout)

    def test_a_codex_session_with_staged_text_is_not_appended_to(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, calls = self.run_kick(td, {4: ("codex", CODEX_STAGED)})
            self.assertEqual(self.sends(calls), [],
                             "this is the wedge that stalled the live codex core")
            self.assertEqual(r.returncode, 1)


class AdapterDelegationTest(unittest.TestCase):
    """Structural: the policy must exist in one file, not three.

    A behavioural test cannot see a duplicate that currently agrees — two copies
    in sync pass everything, and the duplication IS the defect (REVIEW.md 14).
    """

    MARKERS = ("❯", "›", "esc to interrupt", "Esc to cancel",
               "/proactive-loop-pool pass", "C-m")

    def test_kick_pool_sources_the_library_and_keeps_no_markers(self):
        src = KICK.read_text()
        self.assertIn('pool-runtime-drive.sh', src)
        self.assertIn("pool_drive_kick", src)
        for marker in self.MARKERS:
            self.assertNotIn(marker, src,
                             f"kick-pool re-implements {marker!r}; "
                             "per-runtime markers belong to the library")

    def test_wrapper_sources_the_library_and_keeps_no_nudge_policy(self):
        src = WRAPPER.read_text()
        self.assertIn('pool-runtime-drive.sh', src)
        self.assertIn("pool_drive_nudge", src)
        self.assertNotIn("send_nudge", src,
                         "the inline nudge must be gone, not bypassed")
        for marker in ("esc to interrupt", "C-m", "/proactive-loop-pool pass"):
            self.assertNotIn(marker, src)

    def test_the_runtime_allowlist_has_one_definition(self):
        # The installer used to carry its own copy of the supported-runtime case.
        installer = (REPO / "scripts" / "install-worker-pool.sh").read_text()
        self.assertIn("pool-runtime-drive.sh", installer)
        self.assertNotIn("pool_runtime_supported() {", installer)


if __name__ == "__main__":
    unittest.main()
