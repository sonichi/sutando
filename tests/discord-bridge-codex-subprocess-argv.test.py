#!/usr/bin/env python3
"""_run_codex_subprocess spawns the sandbox runner with no tools in an empty dir.

The mod-judge wrapper reads untrusted Discord text, so its runner must not be able
to read any file: claude with `--tools ""`, cwd a fresh empty temp dir that is
removed afterwards, stdin closed. Codex is never spawned (no read-denying policy).

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
        return (b"{}", b"")


async def _fake_exec(*argv, **kwargs):
    captured["argv"] = list(argv)
    captured["kwargs"] = kwargs
    cwd = kwargs.get("cwd")
    captured["cwd_listing"] = sorted(os.listdir(cwd)) if cwd and os.path.isdir(cwd) else None
    return _FakeProc()


async def _run():
    mod.asyncio.create_subprocess_exec = _fake_exec  # patch the spawn
    sr = mod.sandbox_runner
    # Pin the runner: which one is installed is the host's business, not this test's.
    orig = sr.select_runner
    sr.select_runner = lambda *a, **k: (sr.RUNNERS["claude"], "/fake/claude")
    try:
        raw = await mod._run_codex_subprocess("hello prompt", "gpt-x", 5)
    finally:
        sr.select_runner = orig
    argv, kw = captured["argv"], captured["kwargs"]
    check("answer read from stdout", raw == "{}", repr(raw))
    check("argv starts with claude -p", argv[:2] == ["/fake/claude", "-p"], str(argv[:2]))
    check("--tools is empty", "--tools" in argv and argv[argv.index("--tools") + 1] == "", str(argv))
    check("no MCP config", "--strict-mcp-config" in argv and "--mcp-config" not in argv, str(argv))
    check("model is not forwarded", "gpt-x" not in argv, str(argv))
    check("prompt is the last argv after --", argv[-2:] == ["--", "hello prompt"], str(argv[-2:]))
    check("stdin closed", kw.get("stdin") is asyncio.subprocess.DEVNULL)
    cwd = kw.get("cwd") or ""
    check("cwd is a fresh empty temp dir", captured["cwd_listing"] == [] and
          os.path.basename(cwd).startswith("sutando-sandbox-"), cwd)
    check("cwd is outside the repo", not os.path.realpath(cwd).startswith(str(REPO.resolve())), cwd)
    check("cwd removed afterwards", not os.path.exists(cwd), cwd)
    # No safe runner: nothing is spawned.
    captured.clear()
    sr.select_runner = lambda *a, **k: None
    try:
        raw = await mod._run_codex_subprocess("hello", None, 5)
    finally:
        sr.select_runner = orig
    check("no runner -> empty answer, no spawn", raw == "" and not captured)


asyncio.run(_run())

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — sandbox judge argv (no tools, empty cwd)")
