#!/usr/bin/env python3
"""
remote-task-client.py — generic client that bridges a REMOTE task relay to the
local Sutando file queue, so the local core processes remote tasks unchanged.

This is the OPEN, provider-agnostic half of the "agent as a service" design: a
relay service holds the platform connection and
exposes a tiny HTTP protocol; this client pulls *your* tasks down into the local
`tasks/` queue and pushes results back up. No provider-specific logic lives here
— that's the relay's job.

Protocol (versioned, Bearer-auth):
  GET  {REMOTE_TASK_URL}/v1/tasks?wait=<sec>
       → 200 {"tasks": [ {<task fields...>}, ... ]}   (long-poll; [] on timeout)
  POST {REMOTE_TASK_URL}/v1/results
       → body {"id": "<task-id>", "body": "<result text>"}  → 200 on accepted

Each task object uses the same schema Sutando's other bridges write, so this
client just serializes it to `tasks/task-<id>.txt` and the core handles it like
any Discord/Telegram/Slack task. When `results/task-<id>.txt` appears, its body
is POSTed back and the result file is archived.

Config (env / .env):
  AG2_REMOTE_TOKEN      the onboarding string — the ONLY required setting
                        (combined "https://<relay>|<secret>" or a bare secret)
  AG2_REMOTE_URL        relay base URL (only needed with a bare secret)
  REMOTE_TASK_URL/_TOKEN  legacy aliases
  REMOTE_TASK_PROVIDER  label used for the task `source:` field (default "remote")
  REMOTE_TASK_POLL_WAIT long-poll seconds (default 25)

Stdlib only (urllib) — no new dependencies.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# resolve_workspace lives alongside this file in src/.
# Coupled-skill import: this skill ships in the main repo, so use the
# canonical src/ helper rather than a vendored copy (avoids silent drift).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WS = resolve_workspace()
TASKS_DIR = WS / "tasks"
RESULTS_DIR = WS / "results"
ARCHIVE_RESULTS_DIR = RESULTS_DIR / "archive"
# Persist the in-flight set (tasks pulled from the relay, awaiting result-POST)
# so a client restart between pull and POST doesn't strand the result. Scoped to
# relay-pulled tasks only — we must NOT blindly POST every results/ file, or we'd
# cross-send other channels' (Discord/Telegram) results to the relay.
INFLIGHT_FILE = WS / "state" / "remote-task-inflight.json"

# One-token onboarding: AG2_REMOTE_TOKEN alone is enough. The onboarding
# string may be the combined "https://<relay>|<secret>" form (the URL travels
# inside the token — nothing service-specific lives in this repo); a bare
# secret needs AG2_REMOTE_URL/REMOTE_TASK_URL alongside it.
_RAW = os.environ.get("AG2_REMOTE_TOKEN") or os.environ.get("REMOTE_TASK_TOKEN") or ""
if "|" in _RAW:
    _URL_FROM_TOKEN, TOKEN = _RAW.split("|", 1)
else:
    _URL_FROM_TOKEN, TOKEN = "", _RAW
URL = (os.environ.get("AG2_REMOTE_URL") or os.environ.get("REMOTE_TASK_URL")
       or _URL_FROM_TOKEN).rstrip("/")
PROVIDER = os.environ.get("REMOTE_TASK_PROVIDER") or "remote"
POLL_WAIT = int(os.environ.get("REMOTE_TASK_POLL_WAIT") or "25")

_TASK_FIELDS = ("id", "timestamp", "task", "source", "channel_id",
                "source_message_id", "user_id", "priority")

# Trust tier is a LOCAL decision (review 2026-06-13): the relay is outside
# this machine's trust boundary, so its access_tier claim is ignored. The
# tier written to every task file comes from AG2_REMOTE_TIER in .env —
# default "team" (sandboxed processing). Operators who own their relay can
# explicitly set AG2_REMOTE_TIER=owner.
LOCAL_TIER = (os.environ.get("AG2_REMOTE_TIER") or "team").strip().lower()
if LOCAL_TIER not in ("owner", "team", "other"):
    LOCAL_TIER = "team"


# Blocker (review 2026-06-13): the relay is untrusted, so a task `id` flows
# into filesystem paths (task write + result read-back/POST). Reject anything
# that isn't a plain slug — kills path traversal in both directions.
_TID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _valid_tid(tid: str) -> bool:
    return bool(_TID_RE.fullmatch(tid)) and tid not in (".", "..")


def _one_line(value) -> str:
    """Header-safe single-line value: CR/LF stripped so a relay-controlled
    field can't inject extra `key: value` lines (e.g. forge a second
    access_tier). Applied to every field — task content is single-line in
    practice and a stray newline only ever indicates an injection attempt."""
    return str(value).replace("\r", " ").replace("\n", " ")


def _log(msg: str) -> None:
    print(f"[remote-task-client] {msg}", flush=True)


def _req(method: str, path: str, payload: dict | None = None, timeout: int = 35):
    """One authenticated HTTP request. Returns parsed JSON (or {} for empty)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{URL}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/json")
    # CloudFlare bot-fight (error 1010) rejects python-urllib's default
    # User-Agent with a 403; send an explicit client UA so the relay's edge
    # lets the long-poll through. (Same fix the other relay callers carry.)
    req.add_header("User-Agent", "sutando-relay-client/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode().strip()
        return json.loads(raw) if raw else {}


def _write_task(task: dict) -> str | None:
    """Serialize a relay task into tasks/task-<id>.txt (same schema as bridges).
    Returns the task id, or None if it has no id / already present."""
    tid = str(task.get("id") or "").strip()
    if not tid:
        _log("dropping task with no id")
        return None
    if not _valid_tid(tid):
        _log(f"dropping task with unsafe id {tid!r}")
        return None
    dest = TASKS_DIR / f"{tid}.txt"
    # Idempotent: don't re-write a task already queued, claimed, or archived.
    if dest.exists() or any(TASKS_DIR.glob(f"{tid}.claimed-*")):
        return tid
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for f in _TASK_FIELDS:
        if f == "source":
            lines.append(f"source: {_one_line(task.get('source') or PROVIDER)}")
        elif f in task and task[f] not in (None, ""):
            lines.append(f"{f}: {_one_line(task[f])}")
    # access_tier is a LOCAL decision and written LAST so it wins even under a
    # last-occurrence parser; every other field is newline-stripped so none can
    # forge an earlier one either.
    lines.append(f"access_tier: {LOCAL_TIER}")
    tmp = dest.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.rename(dest)  # atomic publish so the watcher never sees a partial file
    _log(f"queued {tid}")
    return tid


def _archive_result(path: Path, tid: str) -> None:
    ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path.rename(ARCHIVE_RESULTS_DIR / f"{tid}-{int(time.time())}.txt")
    except OSError:
        path.unlink(missing_ok=True)


def _load_inflight() -> set[str]:
    """Restore the in-flight set from disk (fail-open to empty)."""
    try:
        data = json.loads(INFLIGHT_FILE.read_text())
        return {str(t) for t in data} if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001
        _log(f"inflight file unreadable ({e}) — starting empty")
        return set()


def _save_inflight(inflight: set[str]) -> None:
    """Atomically persist the in-flight set. Best-effort (never blocks the loop)."""
    try:
        INFLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = INFLIGHT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(inflight)))
        tmp.rename(INFLIGHT_FILE)
    except Exception as e:  # noqa: BLE001
        _log(f"inflight persist failed ({e}) — continuing")


def _post_ready_results(inflight: set[str]) -> None:
    """For each in-flight task, if its result file exists, POST it + archive."""
    changed = False
    for tid in list(inflight):
        if not _valid_tid(tid):  # defense-in-depth: never read an unsafe path
            inflight.discard(tid); changed = True
            continue
        rfile = RESULTS_DIR / f"{tid}.txt"
        if not rfile.exists():
            continue
        body = rfile.read_text().strip()
        # Result-body protocol markers are a local bridge concern — never ship
        # them to the relay. [no-send]/[deduped:] mean "no user-facing reply":
        # archive without POSTing (match the other bridges' semantics).
        low = body.lower()
        if low.startswith("[no-send]") or low.startswith("[deduped:"):
            _archive_result(rfile, tid)
            inflight.discard(tid)
            changed = True
            _log(f"archived {tid} (marker, not sent)")
            continue
        try:
            _req("POST", "/v1/results", {"id": tid, "body": body})
        except urllib.error.HTTPError as e:
            _log(f"result POST failed for {tid}: HTTP {e.code} — will retry")
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            _log(f"result POST network error for {tid}: {e} — will retry")
            continue
        _archive_result(rfile, tid)
        inflight.discard(tid)
        changed = True
        _log(f"delivered result for {tid}")
    if changed:
        _save_inflight(inflight)


def main() -> None:
    if not URL or not TOKEN:
        sys.exit("FATAL: set AG2_REMOTE_TOKEN (and AG2_REMOTE_URL if your token is a bare secret).")
    inflight: set[str] = _load_inflight()
    _log(f"starting — relay={URL} provider={PROVIDER} workspace={WS} "
         f"(restored {len(inflight)} in-flight)")
    backoff = 1
    while True:
        try:
            resp = _req("GET", f"/v1/tasks?wait={POLL_WAIT}", timeout=POLL_WAIT + 10)
            added = False
            for task in resp.get("tasks", []):
                tid = _write_task(task)
                if tid and tid not in inflight:
                    inflight.add(tid)
                    added = True
            if added:
                _save_inflight(inflight)
            _post_ready_results(inflight)
            backoff = 1  # healthy round-trip → reset backoff
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit(f"FATAL: relay auth rejected (HTTP {e.code}) — check AG2_REMOTE_TOKEN.")
            _log(f"poll HTTP {e.code} — backing off {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 60)
        except (urllib.error.URLError, TimeoutError) as e:
            _log(f"poll network error: {e} — backing off {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 60)
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            _log(f"unexpected: {e} — backing off {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
