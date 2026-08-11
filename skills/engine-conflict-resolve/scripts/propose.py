#!/usr/bin/env python3
"""propose.py — commit the resolved merge in the scratch and print the proposal.

Run after the agent has resolved every conflict in the scratch worktree and
`git add`-ed the resolutions. Refuses (exit 2) while unmerged paths remain.

stdout (machine JSON):
  {"status": "proposed", "merged_sha": ..., "files": [...], "diffstat": ...,
   "summary_lines": [...]}
  {"status": "unmerged", "unmerged_paths": [...]}                (exit 2)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    WS_EXCLUDE, die, emit, engine_hint_from_scratch, git_out, is_ancestor,
    is_git_checkout, merge_in_progress, proposal_path, read_meta, run_git,
    set_git, tree_dirty, unmerged_paths, write_proposal,
)


def blob(scratch: Path, rev: str, path: str) -> Optional[str]:
    proc = run_git(scratch, "rev-parse", "-q", "--verify", f"{rev}:{path}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def kept_line(scratch: Path, path: str, merged: str, local: str, update: str) -> str:
    b_merged = blob(scratch, merged, path)
    b_local = blob(scratch, local, path)
    b_update = blob(scratch, update, path)
    if b_merged is None:
        verdict = "removed in the resolution"
    elif b_merged == b_local and b_merged == b_update:
        verdict = "identical on both sides"
    elif b_merged == b_local:
        verdict = "kept your local version"
    elif b_merged == b_update:
        verdict = "took the release version"
    else:
        verdict = "hand-merged — combines local and release changes"
    return f"{path}: {verdict}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scratch", required=True, type=Path)
    ap.add_argument("--git", default=None, help="trusted git executable (top tier of the declared $ENGINE_CONFLICT_GIT config precedence)")
    args = ap.parse_args()
    set_git(args.git)
    engine_hint_from_scratch(args.scratch)

    scratch = args.scratch.resolve()
    if not scratch.is_dir() or not is_git_checkout(scratch):
        die(f"scratch worktree not found: {scratch}", reason="scratch-invalid")
    meta = read_meta(scratch)
    if not meta:
        die(f"{scratch} was not created by prepare.py (no merge metadata)", reason="scratch-invalid")

    unmerged = unmerged_paths(scratch)
    if unmerged:
        emit({"status": "unmerged", "unmerged_paths": unmerged,
              "error": "conflicts are not fully resolved+staged — edit the files, `git add` them, re-run"},
             exit_code=2)

    snapshot_tip = meta["snapshot_tip"]
    new_sha = meta["new_sha"]
    if merge_in_progress(scratch) or tree_dirty(scratch):
        # The whole scratch tree IS the proposal — stage any still-unstaged edits
        # so a resolution can't silently drop, then conclude the merge.
        run_git(scratch, "add", "-A", "--", ".", WS_EXCLUDE)
        msg = "merge: engine release %s resolved by Sutando (%s)" % (
            new_sha[:12], meta["snapshot_branch"])
        run_git(scratch, "commit", "--no-verify", "-m", msg, ident=True)

    merged_sha = git_out(scratch, "rev-parse", "HEAD")
    if merged_sha == snapshot_tip or not is_ancestor(scratch, new_sha, merged_sha):
        die("scratch has no merge to propose (HEAD does not contain the release) — run prepare.py first",
            reason="nothing-to-propose")

    files = []
    for line in run_git(scratch, "diff", "--name-status", snapshot_tip, merged_sha).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0], "path": parts[-1]})
    diffstat = run_git(scratch, "diff", "--stat", snapshot_tip, merged_sha).stdout.rstrip()

    conflicted = list(meta.get("conflicting_files") or [])
    summary = ["Merge of release %s into '%s' — %d conflicted file(s) resolved, %d file(s) changed overall"
               % (new_sha[:12], meta["snapshot_branch"], len(conflicted), len(files))]
    summary += [kept_line(scratch, p, merged_sha, snapshot_tip, new_sha) for p in conflicted]

    # Bind the confirmation to THIS exact commit: apply.py refuses any sha that
    # is not the recorded one. Re-proposing atomically overwrites the record.
    write_proposal(Path(meta["pending"]), {
        "merged_sha": merged_sha,
        "old_sha": meta["old_sha"],
        "new_sha": new_sha,
        "snapshot_branch": meta["snapshot_branch"],
        "scratch": str(scratch),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    emit({
        "status": "proposed",
        "merged_sha": merged_sha,
        "snapshot_tip": snapshot_tip,
        "new_sha": new_sha,
        "files": files,
        "diffstat": diffstat,
        "summary_lines": summary,
        "proposal_record": str(proposal_path(Path(meta["pending"]))),
    })


if __name__ == "__main__":
    main()
