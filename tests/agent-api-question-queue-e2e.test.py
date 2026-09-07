#!/usr/bin/env python3
"""E2E for the triage queue's two HTTP routes: GET /questions/queue, POST /question/dismiss.

Unit coverage of the ranking, re-check verdict and dismissal store lives in
pending-questions-triage.test.py. This drives the routes against a real server,
because a route can be wired to the wrong unit, skip its auth check, or return a
shape the client cannot read while every underlying function is perfectly correct.

Run: python3 tests/agent-api-question-queue-e2e.test.py
Exit: 0 = all pass, 1 = failure
"""
import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from util_paths import _host_label  # noqa: E402 — needs the sys.path above


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")

PQ = """# Pending Questions

## 2026-08-01 — ALPHA, oldest and blocking nothing
Prose only.

## 2026-08-20 — BRAVO, blocked on sonichi/sutando#4242
Waiting on a pull request.

# Resolved

## 2026-07-01 — Archived
Must never be offered as open.
"""

tmp = Path(tempfile.mkdtemp(prefix="pq-queue-e2e-"))
api.WORKSPACE_DIR = tmp
api.API_TOKEN = "test-token-123"

# Per-host file FIRST so personal_path's first probe hits: a fresh tmp otherwise
# falls through to the operator's vault-synced memory tree. See agent-api-answer-e2e.
PQ_FILE = tmp / "hosts" / _host_label() / "pending-questions.md"
PQ_FILE.parent.mkdir(parents=True, exist_ok=True)
PQ_FILE.write_text(PQ)

_resolved = Path(api.personal_path("pending-questions.md", tmp))
assert _resolved == PQ_FILE, f"personal_path escaped the tmp workspace: {_resolved}"

# Handler runs on the MAIN thread; requests come from a worker. Inverted on purpose
# — the coverage tracer misses handler-THREAD execution.
server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
BASE = f"http://127.0.0.1:{server.server_address[1]}"

failures = []
ran = 0


def check(name, cond, detail=""):
    global ran
    ran += 1
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
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload
    except Exception as e:
        return -1, {"error": repr(e)}


def req(method, path, body=None, token="test-token-123"):
    out = {}
    t = threading.Thread(target=lambda: out.update(
        zip(("code", "data"), _raw_req(method, path, body, token))), daemon=True)
    t.start()
    while t.is_alive():
        server.handle_request()
    t.join()
    return out["code"], out["data"]


print("agent-api question-queue e2e")

# The probe is the one part that would reach the network; its own behaviour is
# unit-tested, so the route is exercised against a decided verdict.
_probe = mock.patch.object(
    api, "_probe_ref_states", return_value={("sonichi/sutando", 4242): "MERGED"})
_probe.start()

code, data = req("GET", "/questions/queue")
questions = data.get("questions", [])
check("GET /questions/queue → 200", code == 200, f"got {code}")
check("the archive is not offered", len(questions) == 2, f"got {len(questions)}")
check("every row carries the wait the card renders",
      all("age_days" in q for q in questions))

stale = [q for q in questions if q.get("recheck")]
check("the merged blocker is labelled stale, live, on the way out",
      len(stale) == 1 and stale[0]["recheck"]["status"] == "stale",
      f"got {[q.get('recheck') for q in questions]}")
check("...and the stale row is still offered, not dropped",
      any("BRAVO" in q["text"] for q in questions))
_probe.stop()

# A failing probe must change nothing a client can see except the labels.
with mock.patch.object(api.subprocess, "run", side_effect=OSError("gh missing")):
    code, data = req("GET", "/questions/queue")
check("a failed re-check still returns every question", len(data.get("questions", [])) == 2,
      f"got {len(data.get('questions', []))}")
check("...and labels none of them resolved",
      all(q.get("recheck") is None for q in data.get("questions", [])))

code, _ = req("GET", "/questions/queue", token=None)
check("GET /questions/queue without a token → 401", code == 401, f"got {code}")

# Dismissal, through the route, twice — the second is the resurface guard.
target = next(q["id"] for q in questions if "ALPHA" in q["text"])
code, data = req("POST", "/question/dismiss", {"id": target})
check("POST /question/dismiss → 200", code == 200 and data.get("ok") is True, f"got {code} {data}")

with mock.patch.object(api, "_probe_ref_states", return_value={}):
    code, data = req("GET", "/questions/queue")
left = [q["id"] for q in data.get("questions", [])]
check("the dismissed question is gone from the queue", target not in left, f"got {left}")
check("...and only that one went", len(left) == 1, f"got {len(left)}")

check("dismissing never edits the questions file", PQ_FILE.read_text() == PQ)

code, data = req("POST", "/question/dismiss", {"id": ""})
check("POST /question/dismiss with no id → 400", code == 400, f"got {code} {data}")

code, _ = req("POST", "/question/dismiss", {"id": "Qx"}, token=None)
check("POST /question/dismiss without a token → 401", code == 401, f"got {code}")

code, data = req("POST", "/question/dismiss")
check("POST /question/dismiss with no body → 400", code == 400, f"got {code} {data}")

server.server_close()
print(f"\nagent-api-question-queue-e2e: {ran - len(failures)}/{ran} passed")
if failures:
    print("failed: " + ", ".join(failures))
sys.exit(1 if failures else 0)
