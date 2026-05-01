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

import http.server
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


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


REPO_DIR = Path(__file__).parent.parent
TASK_DIR = REPO_DIR / "tasks"
PORT = 7843

# Personal-asset path resolver — see src/util_paths.py. Imported here so the
# /avatar and /stand-identity endpoints prefer the per-machine private dir
# over the public workspace.
sys.path.insert(0, str(Path(__file__).parent))
from util_paths import personal_path  # noqa: E402

# Simple token auth — set SUTANDO_API_TOKEN in .env for remote access security
API_TOKEN = os.environ.get("SUTANDO_API_TOKEN", "")

RESULT_DIR = REPO_DIR / "results"
TASK_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

# In-memory task history (survives file cleanup, lost on restart)
# {task_id: {status, text, time, result}}
task_history = {}

# Voice state: "connected" or "disconnected". Toggled via /voice/toggle.
# Web client polls /voice/state and connects/disconnects accordingly.
voice_desired_state = "disconnected"



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


def get_task_result(task_id: str):
    """Check if a task result exists."""
    result_file = _safe_path(RESULT_DIR, task_id)
    if result_file and result_file.exists():
        return {"task_id": _safe_id(task_id), "status": "completed", "result": result_file.read_text()}
    task_file = _safe_path(TASK_DIR, task_id)
    if task_file and task_file.exists():
        return {"task_id": _safe_id(task_id), "status": "pending"}
    return None


# Store webhook callbacks for tasks
_webhooks: dict[str, str] = {}


def fire_webhook(task_id: str, result: str) -> None:
    """POST result to registered webhook URL."""
    url = _webhooks.pop(task_id, None)
    if not url:
        return
    try:
        import urllib.request
        data = json.dumps({"task_id": task_id, "status": "completed", "result": result}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Best-effort delivery


class Handler(http.server.BaseHTTPRequestHandler):
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
            status_file = REPO_DIR / "core-status.json"
            if status_file.exists():
                import json as _json
                try:
                    data = _json.loads(status_file.read_text())
                    self.send_json(200, data)
                except:
                    self.send_json(200, {"status": "idle"})
            else:
                self.send_json(200, {"status": "idle"})
        elif path == "/voice/state":
            self.send_json(200, {"state": voice_desired_state})
        elif path == "/status":
            self.send_json(200, get_status())
        elif path == "/tasks/active":
            # List active tasks + system status for the web client
            watcher_ok = subprocess.run(["pgrep", "-f", "watch-tasks"], capture_output=True).returncode == 0
            claude_ok = subprocess.run(["pgrep", "-f", "claude.*sutando-core"], capture_output=True).returncode == 0
            # Scan disk for active tasks, update history (preserve existing text)
            for f in sorted(TASK_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                task_id = f.stem
                content = f.read_text()
                task_line = ""
                for line in content.splitlines():
                    if line.startswith("task:"):
                        task_line = line[5:].strip()
                        break
                result_file = RESULT_DIR / f.name
                status = "done" if result_file.exists() else "working"
                result_text = result_file.read_text().strip() if result_file.exists() else ""
                existing = task_history.get(task_id, {})
                task_history[task_id] = {"status": status, "text": task_line or existing.get("text", task_id), "time": f.stat().st_mtime, "result": result_text or existing.get("result", "")}
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
            # Parse pending questions
            questions = []
            pq_file = Path(personal_path("pending-questions.md", REPO_DIR))
            if pq_file.exists():
                import re
                content = pq_file.read_text()
                # Split into sections by ## headers
                sections = re.split(r'^## ', content, flags=re.MULTILINE)
                for i, section in enumerate(sections):
                    if not section.strip():
                        continue
                    lines = section.strip().split('\n')
                    title = lines[0].strip()
                    body = '\n'.join(lines[1:])
                    # Skip preamble (sections without question metadata)
                    if '**Status:**' not in body and '**Options:**' not in body:
                        continue
                    # Skip resolved/answered questions
                    if re.search(r'\*\*Status:\*\*\s*(resolved|answered|done|complete)', body, re.IGNORECASE):
                        continue
                    # Extract question text — use body before first metadata field
                    q_text = re.split(r'\*\*(?:Status|Options|Asked|Question):\*\*', body)[0].strip()
                    q_text = q_text if q_text else title
                    q = {"id": f"Q{i}", "text": title, "detail": q_text}
                    # Parse custom options if present
                    opts_match = re.search(r'\*\*Options:\*\*\s*(.+)', body)
                    if opts_match:
                        q["options"] = [o.strip() for o in opts_match.group(1).split("|")]
                    questions.append(q)
            self.send_json(200, {"tasks": tasks, "watcher": watcher_ok, "claude": claude_ok, "questions": questions})
        elif path.startswith("/result/"):
            task_id = path[len("/result/"):]
            result = get_task_result(task_id)
            if result:
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "task not found"})
        elif path == "/avatar":
            avatar_file = personal_path("stand-avatar.png", workspace=REPO_DIR)
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
            si_file = personal_path("stand-identity.json", workspace=REPO_DIR)
            data = json.loads(si_file.read_text()) if si_file.exists() else {}
            self.send_json(200, data)
        elif path == "/activity":
            # Recent activity: git commits + processed tasks
            activity = []
            try:
                git_log = subprocess.run(
                    ["git", "-C", str(REPO_DIR), "log", "--oneline", "--since=24 hours ago", "-10"],
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
                results_dir = REPO_DIR / "results"
                result_files = sorted(results_dir.glob("task-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
                for f in result_files:
                    content = f.read_text()[:200]
                    activity.append({"type": "task", "id": f.stem, "preview": content.split("\n")[0]})
            except Exception:
                pass
            self.send_json(200, {"activity": activity})
        elif path == "/contextual-chips":
            chips_file = REPO_DIR / "contextual-chips.json"
            if chips_file.exists():
                try:
                    data = json.loads(chips_file.read_text())
                    self.send_json(200, data)
                except Exception:
                    self.send_json(200, {"chips": []})
            else:
                self.send_json(200, {"chips": []})
        elif path == "/dynamic-content":
            dc_file = REPO_DIR / "dynamic-content.json"
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
            repo_resolved = REPO_DIR.resolve()
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
            # Return last 30 lines of voice-agent.log for debugging
            log_file = REPO_DIR / "src" / "voice-agent.log"
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

        # Create a task from the incoming call
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"task: Incoming phone call from {caller}\n"
            f"source: twilio_voice\n"
            f"from: {caller}\n"
            f"call_sid: {call_sid}\n"
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

        # Create a task from the SMS
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        task_content = (
            f"id: {task_id}\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"task: SMS from {sender}: {body}\n"
            f"source: twilio_sms\n"
            f"from: {sender}\n"
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
            task_content = (
                f"id: {task_id}\n"
                f"timestamp: {datetime.now().isoformat()}\n"
                f"task: Voicemail from {caller}: {text}\n"
                f"source: twilio_voicemail\n"
                f"from: {caller}\n"
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
            except:
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
            except:
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
                pq_file = Path(personal_path("pending-questions.md", REPO_DIR))
                if pq_file.exists():
                    content = pq_file.read_text()
                    # Update status from unanswered to answered
                    import re
                    safe_answer = answer.replace('\n', ' ')
                    # Try new format: - **Status:** unanswered
                    pattern = rf'(## [^\n]*\n(?:.*?\n)*?- \*\*Status:\*\* )unanswered'
                    # Find the right section by matching the question ID
                    sections = re.split(r'(^## )', content, flags=re.MULTILINE)
                    new_content = content
                    # Reconstruct and find the section matching this qid
                    idx = 0
                    for si, section in enumerate(re.split(r'^## ', content, flags=re.MULTILINE)):
                        if not section.strip():
                            continue
                        lines = section.strip().split('\n')
                        title = lines[0].strip()
                        body = '\n'.join(lines[1:])
                        if '**Status:**' not in body and '**Options:**' not in body:
                            continue
                        if re.search(r'\*\*Status:\*\*\s*(resolved|answered|done|complete)', body, re.IGNORECASE):
                            continue
                        idx += 1
                        if f"Q{si}" == qid:
                            # Match any waiting/unanswered status line
                            new_body = re.sub(
                                r'\*\*Status:\*\*\s*(?:Waiting|unanswered).*',
                                f'**Status:** Answered — {safe_answer}',
                                body
                            )
                            if new_body != body:
                                new_content = content.replace(body, new_body)
                            break
                    if new_content != content:
                        pq_file.write_text(new_content)
                        ts = int(datetime.now().timestamp() * 1000)
                        safe_qid = re.sub(r'[^a-zA-Z0-9_\-.]', '', qid)
                        if safe_qid:
                            # os.path.realpath + str.startswith is the CodeQL-recognized
                            # path-injection sanitizer pair (Path::PathNormalization
                            # + Path::SafeAccessCheck in semmle.python).
                            task_dir_real = os.path.realpath(REPO_DIR / "tasks")
                            task_file_str = os.path.realpath(
                                os.path.join(task_dir_real, f"answer-{safe_qid}-{ts}.txt")
                            )
                            if task_file_str.startswith(task_dir_real + os.sep):
                                Path(task_file_str).write_text(f"User answered {safe_qid}: {answer}")
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

        if not task:
            self.send_json(400, {"error": "task is required"})
            return

        callback_url = data.get("callback_url", "")

        # Write to tasks/ for sutando-core to pick up
        task_id = f"task-{int(datetime.now().timestamp() * 1000)}"
        task_content = f"id: {task_id}\ntimestamp: {datetime.now().isoformat()}\ntask: {task}\nsource: api\nfrom: {from_agent}\n"
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


if __name__ == "__main__":
    bind = os.environ.get("AGENT_API_BIND", "127.0.0.1")
    server = http.server.HTTPServer((bind, PORT), Handler)
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
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
