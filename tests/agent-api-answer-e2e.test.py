#!/usr/bin/env python3
"""E2E for the pending-questions round trip: GET /tasks/active → POST /answer.

The bug this covers end-to-end: the web UI listed a free-form question and then
POST /answer 404'd on its id ("question Q1 not found or already answered"), because
the writer required **Status:**/**Options:** markers the reader never needed. Unit
tests over the parser live in agent-api-pending-questions.test.py; this drives the
two HTTP handlers against a real server so the id a client is *given* is provably
the id it can *answer* with.

Run: python3 tests/agent-api-answer-e2e.test.py
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

# The format the agent writes today: prose sections, no metadata markers,
# resolved items parked below a `# Resolved` divider.
PQ = """# Pending Questions

_Open decisions awaiting owner input._

## ❓ Re-auth the Station Gmail connector?
Returns HTTP 410. Reconnect, or say "drop it" and I'll rely on the MCP.

## ❓ Rebuild the Swift menu-bar app?
Dead since Jul 6 — it owns the watcher auto-restart safety net.

# Resolved & archived detail

## ❓ Something already dealt with
Archive. Must never be offered as open.
"""

tmp = Path(tempfile.mkdtemp(prefix="pq-answer-e2e-"))
(tmp / "tasks").mkdir()
api.WORKSPACE_DIR = tmp
api.TASK_DIR = tmp / "tasks"
api.API_TOKEN = "test-token-123"

# Create the file at the per-host path FIRST, then let personal_path find it.
#
# Asking personal_path where to WRITE looked equivalent and was not. Its probes
# run in order — `<ws>/hosts/<host>/`, then the legacy `$SUTANDO_MEMORY_DIR/
# machine-<host>/` — and return the first that EXISTS. With a fresh tmp the
# hosts/ probe misses, so on any machine that still has a legacy file the call
# returned a path in the operator's real, vault-SYNCED memory tree, and the
# write below landed there. Reproduced on a live host: this test rewrote
# `…/memory/machine-<host>/pending-questions.md` on every run, and the vault
# sync then committed the fixture.
#
# #2452 fixed the not-yet-existing case; this is the existing-legacy-file case
# its own test deliberately leaves open (`personal-path-workspace-isolation`
# docstring), because the read fallback is load-bearing for migration. So the
# fix belongs here in the caller, not in the resolver.
#
# Creating it under hosts/ first makes the FIRST probe hit, so resolution never
# reaches the legacy branch.
PQ_FILE = tmp / "hosts" / _host_label() / "pending-questions.md"
PQ_FILE.parent.mkdir(parents=True, exist_ok=True)
PQ_FILE.write_text(PQ)

# The property that actually matters, asserted rather than assumed: the path the
# module resolves is the one inside tmp. Passing a tmpdir is not isolation
# unless the RESOLVED path is inside it — without this line the escape is
# silent, which is exactly how the fixture reached the vault.
_resolved = Path(api.personal_path("pending-questions.md", tmp))
assert _resolved == PQ_FILE, (
    f"personal_path escaped the tmp workspace: {_resolved} is not {PQ_FILE}"
)

# Handler runs on the MAIN thread (plain HTTPServer + handle_request loop);
# requests are issued from a worker thread. Inverted on purpose: the coverage
# gate's tracer misses handler-THREAD execution, so serving on the main
# thread is what makes the handler lines measurable.
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


def _raw_req(method, path, body=None):
    r = urllib.request.Request(f"{BASE}{path}", method=method,
                               data=None if body is None else json.dumps(body).encode())
    r.add_header("Authorization", "Bearer test-token-123")
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


def req(method, path, body=None):
    """Issue the request from a worker thread while the MAIN thread serves it."""
    out = {}
    t = threading.Thread(target=lambda: out.update(
        zip(("code", "data"), _raw_req(method, path, body))), daemon=True)
    t.start()
    while t.is_alive():
        server.handle_request()   # serve on the main thread (traced)
    t.join()
    return out["code"], out["data"]


print("agent-api answer e2e")

# 1. The listing: free-form questions are offered, the archive is not.
code, data = req("GET", "/tasks/active")
questions = data.get("questions", [])
check("GET /tasks/active → 200", code == 200, f"got {code}")
check("free-form questions listed", [q["text"] for q in questions] == [
    "❓ Re-auth the Station Gmail connector?",
    "❓ Rebuild the Swift menu-bar app?",
], f"got {[q.get('text') for q in questions]}")
check("archive below the `# Resolved` divider is not offered",
      all("already dealt with" not in q["text"] for q in questions))
check("splice offsets stay server-side",
      all("start" not in q and "end" not in q for q in questions))

target = questions[0] if questions else {"id": "Q1"}

# 2. The round trip: the id a client was GIVEN is the id it can ANSWER with.
#    Pre-fix this 404'd for every free-form question — the whole bug.
code, data = req("POST", "/answer", {"id": target["id"], "answer": "drop it"})
check("POST /answer with a listed id → 200", code == 200, f"got {code} {data}")
check("response echoes the id", data.get("id") == target["id"])

body = PQ_FILE.read_text()
check("answer recorded on a **Status:** line (the notifier reads this)",
      "**Status:** Answered" in body and "drop it" in body)
check("the other question is untouched", "## ❓ Rebuild the Swift menu-bar app?" in body)
check("the archive is untouched", "## ❓ Something already dealt with" in body)

tasks = list((tmp / "tasks").glob("answer-*.txt"))
check("agent task file written", len(tasks) == 1, f"got {[t.name for t in tasks]}")
if tasks:
    check("task file carries the answer", "drop it" in tasks[0].read_text())

# 3. The answered question drops out of the next listing.
code, data = req("GET", "/tasks/active")
still_open = [q["id"] for q in data.get("questions", [])]
check("answered question no longer listed", target["id"] not in still_open)
check("unanswered question still listed", len(still_open) == 1, f"got {still_open}")

# 4. Genuine 404s — the message now only fires when it's true.
code, data = req("POST", "/answer", {"id": target["id"], "answer": "again"})
check("re-answering the same id → 404 (already answered)", code == 404, f"got {code}")
code, data = req("POST", "/answer", {"id": "Q1", "answer": "stale"})
check("unknown/stale positional id → 404", code == 404, f"got {code}")
code, data = req("POST", "/answer", {"id": target["id"]})
check("missing answer → 400", code == 400, f"got {code}")

# 5. Duplicate-title stale id must never migrate to a neighbour (#2103 review).
#    Three open sections share a title; the UI is handed an id for the SECOND.
#    The agent answers the FIRST (rewriting the file). The id the UI still holds
#    must still resolve the SECOND section over HTTP — not the third.
PQ_FILE.write_text(
    "# Pending Questions\n\n"
    "## Same title\nFirst — ALPHA.\n\n"
    "## Same title\nSecond — BRAVO.\n\n"
    "## Same title\nThird — CHARLIE.\n"
)
code, data = req("GET", "/tasks/active")
dupes = data.get("questions", [])
check("three duplicate-title questions listed", len(dupes) == 3, f"got {len(dupes)}")
id_second = dupes[1]["id"] if len(dupes) > 1 else None
id_third = dupes[2]["id"] if len(dupes) > 2 else None
check("duplicate ids are distinct", len({q["id"] for q in dupes}) == 3)

# Answer the FIRST duplicate, then reuse the stale id for the SECOND.
req("POST", "/answer", {"id": dupes[0]["id"], "answer": "resolved first"})
code, data = req("POST", "/answer", {"id": id_second, "answer": "picked BRAVO"})
check("stale duplicate id still answers → 200", code == 200, f"got {code} {data}")

# The answer must sit in the BRAVO section, never in CHARLIE's.
sections = [s for s in PQ_FILE.read_text().split("## Same title\n") if s.strip()]
answered = [s for s in sections if "picked BRAVO" in s]
check("the owner's answer landed on exactly one section", len(answered) == 1)
check("...and it was BRAVO, not the neighbour",
      bool(answered) and "BRAVO" in answered[0] and "CHARLIE" not in answered[0],
      f"landed on: {answered}")
code, data = req("GET", "/tasks/active")
left = [q["id"] for q in data.get("questions", [])]
check("the third (CHARLIE) question is still open", id_third in left, f"got {left}")

server.server_close()
print(f"\nagent-api-answer-e2e: {ran - len(failures)}/{ran} passed")
if failures:
    print("failed: " + ", ".join(failures))
sys.exit(1 if failures else 0)
