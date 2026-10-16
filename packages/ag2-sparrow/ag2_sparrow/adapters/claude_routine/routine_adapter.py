#!/usr/bin/env python3
"""Forward AG2 Space inbox tasks to a Claude Code cloud routine through its API trigger.

Each `tasks/task-*.txt` becomes one `POST /v1/claude_code/routines/<trig_id>/fire`. The
routine's saved prompt (routine-prompt.md) posts the reply into the room itself, so the
result file written here is `[no-send]` plus the session URL as a comment; `--result-mode
echo` writes a visible "Sent to the cloud agent: <url>" instead. One fire in flight, one
adapter per work dir. The bearer token is read from the env var named by `--token-env`.
Stdlib only.
"""
import argparse
import email.utils
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from ..codex.codex_connector import (acquire_lock, load_done, non_owner_note, parse_task,
                                     record_done, write_atomic)

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
TEXT_MAX = 65536
ROUTINE_CAP_PER_HOUR = 30
TRIGGER_RE = re.compile(r"trig_[A-Za-z0-9]+")
TRUNCATED = "\n[truncated]"
HOUR = 3600.0


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def _field(value):
    # Header values go on the payload's first line, so no whitespace may split or end it.
    return re.sub(r"\s+", "", value or "")


def build_payload(task_id, headers, message, operator_lines=(), note=None):
    """First line: the attested routing fields. Then, for a non-owner tier, the note; then
    the untrusted body. Truncated to the endpoint's limit."""
    tier = headers.get("access_tier") or "guest"
    collaborator = tier == "team" and headers.get("collaborator") == "true"
    room = _field(headers.get("channel_id")) or _field(headers.get("source_room_id"))
    thread = _field(headers.get("thread_root")) or "none"
    head = "room=%s thread=%s tier=%s collaborator=%s task_id=%s\n" % (
        room, thread, tier, "true" if collaborator else "false", _field(task_id))
    if tier != "owner":
        head += "note: %s\n" % (note if note is not None else non_owner_note())
    body = message + ("\n" + "\n".join(operator_lines) if operator_lines else "")
    text = head + body
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX - len(TRUNCATED)] + TRUNCATED
    return room, text


def retry_after_seconds(value, now=None, default=60.0):
    """Retry-After as delta-seconds or an HTTP date, clamped to [1 s, 1 h]."""
    secs = default
    if value:
        try:
            secs = float(value)
        except ValueError:
            try:
                secs = email.utils.parsedate_to_datetime(value).timestamp() - (now or time.time())
            except (TypeError, ValueError):
                secs = default
    return min(max(secs, 1.0), HOUR)


class Outcome:
    """kind: ok | rate_limited | rejected | retry."""

    def __init__(self, kind, status=None, url=None, session_id=None, retry_after=None, detail=""):
        self.kind, self.status, self.url = kind, status, url
        self.session_id, self.retry_after, self.detail = session_id, retry_after, detail


def _error_type(raw):
    try:
        return str(json.loads(raw.decode("utf-8", "replace"))["error"]["type"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return "error"


def fire(endpoint, token, text, timeout=30.0, opener=urllib.request.urlopen):
    req = urllib.request.Request(endpoint, data=json.dumps({"text": text}).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + token, "anthropic-version": API_VERSION,
                                          "Content-Type": "application/json"})
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        etype = _error_type(e.read())
        if e.code == 429:
            return Outcome("rate_limited", 429, retry_after=retry_after_seconds(e.headers.get("Retry-After")),
                           detail=etype)
        if 400 <= e.code < 500:
            return Outcome("rejected", e.code, detail=etype)
        return Outcome("retry", e.code, detail=etype)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return Outcome("retry", detail=type(e).__name__)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # A 200 created a session even when its body is unreadable: never fire this task again.
    return Outcome("ok", 200, url=data.get("claude_code_session_url"), session_id=data.get("claude_code_session_id"))


class RateLimiter:
    """Sliding one-hour window of fire times, kept in a file so a restart does not reset it."""

    def __init__(self, path, per_hour, clock=time.time):
        self.path, self.per_hour, self.clock = path, per_hour, clock
        try:
            with open(path, encoding="utf-8") as f:
                self.times = [float(t) for t in json.load(f)]
        except (OSError, ValueError, TypeError):
            self.times = []

    def _prune(self):
        now = self.clock()
        self.times = [t for t in self.times if now - t < HOUR]

    def wait_seconds(self):
        self._prune()
        if len(self.times) < self.per_hour:
            return 0.0
        return max(min(self.times) + HOUR - self.clock(), 0.0)

    def record(self):
        self._prune()
        self.times.append(self.clock())
        write_atomic(self.path, json.dumps(self.times))


def result_body(outcome, mode):
    url = outcome.url or "unknown"
    if outcome.kind == "ok":
        if mode == "echo":
            return "Sent to the cloud agent: %s\n" % url
        return "[no-send]\n# claude routine session: %s\n" % url
    if outcome.kind == "rejected":
        return "The cloud agent could not take this message (HTTP %s, %s).\n" % (outcome.status, outcome.detail)
    return ("The cloud agent could not be reached for this message after several tries (%s).\n"
            % (outcome.status or outcome.detail))


NO_ROOM = "This message has no room to reply in, so it was not sent to the cloud agent.\n"


class Adapter:
    def __init__(self, args, token, opener=urllib.request.urlopen, clock=time.time, sleep=time.sleep, note=None):
        self.args, self.token, self.opener = args, token, opener
        self.clock, self.sleep = clock, sleep
        self.note = note if note is not None else non_owner_note()
        self.endpoint = "%s/v1/claude_code/routines/%s/fire" % (args.api_base.rstrip("/"), args.trigger_id)
        self.limiter = RateLimiter(os.path.join(args.state, "fires.json"), args.max_per_hour, clock)
        self.done_path = os.path.join(args.state, "done")
        self.attempts = {}     # task id -> failed attempts (5xx / network)
        self.not_before = {}   # task id -> earliest next try
        self.blocked_until = 0.0

    def pending(self, seen):
        names = [n for n in os.listdir(self.args.inbox)
                 if n.startswith("task-") and n.endswith(".txt") and n[:-4] not in seen
                 and not os.path.exists(os.path.join(self.args.results, n))]
        names.sort(key=lambda n: (os.stat(os.path.join(self.args.inbox, n)).st_mtime, n))
        return names

    def finish(self, name, body, seen):
        write_atomic(os.path.join(self.args.results, name), body)
        record_done(self.done_path, name[:-4])
        seen.add(name[:-4])
        self.attempts.pop(name[:-4], None)
        self.not_before.pop(name[:-4], None)

    def step(self, name, seen):
        """Handle one task; returns True when it reached a final result."""
        task_id = name[:-4]
        headers, message, operator_lines = parse_task(os.path.join(self.args.inbox, name))
        room, text = build_payload(task_id, headers, message, operator_lines, self.note)
        if not room:
            log("no room ->", task_id)
            self.finish(name, NO_ROOM, seen)
            return True
        log("fire ->", task_id, headers.get("access_tier"), "chars", len(text))
        out = fire(self.endpoint, self.token, text, self.args.timeout, self.opener)
        if out.kind == "rate_limited":
            self.blocked_until = self.clock() + out.retry_after
            log("429; holding all fires for %.0fs" % out.retry_after)
            return False
        self.limiter.record()
        if out.kind == "retry":
            n = self.attempts.get(task_id, 0) + 1
            self.attempts[task_id] = n
            if n <= self.args.max_retries:
                delay = min(self.args.backoff * (2 ** (n - 1)), self.args.backoff_max)
                self.not_before[task_id] = self.clock() + delay
                log("retry %d/%d for %s in %.1fs (%s)" % (n, self.args.max_retries, task_id, delay,
                                                         out.status or out.detail))
                return False
        log("result <-", task_id, out.kind, out.status, out.session_id or "")
        self.finish(name, result_body(out, self.args.result_mode), seen)
        return True

    def next_ready(self, names):
        now = self.clock()
        if now < self.blocked_until:
            return None, self.blocked_until - now
        ready = [n for n in names if self.not_before.get(n[:-4], 0) <= now]
        if not ready:
            return None, min(self.not_before[n[:-4]] for n in names) - now
        wait = self.limiter.wait_seconds()
        if wait > 0:
            return None, wait
        return ready[0], 0.0

    def serve(self):
        seen = set(load_done(self.done_path))
        done = 0
        idle_since = self.clock()
        while True:
            names = self.pending(seen)
            if not names:
                if self.args.once and done >= self.args.once:
                    return
                if self.args.idle_exit and self.clock() - idle_since > self.args.idle_exit:
                    log("idle-exit")
                    return
                self.sleep(self.args.poll)
                continue
            name, wait = self.next_ready(names)
            if name is None:
                self.sleep(min(max(wait, 0.01), self.args.poll * 50))
                continue
            if self.step(name, seen):
                done += 1
                if self.args.once and done >= self.args.once:
                    return
            idle_since = self.clock()


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="dir holding tasks/ results/ state/")
    ap.add_argument("--trigger-id", required=True, help="the routine's trig_... id from its API trigger URL")
    ap.add_argument("--token-env", default="CLAUDE_ROUTINE_TOKEN",
                    help="name of the env var holding the routine's bearer token (the token is never an argument)")
    ap.add_argument("--api-base", default=API_BASE)
    ap.add_argument("--result-mode", choices=["no-send", "echo"], default="no-send")
    ap.add_argument("--max-per-hour", type=int, default=25,
                    help="local fire cap, 1-%d (the routine's own cap)" % ROUTINE_CAP_PER_HOUR)
    ap.add_argument("--max-retries", type=int, default=5, help="5xx/network retries per task")
    ap.add_argument("--backoff", type=float, default=2.0, help="first retry delay, doubled per retry")
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout per fire")
    ap.add_argument("--once", type=int, default=0, help="exit after N tasks")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--poll", type=float, default=1.0)
    return ap


def main(argv=None, environ=None, opener=urllib.request.urlopen):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not TRIGGER_RE.fullmatch(args.trigger_id):
        ap.error("--trigger-id must look like trig_...")
    if not 1 <= args.max_per_hour <= ROUTINE_CAP_PER_HOUR:
        ap.error("--max-per-hour must be 1-%d" % ROUTINE_CAP_PER_HOUR)
    token = (os.environ if environ is None else environ).get(args.token_env, "").strip()
    if not token:
        sys.stderr.write("refusing to start: env var %s is empty or unset\n" % args.token_env)
        return 2
    args.work = os.path.abspath(args.work)
    args.inbox = os.path.join(args.work, "tasks")
    args.results = os.path.join(args.work, "results")
    args.state = os.path.join(args.work, "state")
    for d in (args.inbox, args.results, args.state):
        os.makedirs(d, exist_ok=True)
    lock = acquire_lock(args.state)
    if lock is None:
        sys.stderr.write("another adapter holds %s (state/connector.pid is alive)\n" % args.work)
        return 3
    try:
        Adapter(args, token, opener).serve()
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
