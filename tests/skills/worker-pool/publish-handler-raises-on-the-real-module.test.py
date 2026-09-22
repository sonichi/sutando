#!/usr/bin/env python3
"""`publish_task_event_handler` raises on a failed publish — on the REAL module.

`launcher-task-event-handler-transitions.test.py` already asserts this property,
but against a `shutil.copytree()`'d temp skill so it runs on a host that already
has a pool. Coverage is path-keyed, so the real file is never credited and the
raise this PR adds reads as uncovered while being demonstrably exercised.

This drives the same branch through the installed module, against a real temp
workspace so nothing durable is touched: the config write itself is patched to
fail (`Path.write_text` on the temp file), which is the actual failure surface
now that the publisher is a JSON config file, not a symlink.

Run: python3 tests/skills/worker-pool/publish-handler-raises-on-the-real-module.test.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
SRC = REPO / "skills/worker-pool/scripts/pool_roster.py"

spec = importlib.util.spec_from_file_location("pool_roster_real", SRC)
pr = importlib.util.module_from_spec(spec)
sys.modules["pool_roster_real"] = pr
spec.loader.exec_module(pr)

checks: dict[str, bool] = {}
P = pathlib.Path
real_write_text = P.write_text


def _boom(self, *a, **kw):
    raise OSError(30, "Read-only file system")


tmp = pathlib.Path(tempfile.mkdtemp(prefix="publish-handler-raises-"))
cfg_before = (tmp / "state" / "task-event-handler.json").exists()

P.write_text = _boom
try:
    raised = None
    try:
        pr.publish_task_event_handler(tmp)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    checks["a failed publish RAISES rather than returning None"] = raised is not None
    checks["it raises HandlerPublishError, not a bare OSError"] = (
        type(raised).__name__ == "HandlerPublishError"
    )
    checks["the message names the path it could not publish"] = (
        raised is not None and "task-event-handler.json" in str(raised)
    )
    checks["the OSError is chained, so the cause survives"] = (
        raised is not None and isinstance(raised.__cause__, OSError)
    )
finally:
    P.write_text = real_write_text

checks["control: the patch is what failed — write_text is restored afterwards"] = (
    P.write_text is real_write_text
)
checks["control: the raise came from the injected OSError, not some other error"] = (
    raised is not None and getattr(raised.__cause__, "errno", None) == 30
)
checks["hermetic: no config file was left behind by the failed publish"] = (
    (tmp / "state" / "task-event-handler.json").exists() == cfg_before
)

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})")
sys.exit(1 if fails else 0)
