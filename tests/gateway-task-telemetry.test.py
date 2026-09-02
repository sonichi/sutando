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
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolate the channel config BEFORE the bridge import: _write_task() reads the
# tierMap from _ag2space_access_path() at write time — without this, the suite
# depends on the operator's REAL AG2 Space tier map (qingyun-wu CR on #2432
# round 2, P1-2: a controlled tierMap mapping the fixture sender to "team"
# made the owner-activity assertion fail on an operator box).
_CFG_ROOT = Path(tempfile.mkdtemp(prefix="rgb-telem-cfg-"))
# AG2_DEVICE_ENV outranks CLAUDE_CONFIG_DIR in _ag2space_access_path, so setting
# only the latter still resolves to the operator's install on a configured host.
os.environ["CLAUDE_CONFIG_DIR"] = str(_CFG_ROOT)
os.environ["AG2_DEVICE_ENV"] = ""
_ACCESS = _CFG_ROOT / "channels" / "ag2space" / "access.json"
_ACCESS.parent.mkdir(parents=True, exist_ok=True)
_ACCESS.write_text('{"allowFrom": [], "tierMap": {}}')

# Import the exact module this PR modifies as a proper package import (same
# rationale as tests/gateway-per-sender-tier.test.py — no shim indirection).
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402
from ag2_sparrow._dirs import set_dirs  # noqa: E402
from ag2_sparrow.event_consumer import TaskifyHandler  # noqa: E402

# Deterministic local trust default regardless of the host's REMOTE_LOCAL_TIER.
rgb.LOCAL_TIER = "owner"

# The REAL bucket_source (pure) so fake-telemetry cases exercise the SAME
# source-bucketing the production caller uses, not a divergent passthrough.
_REAL_TEL_SPEC = importlib.util.spec_from_file_location("telemetry", REPO / "src" / "telemetry.py")
_REAL_TEL = importlib.util.module_from_spec(_REAL_TEL_SPEC)
_REAL_TEL_SPEC.loader.exec_module(_REAL_TEL)

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _fresh_dirs():
    tmp = Path(tempfile.mkdtemp(prefix="rgb-telem-test-"))
    set_dirs(task_dir=tmp / "tasks", result_dir=tmp / "results", state_dir=tmp / "state")
    # The module binds ALL of these at import; rebind every state path so no
    # side effect of _write_task (task file, task-rooms sidecar, owner-activity
    # stamp) can touch the operator's real ~/.ag2-sparrow tree (qingyun-wu CR
    # on #2432 P1-2).
    rgb.TASKS_DIR = tmp / "tasks"
    rgb.RESULTS_DIR = tmp / "results"
    rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
    rgb.TASK_ROOMS_FILE = tmp / "state" / "remote-task-rooms.json"
    rgb.OWNER_ACTIVITY_FILE = tmp / "state" / "last-owner-activity.json"
    rgb.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    rgb.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    # Reset the tierMap to the seeded EMPTY config and drop the mtime cache so
    # no case inherits another case's (or the host's) tier state.
    _ACCESS.write_text('{"allowFrom": [], "tierMap": {}}')
    rgb._TIER_MAP_CACHE["ident"] = None
    rgb._TIER_MAP_CACHE["mtime"] = None
    rgb._TIER_MAP_CACHE["map"] = {}
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
        self.mod.bucket_source = _REAL_TEL.bucket_source

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
                           "user_id": "@rui:ag2.space", "access_tier": "owner",
                           "channel_id": "!room:ag2.space"})
check("write returns the task id", tid == "task-telem1")
check("one task_processed event", t.calls == ["ag2space"], repr(t.calls))

# 2. no source on the task → falls back to PROVIDER (matches the file header)
_fresh_dirs()
with _FakeTelemetry() as t:
    rgb._write_task({"id": "task-telem2", "task": "hi", "user_id": "@rui:ag2.space",
                     "access_tier": "owner"})
check("sourceless task tags PROVIDER", t.calls == [rgb.PROVIDER], repr(t.calls))

# 3. idempotent re-write (file already queued) → no second event
_fresh_dirs()
with _FakeTelemetry() as t:
    rgb._write_task({"id": "task-telem3", "task": "x", "source": "ag2space",
                     "access_tier": "owner"})
    rgb._write_task({"id": "task-telem3", "task": "x", "source": "ag2space",
                     "access_tier": "owner"})
check("idempotent re-write counted once", t.calls == ["ag2space"], repr(t.calls))

# 4. gateway redelivery of an archived task → no event (and no task file)
_fresh_dirs()
archive = rgb.TASKS_DIR / "archive"
archive.mkdir(parents=True, exist_ok=True)
(archive / "task-telem4.txt").write_text("done long ago\n")
with _FakeTelemetry() as t:
    rgb._write_task({"id": "task-telem4", "task": "replay", "source": "ag2space",
                     "access_tier": "owner"})
check("archived redelivery not counted", t.calls == [], repr(t.calls))
check("archived redelivery writes no task file",
      not (rgb.TASKS_DIR / "task-telem4.txt").exists())

# 5. no telemetry module importable (standalone PyPI install) → write still succeeds
_fresh_dirs()
_prev = sys.modules.pop("telemetry", None)
try:
    tid = rgb._write_task({"id": "task-telem5", "task": "standalone", "source": "ag2space",
                           "access_tier": "owner"})
finally:
    if _prev is not None:
        sys.modules["telemetry"] = _prev
check("missing telemetry module → task still queued",
      tid == "task-telem5" and (rgb.TASKS_DIR / "task-telem5.txt").exists())

# 6. telemetry raising mid-call → write still succeeds (fire-and-forget)
_fresh_dirs()
with _FakeTelemetry(raise_on_call=True) as t:
    tid = rgb._write_task({"id": "task-telem6", "task": "boom", "source": "ag2space",
                           "access_tier": "owner"})
check("raising telemetry never breaks the write",
      tid == "task-telem6" and (rgb.TASKS_DIR / "task-telem6.txt").exists())
check("raising telemetry was attempted", t.calls == ["ag2space"], repr(t.calls))


# ---------------------------------------------------------------------------
# Integration through the REAL src/telemetry.py — qingyun-wu CR on #2432 P1-1:
# a fake telemetry module only proves the pre-validation argument. The real
# module coarsens the source against _KNOWN_SOURCES, and before this PR
# "ag2space" was not in the set, so the recorded metric was source="unknown".
# These cases run _write_task → real task_processed → _coarse_source → sink.
# ---------------------------------------------------------------------------

def _load_real_telemetry(state_dir: Path):
    """Fresh real telemetry module pinned to a temp state dir + test key —
    same isolation recipe as tests/telemetry.test.py's _load."""
    for k in ("DO_NOT_TRACK", "SUTANDO_TELEMETRY", "POSTHOG_API_KEY",
              "SUTANDO_DEBUG_TELEMETRY", "SUTANDO_SURFACE", "SUTANDO_TELEMETRY_ID_FILE"):
        os.environ.pop(k, None)
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SUTANDO_STATE_DIR"] = str(state_dir)
    os.environ["SUTANDO_TELEMETRY_ID_FILE"] = str(state_dir / "telemetry-id")
    os.environ["POSTHOG_API_KEY"] = "phc_test"
    spec = importlib.util.spec_from_file_location("telemetry", REPO / "src" / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._KEY = "phc_test"
    return mod


def _write_with_real_telemetry(task: dict):
    """Run _write_task with the REAL telemetry module as the `telemetry` import,
    its network sink stubbed to a capture list. Joins sender threads so the
    captured payloads are complete before asserting."""
    tmp = _fresh_dirs()
    task = {"access_tier": "owner", **task}
    real = _load_real_telemetry(tmp / "telem-state")
    payloads = []
    real._post = lambda payload: payloads.append(payload)  # network boundary stub
    prev = sys.modules.get("telemetry")
    sys.modules["telemetry"] = real
    try:
        before = set(threading.enumerate())
        rgb._write_task(task)
        for th in set(threading.enumerate()) - before:
            th.join(timeout=2)
    finally:
        if prev is None:
            sys.modules.pop("telemetry", None)
        else:
            sys.modules["telemetry"] = prev
    return tmp, payloads


# 7. direct room message: the REAL pipeline records source="ag2space" (the
#    exact metric the owner reported missing), not "unknown".
tmp7, payloads = _write_with_real_telemetry(
    {"id": "task-telem7", "task": "hello", "source": "ag2space",
     "user_id": "@rui:ag2.space", "access_tier": "owner",
     "channel_id": "!room:ag2.space"})
tp = [p for p in payloads if p.get("event") == "task_processed"]
check("real pipeline emits one task_processed", len(tp) == 1, repr(payloads))
check("real pipeline keeps source=ag2space",
      bool(tp) and tp[0]["properties"].get("source") == "ag2space",
      repr(tp and tp[0]["properties"]))

# 8. isolation audit (P1-2), checked BEFORE the next _fresh_dirs() rebinds the
#    globals: every side effect of case 7 stayed under its temp root — task
#    file, task-rooms sidecar, owner-activity stamp.
check("task file under temp root", (tmp7 / "tasks" / "task-telem7.txt").exists())
check("task-rooms sidecar under temp root",
      rgb.TASK_ROOMS_FILE == tmp7 / "state" / "remote-task-rooms.json"
      and rgb.TASK_ROOMS_FILE.exists(),
      f"TASK_ROOMS_FILE={rgb.TASK_ROOMS_FILE}")
check("owner-activity stamp under temp root",
      rgb.OWNER_ACTIVITY_FILE == tmp7 / "state" / "last-owner-activity.json"
      and rgb.OWNER_ACTIVITY_FILE.exists(),
      f"OWNER_ACTIVITY_FILE={rgb.OWNER_ACTIVITY_FILE}")

# 7a. the documented events acceptance entrypoint must expose the host's real
#     telemetry module to the imported sparrow TaskifyHandler. Run isolated so
#     neither the test runner's sys.path nor a fake sys.modules entry can make
#     this pass accidentally (qingyun-wu CR on #2432 round 3).
acceptance = REPO / "skills" / "agent-room-ops" / "events_acceptance.py"
telemetry_py = (REPO / "src" / "telemetry.py").resolve()
probe = subprocess.run(
    [
        sys.executable,
        "-I",
        "-c",
        (
            "import importlib.util, pathlib, sys\n"
            "entry = pathlib.Path(sys.argv[1])\n"
            "spec = importlib.util.spec_from_file_location('events_acceptance', entry)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "import telemetry\n"
            "print(pathlib.Path(telemetry.__file__).resolve())\n"
        ),
        str(acceptance),
    ],
    text=True,
    capture_output=True,
)
check("events acceptance resolves real host telemetry",
      probe.returncode == 0 and probe.stdout.strip() == str(telemetry_py),
      f"rc={probe.returncode} stdout={probe.stdout!r} stderr={probe.stderr!r}")

# 7b. the REAL taskify path: TaskifyHandler._promote() writes its task file
#     directly (never through _write_task), so it carries its own emit — test
#     through the actual handler, not a hand-fed _write_task call (qingyun-wu
#     CR on #2432 round 2, P1-1: the previous case proved the allowlist bucket
#     but the production promotion path emitted nothing).
tmp7b = Path(tempfile.mkdtemp(prefix="rgb-taskify-"))
real = _load_real_telemetry(tmp7b / "telem-state")
payloads = []
real._post = lambda payload: payloads.append(payload)
_prev = sys.modules.get("telemetry")
sys.modules["telemetry"] = real
try:
    handler = TaskifyHandler(str(tmp7b / "tasks"), "@sutando-rui:ag2.space",
                             threshold=1, log=lambda *a, **k: None)
    before = set(threading.enumerate())
    handler.offer({"event_id": "$e1", "type": "message.created",
                   "actor_id": "@member:ag2.space", "room_id": "!room:ag2.space",
                   "content": {"body": "observed message"}, "cursor": 1})
    # idempotent re-drain of the SAME settled event must not double-count
    handler.offer({"event_id": "$e1", "type": "message.created",
                   "actor_id": "@member:ag2.space", "room_id": "!room:ag2.space",
                   "content": {"body": "observed message"}, "cursor": 1})
    for th in set(threading.enumerate()) - before:
        th.join(timeout=2)
finally:
    if _prev is None:
        sys.modules.pop("telemetry", None)
    else:
        sys.modules["telemetry"] = _prev
tp = [p for p in payloads if p.get("event") == "task_processed"]
check("taskify promotion wrote its task file",
      handler.last_path is not None and Path(handler.last_path).exists()
      and Path(handler.last_path).is_relative_to(tmp7b),
      f"last_path={handler.last_path}")
check("REAL taskify path emits task_processed once",
      len(tp) == 1, repr(payloads))
check("taskify emit tagged events-promotion",
      bool(tp) and tp[0]["properties"].get("source") == "events-promotion",
      repr(tp and tp[0]["properties"]))

# 7c. leak/cardinality guard preserved under the fix: a secret-ish source is
#     never emitted as-is. It now collapses to this gateway's "remote" surface
#     bucket (not "unknown"), and the raw identifier appears NOWHERE in the
#     payload — the cardinality/leak posture the allowlist exists for is intact.
_t, payloads = _write_with_real_telemetry(
    {"id": "task-telem7c", "task": "x", "source": "secret-user-identifier-123"})
tp = [p for p in payloads if p.get("event") == "task_processed"]
check("secret-ish gateway source buckets to remote (surface, not unknown)",
      bool(tp) and tp[0]["properties"].get("source") == "remote",
      repr(tp and tp[0]["properties"]))
check("raw secret value never reaches the payload",
      bool(tp) and "secret-user-identifier-123" not in repr(tp[0]),
      repr(tp and tp[0]))

# 7d. hostile-tier control (P1-2 regression pin): with the CONTROLLED temp
#     access.json down-tiering the fixture sender to "team", production must
#     suppress the owner-activity stamp — and the suite must keep passing,
#     proving no assertion secretly depends on the host's ambient tierMap.
tmp7d = _fresh_dirs()
_ACCESS.write_text('{"allowFrom": [], "tierMap": {"@rui:ag2.space": "team"}}')
rgb._TIER_MAP_CACHE["ident"] = None
rgb._TIER_MAP_CACHE["mtime"] = None  # force re-read of the hostile map
real = _load_real_telemetry(tmp7d / "telem-state")
payloads = []
real._post = lambda payload: payloads.append(payload)
_prev = sys.modules.get("telemetry")
sys.modules["telemetry"] = real
try:
    before = set(threading.enumerate())
    rgb._write_task({"id": "task-telem7d", "task": "hi", "source": "ag2space",
                     "user_id": "@rui:ag2.space", "access_tier": "owner",
                     "channel_id": "!room:ag2.space"})
    for th in set(threading.enumerate()) - before:
        th.join(timeout=2)
finally:
    if _prev is None:
        sys.modules.pop("telemetry", None)
    else:
        sys.modules["telemetry"] = _prev
tp = [p for p in payloads if p.get("event") == "task_processed"]
check("hostile tier: telemetry still emits (tier-independent)",
      bool(tp) and tp[0]["properties"].get("source") == "ag2space", repr(payloads))
check("hostile tier: task written with access_tier team",
      "access_tier: team" in (tmp7d / "tasks" / "task-telem7d.txt").read_text())
check("hostile tier: owner-activity stamp suppressed for non-owner",
      not (tmp7d / "state" / "last-owner-activity.json").exists())

# 7e. REGRESSION (#2432 aftermath): a gateway task whose source is neither
#     `ag2space` nor `events-promotion` (e.g. a custom REMOTE_TASK_PROVIDER, or
#     any label the core allowlist doesn't recognize) was recorded as
#     source="unknown" — burying real gateway activity as the single largest
#     bucket fleet-wide. It must now count as this gateway's own surface,
#     "remote", not "unknown".
_t, payloads = _write_with_real_telemetry(
    {"id": "task-telem7e", "task": "x", "source": "acme-cloud-deployment"})
tp = [p for p in payloads if p.get("event") == "task_processed"]
check("non-allowlisted gateway source buckets to remote, not unknown",
      bool(tp) and tp[0]["properties"].get("source") == "remote",
      repr(tp and tp[0]["properties"]))

# 7f. bucket_source unit: known source kept; unknown falls to the given default;
#     a non-allowlisted default can't itself smuggle cardinality (→ unknown).
import telemetry as _tel  # the REAL module (src/ on sys.path from the header)
check("bucket_source keeps a known source",
      _tel.bucket_source("slack", "remote") == "slack")
check("bucket_source falls back to a known default",
      _tel.bucket_source("acme-cloud", "remote") == "remote")
check("bucket_source rejects a bad default (no cardinality via default)",
      _tel.bucket_source("acme-cloud", "acme-default") == "unknown")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All gateway task-telemetry checks passed.")
