#!/usr/bin/env python3
"""`docs/state-sync-allowlist.md` must not resurrect two retired, dangerous claims.

Both shipped in this document and were caught in review on #2491:

1. **"`rm -rf <workspace>` is survivable."** That was true under the retired
   3-space model, where State was a separate, rebuildable top-level space. Under
   the 2-space model (`docs/workspace-design.md`, ratified #1440) the workspace
   also holds persistent notes, agent memory, `build_log.md`,
   `pending-questions.md`, and durable per-host identity under `state/auth/`.
   On a single-host or unsynced install, deleting `<workspace>` destroys owner
   data outright. The survivability claim is only ever true of *rebuildable
   `state/` sub-paths* and of this proposal's own `state/fleet/` subtree.

2. **A copy-pasteable deletion command.** Writing `rm -rf` next to a path that
   actually resolves (`/Users/...`, `$HOME/...`, `~/...`) turns operator prose
   into a working destructive line. `<workspace>` as a literal placeholder does
   not resolve and is safe; a real path is not.

Text-level guard, deliberately: both defects are documentation defects — the
lifecycle behaviour itself is covered by the workspace suites. This mirrors
`tests/workspace-env-var-not-honored-docs.test.py`, including its positive
control, so the file cannot pass merely because someone deleted the prose.

Run: python3 tests/state-sync-allowlist-doc.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "state-sync-allowlist.md"

failures: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


if not DOC.is_file():
    print(f"missing target: {DOC}")
    sys.exit(1)

body = DOC.read_text()

print(f"{DOC.relative_to(REPO)}")

# --- 1. No executable canonical-workspace deletion command -------------------
# `<workspace>` is an inert placeholder and is allowed. A path that resolves is
# not: pasting it deletes a real tree.
executable_rm = re.findall(r"rm\s+-rf[^\n`]*?(/Users/|\$HOME|\$\{HOME\}|~/)", body)
check(
    "no `rm -rf` next to a path that actually resolves (placeholder-only)",
    not executable_rm,
)

# --- 2. No claim that deleting the WORKSPACE itself is survivable ------------
# Scoped claims about state/fleet/ or rebuildable state/ sub-paths are correct
# and must keep passing; only the whole-workspace claim is the defect.
workspace_survivable = re.findall(
    r"`?rm -rf `?<workspace>`?[^.\n]{0,120}(surviv|recoverab)"
    r"|<workspace>[^.\n]{0,80}(is|are)\s+(meant to be\s+)?(surviv|recoverab)"
    r"|\"rm -rf workspace is recoverable\"",
    body,
    re.IGNORECASE,
)
check(
    "does not claim deleting `<workspace>` is survivable/recoverable",
    not workspace_survivable,
)

# --- 3. The retired 3-space model is not cited as the governing contract -----
check(
    "does not cite the retired `3-space model` as governing",
    "3-space" not in body,
)

# --- Positive controls -------------------------------------------------------
# Without these, the file above could pass by simply deleting the discussion.
check(
    "positive control: cites the current 2-space model",
    bool(re.search(r"2-space model", body)),
)
check(
    "positive control: scopes its lifecycle invariant to `state/fleet/`",
    bool(re.search(r"state/fleet/[^\n]{0,120}surviv"
                   r"|invariant[^\n]{0,160}state/fleet/", body, re.IGNORECASE)),
)
check(
    "positive control: states the workspace is NOT disposable",
    bool(re.search(r"destroys owner data|not\s+make[s]?\s+the\s+canonical\s+workspace\s+disposable"
                   r"|canonical workspace disposable", body, re.IGNORECASE)),
)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — no retired survivability claim, no runnable deletion command")
