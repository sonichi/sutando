#!/usr/bin/env python3
"""GET /file-bytes — the four-way authorized generated-file serve (⑤a).

Runs against a REAL ThreadingHTTPServer on an ephemeral port (the route streams
raw bytes, so a fake handler would not exercise the headers or the body).

Authorization matrix: root (under <results>/<task_id>/ only), binding (task's
`source_room_id` == token room), token (read scope only — enqueue and the
legacy global token are refused), quota (the pinned envelope: 10 files, 5 MiB
per file, 80 serves, 400 MiB per task, persisted per task dir).

Open-then-verify: a symlink swapped in between validation and open is refused
deterministically; FIFOs and directories are refused; a file changed underneath
the validated path is refused. Also: task metadata resolves in the live,
processed and archived layouts (the pre-archive window), concurrent serves are
counted exactly once each, counters survive a restart, over-quota is never
served, and the happy path streams the exact bytes with the contract headers.

Run: python3 tests/agent-api-file-bytes.test.py
"""
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

_TEST_WS = tempfile.mkdtemp(prefix="file-bytes-")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = _TEST_WS
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, str(REPO / "src" / "agent-api.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


api = _load("agent_api")
from policy import signal_tokens as st  # noqa: E402
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
    st.make_row("!a:hs", "enqueue", "tok-a-enq", created_at_ms=2),
    st.make_row("!b:hs", "read", "tok-b-read", created_at_ms=3),
])

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
TASK_A = "task-signal-1-aaaa"
TASK_A2 = "task-signal-2-aaaa"     # same room, different task
TASK_B = "task-signal-3-bbbb"      # other room


def write_task(task_id, room):
    (api.TASK_DIR / f"{task_id}.txt").write_text(
        f"id: {task_id}\nsource: signal-room\naccess_tier: team\n"
        f"source_room_id: {room}\ntask: draw\n")
    (api.RESULT_DIR / task_id).mkdir(exist_ok=True)


for tid, room in ((TASK_A, "!a:hs"), (TASK_A2, "!a:hs"), (TASK_B, "!b:hs")):
    write_task(tid, room)
DIR_A = api.RESULT_DIR / TASK_A
IMG_A = DIR_A / "chart.png"
IMG_A.write_bytes(PNG)
IMG_A2 = api.RESULT_DIR / TASK_A2 / "other.png"
IMG_A2.write_bytes(PNG)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def fetch(path, task_id=TASK_A, token="tok-a-read", raw_query=None):
    query = raw_query if raw_query is not None else (
        f"path={quote(str(path), safe='')}&gateway_task_id={quote(task_id, safe='')}")
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    conn.request("GET", f"/file-bytes?{query}", headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, hdrs, body


def quota():
    try:
        return json.loads((DIR_A / api.SERVE_QUOTA_NAME).read_text())
    except FileNotFoundError:
        return {"files": [], "serves": 0, "bytes": 0}


print("== happy path ==")
code, hdrs, body = fetch(IMG_A)
check("200 with the exact bytes", code == 200 and body == PNG, f"code={code} len={len(body)}")
check("Content-Type sniffed from the bytes", hdrs.get("content-type") == "image/png", str(hdrs))
check("Content-Length set", hdrs.get("content-length") == str(len(PNG)))
check("X-Content-Type-Options: nosniff", hdrs.get("x-content-type-options") == "nosniff")
check("lease touched by the serve", (DIR_A / retention.LEASE_NAME).exists())
q = quota()
check("quota: one serve, one file, the fstat size charged",
      q["serves"] == 1 and q["files"] == [os.path.realpath(IMG_A)] and q["bytes"] == len(PNG), str(q))
code, hdrs, body = fetch(os.path.realpath(IMG_A))
check("realpath spelling of the same file also serves", code == 200 and body == PNG)
check("repeat serve counts the serve, not another distinct file",
      quota()["serves"] == 2 and len(quota()["files"]) == 1)

print("== token: scope and legacy ==")
check("enqueue-scope token refused (403)", fetch(IMG_A, token="tok-a-enq")[0] == 403)
check("legacy global token refused (403)", fetch(IMG_A, token="global-token")[0] == 403)
check("unknown token refused (403)", fetch(IMG_A, token="nope")[0] == 403)
check("no token refused (403)", fetch(IMG_A, token=None)[0] == 403)

print("== binding ==")
check("cross-room read token refused (403)", fetch(IMG_A, token="tok-b-read")[0] == 403)
check("other room's task with its own room's token but my path: 403",
      fetch(IMG_A, task_id=TASK_B, token="tok-b-read")[0] == 403)
check("same room, different task id for this path: 403", fetch(IMG_A, task_id=TASK_A2)[0] == 403)
check("same room, the other task's own file under its own id: 200",
      fetch(IMG_A2, task_id=TASK_A2)[0] == 200)
check("unknown task id: 404", fetch(IMG_A, task_id="task-signal-9-zzzz")[0] == 404)
check("non-Signal-Room task id: 404", fetch(IMG_A, task_id="task-1")[0] == 404)
check("traversal in task id: 404", fetch(IMG_A, task_id="../" + TASK_A)[0] == 404)
(api.TASK_DIR / "task-signal-4-cccc.txt").write_text(
    "id: task-signal-4-cccc\nsource: signal-room\naccess_tier: team\ntask: no room\n")
(api.RESULT_DIR / "task-signal-4-cccc").mkdir()
check("task recorded without a room is never bindable (404)",
      fetch(IMG_A, task_id="task-signal-4-cccc")[0] == 404)

print("== root ==")
outside = WS / "outside.png"
outside.write_bytes(PNG)
check("path outside results: 403", fetch(outside)[0] == 403)
check("path under results but outside the task dir: 403", fetch(IMG_A2)[0] == 403)
sibling = api.RESULT_DIR / (TASK_A + "0")
sibling.mkdir()
(sibling / "x.png").write_bytes(PNG)
check("prefix-confusable sibling dir: 403", fetch(sibling / "x.png")[0] == 403)
check("relative path: 403", fetch("chart.png")[0] == 403)
check("dot-dot escape: 403", fetch(f"{DIR_A}/../{TASK_A2}/other.png")[0] == 403)
check("missing file under the task dir: 404", fetch(DIR_A / "missing.png")[0] == 404)
check("missing query: 404/403, never 200", fetch(None, raw_query="")[0] in (403, 404))
(DIR_A / "escape.png").symlink_to(outside)
check("symlink to outside: 403", fetch(DIR_A / "escape.png")[0] == 403)
(DIR_A / "inner.png").symlink_to(IMG_A)
check("symlink inside the dir is still refused (O_NOFOLLOW)", fetch(DIR_A / "inner.png")[0] == 403)
link_dir = api.RESULT_DIR / "task-signal-5-dddd"
write_task("task-signal-5-dddd", "!a:hs")
shutil.rmtree(link_dir)
link_dir.symlink_to(DIR_A)
check("task dir that is itself a symlink: 403", fetch(link_dir / "chart.png", task_id="task-signal-5-dddd")[0] == 403)
(DIR_A / "sub").mkdir()
(DIR_A / "sub" / "deep.png").write_bytes(PNG)
check("nested regular file serves", fetch(DIR_A / "sub" / "deep.png")[0] == 200)
(DIR_A / "sublink").symlink_to(DIR_A / "sub")
check("nested path through a symlinked component: 403", fetch(DIR_A / "sublink" / "deep.png")[0] == 403)

print("== open-then-verify ==")
(DIR_A / "notes.txt").write_text("not an image")
check("non-image bytes: 403", fetch(DIR_A / "notes.txt")[0] == 403)
check("directory: 403", fetch(DIR_A / "sub")[0] == 403)
fifo = DIR_A / "pipe.png"
os.mkfifo(fifo)
check("FIFO: 403 and the handler does not block", fetch(fifo)[0] == 403)
# Deterministic swap: validation sees a regular file; the open sees a symlink.
swap_target = DIR_A / "swap.png"
swap_target.write_bytes(PNG)
real_realpath = os.path.realpath
swapped = []


def swapping_realpath(p, *a, **k):
    out = real_realpath(p, *a, **k)
    if str(p) == str(swap_target) and not swapped:
        swapped.append(True)
        swap_target.unlink()
        swap_target.symlink_to(outside)
    return out


os.path.realpath = swapping_realpath
try:
    code, _h, body = fetch(swap_target)
finally:
    os.path.realpath = real_realpath
check("symlink swapped between validation and open: 403, no bytes",
      swapped and code == 403 and body != PNG, f"code={code}")
before = quota()["serves"]
check("refusals are not charged", before == 3, str(before))

print("== size cap and quota envelope ==")
big = DIR_A / "big.png"
with open(big, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n")
    fh.seek(api.FILE_SERVE_MAX_FILE_BYTES)      # one byte over the cap
    fh.write(b"\0")
check("over the per-file cap: 413 before any read", fetch(big)[0] == 413)
check("413 not charged", quota()["serves"] == 3)
snapshot = quota()
others = [f"/nowhere/{i}.png" for i in range(api.FILE_SERVE_MAX_FILES)]
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps({"v": 1, "files": others, "serves": 3, "bytes": 0}))
code, _h, body = fetch(IMG_A)
check("distinct-file budget exhausted: 429, never served", code == 429 and body != PNG)
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps(
    {"v": 1, "files": [], "serves": api.FILE_SERVE_MAX_SERVES, "bytes": 0}))
code, _h, body = fetch(IMG_A)
check("serve budget exhausted: 429, never served", code == 429 and body != PNG)
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps(
    {"v": 1, "files": [], "serves": 0, "bytes": api.FILE_SERVE_MAX_TOTAL_BYTES - len(PNG) + 1}))
code, _h, body = fetch(IMG_A)
check("byte budget exhausted: 429, never served", code == 429 and body != PNG)
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps(
    {"v": 1, "files": [], "serves": 0, "bytes": api.FILE_SERVE_MAX_TOTAL_BYTES - len(PNG)}))
check("exactly at the byte budget still serves", fetch(IMG_A)[0] == 200)
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps(snapshot))
check("envelope constants pinned",
      (api.FILE_SERVE_MAX_FILES, api.FILE_SERVE_MAX_FILE_BYTES, api.FILE_SERVE_MAX_SERVES,
       api.FILE_SERVE_MAX_TOTAL_BYTES) == (10, 5 * 1024 * 1024, 80, 400 * 1024 * 1024))

print("== concurrent serves ==")
base = quota()["serves"]
results = []


def worker():
    results.append(fetch(IMG_A))


threads = [threading.Thread(target=worker) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("all concurrent serves succeed with exact bytes",
      all(c == 200 and b == PNG for c, _h, b in results))
q = quota()
check("concurrent serves counted exactly once each",
      q["serves"] == base + 12 and q["bytes"] == snapshot["bytes"] + 12 * len(PNG), str(q))
check("no lock or temp files left behind",
      not [p for p in DIR_A.iterdir() if p.name.startswith(".serve-quota.") and p.name != api.SERVE_QUOTA_NAME])

print("== restart persistence ==")
api2 = _load("agent_api_restart")
api2.RESULT_DIR = api.RESULT_DIR
ok, why = api2._reserve_serve_quota(TASK_A, os.path.realpath(IMG_A), len(PNG))
check("a fresh process continues the persisted counter",
      ok and quota()["serves"] == base + 13, str((ok, why, quota())))
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps(
    {"v": 1, "files": [], "serves": api.FILE_SERVE_MAX_SERVES, "bytes": 0}))
ok, why = api2._reserve_serve_quota(TASK_A, os.path.realpath(IMG_A), len(PNG))
check("a fresh process honours an exhausted persisted budget", ok is False and "serve" in why)
(DIR_A / api.SERVE_QUOTA_NAME).write_text(json.dumps(snapshot))

print("== pre-archive window: task metadata in every layout ==")
live = api.TASK_DIR / f"{TASK_A}.txt"
processed = api.TASK_DIR / "processed" / f"{TASK_A}.txt"
processed.parent.mkdir(exist_ok=True)
shutil.move(str(live), str(processed))
check("task in tasks/processed/: still bound and served", fetch(IMG_A)[0] == 200)
month = api.TASK_DIR / "archive" / "2026-09"
month.mkdir(parents=True)
shutil.move(str(processed), str(month / f"{TASK_A}.txt"))
(api.RESULT_DIR / "archive" / "2026-09").mkdir(parents=True)
(api.RESULT_DIR / "archive" / "2026-09" / f"{TASK_A}.txt").write_text("[file: x]\n")
check("task + result archived, output dir intact: still served", fetch(IMG_A)[0] == 200)
check("archived task: cross-room still refused", fetch(IMG_A, token="tok-b-read")[0] == 403)
(month / f"{TASK_A}.txt").unlink()
check("task metadata gone everywhere: 404", fetch(IMG_A)[0] == 404)

server.shutdown()
server.server_close()

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — GET /file-bytes four-way authorization")
