#!/usr/bin/env python3
"""Queue AG2 Space inbox tasks as turns in one persistent Claude Code cloud session.

Each `tasks/task-*.txt` becomes one `claude -p --cloud <session_id> --output-format json` with the
turn text on stdin, never in argv. The CLI returns once the turn is queued; the session (set up once
with session-prompt.md and the AG2 Space connector) posts the reply itself, so the result file written
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

from ...task_io import acquire_lock, non_owner_note, write_atomic
from ...relay_loop import NO_ROOM, TEXT_MAX, Outcome, RelayLoop, build_payload, log, result_body

SESSION_RE = re.compile(r"session_[A-Za-z0-9]+")
ROOM_RE = re.compile(r"![^\s:]+:\S+")
REMINDER = ("[sparrow task] Follow your standing AG2 Space instructions for this message: one answer, "
            "to exactly one place they name (plus only the one DM pointer line when they say so), "
            "then end with posted/not posted <task_id>.")
# CLAUDE_CONFIG_DIR selects which login the CLI uses; without it the CLI falls back to the user default.
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "CLAUDE_CONFIG_DIR")
LABEL = "claude cloud session"
# Output that proves the CLI never reached the service, so the turn was not queued.
UNSENT_RE = re.compile(r"ENOTFOUND|EAI_AGAIN|ECONNREFUSED|getaddrinfo|Could not resolve host|"
                       r"nodename nor servname|Name or service not known", re.I)
INTERRUPTED = Outcome("ambiguous", detail="interrupted")


def child_env(environ=None):
    env = os.environ if environ is None else environ
    return {k: env[k] for k in ENV_KEEP if env.get(k)}


def channel_kind(headers):
    kind = (headers.get("channel_kind") or "").strip().lower()
    return kind if kind in ("dm", "room") else "unknown"


def build_text(task_id, headers, message, operator_lines=(), note=None, owner_dm=None):
    extra = [("channel_kind", channel_kind(headers))]
    if owner_dm:
        extra.append(("owner_dm", owner_dm))
    room, payload = build_payload(task_id, headers, message, operator_lines, note,
                                  text_max=TEXT_MAX - len(REMINDER) - 1, extra=extra)
    return room, REMINDER + "\n" + payload


def _text(out):
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")


def _receipt(*outputs):
    """The first JSON record with "ok": true in any output, line by line or whole; else {}."""
    for out in outputs:
        out = _text(out).strip()
        for chunk in [out] + out.splitlines():
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("ok") is True:
                return data
    return {}


def _queued(data):
    return Outcome("ok", 0, url=data.get("url"), session_id=data.get("session_id"))


def queue_turn(claude, session_id, text, env, timeout=60.0, run=subprocess.run):
    argv = [claude, "-p", "--cloud", session_id, "--output-format", "json"]
    try:
        proc = run(argv, input=text, env=env, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired as e:
        data = _receipt(e.stdout, e.stderr)
        return _queued(data) if data else Outcome("ambiguous", detail="timeout")
    except OSError as e:
        return Outcome("retry", detail=type(e).__name__)
    data = _receipt(proc.stdout, proc.stderr)
    # Any receipt means the turn was queued, whatever else the exit code or output says.
    if data:
        return _queued(data)
    output = _text(proc.stderr) + _text(proc.stdout)
    log("claude:", output.strip()[-300:])
    if proc.returncode != 0 and UNSENT_RE.search(output):
        return Outcome("retry", detail="exit %d" % proc.returncode)
    return Outcome("ambiguous", detail="exit %d" % proc.returncode if proc.returncode else "no ok")


def mark(path, phase):
    write_atomic(path, phase + "\n")


def mark_sending(path):
    mark(path, "sending")


class Adapter(RelayLoop):
    def __init__(self, args, clock=time.time, sleep=time.sleep, note=None, environ=None, run=subprocess.run):
        self.init_loop(args, "sends.json", clock, sleep)
        self.run = run
        self.note = note if note is not None else non_owner_note()
        self.env = child_env(environ)
        self.sending_dir = os.path.join(args.state, "sending")

    def _marker(self, task_id):
        return os.path.join(self.sending_dir, task_id)

    def _phase(self, task_id):
        """sending (the call may have queued it), outcome (known, counted), unsent (proven), or None."""
        try:
            with open(self._marker(task_id), encoding="utf-8") as f:
                return (f.read().split() or ["sending"])[0]
        except FileNotFoundError:
            return None

    def _clear(self, task_id):
        try:
            os.unlink(self._marker(task_id))
        except FileNotFoundError:
            pass

    def finish(self, name, body, seen):
        super().finish(name, body, seen)
        self._clear(name[:-4])

    def recovering(self, name):
        return self._phase(name[:-4]) in ("sending", "outcome") or self.limiter.holds(name[:-4])

    def next_ready(self, names):
        name, wait = super().next_ready(names)
        if name is None or self.recovering(name) or not self.limiter.times:
            return name, wait
        gap = max(self.limiter.times) + self.args.min_gap_seconds - self.clock()
        return (None, gap) if gap > 0 else (name, 0.0)

    def step(self, name, seen):
        """Handle one task; returns True when it reached a final result."""
        task_id = name[:-4]
        task = self.read_task(name, seen)
        if task is None:
            return True
        headers, message, operator_lines = task
        owner = headers.get("access_tier") == "owner"
        phase = self._phase(task_id)
        if phase == "unsent":
            self._clear(task_id)
        elif phase is not None:
            # A send began before a crash; resending could post twice.
            log("interrupted send, not resent ->", task_id, phase)
            if phase == "sending":
                # Its outcome time is unknown: count it as queued now, the latest it can have been.
                self.limiter.touch(task_id)
            self.finish(name, result_body(INTERRUPTED, self.args.result_mode, owner, LABEL), seen)
            return True
        if not self.admit(name, headers, seen):
            return True
        room, text = build_text(task_id, headers, message, operator_lines, self.note, self.args.owner_dm)
        if not room:
            log("no room ->", task_id)
            self.finish(name, NO_ROOM, seen)
            return True
        log("queue ->", task_id, headers.get("access_tier"), "chars", len(text))
        # Reserved before the send, keyed by task, so a crash after it can neither drop nor double the count.
        self.limiter.record(task_id)
        mark_sending(self._marker(task_id))
        out = queue_turn(self.args.claude, self.args.session_id, text, self.env, self.args.timeout, self.run)
        if out.kind == "retry":
            # Marker first: a crash after it still reads as unsent, so a restart sends it again.
            mark(self._marker(task_id), "unsent")
            self.limiter.forget(task_id)
            self._clear(task_id)
            if self.schedule_retry(task_id, out.detail):
                return False
        else:
            self.limiter.touch(task_id)
            mark(self._marker(task_id), "outcome")
        log("result <-", task_id, out.kind, out.detail, out.session_id or "")
        self.finish(name, result_body(out, self.args.result_mode, owner, LABEL), seen)
        return True


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="dir holding tasks/ results/ state/")
    ap.add_argument("--session-id", required=True, help="the session_... id printed by `claude --cloud`")
    ap.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")
    ap.add_argument("--owner-dm", default=None,
                    help="the owner's DM room id; answers meant for the owner alone go there "
                         "instead of a shared room")
    ap.add_argument("--result-mode", choices=["no-send", "echo"], default="no-send")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="queue non-owner tasks too, into the same session and connector scope as the "
                         "owner. Off by default: the session holds the owner's AG2 Space connector, "
                         "and a non-owner tier is bounded only by the prompt's note otherwise.")
    ap.add_argument("--min-gap-seconds", type=float, default=2.0, help="least time between two queued turns")
    ap.add_argument("--max-per-hour", type=int, default=60,
                    help="turns queued (or possibly queued) in a sliding hour; proven-unsent attempts do not count")
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
    if args.owner_dm is not None and not ROOM_RE.fullmatch(args.owner_dm):
        ap.error("--owner-dm must be a room id like !room:server")
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
