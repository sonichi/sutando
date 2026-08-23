#!/usr/bin/env python3
"""install-gateway-bridge-launchd.sh must substitute plist placeholders safely.

CR #2068 (qingyun-wu): the installer used unescaped `sed` to inject paths into
the XML plist, so a valid macOS path/config dir containing `&`, `<`, `>`, `|`,
or a backslash could corrupt the substitution or the plist and break bootstrap.
The fix mirrors install-channel-bridge-launchd.sh: substitute via `plistlib`,
which XML-escapes every value on dump.

This test runs the installer's ACTUAL substitution block (extracted from the
shell script's heredoc, not a copy) against the real template with a hostile
value, and asserts the result is a valid plist with the value intact. Plus a
structural guard that the unescaped-sed path can't come back.

Run: python3 tests/gateway-bridge-launchd-plist-escaping.test.py  (exit 0/1)
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "src" / "install-gateway-bridge-launchd.sh"
TEMPLATE = REPO / "src" / "launchd" / "com.sutando.gateway-bridge.plist"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _extract_substitution_block(script: str) -> str:
    """Pull the `python3 - ... <<'PY' ... PY` heredoc body out of the installer
    so the test exercises the real substitution code, not a reimplementation."""
    marker = "<<'PY'\n"
    start = script.index(marker) + len(marker)
    end = script.index("\nPY", start)
    return script[start:end]


# ── structural guard: unescaped sed is gone, plistlib is in ──────────────────
installer_src = INSTALLER.read_text()
check("installer no longer sed-substitutes plist placeholders",
      's|__REPO__' not in installer_src and 's|__WORKSPACE__' not in installer_src)
check("installer substitutes via plistlib", "plistlib.dump" in installer_src)

# ── behavioral: the real block round-trips a hostile path to a VALID plist ───
block = _extract_substitution_block(installer_src)
# Paths a real user could have: an ampersand, angle brackets, a pipe, a
# backslash — every char that would break raw sed injection into XML.
hostile_repo = "/Users/a&b/Repo<x>|y\\z"
hostile_ws = "/Users/a&b/work space/ws<1>"
hostile_cfg = "/Users/a&b/.claude & config"

with tempfile.TemporaryDirectory() as td:
    block_py = Path(td) / "subst.py"
    block_py.write_text(block)
    dest = Path(td) / "out.plist"
    env = {
        **os.environ,
        "REPO": hostile_repo,
        "WORKSPACE": hostile_ws,
        "BREW_BIN": "/opt/homebrew/bin",
        "HOME": "/Users/a&b",
        "CLAUDE_CFG": hostile_cfg,
    }
    r = subprocess.run(
        [sys.executable, str(block_py), str(TEMPLATE), str(dest)],
        env=env, capture_output=True, text=True,
    )
    check("substitution block runs without error", r.returncode == 0, r.stderr)
    check("output plist was written", dest.exists())

    # The corruption test: the result must PARSE as a plist (sed injection would
    # have produced malformed XML that plistlib.load rejects).
    parsed = None
    try:
        with open(dest, "rb") as fh:
            parsed = plistlib.load(fh)
        valid = True
    except Exception as e:  # noqa: BLE001
        valid = False
        check("output is a valid, parseable plist", False, str(e))
    def _strings(value):
        """Flatten every string value in a decoded plist."""
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [s for v in value for s in _strings(v)]
        if isinstance(value, dict):
            return [s for v in value.values() for s in _strings(v)]
        return []

    if parsed is not None:
        check("output is a valid, parseable plist", valid)
        # Match the DECODED strings, never repr(): repr re-escapes the backslash
        # and false-fails on a value that survived intact.
        joined = " ".join(_strings(parsed))
        check("hostile REPO path substituted intact", hostile_repo in joined, joined[:200])
        check("hostile WORKSPACE path substituted intact", hostile_ws in joined)
        check("hostile CLAUDE_CONFIG_DIR substituted intact", hostile_cfg in joined)
        # No placeholder left unsubstituted.
        check("no __PLACEHOLDER__ tokens remain",
              not any(tok in joined for tok in
                      ("__REPO__", "__WORKSPACE__", "__CLAUDE_CONFIG_DIR__", "__BREW_BIN__", "__HOME__")))

    # On-disk bytes must be well-formed XML with the ampersand ESCAPED (&amp;),
    # never a raw `&` inside a <string> (which is what raw sed would emit).
    raw = dest.read_bytes()
    check("ampersand is XML-escaped on disk (not raw &)", b"&amp;" in raw and b">|y" not in raw)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — gateway-bridge launchd plist escaping")
