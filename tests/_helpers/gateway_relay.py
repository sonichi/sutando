"""A relay on a real socket, and bridge processes running the real poll loop against it.

    relay = Relay()
    bridges = Bridges(test, workspace, relay)
    proc = bridges.start()             # a fresh process: `main()` over the shared workspace
    bridges.wait_for(lambda: relay.acks, proc, "the ack")

Each process imports the bridge through `hermetic_gateway` and shares only the disk with
the ones before it, so a restart reads exactly what a real restart would.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTRACT = "voice-outcome-v1"
_TESTS = Path(__file__).resolve().parents[1]


class Relay:
    """Serves each queued task once. As an AG2 Space broker does, it serves an `ack_first`
    task only to a poll that carries the contract, at most one per response, and serves a
    poll without it the tasks behind that ask. It keeps each task's state for
    `GET /v1/tasks/<id>/status`. The ack of a task in `withdrawn` is refused as not leased,
    and so is any ack of a task in `lease_ended` (an acknowledged lease the broker ended),
    whose state stays as it was. Results get a 503 while `refusing`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue: "list[dict]" = []
        self.withdrawn: "set[str]" = set()
        self.lease_ended: "set[str]" = set()
        self.state: "dict[str, str]" = {}
        self.status_reads: "list[str]" = []
        self.polls: "list[str]" = []
        self.served: "list[tuple[str, str]]" = []     # (task id, the poll's query)
        self.acks: "list[str]" = []                    # broker ids, in order
        self.ack_bodies: "list[dict]" = []
        self.results: "list[dict]" = []
        self.refused = 0
        self.refusing = False
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, code: int, doc: dict) -> None:
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/v1/tasks/") and self.path.endswith("/status"):
                    tid = urllib.parse.unquote(self.path.split("/")[-2])
                    with relay.lock:
                        relay.status_reads.append(tid)
                        state = "withdrawn" if tid in relay.withdrawn else relay.state.get(tid)
                    if state is None:
                        return self._send(404, {"error": "unknown task"})
                    return self._send(200, {"id": tid, "state": state})
                if self.path.startswith("/v1/tasks?"):
                    query = urllib.parse.urlsplit(self.path).query
                    proof = f"capabilities={CONTRACT}" in query.split("&")
                    with relay.lock:
                        relay.polls.append(query)
                        tasks, relay_ask = [], False
                        for task in relay.queue:
                            if task.get("ack_first") is True and (not proof or relay_ask):
                                continue                 # it keeps its place in the queue
                            relay_ask |= task.get("ack_first") is True
                            tasks.append(task)
                        relay.queue = [t for t in relay.queue if t not in tasks]
                        relay.served.extend((t["id"], query) for t in tasks)
                        relay.state.update({t["id"]: "served" for t in tasks})
                    if not tasks:
                        time.sleep(0.2)              # a short hold, so an idle loop never spins
                    return self._send(200, {"tasks": tasks})
                self._send(200, {"agents": []} if self.path == "/v1/agents" else {})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(size)) if size else None
                code, doc = 200, {"ok": True, "delivery_readable": True}
                with relay.lock:
                    if self.path == "/v1/results":
                        if relay.refusing:
                            relay.refused += 1
                            code, doc = 503, {"error": "unavailable"}
                        else:
                            relay.results.append(payload)
                            relay.state[str((payload or {}).get("id"))] = "result_ready"
                    elif self.path.endswith("/ack"):
                        tid = urllib.parse.unquote(self.path.split("/")[-2])
                        relay.acks.append(tid)
                        relay.ack_bodies.append(payload)
                        if tid in relay.withdrawn or tid in relay.lease_ended:
                            code, doc = 404, {"error": "not leased to you"}
                        elif relay.state.get(tid) == "acknowledged":
                            doc = {"ok": True, "durable": True, "duplicate": True}
                        else:
                            relay.state[tid] = "acknowledged"
                            doc = {"ok": True, "durable": True}
                self._send(code, doc)

            do_PUT = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# One bridge process: the hermetic import over a shared workspace, an optional crash
# point (exit 17 there), then the real loop.
_BRIDGE = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _helpers.hermetic_gateway import host_reads, isolate_then_import
gw, reads = isolate_then_import(root=Path(sys.argv[2]), env={
    "REMOTE_TASK_URL": sys.argv[3], "REMOTE_TASK_POLL_WAIT": "1", "SUTANDO_BRIDGE_LOCK": "0",
    **json.loads(sys.argv[5])})
leaked = host_reads(reads)
if leaked:
    sys.exit("the bridge import left its workspace: " + "; ".join(leaked))
crash = sys.argv[4]
ack, publish, put, mark = gw._post_ack_first, gw._publish_staged, gw._put_held, gw._record_ack_first

def ack_at(tid, lease_id):
    if crash == "before-ack":
        os._exit(17)
    out = ack(tid, lease_id)
    if crash == "after-ack" and out == "accepted":
        os._exit(17)
    return out

def put_at(tid, entry):
    staging = isinstance(entry, dict) and not entry.get("accepted") and not entry.get("refused")
    if crash == "before-entry" and staging:
        os._exit(17)
    ok = put(tid, entry)
    if crash == "after-refusal-recorded" and isinstance(entry, dict) and entry.get("refused"):
        os._exit(17)
    return ok

def mark_at(tid):
    if crash == "before-mark":
        os._exit(17)
    return mark(tid)

def publish_at(tmp, path):
    queued = Path(path).parent == gw.TASKS_DIR
    if queued and crash == "before-publish":
        os._exit(17)
    ok = publish(tmp, path)
    if queued and crash == "after-publish":
        os._exit(17)
    return ok

gw._post_ack_first, gw._publish_staged, gw._put_held, gw._record_ack_first = (
    ack_at, publish_at, put_at, mark_at)
gw.main()
"""

CRASH_EXIT = 17
_FIRED = "import sys; open(sys.argv[1], 'a').write(sys.argv[2].rsplit('/', 1)[-1] + '\\n')"


class Bridges:
    """Fresh bridge processes over one workspace (`root / "ws"`), against `relay`.
    Every task a process publishes runs the wake command, which logs it in `fired.log`."""

    def __init__(self, test, root: Path, relay: Relay):
        self.test, self.root, self.relay, self.runs = test, root, relay, 0
        self.ws = root / "ws"
        self.fired_log = root / "fired.log"
        self.env = {"REMOTE_TASK_ON_TASK": " ".join(
            shlex.quote(a) for a in (sys.executable, "-c", _FIRED, str(self.fired_log)))}

    def start(self, crash: str = "") -> "subprocess.Popen":
        self.runs += 1
        log = open(self.root / f"bridge-{self.runs}.log", "w")
        self.test.addCleanup(log.close)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SUTANDO_") and k != "GATEWAY_INSTANCE"}
        proc = subprocess.Popen(
            [sys.executable, "-c", _BRIDGE, str(_TESTS), str(self.ws), self.relay.url, crash,
             json.dumps(self.env)],
            env=env, stdout=log, stderr=subprocess.STDOUT)
        self.test.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc: "subprocess.Popen") -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)

    def log(self) -> str:
        return (self.root / f"bridge-{self.runs}.log").read_text(errors="replace")[-3000:]

    def wait_for(self, done, proc: "subprocess.Popen", what: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done():
                return
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if done():
            return
        self.test.fail(f"{what}: not seen (bridge exit {proc.poll()}); its log ends:\n{self.log()}")

    def wait_logged(self, proc: "subprocess.Popen", text: str) -> None:
        """Wait for the running bridge to log `text`, so a stop cannot cut the step it ends."""
        current = self.root / f"bridge-{self.runs}.log"
        self.wait_for(lambda: text in current.read_text(errors="replace"), proc, f"the log line {text!r}")

    def refused_result_recorded(self, proc: "subprocess.Popen", broker_id: str) -> None:
        """A refused result is on record, not mid-claim: a stop during the send would leave
        a claim that only its reclaim window, longer than any wait here, releases."""
        self.wait_logged(proc, f"result POST not confirmed for {broker_id}")

    def crashed(self, proc: "subprocess.Popen", where: str) -> None:
        """The process reached its crash point and died there."""
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.stop(proc)
        self.test.assertEqual(proc.returncode, CRASH_EXIT,
                              f"the bridge never reached {where}; its log ends:\n{self.log()}")

    def polls_after(self, proc: "subprocess.Popen", n: int = 2) -> None:
        """Let a running bridge finish `n` more loop passes."""
        seen = len(self.relay.polls)
        self.wait_for(lambda: len(self.relay.polls) >= seen + n, proc, f"{n} more polls")

    def fired(self) -> "list[str]":
        """The task files the wake command was run for, in order."""
        try:
            return self.fired_log.read_text().split()
        except FileNotFoundError:
            return []
