#!/usr/bin/env python3
"""PostToolUse hook: append a one-line usage record when a skill is invoked.

Registered for the Skill tool only (matcher "Skill" in settings.json). Reads
the hook payload from stdin, appends {"slug", "ts"} to
<workspace>/state/skill-usage-log.jsonl, and always exits 0 — usage logging
must never block or fail a skill invocation. The log is drained by
scripts/report-usage.py (batched POST /api/skills/usage — AU#93).
"""

import json
import os
import sys
import time
from pathlib import Path


def _skill_dir() -> Path:
    # realpath() for the same reason as repo_root(): this file is symlinked into
    # the core's skills directory, so __file__ alone points at the link.
    return Path(os.path.realpath(__file__)).parents[1]


# The claim lock MUST be the same implementation the reporter uses — a drifted
# copy looks synchronised while it is not.
#
# FAIL CLOSED if that import breaks. The first version yielded True here, on the
# reasoning that the hook should "keep working exactly as before" — but "as
# before" IS the pre-rename-fd data-loss race this PR exists to close, so the
# degraded path silently reopened the bug on any partial install. Measured
# (#2180 review): break only `usage_lock.py` in place and the hook returns 0
# AND writes `skill-usage-log.jsonl` with no lock held.
#
# Yielding False routes into the branch that already drops the record and
# returns 0, so the hook's never-block-a-tool-call contract is unchanged; what
# changes is that an unsynchronised write is no longer the fallback. Losing
# telemetry beats corrupting it.
sys.path.insert(0, str(_skill_dir()))
try:
    from usage_lock import claim_lock as _claim_lock  # type: ignore
except Exception:  # pragma: no cover - degraded path
    import contextlib as _ctx

    @_ctx.contextmanager
    def _claim_lock(_log, *_a, **_kw):
        yield False


def repo_root() -> Path:
    # This file lives at <repo>/skills/skill-usage-report/hooks/log-usage.py.
    # realpath() first: skills/install.sh symlinks this skill into the core's
    # configured skills directory, so __file__ is a symlink and parents[3]
    # would otherwise resolve under that directory instead of the repo.
    return Path(os.path.realpath(__file__)).parents[3]


def workspace() -> "Path | None":
    """The canonical workspace, or None when the resolver cannot answer.

    Returns None rather than inventing `root / "workspace"`. The old fallback
    defeated configured workspace resolution: a malformed config or a resolver
    regression silently created a SECOND telemetry store inside the checkout,
    which then diverged from the real one. Repro from the #2180 review — set
    `sutando.config.local.json` to `{"workspace": {"path": 42}}`, and
    resolve_workspace() raises TypeError while the hook still wrote
    `<worktree>/workspace/state/skill-usage-log.jsonl`.

    The workspace contract is explicit: use the shared helper, do not reinvent
    its fallback. `resolve_workspace()` ALREADY has the correct default
    (`<repo>/workspace/` when nothing is configured), so a local fallback can
    only ever disagree with it — it adds no capability, only divergence.

    Fail-open is preserved by the caller writing nothing. Losing one telemetry
    line is the right trade against writing it somewhere no reader looks.
    """
    root = repo_root()
    sys.path.insert(0, str(root / "src"))
    try:
        from workspace_default import resolve_workspace  # type: ignore

        return Path(resolve_workspace())
    except Exception:
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    # `json.load` succeeding does NOT mean we got an object: `123`, `"x"` and
    # `[]` are all valid JSON, and .get() on them raises. The try above only
    # guards the PARSE, so an unchecked payload here exits 1 and breaks the
    # always-exit-0 contract — which, on a PostToolUse hook, can block a Skill
    # invocation on the host (#2180 review).
    if not isinstance(payload, dict):
        return 0
    if payload.get("tool_name") != "Skill":
        return 0
    # Same trap one level down: `tool_input` may be a non-object. `or {}` only
    # rescues falsy values, so a truthy non-dict (e.g. the string "oops") sails
    # through and .get() raises.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    slug = tool_input.get("skill")
    if not slug or not isinstance(slug, str):
        return 0
    # Directory-scoped ("apps/web:deploy") and plugin ("plugin:skill") forms
    # report the bare skill name — AU skills are keyed by plain slug.
    slug = slug.split(":")[-1].strip()
    if not slug:
        return 0
    ws = workspace()
    if ws is None:
        # Resolver could not answer. Write NOTHING — the hook stays fail-open
        # (exit 0, never blocks the tool call), but it does not fabricate a
        # workspace to write into.
        return 0
    try:
        log = ws / "state" / "skill-usage-log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        # Hold the shared claim lock across BOTH the open and the write.
        #
        # Without it, this hook can open the active log, the reporter can then
        # rename it to .reporting, and this fd — still pointing at the renamed
        # inode — can write AFTER the reporter has read to EOF but BEFORE it
        # unlinks. The record is then neither posted nor folded back: the unlink
        # destroys it, silently violating the "events arriving during a report
        # are never lost" contract (#2180 review, reproduced).
        #
        # Holding the lock across open+write makes that interleaving impossible:
        # the reporter takes the same lock around its rename, so either this
        # append completes first, or it starts afterwards and opens the FRESH
        # active log. The reporter never holds this lock across the network POST,
        # so contention here is a rename, not a round trip.
        with _claim_lock(log) as locked:
            if not locked:
                # Could not acquire quickly. Fail OPEN, and drop the record: this
                # is a PostToolUse hook whose first duty is never to block a tool
                # call. Losing one usage datapoint is strictly better than adding
                # latency to a skill invocation — and better than writing anyway,
                # which is precisely the unsynchronised write this lock exists to
                # prevent.
                return 0
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"slug": slug, "ts": int(time.time())}) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
