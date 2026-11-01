#!/usr/bin/env python3
"""agent-api never grants a cross-origin read to a foreign site.

A REAL HTTP server on an ephemeral port, the workspace a temp dir. Contract:
  a) no response carries `Access-Control-Allow-Origin: *`; a foreign Origin gets no grant at all
     (JSON routes, /media, /avatar, the OPTIONS preflight)
  b) a loopback page, the desktop app's origins and a page served from the same host get their
     Origin echoed, with Vary: Origin
  c) responses still reach callers that need no grant (same-origin, <img>, local tools)
  d) DNS rebinding is refused: a Host that is not loopback, an IP literal or declared gets 403
     on every route (reads and POST /task), unless the request carries a valid bearer

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
    check(req("/ping", origin="http://192.168.1.20:9999", host=f"192.168.1.20:{PORT}")[1] is None,
          "b) another service on the same host (another port) is not")
    for origin in ("null", "http://127.0.0.1@evil.com", "http://localhost.evil.com", "http://127.0.0.1.evil.com"):
        check(req("/ping", origin=origin)[1] is None, f"b) {origin} gets no grant")

    # d) DNS rebinding: the Host is the attacker's name, so the request is refused
    rebound = f"evil.example:{PORT}"
    for path in ("/ping", "/tasks/active", "/activity", "/media/results/media/pic.png"):
        status, acao, _v, body = req(path, origin=f"http://{rebound}", host=rebound)
        check(status == 403 and b"host not allowed" in body, f"d) {path} under a rebinding Host: 403 ({status})")
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request("POST", "/task", body="hello", headers={"Host": rebound, "Origin": f"http://{rebound}",
                                                       "Content-Type": "text/plain"})
    r = c.getresponse()
    r.read()
    c.close()
    check(r.status == 403, f"d) POST /task under a rebinding Host: 403 ({r.status})")
    check(req("/ping", host=rebound, bearer=api.connections_bearer())[0] == 200,
          "d) a valid bearer is not a rebinding page: allowed")
    check(req("/ping", host=f"192.168.1.20:{PORT}")[0] == 200, "d) an IP-literal LAN Host: allowed")
    check(req("/ping", host=f"[::1]:{PORT}")[0] == 200, "d) [::1]: allowed")
    api.ALLOWED_HOSTS = "my-mac.lan"
    try:
        check(req("/ping", host=f"my-mac.lan:{PORT}")[0] == 200, "d) a declared LAN name: allowed")
    finally:
        api.ALLOWED_HOSTS = ""
    check(req("/ping", host=f"my-mac.lan:{PORT}")[0] == 403, "d) an undeclared name: 403")
    check(gate.rebinding_safe_host("") and gate.rebinding_safe_host("app.localhost:1"),
          "d) no Host (no browser) and *.localhost are safe")

    # the response still reaches same-origin callers; only the grant is withheld
    status, acao, _v, body = req("/media/results/media/pic.png")
    check(status == 200 and body.startswith(b"\x89PNG"), f"c) media still served to a same-origin caller ({status})")

    # the predicate itself
    check(gate.cors_origin(None, HOST) == "" and gate.cors_origin("", HOST) == "", "gate: no Origin, no grant")
    check(gate.cors_origin("http://[::1", HOST) == "", "gate: a malformed Origin gets no grant")

    server.shutdown()
    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}")
        return 1
    print("\nPASS: no foreign site is granted a read of agent-api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
