"""One isolated local Claude Code turn for a restricted sender (task_io.turn_policy's restricted kind): no
owner session, no connector, no history, nothing to read. The CLI's login (CLAUDE_CONFIG_DIR) is all it inherits.

Package level so both Claude adapters share it without importing one another. Stdlib only.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from .result_markers import neutralize_markers, parse_markers
from .team_guardrail import SANDBOXED_DELEGATION_CODEX

# CLAUDE_CONFIG_DIR selects which login the CLI uses; without it the CLI falls back to the user default.
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "CLAUDE_CONFIG_DIR")
# --safe-mode: no CLAUDE.md, hooks, skills, plugins or MCP from the cwd or config dir (login kept);
# --no-session-persistence: the turn is never saved, so nothing can resume it; the rest: only reads.
TURN_ARGV = ("-p", "--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence",
             "--tools", "Read,Grep,Glob", "--output-format", "json")
# The turn has no standing instructions: this line is all it knows about where its text goes.
REMINDER = ("[sparrow restricted task] One message from a sender who is not your owner follows; answer it "
            "on your owner's behalf. This turn is isolated: no connectors, no files, no memory of earlier "
            "messages. Write only the reply text; the bridge delivers it to the sender's room for you.")
_BRACKET_RE = re.compile(r"\[(?=\S)")
# The one marker a reply may keep: posting nothing is always safe.
_SKIP_RE = re.compile(r"^\[no-send\]$", re.IGNORECASE)


def child_env(environ=None):
    env = os.environ if environ is None else environ
    return {k: env[k] for k in ENV_KEEP if env.get(k)}


class Turn:
    """kind: ok (text is the reply) | failed (detail says why; never retried)."""

    def __init__(self, kind, text="", detail=""):
        self.kind, self.text, self.detail = kind, text, detail


def _executes(body):
    return bool(parse_markers(body).actions or parse_markers(body, skip_after_channel=True).actions)


def inert_text(text):
    """`text` as a body the delivery parser executes nothing from: the parser's own quoted form first,
    then every bracket opened, each pass checked by the parser itself; None when something still acts."""
    body = text.strip()
    for quote in (neutralize_markers, lambda s: _BRACKET_RE.sub("[ ", s)):
        if not _executes(body):
            return body
        body = quote(body)
    return None if _executes(body) else body


def _stdout(proc):
    out = proc.stdout
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")


def run_collaborator_turn(claude, text, env, timeout=120.0, run=subprocess.run, mkdtemp=tempfile.mkdtemp,
                          allow_skip=False):
    """The text on stdin, in a fresh empty directory removed afterwards; the JSON `result`, made inert, is the reply.
    With `allow_skip`, a reply that is exactly `[no-send]` stays that marker (the sender gets no post)."""
    try:
        cwd = mkdtemp(prefix="collaborator-turn-")
    except OSError as e:
        return Turn("failed", detail=type(e).__name__)
    try:
        proc = run([claude] + list(TURN_ARGV), input=REMINDER + "\n" + text, env=env, cwd=cwd,
                   capture_output=True, text=True, timeout=timeout, shell=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        return Turn("failed", detail=type(e).__name__)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if proc.returncode != 0:
        return Turn("failed", detail="exit %d" % proc.returncode)
    try:
        data = json.loads(_stdout(proc))
    except ValueError:
        return Turn("failed", detail="no json")
    reply = data.get("result") if isinstance(data, dict) and not data.get("is_error") else None
    if not isinstance(reply, str) or not reply.strip():
        return Turn("failed", detail="no result")
    if allow_skip and _SKIP_RE.match(reply.strip()):
        return Turn("ok", text="[no-send]")
    body = inert_text(reply)
    if body is None:
        return Turn("failed", detail="markers")
    return Turn("ok", text=body)


# The gateway's tier block assumes a core that writes results/<task>.txt and runs `codex exec`; this
# turn does neither (its reply is the result, it is the sandbox), so isolated_lines restates those steps.
_RESULT = r"results/[^\s()`'\"]+\.txt"
NO_RESULT_FILE = "there is no results file to write"
TIER_REWRITES = tuple((re.compile(a), b) for a, b in (
    (r"^Write only the user-facing result to %s\.$" % _RESULT,
     "Your reply text is the user-facing result, and only that: it is what Sparrow posts (%s)." % NO_RESULT_FILE),
    (r"^Write only the sandboxed agent's safe user-facing answer to %s\.$" % _RESULT,
     "Your reply text is the safe user-facing answer, and only that: it is what Sparrow posts (%s)."
     % NO_RESULT_FILE),
    (r"write your response to %s \(delivered back to this (\w+)\)" % _RESULT,
     "make your response your reply text (Sparrow delivers it back to this \\1; %s)" % NO_RESULT_FILE),
    (r"write exactly \[no-send\] to %s" % _RESULT, "make your reply exactly [no-send]"),
    (r"Write that DM as your result to %s\." % _RESULT, "Make that DM your reply text."),
))
_RESULT_RE = re.compile(_RESULT)
TIER_BLOCK_HEADER = "===SUTANDO SYSTEM INSTRUCTIONS"
TIER_BLOCK_END = "===END SUTANDO SYSTEM INSTRUCTIONS"
ISOLATED_NOT_CODEX = (
    "There is no Codex here to delegate to, and none is needed: this turn already is the read-only sandbox "
    "(a one-shot Claude Code run in a fresh empty directory, read tools only, no MCP servers, plugins, hooks, "
    "connectors or memory), never your owner's session. Answer here directly. If the task needs something this "
    "turn cannot read, say so and decline: never describe an artifact you could not read.")


def isolated_lines(operator_lines):
    """The bridge-written lines with each Sutando tier block said as the isolated turn can follow it: the
    result steps become "your reply text", the Codex delegation paragraph says this turn is the sandbox, and
    any other line of a block naming a results file or `codex exec` is dropped. Every other line (the scope,
    the DO / DO NOT lists, the trust boundary, the hand-off to the owner), and every line outside a block,
    is kept verbatim."""
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
            if _RESULT_RE.search(line) or "codex exec" in line:
                continue
        out.append(line)
    return out
