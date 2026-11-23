#!/usr/bin/env python3
"""Every core restart goes through src/core_restart.py (one lifecycle authority).

Fails on a new `start-cli.sh --restart` / `--force-restart` caller anywhere in src/, scripts/,
skills/ or packages/ outside ALLOWED. Under the desktop app a direct call skips the app's launch
lock and kills the gateway's session; core_restart.route picks the app's ctl there instead.

A caller is a code line (comments skipped) that names start-cli together with the flag (shell,
Swift, JS/TS), or any Python string literal "--restart" / "--force-restart". Each allowed file
carries its exact count, so a second caller in an allowed file fails too, and so does a stale count.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCOPE = ("src", "scripts", "skills", "packages")
SUFFIXES = (".py", ".sh", ".swift", ".ts", ".js", ".mjs", ".cjs")
FLAG = re.compile(r"(?<![\w-])--(?:force-)?restart(?![\w-])")
PY_LITERAL = re.compile(r"""["']--(?:force-)?restart["']""")
LAUNCHER = re.compile(r"start-cli|START_CLI|startCli")

#: path -> (matching lines, why it may).
ALLOWED = {
    "src/core_restart.py": (1, "the one route: its start-cli fallback outside the desktop bundle"),
    "src/agent/graceful-restart.sh": (1, "dry-run log text naming the command; the call goes through core_restart"),
    "src/agent/restart-guard.sh": (1, "start-cli's own refusal message"),
    "skills/model-switch/scripts/switch-model.sh": (1, "a refusal message telling the owner the command"),
    "skills/worker-pool/scripts/pool_remedy.py": (1, "the pool tool's own --restart option for a worker, not the core"),
    "src/Sutando/main.swift": (1, "the standalone macOS menu-bar app's Force Restart; never runs in the desktop bundle"),
}


def callers(root: Path, files):
    """[(path, line number, text)] for every core-restart call shape in `files` (repo-relative)."""
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
            if rel.endswith(".py"):
                hit = PY_LITERAL.search(line)
            else:
                hit = FLAG.search(line) and LAUNCHER.search(line)
            if hit:
                hits.append((rel, n, stripped))
    return hits


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", *SCOPE], capture_output=True, text=True, check=True)
    return out.stdout.split()


class OneRestartAuthority(unittest.TestCase):
    def test_no_core_restart_caller_outside_the_allowlist(self):
        files = tracked_files()
        examined = [f for f in files if f.endswith(SUFFIXES)]
        self.assertGreater(len(examined), 100, "the scan examined the tree, not nothing")
        found = {}
        for rel, n, text in callers(REPO, files):
            found.setdefault(rel, []).append(f"{rel}:{n}: {text}")
        problems = []
        for rel, lines in sorted(found.items()):
            allowed = ALLOWED.get(rel, (0, ""))[0]
            if len(lines) != allowed:
                problems.append(f"{rel}: {len(lines)} caller line(s), {allowed} allowed\n    " + "\n    ".join(lines))
        for rel, (count, _why) in sorted(ALLOWED.items()):
            if rel not in found:
                problems.append(f"{rel}: allowed {count} but none found; drop it from ALLOWED")
        self.assertEqual(problems, [], "restart the core through src/core_restart.py (route / restart), "
                         "not start-cli.sh --restart:\n" + "\n".join(problems))

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
