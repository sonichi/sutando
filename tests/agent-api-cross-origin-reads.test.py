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
from _helpers.paired_result import publish_result

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
publish_result(WS / "results" / "task-1.txt", "private reply\n")

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

    # f) the same paths in-process: CI's coverage traces the main thread only (concurrency =
    #    multiprocessing), so the server thread above proves behaviour and this measures it
    import io
    from unittest import mock

    def bare(method, path, headers, body=b""):
        h = api.Handler.__new__(api.Handler)
        h.path, h.headers, h.rfile, h.wfile = path, dict(headers), io.BytesIO(body), io.BytesIO()
        h.server = type("S", (), {"server_address": ("127.0.0.1", PORT)})()
        sent = []
        h.send_response = lambda code, msg=None: sent.append(("status", code))
        h.send_header = lambda k, v: sent.append((k, v))
        h.end_headers = lambda: None
        getattr(h, "do_" + method)()
        return dict(sent), h

    rebound = {"Host": f"evil.example:{PORT}", "Origin": f"http://evil.example:{PORT}"}
    sent, _h = bare("GET", "/ping", {"Host": HOST, "Origin": "tauri://localhost"})
    check(sent.get("Access-Control-Allow-Origin") == "tauri://localhost", "f) in-process: the desktop origin is echoed")
    for method, path in (("GET", "/ping"), ("OPTIONS", "/tasks/active"), ("DELETE", "/x")):
        sent, _h = bare(method, path, rebound)
        check(sent.get("status") == 403, f"f) in-process: {method} under a rebinding Host is 403")
    sent, h = bare("POST", "/task", {**rebound, "Content-Length": "5"}, b"hello")
    check(sent.get("status") == 403 and h.rfile.tell() == 5, "f) in-process: POST /task refused after reading its body")
    sent, _h = bare("POST", "/task", {**rebound, "Content-Length": "junk"})
    check(sent.get("status") == 403, "f) in-process: a malformed Content-Length is still a refusal")
    h = api.Handler.__new__(api.Handler)
    h.path, h.headers = "/twilio/voice", dict(rebound)
    check(h.host_allowed() is True, "f) in-process: Twilio's tunnel Host is exempt")
    check(h._bound_port() is None, "f) a handler with no server has no bound port")
    sent, _h = bare("GET", "/media/results/media/pic.png", {"Host": HOST, "Origin": "tauri://localhost"})
    check(sent.get("status") == 200 and sent.get("Access-Control-Allow-Origin") == "tauri://localhost",
          "f) in-process: media carries the echoed grant")
    avatar = WS / "avatar.png"
    avatar.write_bytes(b"\x89PNG")
    with mock.patch.object(api, "personal_path", lambda *a, **k: avatar):
        sent, _h = bare("GET", "/avatar", {"Host": HOST, "Origin": "tauri://localhost"})
    check(sent.get("status") == 200 and sent.get("Access-Control-Allow-Origin") == "tauri://localhost",
          "f) in-process: the avatar carries the echoed grant")
    with mock.patch.object(api, "config_get", lambda key, default=None: "not-a-port"):
        check(api._port_setting("CLIENT_PORT", 8080) == 8080, "f) a malformed CLIENT_PORT falls back to 8080")
    check(gate.cors_origin("tauri://localhost", HOST) == "tauri://localhost", "gate: a loopback origin is echoed")
    check(gate.cors_origin("http://192.168.1.20:9999", "192.168.1.20:7843", {7843}) == "",
          "gate: the same host on another port gets no grant")
    check(gate.cors_origin("http://evil.example", "192.168.1.20:7843", {7843}) == "", "gate: another host gets none")
    check(gate.rebinding_safe_host("10.0.0.1:7843") and not gate.rebinding_safe_host("evil.example:7843"),
          "gate: an IP literal is safe, a foreign name is not")

    server.shutdown()
    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}")
        return 1
    print("\nPASS: no foreign site is granted a read of agent-api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
