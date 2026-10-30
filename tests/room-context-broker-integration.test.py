#!/usr/bin/env python3
"""The combined poll (#5865) against the AG2 Space broker's real `/v1` server.

Sutando's side is the production code, unmodified: a bridge process runs
`remote_gateway_bridge.main()` (the real poll loop, which sends
`capabilities=voice-outcome-v1,room-context-v1`) over a hermetic workspace, writes
the task file through the real `_write_task` / `room_context.validate`, and the
model view is produced by the real `src/room_context.py view` CLI that
task-notifier points the core at.

The broker's side is the backend's own in-process server: `api.v1.build_app` over
the `ApiDeps` its tests build (`tests/test_api_v1._deps`), with the room state read
through `tests/test_room_context.RoomStateMasq` and the voice ask queued as
`tests/test_task_state_routes._granted` queues one. Nothing on either side is
reimplemented; the only test-only addition is an observing aiohttp middleware that
records what each poll was served and what each POST carried.

Needs a backend checkout: AG2SPACE_BACKEND_DIR=<ag2space-backend checkout> (its
services/agent-message-broker is imported) and aiohttp + pytest importable. Without
them every case is skipped with the reason, so ordinary CI is unaffected.

Not covered here: a bridge restart between serve and ack, and exactly-once delivery
(the crash-point tests in gateway-voice-outcome-contract / gateway-ack-first cover
those against the mock relay, not against this broker).

Run: AG2SPACE_BACKEND_DIR=/path/to/ag2space-backend python3 tests/room-context-broker-integration.test.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Every spawn below inherits nothing from the outer environment; only the backend checkout passes.
isolate_os_environ(AG2SPACE_BACKEND_DIR=os.environ.get("AG2SPACE_BACKEND_DIR", ""))

import asyncio  # below the isolation: not in the checker's 3.9 stdlib fallback

_TESTS = Path(__file__).resolve().parent
_REPO = _TESTS.parent

COMBINED = "voice-outcome-v1,room-context-v1"
# The poll string the bridge sent before #5865 (the voice-only baseline of case a).
VOICE_ONLY_PROOF = "&capabilities=voice-outcome-v1"

_BACKEND = os.environ.get("AG2SPACE_BACKEND_DIR", "").strip()
_BROKER_DIR = Path(_BACKEND) / "services" / "agent-message-broker" if _BACKEND else None
SKIP = ""
if not _BACKEND:
    SKIP = ("AG2SPACE_BACKEND_DIR is not set: point it at an ag2space-backend checkout "
            "to run the broker integration")
elif not (_BROKER_DIR / "broker" / "room_context.py").is_file():
    SKIP = f"AG2SPACE_BACKEND_DIR={_BACKEND} has no services/agent-message-broker/broker/room_context.py"
else:
    try:
        import aiohttp
        import pytest
    except ImportError as e:  # pragma: no cover - depends on the host
        SKIP = f"the backend harness needs aiohttp and pytest: {e}"

if not SKIP:
    sys.path[:0] = [str(_BROKER_DIR / "tests"), str(_BROKER_DIR), str(_BROKER_DIR.parent)]
    import test_api_v1 as api_harness
    import test_room_context as rc_harness
    from test_task_state_routes import _granted
    from aiohttp import web
    from api.v1 import build_app
    from broker import room_context as broker_rc
    AGENT, OWNER, ROOM, TOKEN = (api_harness.AGENT, api_harness.OWNER, api_harness.ROOM,
                                 api_harness.BEARER["a"])


class Broker:
    """The backend's `/v1` app on 127.0.0.1, its event loop on a thread of this process."""

    def __init__(self, test, masq, hub_kw=None):
        self.masq = masq
        # Recorded: each GET /v1/tasks query; (query, tasks) when non-empty; (path, json body, status).
        self.polls: "list[str]" = []
        self.served: "list[tuple[str, list]]" = []
        self.posted: "list[tuple[str, object, int]]" = []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.deps, self.runner, self.url = self.call(self._start(hub_kw))
        test.addCleanup(self.close)

    def call(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _start(self, hub_kw):
        deps = api_harness._deps(tempfile.mkdtemp(prefix="broker-"), masq=self.masq,
                                 hub_kw=hub_kw)
        app = build_app(deps)
        broker = self

        @web.middleware
        async def observe(request, handler):
            body = None
            if request.method in ("POST", "PUT") and request.can_read_body:
                try:
                    body = await request.json()
                except Exception:  # noqa: BLE001 - recorded as unreadable, the handler decides
                    body = "<unreadable>"
            resp = await handler(request)
            if request.method == "GET" and request.path == "/v1/tasks":
                broker.polls.append(request.query_string)
                try:
                    tasks = json.loads(resp.body).get("tasks") or []
                except Exception:  # noqa: BLE001
                    tasks = []
                if tasks:
                    broker.served.append((request.query_string, tasks))
            elif request.method in ("POST", "PUT"):
                broker.posted.append((request.path, body, resp.status))
            return resp

        app.middlewares.append(observe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return deps, runner, f"http://127.0.0.1:{port}"

    def close(self):
        async def stop():
            await self.runner.cleanup()
            await self.deps.hub.close()
        try:
            self.call(stop())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def served_tasks(self, task_id):
        return [(q, t) for q, batch in self.served for t in batch if t.get("id") == task_id]


class JoinableMasq(rc_harness.RoomStateMasq if not SKIP else object):
    """The backend's RoomStateMasq, plus the membership probe other /v1 routes ask."""

    async def joined(self, room_id, as_user):
        return self.status == 200


# The production bridge, as tests/_helpers/gateway_relay.py starts it, minus the crash
# points; argv[4] (baseline only) swaps the poll string for the pre-#5865 one.
_LAUNCH = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _helpers.hermetic_gateway import host_reads, isolate_then_import
gw, reads = isolate_then_import(root=Path(sys.argv[2]), env=json.loads(sys.argv[3]))
leaked = host_reads(reads)
if leaked:
    sys.exit("the bridge import left its workspace: " + "; ".join(leaked))
if sys.argv[4]:
    gw._POLL_PROOF = sys.argv[4]
print("poll proof in force: " + gw._POLL_PROOF, flush=True)
gw.main()
"""


class Bridge:
    def __init__(self, test, broker: Broker, proof: str = "", before_start=None):
        self.test, self.broker = test, broker
        self.root = Path(tempfile.mkdtemp(prefix="rc-integ-"))
        self.ws = self.root / "ws"
        if before_start:
            before_start(self.ws)
        self.log_path = self.root / "bridge.log"
        env = {"REMOTE_TASK_URL": broker.url, "REMOTE_TASK_TOKEN": TOKEN,
               "REMOTE_TASK_POLL_WAIT": "1", "SUTANDO_BRIDGE_LOCK": "0",
               "AGENT_MXID": AGENT, "REMOTE_TASK_TIER": "owner"}
        log = open(self.log_path, "w")
        test.addCleanup(log.close)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _LAUNCH, str(_TESTS), str(self.ws), json.dumps(env), proof],
            env=hermetic_env(self.root / "home"), stdout=log, stderr=subprocess.STDOUT)
        test.addCleanup(self.stop)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    def wait_for(self, done, what, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = done()
            if got:
                return got
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        got = done()
        if got:
            return got
        self.test.fail(f"{what}: not seen (bridge exit {self.proc.poll()}); log ends:\n"
                       f"{self.log()[-3000:]}")

    def task_files(self):
        d = self.ws / "tasks"
        return sorted(p for p in d.glob("*.txt") if p.is_file()) if d.is_dir() else []

    def task_file(self):
        return self.wait_for(lambda: (self.task_files() or [None])[0], "a published task file")


def headers_of(text: str) -> dict:
    out = {}
    for line in text.split("\n"):
        key, sep, value = line.partition(": ")
        if sep and key and " " not in key and key not in out:
            out[key] = value
    return out


def consumer_view(task_file: Path) -> str:
    """The model's view, by the CLI task-notifier names (src/room_context.py)."""
    return subprocess.run([sys.executable, str(_REPO / "src" / "room_context.py"), "view",
                           str(task_file)], check=True, capture_output=True, text=True,
                          timeout=30).stdout


def context_json(view: str):
    if "<AG2_ROOM_CONTEXT_JSON>" not in view:
        return None
    return json.loads(view.split("<AG2_ROOM_CONTEXT_JSON>\n", 1)[1]
                      .split("\n</AG2_ROOM_CONTEXT_JSON>", 1)[0])


def _shape(task: dict) -> dict:
    """A served task with its per-delivery values (lease, attempt) reduced to presence."""
    return {k: (type(v).__name__ if k in ("lease_id",) else v) for k, v in task.items()}


@unittest.skipIf(bool(SKIP), SKIP)
class CombinedPollAgainstTheBroker(unittest.TestCase):
    maxDiff = None

    def _voice_run(self, proof: str):
        """One voice relay ask through a fresh broker and a fresh bridge, to its outcome."""
        broker = Broker(self, JoinableMasq(rc_harness._state()))
        broker.call(_granted(broker.deps.hub, "ask-5f3c9e0a1b2c"))
        bridge = Bridge(self, broker, proof)
        tfile = bridge.task_file()
        text = tfile.read_text()
        tid = headers_of(text)["id"]
        (bridge.ws / "results").mkdir(parents=True, exist_ok=True)
        (bridge.ws / "results" / f"{tid}.txt").write_text("Two meetings today.\n")
        rec = bridge.wait_for(
            lambda: (lambda r: r if r and r.get("state") == "result_ready" else None)(
                broker.call(broker.deps.hub.task_state("ask-5f3c9e0a1b2c"))),
            "the typed outcome recorded")
        served = broker.served_tasks("ask-5f3c9e0a1b2c")
        acks = [(p, b) for p, b, _s in broker.posted if p.endswith("/ack")]
        results = [(b, s) for p, b, s in broker.posted if p == "/v1/results"]
        return {"query": [q for q, _t in served], "served": [_shape(t) for _q, t in served],
                "ack_paths": [p for p, _b in acks],
                "ack_bodies": [{k: v for k, v in b.items() if k != "lease_id"}
                               for _p, b in acks if isinstance(b, dict)],
                "results": [(b, s) for b, s in results],
                "record": {k: rec.get(k) for k in ("state", "outcome", "dm_only", "proof",
                                                   "consolidated_into")},
                "task_headers": {k: v for k, v in headers_of(text).items()
                                 if k in ("source", "access_tier", "ack_first", "room_context",
                                          "room_context_status", "room_context_binding")},
                "log": bridge.log()}

    def test_a_voice_outcome_unchanged_against_a_voice_only_poll(self):
        combined = self._voice_run("")
        baseline = self._voice_run(VOICE_ONLY_PROOF)
        print("\n[a] combined poll query:", combined["query"])
        print("[a] baseline poll query:", baseline["query"])
        print("[a] served:", json.dumps(combined["served"]))
        print("[a] result POST:", json.dumps(combined["results"]))
        print("[a] broker record:", combined["record"])
        self.assertIn("poll proof in force: &capabilities=" + COMBINED, combined["log"])
        self.assertIn("poll proof in force: " + VOICE_ONLY_PROOF, baseline["log"])
        self.assertEqual(len(combined["served"]), 1, combined["log"][-2000:])
        # On the wire, not just in the bridge's log: each arm's serving poll names exactly its set.
        caps = lambda q: sorted(c for v in parse_qs(q).get("capabilities", []) for c in v.split(","))
        self.assertEqual(len(baseline["query"]), 1, baseline["log"][-2000:])
        self.assertEqual(caps(combined["query"][0]), ["room-context-v1", "voice-outcome-v1"])
        self.assertEqual(caps(baseline["query"][0]), ["voice-outcome-v1"])
        self.assertTrue(combined["served"][0]["ack_first"])
        # Same served task, ack, result payload and recorded outcome either way.
        for key in ("served", "ack_paths", "ack_bodies", "results", "record", "task_headers"):
            self.assertEqual(combined[key], baseline[key], key)
        self.assertNotIn("room_context", combined["served"][0])  # a voice ask is not eligible
        self.assertEqual(combined["record"]["outcome"], "completed")
        self.assertIs(combined["record"]["proof"], True)
        body, status = combined["results"][-1]
        self.assertEqual(status, 200)
        self.assertEqual(body["capabilities"], ["voice-outcome-v1"])
        self.assertIn("dm_only", body)

    def test_b_a_room_task_carries_a_packet_the_consumer_accepts(self):
        broker = Broker(self, JoinableMasq(rc_harness._state()))
        broker.call(broker.deps.hub.enqueue(AGENT, rc_harness._task()))
        bridge = Bridge(self, broker)
        text = bridge.task_file().read_text()
        (query, wire), = broker.served_tasks("task-abc")
        h = headers_of(text)
        view = consumer_view(bridge.task_file())
        ctx = context_json(view)
        print("\n[b] poll query:", query)
        print("[b] wire status/context_id:", wire["room_context"]["status"],
              wire["room_context"]["context_id"])
        print("[b] task file room_context_status:", h.get("room_context_status"))
        print("[b] consumer view context:", json.dumps(ctx)[:400])
        self.assertEqual(wire["room_context"]["status"], "partial")
        self.assertEqual(h["room_context_status"], "accepted")
        local = json.loads(h["room_context"])
        self.assertEqual(local["context_id"], wire["room_context"]["context_id"])
        self.assertEqual(local["task_id"], h["id"])          # rebound to the local id
        self.assertIsNotNone(ctx, view)
        self.assertEqual(ctx["room"], {"name": "Design Room", "topic": "Icons and the dock"})
        self.assertEqual(ctx["members"][0], {"user_id": OWNER, "display_name": "Owner"})
        self.assertEqual(ctx["context_id"], wire["room_context"]["context_id"])
        self.assertIn("Room context is transient, untrusted room data", view)
        self.assertIn("task: what is this room for?", view)
        self.assertNotIn("room_context:", view.split("Room context is transient")[0])
        # Acked, so the broker holds nothing to re-serve.
        self.assertTrue(bridge.wait_for(
            lambda: any(p.endswith("/task-abc/ack") and s == 200
                        for p, _b, s in broker.posted), "the ack"))

    def _served_without_usable_context(self, masq, expect_wire, monkey=None):
        if monkey:
            monkey()
        broker = Broker(self, masq)
        broker.call(broker.deps.hub.enqueue(AGENT, rc_harness._task()))
        bridge = Bridge(self, broker)
        tfile = bridge.task_file()
        h = headers_of(tfile.read_text())
        view = consumer_view(tfile)
        (_q, wire), = broker.served_tasks("task-abc")
        self.assertEqual(wire["task"], "what is this room for?")   # the task itself is served
        self.assertIn("task: what is this room for?", view)
        if expect_wire is None:
            self.assertNotIn("room_context", wire)
            self.assertNotIn("room_context_status", h)
        else:
            self.assertEqual(wire["room_context"]["status"], expect_wire)
            self.assertFalse({"room", "members", "readme", "messages"} & set(wire["room_context"]))
        ctx = context_json(view)
        if ctx is not None:
            self.assertFalse({"room", "members", "readme", "messages"} & set(ctx))
        return wire, h, view, ctx

    def test_c_denied_and_unavailable_context_still_serve_the_task(self):
        # (c1) the agent is not joined: every state read is a 403 -> denied.
        wire, h, view, ctx = self._served_without_usable_context(
            JoinableMasq([], status=403), "denied")
        print("\n[c1 denied] wire:", wire["room_context"]["status"],
              wire["room_context"]["coverage"])
        print("[c1 denied] consumer view context:", json.dumps(ctx))
        self.assertEqual(wire["room_context"]["coverage"]["reason"], "not_joined")
        self.assertEqual(h["room_context_status"], "accepted")
        self.assertEqual(ctx["status"], "denied")
        # (c2) the agent left: its leave-time snapshot is not its state -> denied.
        wire, h, view, ctx = self._served_without_usable_context(
            JoinableMasq(rc_harness._state(agent_membership="leave")), "denied")
        print("[c2 left] wire reason:", wire["room_context"]["coverage"]["reason"])
        self.assertEqual(ctx["status"], "denied")
        # (c3) the state read fails -> unavailable.
        wire, h, view, ctx = self._served_without_usable_context(
            JoinableMasq(rc_harness._state(), status=500), "unavailable")
        print("[c3 unavailable] wire:", wire["room_context"]["status"],
              wire["room_context"]["coverage"])
        self.assertEqual(ctx["status"], "unavailable")
        # (c4) the builder faults -> no packet at all; the consumer sees a legacy task.
        real_build = broker_rc.build

        def boom(*a, **k):
            raise RuntimeError("builder fault")
        broker_rc.build = boom
        self.addCleanup(setattr, broker_rc, "build", real_build)
        wire, h, view, ctx = self._served_without_usable_context(
            JoinableMasq(rc_harness._state()), None)
        print("[c4 no packet] wire keys:", sorted(wire))
        print("[c4 no packet] view has context block:", "<AG2_ROOM_CONTEXT_JSON>" in view)
        self.assertIsNone(ctx)
        broker_rc.build = real_build

    def test_d_a_redelivery_carries_a_fresh_packet_the_consumer_uses(self):
        # A short lease so the broker's own expiry re-serves an unacked task.
        masq = JoinableMasq(rc_harness._state(name="Design Room"))
        broker = Broker(self, masq, hub_kw={"visibility_timeout": 2})
        broker.call(broker.deps.hub.enqueue(AGENT, rc_harness._task()))
        # The first delivery cannot be written locally (tasks/ read-only), so the
        # bridge drops it unacked, as it does for any task it could not queue.
        tasks_dir = None

        def read_only_tasks(ws):
            nonlocal tasks_dir
            for d in ("claude-config", "tasks", "results", "state", "home"):
                (ws / d).mkdir(parents=True, exist_ok=True)
            tasks_dir = ws / "tasks"
            os.chmod(tasks_dir, stat.S_IRUSR | stat.S_IXUSR)
            self.addCleanup(os.chmod, tasks_dir, stat.S_IRWXU)

        bridge = Bridge(self, broker, before_start=read_only_tasks)
        _first_q, first = bridge.wait_for(
            lambda: (broker.served_tasks("task-abc") or [None])[0], "the first delivery")
        dropped = bridge.wait_for(
            lambda: next((ln for ln in bridge.log().splitlines()
                          if "task-abc" in ln and ("not taken" in ln or "FAILED" in ln)), None),
            "the first delivery dropped unacked")
        print("\n[d] first delivery at the bridge:", dropped.strip())
        self.assertFalse(any(p.endswith("/task-abc/ack") for p, _b, _s in broker.posted),
                         "the first delivery must not have been acked")
        self.assertEqual(bridge.task_files(), [])
        # The room changes between deliveries; tasks/ becomes writable again, and the
        # broker's lease expiry (not the test) re-serves the task.
        masq.state = rc_harness._state(name="Design Room v2", topic="Icons, dock, menus")
        os.chmod(tasks_dir, stat.S_IRWXU)
        tfile = bridge.wait_for(lambda: (bridge.task_files() or [None])[0],
                                "the redelivery written", timeout=90)
        deliveries = broker.served_tasks("task-abc")
        self.assertGreaterEqual(len(deliveries), 2, bridge.log()[-2000:])
        _q2, second = deliveries[-1]
        h = headers_of(tfile.read_text())
        view = consumer_view(tfile)
        ctx = context_json(view)
        local = json.loads(h["room_context"])
        print("[d] deliveries served:", len(deliveries),
              "attempts:", [t.get("attempt") for _q, t in deliveries])
        print("[d] first  packet:", first["room_context"]["context_id"],
              first["room_context"]["room"], "observed_at", first["room_context"]["observed_at"])
        print("[d] second packet:", second["room_context"]["context_id"],
              second["room_context"]["room"], "observed_at", second["room_context"]["observed_at"])
        print("[d] task file context_id:", local["context_id"], "room:", local["room"])
        print("[d] consumer view room:", ctx["room"])
        self.assertEqual(second["id"], first["id"])
        self.assertGreater(second["attempt"], first["attempt"])
        self.assertNotEqual(second["lease_id"], first["lease_id"])
        self.assertNotEqual(second["room_context"]["context_id"], first["room_context"]["context_id"])
        self.assertEqual(first["room_context"]["room"]["name"], "Design Room")
        self.assertEqual(second["room_context"]["room"]["name"], "Design Room v2")
        # What the consumer used is the second packet, not the first.
        self.assertEqual(h["room_context_status"], "accepted")
        self.assertEqual(local["context_id"], second["room_context"]["context_id"])
        self.assertEqual(ctx["context_id"], second["room_context"]["context_id"])
        self.assertEqual(ctx["room"], {"name": "Design Room v2", "topic": "Icons, dock, menus"})
        self.assertNotIn(first["room_context"]["context_id"], tfile.read_text())
        self.assertTrue(bridge.wait_for(
            lambda: any(p.endswith("/task-abc/ack") and s == 200
                        for p, _b, s in broker.posted), "the redelivery acked"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
