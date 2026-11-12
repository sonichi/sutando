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
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import fcntl  # noqa: E402  (an extension module: below the isolation)

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
    br = _load("commons_build_request", REPO / "skills" / "commons-app-builder" / "scripts" / "build_request.py")
    tmp = Path(tempfile.mkdtemp(prefix="rgb-build-thread-test-"))
    rgb.WS = tmp
    rgb.TASKS_DIR = tmp / "tasks"
    rgb.RESULTS_DIR = tmp / "results"
    rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
    logs = []
    rgb._log = logs.append

    def write(tid, fields):
        return rgb._write_task({"id": tid, **fields})

    def fenced(tid, fields):
        """A fence the agent has taken up: queued by the bridge, then noted in the skill's ledger,
        which indexes its thread as an app's (what `plan` and `thread-task` do)."""
        got = write(tid, fields)
        br.Ledger(tmp / "state" / "commons-app-builds").note_fence(br.fence_facts((rgb.TASKS_DIR / f"{tid}.txt").read_text()))
        return got

    def queued(tid):
        return (rgb.TASKS_DIR / f"{tid}.txt").exists()

    def settled(tid):
        path = rgb.RESULTS_DIR / f"{tid}.txt"
        return path.exists() and path.read_text() == rgb.GATEWAY_REDELIVERY_RESULT

    check("the loader installs the intake gate", rgb.TASK_INTAKE_GATE is rgb._intake_gate)

    # 0. A new build: the bridge hands its fence DM to the skill, which indexes its thread, so the
    #    copy that follows (here in the next round) is settled at intake. No run took the fence up.
    write("task-fence0", fence("00000000-7a4d-4e8b-9f61-2c5d8a0b7e14", description="A new board",
                               thread_root="$freshRoot:s.space"))
    check("the fence DM itself is delivered", queued("task-fence0"))
    write("task-fresh0", thread_message("$fr0", text="A new board", root="$freshRoot:s.space"))
    check("a new build's copy in the next round is settled at intake", not queued("task-fresh0")
          and settled("task-fresh0"))
    write("task-fresh0b", thread_message("$fr0b", text="is it ready?", root="$freshRoot:s.space"))
    check("while another message in that thread is delivered", queued("task-fresh0b"))

    # 1. A build fence, then its description in the app's thread.
    rid = "3f2b9c1e-7a4d-4e8b-9f61-2c5d8a0b7e14"
    fenced("task-fence1", fence(rid))
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
    fenced("task-fence2", fence(rid2))
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
    rgb._INTAKE_MODE["round"] += 1
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
    rgb._INTAKE_MODE["round"] += 1  # a new served round, as _take_served_round starts one
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
    os.environ["BUILD_THREAD_CONVERSATION"] = "true"  # only with the switch on does a message wait
    rgb._INTAKE_MODE["round"] += 1  # a new served round, as _take_served_round starts one
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 3.0
    began = time.monotonic()
    timer.start()
    try:
        write("task-change2-msg", thread_message("$c2", text="add a date"))
    finally:
        timer.join()
        rgb._INTAKE_MODE["deadline"] = None
        os.environ.pop("BUILD_THREAD_CONVERSATION", None)
    check("a fence that lands during the wait still drops its message", not queued("task-change2-msg")
          and settled("task-change2-msg") and queued("task-change2") and time.monotonic() - began < 2.5)

    # 4. A message nothing names waits out the round, then is delivered.
    os.environ["BUILD_THREAD_CONVERSATION"] = "true"
    rgb._INTAKE_MODE["round"] += 1  # a new served round, as _take_served_round starts one
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 0.6
    began = time.monotonic()
    try:
        write("task-late", thread_message("$l1", text="also add a date column"))
    finally:
        rgb._INTAKE_MODE["deadline"] = None
        os.environ.pop("BUILD_THREAD_CONVERSATION", None)
    check("an unnamed message is delivered after the round's wait",
          queued("task-late") and 0.4 <= time.monotonic() - began < 2.0)
    write("task-free", thread_message("$l2", text="thanks"))
    check("outside a round it is delivered at once", queued("task-free"))

    # 4b. No commons-app-builder skill: nothing is read, the message is delivered.
    fenced("task-fence5", fence("dddddddd-7a4d-4e8b-9f61-2c5d8a0b7e14"))  # its copy would be dropped
    real = rgb._BUILD_REQUEST
    rgb._BUILD_REQUEST = tmp / "no-such-skill" / "build_request.py"
    try:
        write("task-noskill", thread_message("$n1", text=SAID))
    finally:
        rgb._BUILD_REQUEST = real
    check("without the skill a thread message is delivered", queued("task-noskill"))

    # 5. Contained: the verdict runs out of the bridge's process within 2 s, and anything but a
    #    clean answer delivers. The bridge stays up in every case.
    real = rgb._BUILD_REQUEST
    fakes = tmp / "fake-skill"
    fakes.mkdir()
    for name, body in (("exits", "raise SystemExit(0)\n"), ("crashes", "import sys; sys.exit(3)\n"),
                       ("slow", "import time; time.sleep(30)\n"), ("garbled", "print('not json')\n")):
        fenced("task-fence-" + name, fence(f"{name[:1] * 8}-7a4d-4e8b-9f61-2c5d8a0b7e14"))
        (fakes / f"{name}.py").write_text(body)
        rgb._BUILD_REQUEST = fakes / f"{name}.py"
        began = time.monotonic()
        try:
            got = write(f"task-{name}", thread_message(f"${name}1"))
        except BaseException as e:  # noqa: BLE001 - what the test is for
            got = e
        finally:
            rgb._BUILD_REQUEST = real
        check(f"a verdict that {name} delivers the message, and the bridge stays up",
              queued(f"task-{name}") and isinstance(got, tuple) and time.monotonic() - began < 3.0, repr(got))
    check("and a timeout or garbage says so in one log line",
          any("build thread gate: TimeoutExpired; delivering" in m for m in logs)
          and any("build thread gate: an unreadable verdict; delivering" in m for m in logs))

    # The child sees only the workspace's and the skill's own variables, never the gateway token,
    # and runs isolated (-I). Each probe answers "duplicate" (settled) only if that holds.
    os.environ["REMOTE_TASK_TOKEN"] = "test-token-never-for-the-child"
    os.environ["SOME_OTHER_SECRET"] = "x"
    # Every parent variable outside the list must be absent (the OS adds its own locale ones).
    leaked = sorted(set(os.environ) - set(rgb._INTAKE_ENV) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"})
    probes = {"env": f"import json, os; ok = not set({leaked!r}) & set(os.environ)\n",
              "isolated": "import json, sys; ok = bool(sys.flags.isolated)\n"}
    try:
        for name, body in probes.items():
            (fakes / f"{name}.py").write_text(body + "print(json.dumps({'verdict': 'duplicate' if ok else 'none', "
                                                    "'request_id': 'probe'}))\n")
            rgb._BUILD_REQUEST = fakes / f"{name}.py"
            try:
                write(f"task-probe-{name}", thread_message(f"$p{name}"))
            finally:
                rgb._BUILD_REQUEST = real
            check(f"the verdict's child passes the {name} probe", settled(f"task-probe-{name}")
                  and not queued(f"task-probe-{name}"))
    finally:
        os.environ.pop("REMOTE_TASK_TOKEN", None)
        os.environ.pop("SOME_OTHER_SECRET", None)

    # A late pass already past the round's wait runs no check: the message is delivered at once.
    calls = []
    real_verdict = rgb._build_thread_verdict
    rgb._build_thread_verdict = lambda *x, **k: calls.append(x) or real_verdict(*x, **k)
    rgb._INTAKE_MODE["round"] += 1  # a new served round, as _take_served_round starts one
    rgb._INTAKE_MODE["deadline"] = time.monotonic() - 1.0
    try:
        write("task-overdue", thread_message("$od1"))
    finally:
        rgb._INTAKE_MODE["deadline"] = None
        rgb._build_thread_verdict = real_verdict
    check("past the round's wait a deferred message is delivered with no check", queued("task-overdue") and calls == [])

    # A room's ledger lock held by another process: delivered within the verdict's budget.
    rid6 = "eeeeeeee-7a4d-4e8b-9f61-2c5d8a0b7e14"
    fenced("task-fence6", fence(rid6))
    lock_dir = br.Ledger(tmp / "state" / "commons-app-builds")._path(ROOM, rid6).parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = os.open(str(lock_dir / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    began = time.monotonic()
    try:
        write("task-locked", thread_message("$k1"))
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    check("a held ledger lock delivers the message within the verdict's budget",
          queued("task-locked") and time.monotonic() - began < 2.5)

    # The lane dedupe still runs after an undecided verdict.
    rgb._BUILD_REQUEST = fakes / "crashes.py"
    rgb.GATEWAY_INSTANCE = "sspace"
    try:
        write("task-crashes-copy", thread_message("$crashes1"))
    finally:
        rgb.GATEWAY_INSTANCE = ""
        rgb._BUILD_REQUEST = real
    check("the lane dedupe still settles another lane's copy",
          not (rgb.TASKS_DIR / "task-sspace~task-crashes-copy.txt").exists())

    # 6. The owner retypes the description long after the fence, the client's copy never having
    #    come: the owner's own message, delivered (the slot holds only within the settle time).
    rid7 = "ffffffff-7a4d-4e8b-9f61-2c5d8a0b7e14"
    fenced("task-fence7", dict(fence(rid7, description="A rota for the kitchen"), timestamp=now_iso(-120)))
    write("task-retyped", thread_message("$y1", text="A rota for the kitchen"))
    rid7b = "22222222-7a4d-4e8b-9f61-2c5d8a0b7e14"
    fenced("task-fence7b", dict(fence(rid7b, description="A rota for the garden"), timestamp=now_iso(-10)))
    write("task-prompt", thread_message("$y2", text="A rota for the garden"))
    check("while the same words ten seconds after their fence are still its copy", not queued("task-prompt")
          and settled("task-prompt"))
    check("a retyped description two minutes after its fence is delivered", queued("task-retyped"))

    # 7. Switch on: the owner's answer to the request's question is delivered, never settled.
    rid8 = "11111111-7a4d-4e8b-9f61-2c5d8a0b7e14"
    fenced("task-fence8", fence(rid8))
    fence8 = (rgb.TASKS_DIR / "task-fence8.txt").read_text()
    led = br.Ledger(tmp / "state" / "commons-app-builds")
    claimed = led.claim(ROOM, rid8, fence=br.fence_facts(fence8))
    asked = led.ask(ROOM, rid8, thread_root=ROOT, owner=OWNER, question="Morning or afternoon?", task_text=fence8)
    os.environ["BUILD_THREAD_CONVERSATION"] = "true"
    try:
        write("task-answer", thread_message("$ans1", text="Morning"))
    finally:
        os.environ.pop("BUILD_THREAD_CONVERSATION", None)
    check("with the switch on, the owner's answer is delivered", claimed[0] == "claimed" and asked
          and queued("task-answer"), repr((claimed, asked)))

    # 8. The bridge's own tier check: a team-tier sender never starts the verdict.
    calls = []
    real_verdict = rgb._build_thread_verdict
    rgb._build_thread_verdict = lambda *x, **k: calls.append(x) or real_verdict(*x, **k)
    try:
        write("task-team2", thread_message("$t2", tier="team"))
        write("task-owner2", thread_message("$o2", text="and a notes column"))
    finally:
        rgb._build_thread_verdict = real_verdict
    check("a team-tier sender never starts the verdict; the owner's message does",
          queued("task-team2") and len(calls) == 1 and calls[0][1] == "owner")

    # 9. The review on 5807e0eb. Switch off, an old app thread: the owner's "thanks" never waits.
    rid9 = "33333333-7a4d-4e8b-9f61-2c5d8a0b7e14"
    fenced("task-fence9", dict(fence(rid9, description="A quiz for the team"), timestamp=now_iso(-7 * 86400)))
    root9 = "$threadRoot9:s.space"  # its own thread, so only this week-old fence names it
    fenced("task-fence9b", dict(fence("55555555-7a4d-4e8b-9f61-2c5d8a0b7e14", description="A quiz",
                                      thread_root=root9), timestamp=now_iso(-7 * 86400)))
    calls9 = []
    real_verdict = rgb._build_thread_verdict
    rgb._build_thread_verdict = lambda *x, **k: calls9.append(x) or real_verdict(*x, **k)
    rgb._INTAKE_MODE["round"] += 1  # a new served round, as _take_served_round starts one
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 6.0
    began = time.monotonic()
    try:
        write("task-thanks9", thread_message("$th9", text="thanks, looks great", root=root9))
    finally:
        rgb._INTAKE_MODE["deadline"] = None
        rgb._build_thread_verdict = real_verdict
    check("an owner's thanks in a thread whose only fence is a week old is delivered at once, unchecked",
          queued("task-thanks9") and time.monotonic() - began < 1.5 and calls9 == [])

    # The first pass runs no check at all: every threaded owner task is taken after the acks.
    calls = []
    real_verdict = rgb._build_thread_verdict
    rgb._build_thread_verdict = lambda *x, **k: calls.append(x) or real_verdict(*x, **k)
    rgb._INTAKE_MODE["round"] += 1
    rgb._INTAKE_MODE["defer"] = True
    try:
        write("task-first-pass", thread_message("$fp1"))
        first_pass = "queued"
    except rgb._IntakeDeferred:
        first_pass = "deferred"
    finally:
        rgb._INTAKE_MODE["defer"] = False
        rgb._build_thread_verdict = real_verdict
    check("the first pass defers a threaded owner task without running its check",
          first_pass == "deferred" and calls == [] and not queued("task-first-pass"))

    # A build-thread wait never spends the lane dedupe's: its own runs first, later tasks keep theirs.
    import lane_dedupe  # noqa: PLC0415
    seen_waits = []
    real_reason = lane_dedupe.duplicate_reason

    def reason(*a, wait_s=None, **k):
        seen_waits.append(wait_s)
        return None
    lane_dedupe.duplicate_reason = reason
    rgb.BUILD_THREAD_ROUND_S, real_round = 1.0, rgb.BUILD_THREAD_ROUND_S
    os.environ["BUILD_THREAD_CONVERSATION"] = "true"
    rid10 = "44444444-7a4d-4e8b-9f61-2c5d8a0b7e14"
    root10 = "$threadRoot10:s.space"  # its own thread: no question is open there
    fenced("task-fence10", fence(rid10, mode="refine", app_id="a1b2c3d4", thread_message="$elsewhere",
                                description="a later change", thread_root=root10))
    seen_waits.clear()
    rgb._INTAKE_MODE["round"] += 1  # a new served round, as _take_served_round starts one
    rgb._INTAKE_MODE["deadline"] = time.monotonic() + 3.0
    try:
        write("task-waits10", thread_message("$w10", text="make the title bigger", root=root10))
        write("task-next10", thread_message("$n10", text="and darker", root=root10))
    finally:
        rgb._INTAKE_MODE["deadline"] = None
        os.environ.pop("BUILD_THREAD_CONVERSATION", None)
        rgb.BUILD_THREAD_ROUND_S = real_round
        lane_dedupe.duplicate_reason = real_reason
    check("the waiting task's own lane dedupe ran first, with the round's whole wait",
          len(seen_waits) == 2 and seen_waits[0] is not None and seen_waits[0] > 2.5, repr(seen_waits))
    check("and the next task's lane dedupe still gets the rest of the round",
          len(seen_waits) == 2 and seen_waits[1] is not None and seen_waits[1] > 1.5, repr(seen_waits))
    check("both are delivered", queued("task-waits10") and queued("task-next10"))

    # Wiring: the verdict goes through the shared runner, isolated, the question on stdin.
    import optional_script  # noqa: PLC0415
    got = {}
    real_runner = optional_script.run_optional_script

    def runner(script, args, **kw):
        got.update(script=script, args=list(args), **kw)
        return '{"verdict": "none", "request_id": null}'
    optional_script.run_optional_script = runner
    try:
        write("task-wired", thread_message("$wire1", text="wired"))
    finally:
        optional_script.run_optional_script = real_runner
    question = json.loads(got.get("input") or "{}")
    check("the adapter delegates to run_optional_script", got.get("script") == rgb._BUILD_REQUEST
          and got.get("args", [])[:2] == ["intake-verdict", "-"] and got.get("isolated") is True
          and set(got.get("env") or {}) <= set(rgb._INTAKE_ENV) and got.get("timeout") == rgb.BUILD_THREAD_VERDICT_S
          and question.get("tier") == "owner" and question.get("fields", {}).get("task") == "wired"
          and queued("task-wired"), repr({k: v for k, v in got.items() if k != "on_error"}))

    # 11. A served round of [plain, ordinary thread, app thread, plain]: only the app thread's task
    #     is taken after the acks; the ordinary thread keeps its place, and runs no check.
    order, checks = [], []
    real_write, real_ack, real_verdict = rgb._write_task, rgb._post_task_ack, rgb._build_thread_verdict

    def recording(task, *a, **k):
        got = real_write(task, *a, **k)
        if got:
            order.append(task["id"])
        return got
    rgb._write_task = recording
    rgb._post_task_ack = lambda *a, **k: True
    rgb._build_thread_verdict = lambda *x, **k: checks.append(x[0]["id"]) or real_verdict(*x, **k)
    plain = dict(thread_message("$pl1", text="hello"), thread_root=None)
    ordinary = thread_message("$or1", text="a reply in a chat thread", root="$chatRoot:s.space")
    app = thread_message("$ap1", text="and a footer")
    plain2 = dict(thread_message("$pl2", text="hello again"), thread_root=None)
    try:
        rgb._take_served_round([{"id": "task-r1", **plain}, {"id": "task-r2", **ordinary},
                                {"id": "task-r3", **app}, {"id": "task-r4", **plain2}], set(), [], {}, [])
    finally:
        rgb._write_task, rgb._post_task_ack, rgb._build_thread_verdict = real_write, real_ack, real_verdict
    check("an ordinary thread keeps its place; only the app thread is taken after the acks",
          order == ["task-r1", "task-r2", "task-r4", "task-r3"], repr(order))
    check("and only the app thread's task runs a check", checks == ["task-r3"], repr(checks))

    # 12. An unreadable or hostile index is no app thread: delivered in place, no check, no crash.
    index = tmp / "state" / "commons-app-builds" / "app-threads.json"
    kept_index = index.read_text()
    calls12 = []
    real_verdict = rgb._build_thread_verdict
    rgb._build_thread_verdict = lambda *x, **k: calls12.append(x) or real_verdict(*x, **k)
    try:
        for n, bad in enumerate(("{not json", "[]", json.dumps({ROOM: []}), json.dumps({ROOM: {ROOT: "soon"}}),
                                 '{"%s": {"%s": Infinity}}' % (ROOM, ROOT), '{"%s": {"%s": NaN}}' % (ROOM, ROOT),
                                 json.dumps({ROOM: {ROOT: True}}))):
            index.write_text(bad)
            write(f"task-badindex{n}", thread_message(f"$bi{n}"))
            check(f"an index that is {bad[:28]!r} is no app thread", queued(f"task-badindex{n}"))
        index.unlink()
        write("task-noindex", thread_message("$bi9"))
        check("no index at all is no app thread", queued("task-noindex"))
    finally:
        index.write_text(kept_index)
        rgb._build_thread_verdict = real_verdict
    check("and none of them ran a check", calls12 == [])

    # 13. The fence and its copy in ONE served round: the fence is taken in the first pass and
    #     indexes its thread, so the copy (an app thread now) is checked after the acks and settled.
    real_ack = rgb._post_task_ack
    rgb._post_task_ack = lambda *a, **k: True
    try:
        rgb._take_served_round([{"id": "task-rf13", **fence("13131313-7a4d-4e8b-9f61-2c5d8a0b7e14", description="A poll",
                                                         thread_root="$root13:s.space")},
                                {"id": "task-rc13", **thread_message("$c13", text="A poll", root="$root13:s.space")}],
                               set(), [], {}, [])
    finally:
        rgb._post_task_ack = real_ack
    check("a fence and its copy in one round: the copy is settled", queued("task-rf13") and not queued("task-rc13")
          and settled("task-rc13"))

    # The pre-check costs one substring test: only an owner DM with the fence's opening line starts the
    # skill, and the skill indexes only a whole, valid fence.
    spawned = []
    real_verdict = rgb._build_thread_verdict
    rgb._build_thread_verdict = lambda *x, **k: spawned.append(x[0]["id"]) or real_verdict(*x, **k)
    index = tmp / "state" / "commons-app-builds" / "app-threads.json"
    before = index.read_text()
    dm = {k: v for k, v in fence("14141414-7a4d-4e8b-9f61-2c5d8a0b7e14").items() if k != "task"}
    try:
        write("task-dm-plain", dict(dm, task="Can you build me a poll for Saturday?"))
        write("task-dm-quote", dict(dm, task="The docs say to start with ```commons-app-build and then {oops"))
        write("task-dm-guest", dict(fence("15151515-7a4d-4e8b-9f61-2c5d8a0b7e14", thread_root="$root15:s.space"),
                                    user_id=OTHER, access_tier="guest"))
    finally:
        rgb._build_thread_verdict = real_verdict
    check("an owner DM without the fence's opening line starts no process", "task-dm-plain" not in spawned)
    check("a DM that only quotes the marker is handed over, but indexes nothing", spawned == ["task-dm-quote"]
          and index.read_text() == before)
    check("a non-owner's fence-like DM never reaches the skill", "task-dm-guest" not in spawned
          and "$root15:s.space" not in index.read_text())
    check("and all three are delivered", queued("task-dm-plain") and queued("task-dm-quote") and queued("task-dm-guest"))

    # 14. Fence DMs in the first pass count against the round's 3 s budget: with a hung script and
    #     six fence DMs, the round's first ack still comes within the budget, and every DM is delivered.
    hung = fakes / "hung.py"
    hung.write_text("import time; time.sleep(60)\n")
    acks = []
    began = time.monotonic()
    real_ack, real_script, real_verdict = rgb._post_task_ack, rgb._BUILD_REQUEST, rgb._build_thread_verdict
    started14 = []
    rgb._post_task_ack = lambda *a, **k: acks.append(time.monotonic() - began) or True
    rgb._BUILD_REQUEST = hung
    rgb._build_thread_verdict = lambda *x, **k: started14.append(x[2]) or real_verdict(*x, **k)
    batch = [{"id": f"task-hf{i}", **fence(f"{i}{i}{i}{i}{i}{i}{i}{i}-7a4d-4e8b-9f61-2c5d8a0b7e14",
                                           thread_root=f"$rootH{i}:s.space")} for i in range(1, 7)]
    try:
        rgb._take_served_round(batch, set(), [], {}, [])
    finally:
        rgb._post_task_ack, rgb._BUILD_REQUEST, rgb._build_thread_verdict = real_ack, real_script, real_verdict
    check("once the round's budget is spent, no further check starts", len(started14) == 2, repr(started14))
    check("six fence DMs with a hung script: the first ack comes within the round's budget",
          acks and acks[0] < rgb.BUILD_THREAD_ROUND_S + 1.5, repr(acks[:1]))
    check("and every fence DM is delivered", all(queued(f"task-hf{i}") for i in range(1, 7)))

    print(f"commons-app-builder intake gate: {'FAIL' if failures else 'OK'} ({len(failures)} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
