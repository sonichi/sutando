"""One isolated local Claude Code turn for an attested collaborator: no owner session, no
connector, no history, nothing to read. The CLI's login (CLAUDE_CONFIG_DIR) is all it inherits.

Package level so both Claude adapters share it without importing one another. Stdlib only.
"""
import json
import os
import shutil
import subprocess
import tempfile

# CLAUDE_CONFIG_DIR selects which login the CLI uses; without it the CLI falls back to the user default.
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "CLAUDE_CONFIG_DIR")
# --safe-mode: no CLAUDE.md, hooks, skills, plugins or MCP from the cwd or config dir (login kept);
# --restricted, --strict-mcp-config and the three read tools: nothing runs, no owner connector loads.
TURN_ARGV = ("-p", "--safe-mode", "--restricted", "--strict-mcp-config", "--tools", "Read,Grep,Glob",
             "--output-format", "json")
# The turn has no standing instructions: this line is all it knows about where its text goes.
REMINDER = ("[sparrow collaborator task] One message from a collaborator of your owner follows; answer it "
            "on your owner's behalf. This turn is isolated: no connectors, no files, no memory of earlier "
            "messages. Write only the reply text; the bridge delivers it to the sender's room for you.")


def child_env(environ=None):
    env = os.environ if environ is None else environ
    return {k: env[k] for k in ENV_KEEP if env.get(k)}


class Turn:
    """kind: ok (text is the reply) | failed (detail says why; never retried)."""

    def __init__(self, kind, text="", detail=""):
        self.kind, self.text, self.detail = kind, text, detail


def _stdout(proc):
    out = proc.stdout
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")


def run_collaborator_turn(claude, text, env, timeout=120.0, run=subprocess.run, mkdtemp=tempfile.mkdtemp):
    """The text on stdin, in a fresh empty directory removed afterwards; the JSON `result` is the reply."""
    cwd = mkdtemp(prefix="collaborator-turn-")
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
    return Turn("ok", text=reply)
