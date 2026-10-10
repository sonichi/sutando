#!/usr/bin/env python3
"""format_timestamp() matrix (owner_tz, host fallback, naive/invalid input)
plus an end-to-end main() run against a mocked Discord API."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "discord_read_under_test", REPO / "src" / "discord-read.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok: {name}")
    else:
        _failed += 1
        print(f"FAIL: {name}  {detail}")


@contextlib.contextmanager
def _env(**overrides: str | None):
    """Temporarily set/unset environment variables (None = unset)."""
    old = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _host_tz(name: str):
    """Pin the host timezone (TZ + tzset) for the astimezone() fallback path."""
    with _env(TZ=name):
        time.tzset()
        try:
            yield
        finally:
            pass
    time.tzset()


def main() -> int:
    fmt = _mod.format_timestamp

    # ---- 1. valid OWNER_TZ: conversion + explicit abbreviation ----
    check(
        "valid OWNER_TZ (summer → EDT)",
        fmt("2026-07-21T23:47:00+00:00", "America/New_York")
        == "2026-07-21T19:47:00 EDT",
    )
    check(
        "valid OWNER_TZ (winter → EST)",
        fmt("2026-01-21T23:47:00+00:00", "America/New_York")
        == "2026-01-21T18:47:00 EST",
    )
    check(
        "valid OWNER_TZ handles Z-suffix input",
        fmt("2026-07-21T23:47:00Z", "America/Los_Angeles")
        == "2026-07-21T16:47:00 PDT",
    )

    # ---- 2. host-timezone fallback (no OWNER_TZ) ----
    with _host_tz("America/Chicago"):
        got = fmt("2026-07-21T23:47:00+00:00", None)
    check(
        "host-tz fallback converts via OS timezone (CDT)",
        got == "2026-07-21T18:47:00 CDT",
        f"got={got!r}",
    )

    # ---- 3. naive timestamp treated as UTC ----
    check(
        "naive timestamp treated as UTC",
        fmt("2026-07-21T23:47:00", "America/New_York")
        == "2026-07-21T19:47:00 EDT",
    )

    # ---- 4. invalid timezone name → honest UTC fallback ----
    got = fmt("2026-07-21T23:47:00+00:00", "Not/A_Zone")
    check(
        "invalid OWNER_TZ falls back to raw prefix labeled UTC",
        got == "2026-07-21T23:47:00 UTC",
        f"got={got!r}",
    )

    # ---- 5. invalid timestamp → honest UTC fallback ----
    got = fmt("not-a-timestamp", "America/New_York")
    check(
        "garbage timestamp falls back, labeled UTC",
        got == "not-a-timestamp UTC",
        f"got={got!r}",
    )

    # ---- 6. label is always explicit — never a bare time ----
    for raw, tz in [
        ("2026-07-21T23:47:00+00:00", "America/New_York"),
        ("2026-07-21T23:47:00+00:00", "Not/A_Zone"),
        ("garbage", None),
    ]:
        got = fmt(raw, tz)
        check(
            f"explicit trailing label for ({raw!r}, {tz!r})",
            " " in got and got.rsplit(" ", 1)[1] != "",
            f"got={got!r}",
        )

    # ---- e2e: main() renders message timestamps through the helper ----
    messages = [
        {
            "id": "111",
            "timestamp": "2026-07-21T23:47:00.000000+00:00",
            "author": {"username": "chi"},
            "content": "goodnight?",
        }
    ]
    tmp = tempfile.mkdtemp(prefix="discord-read-test-")
    old_request_json = _mod.request_json
    try:
        _mod.request_json = lambda req, timeout=10: messages
        with _env(
            CLAUDE_CONFIG_DIR=tmp,
            DISCORD_BOT_TOKEN="test-token-not-real",
            OWNER_TZ="America/New_York",
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = _mod.main(["123456789012345678"])
        check("e2e: main() exits 0", rc == 0)
        check(
            "e2e: OWNER_TZ renders 19:47 EDT, not 23:47",
            "[2026-07-21T19:47:00 EDT] chi: goodnight?" in out.getvalue(),
            f"stdout={out.getvalue()!r}",
        )
    finally:
        _mod.request_json = old_request_json

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
