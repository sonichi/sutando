#!/usr/bin/env python3
"""The gateway bridge's intake gate drops an app build's own thread message, end to end.

The s.space client sends each build fence (a private DM task) with a room message in the app's
thread: the description for a build, the change's text for a change or Try-again. That message
is the build's own copy, and the agent must never act on it. Covers src/remote-gateway-bridge.py
_build_thread_gate, which runs commons-app-builder's thread-task verdict before a room task is
queued, through the bridge's own _write_task:
  1. a build fence's first thread message (its description, from the asking owner) settles
     unqueued, answered with the redelivery control result; the fence itself is queued;
  2. the same text again (another message), other text, another sender's copy, a team-tier
     copy, another thread: all queued;
  3. a change fence naming its message (thread_message), the message arriving first in the round:
     taken after the round's acks, it is rechecked once the fence is queued and settles unqueued;
  4. a message nothing names is delivered once the round's wait ends (the skill decides);
  5. a raising check delivers the message, and the lane dedupe still runs.

Run: python3 tests/commons-app-builder-intake-gate.test.py   (exit 0 pass / 1 fail)
"""
import importlib.util
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

ROOM, DM = "!abcDEF123:s.space", "!ownerDM456:s.space"
OWNER, AGENT, OTHER = "@owner:s.space", "@owner.agent:s.space", "@guest:s.space"
ROOT, PAGE = "$threadRoot1:s.space", "html-k3m9x2qa"
SAID = "A sign-up sheet\nfor the weekend event"

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" - " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def now_iso(offset=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def fence(rid, **fields):
    """A DM build fence as the gateway delivers it (the bridge flattens and stamps it)."""
    body = {"v": 1, "request_id": rid, "mode": "build", "description": SAID, "room_id": ROOM,
            "page": PAGE, "template": "poll", "thread_root": ROOT, **fields}
    text = f"Build a Commons app\n\n```commons-app-build\n{json.dumps(body)}\n```"
    return {"task": text, "channel_id": DM, "source_room_id": DM, "user_id": OWNER, "access_tier": "owner",
            "channel_kind": "dm", "room_member_count": "2", "room_members": f"{OWNER}, {AGENT}",
            "source_message_id": f"$f-{rid[:8]}", "timestamp": now_iso(), "source": "ag2space"}


def thread_message(mid, text=SAID, user=OWNER, tier="owner", root=ROOT):
    return {"task": text, "channel_id": ROOM, "source_room_id": ROOM, "thread_root": root, "user_id": user,
            "access_tier": tier, "channel_kind": "room", "room_member_count": "5", "source_message_id": mid,
            "timestamp": now_iso(), "source": "ag2space"}


def main():
    rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")
    tmp = Path(tempfile.mkdtemp(prefix="rgb-build-thread-test-"))
    rgb.WS = tmp
    rgb.TASKS_DIR = tmp / "tasks"
    rgb.RESULTS_DIR = tmp / "results"
    rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
    logs = []
    rgb._log = logs.append

    def write(tid, fields):
        return rgb._write_task({"id": tid, **fields})

    def queued(tid):
        return (rgb.TASKS_DIR / f"{tid}.txt").exists()

    def settled(tid):
        path = rgb.RESULTS_DIR / f"{tid}.txt"
        return path.exists() and path.read_text() == rgb.GATEWAY_REDELIVERY_RESULT

    check("the loader installs the intake gate", rgb.TASK_INTAKE_GATE is rgb._intake_gate)

    # 1. A build fence, then its description in the app's thread.
    rid = "3f2b9c1e-7a4d-4e8b-9f61-2c5d8a0b7e14"
    write("task-fence1", fence(rid))
    check("the fence is queued", queued("task-fence1"))
    got = write("task-desc1", thread_message("$d1"))
    check("its description in the thread is not queued", got is not None and not queued("task-desc1"), repr(got))
    check("it is answered with the redelivery control result", settled("task-desc1"))
    check("the log names the build", any("commons-app-builder" in m and rid in m for m in logs))

    # 2. Everything else in that thread is queued.
    write("task-desc2", thread_message("$d2"))
    check("the same text as a second message is queued (the slot is used once)", queued("task-desc2"))
    write("task-other", thread_message("$o1", text="make the header blue"))
    check("other text in the thread is queued", queued("task-other"))
    rid2 = "aaaaaaaa-7a4d-4e8b-9f61-2c5d8a0b7e14"
    write("task-fence2", fence(rid2))
    write("task-guest", thread_message("$g1", user=OTHER, tier="guest"))
    check("another sender's copy is queued", queued("task-guest"))
    write("task-team", thread_message("$t1", tier="team"))
    check("a team-tier copy is queued", queued("task-team"))
    write("task-root", thread_message("$r1", root="$anotherRoot:s.space"))
    check("the same text in another thread is queued", queued("task-root"))
    write("task-desc3", thread_message("$d3"))
    check("the second fence's own copy is not queued", not queued("task-desc3") and settled("task-desc3"))

    # 3. A change fence that names its message, the message first in the round.
    rid3 = "bbbbbbbb-7a4d-4e8b-9f61-2c5d8a0b7e14"
    change = fence(rid3, mode="refine", app_id="a1b2c3d4", thread_message="$c1", description="make it blue")
    msg = thread_message("$c1", text="make it blue")
    rgb._INTAKE_MODE["defer"] = True
    try:
        write("task-change-msg", msg)
        deferred = False
    except rgb._IntakeDeferred:
        deferred = True
    finally:
        rgb._INTAKE_MODE["defer"] = False
    check("a change's message before its fence is held for the round's late pass", deferred
          and not queued("task-change-msg"))
    write("task-change", change)
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 2.0
    try:
        write("task-change-msg", msg)
    finally:
        rgb._INTAKE_MODE["deadline"] = None
    check("once its fence is queued, that message is not queued", not queued("task-change-msg")
          and settled("task-change-msg"))

    # 3b. The fence lands while the message waits in the round's late pass.
    rid4 = "cccccccc-7a4d-4e8b-9f61-2c5d8a0b7e14"
    late_fence = fence(rid4, mode="refine", app_id="a1b2c3d4", thread_message="$c2", description="add a date")
    timer = threading.Timer(0.3, write, ("task-change2", late_fence))
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 3.0
    began = time.monotonic()
    timer.start()
    try:
        write("task-change2-msg", thread_message("$c2", text="add a date"))
    finally:
        timer.join()
        rgb._INTAKE_MODE["deadline"] = None
    check("a fence that lands during the wait still drops its message", not queued("task-change2-msg")
          and settled("task-change2-msg") and queued("task-change2") and time.monotonic() - began < 2.5)

    # 4. A message nothing names waits out the round, then is delivered.
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 0.6
    began = time.monotonic()
    try:
        write("task-late", thread_message("$l1", text="also add a date column"))
    finally:
        rgb._INTAKE_MODE["deadline"] = None
    check("an unnamed message is delivered after the round's wait",
          queued("task-late") and 0.4 <= time.monotonic() - began < 2.0)
    write("task-free", thread_message("$l2", text="thanks"))
    check("outside a round it is delivered at once", queued("task-free"))

    # 4b. No commons-app-builder skill: nothing is read, the message is delivered.
    write("task-fence5", fence("dddddddd-7a4d-4e8b-9f61-2c5d8a0b7e14"))  # its copy would be dropped
    real = rgb._BUILD_REQUEST
    rgb._BUILD_REQUEST = tmp / "no-such-skill" / "build_request.py"
    try:
        write("task-noskill", thread_message("$n1", text=SAID))
    finally:
        rgb._BUILD_REQUEST = real
    check("without the skill a thread message is delivered", queued("task-noskill"))

    # 5. A raising check fails open, and the lane dedupe still runs.
    class Broken:
        WORKSPACE = None

        def Ledger(self, *_a):
            raise RuntimeError("boom")
    saved = list(rgb._BUILD_REQUEST_MOD)
    rgb._BUILD_REQUEST_MOD[:] = [Broken()]
    try:
        write("task-broken", thread_message("$b1"))
    finally:
        rgb._BUILD_REQUEST_MOD[:] = saved
    check("a raising check delivers the message", queued("task-broken"))
    check("and says so in one log line", any("build thread gate: RuntimeError; delivering" in m for m in logs))
    rgb.GATEWAY_INSTANCE = "sspace"
    try:
        write("task-broken-copy", thread_message("$b1"))
    finally:
        rgb.GATEWAY_INSTANCE = ""
    check("the lane dedupe still settles another lane's copy",
          not (rgb.TASKS_DIR / "task-sspace~task-broken-copy.txt").exists())

    print(f"commons-app-builder intake gate: {'FAIL' if failures else 'OK'} ({len(failures)} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
