#!/usr/bin/env python3
"""Present the desktop app's `tour` local card, if the presenter is installed.

The presenter (`local-card.py`) ships with the AG2 Space desktop app one level
above the engine checkout; a non-desktop install has none, and that is the
text-only case, not an error (exit 0, prints `text-only`).
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]


def find_presenter() -> Path | None:
    override = os.environ.get("LOCAL_CARD_BIN")
    candidates = [Path(override)] if override else [REPO.parent / "local-card.py"]
    return next((p for p in candidates if p.is_file()), None)


def main(argv: list[str]) -> int:
    presenter = find_presenter()
    if presenter is None:
        print("text-only: no local-card presenter beside this checkout")
        return 0
    args = [sys.executable, str(presenter), "present", "tour", "--set", "body=Tap any stop to open it."]
    if "--room" in argv:
        i = argv.index("--room")
        if i + 1 >= len(argv):
            print("--room needs a room id", file=sys.stderr)
            return 2
        args += ["--room", argv[i + 1]]
    proc = subprocess.run(args, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
