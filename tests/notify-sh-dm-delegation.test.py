#!/usr/bin/env python3
"""notify.sh's Discord leg delegates to dm-result.send_dm — no hand-rolled curl.

notify.sh used to open the DM channel and POST the message with raw `curl`,
which sat outside the DiscordRestClient chokepoint (and outside the send
allowlist, the chunker, and the shared owner/token resolution that
dm-result.py carries). Pinned here by EXECUTION, not source-grep alone: the
current notify.sh is copied into a scratch repo skeleton whose src/dm-result.py
is a recorder stub, `curl`/`osascript` are PATH-shimmed (so no voice probe, no
desktop notification, no network), and the script must hand the exact message
to send_dm and still exit 0 when the DM leg fails (best-effort contract).

Run: python3 tests/notify-sh-dm-delegation.test.py
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NOTIFY = REPO / "src" / "notify.sh"

_fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# --- census half: the raw Discord HTTP is gone from the script itself --------
src = NOTIFY.read_text()
check("notify.sh carries no discord.com API URL", "discord.com" not in src)
check("notify.sh delegates to dm-result", "dm-result.py" in src and "send_dm" in src)

# --- execution half: the copied script really reaches send_dm ----------------
with tempfile.TemporaryDirectory() as td:
    scratch = Path(td)
    (scratch / "src").mkdir()
    (scratch / "results").mkdir()
    shutil.copy(NOTIFY, scratch / "src" / "notify.sh")

    marker = scratch / "sent.txt"
    (scratch / "src" / "dm-result.py").write_text(
        "import pathlib, sys\n"
        "def send_dm(text):\n"
        f"    pathlib.Path({str(marker)!r}).write_text(text)\n"
        "    return False\n"  # DM leg FAILS -> script must still exit 0
    )

    shims = scratch / "bin"
    shims.mkdir()
    # curl shim: voice probe reports nothing listening -> no proactive file.
    (shims / "curl").write_text("#!/bin/bash\necho -n 000\nexit 0\n")
    # osascript shim: no desktop notification from a test run.
    (shims / "osascript").write_text("#!/bin/bash\nexit 0\n")
    for f in ("curl", "osascript"):
        (shims / f).chmod((shims / f).stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["PATH"] = f"{shims}:{env['PATH']}"
    message = "gate check: it's done — 100% (`quotes` & $vars survive)"
    proc = subprocess.run(
        ["bash", str(scratch / "src" / "notify.sh"), message],
        env=env, capture_output=True, text=True, timeout=60,
    )

    check("script exits 0 even when the DM leg fails (best-effort contract)",
          proc.returncode == 0, f"rc={proc.returncode} err={proc.stderr[-300:]}")
    check("send_dm received the message VERBATIM",
          marker.exists() and marker.read_text() == message,
          marker.read_text() if marker.exists() else "never called")
    check("no proactive file written when voice probe says down",
          not any((scratch / "results").iterdir()))

print()
if _fails:
    print(f"{len(_fails)} FAILED: " + "; ".join(_fails))
    sys.exit(1)
print("all notify.sh dm-delegation assertions passed")
