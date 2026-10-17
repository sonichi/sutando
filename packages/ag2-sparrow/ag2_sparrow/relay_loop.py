"""Shared library for adapters that relay inbox task files to a cloud agent that posts its own
reply. Package level, so adapters share it without importing one another.

Owns the parts every such adapter must agree on: the payload layout (routing line, non-owner note,
nonce-marked untrusted span, bridge-written lines after it), the result lines, the sliding-hour send
cap kept on disk, and the task/result loop (priority order, done ledger, retry timing). An adapter
subclasses `RelayLoop`, implements `step()`, and keeps only its own transport. Stdlib only.
"""
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from . import task_priority
from .task_io import load_done, non_owner_note, parse_task, record_done, write_atomic

TEXT_MAX = 65536
TRUNCATED = "\n[truncated]"
HOUR = 3600.0
NO_ROOM = "This message has no room to reply in, so it was not sent to the cloud agent.\n"
# The cloud agent acts with the owner's AG2 Space connector, so non-owner tiers are refused
# unless the adapter opts in; the sender sees only this plain line.
NON_OWNER_REFUSED = "This agent only answers its owner here.\n"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def _field(value):
    # Header values go on the payload's first line, so no whitespace may split or end it.
    return re.sub(r"\s+", "", value or "")


def build_payload(task_id, headers, message, operator_lines=(), note=None, text_max=TEXT_MAX, extra=()):
    """First line: the attested routing fields, then any `extra` (key, value) pairs. Then, for a
    non-owner tier, the note; then the untrusted body between nonce markers; then the bridge-written
    lines, outside them. Only the body is cut to fit `text_max`, so the markers and lines survive."""
    tier = headers.get("access_tier") or "guest"
    collaborator = tier == "team" and headers.get("collaborator") == "true"
    room = _field(headers.get("channel_id")) or _field(headers.get("source_room_id"))
    thread = _field(headers.get("thread_root")) or "none"
    fields = "room=%s thread=%s tier=%s collaborator=%s task_id=%s" % (
        room, thread, tier, "true" if collaborator else "false", _field(task_id))
    head = fields + "".join(" %s=%s" % (k, _field(v)) for k, v in extra) + "\n"
    if tier != "owner":
        head += "note: %s\n" % (note if note is not None else non_owner_note())
    tag = "UNTRUSTED_TASK_" + uuid.uuid4().hex[:8]
    opening, closing = "<%s>\n" % tag, "\n</%s>" % tag
    tail = closing + ("\n" + "\n".join(operator_lines) if operator_lines else "")
    room_left = text_max - len(head) - len(opening) - len(tail)
    if len(message) > room_left:
        message = message[:max(room_left - len(TRUNCATED), 0)] + TRUNCATED
    return room, head + opening + message + tail


class Outcome:
    """kind: ok | rate_limited | rejected | retry (provably not accepted) | ambiguous (may have been)."""

    def __init__(self, kind, status=None, url=None, session_id=None, retry_after=None, detail=""):
        self.kind, self.status, self.url = kind, status, url
        self.session_id, self.retry_after, self.detail = session_id, retry_after, detail


def result_body(outcome, mode, owner=True, label="claude routine session"):
    url = outcome.url or "unknown"
    if outcome.kind == "ok":
        if mode == "echo" and owner:
            return "Sent to the cloud agent: %s\n" % url
        return "[no-send]\n# %s: %s\n" % (label, url)
    # A non-owner tier never learns a status or error detail: that is the owner's
    # connector state, and the sender is not the owner.
    if not owner:
        return "This message could not be sent to the cloud agent right now.\n"
    if outcome.kind == "rejected":
        return "The cloud agent could not take this message (HTTP %s, %s).\n" % (outcome.status, outcome.detail)
    if outcome.kind == "rate_limited":
        return "The cloud agent is over its hourly limit, so this message was not sent to it.\n"
    if outcome.kind == "ambiguous":
        return ("This message may or may not have reached the cloud agent (%s); it was not retried.\n"
                % (outcome.status or outcome.detail))
    return ("The cloud agent could not be reached for this message after several tries (%s).\n"
            % (outcome.status or outcome.detail))


class RateLimiter:
    """Sliding one-hour window of send times, kept in a file so a restart does not reset it.
    An entry may carry a task id; recording that id again inside the window is a no-op."""

    def __init__(self, path, per_hour, clock=time.time):
        self.path, self.per_hour, self.clock = path, per_hour, clock
        try:
            with open(path, encoding="utf-8") as f:
                self.entries = [(float(e[0]), str(e[1])) if isinstance(e, list) else (float(e), None)
                                for e in json.load(f)]
        except (OSError, ValueError, TypeError, IndexError):
            self.entries = []

    @property
    def times(self):
        return [t for t, _ in self.entries]

    def _prune(self):
        now = self.clock()
        self.entries = [(t, k) for t, k in self.entries if 0 <= now - t < HOUR]

    def _save(self):
        write_atomic(self.path, json.dumps([t if k is None else [t, k] for t, k in self.entries]))

    def wait_seconds(self):
        self._prune()
        if len(self.entries) < self.per_hour:
            return 0.0
        return max(min(self.times) + HOUR - self.clock(), 0.0)

    def record(self, key=None):
        self._prune()
        if key is not None and any(k == key for _, k in self.entries):
            return
        self.entries.append((self.clock(), key))
        self._save()

    def forget(self, key):
        self.entries = [(t, k) for t, k in self.entries if k != key]
        self._save()


class RelayLoop:
    """The inbox loop. Subclasses set `limiter` (or call `init_loop`) and implement `step()`."""

    def init_loop(self, args, limiter_file, clock=time.time, sleep=time.sleep):
        self.args, self.clock, self.sleep = args, clock, sleep
        self.limiter = RateLimiter(os.path.join(args.state, limiter_file), args.max_per_hour, clock)
        self.done_path = os.path.join(args.state, "done")
        self.attempts, self.limited, self.not_before = {}, {}, {}   # task id -> pre-send failures, 429s, earliest retry
        self.blocked_until = 0.0

    def pending(self, seen):
        """Open task files, highest priority first and oldest first within a priority."""
        names = [n for n in os.listdir(self.args.inbox)
                 if n.startswith("task-") and n.endswith(".txt") and n[:-4] not in seen
                 and not os.path.exists(os.path.join(self.args.results, n))]
        # A file can vanish between listdir and here (a cancel, a cleanup): drop it rather
        # than spend a turn on a task that is no longer there to serve.
        paths = [Path(self.args.inbox, n) for n in names if os.path.exists(os.path.join(self.args.inbox, n))]
        return [p.name for p in task_priority.sort_tasks_by_priority(paths)]

    def finish(self, name, body, seen):
        write_atomic(os.path.join(self.args.results, name), body)
        record_done(self.done_path, name[:-4])
        seen.add(name[:-4])
        self.attempts.pop(name[:-4], None)
        self.limited.pop(name[:-4], None)
        self.not_before.pop(name[:-4], None)

    def read_task(self, name, seen):
        """(headers, message, operator_lines), or None when the file is gone (marked seen)."""
        try:
            return parse_task(os.path.join(self.args.inbox, name))
        except FileNotFoundError:
            log("vanished before read ->", name[:-4])
            seen.add(name[:-4])
            return None

    def admit(self, name, headers, seen):
        """False, with the refusal written, for a non-owner tier unless --allow-non-owner is set."""
        tier = headers.get("access_tier") or "guest"
        if tier == "owner" or self.args.allow_non_owner:
            return True
        log("refused (non-owner, --allow-non-owner not set) ->", name[:-4], tier)
        self.finish(name, NON_OWNER_REFUSED, seen)
        return False

    def schedule_retry(self, task_id, detail, at_least=0.0):
        """True when the task gets another try (after backoff); False once retries are spent."""
        n = self.attempts[task_id] = self.attempts.get(task_id, 0) + 1
        if n > self.args.max_retries:
            return False
        delay = max(min(self.args.backoff * (2 ** (n - 1)), self.args.backoff_max), at_least or 0)
        self.not_before[task_id] = self.clock() + delay
        log("retry %d/%d for %s in %.1fs (%s)" % (n, self.args.max_retries, task_id, delay, detail))
        return True

    def step(self, name, seen):
        raise NotImplementedError

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
