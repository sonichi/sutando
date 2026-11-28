#!/usr/bin/env python3
"""A test that launches the real watcher builds its env from a clean base.

Run from a pool worker's shell, an inherited SUTANDO_WORKSPACE_DIR / RESULTS_DIR /
INSTANCE_ID points the watcher under test at the LIVE workspace, where it stamps and
then removes the seat's own readiness sentinel. Python tests use
tests/fixtures/clean_watcher_env.py; shell tests source tests/fixtures/clean-watcher-env.sh.

Run: python3 tests/watcher-tests-never-inherit-a-seat-env.test.py
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []

SCRIPT = "watch-tasks-stream.sh"
SPAWN = {"Popen", "run", "check_output", "check_call", "call"}
# These spawn a stand-in at the watcher's name (a fake root, a stub, an argv probe), never the real one.
NOT_THE_REAL_WATCHER = {
    "tests/codex-core-launcher.test.py",
    "tests/skills/worker-pool/worker-bootstrap-decision.test.py",
    "tests/watcher-identity.test.py",
}
SH_LAUNCH = re.compile(r'bash\s+("\$REPO/src/watch-tasks-stream\.sh"|"\$WATCHER")\s')


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _mentions(node, names: "set[str]") -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and SCRIPT in n.value:
            return True
        if isinstance(n, ast.Name) and n.id in names:
            return True
    return False


def launches_watcher(source: str) -> bool:
    """True when a subprocess call's argv names the watcher script, directly or via a variable."""
    tree = ast.parse(source)
    names: "set[str]" = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and _mentions(n.value, set()):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args:
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in SPAWN and _mentions(n.args[0], names):
                return True
    return False


def offenders() -> list[str]:
    bad = []
    for p in sorted((REPO / "tests").rglob("*.test.*")):
        try:
            s = p.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(p.relative_to(REPO))
        if rel in NOT_THE_REAL_WATCHER:
            continue
        if p.suffix == ".py" and SCRIPT in s and launches_watcher(s) and "clean_env" not in s:
            bad.append(f"{rel}: launches the watcher without clean_env()")
        if p.suffix == ".sh" and SH_LAUNCH.search(s) and "fixtures/clean-watcher-env.sh" not in s:
            bad.append(f"{rel}: launches the watcher without sourcing clean-watcher-env.sh")
    return bad


def main() -> int:
    bad = offenders()
    check("every watcher-launching test starts from a clean env", not bad, "\n    " + "\n    ".join(bad))
    leaked = {"SUTANDO_WORKSPACE_DIR": "/nonexistent-live-ws", "SUTANDO_INSTANCE_ID": "f" * 32,
              "SUTANDO_RESULTS_DIR": "/nonexistent-live-ws/results", "TMUX": "x", "GIT_DIR": "/x",
              "CLAUDECODE": "1", "KEEP_ME": "1"}
    env = {**os.environ, **leaked}
    sh = subprocess.run(["bash", "-c", f'. "{REPO}/tests/fixtures/clean-watcher-env.sh"; env'],
                        env=env, capture_output=True, text=True).stdout
    names = {ln.split("=", 1)[0] for ln in sh.splitlines() if "=" in ln}
    check("the shell scrub drops every seat variable and keeps the rest",
          not (names & (set(leaked) - {"KEEP_ME"})) and "KEEP_ME" in names, sorted(names & set(leaked)))
    sys.path.insert(0, str(REPO / "tests" / "fixtures"))
    import clean_watcher_env
    os.environ.update(leaked)
    py = clean_watcher_env.clean_env()
    check("the shell scrub and clean_env() drop the same names",
          {k for k in leaked if k not in py} == set(leaked) - names)
    print("\nPASS" if not FAILURES else f"\nFAIL — {len(FAILURES)} check(s) failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
