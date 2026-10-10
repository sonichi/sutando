#!/usr/bin/env python3
"""A task result carrying `[thread: $ask]` is posted in a thread opened on its own ask.

The result server only cites the asking message, so the gateway posts a threaded
result itself, through the thread outbox (an at-most-once `op: message` send), and
closes the task's lease with no_send only once that post is DELIVERED, or parked
with an unknown outcome. A post that provably never happened falls back to the
ordinary result, so the asker gets the answer exactly once.

  a) [thread: $ask]                        -> one thread post in the task's room, lease closed silently
  b) no marker                             -> ordinary result, no thread post
  c) [channel: task room] + [thread:]      -> threaded in the task's room
  d) [channel: other room] + [thread:]     -> root dropped (not the task's room), ordinary redirect
  e) malformed [thread: x]                 -> ordinary result, marker never posted
  f) [dm-only] + [thread:]                 -> ordinary result, no thread post
  g) lease close fails once                -> retried, still exactly one thread post
  h) task room unknown                     -> ordinary result
  i) [thread:] + [file:]                   -> ordinary result (uploads ride the result path)
  j) a Signal (task-media) task            -> ordinary result (the server answers in its request thread)
  k) [thread:] with an empty body          -> ordinary result, never posted in a thread
  l) thread post refused (HTTP 400)        -> the answer goes out once, as the ordinary result
  m) accepted without an event_id          -> one send, never repeated; lease closed, loud log
  n) HTTP 502 / timeout                    -> same as m (the post may be out)
  o) HTTP 429 every time                   -> retried to the cap, then the ordinary result
  p) connection refused, then accepted     -> retried, exactly one thread post
  q) reply replaced after A was posted     -> B is posted too, never archived unposted
  r) media sidecar unreadable              -> deferred (no post, lease open); repaired -> ordinary
  s) alias ledger unreadable               -> deferred (no post, lease open)
  t) the ask is itself in a thread         -> ordinary result (answered in the ask's thread)
  u) root is not the asking message        -> ordinary result
  v) task room is not a Matrix room        -> ordinary result, nothing stranded
  w) inline marker prose in the body       -> posted byte for byte
  x) a dead sender's claim on the post     -> parked unknown, never re-sent; lease closed
  y) task file read: unreadable -> deferred, missing -> ordinary
  z) REMOTE_PROACTIVE_TRUST_OK + bare ok   -> confirmed
  P) AG2SpaceRoomMessageProvider classification, directly

Loads src/remote-gateway-bridge.py in-process, so the production claim gate is
installed (asserted), with Discord configured, alive and last-active; isolated
workspace, fake `_req`; never runs the wrapper as a process.
Run: python3 tests/gateway-task-result-thread.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "remote-gateway-bridge.py"
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
AGENT = "@agent:example.org"
OWNER = "@o:example.org"
OWNER_DM = "!dm:example.org"
ROOM = "!taskroom:example.org"
OTHER = "!other:example.org"
ASK = "$ask:example.org"
OK = {"ok": True, "event_id": "$evt"}
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://gw/v1/room", code, "x", {}, None)


def load(ws: Path, cfg: Path):
    os.environ.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
                       "CLAUDE_CONFIG_DIR": str(cfg), "AG2_DEVICE_ENV": "",
                       "REMOTE_TASK_URL": "http://127.0.0.1:9", "REMOTE_TASK_TOKEN": "t",
                       "REMOTE_PROACTIVE_ROOM": "", "AGENT_MXID": AGENT,
                       "DO_NOT_TRACK": "1", "SUTANDO_TELEMETRY": "0"})
    os.environ.pop("GATEWAY_INSTANCE", None)
    os.environ.pop("REMOTE_PROACTIVE_TRUST_OK", None)
    name = f"rgb_task_thread_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(name, _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Run:
    """One task + result through the production drain; `room` scripts each /v1/room reply."""

    def __init__(self, result: str, *, room=(), lease_fails: int = 0, task_room: str = ROOM,
                 ask_thread: str = "", forget_room: bool = False, attach: bool = False,
                 signal: bool = False, trust_ok: bool = False):
        ws = Path(tempfile.mkdtemp(prefix="gw-task-thread-ws-"))
        cfg = Path(tempfile.mkdtemp(prefix="gw-task-thread-cfg-"))
        for d in ("results", "state", "logs", "tasks"):
            (ws / d).mkdir()
        (ws / "state" / "last-owner-activity.json").write_text(json.dumps(
            {"ts": int(time.time()), "channel": "discord", "summary": "t"}))
        (cfg / "channels" / "discord").mkdir(parents=True)
        (cfg / "channels" / "discord" / ".env").write_text("DISCORD_BOT_TOKEN=x\n")
        (ws / "state" / "discord-bridge.heartbeat").write_text("alive")
        self.ws = ws
        self.mod = mod = load(ws, cfg)
        self.attempts: list[dict] = []
        self.leases: list[tuple] = []
        self.logs: list[str] = []
        self.room = list(room)
        self.fails = [lease_fails]

        def fake_req(method, path, payload=None, timeout=None):
            if path == "/v1/agents":
                return {"agents": [{"id": AGENT, "owner": OWNER, "owner_dm_room": OWNER_DM}]}
            if path == "/v1/room" and (payload or {}).get("op") == "message":
                self.attempts.append(dict(payload))
                reply = self.room.pop(0) if self.room else OK
                if isinstance(reply, BaseException):
                    raise reply
                return reply
            return OK

        def fake_deliver(tid, broker_tid, body, no_send=False, result_file=None, **_kw):
            self.leases.append((body, no_send))
            if self.fails[0]:
                self.fails[0] -= 1
                return False
            return True

        mod._req = fake_req
        mod._log = self.logs.append
        mod._reenroll_identity = lambda: AGENT
        mod.LOCAL_TIER = "owner"
        mod.PROACTIVE_TRUST_OK = trust_ok
        mod._load_tier_map = lambda: {}
        mod._fleet_agent_ids = lambda: set()
        mod._deliver_result_payload = fake_deliver
        mod._save_inflight = lambda s: True
        mod._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=time.time() + 3600)
        task = {"id": "tt1", "task": "what is the status?", "source": "ag2space",
                "channel_id": task_room, "source_room_id": task_room,
                "source_message_id": ASK, "user_id": OWNER, "access_tier": "owner"}
        if ask_thread:
            task["thread_root"] = ask_thread
        self.tid = tid = mod._write_task(task)[0]
        if forget_room:
            mod._save_task_rooms({})
        if signal:
            check(mod._record_task_media(tid, {"signal": {}, "thread_root": ""}),
                  "media mode recorded")
        if attach:
            f = ws / "results" / "chart.png"
            f.write_bytes(b"\x89PNG\r\n")
            result = f"{result}\n[file: {f}]"
        self.write(result)
        self.gate = mod.PROACTIVE_CLAIM_GATE is mod._ag2space_proactive_claim_gate
        self.inflight = {tid}

    def write(self, result: str) -> None:
        (self.mod.RESULTS_DIR / f"{self.tid}.txt").write_text(result, encoding="utf-8")

    def passes(self, n: int = 2) -> "Run":
        for _ in range(n):
            self.mod._post_ready_results(self.inflight)
            self.mod._post_proactive()
        return self

    @property
    def left(self) -> list[str]:
        return sorted(p.name for p in self.mod.RESULTS_DIR.glob("*.txt"))

    def logged(self, text: str) -> bool:
        return any(text in ln for ln in self.logs)


def main() -> int:
    r = Run(f"[thread: {ASK}]\nall green\n").passes()
    check(r.gate, "the loader's production claim gate is installed")
    p = r.attempts[0] if r.attempts else {}
    check(len(r.attempts) == 1, f"a) exactly one thread post over two passes, got {r.attempts}")
    check(p.get("room_id") == ROOM, f"a) the task's room, got {p.get('room_id')!r}")
    check(p.get("thread_root") == ASK, f"a) in the thread on the ask, got {p.get('thread_root')!r}")
    check(p.get("body") == "all green", f"a) body clean, got {p.get('body')!r}")
    check(r.leases == [("[no-send]", True)], f"a) lease closed once, silently, got {r.leases}")
    check(r.left == [], f"a) nothing left queued, got {r.left}")

    r = Run("all green\n").passes()
    check(r.attempts == [] and r.leases == [("all green", False)],
          f"b) no marker: ordinary result, got {r.leases} {r.attempts}")

    r = Run(f"[channel: {ROOM}]\n[thread: {ASK}]\nall green\n").passes()
    check(len(r.attempts) == 1 and r.attempts[0].get("thread_root") == ASK
          and r.attempts[0].get("body") == "all green", f"c) task room named: threaded, got {r.attempts}")
    check(r.leases == [("[no-send]", True)], f"c) lease closed silently, got {r.leases}")

    r = Run(f"[channel: {OTHER}]\n[thread: {ASK}]\nall green\n").passes()
    check(r.attempts == [] and r.leases == [(f"[channel: {OTHER}]\nall green", False)],
          f"d) another room: ordinary redirect, marker gone, got {r.leases} {r.attempts}")
    check(r.logged("not the task's room"), "d) the dropped root is logged")

    r = Run("[thread: not-an-event]\nall green\n").passes()
    check(r.attempts == [] and r.leases == [("all green", False)],
          f"e) malformed: ordinary result, got {r.leases} {r.attempts}")
    check(r.logged("malformed"), "e) the malformed marker is logged")

    r = Run(f"[dm-only]\n[thread: {ASK}]\nall green\n").passes()
    check(r.attempts == [] and len(r.leases) == 1 and not r.leases[0][1]
          and "[thread:" not in r.leases[0][0], f"f) dm-only: ordinary result, got {r.leases}")

    r = Run(f"[thread: {ASK}]\nall green\n", lease_fails=1).passes()
    check(len(r.attempts) == 1, f"g) lease retry: still exactly one thread post, got {r.attempts}")
    check(r.leases == [("[no-send]", True)] * 2, f"g) lease close retried, got {r.leases}")
    check(r.left == [], f"g) nothing left queued, got {r.left}")

    r = Run(f"[thread: {ASK}]\nall green\n", forget_room=True).passes()
    check(r.attempts == [] and r.leases == [("all green", False)],
          f"h) room unknown: ordinary result, got {r.leases} {r.attempts}")

    r = Run(f"[thread: {ASK}]\nall green", attach=True).passes()
    check(r.attempts == [] and len(r.leases) == 1 and not r.leases[0][1],
          f"i) with an attachment: ordinary result, got {r.leases} {r.attempts}")
    check(r.logged("attachments"), "i) the reason is logged")

    r = Run(f"[thread: {ASK}]\nall green\n", signal=True).passes()
    check(r.attempts == [] and r.leases == [("all green", False)],
          f"j) Signal task: ordinary result, got {r.leases} {r.attempts}")
    check(r.logged("Signal task"), "j) the reason is logged")

    r = Run(f"[thread: {ASK}]\n").passes()
    check(r.attempts == [] and r.logged("empty"), f"k) empty body: never posted, got {r.attempts}")

    r = Run(f"[thread: {ASK}]\nall green\n", room=[http(400)]).passes(3)
    check(len(r.attempts) == 1, f"l) refused thread post tried once, got {r.attempts}")
    check(r.leases == [("all green", False)],
          f"l) the answer reaches the asker exactly once, as the ordinary result, got {r.leases}")
    check(r.left == [] and r.logged("thread post refused"), f"l) fallback logged, left {r.left}")

    r = Run(f"[thread: {ASK}]\nall green\n", room=[{"ok": True}, OK]).passes(3)
    check(len(r.attempts) == 1, f"m) accepted without event_id: sent once, never repeated, got {r.attempts}")
    check(r.leases == [("[no-send]", True)], f"m) lease closed silently, got {r.leases}")
    check(r.logged("WARNING thread post outcome unknown"), "m) the unknown outcome is logged loudly")

    for label, reply in (("502", http(502)), ("timeout", TimeoutError("read timed out")),
                         ("reset", urllib.error.URLError(ConnectionResetError("reset")))):
        r = Run(f"[thread: {ASK}]\nall green\n", room=[reply, OK]).passes(3)
        check(len(r.attempts) == 1 and r.leases == [("[no-send]", True)],
              f"n) {label}: one send, never repeated, lease closed, got {r.attempts} {r.leases}")

    r = Run(f"[thread: {ASK}]\nall green\n", room=[http(429)] * 9).passes(7)
    check(len(r.attempts) == 5, f"o) 429: retried up to the cap, got {len(r.attempts)}")
    check(r.leases == [("all green", False)], f"o) then the ordinary result, got {r.leases}")
    check(r.logged("not confirmed yet"), "o) a pending retry is logged")

    r = Run(f"[thread: {ASK}]\nall green\n",
            room=[urllib.error.URLError(ConnectionRefusedError("refused")), OK]).passes(3)
    check(len(r.attempts) == 2 and r.leases == [("[no-send]", True)],
          f"p) refused connection retried, one thread post, got {r.attempts} {r.leases}")

    r = Run(f"[thread: {ASK}]\nanswer A\n", lease_fails=1).passes(1)
    r.write(f"[thread: {ASK}]\nanswer B\n")
    r.passes(2)
    check([a.get("body") for a in r.attempts] == ["answer A", "answer B"],
          f"q) the replacement reply is posted, not archived unposted, got {r.attempts}")
    check(r.leases == [("[no-send]", True)] * 2 and r.left == [],
          f"q) lease closed after B, got {r.leases} {r.left}")

    r = Run(f"[thread: {ASK}]\nall green\n", signal=True)
    r.mod.TASK_MEDIA_FILE.write_text("{corrupt")
    r.passes(2)
    check(r.attempts == [] and r.leases == [] and r.left == [f"{r.tid}.txt"],
          f"r) media sidecar unreadable: deferred, got {r.attempts} {r.leases} {r.left}")
    r.mod.TASK_MEDIA_FILE.write_text(json.dumps(
        {r.mod._broker_tid(r.tid): {"mode": "task-media", "thread_root": ""}}))
    r.passes(1)
    check(r.attempts == [] and r.leases == [("all green", False)],
          f"r) repaired: the Signal task's ordinary result, got {r.attempts} {r.leases}")

    r = Run(f"[thread: {ASK}]\nall green\n")
    r.mod.DEDUP_ALIAS_FILE.write_text("{corrupt")
    r.passes(2)
    check(r.attempts == [] and r.leases == [] and r.logged("thread routing unreadable"),
          f"s) alias ledger unreadable: deferred, got {r.attempts} {r.leases}")

    r = Run(f"[thread: {ASK}]\nall green\n", ask_thread="$t:example.org").passes()
    check(r.attempts == [] and r.leases == [("all green", False)]
          and r.logged("already in a thread"), f"t) ask in a thread: ordinary result, got {r.attempts}")

    r = Run("[thread: $elsewhere:example.org]\nall green\n").passes()
    check(r.attempts == [] and r.leases == [("all green", False)]
          and r.logged("not the asking message"), f"u) foreign root: ordinary result, got {r.attempts}")

    r = Run(f"[thread: {ASK}]\nall green\n", task_room="not-a-matrix-room").passes()
    check(r.attempts == [] and len(r.leases) == 1 and not r.leases[0][1] and r.left == []
          and r.logged("not a Matrix room"), f"v) non-Matrix room: ordinary, got {r.leases} {r.left}")

    prose = "Use [thread: $example] literally, and [channel: !x:y] too."
    r = Run(f"[thread: {ASK}]\n{prose}\n").passes()
    check(len(r.attempts) == 1 and r.attempts[0].get("body") == prose,
          f"w) inline prose posted byte for byte, got {r.attempts}")

    r = Run(f"[thread: {ASK}]\nall green\n")
    mod = r.mod
    raw = mod._read_ready_generation(mod.RESULTS_DIR / f"{r.tid}.txt")[0]
    item = f"{mod._broker_tid(r.tid)}.thread-{mod.source_digest(raw)[:16]}"
    core = mod._thread_core()
    core.backend.publish(item, b"{}")
    from ag2_sparrow import outbox
    check(outbox._acquire_locked(core.backend.root, item, "gateway-thread-drain"), "x) claim taken")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    cp = outbox._claim_path(core.backend.root, item)
    rec = json.loads(cp.read_text())
    rec.update(pid=dead.pid, start_usec=None)
    cp.write_text(json.dumps(rec))
    r.passes(2)
    check(r.attempts == [] and r.leases == [("[no-send]", True)],
          f"x) dead sender's claim: parked unknown, never re-sent, got {r.attempts} {r.leases}")

    r = Run(f"[thread: {ASK}]\nall green\n")
    tfile = r.mod.find_task_file(r.mod.TASKS_DIR, r.tid)
    check(r.mod._task_ask(r.tid) == {"thread_root": "", "source_message_id": ASK},
          "y) the ask is read from the gateway-written task file")
    tfile.chmod(0)
    try:
        check(r.mod._task_ask(r.tid) is None, "y) an unreadable task file is not a top-level ask")
    finally:
        tfile.chmod(0o600)
    tfile.unlink()
    check(r.mod._task_ask(r.tid) == {}, "y) no task file: nothing proves the ask, ordinary path")

    r = Run(f"[thread: {ASK}]\nall green\n", room=[{"ok": True}], trust_ok=True).passes()
    check(len(r.attempts) == 1 and r.leases == [("[no-send]", True)]
          and not r.logged("WARNING"), f"z) trusted ok: confirmed, got {r.leases}")

    provider_cases()
    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


def provider_cases() -> None:
    from ag2_sparrow.delivery_core.contract import (ProviderIndeterminate, ProviderRefused,
                                                    ProviderPermanentRefused)
    from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceRoomMessageProvider
    good = json.dumps({"op": "message", "room_id": ROOM, "body": "b"}).encode()

    def outcome(reply, payload=good):
        def req(*_a, **_k):
            if isinstance(reply, BaseException):
                raise reply
            return reply
        try:
            AG2SpaceRoomMessageProvider(req).deliver("i", payload, "i#0")
            return "confirmed"
        except ProviderPermanentRefused:
            return "permanent"
        except ProviderRefused:
            return "refused"
        except ProviderIndeterminate:
            return "unknown"

    check(AG2SpaceRoomMessageProvider(lambda *a, **k: OK).reconcile(None) is None,
          "P) no read-back: reconcile answers nothing")
    for label, reply, payload, want in (
            ("malformed payload", OK, b"\xff", "permanent"),
            ("not a room message", OK, json.dumps({"op": "say"}).encode(), "permanent"),
            ("event_id", OK, good, "confirmed"),
            ("ok false", {"ok": False}, good, "permanent"),
            ("errcode", {"errcode": "M_FORBIDDEN"}, good, "permanent"),
            ("non-dict", ["x"], good, "unknown"),
            ("403", http(403), good, "refused"),
            ("404", http(404), good, "permanent"),
            ("503", http(503), good, "unknown"),
            ("dns", urllib.error.URLError(socket.gaierror("no host")), good, "refused"),
            ("os error", OSError("broken pipe"), good, "unknown")):
        got = outcome(reply, payload)
        check(got == want, f"P) {label}: {want}, got {got}")


if __name__ == "__main__":
    sys.exit(main())
