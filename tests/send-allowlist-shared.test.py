#!/usr/bin/env python3
"""Architectural regression guard for the send-allowlist policy.

Per @liususan091219's review on PR #1029: the file-attachment
allowlist was duplicated between `src/discord-bridge.py` (live WS
bridge) and `src/dm-result.py` (REST fallback). Even with "keep in
sync" comments, copies drift — and they already had: dm-result was
missing personal-notes / Desktop / Documents roots.

The fix extracted both `SEND_ALLOWED_ROOTS` / `SEND_ALLOWED_PREFIXES`
and `is_path_sendable()` into `src/send_allowlist.py`. This test
pins:

  1. Both consumers import from the shared module (no inline
     reimplementations re-introduced).
  2. The shared module's policy is the documented set (tightening
     should be deliberate, not accidental).
  3. The architectural assertion: `is_path_sendable` is defined in
     exactly ONE place (the shared module) so a future contributor
     can't silently re-add a copy.

Static analysis only; no Discord API, no fixtures.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def test_shared_module_exists():
    """`src/send_allowlist.py` is the new canonical location."""
    assert (SRC / "send_allowlist.py").is_file(), (
        "src/send_allowlist.py missing — this module is the single source "
        "of truth for the send-allowlist policy, shared between "
        "discord-bridge.py and dm-result.py."
    )


def test_discord_bridge_imports_from_shared_module():
    """`discord-bridge.py` must import the policy, not define a copy."""
    src = (SRC / "discord-bridge.py").read_text()
    assert re.search(
        r"from\s+send_allowlist\s+import",
        src,
    ), (
        "discord-bridge.py must `from send_allowlist import` the policy. "
        "If you reverted to an inline copy, the policy will drift from "
        "dm-result.py — exactly what @liususan091219 warned about on "
        "PR #1029."
    )


def test_dm_result_imports_from_shared_module():
    """`dm-result.py` must import the same policy."""
    src = (SRC / "dm-result.py").read_text()
    assert re.search(
        r"from\s+send_allowlist\s+import",
        src,
    ), "dm-result.py must `from send_allowlist import` the policy."


def test_no_inline_send_allowed_roots_definitions():
    """The architectural pin: only the shared module may DEFINE the
    constants/function. Any other file with the same definitions has
    re-introduced a copy."""
    # `discord-bridge.py` and `dm-result.py` may import + alias —
    # that's fine. They must not redefine the literal name with an
    # assignment to a tuple of paths.
    bad_files = []
    for path in (SRC / "discord-bridge.py", SRC / "dm-result.py"):
        src = path.read_text()
        # An inline definition has the shape `SEND_ALLOWED_ROOTS = (`
        # at module level — not inside a function. We check that the
        # only occurrence of `SEND_ALLOWED_ROOTS =` (not `as`/`import`)
        # is via aliasing the import.
        for line in src.split("\n"):
            stripped = line.strip()
            # Allow `from send_allowlist import ... SEND_ALLOWED_ROOTS ...`
            # and `SEND_ALLOWED_ROOTS as _SEND_ALLOWED_ROOTS,` (in
            # `from X import (... as ...,)` blocks).
            if "SEND_ALLOWED_ROOTS" in stripped and stripped.startswith("SEND_ALLOWED_ROOTS = ("):
                bad_files.append(f"{path.name}: inline definition: {stripped}")
            if "SEND_ALLOWED_PREFIXES" in stripped and stripped.startswith("SEND_ALLOWED_PREFIXES = ("):
                bad_files.append(f"{path.name}: inline definition: {stripped}")
    assert bad_files == [], (
        "Found inline (re-)definitions of SEND_ALLOWED_ROOTS / "
        "SEND_ALLOWED_PREFIXES in files that should only IMPORT from "
        "send_allowlist:\n" + "\n".join(f"  - {b}" for b in bad_files)
    )


def test_no_inline_is_path_sendable_function():
    """The function lives in the shared module. Other files import or
    alias it; they must not redefine."""
    bad_files = []
    for path in (SRC / "discord-bridge.py", SRC / "dm-result.py"):
        src = path.read_text()
        # An inline definition has the shape `def is_path_sendable(`
        # OR `def _is_path_sendable(` at module level.
        if re.search(r"^def _?is_path_sendable\(", src, re.MULTILINE):
            # Verify it's not just a `def _is_path_sendable = ...` (alias) —
            # `def` always starts a function definition.
            bad_files.append(
                f"{path.name}: contains `def [_]is_path_sendable(` — "
                f"redefining the shared function re-introduces the drift "
                f"hazard @liususan091219 warned about on PR #1029."
            )
    assert bad_files == [], "\n".join(bad_files)


def test_send_allowlist_module_has_documented_set():
    """Defense: the shared module must contain the documented roots/
    prefixes. A silent tightening (e.g. dropping `/tmp/echo-`) would
    quietly break the echo skill's file delivery — make a tightening
    update this test deliberately."""
    src = (SRC / "send_allowlist.py").read_text()
    must_appear = [
        # Prefixes
        '"/tmp/sutando-"',
        '"/private/tmp/sutando-"',
        '"/tmp/echo-"',
        '"/private/tmp/echo-"',
        # Roots — checking the path components since the literals are
        # built via `str(_REPO / "results")` etc.
        '_REPO / "results"',
        '_REPO / "notes"',
        '_REPO / "docs"',
        '"Desktop"',
        '"Documents"',
    ]
    missing = [s for s in must_appear if s not in src]
    assert missing == [], (
        f"send_allowlist.py missing documented allowlist entries: {missing}. "
        f"If you intentionally tightened the policy, update this test."
    )


def main():
    failures = []
    for fn in (
        test_shared_module_exists,
        test_discord_bridge_imports_from_shared_module,
        test_dm_result_imports_from_shared_module,
        test_no_inline_send_allowed_roots_definitions,
        test_no_inline_is_path_sendable_function,
        test_send_allowlist_module_has_documented_set,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All send-allowlist shared-module tests passed.")


if __name__ == "__main__":
    main()
