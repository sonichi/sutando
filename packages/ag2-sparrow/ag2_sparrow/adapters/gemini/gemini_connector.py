#!/usr/bin/env python3
"""Wake Gemini CLI (or Antigravity) over ACP for each task file Sparrow queues.

Transport: the Agent Client Protocol over the stdio of `gemini --acp` (`agy acp` for Antigravity), through
acp_host's turn: per task, spawn the command, `initialize`, resume the task's Session with `session/load`
(else `session/new`), send one `session/prompt` and wait for it to stop. The agent's environment always
carries GEMINI_CLI_NO_RELAUNCH=true: without it Gemini CLI starts a second copy of itself on the same
pipe, and a request the first copy already read is lost, so `initialize` can go unanswered.

`<dir>/tasks/task-*.txt` in, `<dir>/results/<task>.txt` out. The turn's final message is the answer: Gemini
asks permission without a rawInput, so no reply command can be granted exactly, and its own text is posted
with every delivery marker neutralised. A final message of exactly NO_REPLY posts nothing. A message whose
turn failed gets a line saying so; an event that failed posts nothing.

Sessions, the permission policy and the framing are acp_host's: the owner may read or edit under the working
directory, no command is ever granted, and tier admission is task_io.admission's. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path

from ... import acp_host, held_start, result_labels, result_publish
from ...acp_host import MESSAGE_EVENT, SESSIONS_NAME, TASK_NAME_RE, SessionStore, quoted, trigger_of  # noqa: F401
from ...acp_client import AcpClient as _SharedClient
from ...relay_loop import count_arg
from ...task_io import (NON_OWNER_NOTE, NON_OWNER_REFUSED, LockRefused, acquire_lock, admission, exit_code,
                        exit_on_sigterm, holder_consumer, load_done, make_dirs, parse_task, record_done,
                        release_lock, root_path, stop_requested)

# runtime -> (its name in the room, the ACP command line)
RUNTIMES = {"gemini": ("Gemini", "gemini --acp"), "antigravity": ("Antigravity", "agy acp")}
DEFAULT_RUNTIME = "gemini"
NO_RELAUNCH = ("GEMINI_CLI_NO_RELAUNCH", "true")
NO_REPLY = "NO_REPLY"
HELD_WAIT_S = held_start.WAIT_S
RETRY_S = 60.0
STDERR_NAME = "gemini-acp.stderr"
NOT_SIGNED_IN = ("%s is not signed in on this computer (%%s): run `%s` once in a terminal and sign in, or put "
                 "GEMINI_API_KEY in ~/.gemini/.env")


class AcpClient(_SharedClient):
    client_info = {"name": "ag2-sparrow-gemini", "version": "0.1.0"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def agent_env(environ=None):
    """The ACP agent's environment: the connector's own (the launcher already narrowed it), never relaunching."""
    env = dict(os.environ if environ is None else environ)
    env[NO_RELAUNCH[0]] = NO_RELAUNCH[1]
    return env


def not_signed_in(runtime):
    name, command = RUNTIMES.get(runtime, RUNTIMES[DEFAULT_RUNTIME])
    return NOT_SIGNED_IN % (name, command.split()[0])


def publish(result_file, body):
    """Labels first, so the drain that reads the result finds them, then the body, staged and renamed."""
    labels, _notes = result_publish.result_labels_for(body)
    result_labels.write(Path(result_file), labels, body)
    result_publish.publish_text(result_file, body)


def wake_prompt(task_id, headers, message, operator_lines, cwd, note=NON_OWNER_NOTE):
    event, _ = trigger_of(headers)
    out = acp_host.task_framing(task_id, headers, message, operator_lines, "post in your final message", note)
    out.append("Answer through Sparrow, exactly once for this task: your final message in this turn is what Sparrow "
               "posts%s. Write it as the post itself, with no preamble about tools or steps. To post nothing (an "
               "acknowledgement, thanks or FYI that needs no answer, or an event whose body asks for no post), make "
               "your final message exactly %s and nothing else."
               % (" as the reply" if event in (None, MESSAGE_EVENT) else "", NO_REPLY))
    out.append("No shell command is granted in this session. %s"
               % ("You may read and edit files under %s." % cwd if headers.get("access_tier") == "owner"
                  else "You may not read or change files."))
    return "\n\n".join(out)


class Connector:
    def __init__(self, args, client_factory=AcpClient, note=None):
        self.args = args
        self.client_factory = client_factory
        self.note = note if note is not None else NON_OWNER_NOTE
        self.store = SessionStore(args.session_store)
        self.name = RUNTIMES[args.runtime][0]
        self.retry_at = {}

    def run_turn(self, task_id, headers, prompt, owner):
        """(status, text, detail, turn): status is completed | cancelled | timeout | gone | failed."""
        cwd = self.args.cwd
        return acp_host.run_prompt(self.client_factory, self.args, headers, task_id, prompt,
                                   lambda params: acp_host.decide_permission(params, owner, cwd), self.store,
                                   STDERR_NAME, not_signed_in(self.args.runtime), log)

    def answer_body(self, headers, status, text, detail):
        """What Sparrow posts: the turn's own text, quoted so no delivery marker in it acts; NO_REPLY or a
        failed event posts nothing; a failed message gets a line saying why, quoted too (the detail is the
        agent's own error text)."""
        said = (text or "").strip()
        message = acp_host.is_message(headers)
        if status == "completed":
            if said == NO_REPLY:
                return "[no-send] %s chose not to post\n" % self.name
            if said:
                return quoted(said) + "\n"
            return ("%s finished without an answer.\n" % self.name if message
                    else "[no-send] %s handled the event without posting\n" % self.name)
        if not message:
            return "[no-send] %s handled the event without posting" % self.name + quoted(
                " (%s: %s)" % (status, detail[:120])) + "\n"
        head = {"timeout": "%s did not finish this in time (%s)." % (self.name, detail),
                "gone": "%s stopped before answering (%s)." % (self.name, detail)}.get(
                    status, "%s could not answer this (%s: %s)." % (self.name, status, detail))
        return quoted(head + ("\n\nPartial reply:\n" + said if said else "")) + "\n"

    def handle(self, name):
        task_id = name[:-4]
        path = os.path.join(self.args.inbox, name)
        result_file = os.path.join(self.args.results, name)
        headers, message, operator_lines = parse_task(path)
        tier = admission(headers, self.args.allow_non_owner)
        if tier is None:
            log("refused (non-owner) ->", task_id, headers["access_tier"])
            publish(result_file, NON_OWNER_REFUSED)
            return {"task": task_id, "status": "refused", "tier": headers["access_tier"]}
        prompt = wake_prompt(task_id, headers, message, operator_lines, self.args.cwd, self.note)
        event, _ = trigger_of(headers)
        log("wake ->", task_id, headers["access_tier"], event or "message")
        t0 = time.time()
        status, text, detail, turn = self.run_turn(task_id, headers, prompt, tier == "owner")
        publish(result_file, self.answer_body(headers, status, text, detail))
        return {"task": task_id, "status": status, "detail": detail[:200], "event_type": event,
                "tier": headers["access_tier"], "turn_s": round(time.time() - t0, 3), "permissions": turn.decisions}

    def pending_tasks(self, seen):
        now = time.time()
        names = [n for n in os.listdir(self.args.inbox)
                 if n.endswith(".txt") and TASK_NAME_RE.fullmatch(n[:-4]) and n[:-4] not in seen
                 and self.retry_at.get(n[:-4], 0) <= now and not os.path.exists(os.path.join(self.args.results, n))]
        names.sort(key=lambda n: (os.stat(os.path.join(self.args.inbox, n)).st_mtime, n))
        return names

    def serve(self):
        done_path = os.path.join(self.args.state, "done")
        metrics = os.path.join(self.args.state, "metrics.jsonl")
        seen = set(load_done(done_path))
        done = 0
        idle_since = time.time()
        while not stop_requested():
            names = self.pending_tasks(seen)
            if not names:
                if self.args.once and done >= self.args.once:
                    return
                if self.args.idle_exit and time.time() - idle_since > self.args.idle_exit:
                    log("idle-exit")
                    return
                time.sleep(self.args.poll)
                continue
            name = names[0]
            try:
                rec = self.handle(name)
            except Exception as e:  # noqa: BLE001 - one task's failure must not end the others
                self.retry_at[name[:-4]] = time.time() + RETRY_S
                log("failed, retrying in %ss ->" % RETRY_S, name[:-4], "%s: %s" % (type(e).__name__, e))
                continue
            seen.add(name[:-4])
            record_done(done_path, name[:-4])
            with open(metrics, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            log("done <-", json.dumps(rec)[:400])
            done += 1
            idle_since = time.time()


# ── entry ────────────────────────────────────────────────────────────────────

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="dir holding tasks/ results/ state/")
    ap.add_argument("--held", action="store_true", help="the launcher holds the work root and attaches this "
                    "process as its consumer; take no lock (needs --root-fd)")
    ap.add_argument("--root-fd", type=int, default=None, help="with --held: the held root's directory descriptor")
    ap.add_argument("--runtime", choices=sorted(RUNTIMES), default=DEFAULT_RUNTIME,
                    help="gemini (Gemini CLI, the default) or antigravity; names the agent and its default command")
    ap.add_argument("--acp-command", default=None, help="the ACP agent's command line (default: the runtime's, "
                    "gemini --acp or agy acp)")
    ap.add_argument("--cwd", default=None, help="the Session working directory (default: the current directory)")
    ap.add_argument("--session-store", default=None,
                    help="the sessions.json to resume from and record to (default: <work>/state/sessions.json)")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="serve non-owner senders without collaborator access too (no file operations granted)")
    ap.add_argument("--once", type=count_arg, default=0, help="exit after N tasks")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--poll", type=float, default=0.2)
    ap.add_argument("--turn-timeout", type=float, default=900)
    ap.add_argument("--rpc-timeout", type=float, default=60)
    ap.add_argument("--interrupt-grace", type=float, default=15)
    return ap


def _prepare(args):
    args.acp_argv = shlex.split(args.acp_command or RUNTIMES[args.runtime][1])
    if not args.acp_argv:
        raise SystemExit("--acp-command is empty")
    args.cwd = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))
    args.child_env = agent_env()


def _paths(args, base):
    args.inbox = os.path.join(base, "tasks")
    args.results = os.path.join(base, "results")
    args.state = os.path.join(base, "state")
    args.session_store = os.path.abspath(os.path.expanduser(args.session_store)) if args.session_store \
        else os.path.join(args.state, SESSIONS_NAME)


def main(argv=None):
    args = build_parser().parse_args(argv)
    _prepare(args)
    args.work = os.path.abspath(args.work)
    exit_on_sigterm()
    if args.held:
        if args.root_fd is None:
            build_parser().error("--held needs --root-fd")
        return serve_held(args)
    owner = None
    try:
        owner = acquire_lock(args.work, "gemini")
        make_dirs(owner, ("tasks", "results", "state"))
        args.work = root_path(owner, args.work)
        _paths(args, args.work)
        return exit_code(serve_locked(args))
    except LockRefused as e:
        sys.stderr.write(e.line + "\n")
        return e.code
    finally:
        if owner is not None:
            release_lock(owner)


def serve_held(args):
    """Under a launcher: start only once the holder's record names this very process, alive; then live in
    the held inode (fchdir), with tasks/, results/ and state/ relative to it."""
    refused = held_start.enter(args.root_fd, HELD_WAIT_S, holder_consumer)
    if refused is not None:
        return refused
    _paths(args, ".")
    return exit_code(serve_locked(args))


def serve_locked(args):
    Connector(args).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
