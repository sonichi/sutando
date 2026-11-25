#!/usr/bin/env python3
"""The cron-runner installer's post-install self-test must read the SAME
`hosts/<label>/crons.json` the runner reads.

The runner resolves the label through `_host_label()` (SUTANDO_HOST_LABEL >
scutil LocalHostName > short hostname). The self-test used a bare
`hostname | sed`, so on any host whose label drifted from its short hostname
it read a crons.json nobody writes, printed "skipped", and never ran the TCC
proof it exists for.

This runs the self-test's own two lines (cut from the installer, not copied)
with SUTANDO_HOST_LABEL set, and checks which crons.json they resolve.

Run: python3 tests/install-cron-runner-selftest-host-label.test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "src" / "install-cron-runner-launchd.sh"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def selftest_lines() -> str:
    lines = INSTALLER.read_text(encoding="utf-8").splitlines()
    end = next(i for i, ln in enumerate(lines) if ln.strip().startswith('CRONS_FILE="$WORKSPACE/hosts/'))
    start = next(i for i in range(end, -1, -1) if lines[i].strip().startswith("H="))
    return "\n".join(ln.strip() for ln in lines[start:end + 1])


print("== the self-test resolves the configured host label ==")
snippet = selftest_lines()
with tempfile.TemporaryDirectory() as ws:
    env = {**os.environ, "SUTANDO_HOST_LABEL": "label-not-the-hostname",
           "REPO": str(REPO), "WORKSPACE": ws}
    out = subprocess.run(["bash", "-c", f'set -e\n{snippet}\nprintf "%s" "$CRONS_FILE"'],
                         env=env, capture_output=True, text=True, timeout=60)
    want = f"{ws}/hosts/label-not-the-hostname/crons.json"
    check("CRONS_FILE follows SUTANDO_HOST_LABEL", out.stdout == want,
          f"got {out.stdout!r} (rc {out.returncode}, stderr {out.stderr.strip()[:200]!r})")

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all install-cron-runner self-test host-label checks passed")
