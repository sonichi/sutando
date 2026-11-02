#!/usr/bin/env python3
"""Queue AG2 Space inbox tasks as turns in one persistent Claude Code cloud session.

Each `tasks/task-*.txt` becomes one `claude -p --cloud <session_id> --output-format json` with the
turn text on stdin, never in argv. The CLI returns once the turn is queued; the session (set up once
with session-prompt.md and the AG2 Space connector) posts the reply itself, so the result file written
here is `[no-send]` plus the session URL as a comment; `--result-mode echo` writes a visible
"Sent to the cloud agent: <url>" instead. Every durable step (work-dir lock, claims, publication,
rate journal, settled ids) is task_lifecycle's; this module renders texts and calls the CLI.
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

from ... import task_lifecycle
from ...collaborator_turn import ENV_KEEP, child_env, run_collaborator_turn  # noqa: F401
from ...relay_loop import (COLLAB_LIMITER_FILE, NO_ROOM, NON_OWNER_FAILED, TEXT_MAX, Outcome, RateLimiter, RelayLoop,
                           build_payload, count_arg, log, nonnegative_float, positive_arg, positive_float, result_body)
from ...task_io import (LockRefused, acquire_lock, clear_attributed_pidfile, exit_code, exit_on_sigterm,
                        is_collaborator, make_dirs, non_owner_note, process_identity, release_lock, root_path,
                        stoppable_sleep, verify_lineage)

SESSION_RE = re.compile(r"session_[A-Za-z0-9]+")
ROOM_RE = re.compile(r"![^\s:]+:\S+")
REMINDER = ("[sparrow task] Follow your standing s.space instructions for this message: one answer, "
            "to exactly one place they name (plus only the one DM pointer line when they say so), "
            "then end with posted/not posted <task_id>.")
LABEL = "claude cloud session"
# Output that proves the CLI never reached the service, so the turn was not queued.
UNSENT_RE = re.compile(r"ENOTFOUND|EAI_AGAIN|ECONNREFUSED|getaddrinfo|Could not resolve host|"
                       r"nodename nor servname|Name or service not known", re.I)
INTERRUPTED = Outcome("ambiguous", detail="interrupted")


def channel_kind(headers):
    kind = (headers.get("channel_kind") or "").strip().lower()
    return kind if kind in ("dm", "room") else "unknown"


def build_text(task_id, headers, message, operator_lines=(), note=None, owner_dm=None):
    extra = [("channel_kind", channel_kind(headers))]
    if owner_dm and headers.get("access_tier") == "owner":
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


PARK_SECONDS = 30.0


class JournalGate:
    """The send gates read from the lifecycle's rate journal: queued and possibly queued turns."""

    def __init__(self, journal, per_hour):
        self.journal, self.per_hour = journal, per_hour

    @property
    def times(self):
        return self.journal.times()

    def wait_seconds(self):
        return self.journal.wait_seconds(self.per_hour)


class Adapter(RelayLoop):
    """Every durable step goes through task_lifecycle; this class only renders texts and calls the CLI."""

    def __init__(self, args, clock=time.time, sleep=stoppable_sleep, note=None, environ=None, run=subprocess.run):
        self.args, self.clock, self.sleep, self.run = args, clock, sleep, run
        self.note = note if note is not None else non_owner_note()
        self.env = child_env(environ)
        self.lc = task_lifecycle.Lifecycle(args.work, clock=clock, log=log, identity=process_identity(),
                                           lineage=verify_lineage)
        self.limiter = JournalGate(self.lc.journal, args.max_per_hour)
        # Collaborator turns never touch the session, so they have their own cap, outside the rate journal.
        self.collab_limiter = RateLimiter(os.path.join(args.state, COLLAB_LIMITER_FILE),
                                          args.max_collaborator_per_hour, clock)
        # The loop's done ledger is the settled-ids ledger: same one-id-per-line format, repaired on open.
        self.done_path = self.lc.settled.path
        self.attempts, self.limited, self.not_before, self.lanes, self.seen_at = {}, {}, {}, {}, {}
        self.blocked_until = 0.0

    def recover(self):
        self.lc.recover()

    def _phase(self, task_id):
        if self.not_before.get(task_id, 0) > self.clock():
            return None                                 # parked: not read again until its park ends
        try:
            rec = self.lc.claims.load(task_id)
        except task_lifecycle.ClaimUnreadable:
            return None
        return rec["phase"] if rec else None

    def recovering(self, name):
        """A task past its call only needs publishing: it skips the send gates, not its park delay."""
        phase = self._phase(name[:-4])
        return (phase is not None and phase not in task_lifecycle.CALLABLE
                and self.not_before.get(name[:-4], 0) <= self.clock())

    def provider_wait(self):
        """The loop's hold and cap, then the least gap after the latest counted turn."""
        wait = super().provider_wait()
        if wait > 0 or not self.limiter.times:
            return wait
        return max(max(self.limiter.times) + self.args.min_gap_seconds - self.clock(), 0.0)

    def contained(self, name, unit, *args):
        """The serve loop's one containment: a state read that failed on I/O (a plain StateIOError) anywhere in a
        task's probe or step parks that task for a retry and the loop goes on. A failed durable write
        (StateWriteError) and a refusal on what the state says still stop the adapter (exit 2)."""
        try:
            return unit(*args)
        except task_lifecycle.StateWriteError:
            raise
        except task_lifecycle.StateIOError as e:
            self.park(name[:-4], e)
            return False

    def park(self, task_id, why):
        log("parked ->", task_id, why)
        self.not_before[task_id] = self.clock() + PARK_SECONDS
        return False

    def finish(self, name, body, seen):
        """A result with no provider call (a refusal, no room): published through the lifecycle."""
        if self.lc.complete_without_call(name[:-4], body, body.strip()) == "parked":
            return self.park(name[:-4], "publication")
        self._settled(name, seen)
        return True

    def _settled(self, name, seen):
        seen.add(name[:-4])
        for d in (self.attempts, self.limited, self.not_before):
            d.pop(name[:-4], None)
        self.lanes.pop(name, None)
        self.seen_at.pop(name, None)

    def _publish(self, name, seen):
        if self.lc.publish_and_settle(name[:-4]) == "parked":
            return self.park(name[:-4], "publication")
        self._settled(name, seen)
        return True

    def step(self, name, seen):
        """Handle one task; returns True when it reached a final result."""
        task_id = name[:-4]
        try:
            rec = self.lc.claims.load(task_id)
        except task_lifecycle.ClaimUnreadable as e:
            if task_id in self.lc.settled:
                log("already settled, never called again ->", task_id)
                seen.add(task_id)
                return True
            return self.park(task_id, e)
        # A local claim is never this adapter's to repair: it only reads the ledger for one.
        local = bool(rec and rec.get("claim_mode") == task_lifecycle.LOCAL)
        # Its repair's failures are classified by contained(), never here: a read parks, a write stops.
        settled = task_id in self.lc.settled if local else self.lc.is_settled(task_id)
        if settled:
            log("already settled, never called again ->", task_id)
            seen.add(task_id)
            return True
        if local:
            log("foreign claim (local), left alone ->", task_id)
            return self.park(task_id, "a local claim")
        if rec and rec["phase"] not in task_lifecycle.CALLABLE:
            return self._publish(name, seen)
        task = self.read_task(name, seen)
        if task is None:
            return True
        headers, message, operator_lines = task
        owner = headers.get("access_tier") == "owner"
        refused = self.refusal(name, headers, seen)
        if refused is not None:
            return refused
        collaborator = is_collaborator(headers)
        if collaborator:
            # The isolated turn has no standing instructions, so no reminder and no owner_dm.
            room, text = build_payload(task_id, headers, message, operator_lines, self.note)
        else:
            room, text = build_text(task_id, headers, message, operator_lines, self.note, self.args.owner_dm)
        if not room:
            log("no room ->", task_id)
            return self.finish(name, NO_ROOM, seen)
        decision, state = self.lc.decide(task_id)
        if decision == "settle":
            self.lc.settle_without_result(task_id, "a result already exists (%s)" % state)
            self._settled(name, seen)
            return True
        if decision == "park":
            return self.park(task_id, state)
        if collaborator and self.too_old(name):
            log("collaborator task waited too long ->", task_id)
            return self.finish(name, NON_OWNER_FAILED, seen)
        attempt = self.lc.prepare(task_id, result_body(INTERRUPTED, self.args.result_mode, owner, LABEL))
        if not self.lc.begin_call(task_id, attempt, os.path.join(self.args.inbox, name)):
            self._settled(name, seen)
            return True
        if collaborator:
            self.collab_limiter.record()
            log("collaborator turn ->", task_id, "attempt", attempt, "chars", len(text))
            turn = run_collaborator_turn(self.args.claude, text, self.env, self.args.collaborator_timeout, run=self.run)
            log("result <-", task_id, turn.kind, turn.detail)
            self.lc.record_outcome(task_id, "final", turn.text if turn.kind == "ok" else NON_OWNER_FAILED, turn.detail)
            return self._publish(name, seen)
        log("queue ->", task_id, headers.get("access_tier"), "attempt", attempt, "chars", len(text))
        out = queue_turn(self.args.claude, self.args.session_id, text, self.env, self.args.timeout, self.run)
        if out.kind == "retry":
            self.lc.not_delivered(task_id, attempt, out.detail)
            if self.schedule_retry(task_id, out.detail):
                return False
            kind = "final"
        else:
            kind = "queued" if out.kind == "ok" else "unknown"
        log("result <-", task_id, out.kind, out.detail, out.session_id or "")
        self.lc.record_outcome(task_id, kind, result_body(out, self.args.result_mode, owner, LABEL),
                               out.detail, out.url)
        return self._publish(name, seen)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True,
                    help="$CLOUD_WORK: this adapter's own dir; the bridge's tasks/ and results/ live in it "
                         "(README: deployment)")
    ap.add_argument("--session-id", required=True, help="the session_... id printed by `claude --cloud`")
    ap.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")
    ap.add_argument("--gateway-state", default=None,
                    help="the gateway bridge's AGENT_CONNECT_STATE_DIR; refused when inside --work")
    ap.add_argument("--owner-dm", default=None,
                    help="the owner's DM room id; answers meant for the owner alone go there "
                         "instead of a shared room")
    ap.add_argument("--result-mode", choices=["no-send", "echo"], default="no-send")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="queue tasks from non-owner senders without collaborator access too (guests, and any "
                         "collaborator claim the broker did not attest), into the same session and connector "
                         "scope as the owner. Off by default: the session holds the owner's history and AG2 "
                         "Space connector, and such a sender is bounded only by the prompt's note otherwise. An "
                         "attested collaborator is not covered by it: see --allow-collaborator.")
    ap.add_argument("--allow-collaborator", action="store_true",
                    help="serve attested collaborators (Team) on an isolated local turn: a fresh `claude -p` "
                         "with the three read tools in an empty directory, never this session, no connector, "
                         "no history, and never saved (--no-session-persistence); off by default, so they are "
                         "refused with the same plain line")
    ap.add_argument("--min-gap-seconds", type=float, default=2.0, help="least time between two queued turns")
    ap.add_argument("--max-per-hour", type=int, default=60,
                    help="turns queued (or possibly queued) in a sliding hour; proven-unsent attempts do not count")
    ap.add_argument("--max-collaborator-per-hour", type=positive_arg, default=10,
                    help="isolated collaborator turns in a sliding hour, 1 or more; their own gate, so they never "
                         "count against --max-per-hour or --min-gap-seconds, and neither delays them")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="retries per task for failures that prove the turn was not queued; anything ambiguous is never retried")
    ap.add_argument("--backoff", type=float, default=2.0, help="first retry delay, doubled per retry")
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per claude --cloud call")
    ap.add_argument("--collaborator-timeout", type=positive_float, default=60.0,
                    help="seconds per collaborator turn, more than 0")
    ap.add_argument("--collaborator-max-age", type=nonnegative_float, default=1800.0,
                    help="a collaborator task that waited longer than this many seconds gets the plain failure line "
                         "at once instead of a turn, counted since the task was first seen in a pending scan, or "
                         "its file time if earlier; a restart does not reset it. 0 turns the cut-off off")
    ap.add_argument("--once", type=count_arg, default=0, help="exit after N tasks")
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
    exit_on_sigterm()
    try:
        owner = acquire_lock(args.work, "claude-cloud-session")
    except LockRefused as e:
        sys.stderr.write(e.line + "\n")
        return e.code
    try:
        return exit_code(serve_locked(args, environ, owner))
    finally:
        release_lock(owner)


def gateway_state_conflict(work, gateway_state):
    """--gateway-state, the bridge's own state dir, must be absolute and outside the work dir."""
    if gateway_state is None:
        return None
    if not os.path.isabs(gateway_state):
        return "refusing to start: --gateway-state must be an absolute path (it is %r)" % gateway_state
    root, mine = os.path.realpath(work), os.path.realpath(gateway_state)
    if mine == root or mine.startswith(root + os.sep):
        return ("refusing to start: --gateway-state %s is inside the work dir %s; the bridge's state "
                "belongs outside it" % (gateway_state, work))
    return None


def startup_refusal(work, environ=None, gateway_state=None, owner=None):
    """Checks that run after the lock is published and the directories exist; the first refusal, or None."""
    return (gateway_state_conflict(work, gateway_state)
            or (clear_attributed_pidfile(owner, work) if owner is not None else None)
            or task_lifecycle.prepare_work_dir(work)
            or task_lifecycle.probe_moves(os.path.join(work, "results"))
            or task_lifecycle.probe_moves(os.path.join(work, "state")))


def serve_locked(args, environ=None, owner=None):
    """Under the published lock: the directories through the held root, then the startup checks
    against that root's current path, then serving."""
    try:
        make_dirs(owner, ("tasks", "results", "state"))
        args.work = root_path(owner, args.work)
    except OSError as e:
        sys.stderr.write("refusing to start: cannot create the work dirs (%s)\n" % e)
        return 2
    except LockRefused as e:
        sys.stderr.write(e.line + "\n")
        return e.code
    args.inbox = os.path.join(args.work, "tasks")
    args.results = os.path.join(args.work, "results")
    args.state = os.path.join(args.work, "state")
    try:
        refusal = startup_refusal(args.work, environ, args.gateway_state, owner)
        if refusal:
            raise task_lifecycle.StartupRefused(2, refusal)
        Adapter(args, environ=environ).serve()
    except task_lifecycle.StartupRefused as e:
        sys.stderr.write(e.message + "\n")
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
