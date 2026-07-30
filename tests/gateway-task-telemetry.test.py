#!/usr/bin/env python3
"""task_processed telemetry for gateway (ag2.space) tasks.

#2274 instrumented every messaging bridge with a fire-and-forget
``task_processed(source)`` PostHog event at task-write time — but missed the
gateway surface: ``ag2_sparrow.remote_gateway_bridge._write_task`` emitted
nothing, so ``task_processed{source="ag2space"}`` never existed and gateway
activity was invisible in the product metrics (owner report 2026-07-30).

Covers:
  1. a newly queued task fires task_processed with the task's `source` value
  2. a task with no source falls back to PROVIDER (same value the file header gets)
  3. idempotent re-write (task file already present) does NOT double-count
  4. redelivery of an already-archived task does NOT count
  5. a missing telemetry module (standalone PyPI install) never breaks the write
  6. a telemetry call that raises never breaks the write

Run: python3 tests/gateway-task-telemetry.test.py
Exit 0 on pass, 1 on fail.
"""
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Import the exact module this PR modifies as a proper package import (same
# rationale as tests/gateway-per-sender-tier.test.py — no shim indirection).
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402
from ag2_sparrow._dirs import set_dirs  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _fresh_dirs():
    tmp = Path(tempfile.mkdtemp(prefix="rgb-telem-test-"))
    set_dirs(task_dir=tmp / "tasks", result_dir=tmp / "results", state_dir=tmp / "state")
    # the module binds these at import; rebind for each case
    rgb.TASKS_DIR = tmp / "tasks"
    rgb.RESULTS_DIR = tmp / "results"
    rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
    rgb.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    rgb.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return tmp


class _FakeTelemetry:
    def __init__(self, raise_on_call=False):
        self.calls = []
        self._raise = raise_on_call
        self.mod = types.ModuleType("telemetry")

        def task_processed(source, **kw):
            self.calls.append(source)
            if self._raise:
                raise RuntimeError("telemetry backend down")

        self.mod.task_processed = task_processed

    def __enter__(self):
        self._prev = sys.modules.get("telemetry")
        sys.modules["telemetry"] = self.mod
        return self

    def __exit__(self, *a):
        if self._prev is None:
            sys.modules.pop("telemetry", None)
        else:
            sys.modules["telemetry"] = self._prev


# 1. newly queued task → one event tagged with the task's source
_fresh_dirs()
with _FakeTelemetry() as t:
    tid = rgb._write_task({"id": "task-telem1", "task": "hello", "source": "ag2space",
                           "user_id": "@rui:ag2.space", "channel_id": "!room:ag2.space"})
check("write returns the task id", tid == "task-telem1")
check("one task_processed event", t.calls == ["ag2space"], repr(t.calls))

# 2. no source on the task → falls back to PROVIDER (matches the file header)
_fresh_dirs()
with _FakeTelemetry() as t:
    rgb._write_task({"id": "task-telem2", "task": "hi", "user_id": "@rui:ag2.space"})
check("sourceless task tags PROVIDER", t.calls == [rgb.PROVIDER], repr(t.calls))

# 3. idempotent re-write (file already queued) → no second event
_fresh_dirs()
with _FakeTelemetry() as t:
    rgb._write_task({"id": "task-telem3", "task": "x", "source": "ag2space"})
    rgb._write_task({"id": "task-telem3", "task": "x", "source": "ag2space"})
check("idempotent re-write counted once", t.calls == ["ag2space"], repr(t.calls))

# 4. gateway redelivery of an archived task → no event (and no task file)
_fresh_dirs()
archive = rgb.TASKS_DIR / "archive"
archive.mkdir(parents=True, exist_ok=True)
(archive / "task-telem4.txt").write_text("done long ago\n")
with _FakeTelemetry() as t:
    rgb._write_task({"id": "task-telem4", "task": "replay", "source": "ag2space"})
check("archived redelivery not counted", t.calls == [], repr(t.calls))
check("archived redelivery writes no task file",
      not (rgb.TASKS_DIR / "task-telem4.txt").exists())

# 5. no telemetry module importable (standalone PyPI install) → write still succeeds
_fresh_dirs()
_prev = sys.modules.pop("telemetry", None)
try:
    tid = rgb._write_task({"id": "task-telem5", "task": "standalone", "source": "ag2space"})
finally:
    if _prev is not None:
        sys.modules["telemetry"] = _prev
check("missing telemetry module → task still queued",
      tid == "task-telem5" and (rgb.TASKS_DIR / "task-telem5.txt").exists())

# 6. telemetry raising mid-call → write still succeeds (fire-and-forget)
_fresh_dirs()
with _FakeTelemetry(raise_on_call=True) as t:
    tid = rgb._write_task({"id": "task-telem6", "task": "boom", "source": "ag2space"})
check("raising telemetry never breaks the write",
      tid == "task-telem6" and (rgb.TASKS_DIR / "task-telem6.txt").exists())
check("raising telemetry was attempted", t.calls == ["ag2space"], repr(t.calls))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All gateway task-telemetry checks passed.")
