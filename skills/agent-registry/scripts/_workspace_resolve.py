"""Shared workspace resolution for the agent-registry skill.

Both `registry-client.py` and `registry-service.py` need to locate the
Sutando workspace identically. Lifted into this module so the two scripts
can't drift apart.

Delegates to the canonical M0 helper at `scripts/sutando-config.sh`
(which wraps `src/sutando_config.py:resolve_workspace`). The helper reads
`sutando.config.local.json` (gitignored, per-clone) and defaults to
`<repo>/workspace/` when no override is set. Post-v0.8 / #1440,
`$SUTANDO_WORKSPACE` is no longer honored for workspace resolution; it
is still detected to fire a one-time deprecation warning and trigger
one-time auto-migration via per-source sentinels (PR #1478), but the
resolver itself ignores its value.

If you change resolution behavior, also see `src/workspace_default.py`
and `src/workspace_default.ts` — they implement the same contract for
other consumers.
"""

import os
import subprocess
import sys

# Bounded walk-up: from <repo>/skills/agent-registry/scripts/_workspace_resolve.py
# the repo root is 3 dirs up. Symlinks (e.g. ~/.claude/skills/agent-registry/
# pointing back into the repo) are followed by realpath() before walking.
# Probe 5 levels to absorb mild path variations without runaway scanning.
_WALK_LEVELS = 5


def _find_repo_root():
    """Walk up from this file (following symlinks via realpath) until we
    find a directory containing `scripts/sutando-config.sh`. Returns the
    repo root path or None.
    """
    cur = os.path.realpath(__file__)
    for _ in range(_WALK_LEVELS):
        cur = os.path.dirname(cur)
        if not cur or cur == "/":
            return None
        if os.path.isfile(os.path.join(cur, "scripts", "sutando-config.sh")):
            return cur
    return None


def resolve_workspace():
    """Locate the Sutando workspace dir via the canonical M0 helper."""
    repo_root = _find_repo_root()
    if repo_root is None:
        sys.stderr.write(
            "agent-registry: cannot find scripts/sutando-config.sh from "
            f"{os.path.realpath(__file__)} — using process cwd as a last resort.\n"
        )
        return os.path.abspath(os.getcwd())
    try:
        result = subprocess.run(
            ["bash", os.path.join(repo_root, "scripts", "sutando-config.sh"), "workspace"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            f"agent-registry: sutando-config.sh failed (exit {e.returncode}): "
            f"{e.stderr.strip() or 'no stderr'} — using <repo>/workspace as fallback.\n"
        )
        return os.path.join(repo_root, "workspace")
