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
and 5 carried real peer content that had sat unmerged for hours. Nothing
reported the difference, so the count of batches looked alarming and the count
that mattered was invisible.

WHY NOT A LINE-COUNT THRESHOLD. The first version ignored diffs of <= 3 lines as
"reformatting noise". qingyun-wu reproduced the hole: a peer copy one line longer
-- `important new fact` -- reported clean. Line count cannot separate a reflow
from a short, meaningful addition, and the short addition is exactly what this
tool exists to catch.

It was not hypothetical. Re-classifying the 13 by CONTENT moved two files out of
"trivial" and into real loss (3 -> 5), including a `MEMORY.md` overflow cap and
two lines documenting a send-syntax trap. My own earlier analysis had called both
noise on the strength of their line count.

The discriminator is presence, not size: a reflow re-lays-out text that still
EXISTS in the live copy, so its content is found there under whitespace
normalisation. Genuinely new content is absent at any length. One line of new
text is reported; a hundred lines of pure re-wrapping are not.
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time

# REPO root, to import the resolver from src/ — not a workspace path. The
# line-scoped pragma is the sanctioned form: it exempts this one line and leaves
# every other line in the file visible to the lint, unlike a file-level
# allowlist entry (which #2639 showed is a blind spot, not an exemption).
ROOT = pathlib.Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(ROOT / "src"))
from workspace_default import resolve_workspace  # noqa: E402

def _new_content(saved: str, live: str) -> "list[str]":
    """Lines in the preserved copy whose TEXT is absent from the live copy.

    Whitespace-normalised on both sides, so re-wrapping, re-indenting and
    trailing-space churn all resolve to "already present" — the reflow case the
    old line-count threshold was reaching for, without its blindness to a single
    real line.
    """
    # Space-PADDED on both sides so the containment test lands on word
    # boundaries. A raw substring search reports clean whenever a saved line
    # happens to sit inside unrelated live text -- `The peer fact` is a
    # substring of `The peer facts are documented elsewhere`, so a genuinely
    # unmerged line was treated as present. That is a false CLEAN in the
    # discriminator this PR exists to make trustworthy. (qingyun-wu, #2662.)
    haystack = " ".join(live.split())
    seen = set(live.splitlines())
    out = []
    for line in saved.splitlines():
        if not line.strip() or line in seen:
            continue
        needle = " ".join(line.split())
        # Boundary = a NON-WORD character (or the string edge), not specifically
        # a space. Space-padding was the first attempt and it over-tightened: a
        # live line that gained trailing punctuation or was extended mid-sentence
        # then read as absent. Found on real data — a memory index entry whose
        # live copy is the saved text plus a period and an annotation was
        # reported missing, though the content is fully present.
        #
        #   saved  '... match the body'
        #   live   '... match the body. *(Restored 2026-08-04 ...)*'
        #
        # `\w` boundaries keep qingyun-wu's collision case reported --
        # `The peer fact` inside `The peer facts` is followed by `s`, a word
        # character -- while treating punctuation as a legitimate boundary.
        if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack):
            continue  # same text, laid out differently
        out.append(line)
    return out


def _retired_path(root: pathlib.Path) -> pathlib.Path:
    return root / ".retired.json"


def _load_retired(root: pathlib.Path) -> dict:
    try:
        return json.loads(_retired_path(root).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _entry_key(batch: str, rel: pathlib.PurePath, saved_text: str) -> str:
    """Identify one preserved COPY, not just one path.

    The digest is part of the key on purpose. Retiring `memory/x.md` from one
    batch must not silence a LATER conflict that preserved different content for
    the same path -- that would trade a permanent false positive for a permanent
    false negative, which is the worse of the two.
    """
    digest = hashlib.sha256(saved_text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{batch}/{rel}@{digest}"


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
    retired = _load_retired(root)
    out = []
    for batch in sorted(p for p in root.iterdir() if p.is_dir()):
        for saved in batch.rglob("*"):
            if not saved.is_file():
                continue
            saved_text = saved.read_text(errors="replace")
            rel = saved.relative_to(batch)
            # A RETIRED entry stays silent whatever the live file does later.
            # Without this the reporter has no way to tell "never merged" from
            # "merged, then deliberately retracted" -- the end states are
            # identical on disk -- so a legitimate correction would be flagged
            # as missing content on every 30-minute sync, forever. That is the
            # exact failure this PR cites when rejecting a union merge:
            # a withdrawn claim must not be resurrected, including as a nag.
            # (john-the-dev, #2662 P1.)
            if _entry_key(batch.name, rel, saved_text) in retired:
                continue
            live = workspace / rel
            if not live.exists():
                out.append((batch.name, rel, None))
                continue
            extra = _new_content(saved_text, live.read_text(errors="replace"))
            if extra:
                out.append((batch.name, rel, len(extra)))
    return out, None


def retire(workspace: pathlib.Path, targets: "list[str]"):
    """Mark preserved copies as dealt with — merged, or deliberately declined.

    Either decision ends the same way: the operator has SEEN this content and
    the live file is now correct. Recording that is what lets a later retraction
    stay retracted instead of being re-reported as missing.

    SELECTION FAILS CLOSED. A target must be `<batch>/<relpath>` and must match
    exactly ONE preserved copy; zero matches, an ambiguous match, or no target at
    all mutates nothing and returns an error.

    The first version matched on basename OR relpath across EVERY batch, so
    `--retire note.md` retired batch-a's merged copy AND batch-b's never-merged
    copy, turning exit 1 into a permanent false-clean; bare `--retire` retired
    everything for the same reason (empty targets read as "match all"). The
    content digest in the key protects a LATER copy, not a distinct copy already
    on disk. My own comment on that key called a permanent false negative "the
    worse of the two" — and the selector shipped exactly that.
    (john-the-dev, #2662.)
    """
    _, err = unmerged(workspace)          # reuse its validation of the workspace
    if err:
        return None, err
    gitdir = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--git-dir"],
                            capture_output=True, text=True)
    root = pathlib.Path(gitdir.stdout.strip())
    if not root.is_absolute():
        root = workspace / root
    root = root / "sutando-sync-conflicts"
    if not root.is_dir():
        return [], None
    # Scan EVERY preserved copy, not just the ones currently reporting. The
    # operator retires right after merging, when the live file already matches
    # and the entry is therefore silent -- so a retire limited to the reporting
    # set could never record the case it exists for, and the first later
    # retraction would resurrect it. (Caught by activating john-the-dev's
    # sequence before pushing: step 2 printed "nothing matched".)
    if not targets:
        return None, ("--retire needs an explicit <batch>/<path> selector; "
                      "refusing to retire everything")
    # Index every preserved copy by its batch-qualified path FIRST, so selection
    # is resolved against the full set and ambiguity is detectable before any
    # write.
    index: "dict[str, list]" = {}
    for batch in sorted(p for p in root.iterdir() if p.is_dir()):
        for saved in batch.rglob("*"):
            if not saved.is_file():
                continue
            rel = saved.relative_to(batch)
            index.setdefault(f"{batch.name}/{rel}", []).append((batch.name, rel, saved))
    resolved = []
    for t in targets:
        hits = [v for k, vs in index.items() if k == t for v in vs]
        if not hits:
            near = [k for k in index if k.endswith("/" + t) or pathlib.PurePath(k).name == t]
            hint = (f" — did you mean one of: {', '.join(sorted(near)[:4])}"
                    if near else "")
            return None, f"no preserved copy matches {t!r}{hint}"
        if len(hits) > 1:
            return None, f"{t!r} is ambiguous ({len(hits)} copies) — qualify it further"
        resolved.append(hits[0])
    retired = _load_retired(root)
    done = []
    for batch_name, rel, saved in resolved:
        key = _entry_key(batch_name, rel, saved.read_text(errors="replace"))
        if key in retired:
            continue
        retired[key] = {"retired_at": int(time.time()), "path": str(rel),
                        "batch": batch_name}
        done.append(f"{batch_name}/{rel}")
    _retired_path(root).write_text(json.dumps(retired, indent=1, sort_keys=True))
    return done, None


def main() -> int:
    # No positional arg -> the canonical resolver, never Path.cwd(). A reporter
    # about workspace state that guessed the workspace from the caller's cwd
    # would answer about whichever directory happened to invoke it -- and the
    # cron path invokes it from the repo, not the workspace. `migrate=False`
    # because this is a read-only diagnostic: it must never trigger a migration
    # as a side effect of being asked a question. (bassilkhilo-ag2, #2662.)
    argv = sys.argv[1:]
    targets: "list[str]" = []
    if "--retire" in argv:
        i = argv.index("--retire")
        targets = argv[i + 1:] or []
        argv = argv[:i]
    ws = pathlib.Path(argv[0]) if argv else pathlib.Path(
        resolve_workspace(migrate=False))
    if "--retire" in sys.argv:
        done, err = retire(ws, targets)
        if err:
            print(f"sync-conflicts: {err}")
            return 2
        if not done:
            print("sync-conflicts: nothing matched — nothing retired")
            return 0
        print(f"sync-conflicts: retired {len(done)} entr(ies); they will stay silent "
              f"even if the live copy later changes")
        for d in done:
            print(f"  {d}")
        return 0
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
