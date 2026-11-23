"""One isolated local Claude Code turn for a restricted sender (task_io.turn_policy's restricted kind): no
owner session, no connector, no history; the only files it can read are the ones the sender attached to this
task (stage_own_attachments). The CLI's login (CLAUDE_CONFIG_DIR) is all it inherits.

Package level so both Claude adapters share it without importing one another. Stdlib only.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile

from .local_task_protocol import parse_attachments
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
            "on your owner's behalf. This turn is isolated: no connectors, only the files attached to this "
            "message (copied into your working directory), no memory of earlier messages. Write only the reply text; the bridge delivers it to the sender's room for you.")
_BRACKET_RE = re.compile(r"\[(?=\S)")
# The bridge's own cap on a fetched attachment (remote_gateway_bridge.MAX_MEDIA_BYTES), read the same way.
MAX_MEDIA_BYTES = int(os.environ.get("REMOTE_MEDIA_MAX_BYTES") or str(25 * 1024 * 1024))
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
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


def _under(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _open_under(root_fd, parts):
    """A descriptor for the file at `parts` below the directory `root_fd`, opened one component at a
    time and never through a symlink, so the file read is under that directory whatever is swapped
    meanwhile; None when any component is missing, a link or (but the last) not a directory."""
    nofollow, fd = getattr(os, "O_NOFOLLOW", 0), os.dup(root_fd)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | nofollow | (os.O_NONBLOCK if last else getattr(os, "O_DIRECTORY", 0))
            try:
                nxt = os.open(part, flags | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            except OSError:
                return None
            os.close(fd)
            fd = nxt
        out, fd = fd, -1
        return out
    finally:
        if fd >= 0:
            os.close(fd)


def stage_own_attachments(headers, dest_dir, media_root, max_bytes=None):
    """Copy this task's own attachments into dest_dir, the isolated turn's empty cwd; return [(locator, copy)].

    `headers` are the task's attested headers (task_io.parse_task); only the files its `attachments:` header
    lists are candidates, so another task's file in the same folder is never one. A locator is copied only
    when its directory resolves to a place below the real `media_root` (the bridge's remote-media folder)
    and it is a regular file, not a link, of at most `max_bytes` (MAX_MEDIA_BYTES). The file is opened
    from a descriptor on that folder, one path component at a time and through no symlink
    (`_open_under`), so what is read is under the folder even if a component is replaced meanwhile. Any other locator is skipped, never raised. The folder itself
    is never handed to the turn: only these copies are."""
    limit = MAX_MEDIA_BYTES if max_bytes is None else max_bytes
    root = os.path.realpath(media_root) if media_root else ""
    try:
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)) if root else -1
    except OSError:
        root_fd = -1
    if root_fd < 0:
        return []
    staged, seen = [], set()
    try:
        for ref in parse_attachments(headers):
            loc = ref.locator
            if not os.path.isabs(loc) or loc in seen:
                continue
            # Only which path below the folder to try: its directory resolved, its last name as written
            # (so a file that is a link stays one). The open below is what keeps the read in the folder.
            where = os.path.join(os.path.realpath(os.path.dirname(loc)), os.path.basename(loc))
            parts = [p for p in os.path.relpath(where, root).split(os.sep) if p] if _under(where, root) else []
            if not parts or any(p in (os.curdir, os.pardir) for p in parts):
                continue
            fd = _open_under(root_fd, parts)
            if fd is None:
                continue        # missing, unreadable, or reached through a symlink
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
                    continue
                with os.fdopen(os.dup(fd), "rb") as src:
                    data = src.read(limit + 1)
            except OSError:
                continue
            finally:
                os.close(fd)
            if len(data) > limit:
                continue
            name = "%d-%s" % (len(staged) + 1, _SAFE_NAME_RE.sub("_", os.path.basename(loc)) or "attachment")
            copy = os.path.join(dest_dir, name)
            with open(copy, "xb") as f:
                f.write(data)
            seen.add(loc)
            staged.append((loc, copy))
    finally:
        os.close(root_fd)
    return staged


def with_staged_paths(text, staged):
    """`text` with each staged locator replaced by its copy's path, so the turn reads the copy."""
    for loc, copy in staged:
        text = text.replace(loc, copy)
    return text


def run_collaborator_turn(claude, text, env, timeout=120.0, run=subprocess.run, mkdtemp=tempfile.mkdtemp,
                          allow_skip=False, prepare=None):
    """The text on stdin, in a fresh empty directory removed afterwards; the JSON `result`, made inert, is the reply.
    With `allow_skip`, a reply that is exactly `[no-send]` stays that marker (the sender gets no post).
    `prepare(cwd, text) -> text` runs in that directory first (stage_own_attachments + with_staged_paths)."""
    try:
        cwd = mkdtemp(prefix="collaborator-turn-")
    except OSError as e:
        return Turn("failed", detail=type(e).__name__)
    try:
        if prepare is not None:
            try:
                text = prepare(cwd, text)
            except OSError as e:
                return Turn("failed", detail="attachments: %s" % type(e).__name__)
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
