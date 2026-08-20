#!/usr/bin/env python3
"""The Stage-2 fallback sentinels must name WHICH failure, and the archive
matcher must not swallow prose that merely opens with the same words."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

# Isolate BEFORE import: discord-bridge resolves channel config at module scope,
# so an unset CLAUDE_CONFIG_DIR reads the operator's real allowlist.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-sandbox-sentinel-")
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')
os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken")

try:
    import discord  # noqa: F401
except ImportError:
    print("SKIP — discord.py not importable")
    sys.exit(0)

spec = importlib.util.spec_from_file_location("dbridge", REPO / "src" / "discord-bridge.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["dbridge"] = mod
spec.loader.exec_module(mod)

fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


# The two causes must not collapse to one string.
nonzero = mod.SANDBOX_FALLBACK_NONZERO.format(rc=125)
no_output = mod.SANDBOX_FALLBACK_NO_OUTPUT
check(nonzero != no_output, "nonzero-exit and no-output sentinels are distinct")
check("exit 0" not in nonzero.replace("exit 0 ", ""), "nonzero form never renders as 'exit 0'")
check("125" in nonzero, "nonzero form carries the actual status")
check("no output" in no_output and "exit 125" not in no_output,
      "no-output form names its own cause and no exit code")

# Archive matcher: exact forms only.
for body, want, label in (
    (mod.SANDBOX_FALLBACK_NONZERO.format(rc=125), True, "archives nonzero sentinel"),
    (mod.SANDBOX_FALLBACK_NONZERO.format(rc=124), True, "archives nonzero sentinel (other rc)"),
    (mod.SANDBOX_FALLBACK_NO_OUTPUT, True, "archives no-output sentinel"),
    ("Sandbox unavailable; refusing non-owner task.", True, "archives the LEGACY sentinel (peer on old code)"),
    ("Sandbox unavailable after upgrading — can you diagnose it?", False,
     "NEGATIVE CONTROL: near-match prose reaches RUN CODEX, not the archive"),
    ("Sandbox unavailable (codex exit 125) — no reply generated. Also, please review #123.", False,
     "NEGATIVE CONTROL: sentinel plus trailing prose is not a bare sentinel"),
    ("Here is the review you asked for.", False, "ordinary reply is not archived"),
    ("", False, "empty body is not a sentinel"),
):
    check(mod.is_sandbox_fallback_sentinel(body) is want, label)

print()
if fails:
    print("FAILED: %d" % len(fails))
    raise SystemExit(1)
print("sandbox fallback sentinel invariants hold")
