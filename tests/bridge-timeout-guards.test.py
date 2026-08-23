#!/usr/bin/env python3
"""Tests for the bounded network/subprocess calls added in fix/bridge-timeout-guards.

Three call sites gained bounds; each had a hang mode with a real incident
shape behind it:
  1. discord-bridge self-rescue probe (`subprocess.run([cand, "-c", "import
     discord"])`) — a wedged interpreter candidate hung bridge STARTUP forever.
     Now `timeout=20`, and TimeoutExpired/OSError skip to the next candidate.
  2. discord-bridge `_send_via_rest` — `urlopen` with no timeout could hang a
     REST send indefinitely. Now `timeout=10`.
  3. telegram-bridge `download_file` — `urlretrieve` (no timeout, no UA) could
     hang attachment ingest. Now a streamed `urlopen(..., timeout=30)` +
     `shutil.copyfileobj`.

The discord-bridge sites live at module top level / behind a live-client
module, so they are exercised by compiling the exact AST segments AGAINST THE
REAL FILE PATH (original line numbers preserved) and executing them with
controlled fakes — coverage attributes the runs to src/discord-bridge.py's
changed lines, and the assertions pin the bound values (20/10/30) so a future
"remove the timeout" regression fails loudly.

Run: python3 tests/bridge-timeout-guards.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import subprocess as real_subprocess
import sys
import tempfile
import types
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISCORD_SRC = REPO / "src" / "discord-bridge.py"
TELEGRAM_SRC = REPO / "src" / "telegram-bridge.py"

os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")
# Isolate the Claude config surface BEFORE the telegram-bridge import below:
# on import the bridge resolves claude_home_path("channels","telegram",".env")
# and, if present, chmods + reads it. Without this, the committed test touches
# the developer's real ~/.claude credential file (qingyun repro, PR #1886).
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-timeout-guards-")
# ...and seed the CANONICAL access file inside it. Setting CLAUDE_CONFIG_DIR alone is not
# enough: the bridge also calls channel_access_path("telegram"), which falls back to the
# LEGACY real-home ~/.claude/channels/telegram/access.json when the canonical path is
# missing. That left tg.ACCESS_FILE pointing at the operator's real allowlist inside a unit
# test and emitted a `[util_paths] DEPRECATION: using legacy …` warning (qingyun, #1886).
# Creating an empty allowlist here makes the import hermetic — no host state, no real file.
_ccd_tg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_ccd_tg.mkdir(parents=True, exist_ok=True)
(_ccd_tg / "access.json").write_text('{"allowFrom": []}')
# ...and the SAME for discord: this file exec-loads BOTH bridges (DISCORD_SRC and
# TELEGRAM_SRC), and channel_access_path() resolves PER CHANNEL. Seeding only
# telegram left `channels/discord/access.json` absent, so the discord import could
# still fall back to the operator's real allowlist — the exact per-channel gap the
# hermetic-bridge lint was taught to catch (sonichi/sutando#2429 review 11).
_ccd_dc = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_ccd_dc.mkdir(parents=True, exist_ok=True)
(_ccd_dc / "access.json").write_text('{"allowFrom": []}')

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _compile_segment(path: Path, predicate):
    """Compile one top-level AST node of `path` against its real filename.

    Line numbers are preserved, so executing the code object counts the
    ORIGINAL source lines as covered — this is what lets us exercise
    module-top-level code without importing the whole live bridge.
    """
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if predicate(node):
            mod = ast.Module(body=[node], type_ignores=[])
            return compile(mod, str(path), "exec")
    raise AssertionError(f"segment not found in {path}")


# ── 1. discord-bridge self-rescue probe: bounded, and a wedged candidate is
#       skipped instead of hanging startup ──────────────────────────────────

class _Halt(Exception):
    """Raised by the fake os.execv to stop segment execution at the re-exec."""


class _BlockDiscordImport:
    """Meta-path hook forcing `import discord` to fail deterministically,
    so the rescue branch runs even on machines where discord.py exists."""

    def find_spec(self, name, *_a, **_k):
        if name == "discord" or name.startswith("discord."):
            raise ModuleNotFoundError("No module named 'discord'")
        return None


def test_rescue_probe_bounded_and_skips_wedged():
    code = _compile_segment(
        DISCORD_SRC,
        lambda n: isinstance(n, ast.Try)
        and any(isinstance(b, ast.Import) and b.names[0].name == "discord" for b in n.body),
    )

    run_calls: list[tuple[list, object]] = []
    execv_calls: list[tuple] = []

    def fake_run(argv, capture_output=None, timeout=None):
        run_calls.append((argv, timeout))
        if len(run_calls) == 1:
            raise real_subprocess.TimeoutExpired(cmd=argv, timeout=timeout)  # wedged interpreter
        if len(run_calls) == 2:
            raise OSError("broken interpreter binary")
        return types.SimpleNamespace(returncode=0)

    def fake_execv(*args):
        execv_calls.append(args)
        raise _Halt()

    seg_globals = {
        "__file__": str(DISCORD_SRC),
        "sys": sys,
        "os": types.SimpleNamespace(
            path=types.SimpleNamespace(exists=lambda p: True, realpath=lambda p: p),
            execv=fake_execv,
        ),
        "subprocess": types.SimpleNamespace(
            run=fake_run, TimeoutExpired=real_subprocess.TimeoutExpired,
        ),
        "print": lambda *a, **k: None,
    }

    blocker = _BlockDiscordImport()
    saved_discord = sys.modules.pop("discord", None)
    sys.meta_path.insert(0, blocker)
    halted = False
    try:
        exec(code, seg_globals)
    except _Halt:
        halted = True
    finally:
        sys.meta_path.remove(blocker)
        if saved_discord is not None:
            sys.modules["discord"] = saved_discord

    check("rescue: wedged candidate (TimeoutExpired) skipped, not fatal", len(run_calls) >= 2)
    check("rescue: OSError candidate skipped, not fatal", len(run_calls) >= 3)
    check("rescue: every probe bounded with timeout=20",
          all(t == 20 for _, t in run_calls), f"timeouts={[t for _, t in run_calls]}")
    check("rescue: healthy candidate re-execed", halted and len(execv_calls) == 1)
    if execv_calls:
        check("rescue: re-exec targets the probed interpreter",
              execv_calls[0][0] == run_calls[2][0][0])


# ── 2. discord-bridge _send_via_rest: urlopen bounded with timeout=10 ───────

def test_send_via_rest_bounded():
    # The bound lives in _rest_client (DiscordRestClient timeout=10); the
    # client's _default_transport is what passes it to urlopen.
    sys.path.insert(0, str(REPO / "src"))
    g = {
        "TOKEN": "test-token",
        "_chunk_for_discord": lambda m: [m],
        "json": __import__("json"),
        "sys": sys,
        "print": lambda *a, **k: None,
    }
    for name in ("_rest_client", "_send_via_rest"):
        code = _compile_segment(
            DISCORD_SRC,
            lambda n, name=name: isinstance(n, ast.FunctionDef) and n.name == name,
        )
        exec(code, g)
    send = g["_send_via_rest"]

    seen: dict = {}
    real_urlopen = urllib.request.urlopen

    class _OkResp:
        status = 200

        def read(self):
            return b'{"id": "1"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def ok_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        seen["url"] = req.full_url
        return _OkResp()

    urllib.request.urlopen = ok_urlopen
    try:
        send("1234567890", "hello")
    finally:
        urllib.request.urlopen = real_urlopen
    check("send_via_rest: urlopen bounded with timeout=10", seen.get("timeout") == 10,
          f"got {seen.get('timeout')!r}")

    def bad_urlopen(req, timeout=None):
        raise ConnectionError("boom")

    urllib.request.urlopen = bad_urlopen
    exited = None
    try:
        send("1234567890", "hello")
    except SystemExit as e:
        exited = e.code
    finally:
        urllib.request.urlopen = real_urlopen
    check("send_via_rest: failed send still exits 1 (unchanged contract)", exited == 1)


# ── 3. telegram-bridge download_file: streamed, bounded, UA header ──────────

def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_file_streams_with_timeout():
    tg = _load("tgbridge_timeout_test", TELEGRAM_SRC)
    inbox = Path(tempfile.mkdtemp())
    tg.INBOX_DIR = inbox
    tg.api = lambda *a, **k: {"ok": True, "result": {"file_path": "photos/pic_1.jpg"}}

    seen: dict = {}
    real_urlopen = urllib.request.urlopen

    def ok_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        seen["ua"] = req.get_header("User-agent")
        return _FakeResp(b"PAYLOAD-BYTES")

    urllib.request.urlopen = ok_urlopen
    try:
        out = tg.download_file("file-id-1", "pic.jpg")
    finally:
        urllib.request.urlopen = real_urlopen

    check("download_file: returns local path on success", bool(out))
    if out:
        check("download_file: streamed bytes landed intact",
              Path(out).read_bytes() == b"PAYLOAD-BYTES")
        check("download_file: saved under INBOX_DIR with source ext",
              Path(out).parent == inbox and out.endswith(".jpg"))
    check("download_file: urlopen bounded with timeout=30", seen.get("timeout") == 30,
          f"got {seen.get('timeout')!r}")
    check("download_file: sends a User-Agent header", seen.get("ua") == "Sutando")

    def bad_urlopen(req, timeout=None):
        raise TimeoutError("stalled download")

    urllib.request.urlopen = bad_urlopen
    try:
        out = tg.download_file("file-id-2", "pic.jpg")
    finally:
        urllib.request.urlopen = real_urlopen
    check("download_file: stalled download returns None, not a hang/crash", out is None)


def main() -> int:
    test_rescue_probe_bounded_and_skips_wedged()
    test_send_via_rest_bounded()
    test_download_file_streams_with_timeout()
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nbridge timeout-guard invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
