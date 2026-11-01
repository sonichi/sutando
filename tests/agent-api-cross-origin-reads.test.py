#!/usr/bin/env python3
"""agent-api never grants a cross-origin read to a foreign site, and its loopback state is behind the bearer.

A REAL HTTP server on an ephemeral port, the workspace a temp dir. Contract:
  a) no response carries `Access-Control-Allow-Origin: *`; a foreign Origin gets no grant at all
     (JSON routes, /media, /avatar, the OPTIONS preflight)
  b) a loopback page, the desktop app's origins and a page served from the same host get their
     Origin echoed, with Vary: Origin
  c) /media/state/*, /media/tasks/*, /media/logs/* and any text-like file need the local API
     bearer: 401 without it (before any existence check), 403 for a foreign Origin even with it,
     200 with it from the desktop app's origin
  d) embeddable media in results/, data/ and notes/ needs no bearer (an <img> cannot send one)

Run: python3 tests/agent-api-cross-origin-reads.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

import http.client
import http.server
import importlib.util
import sys
import tempfile
import threading
from pathlib import Path

_WS = tempfile.mkdtemp(prefix="agent-api-cors-")
isolate_os_environ(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=_WS, SUTANDO_TELEMETRY="0",
                   SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
import local_api_gate as gate  # noqa: E402

WS = Path(_WS)
assert api.WORKSPACE_DIR == WS.resolve(), api.WORKSPACE_DIR
api.API_TOKEN = ""
(WS / "state").mkdir(parents=True, exist_ok=True)
(WS / "state" / "agent-activity.jsonl").write_text('{"kind": "working", "line": "private task text"}\n')
(WS / "tasks").mkdir(exist_ok=True)
(WS / "tasks" / "task-1.txt").write_text("task: private\n")
(WS / "results" / "media").mkdir(parents=True, exist_ok=True)
(WS / "results" / "media" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
(WS / "results" / "task-1.txt").write_text("private reply\n")
TOKEN = api.connections_bearer()

server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
HOST = f"127.0.0.1:{PORT}"
FOREIGN = "https://evil.example"
DESKTOP = ("tauri://localhost", "http://tauri.localhost", "http://localhost:1420")

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def req(path: str, origin: "str | None" = None, bearer: "str | None" = None, method: str = "GET",
        host: str = HOST):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    c.request(method, path, headers=headers)
    r = c.getresponse()
    body = r.read()
    out = (r.status, r.getheader("Access-Control-Allow-Origin"), r.getheader("Vary"), body)
    c.close()
    return out


def main() -> int:
    # a) a foreign site gets no grant anywhere, and nothing is ever `*`
    for path, method in (("/ping", "GET"), ("/tasks/active", "GET"), ("/activity", "GET"), ("/avatar", "GET"),
                         ("/media/results/media/pic.png", "GET"), ("/media/state/agent-activity.jsonl", "GET"),
                         ("/media/state/agent-activity.jsonl", "OPTIONS"), ("/tasks/active", "OPTIONS")):
        status, acao, _vary, _body = req(path, origin=FOREIGN, method=method)
        check(acao is None, f"a) {method} {path} from a foreign origin: no grant (status {status}, ACAO {acao!r})")
    for path in ("/ping", "/media/results/media/pic.png", "/tasks/active"):
        check(req(path)[1] != "*", f"a) {path} with no Origin: never a wildcard")

    # b) loopback, the desktop app and a same-host page are echoed
    for origin in DESKTOP + (f"http://127.0.0.1:{PORT}",):
        status, acao, vary, _ = req("/ping", origin=origin)
        check(acao == origin and "Origin" in (vary or ""), f"b) {origin} echoed with Vary: Origin ({acao!r})")
    check(req("/ping", origin="http://192.168.1.20:8080", host=f"192.168.1.20:{PORT}")[1] == "http://192.168.1.20:8080",
          "b) a page served from the same LAN host is echoed")
    check(req("/ping", origin="http://evil.example", host=f"192.168.1.20:{PORT}")[1] is None,
          "b) a different host is not")

    # c) state, tasks and text need the bearer; the refusal comes before any existence check
    for path in ("/media/state/agent-activity.jsonl", "/media/tasks/task-1.txt", "/media/results/task-1.txt",
                 "/media/state/missing.json"):
        status, acao, _v, body = req(path)
        check(status == 401 and b"private" not in body, f"c) {path} without the bearer: 401 ({status})")
        status, acao, _v, body = req(path, origin=FOREIGN, bearer=TOKEN)
        check(status == 403 and acao is None and b"private" not in body,
              f"c) {path} from a foreign origin with the bearer: 403, no grant ({status})")
    status, acao, _v, body = req("/media/state/agent-activity.jsonl", origin="tauri://localhost", bearer=TOKEN)
    check(status == 200 and acao == "tauri://localhost" and b"private task text" in body,
          f"c) the desktop app with the bearer reads its activity rows ({status})")
    check(req("/media/state/missing.json", bearer=TOKEN)[0] == 404, "c) with the bearer, a missing file is 404")
    check(req("/media/state/agent-activity.jsonl", bearer="wrong")[0] == 401, "c) a wrong bearer is 401")

    # d) embeddable media in a shared folder needs no bearer
    status, acao, _v, body = req("/media/results/media/pic.png")
    check(status == 200 and body.startswith(b"\x89PNG"), f"d) an image in results/ embeds without the bearer ({status})")

    # the predicate itself
    check(gate.cors_origin(None, HOST) == "" and gate.cors_origin("", HOST) == "", "gate: no Origin, no grant")
    check(gate.cors_origin("http://[::1", HOST) == "", "gate: a malformed Origin gets no grant")

    server.shutdown()
    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}")
        return 1
    print("\nPASS: no foreign site can read agent-api; loopback state needs the bearer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
