"""Shared workspace resolution for the agent-registry skill.

Both `registry-client.py` and `registry-service.py` need to locate the
Sutando workspace identically. Lifted into this module so the two scripts
can't drift apart.

Resolution order:
  1. `$SUTANDO_WORKSPACE` from the process env.
  2. Best-effort: `SUTANDO_WORKSPACE=` line in the repo's `.env` file, if it
     can be located by walking up from this file's resolved path.
  3. `~/.sutando/workspace` (the canonical default).

Why step 2 exists: the SessionStart hook (which calls registry-client.py)
fires in a fresh shell spawned by Claude Code that does NOT inherit env
vars from `bash src/startup.sh`. On hosts whose `.env` carries a
`SUTANDO_WORKSPACE=` override but whose `.zshenv` does NOT export the
same var, step 1 returns None and step 3 silently falls back to the
default — so the registry writes `state/agent-registry.json` to the
wrong dir while readers (Electron overlay, dashboard) look in the right
one. Step 2 closes that gap by reading the file directly. A loud stderr
note fires when step 2 actually matches, so operators can see when the
env var is missing in their bootstrap chain.

If you change resolution behavior, also see `src/workspace_default.py`
and `src/workspace_default.ts` — they implement the same contract for
other consumers.
"""

import os
import sys

_GREP_KEY = "SUTANDO_WORKSPACE"
_DEFAULT = "~/.sutando/workspace"
# Bounded walk-up: from <repo>/skills/agent-registry/scripts/_workspace_resolve.py
# the repo root is 3 dirs up. Symlinks (e.g. ~/.claude/skills/agent-registry/
# pointing back into the repo) are followed by realpath() before walking.
# Probe 5 levels to absorb mild path variations without runaway scanning.
_WALK_LEVELS = 5


def _grep_env_file(key):
    """Best-effort lookup of `key=VALUE` in the repo's .env file.

    Returns the (tilde-expanded) value as a string, or None if no .env file
    is found within the bounded walk-up or no matching line is present.
    Quotes around the value are stripped. Never raises — failure is silent
    so the caller's own fallback applies.
    """
    try:
        cur = os.path.realpath(__file__)
        for _ in range(_WALK_LEVELS):
            cur = os.path.dirname(cur)
            if not cur or cur == "/":
                return None
            env_path = os.path.join(cur, ".env")
            if os.path.isfile(env_path):
                with open(env_path) as fh:
                    for line in fh:
                        s = line.strip()
                        if s.startswith(key + "="):
                            val = s.split("=", 1)[1].strip()
                            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                                val = val[1:-1]
                            return os.path.expanduser(val) if val else None
                # First .env on the walk wins, even if it doesn't define `key`.
                return None
    except Exception:
        pass
    return None


def resolve_workspace():
    """Locate the Sutando workspace dir. See module docstring for order."""
    env = os.environ.get(_GREP_KEY)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    from_env_file = _grep_env_file(_GREP_KEY)
    if from_env_file:
        sys.stderr.write(
            f"agent-registry: {_GREP_KEY} not in process env; falling back to .env "
            f"value ({from_env_file}). Source .env or export {_GREP_KEY} in the "
            "process that invokes the registry to silence this notice.\n"
        )
        return os.path.abspath(from_env_file)
    return os.path.expanduser(_DEFAULT)
