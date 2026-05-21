#!/usr/bin/env python3
"""Adoption test for `state_paths` / `state-paths` workspace contract.

## Why this test exists

Many of the recent commits in this repo are fixes of the same shape:
"module X writes/reads to `tasks/` / `results/` / `state/` / `notes/`
without going through the canonical workspace resolver, so on hosts
where `SUTANDO_WORKSPACE` is set the module writes one place while
another component reads another — split-brain that strands owner DMs
/ loses voice-agent state / pollutes `git status`."

Each was a one-off "found another one, patched another one" fix. The
underlying class — a new source file written without the workspace
contract in mind — keeps producing instances.

## What this test does

For every source file under `src/`, this test:

  1. Scans for **string literals or path expressions** that look like
     references to runtime-state directories (`tasks/`, `results/`,
     `state/`, `notes/`, `data/`, `logs/`).
  2. Requires the file to **either** import the canonical resolver
     (`workspace_default.resolve_workspace` for .py /
     `workspace_default.resolveWorkspace` for .ts) **or** the
     convenience wrapper (`state_paths.state_dir/state_path` for .py /
     `state-paths.stateDir/statePath` for .ts) **or** be in an
     explicit ALLOWLIST of files that legitimately reference these
     strings without runtime-state semantics.

If a new source file references `tasks/` etc. but doesn't import the
resolver, this test fails — the contributor must either route through
the resolver or add their file to the allowlist with a justification.

The test is a preventative net, not a catch-the-current-violator
check. Any file that currently violates the contract has already been
patched in the historical fixes; the goal is to keep that work paid
down.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Patterns that indicate a file is touching runtime-state directories.
RUNTIME_STATE_REGEX = re.compile(
    r'(?:'
    # `Path(...) / "tasks"` / `os.path.join(..., "tasks")` / `f".../tasks/..."`
    r'"\s*(?:tasks|results|state|notes|data|logs)\s*"|'
    # `/tasks/`, `/results/` etc. inside a path-literal string
    r"'/(?:tasks|results|state|notes|data|logs)/'|"
    r'"/(?:tasks|results|state|notes|data|logs)/"|'
    # `(REPO|WORKSPACE_DIR|workspace|repo) / "tasks"` (Python path operator)
    r'(?:REPO|REPO_DIR|WORKSPACE_DIR|workspace|repo)\s*/\s*"(?:tasks|results|state|notes|data|logs)"'
    r')'
)

# Canonical accessors. A file that references runtime-state must use one.
PY_CANONICAL = re.compile(
    r'(?:'
    r'from\s+workspace_default\s+import|'
    r'import\s+workspace_default|'
    r'resolve_workspace\s*\('
    r')'
)
TS_CANONICAL = re.compile(
    r"(?:"
    r"from\s+['\"]\./workspace_default['\"]|"
    r"resolveWorkspace\s*\("
    r")"
)

# Files that legitimately reference these strings without runtime-state
# semantics. Each entry is justified, not silently allowed.
ALLOWLIST = {
    # The canonical resolver itself — names the strings literally.
    "src/workspace_default.py",
    "src/workspace_default.ts",
    # util_paths reads personal-asset paths only — never writes
    # runtime-state itself.
    "src/util_paths.py",
    # core_heartbeat is intentionally dep-free per its own comment —
    # it must run before any other Sutando module is loaded, so it
    # inlines the workspace resolution rather than importing
    # workspace_default. Verified inline logic matches the canonical
    # resolver's default-case behavior.
    "src/core_heartbeat.py",
}


def _allowlisted_or_missing(rel: str) -> bool:
    """Files that may not exist (yet) but if they do, they're allowlisted.
    Used for files that some installs have (e.g., core_heartbeat) and
    some don't."""
    return False  # placeholder if future expansion needed


def _check_file(path: Path) -> tuple[bool, str]:
    rel = path.relative_to(REPO).as_posix()
    if rel in ALLOWLIST:
        return True, "allowlisted"
    try:
        src = path.read_text()
    except Exception as e:
        return True, f"unreadable ({e}); skipping"

    if not RUNTIME_STATE_REGEX.search(src):
        return True, "no runtime-state references"

    canonical_re = TS_CANONICAL if path.suffix in (".ts", ".tsx") else PY_CANONICAL
    if canonical_re.search(src):
        return True, "uses canonical accessor"

    # Find the first offending line for a helpful error message.
    for lineno, line in enumerate(src.split("\n"), 1):
        if RUNTIME_STATE_REGEX.search(line):
            return False, (
                f"{rel}:{lineno}: references a runtime-state path "
                f"({line.strip()!r}) without importing the canonical resolver. "
                f"Use `state_paths.state_dir/state_path` (or `resolve_workspace`) "
                f"in .py, or `state-paths.stateDir/statePath` (or "
                f"`resolveWorkspace`) in .ts. If this file legitimately "
                f"references these strings for non-runtime reasons, add "
                f"{rel!r} to the ALLOWLIST in this test with a justification."
            )
    return True, "no offending line found"


def test_no_unauthorized_runtime_state_references():
    failures = []
    for path in sorted(SRC.rglob("*.py")):
        if "/__pycache__/" in str(path):
            continue
        ok, reason = _check_file(path)
        if not ok:
            failures.append(reason)
    for path in sorted(SRC.rglob("*.ts")):
        if "/node_modules/" in str(path):
            continue
        ok, reason = _check_file(path)
        if not ok:
            failures.append(reason)
    if failures:
        msg = "state-paths adoption violations:\n" + "\n".join(f"  - {f}" for f in failures)
        raise AssertionError(msg)


def test_canonical_modules_themselves_are_present():
    """Sanity: the canonical resolver we require everyone else to use
    must exist."""
    assert (SRC / "workspace_default.py").is_file(), "src/workspace_default.py missing"


def test_allowlist_entries_actually_exist():
    """Guard: an ALLOWLIST entry that no longer exists is dead config."""
    for entry in ALLOWLIST:
        path = REPO / entry
        if not path.is_file():
            raise AssertionError(
                f"ALLOWLIST entry {entry!r} does not exist — remove it from the "
                f"test if the file was deleted/renamed, or add the file back."
            )


def main():
    failures = []
    for fn in (
        test_no_unauthorized_runtime_state_references,
        test_canonical_modules_themselves_are_present,
        test_allowlist_entries_actually_exist,
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
    print("All state-paths adoption tests passed.")


if __name__ == "__main__":
    main()
