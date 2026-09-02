#!/usr/bin/env python3
"""agent-api: per-room token scoping on the Signal Room routes (R2).

Drives the real Handler.do_POST / do_GET with a registry in a temp workspace:

  * BEFORE any per-room row exists the legacy global token still enqueues and
    polls (an old daemon keeps working mid-upgrade);
  * once the registry is PROVISIONED (any row, live or revoked) the global token
    is refused on /guest-task, the /task guest shim and /result of a Signal Room
    task — revoking every row keeps it refused; an INVALID registry file refuses
    everything on those routes (503) while owner routes are untouched;
  * the token's room is authoritative: `source_room_id` is stamped from it, a
    body room_id must EQUAL it (403), a read-scope token cannot enqueue, and a
    room token never reaches the owner lane;
  * /result: a room token reads only its own room's task; cross-room is 403;
  * revocation and rotation take effect without a restart;
  * every route takes ONE registry snapshot per request (`authorize()`), so a
    file replaced mid-request cannot split the verdict;
  * provisioning is irreversible: once a row was seen, an emptied or deleted
    registry is `invalid` (503 on every scoped route, legacy never re-admitted),
    and the on-disk marker keeps it that way across a restart.

Run: python3 tests/agent-api-signal-scoped-auth.test.py
"""
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_TEST_WS = tempfile.mkdtemp(prefix="scoped-auth-")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = _TEST_WS
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from policy import signal_tokens as st  # noqa: E402

WS = Path(_TEST_WS)


def load_api(name):
    """A fresh agent-api module on the same workspace — what a restart sees."""
    spec = importlib.util.spec_from_file_location(name, str(REPO / "src" / "agent-api.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TASK_DIR = WS / "tasks"
    mod.RESULT_DIR = WS / "results"
    mod.TASK_DIR.mkdir(exist_ok=True)
    mod.RESULT_DIR.mkdir(exist_ok=True)
    mod.API_TOKEN = "global-token"
    mod.SIGNAL_TOKEN_REGISTRY = WS / "state" / "signal-room-tokens.json"
    mod._emit_task_processed = lambda *a, **k: None
    return mod


api = load_api("agent_api")

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeRFile:
    def __init__(self, body=b""):
        self.body = body

    def read(self, n=-1):
        return self.body


def handler(path, token, body=None, mod=None):
    mod = api if mod is None else mod
    h = mod.Handler.__new__(mod.Handler)
    h.path = path
    raw = b"" if body is None else json.dumps(body).encode()
    h.headers = {"Content-Length": str(len(raw))}
    if token is not None:
        h.headers["Authorization"] = f"Bearer {token}"
    h.rfile = FakeRFile(raw)
    h._responses = []
    h.send_json = lambda status, data: h._responses.append((status, data))
    h.send_private_json = h.send_json
    return h


def post(path, token, body, mod=None):
    h = handler(path, token, body, mod)
    h.do_POST()
    return h._responses[0]


def get(path, token, mod=None):
    h = handler(path, token, mod=mod)
    h.do_GET()
    return h._responses[0]


def task_file(task_id):
    return api.TASK_DIR / f"{task_id}.txt"


def source_room(task_id):
    for line in task_file(task_id).read_text().split("\n"):
        if line.startswith("source_room_id:"):
            return line.partition(":")[2].strip()
    return None


def park(task_id):
    """Move a task out of the live dir (frees an admission slot; still findable)."""
    (api.TASK_DIR / "processed").mkdir(exist_ok=True)
    shutil.move(str(task_file(task_id)), str(api.TASK_DIR / "processed" / f"{task_id}.txt"))


print("== before any per-room row: the legacy global token is unchanged ==")
code, data = post("/guest-task", "global-token", {"task": "q", "room_id": "!a:hs"})
check("global token enqueues on /guest-task", code == 200, str(data))
legacy_task = data.get("task_id", "")
check("room stamped from the body when no registry exists", source_room(legacy_task) == "!a:hs")
code, data = get(f"/result/{legacy_task}", "global-token")
check("global token polls a Signal Room result", code == 200 and data.get("status") == "pending")
code, data = post("/guest-task", "wrong", {"task": "q"})
check("wrong token still 401 (ordinary gate)", code == 401)
park(legacy_task)

print("== rows provisioned: the capability flips ==")
registry = api.SIGNAL_TOKEN_REGISTRY
rows = {
    "a_enq": st.make_row("!a:hs", "enqueue", "tok-a-enq", created_at_ms=1),
    "a_read": st.make_row("!a:hs", "read", "tok-a-read", created_at_ms=2),
    "b_enq": st.make_row("!b:hs", "enqueue", "tok-b-enq", created_at_ms=3),
}
st.write_registry(registry, list(rows.values()))

code, data = post("/guest-task", "global-token", {"task": "q", "room_id": "!a:hs"})
check("global token refused on /guest-task once a row exists", code == 403, str(data))
code, data = post("/task", "global-token", {"task": "q", "access_tier": "guest", "room_id": "!a:hs"})
check("global token refused on the /task guest shim once a row exists", code == 403, str(data))
check("refusals wrote no task file", not list(api.TASK_DIR.glob("task-signal-*.txt")))

code, data = post("/guest-task", "tok-a-enq", {"task": "q", "room_id": "!a:hs"})
check("room token enqueues when body room equals its room", code == 200, str(data))
task_a = data.get("task_id", "")
check("source_room_id stamped from the token", source_room(task_a) == "!a:hs")
check("the task's output dir is created", (api.RESULT_DIR / task_a).is_dir())
check("the task body carries the output contract",
      str(api.RESULT_DIR / task_a) in task_file(task_a).read_text())

code, data = post("/guest-task", "tok-a-enq", {"task": "q", "room_id": "!b:hs"})
check("body room != token room -> 403", code == 403, str(data))
code, data = post("/guest-task", "tok-a-enq", {"task": "q"})
check("no body room: stamped from the token", code == 200 and source_room(data["task_id"]) == "!a:hs")
park(data["task_id"])

code, data = post("/guest-task", "tok-a-read", {"task": "q", "room_id": "!a:hs"})
check("read-scope token cannot enqueue (403)", code == 403, str(data))
code, data = post("/guest-task", "tok-unknown", {"task": "q"})
check("unknown token refused (403, not the legacy path)", code == 403, str(data))

code, data = post("/task", "tok-b-enq", {"task": "q", "access_tier": "guest", "room_id": "!b:hs"})
check("/task guest shim honours the room token", code == 200, str(data))
task_b = data.get("task_id", "")
check("shim stamps the token's room", source_room(task_b) == "!b:hs")
code, data = post("/task", "tok-b-enq", {"task": "q", "access_tier": "guest", "room_id": "!a:hs"})
check("shim: body room != token room -> 403", code == 403, str(data))
code, data = post("/task", "tok-b-enq", {"from": "x", "task": "owner work"})
check("room token cannot submit an owner task (403)", code == 403, str(data))
check("owner lane wrote nothing for the room token",
      not [p for p in api.TASK_DIR.glob("task-*.txt") if "owner work" in p.read_text()])
code, data = post("/task", "global-token", {"from": "x", "task": "owner work"})
check("global token still submits owner tasks", code == 200, str(data))

print("== /result: the token's room against the task's stored room ==")
code, data = get(f"/result/{task_a}", "tok-a-enq")
check("own room's enqueue token polls its task", code == 200 and data.get("status") == "pending")
code, data = get(f"/result/{task_a}", "tok-a-read")
check("own room's read token polls its task", code == 200)
code, data = get(f"/result/{task_a}", "tok-b-enq")
check("cross-room token refused on /result (403)", code == 403, str(data))
code, data = get(f"/result/{task_a}", "global-token")
check("global token refused on a Signal Room result once rows exist", code == 403, str(data))
code, data = get("/result/task-signal-does-not-exist", "tok-a-enq")
check("unknown task with a room token is 403 (no cross-room existence oracle)", code == 403)
(api.TASK_DIR / "task-1.txt").write_text("id: task-1\naccess_tier: owner\ntask: mine\n")
code, data = get("/result/task-1", "global-token")
check("global token still polls owner tasks", code == 200 and data.get("status") == "pending")
code, data = get("/result/task-1", "tok-a-enq")
check("room token cannot poll an owner task (403)", code == 403, str(data))
park(task_b)

print("== one registry snapshot per request ==")
reg = api._signal_token_registry()
snapshots = []
real_authorize = reg.authorize
reg.authorize = lambda token: (snapshots.append(token), real_authorize(token))[1]
live_before = sorted(api.TASK_DIR.glob("task-signal-*.txt"))
for label, fn in (("/guest-task", lambda: post("/guest-task", "tok-a-read", {"task": "q"})),
                  ("the /task guest shim", lambda: post("/task", "tok-a-read", {"task": "q", "access_tier": "guest"})),
                  ("/result", lambda: get(f"/result/{task_a}", "tok-a-enq")),
                  ("/scan-text", lambda: post("/scan-text", "tok-a-read", {"texts": ["x"]}))):
    snapshots.clear()
    code, data = fn()
    check(f"{label}: exactly one authorize() per request, on the presented bearer",
          len(snapshots) == 1 and snapshots[0].startswith("tok-a-"), f"{code} {snapshots}")
reg.authorize = real_authorize
check("no task was written by the scope refusals", sorted(api.TASK_DIR.glob("task-signal-*.txt")) == live_before)

print("== revocation and rotation, live ==")
st.write_registry(registry, [dict(rows["a_enq"], revoked_at=10), rows["a_read"], rows["b_enq"]])
code, data = post("/guest-task", "tok-a-enq", {"task": "q"})
check("revoked token refused", code == 403, str(data))
code, data = get(f"/result/{task_a}", "tok-a-enq")
check("revoked token cannot poll either", code == 403)
a_enq2 = st.make_row("!a:hs", "enqueue", "tok-a-enq-2", created_at_ms=11)
st.write_registry(registry, [dict(rows["a_enq"], revoked_at=10), a_enq2, rows["a_read"], rows["b_enq"]])
code, data = post("/guest-task", "tok-a-enq-2", {"task": "q"})
check("rotated-in token enqueues, stamped with the same room",
      code == 200 and source_room(data["task_id"]) == "!a:hs", str(data))

print("== every row revoked: the registry stays provisioned, the global token stays out ==")
st.write_registry(registry, [dict(r, revoked_at=12) for r in (a_enq2, rows["a_read"], rows["b_enq"])])
for stale in api.TASK_DIR.glob("task-signal-*.txt"):
    park(stale.stem)
code, data = post("/guest-task", "global-token", {"task": "q", "room_id": "!c:hs"})
check("global token still refused on /guest-task with every row revoked", code == 403, str(data))
code, data = post("/task", "global-token", {"task": "q", "access_tier": "guest", "room_id": "!c:hs"})
check("global token still refused on the guest shim with every row revoked", code == 403, str(data))
code, data = get(f"/result/{task_a}", "global-token")
check("global token still refused on a Signal Room result with every row revoked", code == 403, str(data))
code, data = post("/guest-task", "tok-a-enq-2", {"task": "q"})
check("a revoked room token is refused as unscoped (403, never the legacy gate)", code == 403, str(data))
check("refusals wrote no task file", not list(api.TASK_DIR.glob("task-signal-*.txt")))
code, data = get("/result/task-1", "global-token")
check("owner /result unaffected", code == 200, str(data))

print("== invalid registry file: every scoped route fails closed (503) ==")
for label, content in (("corrupt", "{not json"),
                       ("wrong version", json.dumps({"v": 2, "tokens": [rows["a_enq"]]})),
                       ("malformed row", json.dumps({"v": 1, "tokens": [rows["a_enq"], {"room_id": "!z:hs"}]}))):
    registry.write_text(content)
    code, data = post("/guest-task", "tok-a-enq", {"task": "q"})
    check(f"{label}: a would-be-valid room token is refused (503)", code == 503, str(data))
    code, data = post("/guest-task", "global-token", {"task": "q", "room_id": "!a:hs"})
    check(f"{label}: the global token is refused (503)", code == 503, str(data))
    code, data = post("/task", "global-token", {"task": "q", "access_tier": "guest"})
    check(f"{label}: the guest shim is refused (503)", code == 503, str(data))
    code, data = get(f"/result/{task_a}", "global-token")
    check(f"{label}: /result of a Signal Room task is refused (503)", code == 503, str(data))
    check(f"{label}: the response carries no reason", data == {"error": "room token registry unavailable"}, str(data))
    code, data = get("/result/task-1", "global-token")
    check(f"{label}: owner /result still served", code == 200, str(data))
    code, data = post("/task", "global-token", {"from": "x", "task": "owner work 2"})
    check(f"{label}: owner /task still accepted", code == 200, str(data))
check("refusals wrote no Signal Room task file", not list(api.TASK_DIR.glob("task-signal-*.txt")))
st.write_registry(registry, [rows["a_enq"]])
code, data = post("/guest-task", "tok-a-enq", {"task": "q"})
check("a rewritten valid registry recovers without restart", code == 200, str(data))
park(data["task_id"])

print("== provisioning is irreversible: emptied or deleted, the registry denies; legacy stays out ==")
marker = st.marker_path(WS)
check("the marker was written on first observation, 0600",
      marker == WS / "state" / "signal-room-tokens.provisioned" and marker.is_file()
      and stat.S_IMODE(os.stat(marker).st_mode) == 0o600)
st.write_registry(registry, [])
code, data = post("/guest-task", "global-token", {"task": "q", "room_id": "!c:hs"})
check("emptied after provisioning: the global token is refused (503), never re-admitted", code == 503, str(data))
code, data = post("/task", "global-token", {"task": "q", "access_tier": "guest", "room_id": "!c:hs"})
check("emptied: the guest shim refuses (503)", code == 503, str(data))
registry.unlink()
scoped_routes = (
    ("/guest-task with the global token", lambda m: post("/guest-task", "global-token", {"task": "q", "room_id": "!a:hs"}, m)),
    ("/guest-task with a would-be room token", lambda m: post("/guest-task", "tok-a-enq", {"task": "q"}, m)),
    ("the guest shim", lambda m: post("/task", "global-token", {"task": "q", "access_tier": "guest"}, m)),
    ("/result of a Signal Room task", lambda m: get(f"/result/{task_a}", "global-token", m)),
    ("/scan-text", lambda m: post("/scan-text", "global-token", {"texts": ["x"]}, m)),
)
for label, fn in scoped_routes:
    code, data = fn(api)
    check(f"registry deleted after provisioning: {label} is refused (503)", code == 503, str(data))
check("refusals wrote no Signal Room task file", not list(api.TASK_DIR.glob("task-signal-*.txt")))
code, data = get("/result/task-1", "global-token")
check("owner /result still served", code == 200, str(data))
code, data = post("/task", "global-token", {"from": "x", "task": "owner work 3"})
check("owner /task still accepted", code == 200, str(data))
restarted = load_api("agent_api_restarted")
for label, fn in scoped_routes:
    code, data = fn(restarted)
    check(f"after a restart the marker still denies: {label} (503)", code == 503, str(data))
st.write_registry(registry, [rows["a_enq"]])
code, data = post("/guest-task", "tok-a-enq", {"task": "q"}, restarted)
check("a rewritten registry with rows recovers after the restart", code == 200, str(data))
park(data["task_id"])
code, data = post("/guest-task", "global-token", {"task": "q", "room_id": "!a:hs"}, restarted)
check("and the legacy token stays out", code == 403, str(data))

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — Signal Room per-room token scoping")
