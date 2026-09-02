#!/usr/bin/env python3
"""Pin both sides of the AG2 launch block's REMOTE_MEDIA_MARKER contract.

The provider-neutral bridge tests never execute startup.sh's launcher mapping,
which is how a marker mismatch shipped (inbound images rendered as plain text).
This test executes the REAL defaulting line extracted from start_gateway_lanes()
in src/startup-runtime.sh (startup.sh's own gateway-launch block moved there so
it can also run standalone via scripts/restart-gateway-lanes.sh) in a bash
subshell — not a re-implementation of it — so it fails if the line is removed,
renamed, or its semantics change:

  1. unset REMOTE_MEDIA_MARKER  -> defaults to "ag2space-media"
  2. explicitly set             -> the explicit value survives untouched
"""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
STARTUP = REPO / "src" / "startup-runtime.sh"


def _marker_line() -> str:
    lines = [
        ln.strip()
        for ln in STARTUP.read_text().splitlines()
        if 'REMOTE_MEDIA_MARKER="${REMOTE_MEDIA_MARKER:-' in ln
    ]
    assert len(lines) == 1, (
        f"expected exactly one REMOTE_MEDIA_MARKER defaulting line in "
        f"src/startup-runtime.sh, found {len(lines)} — the AG2 launch-block "
        f"contract this test pins has moved or been duplicated"
    )
    return lines[0]


def _run(prelude: str) -> str:
    line = _marker_line()
    out = subprocess.run(
        ["bash", "-c", f'{prelude}; {line}; printf %s "$REMOTE_MEDIA_MARKER"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_default_when_unset():
    got = _run("unset REMOTE_MEDIA_MARKER")
    assert got == "ag2space-media", f"unset -> {got!r}, want 'ag2space-media'"
    print("PASS test_default_when_unset")


def test_explicit_value_wins():
    got = _run("REMOTE_MEDIA_MARKER=custom-tag")
    assert got == "custom-tag", f"explicit -> {got!r}, want 'custom-tag'"
    print("PASS test_explicit_value_wins")


def test_empty_string_gets_default():
    # ${VAR:-default} (colon form) treats empty as unset — an empty marker from
    # a half-written channel .env must not disable media resolution.
    got = _run('REMOTE_MEDIA_MARKER=""')
    assert got == "ag2space-media", f"empty -> {got!r}, want 'ag2space-media'"
    print("PASS test_empty_string_gets_default")


if __name__ == "__main__":
    test_default_when_unset()
    test_explicit_value_wins()
    test_empty_string_gets_default()
    print("ALL PASS")
    sys.exit(0)
