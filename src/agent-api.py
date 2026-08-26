#!/usr/bin/env python3
"""
Sutando agent API — simple HTTP endpoint for agent-to-agent communication.

Receives tasks from other agents or services, writes them to tasks/
for processing by the cron loop.

Endpoints:
  POST /task              — submit a task (JSON: {from, task, priority?, callback_url?})
  GET  /result/<id>       — poll for task result
  GET  /tasks/history     — authenticated archive-backed task history
  POST /tasks/workstreams/infer — authenticated manual workstream-classifier trigger
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
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote


def _safe_id(raw: str) -> str:
    """Sanitize an ID to prevent path traversal. Only allow alphanumeric, dash, underscore, dot."""
    return re.sub(r'[^a-zA-Z0-9_\-.]', '', raw)


def validate_twilio_signature(handler, body: str) -> bool:
    """Validate X-Twilio-Signature against TWILIO_AUTH_TOKEN.
    Fails closed — returns False when the token is not configured so that
    unauthenticated requests cannot create tasks via the /twilio/* endpoints.
    TWILIO_AUTH_TOKEN must be set in .env for these endpoints to accept webhooks."""
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        return False
    import hmac
    import hashlib
    import base64
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
from git_binary import git_argv  # noqa: E402
from workspace_default import resolve_workspace, status_read_path  # noqa: E402
import local_task_protocol  # noqa: E402
import task_workstreams  # noqa: E402
from task_archive import task_id_from_filename  # noqa: E402

WORKSPACE_DIR = resolve_workspace()
TASK_DIR = WORKSPACE_DIR / "tasks"
TASK_WORKSTREAM_GROUPING_SKILL = REPO_DIR / "skills" / "task-workstream-grouping" / "SKILL.md"
# Overridable so a live-path witness can run against an isolated instance
# instead of restarting the owner's service; mirrors AGENT_API_BIND below.
_PORT_ENV = os.environ.get("AGENT_API_PORT")
if _PORT_ENV is not None and not _PORT_ENV.isdigit():
    # Refusing rather than defaulting: 7843 is the live service, so a typo'd
    # witness port must not silently collide with it.
    raise ValueError(f"AGENT_API_PORT={_PORT_ENV!r} is not a port number")
PORT = int(_PORT_ENV) if _PORT_ENV is not None else 7843

# Personal-asset path resolver — see src/util_paths.py. Imported here so the
# /avatar and /stand-identity endpoints prefer the per-machine private dir
# over the public workspace.
from util_paths import personal_path  # noqa: E402
from pending_questions_md import active_region  # noqa: E402
from task_body_guard import confine_user_content  # noqa: E402
from delivery.readiness import read_ready_result  # noqa: E402


def _emit_task_processed(content: str) -> None:
    """Anonymous, opt-out product telemetry for tasks this API creates —
    relay-voice (POST /delegation/tasks), local web/API chat (the ``api``
    surface), and the Twilio voice/SMS/voicemail surfaces. Mirrors the discord/slack/
    telegram bridges, which emit at their own accept points; those surfaces are
    counted, these weren't. Source is read from the task body's own ``source:``
    header. Fire-and-forget: never blocks or breaks task creation; no-op when
    telemetry is opted out / unconfigured. Never carries task content or ids.
    """
    try:  # pragma: no cover — fire-and-forget glue; logic tested in tests/telemetry.test.py
        from telemetry import task_processed  # sibling module (src/ on sys.path)

        m = re.search(r"^source:\s*(\S+)", content, re.MULTILINE)
        task_processed(m.group(1) if m else "unknown")
    except Exception:  # pragma: no cover — telemetry must never break the API
        pass


# Simple token auth — set SUTANDO_API_TOKEN in .env for remote access security
API_TOKEN = os.environ.get("SUTANDO_API_TOKEN", "")

RESULT_DIR = WORKSPACE_DIR / "results"
TASK_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory task history (survives file cleanup, lost on restart)
# {task_id: {status, text, time, result}}
task_history = {}

# Voice state: "connected" or "disconnected". Toggled via /voice/toggle.
# Web client polls /voice/state and connects/disconnects accordingly.
# The lock serializes /voice/toggle's read-modify-write against concurrent
# /voice/toggle and /voice/set requests — under a threaded server two
# simultaneous toggles can otherwise interleave and land on the wrong state
# (issue #1922). Bare reads of the string stay lock-free.
voice_desired_state = "disconnected"
voice_state_lock = threading.Lock()


def _task_display_fields(content: str) -> tuple[str, str]:
    """Extract the user-visible task text and source from a task file."""
    task_line = ""
    source_line = ""
    for line in content.splitlines():
        if not source_line and line.startswith("source:"):
            source_line = line[7:].strip()
        elif not task_line and line.startswith("task:"):
            task_line = line[5:].strip()
        if task_line and source_line:
            break
    return task_line, source_line


def _task_display_fields_for_id(task_id: str) -> tuple[str, str]:
    task_file = local_task_protocol.find_archived_task(TASK_DIR, task_id)
    if task_file is None:
        return "", ""
    try:
        return _task_display_fields(task_file.read_text())
    except (OSError, UnicodeDecodeError):
        # Same pair readiness.py catches: a partial write mid-character raises
        # UnicodeDecodeError, which is a ValueError and escapes bare OSError.
        return "", ""


def _remember_done_result_file(result_file: Path) -> None:
    task_id = result_file.stem
    # Existence is not doneness: a body still being written is not a result.
    result_content = read_ready_result(result_file)
    if result_content is None:
        return
    task_line, source_line = _task_display_fields_for_id(task_id)
    display_text = task_line or (result_content.split('\n')[0][:80] if result_content else task_id)

    if task_id not in task_history:
        task_history[task_id] = {
            "status": "done",
            "text": display_text,
            "time": result_file.stat().st_mtime,
            "result": result_content,
            "source": source_line,
        }
    else:
        entry = task_history[task_id]
        if entry.get("status") != "done":
            entry["status"] = "done"
            entry["result"] = result_content
        # Repair a fallback entry once the real task text becomes readable.
        # A "done" row created before the task was archived carries the
        # fallback summary (result's first line); backfill the true `task:`
        # text/source on a later poll instead of caching the fallback until
        # restart (#2034 review, qingyun-wu).
        if task_line and entry.get("text") != task_line:
            entry["text"] = task_line
        if source_line and not entry.get("source"):
            entry["source"] = source_line


# --- pending-questions.md ---------------------------------------------------
# ONE parser, shared by GET /status (lists the questions, mints their ids) and
# POST /answer (resolves an id back to a section). They used to walk the file
# separately, and drifted: the reader took the free-form format (post-#1265, no
# **Status:** markers) while the writer still required a **Status:**/**Options:**
# line, so every free-form question was listed but unanswerable — POST /answer
# 404'd on every id. Both paths stay on this function.
PQ_SECTION_RE = re.compile(r'^## ', re.MULTILINE)
PQ_ANSWERED_RE = re.compile(r'\*\*Status:\*\*\s*(resolved|answered|done|complete)', re.IGNORECASE)
PQ_STATUS_RE = re.compile(r'\*\*Status:\*\*.*')
PQ_FIELD_RE = re.compile(r'\*\*(?:Status|Options|Asked|Question):\*\*')
PQ_OPTIONS_RE = re.compile(r'\*\*Options:\*\*\s*(.+)')


def parse_pending_questions(content: str) -> list[dict]:
    """Open questions in pending-questions.md, in file order.

    Ids are derived from the section's own content, not its title or its
    position. The agent rewrites this file continuously, so a positional id
    minted by one GET points at a different — or already-archived — section by
    the time the owner clicks answer on it. Each dict carries the section's
    `start`/`end` offsets into `content` so the writer can splice it in place.

    Hashing the *whole section* (not just the title) is what makes an id stable
    when two open sections share a title. A title-hash plus an occurrence-count
    suffix (`-2`, `-3`) renumbers the survivors as soon as an earlier duplicate
    is answered or reordered, so a stale id the UI still holds would silently
    resolve to a *neighbour* (#2103 review). A content hash is tied to that one
    section: siblings appearing, being answered, or moving around it don't
    change it, so a stale id resolves to its original section — or, if that
    section's own text has since changed, cleanly 404s — but never a neighbour.
    Two sections with an identical title *and* body are the same question and
    share an id by design.

    Sections below the `# Resolved` divider are the audit trail, not open
    questions (same cut as check-pending-questions.py:95).
    """
    active = active_region(content)
    starts = [m.start() for m in PQ_SECTION_RE.finditer(active)]
    questions: list[dict] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(active)
        section = active[start:end]
        head, _, body = section.partition('\n')
        title = head[3:].strip()  # drop the '## '
        if not title or title.startswith('RESOLVED') or title.startswith('[RESOLVED'):
            continue
        if PQ_ANSWERED_RE.search(body):
            continue
        # Whitespace-normalised so a reflow of the same prose keeps the id.
        qid = "Q" + hashlib.sha1(" ".join(section.split()).encode()).hexdigest()[:12]
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


def _active_task_rows() -> list[dict]:
    """Reconcile task/result files into the ten most recent history rows."""
    # Classifier tasks are machinery, not user work, so they stay out of the
    # history the UI shows.
    for task_file in sorted(
        (
            path
            for path in TASK_DIR.glob("*.txt")
            if not path.stem.startswith(
                (
                    task_workstreams.CLASSIFIER_TASK_PREFIX,
                    task_workstreams.LEGACY_CLASSIFIER_TASK_PREFIX,
                )
            )
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:10]:
        # A CLAIMED task is task-{id}.claimed-core-N.txt, but every writer puts
        # the reply at results/{id}.txt — so key AND look up by the canonical id.
        task_id = task_id_from_filename(task_file.name)
        if task_id is None:
            continue
        # Freshest-first over a dir a bridge is writing: never decode strictly.
        content = task_file.read_text(errors="replace")
        # First `source:` and `task:` regardless of field order; body
        # lookalikes must not override the real headers.
        task_line, source_line = _task_display_fields(content)
        existing = task_history.get(task_id, {})
        # One owner for candidates AND readiness. An authoritative `pending`
        # outranks the cache: cache and older candidates are both superseded.
        state, _found, body = local_task_protocol.resolve_result(RESULT_DIR, task_id)
        if state == "ready":
            status = "done"
            result_text = body
        elif state == "pending":
            status = "working"
            result_text = ""
        elif existing.get("status") == "done" or existing.get("result"):
            status = "done"
            result_text = existing.get("result", "")
        else:
            status = "working"
            result_text = ""
        task_history[task_id] = {
            "status": status,
            "text": task_line or existing.get("text", task_id),
            "time": task_file.stat().st_mtime,
            "result": result_text,
            "source": source_line or existing.get("source", ""),
        }

    # Results may outlive their task files after bridge cleanup.
    for result_file in sorted(
        RESULT_DIR.glob("task-*.txt"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:10]:
        _remember_done_result_file(result_file)

    # Reconcile stale rows after the disk scans above.
    import time as _time

    stale_ids = []
    for task_id, task_data in list(task_history.items()):
        if task_data.get("status") != "working":
            continue
        task_file = TASK_DIR / f"{task_id}.txt"
        result_file = RESULT_DIR / f"{task_id}.txt"
        body = read_ready_result(result_file)
        if body is not None:
            task_data["status"] = "done"
            task_data["result"] = body
        elif not task_file.exists() and _time.time() - task_data.get("time", 0) > 300:
            stale_ids.append(task_id)
    for task_id in stale_ids:
        del task_history[task_id]

    recent = sorted(
        task_history.items(), key=lambda item: item[1].get("time", 0), reverse=True
    )[:10]
    rows = [{"id": task_id, **task_data} for task_id, task_data in recent]
    # Join inferred workstream metadata onto the rows; the classifier filter
    # above is the other half of the same feature.
    return task_workstreams.enrich_task_rows(WORKSPACE_DIR, rows)


def _pending_question_rows() -> list[dict]:
    """Return open questions without parser-only splice offsets."""
    pending_file = Path(personal_path("pending-questions.md", WORKSPACE_DIR))
    if not pending_file.exists():
        return []
    return [
        {key: value for key, value in question.items() if key not in ("start", "end")}
        for question in parse_pending_questions(pending_file.read_text())
    ]


def _active_tasks_payload(watcher_ok: bool, core_ok: bool) -> dict:
    """Build the stable response payload for GET /tasks/active."""
    return {
        "tasks": _active_task_rows(),
        "watcher": watcher_ok,
        "claude": core_ok,
        "questions": _pending_question_rows(),
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
    # Defense-in-depth: apply the same id-shape gate as the submit side so
    # both paths enforce the same invariant (#1959). _safe_path handles
    # traversal, but valid_task_id additionally rejects unsupported charsets.
    if not local_task_protocol.valid_task_id(stem):
        return 400, {"error": "invalid result name"}
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
    # Keep the containment check in this function so CodeQL can follow the
    # tainted HTTP value through normalization to the filesystem sink.  The
    # task-id regex alone prevents lexical traversal, but not an existing
    # `<task-id>.txt` symlink that points outside TASK_DIR.
    task_dir_real = os.path.realpath(TASK_DIR)
    task_file_str = os.path.realpath(
        os.path.join(task_dir_real, f"{tid}.txt")
    )
    if not task_file_str.startswith(task_dir_real + os.sep):
        return 400, {"error": "task path escapes task directory"}
    # Do not reopen the validated pathname: a local process could swap in a
    # symlink between the realpath check above and write_text().  Write a
    # private temporary entry through an already-open directory descriptor,
    # then atomically replace the final directory entry.  os.replace replaces
    # a raced symlink itself instead of following it to an outside target.
    task_name = f"{tid}.txt"
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    task_dir_fd = os.open(task_dir_real, dir_flags)
    temp_name = f".{task_name}.{secrets.token_hex(8)}.tmp"
    try:
        try:
            existing = os.stat(task_name, dir_fd=task_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            return 400, {"error": "task path escapes task directory"}

        open_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                      | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_CLOEXEC", 0))
        temp_fd = os.open(temp_name, open_flags, 0o600, dir_fd=task_dir_fd)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        except Exception:
            try:
                os.close(temp_fd)
            except OSError:
                pass
            raise
        os.replace(temp_name, task_name,
                   src_dir_fd=task_dir_fd, dst_dir_fd=task_dir_fd)
        temp_name = ""
    finally:
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=task_dir_fd)
            except FileNotFoundError:
                pass
        os.close(task_dir_fd)
    _emit_task_processed(content)
    return 200, {"ok": True, "task_id": tid}


def delegation_archive_result(data: dict):
    name = str(data.get("name", ""))
    tid = str(data.get("task_id", ""))
    stem = name[:-4] if name.endswith(".txt") else name
    # Identity coherence (Codex P1 on #1956): a relay client may only archive
    # ITS OWN result — the source name must be exactly <task_id>.txt. Without
    # this, any safe name could be filed under any valid id, hijacking other
    # consumers' results into a foreign archive slot.
    if not local_task_protocol.valid_task_id(tid) or stem != tid:
        return 400, {"error": "invalid name/task id, or name does not match task id"}
    # Inline the CodeQL-modeled normalization + prefix guard at both the
    # source and destination sinks.  `_safe_path` enforces the same policy,
    # but CodeQL does not propagate that sanitizer through the helper return.
    result_dir_real = os.path.realpath(RESULT_DIR)
    src_str = os.path.realpath(
        os.path.join(result_dir_real, f"{stem}.txt")
    )
    if not src_str.startswith(result_dir_real + os.sep):
        return 400, {"error": "result path escapes result directory"}
    src = Path(src_str)
    if not os.path.exists(src):
        return 200, {"ok": True, "note": "already gone"}
    # Same destination scheme as task-bridge's archiveFile():
    # results/archive/YYYY-MM/<taskId>.txt. No-clobber: an occupied slot gets
    # the epoch-suffixed name the bridges already use for re-archived results
    # (task-<id>-<epoch>.txt) instead of silently overwriting history.
    dest_dir = local_task_protocol.archive_month_dir(
        RESULT_DIR, datetime.now().isoformat())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_dir_real = os.path.realpath(dest_dir)
    dest_str = os.path.realpath(
        os.path.join(dest_dir_real, f"{tid}.txt")
    )
    if not dest_str.startswith(dest_dir_real + os.sep):
        return 400, {"error": "archive path escapes archive directory"}
    dest = Path(dest_str)
    if dest.exists():
        dest_str = os.path.realpath(os.path.join(
            dest_dir_real, f"{tid}-{int(datetime.now().timestamp())}.txt"
        ))
        if not dest_str.startswith(dest_dir_real + os.sep):
            return 400, {"error": "archive path escapes archive directory"}
        dest = Path(dest_str)
    os.replace(src, dest)
    return 200, {"ok": True}


def get_task_result(task_id: str):
    """Check if a task result exists.

    Candidates are consulted in priority order — live first, then newest
    archive — mirroring local_task_protocol.find_result. The FIRST candidate
    that exists but is not yet readable ENDS the search as `pending`: falling
    past it to an older one answers `completed` with a superseded body, and
    that is terminal, so the client stops polling and the newer answer is
    stranded. `pending` is merely retryable.
    """
    safe_id = _safe_id(task_id)
    if safe_id:
        # Readiness, not existence: a body read mid-write decodes fatally.
        state, _found, body = local_task_protocol.resolve_result(RESULT_DIR, safe_id)
        if state == "ready":
            return {"task_id": safe_id, "status": "completed", "result": body}
        if state == "pending":
            return {"task_id": safe_id, "status": "pending"}
    task_file = _safe_path(TASK_DIR, task_id)
    if task_file and task_file.exists():
        return {"task_id": safe_id, "status": "pending"}
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

    def send_private_json(self, status: int, data: dict):
        """JSON for owner-history routes: no cross-origin read permission."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def check_private_history_auth(self) -> bool:
        """Fail closed for browsers and non-loopback clients without a token."""
        import hmac as _hmac

        if API_TOKEN:
            token = self.headers.get("Authorization", "").replace("Bearer ", "")
            if _hmac.compare_digest(token, API_TOKEN):
                return True
            self.send_private_json(401, {"error": "unauthorized"})
            return False
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            loopback = False
        # The same-origin web-client proxy and local CLI callers send no
        # Origin. A browser page always does for cross-origin fetches, so this
        # also blocks hostile sites from reading localhost history.
        if loopback and not self.headers.get("Origin"):
            return True
        self.send_private_json(403, {"error": "task history requires a local proxy or API token"})
        return False

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
        elif path == "/tasks/history":
            if not self.check_private_history_auth():
                return
            # Reconstruct durable history from immutable live + archived task
            # records, then join inferred workstream metadata from its sidecar.
            # This GET is read-only; inference has a separate authenticated
            # POST trigger used by the same-origin dashboard proxy.
            payload = task_workstreams.task_history_payload(WORKSPACE_DIR, limit=500)
            try:
                inference = task_workstreams.classifier_status(WORKSPACE_DIR)
            except Exception:
                # Workstream inference is optional maintenance. History stays
                # useful when classifier state is temporarily unavailable.
                inference = task_workstreams.ClassifierQueueResult(
                    pending=True, enqueued=False, reason="classifier-unavailable"
                )
            payload["inference"] = {
                "pending": inference.pending,
                "enqueued": inference.enqueued,
                "reason": inference.reason,
                "snapshot_hash": inference.snapshot_hash,
            }
            self.send_private_json(200, payload)
        elif path == "/tasks/active":
            # List active tasks + system status for the web client
            watcher_ok = subprocess.run(["/usr/bin/pgrep", "-f", "watch-tasks"], capture_output=True).returncode == 0
            # Historical response key is `claude`; its meaning is now "selected
            # core CLI is alive" so existing web clients remain compatible.
            try:
                tmux_bin = (shutil.which("tmux") or
                            next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux")
                                  if Path(p).is_file()), None))
                claude_ok = bool(tmux_bin) and subprocess.run(
                    [tmux_bin, "-S", os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock"),
                     "has-session", "-t", "=sutando-core"], capture_output=True,
                ).returncode == 0
            except OSError:
                claude_ok = False
            self.send_json(200, _active_tasks_payload(watcher_ok, claude_ok))
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
            # Owner data: gated like the write leg it belongs to (POST /task) —
            # token checked when configured. NOT delegation's refuse-outright.
            if not self.check_auth():
                return
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
                # git_argv raises GitUnavailable (an OSError) on a host with no
                # runnable git — absorbed by the `except Exception` below, which
                # already degrades this endpoint to "no commit activity". Never
                # hardcode /usr/bin/git: on a Mac without developer tools it is
                # the CLT shim and raises a modal install dialog.
                git_log = subprocess.run(
                    git_argv("-C", str(REPO_DIR), "log", "--oneline", "--since=24 hours ago", "-10"),
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
        _emit_task_processed(task_content)

        # TwiML: greet caller, record message
        self.send_twiml(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            '<Say voice="alice">Hello, you\'ve reached Sutando. '
            "Please leave a message after the tone and I will get back to you.</Say>"
            '<Record maxLength="120" transcribe="true" '
            'transcribeCallback="/twilio/transcription"/>'
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
        _emit_task_processed(task_content)

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
            _emit_task_processed(task_content)
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
            with voice_state_lock:
                voice_desired_state = "connected" if voice_desired_state == "disconnected" else "disconnected"
                new_state = voice_desired_state
            self.send_json(200, {"state": new_state})
            return

        if path == "/voice/set":
            if not self.check_auth():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                with voice_state_lock:
                    voice_desired_state = data.get("state", "disconnected")
                    new_state = voice_desired_state
                self.send_json(200, {"state": new_state})
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
                # Only genuine task-* ids belong in the Task list. voice-* and
                # proactive-* files are notification channels, not tasks (#1786).
                if not tid.startswith("task-"):
                    self.send_json(200, {"ok": True})
                    return
                if tid in task_history:
                    task_history[tid]["status"] = "done"
                    task_history[tid]["result"] = result
                else:
                    task_line, source_line = _task_display_fields_for_id(tid)
                    task_history[tid] = {
                        "status": "done",
                        "text": task_line or result[:80],
                        "time": datetime.now().timestamp(),
                        "result": result,
                        "source": source_line,
                    }
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

        if path == "/tasks/workstreams/infer":
            if not self.check_private_history_auth():
                return
            try:
                queued = task_workstreams.maybe_enqueue_classifier_task(
                    WORKSPACE_DIR, skill_file=TASK_WORKSTREAM_GROUPING_SKILL
                )
                self.send_private_json(200, {
                    "pending": queued.pending,
                    "enqueued": queued.enqueued,
                    "reason": queued.reason,
                    "snapshot_hash": queued.snapshot_hash,
                })
            except Exception:
                self.send_private_json(503, {"error": "task workstream classifier unavailable"})
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
        _emit_task_processed(task_content)

        # Dashboard replies name their parent task explicitly. Give those
        # follow-ups the same workstream immediately; unrelated new tasks remain
        # ungrouped until the idle classifier can inspect them.
        parent_match = re.fullmatch(r"web-reply:(task-[a-zA-Z0-9_.-]+)", from_agent)
        if parent_match:
            task_workstreams.inherit_assignment(WORKSPACE_DIR, task_id, parent_match.group(1))

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
    workstream_maintenance_stop = threading.Event()
    workstream_maintenance = threading.Thread(
        target=task_workstreams.run_classifier_maintenance,
        kwargs={
            "workspace": WORKSPACE_DIR,
            "skill_file": TASK_WORKSTREAM_GROUPING_SKILL,
            "stop_event": workstream_maintenance_stop,
        },
        name="task-workstream-maintenance",
        daemon=True,
    )
    workstream_maintenance.start()
    local_ip = _resolve_local_ip()
    print(f"Sutando Agent API → http://{bind}:{PORT}")
    print("  POST /task  — submit a task")
    print("  GET  /status — health + capabilities")
    print("  GET  /ping   — alive check")
    if bind == "127.0.0.1":
        print("  (localhost only — set AGENT_API_BIND=0.0.0.0 for LAN access)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        workstream_maintenance_stop.set()
        workstream_maintenance.join(timeout=1)
        server.server_close()
