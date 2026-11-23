#!/usr/bin/env python3
"""Every core restart goes through src/core_restart.py (one lifecycle authority).

Fails on a new `start-cli.sh --restart` / `--force-restart` caller anywhere in src/, scripts/,
skills/ or packages/ outside ALLOWED. Under the desktop app a direct call skips the app's launch
lock and kills the gateway's session; core_restart.route picks the app's ctl there instead.

A caller is a code line (comments skipped) that names start-cli together with the flag (shell,
Swift, JS/TS), or any Python string literal "--restart" / "--force-restart". Each allowed file
carries its exact count, so a second caller in an allowed file fails too, and so does a stale count.

A relaunch can also run the launcher with no flag (a paused core started again), so a second scan
fails on any direct start-cli.sh / launch-sutando.sh invocation outside ALLOWED_LAUNCH: a shell
`bash`/`sh`/`exec` of either script or of a $...LAUNCHER / $...START_CLI variable, or a Python
string literal naming either script.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import re
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent
SCOPE = ("src", "scripts", "skills", "packages")
SUFFIXES = (".py", ".sh", ".swift", ".ts", ".js", ".mjs", ".cjs")
FLAG = re.compile(r"(?<![\w-])--(?:force-)?restart(?![\w-])")
PY_LITERAL = re.compile(r"""["']--(?:force-)?restart["']""")
LAUNCHER = re.compile(r"start-cli|START_CLI|startCli")
SH_LAUNCH = re.compile(r"""(?:^|[\s;&|(`])(?:bash|sh|exec)\s+(?:-\w+\s+)*["']?"""
                       r"""(?:\$\{?\w*(?:LAUNCHER|START_CLI|launcher)\w*\}?|[^\s"']*(?:start-cli|launch-sutando)\.sh)""")
PY_LAUNCH = re.compile(r"""["'][^"'\s]*(?:start-cli|launch-sutando)\.sh["']""")

#: path -> (matching lines, why it may).
ALLOWED = {
    "src/core_restart.py": (1, "the one route: its start-cli fallback outside the desktop bundle"),
    "src/agent/graceful-restart.sh": (1, "dry-run log text naming the command; the call goes through core_restart"),
    "src/agent/restart-guard.sh": (1, "start-cli's own refusal message"),
    "skills/model-switch/scripts/switch-model.sh": (1, "a refusal message telling the owner the command"),
    "skills/worker-pool/scripts/pool_remedy.py": (1, "the pool tool's own --restart option for a worker, not the core"),
    "src/Sutando/main.swift": (1, "the standalone macOS menu-bar app's Force Restart; never runs in the desktop bundle"),
}

#: path -> (direct launcher invocations, why it may).
ALLOWED_LAUNCH = {
    "src/core_restart.py": (2, "the one route: its start-cli fallback, and the app's launcher for a start-if-down"),
    "src/agent/start-cli.sh": (1, "the dispatcher handing off to the runtime's own launcher"),
    "scripts/start-cli.sh": (1, "the compat shim forwarding to src/agent/start-cli.sh"),
    "src/startup.sh": (1, "the standalone startup entry point; the desktop app runs its own launcher"),
    "src/core-input-watch.py": (1, "names the launcher for core_limit_hold, whose relaunch goes through core_restart"),
    "src/health-check.py": (3, "core_restart's existence check, and the task-notifier repair of a LIVE core"),
    "skills/model-switch/scripts/switch-model.sh": (1, "a refusal message telling the owner the command"),
    "src/agent/graceful-restart.sh": (1, "dry-run log text naming the command; the call goes through core_restart"),
}


def callers(root: Path, files, launch: bool = False):
    """[(path, line number, text)] for every core-restart call shape in `files` (repo-relative);
    with `launch`, every direct launcher invocation instead."""
    hits = []
    for rel in files:
        if not rel.endswith(SUFFIXES):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*")):
                continue
            if launch:
                hit = (PY_LAUNCH if rel.endswith(".py") else SH_LAUNCH).search(line)
            elif rel.endswith(".py"):
                hit = PY_LITERAL.search(line)
            else:
                hit = FLAG.search(line) and LAUNCHER.search(line)
            if hit:
                hits.append((rel, n, stripped))
    return hits


def problems(hits, allowed):
    found = {}
    for rel, n, text in hits:
        found.setdefault(rel, []).append(f"{rel}:{n}: {text}")
    out = []
    for rel, lines in sorted(found.items()):
        count = allowed.get(rel, (0, ""))[0]
        if len(lines) != count:
            out.append(f"{rel}: {len(lines)} line(s), {count} allowed\n    " + "\n    ".join(lines))
    for rel, (count, _why) in sorted(allowed.items()):
        if rel not in found:
            out.append(f"{rel}: allowed {count} but none found; drop it from the allowlist")
    return out


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", *SCOPE], capture_output=True, text=True, check=True)
    return out.stdout.split()


class OneRestartAuthority(unittest.TestCase):
    def test_no_core_restart_caller_outside_the_allowlist(self):
        files = tracked_files()
        examined = [f for f in files if f.endswith(SUFFIXES)]
        self.assertGreater(len(examined), 100, "the scan examined the tree, not nothing")
        self.assertEqual(problems(callers(REPO, files), ALLOWED), [],
                         "restart the core through src/core_restart.py (route / restart), not start-cli.sh --restart")

    def test_no_direct_launcher_invocation_outside_the_allowlist(self):
        self.assertEqual(problems(callers(REPO, tracked_files(), launch=True), ALLOWED_LAUNCH), [],
                         "start or relaunch the core through src/core_restart.py, not the launcher directly")

    def test_the_launch_scan_finds_each_shape_and_skips_comments(self):
        samples = {
            "a.sh": 'bash "$LAUNCHER" < /dev/null\n# bash "$LAUNCHER"\nLAUNCHER="$REPO/src/agent/start-cli.sh"\n',
            "b.sh": 'exec bash "$HERE/launch-sutando.sh"\nsh ${GR_START_CLI}\n',
            "c.py": 'argv = ["/bin/bash", "src/agent/start-cli.sh"]\nx = REPO / "src" / "agent" / "start-cli.sh"\n',
            "d.py": '# "start-cli.sh" in a comment\nprint("starting")\n',
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, body in samples.items():
                (root / name).write_text(body)
            hits = [(rel, n) for rel, n, _t in callers(root, sorted(samples), launch=True)]
        self.assertEqual(hits, [("a.sh", 1), ("b.sh", 1), ("b.sh", 2), ("c.py", 1), ("c.py", 2)])

    def test_the_scan_finds_each_call_shape_and_skips_comments(self):
        # A scanner that matches nothing would pass the test above; these shapes must be found.
        samples = {
            "a.py": 'argv = ["bash", launcher, "--restart"]\n# ["bash", x, "--restart"] in a comment\n',
            "b.sh": 'exec bash "$REPO/src/agent/start-cli.sh" --force-restart\n# start-cli.sh --restart\n',
            "c.sh": 'bash "${GR_START_CLI}" --restart\nbash tool --restart-core\n',
            "d.ts": 'spawn("bash", [startCli, "--restart"])\n',
            "e.md": 'start-cli.sh --restart\n',
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, body in samples.items():
                (root / name).write_text(body)
            hits = [(rel, n) for rel, n, _t in callers(root, sorted(samples))]
        self.assertEqual(hits, [("a.py", 1), ("b.sh", 1), ("c.sh", 1), ("d.ts", 1)])


if __name__ == "__main__":
    unittest.main()
