#!/usr/bin/env python3
"""Every tmux target that names a Sutando session does so exactly (`=name`).

tmux resolves a bare `-t sutando-core` by PREFIX when no session has that exact name,
so with the core gone it lands on `sutando-core-watcher`: a capture reads the watcher's
pane as the core's, a send-keys types into it, a kill-session kills it. The same holds
for `sutando-worker-<id>` against its `-input`/`-watcher` siblings. This scan of src/ and
scripts/ keeps the next call site from reintroducing it. FOLLOW_UP counts the bare targets
still standing in files outside this fix; the count may only fall, never grow.

Run: python3 tests/tmux-session-targets-are-exact.test.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("src", "scripts")
SUFFIXES = {".sh", ".py", ".swift", ".ts"}

# Shell shapes count only on a line that runs tmux: `mktemp -t` and `ls -t` are not targets.
TMUX_ONLY = [re.compile(r"""-t\s+["']?\$\{?\w*SESSION\w*"""), re.compile(r"""-t\s+["']?sutando-""")]
# Python/TS argv, a Swift attach, and a shell TARGET default, each with no `=` before the name.
BARE = [
    re.compile(r"""["']-t["'],\s*f?["']\{\w*(session|name|seat)\w*\}""", re.I),
    re.compile(r"""["']-t["'],\s*(session|name|seat|SESSION)\b"""),
    re.compile(r"""-t\s+'\\\(session\)"""),
    re.compile(r"""\w*TARGET=["']?\$\{\w+:-\$\w*SESSION"""),
]


# Launchers, attach helpers and the shared sender: a separate fix, so the number here can only fall.
FOLLOW_UP = {
    "scripts/tmux-send-line.sh": 3,
    "src/Sutando/main.swift": 1,
    "src/agent/agy/cli/start-cli.sh": 3,
    "src/agent/claude/cli/start-cli.sh": 11,
    "src/agent/claude/cli/task-notifier.sh": 1,
    "src/agent/codex/cli/start-cli.sh": 4,
    "src/agent/codex/cli/task-notifier-supervisor.sh": 1,
    "src/runtime-api/instance_registry.py": 1,
    "src/runtime-cli/sutando-runtime.py": 1,
    "src/tmux-status.ts": 1,
}


def offenders(text: str) -> list[tuple[int, str]]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("#", "//")):
            continue
        if any(p.search(line) for p in BARE) or ("tmux" in line and any(p.search(line) for p in TMUX_ONLY)):
            out.append((n, line.strip()))
    return out


class ExactTargets(unittest.TestCase):
    def test_no_bare_session_target_in_src_or_scripts(self):
        files = subprocess.run(["git", "ls-files", *ROOTS], cwd=REPO, capture_output=True,
                               text=True, check=True).stdout.split()
        found, grew = {}, []
        for rel in files:
            path = REPO / rel
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            hits = offenders(path.read_text(errors="replace"))
            if hits:
                found[rel] = len(hits)
            if len(hits) > FOLLOW_UP.get(rel, 0):
                grew += [f"{rel}:{n}: {line}" for n, line in hits]
        self.assertEqual(grew, [], "bare tmux session targets prefix-match a sibling:\n" + "\n".join(grew))
        fixed = {f: (n, found.get(f, 0)) for f, n in FOLLOW_UP.items() if found.get(f, 0) < n}
        self.assertEqual(fixed, {}, "fewer bare targets than FOLLOW_UP lists: lower the count")

    def test_the_scan_catches_each_shape_and_passes_the_exact_ones(self):
        bad = ['tmux send-keys -t "$TMUX_SESSION" Enter', "tmux has-session -t sutando-core",
               '["tmux", "-S", s, "capture-pane", "-p", "-t", f"{session}:0"]',
               '["tmux", "has-session", "-t", name]', "exec tmux attach -t '\\(session)'",
               'TARGET="${SUTANDO_TMUX_PANE:-$SESSION:$CORE_WINDOW}"']
        good = ['tmux send-keys -t "=$TMUX_SESSION:$idx" Enter', "tmux has-session -t =sutando-core",
                'tmp="$(mktemp -t sutando-x.XXXXXX)"', 'ls -t "$SESSION_DIR"/*.jsonl',
                '["tmux", "capture-pane", "-p", "-t", f"={session}:0"]', '["tmux", "-t", target]',
                'TARGET="${SUTANDO_TMUX_PANE:-=$SESSION:$CORE_WINDOW}"', "# tmux -t sutando-core in a comment"]
        self.assertEqual([len(offenders(b)) for b in bad], [1] * len(bad))
        self.assertEqual([offenders(g) for g in good], [[]] * len(good))


if __name__ == "__main__":
    sys.exit(unittest.main())
