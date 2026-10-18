"""Shared library for adapters that relay inbox task files to a cloud agent that posts its own
reply. Package level, so adapters share it without importing one another.

Owns the parts every such adapter must agree on: the payload layout (routing line, non-owner note,
nonce-marked untrusted span, bridge-written lines after it), the result lines, the sliding-hour send
cap kept on disk, and the task/result loop (priority order, done ledger, retry timing). An adapter
subclasses `RelayLoop`, implements `step()`, and keeps only its own transport. Stdlib only.
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from . import local_task_protocol, result_disposal, task_priority
from .task_io import (NON_OWNER_REFUSED, admission, is_collaborator, load_done, non_owner_note,  # noqa: F401
                      parse_task, record_done, stop_requested, stoppable_sleep, write_atomic)

TEXT_MAX = 65536
TRUNCATED = "\n[truncated]"
HOUR = 3600.0
NO_ROOM = "This message has no room to reply in, so it was not sent to the cloud agent.\n"
# A non-owner tier never learns a status or error detail: that is the owner's connector state.
NON_OWNER_FAILED = "This message could not be sent to the cloud agent right now.\n"
COLLAB_LIMITER_FILE = "collaborator-turns.json"


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
    collaborator = is_collaborator(headers)
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


def count_arg(text):
    """argparse type for --once: an integer of zero or more (zero means no limit)."""
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def positive_arg(text):
    """argparse type for a cap: an integer of one or more."""
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


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
    if not owner:
        return NON_OWNER_FAILED
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

    def holds(self, key):
        self._prune()
        return any(k == key for _, k in self.entries)

    def touch(self, key):
        """Move a reservation to now, keeping it the one entry for that key."""
        self.entries = [(self.clock() if k == key else t, k) for t, k in self.entries]
        self._save()

    def forget(self, key):
        self.entries = [(t, k) for t, k in self.entries if k != key]
        self._save()


STAGED_RE = re.compile(r"\.(task-.+\.txt)\.staged")


def staged_path(results, name):
    # Dot-prefixed in the same directory: drains never list it, and publishing is one rename.
    return os.path.join(results, ".%s.staged" % name)


def publish(staged, path):
    """Put a staged body at `path` unless a result already sits there; the stage goes either way.
    No replace: a result another writer published, or a drain is reading, is never overwritten."""
    try:
        os.link(staged, path)
    except FileExistsError:
        log("a result is already at", os.path.basename(path), "- the staged copy is discarded")
    os.unlink(staged)


def result_exists(results, task_id):
    """Any generation of the task's result: live, archived, or mid-disposal."""
    return (local_task_protocol.find_result(Path(results), task_id) is not None
            or bool(result_disposal.find_claims(Path(results), task_id)))


class RelayLoop:
    """The inbox loop. Subclasses set `limiter` (or call `init_loop`) and implement `step()`."""

    def init_loop(self, args, limiter_file, clock=time.time, sleep=stoppable_sleep):
        self.args, self.clock, self.sleep = args, clock, sleep
        self.limiter = RateLimiter(os.path.join(args.state, limiter_file), args.max_per_hour, clock)
        # Collaborator turns never touch the provider, so they have their own cap, not the provider's.
        self.collab_limiter = RateLimiter(os.path.join(args.state, COLLAB_LIMITER_FILE),
                                          args.max_collaborator_per_hour, clock)
        self.done_path = os.path.join(args.state, "done")
        self.attempts, self.limited, self.not_before = {}, {}, {}   # task id -> pre-send failures, 429s, earliest retry
        self.lanes = {}                                            # task name -> which gates apply
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
        """Stage the body, record the task done, then publish: a drain may claim the result the
        moment it appears, so the done record must already say the task is settled."""
        staged = staged_path(self.args.results, name)
        write_atomic(staged, body)
        record_done(self.done_path, name[:-4])
        publish(staged, os.path.join(self.args.results, name))
        self._settled(name, seen)
        return True

    def settle(self, name, body, seen):
        """finish() unless some generation of this task's result already exists; then only done."""
        if result_exists(self.args.results, name[:-4]):
            log("a result already exists, none added ->", name[:-4])
            record_done(self.done_path, name[:-4])
            self._settled(name, seen)
        else:
            self.finish(name, body, seen)

    def _settled(self, name, seen):
        seen.add(name[:-4])
        self.attempts.pop(name[:-4], None)
        self.limited.pop(name[:-4], None)
        self.not_before.pop(name[:-4], None)
        self.lanes.pop(name, None)

    def read_task(self, name, seen):
        """(headers, message, operator_lines), or None when the file is gone (marked seen)."""
        try:
            return parse_task(os.path.join(self.args.inbox, name))
        except FileNotFoundError:
            log("vanished before read ->", name[:-4])
            seen.add(name[:-4])
            return None

    def classify(self, headers):
        """The one admission decision, from task_io.admission and both allow flags: owner | non_owner |
        collaborator (served), or refused-collaborator | refused. refusal() and lane() both read it."""
        kind = admission(headers, self.args.allow_non_owner)
        if kind == "collaborator" and not self.args.allow_collaborator:
            return "refused-collaborator"
        return kind or "refused"

    def refusal(self, name, headers, seen):
        """None when classify() serves the task; otherwise the refusal is finished and its step result
        returned: True when published, False when parked."""
        kind = self.classify(headers)
        if kind == "refused-collaborator":
            log("refused (collaborator, --allow-collaborator not set) ->", name[:-4])
        elif kind == "refused":
            log("refused (non-owner, --allow-non-owner not set) ->", name[:-4], headers.get("access_tier") or "guest")
        else:
            return None
        return self.finish(name, NON_OWNER_REFUSED, seen)

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

    def recovering(self, name):
        """True for a task whose earlier attempt is already counted; it skips the send gates."""
        return False

    def lane(self, name):
        """Which gate applies, from classify(): `refuse` (none: the step refuses it at once), `collaborator`
        (its own cap) or `provider` (the hold and the cap). Read once per task; a gone file is the step's."""
        if name not in self.lanes:
            try:
                headers, _, _ = parse_task(os.path.join(self.args.inbox, name))
            except OSError:
                return "provider"
            kind = self.classify(headers)
            self.lanes[name] = ("refuse" if kind.startswith("refused") else
                                "collaborator" if kind == "collaborator" else "provider")
        return self.lanes[name]

    def lane_wait(self, lane):
        if lane == "refuse":
            return 0.0
        return self.collab_limiter.wait_seconds() if lane == "collaborator" else self.provider_wait()

    def provider_wait(self):
        """Seconds until the provider may be called: its hold first, then its sliding-hour cap."""
        hold = self.blocked_until - self.clock()
        return hold if hold > 0 else self.limiter.wait_seconds()

    def next_ready(self, names):
        """The first ready task whose own lane's gate is open, else how long until some gate opens."""
        now = self.clock()
        for n in names:
            if self.recovering(n):
                return n, 0.0
        ready = [n for n in names if self.not_before.get(n[:-4], 0) <= now]
        if not ready:
            return None, min(self.not_before[n[:-4]] for n in names) - now
        waits = {}
        for n in ready:
            lane = self.lane(n)
            if lane not in waits:
                waits[lane] = self.lane_wait(lane)
            if waits[lane] <= 0:
                return n, 0.0
        return None, min(waits.values())

    def recover(self):
        """A staged body a crash left is that task's result, unless any generation of a result
        already exists (published, claimed, archived); then the stage is discarded."""
        for entry in sorted(os.listdir(self.args.results)):
            m = STAGED_RE.fullmatch(entry)
            if not m:
                continue
            staged, task_id = os.path.join(self.args.results, entry), m.group(1)[:-4]
            record_done(self.done_path, task_id)
            if result_exists(self.args.results, task_id):
                log("a result already exists, staged copy discarded ->", task_id)
                os.unlink(staged)
            else:
                log("publishing a staged result ->", task_id)
                publish(staged, os.path.join(self.args.results, m.group(1)))

    def serve(self):
        self.recover()
        seen = set(load_done(self.done_path))
        done = 0
        idle_since = self.clock()
        while not stop_requested():
            names = self.pending(seen)
            if not names:
                if self.args.once and done >= self.args.once:  # pragma: no cover - a step returns first; --once is never negative
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
