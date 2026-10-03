#!/usr/bin/env python3
"""Discovered entry point for the ag2-sparrow poll-stall-exit suite.

The assertions live in `packages/ag2-sparrow/tests/test_poll_stall_exit.py`;
this file only makes them discoverable. `ci.yml` hand-lists that suite, which
runs it but leaves it invisible to the coverage gate: `discover-python-tests.sh`
-- the single owner of "which Python tests does CI run" -- globs `*.test.py`
under `tests/` and `skills/` only, so the coverage run never executes it and the
stall-exit branch it covers reports as uncovered. Same reason
`tests/ag2-sparrow-drift.test.py` lives here rather than in the package.

Delegating in-process (not via subprocess) is deliberate: the bridge is already
inside `.coveragerc`'s `[run] source`, so an in-process call records its lines
without needing the `SUTANDO_TEST_SUBPROCESS_COVERAGE` dance.

Run: python3 tests/gateway-poll-stall-exit.test.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITE = REPO / "packages" / "ag2-sparrow" / "tests" / "test_poll_stall_exit.py"


def _suite():
    # The suite puts packages/ag2-sparrow on sys.path itself; importing it by
    # location keeps one copy of the assertions rather than restating them.
    assert SUITE.exists(), f"missing {SUITE}"
    spec = importlib.util.spec_from_file_location("ag2_sparrow_poll_stall_exit", SUITE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = _suite()
    cases = sorted(n for n in dir(mod) if n.startswith("test_"))
    # A delegating runner that discovers zero cases would exit green and assert
    # nothing, which is the failure this file exists to prevent.
    assert cases, f"no test_* functions found in {SUITE}"
    failed = []
    for name in cases:
        try:
            getattr(mod, name)()
            print(f"ok    {name}")
        except Exception as exc:  # noqa: BLE001 -- report every case, then fail
            failed.append((name, exc))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(cases) - len(failed)}/{len(cases)} passed ({SUITE.relative_to(REPO)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
