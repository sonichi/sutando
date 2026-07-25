#!/usr/bin/env python3
"""_run_codex_subprocess builds a --skip-git-repo-check argv (PR #2155 coverage).

The mod-judge codex wrapper spawns `codex exec` from the repo cwd, which codex
refuses ("Not inside a trusted directory") without --skip-git-repo-check. This
covers the argv-construction line by patching create_subprocess_exec and
asserting the flag is present (and -m <model> is appended only when given).

Run: python3 tests/discord-bridge-codex-subprocess-argv.test.py  (exit 0/1)
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken")
os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/dc-codex-argv-ccd"

try:
    import discord  # noqa: F401
except ImportError:
    for _c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(_c) and os.path.realpath(_c) != os.path.realpath(sys.executable):
            import subprocess
            if subprocess.run([_c, "-c", "import discord"], capture_output=True).returncode == 0:
                os.execv(_c, [_c, os.path.abspath(__file__), *sys.argv[1:]])
    print("SKIP — discord.py not importable")
    sys.exit(0)

spec = importlib.util.spec_from_file_location("dbridge_argv", REPO / "src" / "discord-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures: list[str] = []
captured: dict = {}


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return (b"", b"")


async def _fake_exec(*argv, **kwargs):
    captured["argv"] = list(argv)
    return _FakeProc()


async def _run():
    mod.asyncio.create_subprocess_exec = _fake_exec  # patch the spawn
    # model=None path
    await mod._run_codex_subprocess("hello prompt", None, 5)
    argv1 = captured["argv"]
    check("argv includes --skip-git-repo-check", "--skip-git-repo-check" in argv1, str(argv1))
    check("argv starts with codex exec --sandbox read-only",
          argv1[:4] == ["codex", "exec", "--sandbox", "read-only"], str(argv1[:4]))
    check("no -m when model is None", "-m" not in argv1)
    # model set path
    await mod._run_codex_subprocess("hello", "gpt-x", 5)
    argv2 = captured["argv"]
    check("argv appends -m <model> when model given", "-m" in argv2 and "gpt-x" in argv2, str(argv2))


asyncio.run(_run())

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — codex subprocess argv (skip-git-repo-check)")
