#!/usr/bin/env python3
"""Fail a test that writes into the resolved live workspace.

WHY
---
Six fixes for this one class landed on `main` in a single day, each found after
the fact by damage on someone's machine:

    #2617 cron-runner makes a real network POST per tick
    #2615 tofu-enroll overwrote the live owner-presence signal
    #2614 slack-allowlist left fixtures in the live workspace
    #2619 reply-directive fixture wrote to the owner's live workspace
    #2620 dm-result-multipart-upload appended to the outbox log
    #2618 dm-result-send-dm appended to the live outbox log

The suite has **prevention without detection**. Tests achieve isolation by
redirecting `WORKSPACE_DIR`/`RESULTS_DIR` to a tmpdir; ~35 files describe
themselves as hermetic on that basis. Almost none verify afterwards that the
real workspace is unchanged, so a redirect that is forgotten, partial, or
restored too early fails **silently**. @Sutando-Pro's per-suite guard in #2601
caught exactly that within one run — two calls left outside the redirect
context — which is the proof this shape works. This is the floor under the
files that never got one.

WHY CI AND NOT LOCALLY
----------------------
Measured on a live host: **62 files in the workspace changed in two minutes**
from the core, the bridges and the heartbeat writing normally. A
snapshot-and-compare there fires on every invocation, and no allowlist
separates the test's write from that churn without also blinding the guard to
`state/`, where two of the six bugs landed. CI has no core, no bridges and no
heartbeat, and `workspace/` exists on disk despite being gitignored — so a
write is an unambiguous diff. Detection belongs where the signal is clean; the
damage is still local, which is what makes detecting it upstream worthwhile.

EXEMPTIONS
----------
Per @Sutando-Pro, and both constraints matter:

  * an exemption **names the path** it may write, never a bare flag — the same
    token-specific discipline `REVIEW.md` already requires of hardcoded-path
    fixture exclusions, so one exemption cannot quietly cover a second real
    violation;
  * an exemption whose path is **not** written FAILS. An unused exemption is a
    stale exemption, and stale exemptions are what make the next real one
    invisible.

Format, one per line in `tests/hermetic-workspace-exemptions.txt`:

    <test-file-basename>  <workspace-relative-path>   # why

Usage:
    python3 scripts/hermetic-workspace-guard.py run tests/foo.test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))

EXEMPTIONS = REPO / "tests" / "hermetic-workspace-exemptions.txt"


def resolved_workspace() -> Path:
    """The workspace the test will actually resolve — not a guess at it."""
    from workspace_default import resolve_workspace  # noqa: E402

    return Path(resolve_workspace())


def snapshot(root: Path) -> dict:
    """Map every file under `root` to (size, mtime_ns).

    Not a content hash: the point is to notice that a file was created,
    removed, appended to, or rewritten, and size+mtime catches all four at a
    fraction of the cost. A same-size same-mtime rewrite is possible in
    principle and is not the failure mode this exists for — the six real cases
    were appends and creations.
    """
    if not root.exists():
        return {}
    out: dict = {}
    for p in root.rglob("*"):
        try:
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        except OSError:
            # A file that vanished mid-walk is itself churn; record it as absent
            # rather than crashing the guard on a race.
            continue
    return out


def diff(before: dict, after: dict) -> list[str]:
    """Workspace-relative paths that were created, removed, or changed."""
    changed = [p for p in after if p not in before or before[p] != after[p]]
    changed += [p for p in before if p not in after]
    return sorted(set(changed))


def exemptions_for(test_file: str) -> list[str]:
    """Paths this specific test file is permitted to write."""
    if not EXEMPTIONS.exists():
        return []
    name = Path(test_file).name
    allowed = []
    for line in EXEMPTIONS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            allowed.append(parts[1])
    return allowed


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "run":
        print("usage: hermetic-workspace-guard.py run <testfile> [args...]", file=sys.stderr)
        return 2
    test_file = argv[1]

    ws = resolved_workspace()
    before = snapshot(ws)

    proc = subprocess.run([sys.executable, test_file, *argv[2:]])

    after = snapshot(ws)
    touched = diff(before, after)
    allowed = exemptions_for(test_file)

    violations = [p for p in touched if p not in allowed]
    unused = [p for p in allowed if p not in touched]

    rc = proc.returncode
    if violations:
        print(f"\n::error::{test_file} wrote into the LIVE workspace ({ws})", flush=True)
        for p in violations:
            print(f"    {p}", flush=True)
        print("  A test must redirect WORKSPACE_DIR/RESULTS_DIR to a tmpdir. If this write",
              flush=True)
        print("  is genuinely required, add `<test> <path>` to "
              f"{EXEMPTIONS.relative_to(REPO)} with a reason.", flush=True)
        rc = rc or 1
    if unused:
        # An exemption that never fires is the mechanism by which this class
        # comes back: it stays in the file, reads as sanctioned, and hides the
        # next real violation of the same path.
        print(f"\n::error::{test_file} has STALE exemption(s) — the path was not written:",
              flush=True)
        for p in unused:
            print(f"    {p}", flush=True)
        print("  Remove it. An unused exemption makes the next real one invisible.", flush=True)
        rc = rc or 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
