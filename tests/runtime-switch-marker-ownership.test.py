#!/usr/bin/env python3
"""The active-runtime marker is owned by whichever launcher actually comes up.

A refused or failed restart must leave the previous marker truthful, so the
switch path may write desired state only.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SWITCH = REPO / "src" / "agent" / "start-cli.sh"
CLAUDE = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
CODEX = REPO / "src" / "agent" / "codex" / "cli" / "start-cli.sh"


class MarkerOwnership(unittest.TestCase):
    def test_switch_path_never_writes_the_active_marker(self):
        code = re.sub(r"(?m)^\s*#.*$", "", SWITCH.read_text(encoding="utf-8"))
        self.assertNotIn("core-runtime.json", code,
                         "the switch path must write desired state only")

    def test_both_launchers_publish_the_marker(self):
        for f in (CLAUDE, CODEX):
            self.assertIn("core-runtime.json", f.read_text(encoding="utf-8"),
                          f"{f.name} must publish the active runtime when it comes up")

    def test_the_detached_publish_sits_behind_the_liveness_gate(self):
        """Presence is not the question — ordering is. A publish before the gate
        can replace a truthful marker with a runtime that never came up."""
        src = CLAUDE.read_text(encoding="utf-8")
        gate = src.index("did not come up within")
        pub = src.index("publish_active_runtime", src.index("new-session -d"))
        self.assertGreater(pub, gate,
                           "the detached path must publish only after the liveness check")

    def test_every_codex_publish_sits_inside_a_liveness_gate(self):
        """qingyun-wu's standing P1 on this PR: the codex launcher published the
        marker BEFORE new-session could succeed. An offset comparison cannot check
        this — branch A's publish legitimately precedes branch B's launch in an
        if/elif — so assert the GATE each call sits under, not its position."""
        src = CODEX.read_text(encoding="utf-8").splitlines()
        calls = [i for i, l in enumerate(src)
                 if l.strip() == "publish_active_runtime"]
        self.assertTrue(calls, "the codex launcher never publishes")
        for i in calls:
            gate = next((src[j] for j in range(i - 1, max(0, i - 12), -1)
                         if src[j].lstrip().startswith("if ")), "")
            self.assertIn("session_exists", gate,
                          f"codex publish at line {i+1} is not under a session_exists gate "
                          f"(nearest if: {gate.strip()!r}) — a launch that never came up "
                          f"would overwrite a truthful marker")

    # Region keys. Each is asserted UNIQUE before use: a non-unique anchor plus
    # str.index() silently slices the wrong region and the suite still passes.
    HEAL_OPEN = "if tmux_session_exists; then"
    BARE_OPEN = "if ! command -v tmux > /dev/null 2>&1; then"
    TTY_INNER = "ensure_core_monitor   # backgrounded child survives the exec below"
    DET_INNER = "ensure_core_monitor   # canonical session now exists"
    TTY_OPEN = "if [ -t 1 ]; then"

    def _unique(self, src, pat, label):
        self.assertEqual(src.count(pat), 1,
                         f"{label} anchor is not unique ({src.count(pat)} matches) — "
                         f"region slicing would silently move")
        return src.index(pat)

    @staticmethod
    def _matching_fi(src, open_off):
        """Offset just past the `fi` that closes the branch opening at open_off.
        Without this the regions tile contiguously, every offset belongs to some
        shape, and "outside every shape" becomes unreachable."""
        depth = 0
        for m in re.finditer(r"^[ \t]*(if|fi)\b", src[open_off:], re.M):
            depth += 1 if m.group(1) == "if" else -1
            if depth == 0:
                return open_off + m.end()
        return len(src)

    def _regions(self, src):
        """[(name, start, end)] for the four launch shapes, each sliced from the
        branch that opens it to that branch's own `fi` — gaps are common code."""
        heal = self._unique(src, self.HEAL_OPEN, "heal")
        bare = self._unique(src, self.BARE_OPEN, "bare")
        tty_in = self._unique(src, self.TTY_INNER, "tty-inner")
        det_in = self._unique(src, self.DET_INNER, "detached-inner")
        tty = src.rindex(self.TTY_OPEN, 0, tty_in)
        els = src.rindex("\nelse\n", tty_in, det_in)
        self.assertLess(heal, bare, "heal must precede the bare path")
        self.assertLess(bare, tty, "bare path must precede the tty/detached block")
        return [("heal", heal, self._matching_fi(src, heal)),
                ("bare no-tmux", bare, self._matching_fi(src, bare)),
                ("tty", tty, els),
                ("detached", els, self._matching_fi(src, tty))]

    def _publish_calls(self, src):
        return [i for i in range(len(src))
                if src.startswith("publish_active_runtime", i)
                and not src.startswith("publish_active_runtime() {", i)]

    def test_every_publish_call_lives_inside_a_known_launch_shape(self):
        """Replaces the old positional guard, which used "before the first
        `new-session -d`" as a proxy for ungated. That proxy held only while the
        detached branch was both the first launch and the sole publisher."""
        src = CLAUDE.read_text(encoding="utf-8")
        regions = self._regions(src)
        calls = self._publish_calls(src)
        self.assertTrue(calls, "the launcher never publishes at all")
        for i in calls:
            owner = [n for n, a, b in regions if a <= i < b]
            self.assertTrue(owner, f"publish at offset {i} sits outside every "
                                   f"known launch shape (common region)")

    def test_each_launch_shape_publishes(self):
        """A fifth shape, or one that quietly stops publishing, must declare itself
        here. The bare path is included: its publish is paired with a restore."""
        src = CLAUDE.read_text(encoding="utf-8")
        calls = self._publish_calls(src)
        for name, a, b in self._regions(src):
            self.assertTrue([i for i in calls if a <= i < b],
                            f"the {name} shape never publishes")

    def test_the_fresh_tty_launch_publishes_after_its_gate(self):
        """It verifies liveness then execs attach, and published nothing — so a
        Codex->Claude switch from a terminal left the marker naming codex."""
        src = CLAUDE.read_text()
        attach = src.rindex('exec tmux -S "$TMUX_SOCKET" attach')   # LAST = fresh launch
        gate = src.rindex("tmux_core_session_running", 0, attach)
        pub = src.rindex("publish_active_runtime", 0, attach)
        self.assertGreater(pub, gate, "must publish AFTER the liveness gate")
        self.assertLess(pub, attach, "must publish BEFORE it execs attach")

    def test_the_heal_path_publishes_inside_its_liveness_gate(self):
        """A heal starts a real Claude core in an existing session. Not named in
        the review; found by enumerating every attach site rather than two."""
        src = CLAUDE.read_text()
        heal = src.index('echo "Attaching to healed')
        gate = src.rindex("if tmux_core_session_running; then", 0, heal)
        pub = src.rindex("publish_active_runtime", 0, heal)
        self.assertGreater(pub, gate,
                           "the heal path must publish inside its liveness gate")

    def test_attaching_to_an_ALREADY_RUNNING_core_must_not_publish(self):
        """The control on the three fixes above. That core may be codex; attaching
        does not make it claude, so a publish-everywhere correction breaks this."""
        src = CLAUDE.read_text()
        existing = src.index('echo "Attaching to existing')
        attach = src.index('exec tmux -S "$TMUX_SOCKET" attach', existing)
        gate = src.rindex("if tmux_core_session_running; then", 0, existing)
        between = src[gate:attach]   # window must reach the ATTACH, not just the echo
        self.assertNotIn("publish_active_runtime", between,
                         "attaching to an existing core must NOT claim the runtime")

    def test_the_publish_function_is_defined_before_every_call(self):
        """Shell resolves functions at call time, so a call above the definition is
        'command not found' at runtime and `bash -n` cannot see it."""
        src = CLAUDE.read_text()
        definition = src.index("publish_active_runtime() {")
        calls = [i for i in range(len(src))
                 if src.startswith("publish_active_runtime", i)
                 and not src.startswith("publish_active_runtime() {", i)]
        self.assertTrue(calls, "no call sites at all")
        self.assertTrue(all(i > definition for i in calls),
                        "a publish call precedes the function definition")

    def test_a_refused_switch_leaves_the_previous_marker_truthful(self):
        """Codex->Claude: the switch runs, the restart never does."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True)
            before = {"runtime": "codex", "session": "sutando-core", "started_at": 1}
            (ws / "core-runtime.json").write_text(json.dumps(before), encoding="utf-8")

            # everything the switch path does, minus the launcher it never reaches
            src = SWITCH.read_text(encoding="utf-8")
            i = src.index('if [ -n "$requested_runtime" ]')
            j = src.index("\nfi\n", i)
            snippet = src[i:j]
            harness = (
                "set -euo pipefail\n"
                f'REPO="{REPO}"\n'
                'requested_runtime="claude"\n'
                f'sutando_config() {{ printf "%s" "{Path(tmp) / "workspace"}"; }}\n'
                + snippet.replace(
                    'bash "$REPO/scripts/sutando-config.sh" workspace', 'sutando_config')
                + "\nfi\n"
            )
            subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True,
                           cwd=tmp)

            after = json.loads((ws / "core-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(after, before,
                             "a switch that never launched must not rewrite the marker")


    def test_codex_publishes_only_after_the_session_is_created(self):
        """Ordering, not presence. Publishing before creation replaces a
        truthful marker with a runtime that never came up."""
        src = CODEX.read_text(encoding="utf-8")
        launch = src.index("new-session -d")
        self.assertGreater(src.index("publish_active_runtime", launch), launch,
                           "codex must publish only after tmux new-session")

    # Claude executable branch controls. Slices are asserted unique before use,
    # because a non-unique anchor silently slices the wrong region.
    HEAL_BLOCK = (r'(?m)^  if tmux_core_session_running; then\n'
                  r'    clear_shutdown_sentinel\n(?:.*\n)*?  fi\n')
    NEG_GATE = (r'(?m)^  if ! tmux_core_session_running; then\n'
                r'(?:.*\n)*?  fi\n(?:.*\n)*?^  publish_active_runtime.*\n')
    BARE_PUBLISH = r'(?m)^  if stash_active_runtime; then publish_active_runtime; fi\n'
    BARE_RESTORE = r'(?m)^  restore_active_runtime\n'

    def _claude_fns(self, src, *names):
        out = []
        for n in names:
            i = src.index(n + "() {")
            out.append(src[i:src.index("\n}\n", i) + 3])
        return "".join(out)

    def _drive_claude(self, tmp, body, live, fns=("publish_active_runtime",)):
        """Run a real sliced branch with liveness forced to `live`. Returns the
        marker's content afterwards, or None when no marker exists."""
        ws = Path(tmp) / "workspace"
        (ws / "state").mkdir(parents=True, exist_ok=True)
        src = CLAUDE.read_text(encoding="utf-8")
        harness = (
            "set -uo pipefail\n"
            f'REPO="{REPO}"\nSESSION="sutando-core"\nTMUX_SOCKET="/tmp/none"\n'
            'RESTART_REQUESTED=""\nVISIBLE=0\n'
            f'sutando_config() {{ printf "%s" "{ws}"; }}\n'
            f"tmux_core_session_running() {{ return {0 if live else 1}; }}\n"
            "clear_shutdown_sentinel() { :; }\nlog_restart_attempt() { :; }\n"
            "ensure_core_monitor() { :; }\nopen_visible_terminal() { :; }\n"
            "tmux_session_exists() { return 0; }\nstash_shutdown_sentinel() { :; }\n"
            "tmux() { return 0; }\nsleep() { :; }\necho() { :; }\n"
            "CORE_CMD=(true)\nhealed_idx=0\n"
            + self._claude_fns(src, *fns).replace(
                'bash "$REPO/scripts/sutando-config.sh" workspace', "sutando_config")
            + "\n" + body + "\n"
        )
        try:
            subprocess.run(["/bin/bash", "-c", harness], capture_output=True,
                           text=True, cwd=tmp, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("sliced branch hung — a stub is missing")
        f = ws / "state" / "core-runtime.json"
        return f.read_text() if f.exists() else None

    def _slice(self, pattern, label, expect=1, index=0):
        src = CLAUDE.read_text(encoding="utf-8")
        ms = list(re.finditer(pattern, src))
        self.assertEqual(len(ms), expect,
                         f"{label}: expected {expect} slice(s), found {len(ms)} — a "
                         f"launch shape was added or removed without updating this test")
        return ms[index].group(0)

    def test_claude_shapes_publish_on_success_and_not_on_failed_start(self):
        """Eight arms. A shape that publishes when liveness is FALSE is the
        optimistic-write defect; one that stays silent when TRUE loses the switch."""
        # Sliced from each branch OPENER, not its gate: an ungated publish placed
        # before the launch must fall INSIDE the slice or the harness cannot see it.
        src = CLAUDE.read_text(encoding="utf-8")
        regions = {n: src[a:b] for n, a, b in self._regions(src)}
        # A region sliced at its opener is unbalanced shell (`if..then` with no `fi`),
        # so drop the opener line and run the body; inner if/fi pairs are intact.
        def body(text):
            head, _, rest = text.lstrip("\n").partition("\n")
            self.assertRegex(head.strip(), r"^(if |else$)", "unexpected region opener")
            return rest
        shapes = (("heal", regions["heal"]), ("tty", body(regions["tty"])),
                  ("detached", body(regions["detached"])))
        for name, body in shapes:
            for live in (True, False):
                with tempfile.TemporaryDirectory() as tmp:
                    got = self._drive_claude(tmp, body, live)
                if live:
                    self.assertIsNotNone(got, f"{name} did not publish on a live core")
                    self.assertIn('"runtime":"claude"', got, f"{name} published junk")
                else:
                    self.assertIsNone(got, f"{name} published on a FAILED start — the "
                                           f"optimistic-write defect")

    def test_the_bare_rollback_survives_a_REAL_failed_exec(self):
        """qingyun-wu at e923f0bfc: the test above concatenates the publish and
        restore slices and omits the exec between them, so it cannot see that a
        failed exec ENDS a non-interactive bash and every line after it is dead.
        `set +e` does not change that; only `shopt -s execfail` does.

        This drives ONE contiguous region containing the exec, with a `claude`
        that passes `command -v` and fails to exec (bad interpreter) — the exact
        shape the reviewer used.
        """
        src = CLAUDE.read_text(encoding="utf-8")
        start = src.index("  stash_shutdown_sentinel")
        end = src.index('exit "$_exec_rc"', start) + len('exit "$_exec_rc"')
        region = src[start:end]
        self.assertIn("exec claude", region,
                      "the slice must CONTAIN the exec — omitting it is the defect")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "claude").write_text("#!/nonexistent/interp\n")
            (d / "claude").chmod(0o755)
            marker = d / "marker"
            marker.write_text("PUBLISHED")
            script = (
                "#!/bin/bash\nset -e\n"
                f'export PATH="{d}:$PATH"\n'
                'SESSION=x; SETTINGS_ARGS=()\n'
                'stash_shutdown_sentinel() { :; }; clear_shutdown_sentinel() { :; }\n'
                'restore_shutdown_sentinel() { :; }\n'
                'stash_active_runtime() { return 0; }\n'
                'publish_active_runtime() { :; }\n'
                f'restore_active_runtime() {{ rm -f "{marker}"; }}\n'
                + region + "\n"
            )
            sp = d / "bare.sh"
            sp.write_text(script)
            r = subprocess.run(["bash", str(sp)], capture_output=True, text=True)
            # Read INSIDE the with-block: TemporaryDirectory deletes the tree on
            # exit, so a marker.exists() after it is False no matter what ran.
            survived = marker.exists()
        self.assertFalse(survived,
                         f"the rollback never ran, so the marker stayed published "
                         f"after a failed exec (rc={r.returncode}, "
                         f"stderr={r.stderr.strip()[:160]!r})")

    def test_the_bare_path_publishes_on_success_and_restores_on_failure(self):
        """Its exec IS the core, so it cannot verify first; the pairing is the proof.
        absent != cleared for a marker, so the failure arm must UNLINK."""
        pub = self._slice(self.BARE_PUBLISH, "bare-publish")
        res = self._slice(self.BARE_RESTORE, "bare-restore")
        fns = ("stash_active_runtime", "restore_active_runtime", "publish_active_runtime")
        with tempfile.TemporaryDirectory() as tmp:
            got = self._drive_claude(tmp, pub, True, fns=fns)
        self.assertIsNotNone(got, "the bare path never published")
        self.assertIn('"runtime":"claude"', got)
        with tempfile.TemporaryDirectory() as tmp:
            got = self._drive_claude(tmp, pub + res, True, fns=fns)
        self.assertIsNone(got, "a failed exec with NO previous marker must leave none "
                               "— copying back an empty stash forges a claude marker")
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "core-runtime.json").write_text('{"runtime":"codex"}\n')
            got = self._drive_claude(tmp, pub + res, True, fns=fns)
        self.assertIsNotNone(got, "a failed exec erased a truthful previous marker")
        self.assertIn('"runtime":"codex"', got, "the previous marker was not restored")

    def _run_codex_publish(self, tmp, tmux_rc):
        """The real publish function plus its real guarded call site, with tmux
        forced to `tmux_rc`. Slicing further would cut an unbalanced if-block."""
        src = CODEX.read_text(encoding="utf-8")
        i = src.index("publish_active_runtime() {")
        fn = src[i:src.index("\n}\n", i) + 3]
        m = re.search(r'(?m)^  if session_exists "\$SESSION"; then\n(?:.*\n)*?  fi\n', src)
        self.assertTrue(m, "no gated publish call in the codex launcher — the "
                           "publish is ungated, which is the defect this pins")
        self.assertIn("publish_active_runtime", m.group(0),
                      "the liveness gate exists but does not publish inside it")
        call = m.group(0)
        harness = (
            "set -uo pipefail\n"
            f'REPO="{REPO}"\nSESSION="sutando-core"\nTMUX_SOCKET="/tmp/none"\n'
            f'sutando_config() {{ printf "%s" "{Path(tmp) / "workspace"}"; }}\n'
            f"tmux() {{ return {tmux_rc}; }}\n"
            # the gate now routes through session_exists; without BOTH helpers it
            # is always false and the rc=0 positive control could never fire.
            "tmux_available() { return 0; }\n"
            'session_exists() { tmux_available && tmux -S "$TMUX_SOCKET" has-session -t "=$1" 2>/dev/null; }\n'
            "sleep() { :; }\n"
            "clear_shutdown_sentinel() { :; }\n"
            + fn.replace('bash "$REPO/scripts/sutando-config.sh" workspace', "sutando_config")
            + "\n" + call + "\n"
        )
        subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True, cwd=tmp)

    def test_a_failed_codex_launch_leaves_the_previous_marker_truthful(self):
        """Claude->Codex where tmux refuses: the marker must still say claude."""
        before = {"runtime": "claude", "session": "sutando-core", "started_at": 1}
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True)
            (ws / "core-runtime.json").write_text(json.dumps(before), encoding="utf-8")
            self._run_codex_publish(tmp, 42)
            after = json.loads((ws / "core-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(after, before,
                             "a codex launch that never came up must not rewrite the marker")

        # Positive control: a snippet that does nothing would also leave the
        # marker unchanged, so prove the same harness DOES publish on success.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True)
            (ws / "core-runtime.json").write_text(json.dumps(before), encoding="utf-8")
            self._run_codex_publish(tmp, 0)
            after = json.loads((ws / "core-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(after.get("runtime"), "codex",
                             "control failed: the harness never publishes, so the "
                             "failure case above proves nothing")


if __name__ == "__main__":
    unittest.main(verbosity=0)
