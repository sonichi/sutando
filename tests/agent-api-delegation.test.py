#!/usr/bin/env python3
"""E2E test for the TaskDelegationService relay endpoints on agent-api
(step 4 / #1947): POST /delegation/tasks, GET /delegation/results[/name],
POST /delegation/archive — run against a REAL HTTP server on an ephemeral
port with the module's dirs patched to a temp workspace.

Covers: bearer enforcement (403 with no token configured, 401 wrong token),
submit → file lands byte-identical, list/read round-trip, archive moves to
the month-partitioned layout, id/name validation rejects traversal.

Run: python3 tests/agent-api-delegation.test.py
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

tmp = Path(tempfile.mkdtemp(prefix="delegation-e2e-"))
api.TASK_DIR = tmp / "tasks"
api.RESULT_DIR = tmp / "results"
api.TASK_DIR.mkdir()
api.RESULT_DIR.mkdir()
api.API_TOKEN = "test-token-123"

# Handler runs on the MAIN thread (plain HTTPServer + handle_request loop);
# requests are issued from a worker thread. Inverted on purpose: the coverage
# gate's tracer misses handler-THREAD execution, so serving on the main
# thread is what makes the dispatch lines measurable.
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
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        # The server may close the socket right after an error response;
        # reading the body can hit ECONNRESET — the status code is what
        # the assertions need, so treat the body as best-effort.
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload
    except Exception as e:  # connection-level failure — surface it as a check failure
        return -1, {"error": repr(e)}


def req(method, path, body=None, token="test-token-123"):
    """Issue the request from a worker thread while the MAIN thread serves it."""
    out = {}
    t = threading.Thread(target=lambda: out.update(
        zip(("code", "data"), _raw_req(method, path, body, token))), daemon=True)
    t.start()
    while t.is_alive():
        server.handle_request()   # serve on the main thread (traced)
    t.join()
    return out["code"], out["data"]


CONTENT = ("id: task-e2e-1\ntimestamp: 2026-07-07T00:00:00Z\nsource: voice\n"
           "interaction_type: realtime_audio\nchannel_id: local-voice\n"
           "user_id: o\naccess_tier: owner\npriority: urgent\ntask: hello world\n")

# 1. Auth: wrong token → 401; valid token path works below.
code, _ = req("POST", "/delegation/tasks", {"id": "task-e2e-1", "content": CONTENT}, token="wrong")
check("wrong bearer rejected (401)", code == 401)

# 2. Submit lands byte-identical.
code, data = req("POST", "/delegation/tasks", {"id": "task-e2e-1", "content": CONTENT})
check("submit accepted", code == 200 and data.get("ok") is True, str(data))
check("task file byte-identical",
      (api.TASK_DIR / "task-e2e-1.txt").read_text() == CONTENT)

# 3. Traversal / malformed ids rejected.
for bad in ("../evil", "task-a/b", "", "task-" + "x" * 200):
    code, _ = req("POST", "/delegation/tasks", {"id": bad, "content": "x"})
    check(f"bad id rejected: {bad[:16]!r}", code == 400)

# 4. Results list/read round-trip.
(api.RESULT_DIR / "task-e2e-1.txt").write_text("the answer\n")
(api.RESULT_DIR / "task-other.txt").write_text("not ours\n")
code, data = req("GET", "/delegation/results")
check("list results", code == 200 and set(data.get("files", [])) == {"task-e2e-1.txt", "task-other.txt"}, str(data))
code, data = req("GET", "/delegation/results/task-e2e-1.txt")
check("read result body", code == 200 and data.get("body") == "the answer\n", str(data))
code, _ = req("GET", "/delegation/results/..%2F..%2Fetc")
check("traversal name 404s", code == 404)

# 5. Archive moves to the month-partitioned layout.
code, data = req("POST", "/delegation/archive", {"name": "task-e2e-1.txt", "task_id": "task-e2e-1"})
archived = list((api.RESULT_DIR / "archive").glob("*/task-e2e-1.txt"))
check("archive accepted + moved", code == 200 and len(archived) == 1
      and not (api.RESULT_DIR / "task-e2e-1.txt").exists(), str(data))
check("archive is month-partitioned", bool(archived) and
      archived[0].parent.name.count("-") == 1 and len(archived[0].parent.name) == 7)

# 5b. Wrong bearer on every route class (exercises each check_auth branch).
code, _ = req("GET", "/delegation/results", token="wrong")
check("wrong bearer on list (401)", code == 401)
code, _ = req("GET", "/delegation/results/task-other.txt", token="wrong")
check("wrong bearer on read (401)", code == 401)
code, _ = req("POST", "/delegation/archive", {"name": "x.txt", "task_id": "task-x"}, token="wrong")
check("wrong bearer on archive (401)", code == 401)

# 5c. Malformed (non-JSON) POST bodies → 400 via the except branch.
def raw_post(path, raw, token="test-token-123"):
    def go():
        r = urllib.request.Request(f"{BASE}{path}", method="POST", data=raw)
        r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                out["code"] = resp.status
        except urllib.error.HTTPError as e:
            out["code"] = e.code
        except Exception as e:
            out["code"] = -1
            out["err"] = repr(e)
    out = {}
    t = threading.Thread(target=go, daemon=True)
    t.start()
    while t.is_alive():
        server.handle_request()
    t.join()
    return out["code"]

check("malformed JSON to submit (400)", raw_post("/delegation/tasks", b"not json{") == 400)
check("malformed JSON to archive (400)", raw_post("/delegation/archive", b"],![") == 400)

# 6. No-token-configured core refuses delegation entirely (403), every route.
api.API_TOKEN = ""
code, data = req("POST", "/delegation/tasks", {"id": "task-e2e-2", "content": "x"}, token=None)
check("tokenless core refuses delegation (403)", code == 403, str(data))
code, _ = req("GET", "/delegation/results", token=None)
check("tokenless core refuses list (403)", code == 403)
code, _ = req("GET", "/delegation/results/task-other.txt", token=None)
check("tokenless core refuses read (403)", code == 403)
code, _ = req("POST", "/delegation/archive", {"name": "x.txt", "task_id": "task-x"}, token=None)
check("tokenless core refuses archive (403)", code == 403)

server.server_close()

# ── Direct route-body calls (main thread) ────────────────────────────────────
# The HTTP layer above proves dispatch + auth; these direct calls prove the
# route bodies themselves AND give the coverage gate main-thread attribution
# (its tracer misses handler-thread execution).
code, data = api.delegation_submit_task({"id": "task-direct-1", "content": CONTENT})
check("direct submit", code == 200 and (api.TASK_DIR / "task-direct-1.txt").read_text() == CONTENT)
check("direct submit rejects bad id", api.delegation_submit_task({"id": "../x", "content": "y"})[0] == 400)
check("direct submit rejects empty content", api.delegation_submit_task({"id": "task-d2", "content": ""})[0] == 400)
(api.RESULT_DIR / "task-direct-1.txt").write_text("direct answer\n")
code, data = api.delegation_list_results()
check("direct list", code == 200 and "task-direct-1.txt" in data["files"])
code, data = api.delegation_read_result("task-direct-1.txt")
check("direct read", code == 200 and data["body"] == "direct answer\n")
check("direct read 404", api.delegation_read_result("nope.txt")[0] == 404)
check("direct read traversal", api.delegation_read_result("../../etc")[0] == 404)
code, data = api.delegation_archive_result({"name": "task-direct-1.txt", "task_id": "task-direct-1"})
check("direct archive", code == 200 and list((api.RESULT_DIR / "archive").glob("*/task-direct-1.txt")))
check("direct archive already-gone", api.delegation_archive_result(
    {"name": "task-direct-1.txt", "task_id": "task-direct-1"})[1].get("note") == "already gone")
check("direct archive bad tid", api.delegation_archive_result(
    {"name": "x.txt", "task_id": "../evil"})[0] == 400)

# OSError branch of delegation_list_results (unreadable results dir).
_saved = api.RESULT_DIR
api.RESULT_DIR = tmp / "not-a-dir-file"
api.RESULT_DIR.write_text("plain file")
check("direct list handles unreadable dir", api.delegation_list_results()[0] == 500)
api.RESULT_DIR = _saved

if failures:
    sys.exit(1)
print("PASS — delegation endpoints E2E")
