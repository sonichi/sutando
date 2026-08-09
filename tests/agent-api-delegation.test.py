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
check("traversal name rejected", code == 400)  # valid_task_id gate fires before file lookup

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

# 5d. POST /task success path — the agent-to-agent submit endpoint writes a
# `source: api` task file and fires the anonymous task_processed emit at the
# write site (the telemetry coverage added for API-created tasks; the messaging
# bridges already emitted, this surface didn't). No callback_url → skips the
# SSRF branch. Served on the main thread by req(), so the write + emit lines are
# traced for the coverage gate.
code, data = req("POST", "/task", {"from": "agent-2", "task": "research the thing"})
check("POST /task accepted", code == 200 and data.get("ok") is True, str(data))
_api_tasks = [p for p in api.TASK_DIR.glob("task-*.txt")
              if "source: api" in p.read_text() and "from: agent-2" in p.read_text()]
check("POST /task wrote a source: api task file", len(_api_tasks) == 1,
      f"found {len(_api_tasks)} matching task files")

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
DIRECT_CONTENT = CONTENT.replace("id: task-e2e-1", "id: task-direct-1")
code, data = api.delegation_submit_task({"id": "task-direct-1", "content": DIRECT_CONTENT})
check("direct submit", code == 200 and (api.TASK_DIR / "task-direct-1.txt").read_text() == DIRECT_CONTENT)
# Identity coherence (Codex P1): filename id must equal the body id header.
check("submit rejects id/body divergence",
      api.delegation_submit_task({"id": "task-direct-9", "content": DIRECT_CONTENT})[0] == 400)
check("direct submit rejects bad id", api.delegation_submit_task({"id": "../x", "content": "y"})[0] == 400)
check("direct submit rejects empty content", api.delegation_submit_task({"id": "task-d2", "content": ""})[0] == 400)
# A filename-safe task id can still escape through a pre-positioned symlink.
# The HTTP-controlled submit path must reject it rather than overwrite the
# symlink target outside tasks/.
outside = tmp / "outside-task.txt"
outside.write_text("do not overwrite\n")
escape_link = api.TASK_DIR / "task-symlink-escape.txt"
try:
    escape_link.symlink_to(outside)
except OSError:
    # Windows/non-privileged environments may not permit symlink creation.
    pass
else:
    escape_content = DIRECT_CONTENT.replace("task-direct-1", "task-symlink-escape")
    code, _ = api.delegation_submit_task({
        "id": "task-symlink-escape", "content": escape_content,
    })
    check("direct submit rejects symlink escape",
          code == 400 and outside.read_text() == "do not overwrite\n")

# Race the final task entry into a symlink after validation but immediately
# before the atomic publish.  The submit must replace the directory entry,
# never follow it and overwrite the outside target.
race_outside = tmp / "outside-task-race.txt"
race_outside.write_text("race sentinel\n")
race_name = "task-symlink-race.txt"
race_content = DIRECT_CONTENT.replace("task-direct-1", "task-symlink-race")
real_replace = api.os.replace


def _race_replace(src, dst, *args, **kwargs):
    raced = api.TASK_DIR / dst
    try:
        raced.symlink_to(race_outside)
    except OSError:
        pass
    return real_replace(src, dst, *args, **kwargs)


api.os.replace = _race_replace
try:
    code, _ = api.delegation_submit_task({
        "id": "task-symlink-race", "content": race_content,
    })
finally:
    api.os.replace = real_replace
check("direct submit is safe against symlink swap race",
      code == 200 and race_outside.read_text() == "race sentinel\n"
      and (api.TASK_DIR / race_name).read_text() == race_content
      and not (api.TASK_DIR / race_name).is_symlink())

# A pre-positioned symlink whose target stays inside TASK_DIR passes the
# realpath containment gate, so the descriptor-level no-symlink check must
# still reject it.
inside_target = api.TASK_DIR / "inside-target.txt"
inside_target.write_text("inside sentinel\n")
inside_link = api.TASK_DIR / "task-inside-symlink.txt"
try:
    inside_link.symlink_to(inside_target)
except OSError:
    pass
else:
    inside_content = DIRECT_CONTENT.replace("task-direct-1", "task-inside-symlink")
    code, _ = api.delegation_submit_task({
        "id": "task-inside-symlink", "content": inside_content,
    })
    check("direct submit rejects in-directory symlink",
          code == 400 and inside_target.read_text() == "inside sentinel\n")

# If wrapping/writing the private descriptor fails, the descriptor and hidden
# temporary entry are both cleaned up before the error propagates.
real_fdopen = api.os.fdopen


def _failing_fdopen(fd, *args, **kwargs):
    api.os.close(fd)
    raise OSError("injected fdopen failure")


api.os.fdopen = _failing_fdopen
try:
    try:
        api.delegation_submit_task({
            "id": "task-write-failure",
            "content": DIRECT_CONTENT.replace("task-direct-1", "task-write-failure"),
        })
    except OSError as exc:
        write_failed = "injected fdopen failure" in str(exc)
    else:
        write_failed = False
finally:
    api.os.fdopen = real_fdopen
check("failed task write removes private temp entry",
      write_failed and not list(api.TASK_DIR.glob(".task-write-failure.txt.*.tmp")))

# If publication fails after another process has already removed the temp
# entry, the cleanup path tolerates the missing name while preserving the
# original publish error.
real_replace = api.os.replace


def _remove_temp_then_fail(src, dst, *args, **kwargs):
    api.os.unlink(src, dir_fd=kwargs["src_dir_fd"])
    raise OSError("injected publish failure")


api.os.replace = _remove_temp_then_fail
try:
    try:
        api.delegation_submit_task({
            "id": "task-publish-failure",
            "content": DIRECT_CONTENT.replace("task-direct-1", "task-publish-failure"),
        })
    except OSError as exc:
        publish_failed = "injected publish failure" in str(exc)
    else:
        publish_failed = False
finally:
    api.os.replace = real_replace
check("missing temp during failed publish cleanup is harmless", publish_failed)
(api.RESULT_DIR / "task-direct-1.txt").write_text("direct answer\n")
code, data = api.delegation_list_results()
check("direct list", code == 200 and "task-direct-1.txt" in data["files"])
code, data = api.delegation_read_result("task-direct-1.txt")
check("direct read", code == 200 and data["body"] == "direct answer\n")
check("direct read 404", api.delegation_read_result("task-nonexistent-99999.txt")[0] == 404)
check("direct read traversal rejected", api.delegation_read_result("../../etc")[0] == 400)
# Defense-in-depth: read side applies valid_task_id gate symmetrically with
# submit side (#1959). Malformed ids that pass _safe_path (e.g. "../x" gets
# None from _safe_path, but "task-../x" could differ) are rejected before it.
check("direct read rejects malformed id (has space)",
      api.delegation_read_result("task-a b.txt")[0] == 400)
check("direct read rejects path-separator id",
      api.delegation_read_result("task-a/b.txt")[0] == 400)
check("direct read rejects path-traversal id",
      api.delegation_read_result("task-../x.txt")[0] == 400)
code, data = api.delegation_archive_result({"name": "task-direct-1.txt", "task_id": "task-direct-1"})
check("direct archive", code == 200 and list((api.RESULT_DIR / "archive").glob("*/task-direct-1.txt")))
check("direct archive already-gone", api.delegation_archive_result(
    {"name": "task-direct-1.txt", "task_id": "task-direct-1"})[1].get("note") == "already gone")
check("direct archive bad tid", api.delegation_archive_result(
    {"name": "x.txt", "task_id": "../evil"})[0] == 400)
# Cross-archive hijack (Codex P1): name must be exactly <task_id>.txt.
(api.RESULT_DIR / "task-foreign.txt").write_text("someone else's\n")
check("archive rejects name/tid mismatch", api.delegation_archive_result(
    {"name": "task-foreign.txt", "task_id": "task-direct-1"})[0] == 400)
# Both archive paths are confined after resolving symlinks. A relay client
# cannot make the archive endpoint inspect or move a result outside results/,
# or replace a destination outside the month archive directory.
outside_result = tmp / "outside-result.txt"
outside_result.write_text("outside result\n")
source_escape = api.RESULT_DIR / "task-source-escape.txt"
try:
    source_escape.symlink_to(outside_result)
except OSError:
    pass
else:
    code, _ = api.delegation_archive_result({
        "name": "task-source-escape.txt", "task_id": "task-source-escape",
    })
    check("archive rejects source symlink escape",
          code == 400 and outside_result.read_text() == "outside result\n")

(api.RESULT_DIR / "task-dest-escape.txt").write_text("inside result\n")
archive_month = api.local_task_protocol.archive_month_dir(
    api.RESULT_DIR, api.datetime.now().isoformat())
archive_month.mkdir(parents=True, exist_ok=True)
dest_escape = archive_month / "task-dest-escape.txt"
try:
    dest_escape.symlink_to(outside_result)
except OSError:
    pass
else:
    code, _ = api.delegation_archive_result({
        "name": "task-dest-escape.txt", "task_id": "task-dest-escape",
    })
    check("archive rejects destination symlink escape",
          code == 400 and outside_result.read_text() == "outside result\n"
          and (api.RESULT_DIR / "task-dest-escape.txt").exists())
# No-clobber (Codex P1): an occupied archive slot gets an epoch-suffixed name.
(api.RESULT_DIR / "task-direct-1.txt").write_text("second result\n")
code, _ = api.delegation_archive_result({"name": "task-direct-1.txt", "task_id": "task-direct-1"})
suffixed = list((api.RESULT_DIR / "archive").glob("*/task-direct-1-*.txt"))
originals = list((api.RESULT_DIR / "archive").glob("*/task-direct-1.txt"))
check("archive no-clobber: suffixed slot, original intact",
      code == 200 and len(suffixed) == 1 and len(originals) == 1
      and originals[0].read_text() == "direct answer\n"
      and suffixed[0].read_text() == "second result\n")

# OSError branch of delegation_list_results (unreadable results dir).
_saved = api.RESULT_DIR
api.RESULT_DIR = tmp / "not-a-dir-file"
api.RESULT_DIR.write_text("plain file")
check("direct list handles unreadable dir", api.delegation_list_results()[0] == 500)
api.RESULT_DIR = _saved

if failures:
    sys.exit(1)
print("PASS — delegation endpoints E2E")
