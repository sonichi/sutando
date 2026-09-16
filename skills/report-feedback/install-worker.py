#!/usr/bin/env python3
"""Install the macOS deadline worker; --render prints the launchd job only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys


SCRIPT = Path(__file__).resolve().with_name("report-feedback.py")
LABEL = "com.sutando.feedback-recovery"
sys.path.insert(0, str(SCRIPT.parents[2] / "src"))
from workspace_default import resolve_workspace  # noqa: E402


def job(workspace: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(SCRIPT), "--apply"],
        "WorkingDirectory": str(SCRIPT.parents[2]),
        "StartInterval": 60,
        "RunAtLoad": True,
        "StandardOutPath": str(workspace / "logs" / "feedback-recovery.log"),
        "StandardErrorPath": str(workspace / "logs" / "feedback-recovery.log"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    workspace = Path(resolve_workspace())
    data = plistlib.dumps(job(workspace))
    if args.render:
        sys.stdout.buffer.write(data)
        return
    if platform.system() != "Darwin":
        parser.error("use your OS scheduler to run report-feedback.py --apply every minute")
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    service = f"gui/{os.getuid()}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0
    if loaded:
        subprocess.run(["launchctl", "bootout", service], check=True)
    if args.uninstall:
        destination.unlink(missing_ok=True)
        return
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], check=True)
    subprocess.run(["launchctl", "print", service], check=True, stdout=subprocess.DEVNULL)
    print("Installed feedback deadline worker (every 60 seconds). Pending reports survive restarts.")


if __name__ == "__main__":
    main()
