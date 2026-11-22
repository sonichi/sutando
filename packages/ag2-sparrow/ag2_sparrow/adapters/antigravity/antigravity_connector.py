#!/usr/bin/env python3
"""Wake Antigravity CLI (`agy`) over ACP for each task file Sparrow queues.

Transport: the Agent Client Protocol over the stdio of `agy acp`, through acp_host's turn: per task, spawn
the command, `initialize`, resume the task's Session with `session/load` (else `session/new`), send one
`session/prompt` and wait for it to stop.

`<dir>/tasks/task-*.txt` in, `<dir>/results/<task>.txt` out. The turn's final message is the answer: no
reply command is granted (no shell command is), and the agent's own text is posted
with every delivery marker neutralised. A final message of exactly NO_REPLY posts nothing. A message whose
turn failed gets a line saying so; an event that failed posts nothing. The gateway's SKILL INSTRUCTIONS
prelude (run a room-read command, write results/<task>.txt) is for a core with a shell: its steps are not
passed on (`session_lines`). A turn that ends with no text says what it did instead (`no_answer_why`).

Sessions, the permission policy and the framing are acp_host's: the owner may read or edit under the working
directory, no command is ever granted, and only the owner is served unless --allow-non-owner, which admits
attested collaborators and other non-owners alike (task_io.admission decides the tier). A non-owner turn runs
isolated: a fresh temporary directory per task holds its working directory and its own agy home
(`--gemini-dir`, with deny-all permission rules and no global context), its Session is new and never stored,
and it is refused unless agy offers a read-only (plan) session mode and accepts it. Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ... import acp_host, held_start, mcp_auth, result_labels, result_publish
from ...acp_host import MESSAGE_EVENT, SESSIONS_NAME, TASK_NAME_RE, SessionStore, quoted, trigger_of  # noqa: F401
from ...acp_client import AcpClient as _SharedClient
from ...relay_loop import count_arg
from ...task_io import (NON_OWNER_NOTE, NON_OWNER_REFUSED, LockRefused, acquire_lock, admission, exit_code,
                        exit_on_sigterm, holder_consumer, load_done, make_dirs, parse_task, record_done,
                        release_lock, root_path, stop_requested)

NAME = "Antigravity"
DEFAULT_ACP_COMMAND = "agy acp"
NO_REPLY = "NO_REPLY"
HELD_WAIT_S = held_start.WAIT_S
RETRY_S = 60.0
STDERR_NAME = "agy-acp.stderr"
NOT_SIGNED_IN = ("Antigravity is not signed in on this computer (%s): run `agy` once in a terminal and sign in, "
                 "or run `agy acp --login`")


READ_ONLY_MODES = frozenset({"plan", "read-only", "readonly", "read_only"})
GUEST_PREFIX = "ag2-sparrow-antigravity-guest-"
GUEST_REFUSED = "it answers non-owners only in a read-only session mode, and %s"
# agy evaluates Deny > Ask > Allow, and deny rules apply in every mode.
GUEST_SETTINGS = {"agentMode": "plan", "permissions": {"allow": [], "ask": [], "deny": [
    "read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "download_url(*)", "command(*)", "mcp(*)"]}}
# Who gets the AG2 Space MCP server as this agent (owners' rule, 2026-11-21): the owner and Team, an attested
# collaborator (`task_io.admission`, as #6645 serves them). Anyone else gets none and `mcp(*)` stays denied.
SSPACE_ADMISSIONS = frozenset({"owner", "collaborator"})


def sspace_tools_for(headers):
    return admission(headers, True) in SSPACE_ADMISSIONS


def isolated_settings(sspace):
    """A non-owner turn's agy settings: GUEST_SETTINGS, without the `mcp(*)` deny when the turn has the AG2
    Space tools (agy applies deny rules before asking, so the policy would never see the request)."""
    if not sspace:
        return GUEST_SETTINGS
    deny = [r for r in GUEST_SETTINGS["permissions"]["deny"] if r != "mcp(*)"]
    return dict(GUEST_SETTINGS, permissions=dict(GUEST_SETTINGS["permissions"], deny=deny))


MINT_TIMEOUT_S = 30.0
# What the token exchange's child keeps of this process's (already allowlisted) environment.
MINT_ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "TMPDIR")


def mint_argv(*command):
    """`ag2-sparrow-mcp <command...>` (default `acp`) from this very package (a source tree or the zipapp),
    isolated (-I) so neither the child's directory nor PYTHONPATH can put another module first."""
    root = str(Path(mcp_auth.__file__).resolve().parents[1])
    code = ("import sys; sys.path.insert(0, %r); from ag2_sparrow.mcp_auth import main; "
            "raise SystemExit(main(%r))" % (root, list(command or ("acp",))))
    return [sys.executable, "-I", "-c", code]


def mint_env(token_env_file, agent_id, environ=None):
    """(the exchange's environment, '') or ({}, why): the agent's own token file as its bridge reads it
    (`connect_bridge.credential_env`) and MINT_ENV_KEEP, nothing else."""
    from ... import connect_bridge
    cred, problem = connect_bridge.credential_env(token_env_file, agent_id)
    if problem:
        return {}, problem
    source = os.environ if environ is None else environ
    env = {k: source[k] for k in MINT_ENV_KEEP if k in source}
    env.update(cred)
    return env, ""


def _exchange(command, token_env_file, agent_id, run, extra=()):
    """(the JSON object `ag2-sparrow-mcp <command>` printed, '') or (None, why), run as `mint_env` says."""
    env, problem = mint_env(token_env_file, agent_id)
    if problem:
        return None, problem
    try:
        done = run(mint_argv(command, *extra), env=env, cwd=os.path.dirname(token_env_file) or "/", capture_output=True,
                   text=True, timeout=MINT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "the token exchange did not run (%s)" % type(e).__name__
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines()
        return None, (lines[-1][:200] if lines else "the token exchange exited %s" % done.returncode)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = None
    return (out, "") if isinstance(out, dict) else (None, "the token exchange printed no JSON object")


def sspace_entry(token_env_file, agent_id, run=subprocess.run):
    """(the ACP HTTP `McpServer` for AG2 Space as this agent, '') or (None, why). The relay token stays in the
    child; only the short-lived access token it mints comes back, in the entry's Authorization header."""
    entry, why = _exchange("acp", token_env_file, agent_id, run)
    if entry is None:
        return None, why
    try:
        mcp_auth._safe_url("mcp_url", entry.get("url"))
        ok = (entry.get("type") == "http" and entry.get("name") == mcp_auth.SERVER_NAME
              and isinstance(entry.get("headers"), list)
              and all(isinstance(h, dict) and set(h) == {"name", "value"} and all(isinstance(v, str) for v in h.values())
                      for h in entry["headers"]))
    except (ValueError, AttributeError, mcp_auth.McpAuthError):
        ok = False
    return (entry, "") if ok else (None, "the token exchange printed no MCP server entry")


DESCRIPTOR_NAME = "ag2-mcp-descriptor.json"
PROXY_LOG_NAME = "ag2-mcp-proxy.log"


SHIM_NAME = "ag2-mcp-shim"


def shim_args(argv):
    """The arguments that run `argv` (the proxy) under `mcp_name_shim`, from this very package, isolated: agy
    drops every tool whose name has a dot, so the shim offers `room_action_execute` for `room.action.execute`."""
    root = str(Path(mcp_auth.__file__).resolve().parents[1])
    code = ("import sys; sys.path.insert(0, %r); from ag2_sparrow.mcp_name_shim import main; "
            "raise SystemExit(main(sys.argv[1:]))" % root)
    return ["-I", "-c", code, "--"] + list(argv)


def write_launcher(state):
    """`<state>/ag2-mcp-shim` (0700): a plain executable that runs the shim, so agy's stdio entry is a file and
    its args are only the proxy's command line (agy started no process for a `python -I -c <code>` entry)."""
    path = os.path.join(state, SHIM_NAME)
    text = "#!/bin/sh\nexec %s \"$@\"\n" % " ".join(shlex.quote(a) for a in [sys.executable] + shim_args([]))
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return path


def _descriptor_file(desc, token_env_file):
    """The file a descriptor must name: the .env itself, or for `token_format: "raw"` (no env_key) the raw
    token file that seat .env points at; None for any other shape."""
    if desc.get("token_format") == "raw" and "env_key" not in desc:
        return mcp_auth.seat_token_file(token_env_file) or None
    if "token_format" not in desc and isinstance(desc.get("env_key"), str):
        return os.path.realpath(token_env_file)
    return None


def stdio_entry(proxy_command, token_env_file, agent_id, state_dir, run=subprocess.run):
    """(the ACP stdio `McpServer` running the desktop's `ag2-mcp-proxy` as this agent, '') or (None, why). The
    proxy gets only the path of a non-secret descriptor (written 0600 in `state_dir`) naming the agent's own
    token file, as Claude Code's `ag2-space` stdio entry does; the relay token never enters agy's environment."""
    argv = shlex.split(proxy_command or "")
    if not argv:
        return None, "no --mcp-proxy-command"
    desc, why = _exchange("descriptor", token_env_file, agent_id, run, ("--env-file", token_env_file))
    if desc is None:
        return None, why
    try:
        for k in ("mint_url", "mcp_url"):
            mcp_auth._safe_url(k, desc.get(k))
        ok = desc.get("version") == 1 and isinstance(desc.get("env_file"), str) \
            and os.path.realpath(desc["env_file"]) == _descriptor_file(desc, token_env_file)
    except mcp_auth.McpAuthError:
        ok = False
    if not ok:
        return None, "the token exchange printed no proxy descriptor"
    if isinstance(agent_id, str) and agent_id.startswith("@") and ":" in agent_id:
        desc["agent_id"] = agent_id   # who a re-mint must turn out to be
    state = os.path.abspath(state_dir)
    path = os.path.join(state, DESCRIPTOR_NAME)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(desc, f)
    os.replace(tmp, path)
    return {"name": mcp_auth.SERVER_NAME, "command": write_launcher(state), "args": list(argv),
            "env": [{"name": "AG2_MCP_DESCRIPTOR", "value": path},
                    {"name": "AG2_MCP_LOG", "value": os.path.join(state, PROXY_LOG_NAME)}]}, ""


def declares_http_mcp(init):
    caps = (init.get("agentCapabilities") or {}) if isinstance(init, dict) else {}
    mcp = caps.get("mcpCapabilities") if isinstance(caps, dict) else None
    return isinstance(mcp, dict) and mcp.get("http") is True


class AcpClient(_SharedClient):
    client_info = {"name": "ag2-sparrow-antigravity", "version": "0.1.0"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)




def publish(result_file, body):
    """Labels first, so the drain that reads the result finds them, then the body, staged and renamed."""
    labels, _notes = result_publish.result_labels_for(body)
    result_labels.write(Path(result_file), labels, body)
    result_publish.publish_text(result_file, body)


@contextlib.contextmanager
def guest_turn(args, sspace=False):
    """An `acp_host.Isolated` for one non-owner task: a new 0700 temporary directory holding the Session's
    working directory and agy's home (`--gemini-dir`, `isolated_settings` only), removed after the turn."""
    base = os.path.realpath(tempfile.mkdtemp(prefix=GUEST_PREFIX))
    try:
        home, cwd = os.path.join(base, "agy"), os.path.join(base, "cwd")
        os.makedirs(os.path.join(home, "antigravity-cli"), mode=0o700)
        os.mkdir(cwd, 0o700)
        settings = os.path.join(home, "antigravity-cli", "settings.json")
        fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(isolated_settings(sspace), f)
        yield acp_host.Isolated(argv=list(args.acp_argv) + ["--gemini-dir", home], cwd=cwd,
                                env=dict(args.child_env),
                                choose_mode=lambda modes: acp_host.pick_mode(modes, READ_ONLY_MODES),
                                refused=GUEST_REFUSED)
    finally:
        shutil.rmtree(base, ignore_errors=True)


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


def wake_prompt(task_id, headers, message, operator_lines, cwd, note=NON_OWNER_NOTE):
    event, _ = trigger_of(headers)
    out = acp_host.task_framing(task_id, headers, message, session_lines(operator_lines),
                                "post in your final message", note)
    out.append("Answer through Sparrow, exactly once for this task: your final message in this turn is what Sparrow "
               "posts%s. Write it as the post itself, with no preamble about tools or steps. To post nothing (an "
               "acknowledgement, thanks or FYI that needs no answer, or an event whose body asks for no post), make "
               "your final message exactly %s and nothing else."
               % (" as the reply" if event in (None, MESSAGE_EVENT) else "", NO_REPLY))
    if sspace_tools_for(headers):
        out.append("When this session has the %s MCP tools, they act in AG2 Space as you, this agent: use "
                   "room_action_execute (the server's room.action.execute; find Actions with room_actions_search and "
                   "room_actions_describe) to create or edit Room Commons pages and for other room actions, and put "
                   "the page link in your reply." % mcp_auth.SERVER_NAME)
    out.append("No shell command is granted in this session. %s"
               % ("You may read and edit files under %s." % cwd if headers.get("access_tier") == "owner"
                  else "You may not read or change files."))
    return "\n\n".join(out)


class Connector:
    def __init__(self, args, client_factory=AcpClient, note=None, mcp_entry=sspace_entry, proxy_entry=stdio_entry):
        self.args = args
        self.client_factory = client_factory
        self.mcp_entry = mcp_entry
        self.proxy_entry = proxy_entry
        self.note = note if note is not None else NON_OWNER_NOTE
        self.store = SessionStore(args.session_store)
        self.name = NAME
        self.retry_at = {}

    def sspace_servers(self, init):
        """An owner's or team turn's MCP servers: AG2 Space as this agent, over HTTP when agy declares it, else
        over stdio through --mcp-proxy-command; none without the agent's token file, with the reason logged."""
        token_file = getattr(self.args, "token_env_file", None)
        agent_id = getattr(self.args, "agent_id", None) or ""
        if not token_file:
            log("ag2-space MCP tools not offered: no --token-env-file (the agent's own token file)")
            return []
        if declares_http_mcp(init):
            entry, why = self.mcp_entry(token_file, agent_id)
        elif getattr(self.args, "mcp_proxy_command", None):
            entry, why = self.proxy_entry(self.args.mcp_proxy_command, token_file, agent_id, self.args.state)
        else:
            log("ag2-space MCP tools not offered: agy declares no HTTP MCP transport and no --mcp-proxy-command "
                "is set")
            return []
        if entry is None:
            log("ag2-space MCP tools not offered:", why)
            return []
        return [entry]

    def run_turn(self, task_id, headers, prompt, owner):
        """(status, text, detail, turn): status is completed | cancelled | timeout | gone | failed | refused.
        The owner's turn resumes the room's Session in the working directory; any other runs `guest_turn`."""
        if owner:
            cwd = self.args.cwd
            return acp_host.run_prompt(self.client_factory, self.args, headers, task_id, prompt,
                                       lambda params: acp_host.decide_permission(params, True, cwd,
                                                                                 **self.mcp_grant(headers)),
                                       self.store, STDERR_NAME, NOT_SIGNED_IN, log,
                                       mcp_servers=self.mcp_servers_for(headers))
        sspace = sspace_tools_for(headers) and bool(getattr(self.args, "token_env_file", None))
        with guest_turn(self.args, sspace) as isolated:
            return acp_host.run_prompt(self.client_factory, self.args, headers, task_id, prompt,
                                       lambda params: acp_host.decide_permission(params, False, isolated.cwd,
                                                                                 **self.mcp_grant(headers)),
                                       self.store, STDERR_NAME, NOT_SIGNED_IN, log, isolated=isolated,
                                       mcp_servers=self.mcp_servers_for(headers))

    @staticmethod
    def mcp_grant(headers):
        """The policy's MCP grant for this sender: the AG2 Space tools for the owner and Team, else none."""
        return {"mcp_tool_ok": mcp_auth.owner_tool_ok} if sspace_tools_for(headers) else {}

    def mcp_servers_for(self, headers):
        return self.sspace_servers if sspace_tools_for(headers) else None

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
        tier = admission(headers, self.args.allow_non_owner)
        # A non-owner turn runs on the owner's agy sign-in, so collaborators need the opt-in too.
        if tier is None or (tier != "owner" and not self.args.allow_non_owner):
            log("refused (non-owner) ->", task_id, headers["access_tier"])
            publish(result_file, NON_OWNER_REFUSED)
            return {"task": task_id, "status": "refused", "tier": headers["access_tier"]}
        prompt = wake_prompt(task_id, headers, message, operator_lines, self.args.cwd, self.note)
        event, _ = trigger_of(headers)
        log("wake ->", task_id, headers["access_tier"], event or "message")
        t0 = time.time()
        status, text, detail, turn = self.run_turn(task_id, headers, prompt, tier == "owner")
        publish(result_file, self.answer_body(headers, status, text, detail, turn))
        return {"task": task_id, "status": status, "detail": detail[:200], "event_type": event,
                "tier": headers["access_tier"], "isolated": tier != "owner", "turn_s": round(time.time() - t0, 3),
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
    ap.add_argument("--token-env-file", default=None,
                    help="the agent's own token .env (its bridge's): owner and team turns get the ag2-space MCP "
                         "tools as this agent, minted from it per turn (default: no MCP tools)")
    ap.add_argument("--agent-id", default=None, help="the agent's Matrix ID, to check a seat .env against")
    ap.add_argument("--mcp-proxy-command", default=None,
                    help="the stdio ag2-mcp-proxy command line (node and the desktop app's ag2-mcp-proxy.mjs), "
                         "for an agy that declares no HTTP MCP (default: none)")
    ap.add_argument("--session-store", default=None,
                    help="the sessions.json to resume from and record to (default: <work>/state/sessions.json)")
    ap.add_argument("--allow-non-owner", action="store_true",
                    help="serve non-owner senders, attested collaborators included, on the owner's agy sign-in "
                         "(no file operations granted; default: the owner only)")
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
