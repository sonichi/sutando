#!/usr/bin/env python3
"""Queue AG2 Space inbox tasks as turns in one persistent Claude Code cloud session.

Each `tasks/task-*.txt` becomes one `claude -p <text> --cloud <session_id> --output-format json`.
The CLI returns as soon as the turn is queued; the session (set up once with session-prompt.md
and the AG2 Space connector) posts the reply into the room itself, so the result file written
here is `[no-send]` plus the session URL as a comment; `--result-mode echo` writes a visible
"Sent to the cloud agent: <url>" instead. One turn queued at a time, one adapter per work dir.
The CLI's own login is the credential. Stdlib only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

from ..claude_routine.routine_adapter import (NO_ROOM, NON_OWNER_REFUSED, TEXT_MAX, Adapter as _RoutineAdapter,
                                              Outcome, RateLimiter, build_payload, log, result_body)
from ..codex.codex_connector import acquire_lock, non_owner_note, parse_task, write_atomic

SESSION_RE = re.compile(r"session_[A-Za-z0-9]+")
REMINDER = ("[sparrow task] Follow your standing AG2 Space instructions for this message: "
            "post exactly once into room=, then end with posted/not posted <task_id>.")
ENV_KEEP = ("PATH", "HOME", "USER", "LANG")
LABEL = "claude cloud session"
# Output that proves the CLI never reached the service, so the turn was not queued.
UNSENT_RE = re.compile(r"ENOTFOUND|EAI_AGAIN|ECONNREFUSED|getaddrinfo|Could not resolve host|"
                       r"nodename nor servname|Name or service not known", re.I)


def child_env(environ=None):
    env = os.environ if environ is None else environ
    return {k: env[k] for k in ENV_KEEP if env.get(k)}


def build_text(task_id, headers, message, operator_lines=(), note=None):
    room, payload = build_payload(task_id, headers, message, operator_lines, note,
                                  text_max=TEXT_MAX - len(REMINDER) - 1)
    return room, REMINDER + "\n" + payload


def _last_json(out):
    for chunk in [out.strip()] + out.strip().splitlines()[::-1]:
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def queue_turn(claude, session_id, text, env, timeout=60.0, run=subprocess.run):
    argv = [claude, "-p", text, "--cloud", session_id, "--output-format", "json"]
    try:
        proc = run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return Outcome("ambiguous", detail="timeout")
    except OSError as e:
        return Outcome("retry", detail=type(e).__name__)
    data = _last_json(proc.stdout or "")
    if proc.returncode == 0 and data.get("ok") is True:
        return Outcome("ok", 0, url=data.get("url"), session_id=data.get("session_id"))
    detail = "exit %d" % proc.returncode
    log("claude:", ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:])
    if proc.returncode != 0 and UNSENT_RE.search((proc.stderr or "") + (proc.stdout or "")):
        return Outcome("retry", detail=detail)
    return Outcome("ambiguous", detail=detail if proc.returncode else "no ok")


def mark_sending(path):
    write_atomic(path, "%d\n" % os.getpid())


INTERRUPTED = Outcome("ambiguous", detail="interrupted")


class Adapter(_RoutineAdapter):
    def __init__(self, args, clock=time.time, sleep=time.sleep, note=None, environ=None, run=subprocess.run):
        self.args, self.clock, self.sleep, self.run = args, clock, sleep, run
        self.note = note if note is not None else non_owner_note()
        self.env = child_env(environ)
        self.limiter = RateLimiter(os.path.join(args.state, "sends.json"), args.max_per_hour, clock)
        self.done_path = os.path.join(args.state, "done")
        self.sending_dir = os.path.join(args.state, "sending")
        self.attempts, self.limited, self.not_before = {}, {}, {}
        self.blocked_until = 0.0

    def _marker(self, task_id):
        return os.path.join(self.sending_dir, task_id)

    def _clear(self, task_id):
        try:
            os.unlink(self._marker(task_id))
        except FileNotFoundError:
            pass

    def finish(self, name, body, seen):
        super().finish(name, body, seen)
        self._clear(name[:-4])

    def next_ready(self, names):
        name, wait = super().next_ready(names)
        if name is None or not self.limiter.times:
            return name, wait
        gap = max(self.limiter.times) + self.args.min_gap_seconds - self.clock()
        return (None, gap) if gap > 0 else (name, 0.0)

    def step(self, name, seen):
        """Handle one task; returns True when it reached a final result."""
        task_id = name[:-4]
        try:
            headers, message, operator_lines = parse_task(os.path.join(self.args.inbox, name))
        except FileNotFoundError:
            log("vanished before read ->", task_id)
            seen.add(task_id)
            return True
        if os.path.exists(self._marker(task_id)):
            # A send began before a crash; resending could post twice.
            log("interrupted send, not resent ->", task_id)
            self.finish(name, result_body(INTERRUPTED, self.args.result_mode, headers.get("access_tier") == "owner", LABEL), seen)
            return True
        tier = headers.get("access_tier") or "guest"
        if tier != "owner" and not self.args.allow_non_owner:
            log("refused (non-owner, --allow-non-owner not set) ->", task_id, tier)
            self.finish(name, NON_OWNER_REFUSED, seen)
            return True
        room, text = build_text(task_id, headers, message, operator_lines, self.note)
        if not room:
            log("no room ->", task_id)
            self.finish(name, NO_ROOM, seen)
            return True
        log("queue ->", task_id, tier, "chars", len(text))
        mark_sending(self._marker(task_id))
        out = queue_turn(self.args.claude, self.args.session_id, text, self.env, self.args.timeout, self.run)
        self.limiter.record()
        if out.kind == "retry":
            self._clear(task_id)
            n = self.attempts[task_id] = self.attempts.get(task_id, 0) + 1
            if n <= self.args.max_retries:
                delay = min(self.args.backoff * (2 ** (n - 1)), self.args.backoff_max)
                self.not_before[task_id] = self.clock() + delay
                log("retry %d/%d for %s in %.1fs (%s)" % (n, self.args.max_retries, task_id, delay, out.detail))
                return False
        log("result <-", task_id, out.kind, out.detail, out.session_id or "")
        self.finish(name, result_body(out, self.args.result_mode, headers.get("access_tier") == "owner", LABEL), seen)
        return True


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="dir holding tasks/ results/ state/")
    ap.add_argument("--session-id", required=True, help="the session_... id printed by `claude --cloud`")
    ap.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")
    ap.add_argument("--result-mode", choices=["no-send", "echo"], default="no-send")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="queue non-owner tasks too, into the same session and connector scope as the "
                         "owner. Off by default: the session holds the owner's AG2 Space connector, "
                         "and a non-owner tier is bounded only by the prompt's note otherwise.")
    ap.add_argument("--min-gap-seconds", type=float, default=2.0, help="least time between two queued turns")
    ap.add_argument("--max-per-hour", type=int, default=60, help="queued turns in a sliding hour")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="retries per task for failures that prove the turn was not queued; anything ambiguous is never retried")
    ap.add_argument("--backoff", type=float, default=2.0, help="first retry delay, doubled per retry")
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per claude call")
    ap.add_argument("--once", type=int, default=0, help="exit after N tasks")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--poll", type=float, default=1.0)
    return ap


def main(argv=None, environ=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not SESSION_RE.fullmatch(args.session_id):
        ap.error("--session-id must look like session_...")
    if args.max_per_hour < 1 or args.min_gap_seconds < 0:
        ap.error("--max-per-hour must be at least 1 and --min-gap-seconds not negative")
    env = child_env(environ)
    resolved = shutil.which(args.claude, path=env.get("PATH"))
    if not resolved:
        sys.stderr.write("refusing to start: %s is not an executable on PATH\n" % args.claude)
        return 2
    args.claude = os.path.abspath(resolved)
    args.work = os.path.abspath(args.work)
    args.inbox = os.path.join(args.work, "tasks")
    args.results = os.path.join(args.work, "results")
    args.state = os.path.join(args.work, "state")
    for d in (args.inbox, args.results, args.state, os.path.join(args.state, "sending")):
        os.makedirs(d, exist_ok=True)
    lock = acquire_lock(args.state)
    if lock is None:
        sys.stderr.write("another adapter holds %s (state/connector.pid is alive)\n" % args.work)
        return 3
    try:
        Adapter(args, environ=environ).serve()
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
