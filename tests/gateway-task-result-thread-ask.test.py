#!/usr/bin/env python3
"""A bare `[thread]` on a task result asks the broker to thread the answer on the ask.

The gateway's ordinary task-result POST adds `"thread": "ask"`; the broker roots
the thread on the task's own asking message (no event id is ever sent).

  a) owner result with [thread]        -> one POST, thread "ask", body clean
  b) no marker                         -> payload byte-identical to today, no thread key
  c) team result, [thread] + a secret  -> withheld: lease closed [no-send], no thread, no secret
  d) worker attribution                -> metadata.worker_id rides with thread "ask";
                                          refused attribution -> no POST at all
  e) [thread] + [file:]                -> upload kept, marker stripped, thread "ask"
  f) [channel:] + [thread]             -> redirect re-stitched as today, no thread field
  g) broker 400 on the thread field    -> re-posted once without it, delivered, archived
  h) proactive file with [thread]      -> stripped, posted top level (task results only)
  i) `[thread]` not alone on its line  -> prose: body untouched, no thread field

Loads src/remote-gateway-bridge.py in-process (its real PROACTIVE_CLAIM_GATE),
with an isolated workspace and a fake `_req`; never runs the wrapper as a process.
Run: python3 tests/gateway-task-result-thread-ask.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.error
from urllib.parse import quote
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "remote-gateway-bridge.py"
AGENT = "@agent:example.org"
OWNER_DM = "!dm:example.org"
ROOM = "!taskroom:example.org"
OTHER = "!other:example.org"
WORKER = "0123456789abcdef0123456789abcdef"
SECRET = "ghp_" + "a" * 36
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class Harness:
    def __init__(self, refuse_thread_400: bool = False):
        self.ws = Path(tempfile.mkdtemp(prefix="gw-thread-ask-ws-"))
        cfg = Path(tempfile.mkdtemp(prefix="gw-thread-ask-cfg-"))
        for d in ("tasks", "results", "state", "logs"):
            (self.ws / d).mkdir()
        os.environ.update({
            "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
            "CLAUDE_CONFIG_DIR": str(cfg), "AG2_DEVICE_ENV": "",
            "REMOTE_TASK_URL": "http://127.0.0.1:9", "REMOTE_TASK_TOKEN": "t",
            "REMOTE_PROACTIVE_ROOM": "", "AGENT_MXID": AGENT,
            "DO_NOT_TRACK": "1", "SUTANDO_TELEMETRY": "0"})
        os.environ.pop("GATEWAY_INSTANCE", None)
        name = f"rgb_thread_ask_{time.monotonic_ns()}"
        spec = importlib.util.spec_from_file_location(name, _SRC)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod
        self.results: list[dict] = []   # every /v1/results attempt, in order
        self.calls: list[tuple] = []
        self.logs: list[str] = []
        self.refuse_thread_400 = refuse_thread_400

        def fake_req(method, path, payload=None, timeout=None):
            self.calls.append((method, path, payload))
            if path == "/v1/agents":
                return {"agents": [{"id": AGENT, "owner": "@o:example.org",
                                    "owner_dm_room": OWNER_DM}]}
            if path == "/v1/results":
                self.results.append(json.loads(json.dumps(payload)))
                if self.refuse_thread_400 and "thread" in (payload or {}):
                    raise urllib.error.HTTPError(
                        "http://127.0.0.1:9/v1/results", 400,
                        'invalid thread: expected "ask", false or absent', None, None)
                return {"ok": True}
            return {"ok": True, "event_id": "$evt"}

        mod._req = fake_req
        mod._log = lambda msg, *_a, **_k: self.logs.append(str(msg))
        mod._reenroll_identity = lambda: AGENT
        mod._route_withheld_review = lambda _artifact: True
        mod._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=time.time() + 3600)
        self.gate_is_production = mod.PROACTIVE_CLAIM_GATE is mod._ag2space_proactive_claim_gate

    def task(self, tid: str, tier: str = "owner") -> None:
        (self.mod.TASKS_DIR / f"{tid}.txt").write_text(
            f"id: {tid}\nsource: ag2space\nchannel_id: {ROOM}\n"
            f"source_message_id: $ask:example.org\nuser_id: @u:example.org\n"
            f"access_tier: {tier}\ntask: please check\n", encoding="utf-8")

    def run(self, tid: str, body: str, tier: str = "owner") -> set:
        self.task(tid, tier)
        (self.mod.RESULTS_DIR / f"{tid}.txt").write_text(body, encoding="utf-8")
        inflight = {tid}
        self.mod._post_ready_results(inflight)
        return inflight

    def archived(self, tid: str) -> bool:
        return not (self.mod.RESULTS_DIR / f"{tid}.txt").exists() and any(
            self.mod.ARCHIVE_RESULTS_DIR.rglob(f"{tid}*.txt"))


def main() -> int:
    h = Harness()
    check(h.gate_is_production, "the loader's production claim gate is installed")

    # a) the marker sets the field
    left = h.run("tt-a", "[thread]\nall green\n")
    p = h.results[0] if h.results else {}
    check(len(h.results) == 1, f"a) one POST, got {len(h.results)}")
    check(p.get("thread") == "ask", f"a) thread 'ask' sent, got {p.get('thread')!r}")
    check(p.get("body") == "all green", f"a) body clean, got {p.get('body')!r}")
    check(not any(k in p for k in ("thread_root", "reply_to", "event_id")),
          f"a) no event id is ever named, got keys {sorted(p)}")
    check(left == set() and h.archived("tt-a"), "a) archived, out of flight")

    # b) no marker: byte-identical to today's payload
    h = Harness()
    h.run("tt-b", "all green\n")
    p = h.results[0] if h.results else {}
    check("thread" not in p, f"b) no thread key, got {p}")
    check(json.dumps(p) == json.dumps({"id": "tt-b", "body": "all green"}),
          f"b) payload byte-identical to today, got {json.dumps(p)}")

    # c) the guard runs first: a team result with the marker and a secret is withheld
    h = Harness()
    h.run("tt-c", f"[thread]\nhere is the key {SECRET}\n", tier="team")
    check(len(h.results) == 1, f"c) exactly the lease-close POST, got {len(h.results)}")
    check(all("thread" not in r for r in h.results), f"c) no thread post, got {h.results}")
    check(all(r.get("no_send") is True for r in h.results), f"c) lease closed no_send, got {h.results}")
    check(SECRET not in json.dumps(h.results), "c) the withheld text never reaches the wire")

    # d) attribution runs before the post and rides alongside the field
    h = Harness()
    (h.mod._STATE / "attribution").mkdir(parents=True, exist_ok=True)
    (h.mod._STATE / "attribution" / "tt-d").write_text(WORKER)
    h.run("tt-d", "[thread]\nworker answer\n")
    p = h.results[0] if h.results else {}
    check(p.get("thread") == "ask" and (p.get("metadata") or {}).get("worker_id") == WORKER,
          f"d) worker attribution kept with thread 'ask', got {p}")
    h = Harness()
    deliveries = h.mod._STATE.parent / "deliveries" / WORKER
    deliveries.mkdir(parents=True)
    (deliveries / "tt-d2.txt").write_text("x")
    left = h.run("tt-d2", "[thread]\nunattributed worker answer\n")
    check(h.results == [], f"d) refused attribution: no POST at all, got {h.results}")
    check(not (h.mod.RESULTS_DIR / "tt-d2.txt").exists()
          and any(h.mod.UNDELIVERABLE_RESULTS_DIR.rglob("tt-d2*")),
          "d) refused attribution: quarantined, not published")

    # e) attachments keep working
    h = Harness()
    fd, fpath = tempfile.mkstemp(prefix="sutando-thread-ask-", suffix=".txt", dir="/tmp")
    os.write(fd, b"payload")
    os.close(fd)
    try:
        h.mod._record_task_room("tt-e", ROOM)
        h.run("tt-e", f"[thread]\nhere you go [file: {fpath}]\n")
    finally:
        os.unlink(fpath)
    media = [c for c in h.calls if "/media" in c[1]]
    p = h.results[0] if h.results else {}
    check(len(media) == 1 and quote(ROOM, safe="") in media[0][1], f"e) one upload to the task room, got {[c[1] for c in media]}")
    check(p.get("thread") == "ask" and p.get("body") == "here you go",
          f"e) thread 'ask', file marker stripped, got {p}")

    # f) [channel:] is re-stitched exactly as today
    h = Harness()
    h.run("tt-f", f"[channel: {OTHER}]\n[thread]\nmoved\n")
    p = h.results[0] if h.results else {}
    check(p.get("body") == f"[channel: {OTHER}]\nmoved", f"f) redirect re-stitched, body clean, got {p.get('body')!r}")
    check("thread" not in p, f"f) a redirected answer never asks for a thread, got {p}")

    # g) a 400 for the field re-posts without it instead of parking the answer
    h = Harness(refuse_thread_400=True)
    left = h.run("tt-g", "[thread]\nall green\n")
    check(len(h.results) == 2, f"g) two attempts, got {len(h.results)}")
    check(h.results[:1] and h.results[0].get("thread") == "ask", "g) first attempt asked for the thread")
    check(len(h.results) == 2 and json.dumps(h.results[1]) == json.dumps({"id": "tt-g", "body": "all green"}),
          f"g) re-posted without the field, got {h.results[1:]}")
    check(left == set() and h.archived("tt-g"), "g) delivered and archived, not parked")
    check(not any(h.mod.UNDELIVERABLE_RESULTS_DIR.rglob("tt-g*")), "g) nothing quarantined")

    # h) valid only on a task result: a proactive file posts top level, marker stripped
    h = Harness()
    (h.ws / "state" / "last-owner-activity.json").write_text(json.dumps(
        {"ts": int(time.time()), "channel": "ag2space", "summary": "t"}))
    (h.mod.RESULTS_DIR / "proactive-1.txt").write_text(f"[channel: {ROOM}]\n[thread]\nnudge\n")
    h.mod._post_proactive()
    posts = [c[2] for c in h.calls if c[1] == "/v1/room" and (c[2] or {}).get("op") == "message"]
    p = posts[0] if posts else {}
    check(len(posts) == 1 and p.get("body") == "nudge" and "thread_root" not in p
          and "thread" not in p, f"h) proactive: top level, body clean, got {posts}")

    # i) the marker is the whole line; anything else on it is prose
    for n, text in enumerate(("[thread]ing is a library primitive", "[thread] prose on one line")):
        h = Harness()
        h.run(f"tt-i{n}", text + "\n")
        p = h.results[0] if h.results else {}
        check(p.get("body") == text and "thread" not in p, f"i) {text!r} delivered verbatim, got {p}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
