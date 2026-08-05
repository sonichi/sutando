#!/usr/bin/env python3
"""Report peer content that sync discarded and nobody has merged back.

`_resolve_conflicts_keep_ours` resolves every conflict by keeping the local
version and preserving the incoming one under `<git-dir>/sutando-sync-conflicts/`.
That is the right default -- it is recoverable, whereas a union merge would
never lose a line but would resurrect an in-place retraction next to its own
correction, which reads as current (demonstrated 2026-08-05).

Recoverable only helps if someone looks. Measured on Chis-MacBook-Pro the same
day: 13 preserved files, of which 6 were strict SUBSETS of the local copy (zero
loss -- keeping ours was correct), 1 was a legacy flat path retired by #2567,
and 3 carried real peer content that had sat unmerged for hours. Nothing
reported the difference, so the count of batches looked alarming and the count
that mattered was invisible.

This prints only the third category: files where the discarded copy still holds
lines the live copy lacks.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from workspace_default import resolve_workspace  # noqa: E402

TRIVIAL_LINES = 3  # a handful of differing lines is reformatting, not content


def unmerged(workspace: pathlib.Path):
    if not workspace.is_dir():
        return None, f"no such directory: {workspace}"
    # `git rev-parse` SEARCHES ANCESTORS. Pointing it at a non-repo directory
    # that merely sits inside one succeeds and answers about the ANCESTOR --
    # so a workspace that was never `--init`ed reports "no unmerged peer
    # content" about somebody else's repository. Caught by running this from a
    # PR worktree, where the fallback workspace exists but is not a repo and
    # git happily returned the worktree's own git dir.
    top = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if top.returncode:
        return None, f"not a git repo: {workspace}"
    if pathlib.Path(top.stdout.strip()).resolve() != workspace.resolve():
        return None, (f"not a git repo: {workspace} "
                      f"(git resolved the ancestor {top.stdout.strip()})")
    gitdir = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    )
    if gitdir.returncode:
        return None, f"not a git repo: {workspace}"
    root = pathlib.Path(gitdir.stdout.strip())
    if not root.is_absolute():
        root = workspace / root
    root = root / "sutando-sync-conflicts"
    if not root.is_dir():
        return [], None
    out = []
    for batch in sorted(p for p in root.iterdir() if p.is_dir()):
        for saved in batch.rglob("*"):
            if not saved.is_file():
                continue
            live = workspace / saved.relative_to(batch)
            if not live.exists():
                out.append((batch.name, saved.relative_to(batch), None))
                continue
            ours = set(live.read_text(errors="replace").splitlines())
            extra = [
                ln for ln in saved.read_text(errors="replace").splitlines()
                if ln not in ours and ln.strip()
            ]
            if len(extra) > TRIVIAL_LINES:
                out.append((batch.name, saved.relative_to(batch), len(extra)))
    return out, None


def main() -> int:
    # No positional arg -> the canonical resolver, never Path.cwd(). A reporter
    # about workspace state that guessed the workspace from the caller's cwd
    # would answer about whichever directory happened to invoke it -- and the
    # cron path invokes it from the repo, not the workspace. `migrate=False`
    # because this is a read-only diagnostic: it must never trigger a migration
    # as a side effect of being asked a question. (bassilkhilo-ag2, #2662.)
    ws = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(
        resolve_workspace(migrate=False))
    rows, err = unmerged(ws)
    if err:
        print(f"sync-conflicts: {err}")
        return 2
    if not rows:
        # Name the workspace even on the clean path. "no unmerged peer content"
        # is only meaningful if the reader can see WHICH workspace was examined;
        # the ancestor-walk bug above produced exactly that sentence about the
        # wrong repository, and printing the path is what made it visible.
        print(f"sync-conflicts: no unmerged peer content ({ws})")
        return 0
    print(f"sync-conflicts: {len(rows)} file(s) hold peer content not in the live copy")
    for batch, rel, n in rows:
        where = f"{n} lines" if n is not None else "live file MISSING"
        print(f"  {rel}  ({where})  <- {batch}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
