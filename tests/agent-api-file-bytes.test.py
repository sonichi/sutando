#!/usr/bin/env python3
"""GET /file-bytes — the four-way authorized generated-file serve (⑤a).

Runs against a REAL ThreadingHTTPServer on an ephemeral port (the route streams
raw bytes, so a fake handler would not exercise the headers or the body).

Authorization matrix: root (under <results>/<task_id>/ only), binding (task's
`source_room_id` == token room — unknown, malformed, roomless and foreign-room
ids all get the SAME refusal, no existence oracle), token (read scope only —
enqueue and the legacy global token are refused), quota (the pinned envelope:
10 files, 5 MiB per file, 80 serves, 400 MiB per task, persisted OUTSIDE the
task dir in <workspace>/state/serve-quota/, validated, monotonic, flock'd).

Open-then-verify: a symlink swapped in between validation and open is refused
deterministically; FIFOs, directories and hard links are refused; a file changed
underneath the validated path is refused. Also: task metadata resolves in the
live, processed and archived layouts (the pre-archive window), concurrent serves
are counted exactly once each (threads AND processes), counters survive a
restart, corrupt or regressing quota state is refused, over-quota is never
served, a serve holds the task lease against the sweeper, and the happy path
streams the exact bytes with the contract headers.

Run: python3 tests/agent-api-file-bytes.test.py
"""
import fcntl
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
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


STATE = WS / "state"
QUOTA_A, LOCK_A = retention.serve_quota_paths(STATE, TASK_A)
_modules = [api]


def quota(task_id=TASK_A):
    try:
        return json.loads(retention.serve_quota_paths(STATE, task_id)[0].read_text())
    except FileNotFoundError:
        return {"files": [], "serves": 0, "bytes": 0}


def set_quota(state, task_id=TASK_A):
    """Write persisted counters directly — what a FRESH process would find, so
    every loaded module forgets its in-process high-water mark for the task."""
    retention.serve_quota_paths(STATE, task_id)[0].write_text(json.dumps(state))
    for mod in _modules:
        mod._serve_quota_seen.pop(task_id, None)


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

print("== binding: one indistinguishable refusal ==")
foreign = fetch(IMG_A, token="tok-b-read")
check("cross-room read token refused (403)", foreign[0] == 403)
check("other room's task with its own room's token but my path: 403",
      fetch(IMG_A, task_id=TASK_B, token="tok-b-read")[0] == 403)
check("same room, different task id for this path: 403", fetch(IMG_A, task_id=TASK_A2)[0] == 403)
check("same room, the other task's own file under its own id: 200",
      fetch(IMG_A2, task_id=TASK_A2)[0] == 200)
(api.TASK_DIR / "task-signal-4-cccc.txt").write_text(
    "id: task-signal-4-cccc\nsource: signal-room\naccess_tier: team\ntask: no room\n")
(api.RESULT_DIR / "task-signal-4-cccc").mkdir()
(api.TASK_DIR / "task-signal-6-eeee.txt").write_text(
    "id: task-signal-6-eeee\nsource: discord\naccess_tier: team\nsource_room_id: !a:hs\ntask: x\n")
for label, tid in (("unknown task id", "task-signal-9-zzzz"), ("non-Signal-Room task id", "task-1"),
                   ("traversal in task id", "../" + TASK_A), ("empty task id", ""),
                   ("task recorded without a room", "task-signal-4-cccc"),
                   ("task from another lane under the id prefix", "task-signal-6-eeee")):
    got = fetch(IMG_A, task_id=tid)
    check(f"{label}: the foreign-room refusal, status and body identical",
          got[0] == foreign[0] == 403 and got[2] == foreign[2], f"{got[0]} {got[2]!r}")

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
linked_out = WS / "linked-out.png"
linked_out.write_bytes(PNG)
os.link(linked_out, DIR_A / "hard.png")
code, _h, body = fetch(DIR_A / "hard.png")
check("hard link to an outside image: 403, no bytes", code == 403 and body != PNG, f"code={code}")
os.link(IMG_A, DIR_A / "hard-inner.png")
check("hard link inside the dir is still refused (link count > 1)",
      fetch(DIR_A / "hard-inner.png")[0] == 403 and fetch(IMG_A)[0] == 403)
os.unlink(DIR_A / "hard-inner.png")
check("the original serves again once it is the only link", fetch(IMG_A)[0] == 200)

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
check("refusals are not charged", before == 4, str(before))

print("== size cap and quota envelope ==")
big = DIR_A / "big.png"
with open(big, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n")
    fh.seek(api.FILE_SERVE_MAX_FILE_BYTES)      # one byte over the cap
    fh.write(b"\0")
check("over the per-file cap: 413 before any read", fetch(big)[0] == 413)
check("413 not charged", quota()["serves"] == 4)
snapshot = quota()
others = [f"/nowhere/{i}.png" for i in range(api.FILE_SERVE_MAX_FILES)]
set_quota({"v": 1, "files": others, "serves": 4, "bytes": 0})
code, _h, body = fetch(IMG_A)
check("distinct-file budget exhausted: 429, never served", code == 429 and body != PNG)
set_quota({"v": 1, "files": [], "serves": api.FILE_SERVE_MAX_SERVES, "bytes": 0})
code, _h, body = fetch(IMG_A)
check("serve budget exhausted: 429, never served", code == 429 and body != PNG)
set_quota({"v": 1, "files": [], "serves": 0, "bytes": api.FILE_SERVE_MAX_TOTAL_BYTES - len(PNG) + 1})
code, _h, body = fetch(IMG_A)
check("byte budget exhausted: 429, never served", code == 429 and body != PNG)
set_quota({"v": 1, "files": [], "serves": 0, "bytes": api.FILE_SERVE_MAX_TOTAL_BYTES - len(PNG)})
check("exactly at the byte budget still serves", fetch(IMG_A)[0] == 200)
set_quota(dict(snapshot, v=1))
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
check("no temp files left behind; the flock file stays (never unlinked)",
      not [p for p in QUOTA_A.parent.iterdir() if p.suffix == ".tmp"] and LOCK_A.is_file())
check("counters live outside the task-writable dir, 0600 in a 0700 dir",
      QUOTA_A == STATE / "serve-quota" / f"{TASK_A}.json"
      and not [p for p in DIR_A.iterdir() if "quota" in p.name]
      and stat.S_IMODE(os.stat(QUOTA_A).st_mode) == 0o600
      and stat.S_IMODE(os.stat(LOCK_A).st_mode) == 0o600
      and stat.S_IMODE(os.stat(QUOTA_A.parent).st_mode) == 0o700)

print("== restart persistence ==")
api2 = _load("agent_api_restart")
api2.RESULT_DIR = api.RESULT_DIR
_modules.append(api2)
ok, why = api2._reserve_serve_quota(TASK_A, os.path.realpath(IMG_A), len(PNG))
check("a fresh process continues the persisted counter",
      ok and quota()["serves"] == base + 13, str((ok, why, quota())))
set_quota({"v": 1, "files": [], "serves": api.FILE_SERVE_MAX_SERVES, "bytes": 0})
ok, why = api2._reserve_serve_quota(TASK_A, os.path.realpath(IMG_A), len(PNG))
check("a fresh process honours an exhausted persisted budget", ok is False and "serve" in why)
set_quota(dict(snapshot, v=1))

print("== quota integrity: fail closed ==")
check("a serve first, so this process holds a high-water mark", fetch(IMG_A)[0] == 200)
mark = quota()
for label, raw in (("corrupt JSON", "{nope"), ("wrong version", json.dumps(dict(mark, v=2))),
                   ("negative serves", json.dumps(dict(mark, v=1, serves=-1))),
                   ("negative bytes", json.dumps(dict(mark, v=1, bytes=-5))),
                   ("bool counter", json.dumps(dict(mark, v=1, serves=True))),
                   ("float counter", json.dumps(dict(mark, v=1, bytes=1.5))),
                   ("files not a list", json.dumps(dict(mark, v=1, files="x"))),
                   ("non-string file entry", json.dumps(dict(mark, v=1, files=[1]))),
                   ("not an object", "[1]")):
    set_quota({})
    QUOTA_A.write_text(raw)
    code, _h, body = fetch(IMG_A)
    check(f"{label}: 429, never served, state untouched",
          code == 429 and body != PNG and QUOTA_A.read_text() == raw, f"code={code}")
set_quota(dict(mark, v=1))
check("valid state again: served", fetch(IMG_A)[0] == 200)
mark = quota()
QUOTA_A.write_text(json.dumps(dict(mark, v=1, serves=mark["serves"] - 1)))
check("serves below this process's mark: corruption, 429", fetch(IMG_A)[0] == 429)
QUOTA_A.write_text(json.dumps(dict(mark, v=1, bytes=mark["bytes"] - 1)))
check("bytes below the mark: 429", fetch(IMG_A)[0] == 429)
QUOTA_A.write_text(json.dumps(dict(mark, v=1, files=[])))
check("fewer files than the mark: 429", fetch(IMG_A)[0] == 429)
QUOTA_A.write_text(json.dumps(dict(mark, v=1)))
check("the mark itself is not a decrease: served", fetch(IMG_A)[0] == 200)
QUOTA_A.unlink()
code, _h, body = fetch(IMG_A)
check("deleted file with an in-process record does NOT reset: 429, not recreated",
      code == 429 and body != PNG and not QUOTA_A.exists(), f"code={code}")
set_quota(dict(quota(), v=1, serves=mark["serves"] + 1, bytes=mark["bytes"] + len(PNG), files=mark["files"]))
check("repaired state serves again", fetch(IMG_A)[0] == 200)

print("== two processes contending ==")
CHILD = f"""
import importlib.util, os, sys, time
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = {str(WS)!r}
sys.path.insert(0, {str(REPO / "src")!r})
spec = importlib.util.spec_from_file_location("agent_api_child", {str(REPO / "src" / "agent-api.py")!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("ready", flush=True)
sys.stdin.readline()
for _ in range(int(sys.argv[1])):
    ok, why = mod._reserve_serve_quota({TASK_A!r}, {os.path.realpath(IMG_A)!r}, {len(PNG)})
    assert ok, why
"""
HOLDER = f"""
import fcntl, os, sys, time
fd = os.open({str(LOCK_A)!r}, os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(0.8)
"""
holder = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True)
holder.stdout.readline()
t0 = time.monotonic()
ok, why = api._reserve_serve_quota(TASK_A, os.path.realpath(IMG_A), len(PNG))
waited = time.monotonic() - t0
holder.wait()
check("a reserve blocks on another process's flock, then proceeds",
      ok and waited >= 0.4, f"ok={ok} why={why} waited={waited:.2f}")
before = quota()["serves"]
kids = [subprocess.Popen([sys.executable, "-c", CHILD, "15"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True) for _ in range(2)]
for kid in kids:
    kid.stdout.readline()
for kid in kids:
    kid.stdin.write("go\n")
    kid.stdin.close()
rcs = [kid.wait() for kid in kids]
check("two processes reserving concurrently are each counted exactly once",
      rcs == [0, 0] and quota()["serves"] == before + 30, f"rcs={rcs} serves={quota()['serves']} before={before}")
set_quota(dict(quota(), v=1))

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
gone = fetch(IMG_A)
check("task metadata gone everywhere: the same foreign-room refusal",
      gone[0] == 403 and gone[2] == foreign[2], str(gone[:1]))

print("== serve vs sweep: the per-task lease lock ==")
far = time.time() + 30 * 3600
DIR_A2 = api.RESULT_DIR / TASK_A2
LEASE_LOCK_A2 = retention.lease_lock_path(STATE, TASK_A2)
held = retention.hold_task_lease(STATE, TASK_A2)
report = retention.sweep_task_outputs(api.RESULT_DIR, now=far, state_dir=STATE)
check("a serve in progress makes the sweep skip its dir",
      DIR_A2.exists() and TASK_A2 in report["busy"], str(report))
check("the same sweep reclaims the dirs no serve holds",
      TASK_A in report["removed"] and not DIR_A.exists(), str(report))
check("a removed task's quota counters and locks are gone",
      not QUOTA_A.exists() and not LOCK_A.exists() and not retention.lease_lock_path(STATE, TASK_A).exists())
os.close(held)
raw = os.open(LEASE_LOCK_A2, os.O_RDWR)
fcntl.flock(raw, fcntl.LOCK_EX)
got = []
t = threading.Thread(target=lambda: got.append(fetch(IMG_A2, task_id=TASK_A2)), daemon=True)
t.start()
t.join(0.5)
check("a serve waits while the sweep holds the exclusive lock", t.is_alive() and not got)
fcntl.flock(raw, fcntl.LOCK_UN)
t.join(5)
check("and proceeds with the exact bytes once it is released",
      got and got[0][0] == 200 and got[0][2] == PNG, str(got[:1]))
fcntl.flock(raw, fcntl.LOCK_EX)
got = []
t = threading.Thread(target=lambda: got.append(fetch(IMG_A2, task_id=TASK_A2)), daemon=True)
t.start()
t.join(0.3)
shutil.rmtree(DIR_A2)
fcntl.flock(raw, fcntl.LOCK_UN)
t.join(5)
check("a serve that waited on a sweep which removed the dir gets 404, no bytes",
      got and got[0][0] == 404 and got[0][2] != PNG, str(got[:1]))
os.close(raw)
report = retention.sweep_task_outputs(api.RESULT_DIR, now=far, state_dir=STATE)
check("nothing is busy once no serve holds a lease", report["busy"] == [] and not DIR_A2.exists(), str(report))

server.shutdown()
server.server_close()

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — GET /file-bytes four-way authorization")
