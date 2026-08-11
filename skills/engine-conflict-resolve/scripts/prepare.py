#!/usr/bin/env python3
"""prepare.py — reproduce a deferred engine-update merge in a scratch worktree.

Reads the desktop updater's ENGINE_UPDATE_PENDING.json, creates a scratch git
worktree off the snapshot tip (shared object db — no re-fetch), and re-runs the
conflicted merge THERE. Never touches the live checkout's index or worktree.

stdout (machine JSON):
  {"status": "clean",     "scratch": ..., "merged_sha": ..., ...}
  {"status": "conflicts", "scratch": ..., "conflicting_files": [...], ...}
  {"status": "error",     "reason": ..., "error": ...}          (exit 1)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    commit_exists, default_scratch, die, emit, ensure_workspace_guard, git_out,
    is_ancestor, is_git_checkout, load_pending, merge_in_progress, read_meta,
    run_git, set_engine_hint, set_git, tree_dirty, unmerged_paths, write_meta,
)


def result(status: str, scratch: Path, pending: dict, snapshot_tip: str, **extra) -> None:
    payload = {
        "status": status,
        "scratch": str(scratch),
        "snapshot_tip": snapshot_tip,
        "new_sha": pending["new_sha"],
        "snapshot_branch": pending["snapshot_branch"],
    }
    payload.update(extra)
    emit(payload)


def run_merge(scratch: Path, pending: dict, snapshot_tip: str) -> None:
    new_sha = pending["new_sha"]
    msg = "merge engine release %s into %s (Sutando conflict resolution)" % (
        new_sha[:12], pending["snapshot_branch"])
    proc = run_git(scratch, "merge", "--no-edit", "-m", msg, new_sha, check=False, ident=True)
    if proc.returncode == 0:
        result("clean", scratch, pending, snapshot_tip,
               merged_sha=git_out(scratch, "rev-parse", "HEAD"), conflicting_files=[])
    if merge_in_progress(scratch):
        conflicts = unmerged_paths(scratch)
        write_meta(scratch, dict(read_meta(scratch) or {}, conflicting_files=conflicts))
        result("conflicts", scratch, pending, snapshot_tip, conflicting_files=conflicts)
    die(f"merge failed without a conflict state: {proc.stderr.strip()}", reason="merge-failed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pending", required=True, type=Path)
    ap.add_argument("--engine", required=True, type=Path)
    ap.add_argument("--scratch", type=Path, default=None)
    ap.add_argument("--git", default=None, help="trusted git executable (top tier of the declared $ENGINE_CONFLICT_GIT config precedence)")
    args = ap.parse_args()
    set_git(args.git)
    set_engine_hint(args.engine)

    pending = load_pending(args.pending)
    engine = args.engine
    if not engine.is_dir() or not is_git_checkout(engine):
        die(f"engine checkout not found or not a git checkout: {engine}", reason="engine-invalid")

    branch = pending["snapshot_branch"]
    if run_git(engine, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).returncode != 0:
        die(f"snapshot branch '{branch}' not found in {engine} — pending state is stale",
            reason="pending-stale")
    snapshot_tip = git_out(engine, "rev-parse", f"refs/heads/{branch}")
    head = git_out(engine, "rev-parse", "HEAD")

    # Same validity rule as the updater's own reconciliation: the pending state
    # describes the checkout only while HEAD == snapshot tip == old_sha, clean.
    if head != snapshot_tip or snapshot_tip != pending["old_sha"]:
        die("checkout no longer matches the pending state (HEAD %s, snapshot tip %s, pending old_sha %s)"
            " — re-run the app's updater to re-detect" % (head[:12], snapshot_tip[:12], pending["old_sha"][:12]),
            reason="pending-stale")
    if tree_dirty(engine):
        die("checkout has local changes newer than the pending snapshot — re-run the app's updater",
            reason="pending-stale")

    new_sha = pending["new_sha"]
    if not commit_exists(engine, new_sha):
        fetched = run_git(engine, "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=30",
                          "fetch", "--depth", "50", "origin", new_sha, check=False)
        if fetched.returncode != 0 or not commit_exists(engine, new_sha):
            die(f"release commit {new_sha[:12]} not present and could not be fetched",
                reason="new-sha-missing")

    scratch = (args.scratch or default_scratch(engine, new_sha)).resolve()

    if scratch.exists():
        meta = read_meta(scratch) if is_git_checkout(scratch) else None
        if not meta or meta.get("new_sha") != new_sha or meta.get("snapshot_tip") != snapshot_tip:
            die(f"scratch {scratch} exists but was made for a different merge — remove it or pass --scratch",
                reason="scratch-mismatch")
        if merge_in_progress(scratch):
            result("conflicts", scratch, pending, snapshot_tip,
                   conflicting_files=unmerged_paths(scratch), reused=True)
        scratch_head = git_out(scratch, "rev-parse", "HEAD")
        if scratch_head != snapshot_tip and is_ancestor(scratch, new_sha, scratch_head):
            result("clean", scratch, pending, snapshot_tip, merged_sha=scratch_head,
                   conflicting_files=[], reused=True)
        if scratch_head != snapshot_tip or tree_dirty(scratch):
            die(f"scratch {scratch} is in an unexpected state — remove it or pass --scratch",
                reason="scratch-mismatch")
        run_merge(scratch, pending, snapshot_tip)  # same base, merge never ran: retry

    scratch.parent.mkdir(parents=True, exist_ok=True)
    run_git(engine, "worktree", "add", "--no-checkout", "--detach", str(scratch), snapshot_tip)
    # The engine repo runs sparse (attach's workspace guard is repo config, the
    # pattern file is per-worktree) — give the scratch the same guard, then populate.
    if git_out(engine, "config", "--default", "false", "--bool", "core.sparseCheckout") == "true":
        ensure_workspace_guard(scratch)
    run_git(scratch, "reset", "--hard", snapshot_tip)
    write_meta(scratch, {
        "engine": str(engine.resolve()),
        "pending": str(args.pending.resolve()),
        "new_sha": new_sha,
        "old_sha": pending["old_sha"],
        "snapshot_tip": snapshot_tip,
        "snapshot_branch": branch,
        "conflicting_files": list(pending.get("conflicting_files") or []),
    })
    run_merge(scratch, pending, snapshot_tip)


if __name__ == "__main__":
    main()
