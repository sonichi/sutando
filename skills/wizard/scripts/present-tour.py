#!/usr/bin/env python3
# Presents the desktop `tour` card; with no presenter above this checkout it is text-only (exit 0).
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def find_presenter(script: Path) -> Path | None:
    # Only the two supported locations: engine/local-card.py beside engine/sutando/
    # (the desktop layout), or the checkout root; never an arbitrary ancestor's file.
    repo = script.resolve().parents[3]
    for candidate in (repo.parent / "local-card.py", repo / "local-card.py"):
        if candidate.is_file():
            return candidate
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="present the wizard tour card if the desktop presenter exists")
    ap.add_argument("--room", help="room the tour is about (room-bound actions resolve here)")
    ap.add_argument("--presenter", help="explicit path to local-card.py (tests; production walks up from here)")
    a = ap.parse_args(argv)
    presenter = Path(a.presenter) if a.presenter else find_presenter(Path(__file__))
    if presenter is None or not presenter.is_file():
        print("text-only: no local-card presenter beside this checkout")
        return 0
    args = [sys.executable, str(presenter), "present", "tour", "--set", "body=Tap any stop to open it."]
    if a.room:
        args += ["--room", a.room]
    proc = subprocess.run(args, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
