#!/usr/bin/env python3
"""/result and /file-bytes on a task under its LIVE processing names.

A core renames `task-<id>.txt` to `task-<id>.claimed-core-N.txt` when it claims
the task, and a pool lead to `task-<id>.assigned-<inst>.txt`. Both routes bind a
room token by reading the task's `source_room_id`, and that lookup used to know
only the bare live name plus the processed/archived layouts — so exactly while
a task was being processed its own room's token got the foreign-room 403, and
/result answered 404 for a task that was pending. The lookup now goes through
`task_archive.find_task_file` (which knows every live name) before the archive
walk. Runs against a REAL ThreadingHTTPServer, as tests/agent-api-file-bytes.test.py.

Run: python3 tests/agent-api-live-task-names.test.py
"""
import http.client
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

_TEST_WS = tempfile.mkdtemp(prefix="live-task-names-")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = _TEST_WS
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("agent_api", str(REPO / "src" / "agent-api.py"))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
from policy import signal_tokens as st  # noqa: E402

import signal_worker_launch as launch  # noqa: E402
import task_output_retention as retention  # noqa: E402

WS = Path(_TEST_WS)
api.TASK_DIR = WS / "tasks"
api.RESULT_DIR = WS / "results"
api.TASK_DIR.mkdir(exist_ok=True)
api.RESULT_DIR.mkdir(exist_ok=True)
api.API_TOKEN = "global-token"
api.SIGNAL_TOKEN_REGISTRY = WS / "state" / "signal-room-tokens.json"
st.write_registry(api.SIGNAL_TOKEN_REGISTRY, [
    st.make_row("!a:hs", "read", "tok-a-read", created_at_ms=1),
    st.make_row("!b:hs", "read", "tok-b-read", created_at_ms=3),
])
STATE = WS / "state"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
CLAIMED = "task-signal-7-ffff"
ASSIGNED = "task-signal-8-gggg"
NAMES = {CLAIMED: f"{CLAIMED}.claimed-core-1.txt", ASSIGNED: f"{ASSIGNED}.assigned-inst-2.txt"}


def write_task(task_id, name, room="!a:hs"):
    """A published-then-claimed task by hand: the renamed file, its counter, its dir."""
    (api.TASK_DIR / name).write_text(
        f"id: {task_id}\nsource: signal-room\naccess_tier: team\nsource_room_id: {room}\ntask: draw\n")
    retention.init_serve_quota(STATE, task_id)
    (api.RESULT_DIR / task_id).mkdir(exist_ok=True)
    (api.RESULT_DIR / task_id / "chart.png").write_bytes(PNG)


for tid, name in NAMES.items():
    write_task(tid, name)
(api.TASK_DIR / "task-owner-1.claimed-core-1.txt").write_text("id: task-owner-1\nsource: api\ntask: owner work\n")

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def get(path, token):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def result(task_id, token="tok-a-read"):
    code, body = get(f"/result/{task_id}", token)
    try:
        return code, json.loads(body)
    except ValueError:
        return code, {}


def file_bytes(task_id, token="tok-a-read"):
    path = api.RESULT_DIR / task_id / "chart.png"
    return get(f"/file-bytes?path={quote(str(path), safe='')}&gateway_task_id={quote(task_id, safe='')}", token)


for tid, name in NAMES.items():
    kind = "CLAIMED" if "claimed" in name else "ASSIGNED"
    print(f"== {kind} live name: {name} ==")
    check(f"{kind}: the bare name is absent, only the live name exists",
          not (api.TASK_DIR / f"{tid}.txt").exists() and (api.TASK_DIR / name).exists())
    code, data = result(tid)
    check(f"{kind}: /result with the room's own token is pending, not 403/404",
          code == 200 and data.get("status") == "pending", f"{code} {data}")
    check(f"{kind}: /result cross-room is still 403", result(tid, "tok-b-read")[0] == 403)
    check(f"{kind}: /result with the legacy global token is 403 (registry provisioned)",
          result(tid, "global-token")[0] == 403)
    code, body = file_bytes(tid)
    check(f"{kind}: /file-bytes with the room's own token serves the exact bytes",
          code == 200 and body == PNG, f"code={code} len={len(body)}")
    code, body = file_bytes(tid, "tok-b-read")
    check(f"{kind}: /file-bytes cross-room is still 403, no bytes", code == 403 and body != PNG)
    root = launch.output_root_for(tid, api.TASK_DIR, api.RESULT_DIR)
    check(f"{kind}: the wrapper's root derivation resolves the live name",
          root == os.path.join(os.path.realpath(api.RESULT_DIR), tid), root)
    (api.RESULT_DIR / f"{tid}.txt").write_text(f"hello from the sandbox\n[file: {root}/chart.png]\n")
    code, data = result(tid)
    check(f"{kind}: once the result lands, /result is completed with the guarded body intact",
          code == 200 and data.get("status") == "completed" and "hello from the sandbox" in data.get("result", "")
          and f"[file: {root}/chart.png]" in data.get("result", ""), f"{code} {data}")

print("== the fix widens nothing else ==")
check("a longer id's claimed file does not satisfy a shorter id",
      result("task-signal-7-fff")[0] == 403 and file_bytes("task-signal-7-fff")[0] == 403)
check("an unknown id under a claimed-looking name is still the foreign-room refusal",
      result("task-signal-9-zzzz")[0] == 403 and file_bytes("task-signal-9-zzzz")[0] == 403)
check("a traversal id is refused", result("..%2Ftask-signal-7-ffff")[0] in (403, 404))
code, data = result("task-owner-1", "global-token")
check("a CLAIMED owner task polls as pending under the global token (was 404)",
      code == 200 and data.get("status") == "pending", f"{code} {data}")
check("an unknown owner id is still 404", result("task-owner-9", "global-token")[0] == 404)

server.shutdown()
server.server_close()
print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — live task names authorize on /result and /file-bytes")
