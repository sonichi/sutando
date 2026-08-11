#!/usr/bin/env python3
"""apply.py — land a confirmed resolution in the LIVE engine checkout.

Run ONLY after the owner explicitly confirmed the proposal. Takes the desktop
updater's own mkdir lock, re-asserts the checkout is exactly where the pending
state recorded it, then fast-forwards to the proposed merge — an ff can only
move HEAD to a descendant, and the sparse workspace guard is re-asserted first,
so the live workspace can never be touched.

Exit codes: 0 applied · 3 lock busy · 4 checkout moved/dirty · 1 other error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    acquire_lock, commit_exists, die, emit, ensure_workspace_guard, git_out,
    is_ancestor, is_git_checkout, load_pending, log, read_meta, release_lock,
    run_git, tree_dirty,
)


def find_scratch(engine: Path, merged_sha: str) -> Optional[Path]:
    """Locate the prepare.py worktree that carries the proposed merge."""
    blocks = run_git(engine, "worktree", "list", "--porcelain").stdout.split("\n\n")
    for block in blocks[1:]:  # first block is the live checkout itself
        lines = dict(l.split(" ", 1) for l in block.splitlines() if " " in l)
        path = lines.get("worktree")
        if not path:
            continue
        wt = Path(path)
        meta = read_meta(wt) if wt.is_dir() and is_git_checkout(wt) else None
        if meta and run_git(wt, "rev-parse", "HEAD", check=False).stdout.strip() == merged_sha:
            return wt
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pending", required=True, type=Path)
    ap.add_argument("--engine", required=True, type=Path)
    ap.add_argument("--merged-sha", required=True)
    ap.add_argument("--scratch", type=Path, default=None)
    args = ap.parse_args()

    pending = load_pending(args.pending)
    engine = args.engine
    if not engine.is_dir() or not is_git_checkout(engine):
        die(f"engine checkout not found or not a git checkout: {engine}", reason="engine-invalid")

    lock_dir = acquire_lock(args.pending.resolve().parent, busy_exit=3)
    try:
        branch = pending["snapshot_branch"]
        head = git_out(engine, "rev-parse", "HEAD")
        tip_proc = run_git(engine, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False)
        snapshot_tip = tip_proc.stdout.strip() if tip_proc.returncode == 0 else ""
        if head != snapshot_tip or head != pending["old_sha"]:
            die("checkout moved since the conflict was recorded (HEAD %s, snapshot tip %s, pending old_sha %s)"
                " — NOT applying; re-run prepare.py against fresh state"
                % (head[:12], (snapshot_tip or "missing")[:12], pending["old_sha"][:12]),
                reason="checkout-moved", exit_code=4)
        if tree_dirty(engine):
            die("checkout has local changes since the conflict was recorded — NOT applying",
                reason="checkout-moved", exit_code=4)

        merged_sha = args.merged_sha
        if not commit_exists(engine, merged_sha):
            die(f"proposed merge commit {merged_sha[:12]} not found in the engine repo", reason="merged-sha-missing")
        merged_sha = git_out(engine, "rev-parse", f"{merged_sha}^{{commit}}")
        if not is_ancestor(engine, head, merged_sha):
            die(f"{merged_sha[:12]} is not a descendant of HEAD {head[:12]} — refusing non-ff apply",
                reason="not-fast-forward")
        if not is_ancestor(engine, pending["new_sha"], merged_sha):
            die(f"{merged_sha[:12]} does not contain the release {pending['new_sha'][:12]} — wrong proposal?",
                reason="release-missing")

        ensure_workspace_guard(engine)
        run_git(engine, "merge", "--ff-only", merged_sha)
        landed = git_out(engine, "rev-parse", "HEAD")
        if landed != merged_sha:
            die(f"fast-forward did not land on {merged_sha[:12]} (HEAD is {landed[:12]})", reason="apply-failed")

        os.unlink(args.pending)

        scratch = args.scratch.resolve() if args.scratch else find_scratch(engine, merged_sha)
        removed = None
        if scratch and scratch.exists():
            rm = run_git(engine, "worktree", "remove", "--force", str(scratch), check=False)
            if rm.returncode == 0:
                removed = str(scratch)
            else:
                log(f"scratch worktree not removed ({rm.stderr.strip()}) — clean up manually: {scratch}")
            run_git(engine, "worktree", "prune", check=False)
    finally:
        release_lock(lock_dir)

    emit({"status": "applied", "head": landed, "snapshot_branch": pending["snapshot_branch"],
          "pending_cleared": True, "scratch_removed": removed})


if __name__ == "__main__":
    main()
