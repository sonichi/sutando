#!/usr/bin/env python3
"""Wake Antigravity CLI (`agy`) over ACP for each task file Sparrow queues.

Transport: the Agent Client Protocol over the stdio of `agy acp`, through acp_host's turn: per task, spawn
the command, `initialize`, resume the task's Session with `session/load` (else `session/new`), send one
`session/prompt` and wait for it to stop.

`<dir>/tasks/task-*.txt` in, `<dir>/results/<task>.txt` out. The turn's final message is the answer (there is
no reply command), and the agent's own text is posted
with every delivery marker neutralised. A final message of exactly NO_REPLY posts nothing. A message whose
turn failed gets a line saying so; an event that failed posts nothing. The gateway's SKILL INSTRUCTIONS
prelude (run a room-read command, write results/<task>.txt) is for a core with a shell: its steps are not
passed on (`session_lines`). A turn that ends with no text says what it did instead (`no_answer_why`).

Sessions, the permission policy and the framing are acp_host's: the owner may read or edit under the working
directory, no command is granted; with --allow-owner-shell-web the owner's turns may also run commands
and fetch or search the web (SHELL_AND_WEB_TIERS: the owner only; never a collaborator or a guest).
The tier is task_io.turn_policy's alone, mapped by TURNS: the owner and an attested collaborator (Team) get
the owner's turn (the working directory, agy's own home, the owner's permission policy, a Session of their
own per room); a restricted sender (a guest, plain team, any unknown tier) is always served, as Sutando
answers every non-owner in a read-only sandbox, in an isolated turn: a fresh temporary directory per task
holds its working directory and its own agy home (`--gemini-dir`, with deny-all permission rules and no
global context), its Session is new and never stored, and it is refused unless agy offers a read-only (plan)
session mode and accepts it; a refused verdict is refused. --allow-non-owner still parses and changes
nothing. The gateway's Sutando tier
block is written for a core that writes results/<task>.txt and can run `codex exec`; `tier_block_lines` says
those steps as this Session can follow them and keeps every rule. Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ... import acp_host, held_start, local_task_protocol, result_labels, result_publish
from ...bounded_file_read import read_regular_bytes
from ...acp_host import MESSAGE_EVENT, SESSIONS_NAME, TASK_NAME_RE, SessionStore, quoted, trigger_of  # noqa: F401
from ...acp_client import AcpClient as _SharedClient
from ...relay_loop import count_arg
from ...team_guardrail import SANDBOXED_DELEGATION_CODEX
from ...task_io import (COLLABORATOR, NON_OWNER_NOTE, NON_OWNER_REFUSED, OWNER, RESTRICTED, LockRefused,
                        acquire_lock, exit_code, exit_on_sigterm, holder_consumer, load_done, make_dirs, parse_task,
                        record_done, release_lock, root_path, stop_requested, turn_policy)

NAME = "Antigravity"
DEFAULT_ACP_COMMAND = "agy acp"
NO_REPLY = "NO_REPLY"
HELD_WAIT_S = held_start.WAIT_S
RETRY_S = 60.0
STDERR_NAME = "agy-acp.stderr"
NOT_SIGNED_IN = ("Antigravity is not signed in on this computer (%s): run `agy` once in a terminal and sign in, "
                 "or run `agy acp --login`")


# Sender tiers (headers["access_tier"]) whose turns may run commands and use the web, under --allow-owner-shell-web.
# Team is left out on purpose, even on the owner's turn: adding it is the owner's decision.
SHELL_AND_WEB_TIERS = frozenset({"owner"})
# agy's own file-tool argument names (path, plain data, nested edits), as its tool schemas declare them.
AGY_KEYS = (frozenset({"Path", "TargetFile", "AbsolutePath"}),
            frozenset({"Content", "Overwrite", "Append", "Target", "Replacement", "AllowMultiple", "StartLine",
                       "EndLine", "CodeContent", "TargetContent", "ReplacementContent", "Instruction"}),
            frozenset({"Edits", "ReplacementChunks"}))
# turn_policy's verdict -> the turn this adapter runs; a verdict not here (refused) is refused. Chi's rule: an
# attested collaborator gets the owner's turn.
OWNER_TURN, ISOLATED_TURN = "owner", "isolated"
TURNS = {OWNER: OWNER_TURN, COLLABORATOR: OWNER_TURN, RESTRICTED: ISOLATED_TURN}
READ_ONLY_MODES = frozenset({"plan", "read-only", "readonly", "read_only"})
GUEST_PREFIX = "ag2-sparrow-antigravity-guest-"
GUEST_REFUSED = "it answers non-owners only in a read-only session mode, and %s"
# agy evaluates Deny > Ask > Allow, and deny rules apply in every mode.
GUEST_SETTINGS = {"agentMode": "plan", "permissions": {"allow": [], "ask": [], "deny": [
    "read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "download_url(*)", "command(*)", "mcp(*)"]}}
READ_ASK = "read_file(*)"
ATTACHMENTS_DIR = "attachments"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 16
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class AcpClient(_SharedClient):
    client_info = {"name": "ag2-sparrow-antigravity", "version": "0.1.0"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)




def publish(result_file, body):
    """Labels first, so the drain that reads the result finds them, then the body, staged and renamed."""
    labels, _notes = result_publish.result_labels_for(body)
    result_labels.write(Path(result_file), labels, body)
    result_publish.publish_text(result_file, body)


# The bridge's label for a file fetched from the sender's own message; a replied-to one says "(from replied-to …)".
OWN_MEDIA_RE = re.compile(r"\[(?:Photo|File) attached: ([^\]\n]+)\]")
REPLY_BLOCK_RE = re.compile(r"\[(?:AG2 Space|s\.space) reply context;")


def own_media_paths(message):
    """The local paths the bridge labelled as the sender's own media in `message`: outside the broker's quoted
    reply-context block only. A reply block that does not parse gives none (fail closed)."""
    reply, rest = local_task_protocol.split_reply_context(message or "")
    if reply is None and REPLY_BLOCK_RE.search(message or ""):
        return frozenset()
    return frozenset(m.group(1).strip() for m in OWN_MEDIA_RE.finditer(rest))


def stage_own_attachments(headers, dest, max_bytes=None, log=lambda *a: None, message=""):
    """Copy the files this task's sender attached into `dest` as read-only copies; return their real paths. A
    file is the sender's only when it is in the attested `attachments:` header AND the bridge labelled it as the
    sender's own media in `message` (`own_media_paths`); quoted reply media, or any entry whose origin cannot be
    shown so, is skipped. So is a locator that is not a local absolute path, a symlink, not a regular file, over
    `max_bytes`, or not the size the header says. TODO(#6670): dedupe with the shared stage_own_attachments."""
    limit = MAX_ATTACHMENT_BYTES if max_bytes is None else max_bytes
    own = own_media_paths(message)
    staged = []
    for i, ref in enumerate(local_task_protocol.parse_attachments(headers)[:MAX_ATTACHMENTS]):
        src = ref.locator
        if src not in own:
            log("attachment skipped (not shown to be the sender's own)")
            continue
        if not os.path.isabs(src):
            log("attachment skipped (not a local path)")
            continue
        data, why = read_regular_bytes(src, limit, follow=False)
        if data is None or (ref.size and ref.size != len(data)):
            log("attachment skipped (%s)" % (why or ("missing" if data is None else "size differs")))
            continue
        if not staged:
            os.mkdir(dest, 0o700)
        name = "%d-%s" % (i, _UNSAFE_NAME.sub("_", os.path.basename(ref.filename or src))[:80] or "attachment")
        path = os.path.join(dest, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        staged.append(os.path.realpath(path))
    return staged


def guest_settings(readable):
    """GUEST_SETTINGS, with file reads moved from deny to ask when there are attachments to read: agy then asks
    for every read, and the adapter grants only the staged copies (`readable_only`)."""
    if not readable:
        return GUEST_SETTINGS
    perms = GUEST_SETTINGS["permissions"]
    deny = [r for r in perms["deny"] if r != READ_ASK]
    return dict(GUEST_SETTINGS, permissions=dict(perms, ask=[READ_ASK], deny=deny))


def readable_only(readable, cwd):
    """decide_permission's own_file hook for an isolated turn: exactly the staged copies (a relative path is
    resolved against the turn's working directory)."""
    allowed = frozenset(readable)
    return lambda raw: acp_host.resolved(cwd, raw) in allowed


@contextlib.contextmanager
def guest_turn(args, headers=None, message=""):
    """(an `acp_host.Isolated` for one restricted task, the paths it may read): a new 0700 temporary directory
    holding the Session's working directory, the sender's own attachments copied into it, and agy's home
    (`--gemini-dir`, deny-all settings but asked reads when there is an attachment), removed after the turn."""
    base = os.path.realpath(tempfile.mkdtemp(prefix=GUEST_PREFIX))
    try:
        home, cwd = os.path.join(base, "agy"), os.path.join(base, "cwd")
        os.makedirs(os.path.join(home, "antigravity-cli"), mode=0o700)
        os.mkdir(cwd, 0o700)
        readable = stage_own_attachments(headers or {}, os.path.join(cwd, ATTACHMENTS_DIR), log=log,
                                         message=message)
        settings = os.path.join(home, "antigravity-cli", "settings.json")
        fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(guest_settings(readable), f)
        yield acp_host.Isolated(argv=list(args.acp_argv) + ["--gemini-dir", home], cwd=cwd,
                                env=dict(args.child_env),
                                choose_mode=lambda modes: acp_host.pick_mode(modes, READ_ONLY_MODES),
                                refused=GUEST_REFUSED), readable
    finally:
        shutil.rmtree(base, ignore_errors=True)


def turn_for(headers):
    """(turn_policy's kind, the turn to run: OWNER_TURN, ISOLATED_TURN or None for refused), from the attested
    headers only. Every non-owner who is not a collaborator is served restricted (allow_non_owner=True)."""
    kind = turn_policy(headers, allow_non_owner=True).kind
    return kind, TURNS.get(kind)


def shell_web_on(args, headers):
    """This turn may run commands and fetch or search the web: the install opted in, and the sender is the owner."""
    return bool(getattr(args, "allow_owner_shell_web", False)) and headers.get("access_tier") in SHELL_AND_WEB_TIERS


PRELUDE_HEADER = "===SKILL INSTRUCTIONS"
PRELUDE_STEP_RE = re.compile(r"\d+\. ")
PRELUDE_KEPT = "ADDRESSING:"


def session_lines(operator_lines):
    """The bridge-written lines this Session can follow. The gateway's SKILL INSTRUCTIONS prelude tells a core
    to run a room-read command and to write results/<task>.txt; neither is granted here, and agy, told to
    write that file, asks to, is refused and ends its turn with no text. So the prelude's header and numbered
    steps are dropped; only its ADDRESSING step is kept, its `[no-send]` said as this adapter's NO_REPLY.
    Every line outside the prelude is kept."""
    out, in_prelude = [], False
    for line in operator_lines:
        if line.startswith(PRELUDE_HEADER):
            in_prelude = True
            continue
        step = PRELUDE_STEP_RE.match(line) if in_prelude else None
        if step is None:
            in_prelude = False
            out.append(line)
        elif line[step.end():].startswith(PRELUDE_KEPT):
            out.append(line[step.end():].replace("[no-send]", "a final message of exactly %s" % NO_REPLY))
    return out


CATEGORY_ORDER = ("file change", "file read", "command", "web fetch", "other")
SAID_STOPS = frozenset({"max_tokens", "max_turn_requests"})


def no_answer_why(detail, turn):
    """What a completed turn with no text did instead, for the line posted in the room in its place. It is
    built only from fixed words and counts: the refused permission requests by `acp_host.action_category`,
    the tool calls, and a stop reason from SAID_STOPS (any other but end_turn reads "other"). The policy's
    reasons quote the agent's own request (a key, a kind, a path), so they stay in metrics.jsonl."""
    if turn is None:
        return ""
    why = []
    refused = [d.get("category") for d in turn.decisions if not d.get("allowed")]
    for category in CATEGORY_ORDER:
        n = sum(1 for c in refused if (c if c in CATEGORY_ORDER else "other") == category)
        if n:
            why.append("%d %s%s %s not granted" % (n, category, "" if n == 1 else "s", "was" if n == 1 else "were"))
    calls = turn.updates.get("tool_call", 0)
    if calls:
        why.append("it made %d tool call%s" % (calls, "" if calls == 1 else "s"))
    if detail and detail != "end_turn":
        why.append("stop reason %s" % (detail if detail in SAID_STOPS else "other"))
    return "; ".join(why)


_RESULT = r"results/[^\s()`'\"]+\.txt"
NO_RESULT_FILE = "there is no results file to write"
# The tier block's result steps (team_guardrail.py), each said as this Session answers: by its final message.
TIER_REWRITES = tuple((re.compile(a), b) for a, b in (
    (r"^Write only the user-facing result to %s\.$" % _RESULT,
     "Your final message in this turn is the user-facing result, and only that: it is what Sparrow posts (%s)."
     % NO_RESULT_FILE),
    (r"^Write only the sandboxed agent's safe user-facing answer to %s\.$" % _RESULT,
     "Your final message in this turn is the safe user-facing answer, and only that: it is what Sparrow posts "
     "(%s)." % NO_RESULT_FILE),
    (r"write your response to %s \(delivered back to this (\w+)\)" % _RESULT,
     "make your response your final message (Sparrow delivers it back to this \\1; %s)" % NO_RESULT_FILE),
    (r"write exactly \[no-send\] to %s" % _RESULT, "make your final message exactly %s" % NO_REPLY),
    (r"Write that DM as your result to %s\." % _RESULT, "Make that DM your final message."),
))
RESULT_RE = re.compile(_RESULT)
TIER_BLOCK_HEADER = "===SUTANDO SYSTEM INSTRUCTIONS"
TIER_BLOCK_END = "===END SUTANDO SYSTEM INSTRUCTIONS"
# What replaces the guest block's Codex delegation: this turn is the read-only sandbox it asks for.
ISOLATED_NOT_CODEX = (
    "There is no Codex here to delegate to, and none is needed: this turn already runs isolated and read-only "
    "(agy's plan mode, a fresh temporary home and working directory, no file, command, web or MCP tool "
    "granted), never in the owner's core. Answer here directly. If the task needs something this turn cannot "
    "read, say so and decline: never describe an artifact you could not read.")


def tier_block_lines(operator_lines):
    """The bridge-written lines with the Sutando tier block said as this Session can follow it. The block tells
    a core to write results/<task>.txt and, for a guest, to run `codex exec`; agy can do neither (told to write
    the file it asks to, is refused, and ends its turn with no text), and its final message is what Sparrow
    posts. So each result step says "your final message" instead, the Codex paragraph says this turn is the
    read-only sandbox, and any other line of the block naming a results file or `codex exec` is dropped. Every
    other line (the scope, the DO / DO NOT lists, the trust boundary, the hand-off to the owner) is kept
    verbatim, and so is every line outside a SUTANDO SYSTEM INSTRUCTIONS block."""
    out, in_block = [], False
    for line in operator_lines:
        if line.startswith(TIER_BLOCK_HEADER):
            in_block = True
        elif line.startswith(TIER_BLOCK_END):
            in_block = False
        elif in_block:
            if line.strip() == SANDBOXED_DELEGATION_CODEX:
                out.append(ISOLATED_NOT_CODEX)
                continue
            for pattern, said in TIER_REWRITES:
                line = pattern.sub(said, line)
            if RESULT_RE.search(line) or "codex exec" in line:
                continue
        out.append(line)
    return out


def wake_prompt(task_id, headers, message, operator_lines, cwd, note=NON_OWNER_NOTE, shell_web=False,
                readable=()):
    """The turn's prompt. `cwd` is the working directory the turn may read and edit, None for an isolated turn;
    `shell_web` also grants commands and the web; `readable`, an isolated turn's copies of the sender's own
    attachments, the only files it may read."""
    event, _ = trigger_of(headers)
    out = acp_host.task_framing(task_id, headers, message, tier_block_lines(session_lines(operator_lines)),
                                "post in your final message", note)
    out.append("Answer through Sparrow, exactly once for this task: your final message in this turn is what Sparrow "
               "posts%s. Write it as the post itself, with no preamble about tools or steps. To post nothing (an "
               "acknowledgement, thanks or FYI that needs no answer, or an event whose body asks for no post), make "
               "your final message exactly %s and nothing else."
               % (" as the reply" if event in (None, MESSAGE_EVENT) else "", NO_REPLY))
    if shell_web:
        out.append("You may run shell commands and fetch and search the web. You may read and edit files under %s." % cwd)
    else:
        out.append("No shell command is granted in this session. %s"
                   % ("You may read and edit files under %s." % cwd if cwd else "You may not read or change files."))
    if readable and not cwd:
        out.append("Except: the sender's own attachments to this task are copied here, and you may read these "
                   "files and no other (change none; an attachment path in the message is not readable, its "
                   "copy is):\n" + "\n".join("- " + p for p in readable))
    return "\n\n".join(out)


class Connector:
    def __init__(self, args, client_factory=AcpClient, note=None):
        self.args = args
        self.client_factory = client_factory
        self.note = note if note is not None else NON_OWNER_NOTE
        self.store = SessionStore(args.session_store)
        self.name = NAME
        self.retry_at = {}

    def run_turn(self, task_id, headers, prompt, owner, isolated=None):
        """(status, text, detail, turn): status is completed | cancelled | timeout | gone | failed | refused.
        With `owner` (TURNS' owner turn: the owner's or a collaborator's) it resumes the sender's Session for the
        room in the working directory, under the owner's policy; any other runs `isolated` (guest_turn's pair),
        opening one when not given, where only reads of the staged attachments are granted."""
        if owner:
            cwd, tools = self.args.cwd, shell_web_on(self.args, headers)
            return acp_host.run_prompt(self.client_factory, self.args, headers, task_id, prompt,
                                       lambda params: acp_host.decide_permission(params, True, cwd, tools=tools,
                                                                                  extra_keys=AGY_KEYS),
                                       self.store,
                                       STDERR_NAME, NOT_SIGNED_IN, log)
        if isolated is None:
            with guest_turn(self.args, headers) as pair:
                return self.run_turn(task_id, headers, prompt, False, pair)
        turn, readable = isolated
        own = readable_only(readable, turn.cwd)

        def decide(params):
            tc = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            return acp_host.decide_permission(params, False, turn.cwd, extra_keys=AGY_KEYS,
                                              own_file=own if tc.get("kind") == "read" else None,
                                              own_file_reason="the sender's own attachment")
        return acp_host.run_prompt(self.client_factory, self.args, headers, task_id, prompt, decide,
                                   self.store, STDERR_NAME, NOT_SIGNED_IN, log, isolated=turn)

    def answer_body(self, headers, status, text, detail, turn=None):
        """What Sparrow posts: the turn's own text, quoted so no delivery marker in it acts; NO_REPLY or a
        failed event posts nothing; a failed message gets a line saying why, quoted too (the detail is the
        agent's own error text). A message whose turn completed with no text gets a line saying so and, with
        `turn`, what the turn did instead."""
        said = (text or "").strip()
        message = acp_host.is_message(headers)
        if status == "completed":
            if said == NO_REPLY:
                return "[no-send] %s chose not to post\n" % self.name
            if said:
                return quoted(said) + "\n"
            if not message:
                return "[no-send] %s handled the event without posting\n" % self.name
            why = no_answer_why(detail, turn)
            return quoted("%s finished without an answer%s" % (self.name, ": %s." % why if why else ".")) + "\n"
        if not message:
            return "[no-send] %s handled the event without posting" % self.name + quoted(
                " (%s: %s)" % (status, detail[:120])) + "\n"
        head = {"timeout": "%s did not finish this in time (%s)." % (self.name, detail),
                "gone": "%s stopped before answering (%s)." % (self.name, detail),
                "refused": "%s did not take this: %s." % (self.name, detail)}.get(
                    status, "%s could not answer this (%s: %s)." % (self.name, status, detail))
        return quoted(head + ("\n\nPartial reply:\n" + said if said else "")) + "\n"

    def handle(self, name):
        task_id = name[:-4]
        path = os.path.join(self.args.inbox, name)
        result_file = os.path.join(self.args.results, name)
        headers, message, operator_lines = parse_task(path)
        kind, turn_kind = turn_for(headers)
        if turn_kind is None:
            log("refused (non-owner) ->", task_id, headers["access_tier"])
            publish(result_file, NON_OWNER_REFUSED)
            return {"task": task_id, "status": "refused", "tier": headers["access_tier"], "policy": kind}
        owner_turn = turn_kind == OWNER_TURN
        event, _ = trigger_of(headers)
        log("wake ->", task_id, headers["access_tier"], event or "message")
        t0 = time.time()
        if owner_turn:
            # shell_web_on reads the sender's tier itself, so a collaborator on the owner's turn stays off.
            prompt = wake_prompt(task_id, headers, message, operator_lines, self.args.cwd, self.note,
                                 shell_web=shell_web_on(self.args, headers))
            status, text, detail, turn = self.run_turn(task_id, headers, prompt, True)
        else:
            with guest_turn(self.args, headers, message) as pair:
                prompt = wake_prompt(task_id, headers, message, operator_lines, None, self.note, readable=pair[1])
                status, text, detail, turn = self.run_turn(task_id, headers, prompt, False, pair)
        publish(result_file, self.answer_body(headers, status, text, detail, turn))
        return {"task": task_id, "status": status, "detail": detail[:200], "event_type": event,
                "tier": headers["access_tier"], "policy": kind, "isolated": not owner_turn, "turn_s": round(time.time() - t0, 3),
                "permissions": turn.decisions, "updates": dict(turn.updates), "answer_chars": len(text or "")}

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
    ap.add_argument("--acp-command", default=None, help="the ACP agent's command line (default: agy acp)")
    ap.add_argument("--cwd", default=None, help="the Session working directory (default: the current directory)")
    ap.add_argument("--session-store", default=None,
                    help="the sessions.json to resume from and record to (default: <work>/state/sessions.json)")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="no effect, kept so existing configs parse: an attested collaborator always gets the "
                         "owner's turn, any other non-owner an isolated read-only one (all on the owner's agy sign-in)")
    ap.add_argument("--allow-owner-shell-web", action="store_true",
                    help="let the owner's turns run shell commands and fetch or search the web (never a "
                         "collaborator's or a guest's; default: off)")
    ap.add_argument("--once", type=count_arg, default=0, help="exit after N tasks")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--poll", type=float, default=0.2)
    ap.add_argument("--turn-timeout", type=float, default=900)
    ap.add_argument("--rpc-timeout", type=float, default=60)
    ap.add_argument("--interrupt-grace", type=float, default=15)
    return ap


def _prepare(args):
    args.acp_argv = shlex.split(args.acp_command or DEFAULT_ACP_COMMAND)
    if not args.acp_argv:
        raise SystemExit("--acp-command is empty")
    args.cwd = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))
    args.child_env = dict(os.environ)


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
        owner = acquire_lock(args.work, "antigravity")
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
