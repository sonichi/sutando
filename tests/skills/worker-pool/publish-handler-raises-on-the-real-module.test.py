#!/usr/bin/env python3
"""`publish_task_event_handler` raises on a failed publish — on the REAL module.

`launcher-task-event-handler-transitions.test.py` already asserts this property,
but against a `shutil.copytree()`'d temp skill so it runs on a host that already
has a pool. Coverage is path-keyed, so the real file is never credited and the
raise this PR adds reads as uncovered while being demonstrably exercised.

This drives the same branch through the installed module. The live host already
has the publisher symlink, so the function would early-return; the existence
probes are patched to miss and `symlink_to` to fail. Nothing touches the
filesystem, so the real publisher is never disturbed.

Run: python3 tests/skills/worker-pool/publish-handler-raises-on-the-real-module.test.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
SRC = REPO / "skills/worker-pool/scripts/pool_roster.py"

spec = importlib.util.spec_from_file_location("pool_roster_real", SRC)
pr = importlib.util.module_from_spec(spec)
sys.modules["pool_roster_real"] = pr
spec.loader.exec_module(pr)

checks: dict[str, bool] = {}
P = pathlib.Path
real = (P.is_symlink, P.exists, P.symlink_to)


def _boom(self, target, target_is_directory=False):
    raise OSError(30, "Read-only file system")


P.is_symlink = lambda self: False
P.exists = lambda self: False
P.symlink_to = _boom
try:
    raised = None
    try:
        pr.publish_task_event_handler()
    except Exception as exc:  # noqa: BLE001
        raised = exc
    checks["a failed publish RAISES rather than returning None"] = raised is not None
    checks["it raises HandlerPublishError, not a bare OSError"] = (
        type(raised).__name__ == "HandlerPublishError"
    )
    checks["the message names the path it could not publish"] = (
        raised is not None and "task-event-handler" in str(raised)
    )
    checks["the OSError is chained, so the cause survives"] = (
        raised is not None and isinstance(raised.__cause__, OSError)
    )
finally:
    P.is_symlink, P.exists, P.symlink_to = real

# Control: with publishing restored the same call succeeds on the live host,
# proving the failure above was the injected one and not a broken fixture.
checks["control: the real publisher resolves once symlink_to works"] = (
    pr.publish_task_event_handler().name == "task-event-handler"
)

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})")
sys.exit(1 if fails else 0)
