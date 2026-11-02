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
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from ...collaborator_turn import child_env, run_collaborator_turn
from ...task_io import (LockRefused, acquire_lock, exit_code, exit_on_sigterm, is_collaborator, make_dirs,
                        non_owner_note, release_lock, root_path, stoppable_sleep, write_atomic)
from ...relay_loop import (HOUR, NO_ROOM, NON_OWNER_FAILED, NON_OWNER_REFUSED, TEXT_MAX, Outcome,  # noqa: F401
                          RateLimiter, RelayLoop, build_payload, count_arg, log, nonnegative_float, positive_arg,
                          positive_float, result_body)

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
ROUTINE_CAP_PER_HOUR = 30
TRIGGER_RE = re.compile(r"trig_[A-Za-z0-9]+")


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
        if e.code == 503 and e.headers.get("Retry-After"):
            return Outcome("retry", 503, retry_after=retry_after_seconds(e.headers.get("Retry-After")), detail=etype)
        return Outcome("ambiguous", e.code, detail=etype)
    except urllib.error.URLError as e:
        # Only a refused connection or a failed name lookup proves the request was never sent.
        if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return Outcome("retry", detail=type(e.reason).__name__)
        return Outcome("ambiguous", detail=type(e.reason).__name__)
    except (OSError, http.client.HTTPException) as e:
        return Outcome("ambiguous", detail=type(e).__name__)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # A 200 created a session even when its body is unreadable: never fire this task again.
    return Outcome("ok", 200, url=data.get("claude_code_session_url"), session_id=data.get("claude_code_session_id"))


class Adapter(RelayLoop):
    def __init__(self, args, token, opener=urllib.request.urlopen, clock=time.time, sleep=stoppable_sleep, note=None,
                 environ=None, run=subprocess.run):
        self.init_loop(args, "fires.json", clock, sleep)
        self.token, self.opener, self.run = token, opener, run
        self.env = child_env(environ)
        self.note = note if note is not None else non_owner_note()
        self.endpoint = "%s/v1/claude_code/routines/%s/fire" % (args.api_base.rstrip("/"), args.trigger_id)
        self.hold_path = os.path.join(args.state, "hold.json")
        try:
            with open(self.hold_path, encoding="utf-8") as f:
                self.blocked_until = float(json.load(f))
        except (OSError, ValueError, TypeError):
            self.blocked_until = 0.0
        if self.blocked_until > self.clock() + HOUR:
            self.blocked_until = 0.0

    def step(self, name, seen):
        """Handle one task; returns True when it reached a final result."""
        task_id = name[:-4]
        task = self.read_task(name, seen)
        if task is None:
            return True
        headers, message, operator_lines = task
        refused = self.refusal(name, headers, seen)
        if refused is not None:
            return refused
        room, text = build_payload(task_id, headers, message, operator_lines, self.note)
        if not room:
            log("no room ->", task_id)
            self.finish(name, NO_ROOM, seen)
            return True
        if is_collaborator(headers):
            return self.collaborator_turn(name, text, seen)
        log("fire ->", task_id, headers.get("access_tier"), "chars", len(text))
        out = fire(self.endpoint, self.token, text, self.args.timeout, self.opener)
        if out.kind == "rate_limited":
            self.blocked_until = self.clock() + out.retry_after
            write_atomic(self.hold_path, json.dumps(self.blocked_until))
            n = self.limited[task_id] = self.limited.get(task_id, 0) + 1
            log("429 %d/%d; holding all fires for %.0fs" % (n, self.args.max_rate_limited, out.retry_after))
            if n < self.args.max_rate_limited:
                return False
        else:
            self.limiter.record()
        if out.kind == "retry" and self.schedule_retry(task_id, out.status or out.detail, out.retry_after):
            return False
        log("result <-", task_id, out.kind, out.status, out.session_id or "")
        self.finish(name, result_body(out, self.args.result_mode, headers.get("access_tier") == "owner"), seen)
        return True

    def collaborator_turn(self, name, text, seen):
        """An isolated local turn, never the routine: its reply is the result, else the plain failure line.
        Its cap is next_ready's gate, so a turn that reaches here only has to be counted."""
        task_id = name[:-4]
        if self.too_old(name):
            log("collaborator task waited too long ->", task_id)
            return self.finish(name, NON_OWNER_FAILED, seen)
        self.collab_limiter.record()
        log("collaborator turn ->", task_id)
        turn = run_collaborator_turn(self.args.claude, text, self.env, self.args.collaborator_timeout, run=self.run)
        log("result <-", task_id, turn.kind, turn.detail)
        return self.finish(name, turn.text if turn.kind == "ok" else NON_OWNER_FAILED, seen)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="dir holding tasks/ results/ state/")
    ap.add_argument("--trigger-id", required=True, help="the routine's trig_... id from its API trigger URL")
    ap.add_argument("--token-env", default="CLAUDE_ROUTINE_TOKEN",
                    help="name of the env var holding the routine's bearer token (the token is never an argument)")
    ap.add_argument("--api-base", default=API_BASE)
    ap.add_argument("--allow-insecure-api-base", action="store_true", help="accept a non-https --api-base (tests only)")
    ap.add_argument("--result-mode", choices=["no-send", "echo"], default="no-send")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="fire tasks from non-owner senders without collaborator access too (guests, and any "
                         "collaborator claim the broker did not attest), at the same routine and connector "
                         "scope as the owner. Off by default: this routine holds the owner's s.space "
                         "connector, and such a sender is bounded only by the prompt's note otherwise. An "
                         "attested collaborator is not covered by it: see --allow-collaborator.")
    ap.add_argument("--allow-collaborator", action="store_true",
                    help="serve attested collaborators (Team) on an isolated local turn: a fresh `claude -p` "
                         "with the three read tools in an empty directory, no session, no connector, no "
                         "history, and never saved (--no-session-persistence); off by default, so they are "
                         "refused with the same plain line")
    ap.add_argument("--claude", default="claude",
                    help="the claude executable for collaborator turns (default: claude on PATH)")
    ap.add_argument("--max-per-hour", type=int, default=25,
                    help="local fire cap, 1-%d (the routine's own cap)" % ROUTINE_CAP_PER_HOUR)
    ap.add_argument("--max-collaborator-per-hour", type=positive_arg, default=10,
                    help="isolated collaborator turns in a sliding hour, 1 or more; their own gate, so they never "
                         "count against --max-per-hour and a fire hold never delays them")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="retries per task for failures that prove the fire was not sent; anything ambiguous is never retried")
    ap.add_argument("--max-rate-limited", type=int, default=5, help="429s per task before it gets a failure result")
    ap.add_argument("--backoff", type=float, default=2.0, help="first retry delay, doubled per retry")
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout per fire")
    ap.add_argument("--collaborator-timeout", type=positive_float, default=30.0,
                    help="seconds per collaborator turn, more than 0. The turn runs inside the one serve loop, so "
                         "raising this delays any owner fire that arrives meanwhile by up to that much")
    ap.add_argument("--collaborator-max-age", type=nonnegative_float, default=1800.0,
                    help="a collaborator task that waited longer than this many seconds gets the plain failure line "
                         "at once instead of a turn, counted since the task was first seen in a pending scan, or "
                         "its file time if earlier; a restart does not reset it. 0 turns the cut-off off")
    ap.add_argument("--once", type=count_arg, default=0, help="exit after N tasks")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--poll", type=float, default=1.0)
    return ap


def main(argv=None, environ=None, opener=urllib.request.urlopen):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not TRIGGER_RE.fullmatch(args.trigger_id):
        ap.error("--trigger-id must look like trig_...")
    if not args.api_base.startswith("https://") and not args.allow_insecure_api_base:
        ap.error("--api-base must be https")
    if not 1 <= args.max_per_hour <= ROUTINE_CAP_PER_HOUR:
        ap.error("--max-per-hour must be 1-%d" % ROUTINE_CAP_PER_HOUR)
    token = (os.environ if environ is None else environ).get(args.token_env, "").strip()
    if not token:
        sys.stderr.write("refusing to start: env var %s is empty or unset\n" % args.token_env)
        return 2
    args.work = os.path.abspath(args.work)
    exit_on_sigterm()
    try:
        owner = acquire_lock(args.work, "claude-routine")
    except LockRefused as e:
        sys.stderr.write(e.line + "\n")
        return e.code
    try:
        make_dirs(owner, ("tasks", "results", "state"))
        args.work = root_path(owner, args.work)
        args.inbox = os.path.join(args.work, "tasks")
        args.results = os.path.join(args.work, "results")
        args.state = os.path.join(args.work, "state")
        Adapter(args, token, opener, environ=environ).serve()
    except LockRefused as e:
        sys.stderr.write(e.line + "\n")
        return e.code
    finally:
        release_lock(owner)
    return exit_code(0)


if __name__ == "__main__":
    sys.exit(main())
