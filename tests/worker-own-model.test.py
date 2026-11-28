#!/usr/bin/env python3
"""A worker's model pick is kept per worker and reaches only its own launch.

  a) no pick -> nothing is printed, so the launcher adds no --model
  b) a pick is kept, read back, replaced, and dropped by an empty one
  c) a pick that is not a model name is refused and a damaged file reads as no pick
  d) one worker's pick is not another's
  e) the launcher hands the pick to the CLI as --model

Run: python3 tests/worker-own-model.test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
import worker_model as M  # noqa: E402

FAILS: list[str] = []
A, B = "0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="wm-"))
    check(M.get(ws, A) == "" and M.main(["x", "get", str(ws), A]) == 0, "a) a worker with no pick has none")
    check(M.set(ws, A, "sonnet") == "sonnet" and M.get(ws, A) == "sonnet", "b) a pick is kept and read back")
    check(M.set(ws, A, "opus[1m]") == "opus[1m]" and M.get(ws, A) == "opus[1m]", "b) a later pick replaces it")
    check(M.get(ws, B) == "", "d) another worker has no pick")
    for bad in ("--dangerously", "a b", "x;rm", "a" * 80):
        try:
            M.set(ws, A, bad)
            check(False, f"c) {bad[:12]!r} is refused")
        except ValueError:
            check(True, f"c) {bad[:12]!r} is refused")
    check(M.get(ws, A) == "opus[1m]", "c) a refused pick leaves the kept one")
    M.path(ws, B).parent.mkdir(parents=True)
    M.path(ws, B).write_text("--not a model\n")
    check(M.get(ws, B) == "", "c) a damaged file reads as no pick")
    check(M.set(ws, A, "") == "" and M.get(ws, A) == "" and M.set(ws, A, "") == "", "b) an empty pick drops it")
    check(M.main(["x", "set", str(ws), A, "haiku"]) == 0 and M.main(["x", "set", str(ws), "../x", "haiku"]) == 1
          and M.main(["x", "nope"]) == 2 and M.get(ws, "../x") == "", "c) the command line answers 0, 1 and 2")
    launcher = (REPO / "skills" / "worker-pool" / "scripts" / "launch-worker-session.sh").read_text()
    check('worker_model.py" get' in launcher and 'SESSION_ARGS+=(--model "$_own_model")' in launcher,
          "e) the launcher hands the pick to the CLI as --model")
    print("\n%d failure(s)" % len(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
