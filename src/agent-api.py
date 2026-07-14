#!/usr/bin/env python3
"""
Sutando agent API — simple HTTP endpoint for agent-to-agent communication.

Receives tasks from other agents or services, writes them to tasks/
for processing by the cron loop.

Endpoints:
  POST /task              — submit a task (JSON: {from, task, priority?, callback_url?})
  GET  /result/<id>       — poll for task result
  GET  /status            — current health + capabilities
  GET  /ping              — alive check
  POST /twilio/voice      — inbound call webhook (Twilio)
  POST /twilio/sms        — inbound SMS webhook (Twilio)
  POST /twilio/transcription — voicemail transcription callback (Twilio)

Usage:
  python3 src/agent-api.py              # start on port 7843
  curl -X POST http://localhost:7843/task -d '{"from":"agent-2","task":"research X"}'
  curl http://localhost:7843/result/task-123456   # poll for result

Agent-to-agent:
  POST /task with callback_url → Sutando POSTs result to that URL when done
  Or poll GET /result/<task_id> until status="completed"

Twilio setup:
  Set webhook URL in Twilio console to https://<your-tunnel>/twilio/voice (calls)
  and https://<your-tunnel>/twilio/sms (messages).

Security: Set SUTANDO_API_TOKEN in .env for token auth (Authorization: Bearer <token>).
For remote access: use ngrok or SSH tunnel.
"""

import hashlib
import http.server
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote


def _safe_id(raw: str) -> str:
    """Sanitize an ID to prevent path traversal. Only allow alphanumeric, dash, underscore, dot."""
    return re.sub(r'[^a-zA-Z0-9_\-.]', '', raw)


def validate_twilio_signature(handler, body: str) -> bool:
    """Validate X-Twilio-Signature if TWILIO_AUTH_TOKEN is configured.
    Returns True if valid or if token not configured (local dev)."""
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        return True
    import hmac, hashlib, base64
    from urllib.parse import parse_qs

    signature = handler.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    # Prefer static base URL to prevent Host header injection bypass.
    # TWILIO_WEBHOOK_URL is the public ngrok/funnel URL Twilio sends webhooks to.
    base_url = os.environ.get("TWILIO_WEBHOOK_URL", "")
    if base_url:
        url = base_url.rstrip("/") + handler.path
    else:
        host = handler.headers.get("Host", "localhost")
        scheme = handler.headers.get("X-Forwarded-Proto", "https")
        url = f"{scheme}://{host}{handler.path}"

    params = parse_qs(body, keep_blank_values=True)
    param_string = url
    for key, values in sorted(params.items()):
        param_string += key + values[0]

    mac = hmac.new(auth_token.encode(), param_string.encode(), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(expected, signature)


# Two separate concerns (per qingyun review on PR #775):
# - REPO_DIR  = source tree (this file's parent.parent) — for reading source
#               files like src/health-check.py, running `git -C` against the
#               checkout, loading .env, etc. Stays anchored to the checkout.
# - WORKSPACE_DIR = runtime state (resolve_workspace()) — for tasks/, results/,
#               core-status.json, pending-questions.md, contextual-chips.json,
#               etc. Honors SUTANDO_WORKSPACE when set so watcher + bridges
#               stay aligned with these writes.
REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from workspace_default import resolve_workspace, status_read_path  # noqa: E402
import local_task_protocol  # noqa: E402

WORKSPACE_DIR = resolve_workspace()
TASK_DIR = WORKSPACE_DIR / "tasks"
PORT = 7843

# Personal-asset path resolver — see src/util_paths.py. Imported here so the
# /avatar and /stand-identity endpoints prefer the per-machine private dir
# over the public workspace.
from util_paths import personal_path  # noqa: E402
from task_body_guard import confine_user_content  # noqa: E402

# Simple token auth — set SUTANDO_API_TOKEN in .env for remote access security
API_TOKEN = os.environ.get("SUTANDO_API_TOKEN", "")

RESULT_DIR = WORKSPACE_DIR / "results"
TASK_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

# In-memory task history (survives file cleanup, lost on restart)
# {task_id: {status, text, time, result}}
task_history = {}

# Voice state: "connected" or "disconnected". Toggled via /voice/toggle.
# Web client polls /voice/state and connects/disconnects accordingly.
voice_desired_state = "disconnected"



# --- pending-questions.md ---------------------------------------------------
# ONE parser, shared by GET /status (lists the questions, mints their ids) and
# POST /answer (resolves an id back to a section). They used to walk the file
# separately, and drifted: the reader took the free-form format (post-#1265, no
# **Status:** markers) while the writer still required a **Status:**/**Options:**
# line, so every free-form question was listed but unanswerable — POST /answer
# 404'd on every id. Both paths stay on this function.
PQ_ARCHIVE_RE = re.compile(r'^#\s+Resolved\b', re.MULTILINE)
PQ_SECTION_RE = re.compile(r'^## ', re.MULTILINE)
PQ_ANSWERED_RE = re.compile(r'\*\*Status:\*\*\s*(resolved|answered|done|complete)', re.IGNORECASE)
PQ_STATUS_RE = re.compile(r'\*\*Status:\*\*.*')
PQ_FIELD_RE = re.compile(r'\*\*(?:Status|Options|Asked|Question):\*\*')
PQ_OPTIONS_RE = re.compile(r'\*\*Options:\*\*\s*(.+)')


def parse_pending_questions(content: str) -> list[dict]:
    """Open questions in pending-questions.md, in file order.

    Ids are derived from the title, not the section's position. The agent
    rewrites this file continuously, so a positional id minted by one GET
    points at a different — or already-archived — section by the time the owner
    clicks answer on it. Each dict carries the section's `start`/`end` offsets
    into `content` so the writer can splice it back in place.

    Sections below the `# Resolved` divider are the audit trail, not open
    questions (same cut as check-pending-questions.py:95).
    """
    archive = PQ_ARCHIVE_RE.search(content)
    active = content[:archive.start()] if archive else content
    starts = [m.start() for m in PQ_SECTION_RE.finditer(active)]
    questions: list[dict] = []
    seen: dict[str, int] = {}
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(active)
        head, _, body = active[start:end].partition('\n')
        title = head[3:].strip()  # drop the '## '
        if not title or title.startswith('RESOLVED') or title.startswith('[RESOLVED'):
            continue
        if PQ_ANSWERED_RE.search(body):
            continue
        qid = "Q" + hashlib.sha1(" ".join(title.split()).encode()).hexdigest()[:8]
        seen[qid] = seen.get(qid, 0) + 1
        if seen[qid] > 1:  # two sections sharing a title — keep the ids distinct
            qid = f"{qid}-{seen[qid]}"
        q = {
            "id": qid,
            "text": title,
            "detail": PQ_FIELD_RE.split(body)[0].strip() or title,
            "start": start,
            "end": end,
        }
        opts = PQ_OPTIONS_RE.search(body)
        if opts:
            q["options"] = [o.strip() for o in opts.group(1).split("|")]
        questions.append(q)
    return questions


def answer_pending_question(content: str, question: dict, answer: str) -> str:
    """Return `content` with `question`'s section marked answered.

    The resolution has to land on a **Status:** line: check-pending-questions.py
    treats a status-less section as unanswered (its free-form convention), so a
    [RESOLVED] title prefix alone would silence this API's own reader while the
    notifier kept re-asking the owner hourly.
    """
    section = content[question["start"]:question["end"]]
    status = f"**Status:** Answered {datetime.now().strftime('%Y-%m-%d')} — {' '.join(answer.split())}"
    if PQ_STATUS_RE.search(section):
        # A function repl, not a string: a raw answer may contain \1-style escapes.
        new_section = PQ_STATUS_RE.sub(lambda _m: status, section, count=1)
    else:
        new_section = section.rstrip("\n") + f"\n{status}\n\n"
    return content[:question["start"]] + new_section + content[question["end"]:]


def get_status() -> dict:
    try:
        # Use sys.executable — under launchd, bare `python3` resolves to
        # /usr/bin/python3 (3.9) which can't parse health-check.py's 3.10+
        # union syntax. Same regression source as dashboard.get_health().
        result = subprocess.run(
            [sys.executable, str(REPO_DIR / "src/health-check.py"), "--json"],
            capture_output=True, text=True, timeout=15,
        )
        health = json.loads(result.stdout.strip())
    except Exception:
        health = {"error": "health check unavailable"}

    return {
        "agent": "sutando",
        "version": "0.1.0",
        "status": "running",
        "health": health,
        "capabilities": [
            "research", "email", "calendar", "reminders", "screen-capture",
            "browser-automation", "notes", "file-management", "code",
            "image-generation", "translation", "contacts",
        ],
        "endpoints": {
            "task": "POST /task",
            "status": "GET /status",
            "ping": "GET /ping",
        },
    }


def _safe_path(base_dir: Path, filename: str) -> Path:
    """Resolve a path safely under base_dir. Returns None if path escapes.

    Uses the two-stage CodeQL-recognized path-injection defense:
    1. Whitelist the basename to `[a-zA-Z0-9_.-]+` (reject empty).
    2. `os.path.realpath` to normalize (Path::PathNormalization).
    3. `.startswith(base + sep)` prefix check (Path::SafeAccessCheck).
    `os.path.realpath` and `str.startswith` are the CodeQL-modeled pair —
    `Path.resolve` and `Path.is_relative_to` are NOT recognized, which is
    why the earlier in-helper markers didn't close py/path-injection.
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '', filename)
    if not safe_name:
        return None
    base_real = os.path.realpath(base_dir)
    resolved = os.path.realpath(os.path.join(base_real, f"{safe_name}.txt"))
    if not resolved.startswith(base_real + os.sep):
        return None
    return Path(resolved)


# ── TaskDelegationService relay endpoints (#1947) — route bodies ─────────────
# Module-level (status, payload) functions rather than inline handler code:
# unit-testable from the main thread (the coverage gate's tracer misses
# handler-thread execution), and the HTTP layer stays a thin dispatch.

def delegation_list_results():
    try:
        files = sorted(f.name for f in RESULT_DIR.iterdir()
                       if f.is_file() and f.name.endswith(".txt"))
        return 200, {"files": files}
    except OSError:
        return 500, {"error": "results dir unreadable"}


def delegation_read_result(name: str):
    # _safe_path appends ".txt" itself — hand it the stem.
    stem = name[:-4] if name.endswith(".txt") else name
    target = _safe_path(RESULT_DIR, stem)
    if target is None or not os.path.isfile(target):
        return 404, {"error": "no such result"}
    return 200, {"body": Path(target).read_text(errors="replace")}


def delegation_submit_task(data: dict):
    tid = str(data.get("id", ""))
    content = data.get("content", "")
    if not local_task_protocol.valid_task_id(tid) or not content:
        return 400, {"error": "invalid task id or empty content"}
    # Identity coherence (Codex P1 on #1956): the filename id and the body's
    # embedded `id:` header must agree, or downstream identity splits —
    # result polling, dedupe, archive, and history all key off one or the
    # other. The relay backend only submits task-last bodies, so the safe
    # parser reads the header unambiguously.
    embedded = local_task_protocol.parse_task_headers(content).get("id")
    if embedded != tid:
        return 400, {"error": f"body id header ({embedded!r}) does not match request id"}
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    (TASK_DIR / f"{tid}.txt").write_text(content)
    return 200, {"ok": True, "task_id": tid}


def delegation_archive_result(data: dict):
    name = str(data.get("name", ""))
    tid = str(data.get("task_id", ""))
    stem = name[:-4] if name.endswith(".txt") else name
    src = _safe_path(RESULT_DIR, stem)
    # Identity coherence (Codex P1 on #1956): a relay client may only archive
    # ITS OWN result — the source name must be exactly <task_id>.txt. Without
    # this, any safe name could be filed under any valid id, hijacking other
    # consumers' results into a foreign archive slot.
    if src is None or not local_task_protocol.valid_task_id(tid) or stem != tid:
        return 400, {"error": "invalid name/task id, or name does not match task id"}
    if not os.path.exists(src):
        return 200, {"ok": True, "note": "already gone"}
    # Same destination scheme as task-bridge's archiveFile():
    # results/archive/YYYY-MM/<taskId>.txt. No-clobber: an occupied slot gets
    # the epoch-suffixed name the bridges already use for re-archived results
    # (task-<id>-<epoch>.txt) instead of silently overwriting history.
    dest_dir = local_task_protocol.archive_month_dir(
        RESULT_DIR, datetime.now().isoformat())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{tid}.txt"
    if dest.exists():
        dest = dest_dir / f"{tid}-{int(datetime.now().timestamp())}.txt"
    os.replace(src, dest)
    return 200, {"ok": True}


def get_task_result(task_id: str):
    """Check if a task result exists."""
    result_file = _safe_path(RESULT_DIR, task_id)
    if result_file and result_file.exists():
        return {"task_id": _safe_id(task_id), "status": "completed", "result": result_file.read_text()}
    # Check archive — task-bridge archives results within seconds of delivery,
    # so direct /result polls often arrive after the file has been moved.
    safe_id = _safe_id(task_id)
    if safe_id:
        filename = f"{safe_id}.txt"
        for month_dir in sorted((RESULT_DIR / "archive").glob("*/"), reverse=True):
            candidate = month_dir / filename
            if candidate.exists():
                return {"task_id": safe_id, "status": "completed", "result": candidate.read_text()}
    task_file = _safe_path(TASK_DIR, task_id)
    if task_file and task_file.exists():
        return {"task_id": _safe_id(task_id), "status": "pending"}
    return None


# Store webhook callbacks for tasks
_webhooks: dict[str, str] = {}


def _is_safe_callback_url(url: str) -> tuple[bool, str]:
    """Validate a callback URL to prevent SSRF attacks.

    Returns (is_safe, reason).

    Rejects:
    - Non-HTTPS schemes (http, file, gopher, etc.)
    - Private / reserved IPs (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    - Cloud metadata endpoints (169.254.169.254)
    - Link-local addresses (169.254.0.0/16, fe80::/10)
    - Hostnames that resolve to private IPs

    Residual TOCTOU: getaddrinfo resolves at validation time; urlopen resolves
    again at request time. An attacker with a malicious DNS server and very low
    TTL could rebind between the two calls. In practice this window is narrow
    (microseconds + OS DNS cache) and callback URLs are owner-curated, making
    the attack impractical. IP pinning was considered but rejected because most
    TLS certs use hostname SANs — pinning to IP causes SSLCertVerificationError.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL"

    if parsed.scheme != "https":
        return False, "not HTTPS"
    if not parsed.hostname:
        return False, "no hostname"
    hostname_lower = parsed.hostname.lower()
    if hostname_lower in ("localhost", "localhost.localdomain"):
        return False, "localhost"
    try:
        addrinfos = socket.getaddrinfo(hostname_lower, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, "DNS resolution failed"
    private_ranges = [
        ipaddress.ip_network(b) for b in (
            "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
            "169.254.0.0/16", "0.0.0.0/8", "100.64.0.0/10", "198.18.0.0/15",
            "224.0.0.0/4", "240.0.0.0/4",
            "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
        )
    ]
    for family, _type, _proto, _canon, sockaddr in addrinfos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
            for net in private_ranges:
                if addr in net:
                    return False, f"private IP: {addr}"
            # IPv4-mapped IPv6 bypass guard. ipaddress's cross-family `in`
            # check returns False (an IPv6Address is never in an IPv4Network),
            # so a hostname that resolves to e.g. `::ffff:127.0.0.1` would
            # otherwise pass the loop above. Project the mapped IPv4 onto
            # the IPv4 private-range checks to close the bypass. Same
            # applies to IPv4-compatible IPv6 (`::a.b.c.d`), exposed via
            # `IPv6Address.ipv4_mapped` for the v4-mapped form;
            # `sixtofour` / `teredo` are public-routable tunneling and
            # don't need this projection.
            if isinstance(addr, ipaddress.IPv6Address):
                v4 = addr.ipv4_mapped
                if v4 is not None:
                    for net in private_ranges:
                        if isinstance(net, ipaddress.IPv4Network) and v4 in net:
                            return False, f"private IP (via IPv4-mapped IPv6 {addr}): {v4}"
        except ValueError:
            return False, f"invalid address: {sockaddr[0]}"
    return True, "ok"


def fire_webhook(task_id: str, result: str) -> None:
    """POST result to registered webhook URL."""
    url = _webhooks.pop(task_id, None)
    if not url:
        return
    safe, reason = _is_safe_callback_url(url)
    if not safe:
        print(f"[webhook] BLOCKED: callback URL failed SSRF check: {url} ({reason})")
        return
    try:
        import urllib.request
        data = json.dumps({"task_id": task_id, "status": "completed", "result": result}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Best-effort delivery


class Handler(http.server.BaseHTTPRequestHandler):
    # Drop connections that go silent (e.g. a client that opens TCP and never
    # sends a request line). Without this, readline() in handle_one_request
    # blocks forever holding a server thread. Same guard as dashboard (#1709).
    timeout = 30

    def log_message(self, format, *args):
        pass

    def send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ping":
            self.send_json(200, {"pong": True})
        elif path == "/core-status":
            # Read loop status file for web UI
            status_file = status_read_path("core-status.json", WORKSPACE_DIR)
            if status_file.exists():
                import json as _json
                try:
                    data = _json.loads(status_file.read_text())
                    self.send_json(200, data)
                except Exception:
                    self.send_json(200, {"status": "idle"})
            else:
                self.send_json(200, {"status": "idle"})
        elif path == "/voice/state":
            self.send_json(200, {"state": voice_desired_state})
        elif path == "/status":
            self.send_json(200, get_status())
        elif path == "/tasks/active":
            # List active tasks + system status for the web client
            watcher_ok = subprocess.run(["/usr/bin/pgrep", "-f", "watch-tasks"], capture_output=True).returncode == 0
            claude_ok = subprocess.run(["/usr/bin/pgrep", "-f", "claude.*sutando-core"], capture_output=True).returncode == 0
            # Scan disk for active tasks, update history (preserve existing text)
            for f in sorted(TASK_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                task_id = f.stem
                content = f.read_text()
                task_line = ""
                source_line = ""
                # Capture the first `source:` and first `task:` regardless of
                # field order — voice/chat tasks put `source:` before `task:`,
                # but discord/slack tasks put `task:` first. The `not …` guards
                # keep the real header `source:` from being overridden by any
                # `source:` line inside the task body (#1781 review, sonichi).
                for line in content.splitlines():
                    if not source_line and line.startswith("source:"):
                        source_line = line[7:].strip()
                    elif not task_line and line.startswith("task:"):
                        task_line = line[5:].strip()
                    if task_line and source_line:
                        break
                result_file = RESULT_DIR / f.name
                existing = task_history.get(task_id, {})
                # Look for the result in three places, in priority order:
                # (1) live results/ dir, (2) prior in-memory history (covers
                # the case where the bridge has already archived the file),
                # (3) results/archive/<YYYY-MM>/. Without (3), an agent-api
                # restart loses every prior task's result and the web UI
                # shows them all as "working" with no body.
                archived_file = None
                for month_dir in (RESULT_DIR / "archive").glob("*/"):
                    candidate = month_dir / f.name
                    if candidate.exists():
                        archived_file = candidate
                        break
                if result_file.exists():
                    status = "done"
                    result_text = result_file.read_text().strip()
                elif existing.get("status") == "done" or existing.get("result"):
                    status = "done"
                    result_text = existing.get("result", "")
                elif archived_file is not None:
                    status = "done"
                    result_text = archived_file.read_text().strip()
                else:
                    status = "working"
                    result_text = ""
                task_history[task_id] = {"status": status, "text": task_line or existing.get("text", task_id), "time": f.stat().st_mtime, "result": result_text, "source": source_line or existing.get("source", "")}
            # Also check for result files without task files (already cleaned up)
            for f in sorted(RESULT_DIR.glob("task-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                task_id = f.stem
                if task_id not in task_history:
                    result_content = f.read_text().strip()
                    display_text = result_content.split('\n')[0][:80] if result_content else task_id
                    task_history[task_id] = {"status": "done", "text": display_text, "time": f.stat().st_mtime, "result": result_content}
                elif task_history[task_id].get("status") != "done":
                    task_history[task_id]["status"] = "done"
                    task_history[task_id]["result"] = f.read_text().strip()
            # Reconcile stale entries: if task file is gone and result exists, mark done;
            # if task file is gone, no result, and older than 5 min, remove from history
            import time as _time
            stale_ids = []
            for tid, tdata in list(task_history.items()):
                if tdata.get("status") == "working":
                    task_file = TASK_DIR / f"{tid}.txt"
                    result_file = RESULT_DIR / f"{tid}.txt"
                    if result_file.exists():
                        tdata["status"] = "done"
                        tdata["result"] = result_file.read_text().strip()
                    elif not task_file.exists() and _time.time() - tdata.get("time", 0) > 300:
                        stale_ids.append(tid)
            for tid in stale_ids:
                del task_history[tid]
            # Return most recent 10 from history
            sorted_tasks = sorted(task_history.items(), key=lambda x: x[1].get("time", 0), reverse=True)[:10]
            tasks = [{"id": tid, **tdata} for tid, tdata in sorted_tasks]
            # Parse pending questions. `start`/`end` are the writer's splice
            # offsets — internal, not part of the wire format.
            questions = []
            pq_file = Path(personal_path("pending-questions.md", WORKSPACE_DIR))
            if pq_file.exists():
                questions = [
                    {k: v for k, v in q.items() if k not in ("start", "end")}
                    for q in parse_pending_questions(pq_file.read_text())
                ]
            self.send_json(200, {"tasks": tasks, "watcher": watcher_ok, "claude": claude_ok, "questions": questions})
        elif path == "/delegation/results":
            # TaskDelegationService relay backend (#1947): list results/ for
            # the split-host watcher. Read-only but bearer-gated like the
            # write side — result bodies are owner data.
            if not API_TOKEN:
                self.send_json(403, {"error": "delegation requires SUTANDO_API_TOKEN on the core host"})
            elif not self.check_auth():
                pass
            else:
                self.send_json(*delegation_list_results())
        elif path.startswith("/delegation/results/"):
            if not API_TOKEN:
                self.send_json(403, {"error": "delegation requires SUTANDO_API_TOKEN on the core host"})
            elif not self.check_auth():
                pass
            else:
                self.send_json(*delegation_read_result(
                    unquote(path[len("/delegation/results/"):])))
        elif path.startswith("/result/"):
            task_id = path[len("/result/"):]
            result = get_task_result(task_id)
            if result:
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "task not found"})
        elif path == "/avatar":
            avatar_file = personal_path("stand-avatar.png", workspace=WORKSPACE_DIR)
            if avatar_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(avatar_file.read_bytes())
            else:
                self.send_json(404, {"error": "no avatar"})
        elif path == "/stand-identity":
            si_file = personal_path("stand-identity.json", workspace=WORKSPACE_DIR)
            data = json.loads(si_file.read_text()) if si_file.exists() else {}
            self.send_json(200, data)
        elif path == "/activity":
            # Recent activity: git commits + processed tasks
            activity = []
            try:
                git_log = subprocess.run(
                    ["/usr/bin/git", "-C", str(REPO_DIR), "log", "--oneline", "--since=24 hours ago", "-10"],
                    capture_output=True, text=True, timeout=5
                ).stdout.strip()
                for line in git_log.split("\n"):
                    if line.strip():
                        parts = line.split(" ", 1)
                        activity.append({"type": "commit", "hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
            except Exception:
                pass
            # Recent results
            try:
                results_dir = WORKSPACE_DIR / "results"
                result_files = sorted(results_dir.glob("task-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
                for f in result_files:
                    content = f.read_text()[:200]
                    activity.append({"type": "task", "id": f.stem, "preview": content.split("\n")[0]})
            except Exception:
                pass
            self.send_json(200, {"activity": activity})
        elif path == "/contextual-chips":
            chips_file = status_read_path("contextual-chips.json", WORKSPACE_DIR)
            if chips_file.exists():
                try:
                    data = json.loads(chips_file.read_text())
                    self.send_json(200, data)
                except Exception:
                    self.send_json(200, {"chips": []})
            else:
                self.send_json(200, {"chips": []})
        elif path == "/dynamic-content":
            dc_file = status_read_path("dynamic-content.json", WORKSPACE_DIR)
            if dc_file.exists():
                try:
                    data = json.loads(dc_file.read_text())
                    self.send_json(200, data)
                except Exception:
                    self.send_json(200, {})
            else:
                self.send_json(200, {})
        elif path.startswith("/media/"):
            # Serve local files for dynamic region (images, audio, video, docs)
            # Note: mimetypes import removed — replaced by SAFE_TYPES allowlist (CodeQL #19-23 mitigation)
            rel = path[len("/media/"):]
            # Sanitize: strip everything except safe filename characters (fixes CodeQL #20-21)
            safe_rel = re.sub(r'[^a-zA-Z0-9_./-]', '', rel)
            if not safe_rel or safe_rel != rel or '..' in safe_rel or safe_rel.startswith('/') or '\x00' in safe_rel:
                self.send_json(400, {"error": "invalid path"})
                return
            # Decompose + rebuild via Path(...).name per component — breaks
            # CodeQL's taint flow (Path.name is a recognized path-injection
            # sanitizer). After the regex above, each split('/') component
            # is already [a-zA-Z0-9_.-]+, so this is a functional no-op.
            safe_parts = [Path(p).name for p in safe_rel.split('/') if p]
            if not safe_parts:
                self.send_json(400, {"error": "invalid path"})
                return
            repo_resolved = WORKSPACE_DIR.resolve()  # /media/ serves from workspace (results/, data/, notes/)
            media_path = repo_resolved.joinpath(*safe_parts).resolve()
            if not media_path.is_relative_to(repo_resolved) or not media_path.is_file():
                self.send_json(404, {"error": "not found"})
                return
            # Use a fixed allowlist of safe content types
            SAFE_TYPES = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
                '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg',
                '.wav': 'audio/wav', '.pdf': 'application/pdf', '.json': 'application/json',
                '.txt': 'text/plain', '.html': 'text/html', '.css': 'text/css',
                '.js': 'application/javascript',
            }
            ext = media_path.suffix.lower()
            mime = SAFE_TYPES.get(ext, 'application/octet-stream')
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(media_path.read_bytes())
        elif path == "/logs/voice":
            # Return last 30 lines of voice-agent.log for debugging.
            # Canonical path is logs/voice-agent.log (see startup.sh:153,
            # health-check.py:288, check-pending-questions.py:24). The
            # original src/ path here predated that migration and silently
            # 404'd every /logs/voice request from web-client.ts:2183's
            # "Copy logs" button.
            log_file = WORKSPACE_DIR / "logs" / "voice-agent.log"
            if log_file.exists():
                lines = log_file.read_text().splitlines()[-30:]
                self.send_json(200, {"lines": lines})
            else:
                self.send_json(404, {"error": "voice-agent.log not found"})
        elif path == "/":
            # Serve task submission form (works from phone on same Wi-Fi)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(TASK_FORM.encode())
        else:
            self.send_json(404, {"error": "not found"})

    def check_auth(self) -> bool:
        """Check API token if configured. Returns True if authorized."""
        if not API_TOKEN:
            return True  # No token = no auth required (local use)
        import hmac as _hmac
        token = self.headers.get("Authorization", "").replace("Bearer ", "")
        if _hmac.compare_digest(token, API_TOKEN):
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def send_twiml(self, twiml: str):
        """Send TwiML response for Twilio webhooks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(twiml.encode())

    def handle_twilio_voice(self, form_data: dict):
        """Handle inbound phone call from Twilio webhook."""
        caller = form_data.get("From", ["unknown"])[0]
        call_sid = form_data.get("CallSid", [""])[0]

        # Create a task from the incoming call.
        # source:/from:/call_sid: precede task: so the (Twilio-supplied) caller
        # string can't forge those fields even if it contains newlines.
        # confine_user_content() normalises any \r\n/\r and ZWSP-prefixes
        # header-key lookalike lines — belt-and-suspenders alongside field order.
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        safe_caller = confine_user_content(caller)
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"source: twilio_voice\n"
            f"interaction_type: system_event\n"
            f"access_tier: owner\n"
            f"from: {safe_caller}\n"
            f"call_sid: {call_sid}\n"
            f"task: Incoming phone call from {safe_caller}\n"
        )
        (TASK_DIR / f"{task_id}.txt").write_text(task_content)

        # TwiML: greet caller, record message
        self.send_twiml(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            '<Say voice="alice">Hello, you\'ve reached Sutando. '
            "Please leave a message after the tone and I will get back to you.</Say>"
            '<Record maxLength="120" transcribe="true" '
            f'transcribeCallback="/twilio/transcription"/>'
            '<Say voice="alice">Thank you. Goodbye.</Say>'
            "</Response>"
        )

    def handle_twilio_sms(self, form_data: dict):
        """Handle inbound SMS from Twilio webhook."""
        sender = form_data.get("From", ["unknown"])[0]
        body = form_data.get("Body", [""])[0]

        # Create a task from the SMS. task: is last so newlines in body
        # cannot forge the source:/from: fields that precede it. Body is
        # also run through confine_user_content to defang any ===fence===
        # or header-key line (the fence check is independent of field order).
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        safe_sender = confine_user_content(sender)
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"source: twilio_sms\n"
            f"interaction_type: message\n"
            f"access_tier: owner\n"
            f"from: {safe_sender}\n"
            f"task: SMS from {safe_sender}: {confine_user_content(body)}\n"
        )
        (TASK_DIR / f"{task_id}.txt").write_text(task_content)

        # Reply with acknowledgment
        self.send_twiml(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Message>Got it. Sutando is on it.</Message>"
            "</Response>"
        )

    def handle_twilio_transcription(self, form_data: dict):
        """Handle voicemail transcription callback from Twilio."""
        text = form_data.get("TranscriptionText", [""])[0]
        caller = form_data.get("From", ["unknown"])[0]
        if text:
            task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
            safe_caller = confine_user_content(caller)
            task_content = (
                f"id: {task_id}\n"
                f"timestamp: {datetime.now().isoformat()}\n"
                f"source: twilio_voicemail\n"
                f"interaction_type: message\n"
                f"access_tier: owner\n"
                f"from: {safe_caller}\n"
                f"task: Voicemail from {safe_caller}: {confine_user_content(text)}\n"
            )
            (TASK_DIR / f"{task_id}.txt").write_text(task_content)
        self.send_json(200, {"ok": True})

    def do_POST(self):
        global voice_desired_state
        path = urlparse(self.path).path

        # Twilio webhook endpoints (no auth — Twilio signs requests)
        if path.startswith("/twilio/"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            if not validate_twilio_signature(self, body):
                self.send_json(403, {"error": "invalid Twilio signature"})
                return
            from urllib.parse import parse_qs
            form_data = parse_qs(body)

            if path == "/twilio/voice":
                self.handle_twilio_voice(form_data)
            elif path == "/twilio/sms":
                self.handle_twilio_sms(form_data)
            elif path == "/twilio/transcription":
                self.handle_twilio_transcription(form_data)
            else:
                self.send_json(404, {"error": "not found"})
            return

        if path == "/voice/toggle":
            if not self.check_auth():
                return
            voice_desired_state = "connected" if voice_desired_state == "disconnected" else "disconnected"
            self.send_json(200, {"state": voice_desired_state})
            return

        if path == "/voice/set":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                voice_desired_state = data.get("state", "disconnected")
                self.send_json(200, {"state": voice_desired_state})
            except Exception:
                self.send_json(400, {"error": "invalid"})
            return

        if path == "/delegation/tasks":
            # TaskDelegationService relay backend (#1947): a split-host
            # voice-agent submits a FULLY-SERIALIZED task file body. The
            # writer owns header order (see local_task_protocol's shape
            # taxonomy) — this endpoint only validates the id and stores the
            # bytes, exactly like the local writeFileSync it replaces.
            # Bearer-gated: remote submission is full-capability delegation,
            # so a core with no API_TOKEN configured refuses (unlike the
            # local-dev default elsewhere in this file).
            if not API_TOKEN:
                self.send_json(403, {"error": "delegation requires SUTANDO_API_TOKEN on the core host"})
                return
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                self.send_json(*delegation_submit_task(json.loads(body)))
            except Exception:
                self.send_json(400, {"error": "invalid"})
            return

        if path == "/delegation/archive":
            if not API_TOKEN:
                self.send_json(403, {"error": "delegation requires SUTANDO_API_TOKEN on the core host"})
                return
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                self.send_json(*delegation_archive_result(json.loads(body)))
            except Exception:
                self.send_json(400, {"error": "invalid"})
            return

        if path == "/task-done":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                tid = data.get("taskId", "")
                result = data.get("result", "")
                if tid in task_history:
                    task_history[tid]["status"] = "done"
                    task_history[tid]["result"] = result
                else:
                    task_history[tid] = {"status": "done", "text": result[:80], "time": datetime.now().timestamp(), "result": result}
                self.send_json(200, {"ok": True})
            except Exception:
                self.send_json(400, {"error": "invalid"})
            return

        if path == "/answer":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                qid = data.get("id", "")
                answer = data.get("answer", "")
                if not qid or not answer:
                    self.send_json(400, {"error": "id and answer required"})
                    return
                pq_file = Path(personal_path("pending-questions.md", WORKSPACE_DIR))
                if pq_file.exists():
                    content = pq_file.read_text()
                    # Same parser the ids were minted by — see parse_pending_questions.
                    match = next(
                        (q for q in parse_pending_questions(content) if q["id"] == qid),
                        None,
                    )
                    if match:
                        pq_file.write_text(answer_pending_question(content, match, answer))
                        ts = int(datetime.now().timestamp() * 1000)
                        safe_qid = re.sub(r'[^a-zA-Z0-9_\-.]', '', qid)
                        if safe_qid:
                            # os.path.realpath + str.startswith is the CodeQL-recognized
                            # path-injection sanitizer pair (Path::PathNormalization
                            # + Path::SafeAccessCheck in semmle.python).
                            task_dir_real = os.path.realpath(WORKSPACE_DIR / "tasks")
                            task_file_str = os.path.realpath(
                                os.path.join(task_dir_real, f"answer-{safe_qid}-{ts}.txt")
                            )
                            if task_file_str.startswith(task_dir_real + os.sep):
                                Path(task_file_str).write_text(f"User answered {safe_qid}: {confine_user_content(answer)}")
                        self.send_json(200, {"ok": True, "id": qid, "answer": answer})
                    else:
                        self.send_json(404, {"error": f"question {qid} not found or already answered"})
                else:
                    self.send_json(404, {"error": "no pending questions"})
            except Exception as e:
                self.send_json(400, {"error": str(e)})
            return

        if path != "/task":
            self.send_json(404, {"error": "not found"})
            return

        if not self.check_auth():
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return

        from_agent = data.get("from", "unknown")
        task = data.get("task", "")
        priority = data.get("priority", "normal")

        # Task-file header injection guard. `from_agent` lands on a single
        # line in the task file ("from: <value>\n"). Without sanitization,
        # a `\n` in the value forges extra task-file fields downstream
        # consumers parse line-by-line — e.g. `from_agent =
        # "evil\nchannel_id: local-voice"` makes the task file look
        # voice-originated to `_isVoiceTask` (which scans every line for
        # `channel_id: local-voice`). The misclassif routes the task
        # through the voice-only fallback path with incorrect downstream
        # behavior. Strip line terminators; cap to a sane single-line
        # length.
        from_agent = (
            from_agent.replace("\r", " ").replace("\n", " ").strip()[:120]
            or "unknown"
        )

        if not task:
            self.send_json(400, {"error": "task is required"})
            return

        callback_url = data.get("callback_url", "")

        # Validate callback URL before accepting
        if callback_url:
            safe, reason = _is_safe_callback_url(callback_url)
            if not safe:
                print(f"[api] BLOCKED: callback_url failed SSRF check ({reason}): {callback_url}")
                self.send_json(400, {"error": "callback_url failed validation"})
                return

        # Write to tasks/ for sutando-core to pick up
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        # Write to tasks/ for sutando-core to pick up. Field order matters:
        # `task:` is the LAST line so any newlines in the user-supplied
        # task body just extend the task body rather than forge new
        # task-file fields below. Pre-fix the format was
        # `id, timestamp, task, source, from` — a task body containing
        # `\nsource: voice` would land between the legitimate `source:` and
        # `from:` lines, and `_isVoiceTask` (any-line scan) would treat the
        # task as voice-originated. With `task:` last, the body's newlines
        # have no field to inject into; the file ends with the body.
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"source: api\n"
            f"interaction_type: tool_initiated\n"
            f"access_tier: owner\n"
            f"from: {from_agent}\n"
            f"task: {confine_user_content(task)}\n"
        )
        (TASK_DIR / f"{task_id}.txt").write_text(task_content)

        # Register webhook callback if provided
        if callback_url:
            _webhooks[task_id] = callback_url

        self.send_json(200, {
            "ok": True,
            "task_id": task_id,
            "result_url": f"/result/{task_id}",
            "message": "Task accepted",
        })


TASK_FORM = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sutando</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#0a0a12;color:#e8e8e8;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{max-width:400px;width:100%;background:#12121e;border:1px solid #1e1e30;border-radius:12px;padding:28px}
.header{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.avatar{width:48px;height:48px;border-radius:50%;border:2px solid #4ecca3;object-fit:cover;display:none}
h1{font-size:16px;font-weight:500;color:#fff;margin-bottom:2px}
.sub{font-size:11px;color:#555}
textarea{width:100%;background:#0a0a12;border:1px solid #1e1e30;border-radius:8px;padding:12px;color:#e8e8e8;font-size:14px;font-family:inherit;min-height:100px;resize:vertical;margin-bottom:16px}
textarea:focus{outline:none;border-color:#4ecca3}
button{width:100%;background:#1a2e24;color:#4ecca3;border:1px solid #2a4a36;border-radius:8px;padding:12px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit}
button:hover{background:#243e30}
.result{margin-top:16px;padding:12px;background:#0e1a14;border:1px solid #1a3a26;border-radius:8px;font-size:13px;color:#4ecca3;display:none}
</style></head><body>
<div class="card">
<div class="header">
<img class="avatar" id="avatar" src="/avatar">
<div><h1 id="stand-name">Sutando</h1>
<p class="sub" id="stand-sub">Send a task from any device</p></div>
</div>
<textarea id="task" placeholder="What do you need?"></textarea>
<button onclick="send()">Send Task</button>
<div class="result" id="result"></div>
</div>
<script>
fetch('/stand-identity').then(r=>r.json()).then(s=>{
  if(s.name)document.getElementById('stand-name').textContent='Sutando — '+s.name;
  if(s.avatarGenerated)document.getElementById('avatar').style.display='block';
}).catch(()=>{});
async function send(){
  const task=document.getElementById('task').value.trim();
  if(!task)return;
  const r=await fetch('/task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:'mobile',task})});
  const d=await r.json();
  const el=document.getElementById('result');
  el.textContent=d.ok?'Sent: '+d.task_id:'Error: '+(d.error||'unknown');
  el.style.display='block';
  if(d.ok)document.getElementById('task').value='';
}
</script></body></html>"""


def _resolve_local_ip() -> str:
    """Best-effort LAN IP for the startup log line. An unresolvable hostname
    (e.g. a DHCP-assigned name not in DNS/hosts) must NOT crash startup —
    `socket.gethostbyname(socket.gethostname())` raises gaierror in that case.
    The value is informational only, so fall back to loopback."""
    import socket
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    bind = os.environ.get("AGENT_API_BIND", "127.0.0.1")
    # ThreadingHTTPServer: the single-threaded HTTPServer wedged whenever one
    # client stalled mid-request or a handler ran a slow subprocess/urlopen —
    # every later request hung on a port that still looked open to startup.sh's
    # lsof guard, so nothing restarted it (2026-07-04 incident; same fix as
    # dashboard, #1709).
    server = http.server.ThreadingHTTPServer((bind, PORT), Handler)
    local_ip = _resolve_local_ip()
    print(f"Sutando Agent API → http://{bind}:{PORT}")
    print(f"  POST /task  — submit a task")
    print(f"  GET  /status — health + capabilities")
    print(f"  GET  /ping   — alive check")
    if bind == "127.0.0.1":
        print(f"  (localhost only — set AGENT_API_BIND=0.0.0.0 for LAN access)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
