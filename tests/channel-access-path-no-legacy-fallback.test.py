#!/usr/bin/env python3
"""channel_access_path() never leaves the configured Claude home.

The ~/.claude fallback shipped 2026-06-21 with a ~30-day life; past it, an
isolated CLAUDE_CONFIG_DIR that lacks the file must resolve inside itself,
never to the operator's real home (a test fixture wrote there, 2026-09-05).
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    fake_home = tmp / "home"
    legacy = fake_home / ".claude" / "channels" / "discord" / "access.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"allowFrom": ["REAL-OPERATOR-ID"]}')
    isolated = tmp / "ccd"
    isolated.mkdir()
    os.environ["HOME"] = str(fake_home)
    os.environ["CLAUDE_CONFIG_DIR"] = str(isolated)
    os.environ.pop("CLAUDE_HOME", None)
    spec = importlib.util.spec_from_file_location("util_paths", ROOT / "src" / "util_paths.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "src"))
    spec.loader.exec_module(mod)
    got = mod.channel_access_path("discord")
    want = isolated / "channels" / "discord" / "access.json"
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - an isolated CLAUDE_CONFIG_DIR resolves inside itself, not to HOME's legacy file (got {got})")
    ok2 = not str(got).startswith(str(fake_home))
    print(("ok  " if ok2 else "FAIL") + " - the resolved path is never under the operator's home")
    sys.exit(0 if ok and ok2 else 1)
