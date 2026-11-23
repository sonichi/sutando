#!/usr/bin/env python3
"""Wake an OpenClaw agent over ACP for each task file Sparrow queues.

Transport: the Agent Client Protocol over the stdio of `openclaw acp` (or `openclaw --profile <p> acp`),
the same wake agent-connect uses: per task, spawn the ACP command, `initialize`, resume the task's
Session with `session/load` (else `session/new`), send one `session/prompt` and wait for it to stop.

`<dir>/tasks/task-*.txt` in. The agent answers each task itself with `ag2-sparrow-openclaw reply
<result_file>` (text on stdin) or `no-reply <result_file>`, which publish `<dir>/results/<task>.txt`
for Sparrow to deliver. A message the agent ended without either is answered with the turn's own text,
as agent-connect would post it; an event with neither posts nothing.

Sessions are keyed like agent-connect's, (room, access tier; a collaborator's own per room), stored in
agent-connect's `sessions.json` shape, and never retired: a Session is replaced only when the agent
refuses to resume it or the working directory changed. Tier policy is task_io.turn_policy's: the owner and
an attested collaborator are served, with file operations under the working directory; every turn may run
its reply command. Any other non-owner is refused, --allow-non-owner or not: its restricted turn must be
isolated, connector-free and ephemeral, and this adapter runs every turn in the owner's OpenClaw profile.
Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
import time
from pathlib import Path

from ... import acp_host, held_start, result_labels, result_publish
from ...acp_host import (MESSAGE_EVENT, SESSIONS_NAME, TASK_NAME_RE, SessionStore, at_or_above_home,  # noqa: F401
                         delivery_markers, is_message, quoted, request_paths, resolved, session_key, trigger_of, under)
from ...relay_loop import count_arg
from ...task_io import (NON_OWNER_NOTE, NON_OWNER_REFUSED, LockRefused, acquire_lock, turn_policy, exit_code,
                        exit_on_sigterm, holder_consumer, load_done, make_dirs, parse_task, record_done,
                        release_lock, root_path, stop_requested)
from .acp_client import AcpClient

ANSWERED_DIR = "openclaw-answered"     # under state/: one O_EXCL claim per answered task
HELD_WAIT_S = held_start.WAIT_S
# Said in the room: the person who asked gets a line, as for a timeout or a failure.
NO_ANSWER = "OpenClaw finished without an answer."
EVENT_NO_ANSWER = "[no-send] OpenClaw handled the event without posting"
DRAFTS_DIR = "openclaw-drafts"   # under state/: the text an agent's `reply --draft` posts
PUBLISHED = b"published\n"        # written into a claim once its result is published
RETRY_S = 60.0


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ── answers: the claim the reply command and the connector's fallback share ──

def answered_dir(state_dir):
    return Path(state_dir) / ANSWERED_DIR


def claim_answer(state_dir, task_id):
    """True for the first caller per task: whoever claims publishes its one result."""
    d = answered_dir(state_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.close(os.open(str(d / task_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def is_answered(state_dir, task_id):
    return (answered_dir(state_dir) / task_id).exists()


def release_answer(state_dir, task_id):
    with contextlib.suppress(OSError):
        (answered_dir(state_dir) / task_id).unlink()


def publish_answer(result_file, body, title=None, outcome=None):
    """Labels first, so the drain that reads the result finds them, then the body, staged and renamed."""
    labels, notes = result_publish.result_labels_for(body, title, outcome)
    result_labels.write(Path(result_file), labels, body)
    result_publish.publish_text(result_file, body)
    return notes


def answer_once(state_dir, task_id, result_file, body, title=None, outcome=None):
    """(True, notes) when this caller claimed the task and published `body`; (False, []) when it was already
    answered. A failed publication releases the claim and raises, so the task can be answered again."""
    if not claim_answer(state_dir, task_id):
        return False, []
    try:
        notes = publish_answer(result_file, body, title, outcome)
    except BaseException:
        release_answer(state_dir, task_id)
        raise
    with contextlib.suppress(OSError), open(answered_dir(state_dir) / task_id, "wb") as f:
        f.write(PUBLISHED)
    return True, notes


def release_unpublished(state_dir, task_id, result_file):
    """Release `task_id`'s claim when it was never marked published and no result exists: a reply process
    that claimed and then died (killed between claiming and publishing). True when released. Called after
    the turn, when the agent's process, and so its reply command, has ended."""
    claim = answered_dir(state_dir) / task_id
    try:
        stranded = claim.read_bytes() != PUBLISHED and not Path(result_file).exists()
    except OSError:
        return False
    if stranded:
        release_answer(state_dir, task_id)
    return stranded


def recover_claims(state_dir, task_dir, result_dir):
    """Release each claim a crash left between claiming and publishing: never marked published, its task still
    open and no result written. Run at start, when no turn (and so no reply command) is in flight."""
    released = []
    d = answered_dir(state_dir)
    for claim in sorted(d.iterdir()) if d.is_dir() else []:
        tid = claim.name
        try:
            stranded = (claim.read_bytes() != PUBLISHED and (Path(task_dir) / (tid + ".txt")).is_file()
                        and not (Path(result_dir) / (tid + ".txt")).exists())
        except OSError:
            continue
        if stranded:
            release_answer(state_dir, tid)
            released.append(tid)
    return released


# ── the permission policy ────────────────────────────────────────────────────

def draft_for(result_file):
    """Where the agent writes a reply's text: `<root>/state/openclaw-drafts/<task>.txt`, beside its result_file's root."""
    result_file = Path(result_file)
    return result_file.parent.parent / "state" / DRAFTS_DIR / result_file.name


def reply_commands(reply_command, result_file):
    """The only two command lines an agent is ever granted for a task, written here, never by the agent."""
    q = shlex.quote(str(result_file))
    return ("%s reply %s --draft" % (reply_command, q), "%s no-reply %s" % (reply_command, q))


def is_reply_command(command, reply_command, result_file):
    """True only for one of this task's two lines exactly (a list: exactly its argv); nothing else is parsed."""
    if not reply_command or not result_file:
        return False
    allowed = reply_commands(reply_command, result_file)
    if isinstance(command, list):
        return command in [shlex.split(c) for c in allowed]
    return isinstance(command, str) and command in allowed


def is_draft(root, raw, result_file):
    """`raw` names this task's reply draft itself: compared as written (.. and ~ normalised, links not
    followed), and the draft is not a link, so a link placed at its path cannot hand out its target."""
    if "\0" in str(raw):
        return False
    draft = os.path.abspath(str(draft_for(result_file)))
    p = Path(raw).expanduser()
    if os.path.abspath(str(Path(root) / p if not p.is_absolute() else p)) != draft:
        return False
    return not os.path.islink(draft)


def decide_permission(params, writes, cwd, reply_command, result_file):
    """(allowed, reason, result). Default deny (acp_host's policy). Granted: this task's own reply or no-reply
    line, exactly; a read or edit of this task's reply draft; and, for a turn with writes, a read or edit whose every
    path, nested ones included, resolves under the working directory. Every other command, execute, fetch,
    delete or move is refused, as the codex adapter declines every approval it is asked for."""
    return acp_host.decide_permission(
        params, writes, cwd, command_ok=lambda command: is_reply_command(command, reply_command, result_file),
        own_file=lambda raw: is_draft(cwd, raw, result_file),
        command_refused="no command is granted but this task's reply line",
        command_granted="this task's reply command", own_file_reason="this task's reply draft")


# ── the prompt ───────────────────────────────────────────────────────────────

def wake_prompt(task_id, headers, message, operator_lines, result_file, reply_command, note=NON_OWNER_NOTE):
    event, _ = trigger_of(headers)
    out = acp_host.task_framing(task_id, headers, message, operator_lines, "post with the reply command", note)
    post, skip = reply_commands(reply_command, result_file)
    out.append("Answer through Sparrow, exactly once for this task. To post, write the reply's exact text to the file "
               "%s, then run exactly this command line, alone and unchanged:\n%s\n"
               "To post nothing (an acknowledgement, thanks or FYI that needs no answer, or an event whose body asks "
               "for no post), run exactly:\n%s\n"
               "No other command line is granted. Exit 0 means Sparrow has it; do not run either again for this task."
               % (draft_for(result_file), post, skip))
    if event in (None, MESSAGE_EVENT):
        out.append("If you run neither, your final message in this turn is posted as the reply.")
    else:
        out.append("If you run neither, nothing is posted.")
    return "\n\n".join(out)


# ── one turn ─────────────────────────────────────────────────────────────────

class Connector:
    def __init__(self, args, client_factory=AcpClient, note=None):
        self.args = args
        self.client_factory = client_factory
        self.note = note if note is not None else NON_OWNER_NOTE
        self.store = SessionStore(args.session_store)
        self.retry_at = {}

    def run_turn(self, task_id, headers, prompt, result_file, writes):
        """(status, text, detail, turn): status is completed | cancelled | timeout | gone | failed."""
        cwd = self.args.cwd
        return acp_host.run_prompt(
            self.client_factory, self.args, headers, task_id, prompt,
            lambda params: decide_permission(params, writes, cwd, self.args.reply_command, result_file), self.store,
            "openclaw-acp.stderr", "OpenClaw is not signed in (%s); sign in to OpenClaw on this host", log)

    def fallback_body(self, headers, status, text, detail):
        """What the connector posts when the agent did not answer through `reply`. The turn's own text is
        posted with its delivery markers neutralised: `reply` refuses a marker, and this path must not be
        the way round that refusal (an attachment, a redirect or a skip written into the final text)."""
        if not is_message(headers):  # the leading [no-send] stays live; what follows it is quoted
            return EVENT_NO_ANSWER + quoted("" if status == "completed" else " (%s: %s)" % (status, detail[:120])) + "\n"
        said = (text or "").strip()
        if status == "completed":
            return quoted(said or NO_ANSWER) + "\n"
        # `detail` is OpenClaw's own text too (an error's message, a stopReason): the whole body is quoted.
        head = {"timeout": "OpenClaw did not finish this in time (%s)." % detail,
                "gone": "OpenClaw stopped before answering (%s)." % detail}.get(
                    status, "OpenClaw could not answer this (%s: %s)." % (status, detail))
        return quoted(head + ("\n\nPartial reply:\n" + said if said else "")) + "\n"

    def handle(self, name):
        task_id = name[:-4]
        path = os.path.join(self.args.inbox, name)
        result_file = os.path.join(self.args.results_abs, name)
        headers, message, operator_lines = parse_task(path)
        policy = turn_policy(headers, self.args.allow_non_owner)
        # A restricted turn must be isolated, connector-free and ephemeral; OpenClaw runs in the owner's profile.
        if not policy.served or policy.capabilities.workspace != "owner":
            log("refused (non-owner%s) ->" % ("" if policy.served else ", " + policy.unlock + " not set"),
                task_id, headers["access_tier"])
            answer_once(self.args.state, task_id, os.path.join(self.args.results, name), NON_OWNER_REFUSED)
            return {"task": task_id, "status": "refused", "tier": headers["access_tier"]}
        prompt = wake_prompt(task_id, headers, message, operator_lines, result_file, self.args.reply_command,
                             self.note)
        event, _ = trigger_of(headers)
        log("wake ->", task_id, headers["access_tier"], event or "message")
        t0 = time.time()
        status, text, detail, turn = self.run_turn(task_id, headers, prompt, result_file,
                                                   policy.capabilities.writes)
        results_file = os.path.join(self.args.results, name)
        if release_unpublished(self.args.state, task_id, results_file):
            log("a reply claimed and never published; answering with the fallback ->", task_id)
        published, _ = answer_once(self.args.state, task_id, os.path.join(self.args.results, name),
                                   self.fallback_body(headers, status, text, detail))
        by = "connector" if published else "agent"
        return {"task": task_id, "status": status, "detail": detail[:200], "answered_by": by,
                "event_type": event, "tier": headers["access_tier"], "turn_s": round(time.time() - t0, 3),
                "permissions": turn.decisions}

    def pending_tasks(self, seen):
        now = time.time()
        names = [n for n in os.listdir(self.args.inbox)
                 if n.endswith(".txt") and TASK_NAME_RE.fullmatch(n[:-4]) and n[:-4] not in seen
                 and self.retry_at.get(n[:-4], 0) <= now
                 and not is_answered(self.args.state, n[:-4])
                 and not os.path.exists(os.path.join(self.args.results, n))]
        names.sort(key=lambda n: (os.stat(os.path.join(self.args.inbox, n)).st_mtime, n))
        return names

    def serve(self):
        done_path = os.path.join(self.args.state, "done")
        metrics = os.path.join(self.args.state, "metrics.jsonl")
        seen = set(load_done(done_path))
        for tid in recover_claims(self.args.state, self.args.inbox, self.args.results):
            log("released a claim left unpublished ->", tid)
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

def child_env(environ=None):
    """The ACP agent's environment: the connector's own (the launcher already narrowed it)."""
    return dict(os.environ if environ is None else environ)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="dir holding tasks/ results/ state/")
    ap.add_argument("--held", action="store_true", help="the launcher holds the work root and attaches this "
                    "process as its consumer; take no lock (needs --root-fd)")
    ap.add_argument("--root-fd", type=int, default=None, help="with --held: the held root's directory descriptor")
    ap.add_argument("--acp-command", default="openclaw acp",
                    help="the ACP agent's command line (default: openclaw acp; a profile: openclaw --profile <p> acp)")
    ap.add_argument("--cwd", default=None, help="the Session working directory (default: the current directory)")
    ap.add_argument("--reply-command", required=True,
                    help="the command line the agent answers with (the absolute ag2-sparrow-openclaw)")
    ap.add_argument("--session-store", default=None,
                    help="the sessions.json to resume from and record to (default: <work>/state/sessions.json); "
                         "agent-connect's own file keeps its conversations")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="recorded but not honoured here: a restricted turn must be isolated, and this adapter "
                         "runs every turn in the owner's OpenClaw profile")
    ap.add_argument("--once", type=count_arg, default=0, help="exit after N tasks")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--poll", type=float, default=0.2)
    ap.add_argument("--turn-timeout", type=float, default=900)
    ap.add_argument("--rpc-timeout", type=float, default=60)
    ap.add_argument("--interrupt-grace", type=float, default=15)
    return ap


def _prepare(args):
    args.acp_argv = shlex.split(args.acp_command)
    if not args.acp_argv:
        raise SystemExit("--acp-command is empty")
    args.cwd = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))
    args.child_env = child_env()


def _paths(args, base):
    args.inbox = os.path.join(base, "tasks")
    args.results = os.path.join(base, "results")
    args.state = os.path.join(base, "state")
    args.results_abs = os.path.abspath(os.path.join(args.work_abs, "results"))
    args.session_store = os.path.abspath(os.path.expanduser(args.session_store)) if args.session_store \
        else os.path.join(args.state, SESSIONS_NAME)


def main(argv=None):
    args = build_parser().parse_args(argv)
    _prepare(args)
    args.work_abs = args.work = os.path.abspath(args.work)
    exit_on_sigterm()
    if args.held:
        if args.root_fd is None:
            build_parser().error("--held needs --root-fd")
        return serve_held(args)
    owner = None
    try:
        owner = acquire_lock(args.work, "openclaw")
        make_dirs(owner, ("tasks", "results", "state"))
        args.work_abs = args.work = root_path(owner, args.work)
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
