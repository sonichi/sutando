#!/usr/bin/env python3
"""Deprecated forwarder. The implementation moved to the pr-triage skill.

Kept for one release because a registered cron is a PROMPT SNAPSHOT: a host
whose job was registered against this path keeps invoking it until someone
re-runs /schedule-crons, and no repository change can reach that snapshot.
Holds no logic, so it cannot drift from the skill copy the way the vendored
duplicate did.
"""
from __future__ import annotations

import os
import pathlib
import sys

_REL = pathlib.Path("skills/pr-triage/scripts/pr_flag.py")
_MIGRATE = (
    "Re-point this host, then remove the shim:\n"
    "  bash skills/install.sh            # links the pr-triage skill\n"
    "  /schedule-crons                   # re-registers the job from crons.json\n"
    "The registered prompt must invoke "
    "\"$CLAUDE_CONFIG_DIR/skills/pr-triage/scripts/pr_flag.py\"."
)


def _target() -> "pathlib.Path | None":
    """The skill's copy, from the config dir first and the checkout as fallback."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    seen = []
    if cfg:
        seen.append(pathlib.Path(cfg) / "skills/pr-triage/scripts/pr_flag.py")
    # Resolves the REPO root to find the skill checkout, never the workspace.
    seen.append(pathlib.Path(__file__).resolve().parent.parent / _REL)  # lint-workspace-resolution: allow-repo-root
    for p in seen:
        if p.is_file():
            return p
    _target.searched = seen
    return None


def main() -> int:
    tgt = _target()
    if tgt is None:
        searched = "\n".join(f"  {p}" for p in getattr(_target, "searched", []))
        sys.stderr.write(
            "pr_flag.py: this path is a deprecated shim and the pr-triage skill "
            f"is not installed.\nLooked in:\n{searched}\n{_MIGRATE}\n")
        # Loud and non-zero: a silent success here is the digest going missing.
        return 2
    sys.stderr.write(
        f"pr_flag.py: DEPRECATED path; forwarding to {tgt}. {_MIGRATE}\n")
    os.execv(sys.executable, [sys.executable, str(tgt), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
