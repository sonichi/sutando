#!/usr/bin/env python3
"""Contract tests for the production cross-platform advisory lock."""

from __future__ import annotations

import multiprocessing
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from file_lock import locked_file  # noqa: E402


def contender(path: str, ready, release, acquired) -> None:
    with locked_file(Path(path)):
        acquired.set()
        ready.set()
        release.wait(10)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sutando-file-lock-") as raw:
        path = str(Path(raw) / "state.lock")
        ready1 = multiprocessing.Event()
        release1 = multiprocessing.Event()
        acquired1 = multiprocessing.Event()
        first = multiprocessing.Process(
            target=contender, args=(path, ready1, release1, acquired1)
        )
        first.start()
        assert ready1.wait(10), "first process never acquired"

        ready2 = multiprocessing.Event()
        release2 = multiprocessing.Event()
        acquired2 = multiprocessing.Event()
        second = multiprocessing.Process(
            target=contender, args=(path, ready2, release2, acquired2)
        )
        second.start()
        time.sleep(0.5)
        assert not acquired2.is_set(), "second process acquired while first held"

        release1.set()
        assert ready2.wait(10), "second process did not acquire after release"
        release2.set()
        first.join(10)
        second.join(10)
        assert first.exitcode == 0 and second.exitcode == 0

    print("PASS: cross-platform file lock serializes contenders")


if __name__ == "__main__":
    main()
