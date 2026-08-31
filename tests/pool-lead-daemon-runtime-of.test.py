#!/usr/bin/env python3
"""`runtime_of` reads a seat's runtime from its own launchd plist.

It was nested inside main(), so nothing could reach it and the whole function
read as uncovered. Hoisted to module scope with an injectable agents dir; these
drive it directly. Every unreadable or unstated case must degrade to claude —
that matches every plist written before the runtime flag existed.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "pool_lead_daemon", REPO / "scripts" / "pool-lead-daemon.py")
_mod = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_mod)
except SystemExit:
    pass

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _plist(d: Path, inst: str, body: str) -> None:
    (d / f"com.sutando.{inst}.plist").write_text(body)


PLIST = ('<plist><dict><key>POOL_RUNTIME</key><string>%s</string>'
         '</dict></plist>')

with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    _plist(d, "core-1", PLIST % "codex")
    check(_mod.runtime_of("core-1", d) == "codex",
          "a plist stating codex reads as codex")

    _plist(d, "core-2", PLIST % "claude")
    check(_mod.runtime_of("core-2", d) == "claude",
          "a plist stating claude reads as claude")

    # Absent file — the pre-flag case, and the one every old seat is in.
    check(_mod.runtime_of("core-absent", d) == "claude",
          "no plist at all degrades to claude")

    _plist(d, "core-3", "<plist><dict/></plist>")
    check(_mod.runtime_of("core-3", d) == "claude",
          "a plist with no POOL_RUNTIME key degrades to claude")

    _plist(d, "core-4", PLIST % "  ")
    check(_mod.runtime_of("core-4", d) == "claude",
          "a blank runtime value degrades to claude")

    # An unrecognised value must NOT pass through — the seat would be driven
    # with a runtime nothing implements.
    _plist(d, "core-5", PLIST % "bash")
    check(_mod.runtime_of("core-5", d) == "claude",
          "an unknown runtime degrades to claude, never passes through")

    # Unreadable: a directory where the plist should be. read_text raises
    # OSError (IsADirectoryError), which is the branch that was uncovered.
    (d / "com.sutando.core-6.plist").mkdir()
    check(_mod.runtime_of("core-6", d) == "claude",
          "an unreadable plist degrades to claude")

# NEGATIVE CONTROL: without this, a runtime_of that returned "claude"
# unconditionally would satisfy every assertion above except the first two.
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    _plist(d, "core-7", PLIST % "codex")
    check(_mod.runtime_of("core-7", d) != "claude",
          "it can still return something OTHER than claude")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
