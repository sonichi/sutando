#!/usr/bin/env python3
"""E2E test for token auth on GET /result/<id> on agent-api (T2.6) — run
against a REAL HTTP server on an ephemeral port with the module's dirs
patched to a temp workspace.

Result bodies are owner data, so the poll leg is gated exactly like the write
leg it belongs to (POST /task): bearer-checked when SUTANDO_API_TOKEN is
configured, open when it is not (the documented local-dev default). This is
deliberately NOT the /delegation/* posture, which refuses outright with no
token configured — that stricter shape is for full-capability remote
delegation, and the same local clients that POST /task poll /result.

Covers: token configured — missing bearer is 401, wrong bearer is 401, the
result body does not leak on either, correct bearer reads completed/pending/
404 as before; no token configured — every one of those is unchanged; and
POST /task's own gate is untouched in both modes.

Run: python3 tests/agent-api-result-auth.test.py
"""
import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")

tmp = Path(tempfile.mkdtemp(prefix="result-auth-e2e-"))
api.TASK_DIR = tmp / "tasks"
api.RESULT_DIR = tmp / "results"
api.TASK_DIR.mkdir()
api.RESULT_DIR.mkdir()
api.API_TOKEN = "test-token-123"

SECRET_BODY = "the owner's private answer\n"
# /result serves the READY body, which read_ready_result strips -- that strip
# IS the emptiness test, so it cannot be bypassed without re-forking the owner.
SECRET_READ = SECRET_BODY.strip()
(api.RESULT_DIR / "task-owner-1.txt").write_text(SECRET_BODY)
(api.TASK_DIR / "task-pending-1.txt").write_text("id: task-pending-1\ntask: still running\n")

# Handler on the MAIN thread, requests from a worker: inverted on purpose, the
# coverage tracer misses handler-THREAD execution so dispatch would read as 0.
server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
port = server.server_address[1]
BASE = f"http://127.0.0.1:{port}"

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _raw_req(method, path, body=None, token="test-token-123"):
    r = urllib.request.Request(f"{BASE}{path}", method=method,
                               data=None if body is None else json.dumps(body).encode())
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        # The socket may close right after an error response (ECONNRESET), and
        # only the status is asserted — so the body read is best-effort.
        try:
            payload = e.read().decode()
        except Exception:
            payload = ""
        return e.code, payload
    except Exception as e:  # connection-level failure — surface it as a check failure
        return -1, repr(e)


def req(method, path, body=None, token="test-token-123"):
    """Issue the request from a worker thread while the MAIN thread serves it."""
    out = {}
    t = threading.Thread(target=lambda: out.update(
        zip(("code", "raw"), _raw_req(method, path, body, token))), daemon=True)
    t.start()
    while t.is_alive():
        server.handle_request()   # serve on the main thread (traced)
    t.join()
    try:
        data = json.loads(out["raw"] or "{}")
    except ValueError:
        data = {}
    return out["code"], data, out["raw"]


# ── 1. Token configured: /result demands the bearer ──────────────────────────
code, data, raw = req("GET", "/result/task-owner-1", token=None)
check("no bearer on /result rejected (401)", code == 401, str(data))
check("401 body does not leak the result", SECRET_BODY.strip() not in raw, raw[:120])

code, data, raw = req("GET", "/result/task-owner-1", token="wrong")
check("wrong bearer on /result rejected (401)", code == 401, str(data))
check("wrong-bearer body does not leak the result", SECRET_BODY.strip() not in raw, raw[:120])

# ── 2. Token configured: the authenticated poll is unchanged ─────────────────
code, data, _ = req("GET", "/result/task-owner-1")
check("authenticated /result returns the completed result",
      code == 200 and data.get("status") == "completed" and data.get("result") == SECRET_READ,
      str(data))

code, data, _ = req("GET", "/result/task-pending-1")
check("authenticated /result reports a pending task",
      code == 200 and data.get("status") == "pending", str(data))

code, data, _ = req("GET", "/result/task-nonexistent-99999")
check("authenticated /result 404s an unknown id", code == 404, str(data))

# ── 3. Token configured: POST /task's existing behaviour is untouched ────────
code, data, _ = req("POST", "/task", {"from": "agent-2", "task": "research the thing"}, token=None)
check("POST /task still rejects a missing bearer (401)", code == 401, str(data))
check("rejected POST /task wrote no task file",
      not [p for p in api.TASK_DIR.glob("task-*.txt") if "research the thing" in p.read_text()])

code, data, _ = req("POST", "/task", {"from": "agent-2", "task": "research the thing"})
check("authenticated POST /task still accepted",
      code == 200 and data.get("ok") is True, str(data))
submitted_id = data.get("task_id", "")
check("POST /task wrote a source: api task file",
      bool(submitted_id) and "source: api" in (api.TASK_DIR / f"{submitted_id}.txt").read_text())
# The advertised result_url is now on the same gate as the submit that minted it.
code, data, _ = req("GET", data.get("result_url", "/result/missing"))
check("the advertised result_url polls under the same token",
      code == 200 and data.get("status") == "pending", str(data))

# ── 4. No token configured: the local-dev default is unchanged ───────────────
api.API_TOKEN = ""

code, data, _ = req("GET", "/result/task-owner-1", token=None)
check("tokenless core still serves /result unauthenticated",
      code == 200 and data.get("result") == SECRET_READ, str(data))

code, data, _ = req("GET", "/result/task-pending-1", token=None)
check("tokenless core still reports pending", code == 200 and data.get("status") == "pending", str(data))

code, data, _ = req("GET", "/result/task-nonexistent-99999", token=None)
check("tokenless core still 404s an unknown id", code == 404, str(data))

code, data, _ = req("POST", "/task", {"from": "agent-2", "task": "tokenless submit"}, token=None)
check("tokenless core still accepts POST /task",
      code == 200 and data.get("ok") is True, str(data))
code, data, _ = req("GET", data.get("result_url", "/result/missing"), token=None)
check("tokenless submit → poll round-trip unchanged",
      code == 200 and data.get("status") == "pending", str(data))

server.server_close()

if failures:
    sys.exit(1)
print("PASS — GET /result token auth")
