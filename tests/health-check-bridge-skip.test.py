#!/usr/bin/env python3
"""Tests for _should_skip_bridge() — issue #1916.

SKIP_TELEGRAM / SKIP_DISCORD / SKIP_SLACK env vars let operators silence a
bridge on a specific host without removing its token from shared config.
The helper is read by both the health-check (no warn) and --fix (no restart).

Run: python3 tests/health-check-bridge-skip.test.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


_no_env = Path("/nonexistent/.env")

# ── env-var path ─────────────────────────────────────────────────────────────

with patch.dict(os.environ, {"SKIP_TELEGRAM": "1"}, clear=False):
    check("env var SKIP_TELEGRAM=1 → skip telegram",
          hc._should_skip_bridge("telegram", _no_env))
    check("env var SKIP_TELEGRAM=1 → does not skip discord",
          not hc._should_skip_bridge("discord", _no_env))

with patch.dict(os.environ, {"SKIP_DISCORD": "1"}, clear=False):
    check("env var SKIP_DISCORD=1 → skip discord",
          hc._should_skip_bridge("discord", _no_env))
    check("env var SKIP_DISCORD=1 → does not skip telegram",
          not hc._should_skip_bridge("telegram", _no_env))

with patch.dict(os.environ, {"SKIP_SLACK": "1"}, clear=False):
    check("env var SKIP_SLACK=1 → skip slack",
          hc._should_skip_bridge("slack", _no_env))

check("no skip vars → returns False",
      not hc._should_skip_bridge("telegram", _no_env) and
      not hc._should_skip_bridge("discord", _no_env))

# ── .env file path ───────────────────────────────────────────────────────────

with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
    f.write("SKIP_TELEGRAM=1\nSOME_OTHER=val\n")
    _env_file = Path(f.name)

try:
    check(".env SKIP_TELEGRAM=1 → skip telegram",
          hc._should_skip_bridge("telegram", _env_file))
    check(".env SKIP_TELEGRAM=1 → does not skip discord",
          not hc._should_skip_bridge("discord", _env_file))
finally:
    _env_file.unlink()

with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
    f.write("SKIP_DISCORD=1\n")
    _env_file2 = Path(f.name)

try:
    check(".env SKIP_DISCORD=1 → skip discord",
          hc._should_skip_bridge("discord", _env_file2))
    check(".env SKIP_DISCORD=1 → does not skip telegram",
          not hc._should_skip_bridge("telegram", _env_file2))
finally:
    _env_file2.unlink()

# .env parsing skips blank lines and comments before finding the flag
with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
    f.write("# per-host bridge overrides\n\n  # indented comment\nSKIP_SLACK=1\n")
    _env_file3 = Path(f.name)

try:
    check(".env with comments/blank lines before flag → skip slack",
          hc._should_skip_bridge("slack", _env_file3))
finally:
    _env_file3.unlink()

# unreadable env_path (exists but read_text raises) → fail-open, no skip
with tempfile.TemporaryDirectory() as _d:
    check("env_path exists but unreadable (directory) → returns False",
          not hc._should_skip_bridge("telegram", Path(_d)))

# ── env var value must be exactly "1" ────────────────────────────────────────

with patch.dict(os.environ, {"SKIP_DISCORD": "0"}, clear=False):
    check("SKIP_DISCORD=0 is not a skip", not hc._should_skip_bridge("discord", _no_env))

with patch.dict(os.environ, {"SKIP_DISCORD": "true"}, clear=False):
    check("SKIP_DISCORD=true is not a skip (must be '1')",
          not hc._should_skip_bridge("discord", _no_env))

# ── run_all_checks() integration: calling site in the bridge loop ─────────────
# Exercises the `if _should_skip_bridge(channel_name, env_path): continue`
# line inside run_all_checks(). check_port is stubbed to avoid TCP connects;
# SKIP_TELEGRAM + SKIP_DISCORD ensure the bridge-skip path fires immediately.

def _stub_port(port, name, **kwargs):
    return {"name": name, "status": "down", "detail": "mocked"}

with patch.object(hc, "check_port", side_effect=_stub_port), \
     patch.dict(os.environ, {"SKIP_TELEGRAM": "1", "SKIP_DISCORD": "1", "SKIP_SLACK": "1"}, clear=False):
    _checks = hc.run_all_checks()
    _names = {c["name"] for c in _checks}
    check("run_all_checks: SKIP_TELEGRAM=1 → telegram-bridge absent",
          "telegram-bridge" not in _names)
    check("run_all_checks: SKIP_DISCORD=1 → discord-bridge absent",
          "discord-bridge" not in _names)
    check("run_all_checks: SKIP_SLACK=1 → slack-bridge absent",
          "slack-bridge" not in _names)

# slack-bridge is actually IN the bridge loop (the whole point of SKIP_SLACK):
# with no skip vars and a configured slack channel dir, the loop must emit a
# slack-bridge check. Point claude_home_path at a temp channels tree so the
# "configured" gate (channels/slack/.env exists) passes hermetically.
with tempfile.TemporaryDirectory() as _home:
    _ch = Path(_home) / "channels" / "slack"
    _ch.mkdir(parents=True)
    (_ch / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n")
    _orig_chp = hc.claude_home_path

    def _fake_chp(*sub):
        # mirror claude_home_path(*subpath): redirect the whole channels/ tree
        # (any depth, e.g. channels/ag2space/.env) into the temp home; delegate
        # everything else to the real resolver.
        if sub and sub[0] == "channels":
            return Path(_home).joinpath(*sub)
        return _orig_chp(*sub)

    with patch.object(hc, "check_port", side_effect=_stub_port), \
         patch.object(hc, "claude_home_path", side_effect=_fake_chp), \
         patch.dict(os.environ, {"SKIP_TELEGRAM": "1", "SKIP_DISCORD": "1"}, clear=False):
        os.environ.pop("SKIP_SLACK", None)
        _checks2 = hc.run_all_checks()
        _names2 = {c["name"] for c in _checks2}
        check("run_all_checks: slack configured + no SKIP_SLACK → slack-bridge check emitted",
              "slack-bridge" in _names2, str(sorted(_names2)))

if failures:
    sys.exit(1)
print("PASS — _should_skip_bridge tests")
