#!/usr/bin/env python3
"""Report peer content that sync discarded and nobody has merged back.

Classify by presence, not size: a reflow's text still exists in the live copy
under whitespace normalisation, while new content is absent at any length.
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time

# REPO root, not a workspace path. The line-scoped pragma exempts only this
# line; a file-level allowlist entry would blind the lint to the whole file.
ROOT = pathlib.Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(ROOT / "src"))
from workspace_default import resolve_workspace  # noqa: E402

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


def _by_section(text: str):
    """Yield (heading, line) for every line, heading = nearest one ABOVE it.

    A heading line's own context is itself, so a renamed heading is detected
    like any other changed line. Lines before the first heading get "".
    """
    head = ""
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            head = " ".join(line.split())
        yield head, line


def _new_content(saved: str, live: str) -> "list[str]":
    """Lines in the preserved copy whose TEXT is absent from the live copy
    IN THE SAME SECTION.

    Section-scoped because a global presence test discards heading association,
    so an inverted policy (the same names swapped between two headings) reads as
    clean. Two blind spots remain: a saved line whose heading is absent from live
    falls back to a global search, and pure reordering within one section is
    undetected.
    """
    # Word-boundary matching, not raw substring: `The peer fact` sits inside
    # `The peer facts ...`, which would report an unmerged line as present.
    haystack = " ".join(live.split())          # global fallback (renamed section)
    live_pairs = set()
    live_lines_by_section: "dict[str, list[str]]" = {}
    for head, line in _by_section(live):
        live_pairs.add((head, line))
        live_lines_by_section.setdefault(head, []).append(line)
    # One haystack PER section, so text elsewhere cannot vouch for a line that
    # moved out of this one.
    live_haystacks = {
        h: " ".join(" ".join(ls).split()) for h, ls in live_lines_by_section.items()
    }
    out = []
    for head, line in _by_section(saved):
        if not line.strip() or (head, line) in live_pairs:
            continue
        needle = " ".join(line.split())
        # Absent section falls back to the global haystack. LOCAL name:
        # rebinding `haystack` would leak this section's scope into later ones.
        hay = live_haystacks.get(head, haystack)
        # Boundary is any NON-WORD character, not a space: a live line that
        # gained trailing punctuation would otherwise read as absent.
        if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay):
            continue  # same text, laid out differently
        out.append(line)
    return out


def _split_by_reason(extra: "list[str]", live: str) -> "tuple[list[str], list[str]]":
    """Split reported lines into (absent-entirely, present-under-another-heading).

    Section-scoping also reports a line that merely MOVED between headings: the
    text is present, the association is not. The second bucket is labelled, not
    filtered -- a policy inversion IS a line under a different heading.

    The test is deliberately the OLD global-haystack rule, so the second bucket
    is exactly "what the pre-section-scoping version would have called clean".
    """
    haystack = " ".join(live.split())
    absent, resectioned = [], []
    for line in extra:
        needle = " ".join(line.split())
        if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack):
            resectioned.append(line)
        else:
            absent.append(line)
    return absent, resectioned


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
    # `git rev-parse` SEARCHES ANCESTORS, so a non-repo workspace answers about
    # its ancestor -- hence the toplevel-equality check below. ONE rev-parse for
    # both answers: `--git-dir` cannot fail once `--show-toplevel` succeeded.
    rp = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel", "--git-dir"],
        capture_output=True, text=True,
    )
    if rp.returncode:
        return None, f"not a git repo: {workspace}"
    lines = rp.stdout.strip().splitlines()
    if len(lines) < 2:
        return None, f"not a git repo: {workspace}"
    top_s, gitdir_s = lines[0], lines[1]
    if pathlib.Path(top_s).resolve() != workspace.resolve():
        return None, (f"not a git repo: {workspace} "
                      f"(git resolved the ancestor {top_s})")
    root = pathlib.Path(gitdir_s)
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
            # A RETIRED entry stays silent whatever the live file does later:
            # "never merged" and "merged then retracted" are identical on disk.
            if _entry_key(batch.name, rel, saved_text) in retired:
                continue
            live = workspace / rel
            if not live.exists():
                out.append((batch.name, rel, None))
                continue
            live_text = live.read_text(errors="replace")
            extra = _new_content(saved_text, live_text)
            if extra:
                absent, resectioned = _split_by_reason(extra, live_text)
                out.append((batch.name, rel, (len(extra), len(absent), len(resectioned))))
    return out, None


def retire(workspace: pathlib.Path, targets: "list[str]"):
    """Mark preserved copies as dealt with — merged, or deliberately declined.

    Either decision ends the same way: the operator has SEEN this content and
    the live file is now correct. Recording that is what lets a later retraction
    stay retracted instead of being re-reported as missing.

    SELECTION FAILS CLOSED. A target must be `<batch>/<relpath>` and must match
    exactly ONE preserved copy; zero matches, an ambiguous match, or no target at
    all mutates nothing and returns an error.

    The key is batch-qualified and digest-bearing: a basename-or-relpath match
    across batches would retire another batch's never-merged copy, and empty
    targets would read as "match all". The digest protects a LATER copy only.
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
    # Scan EVERY preserved copy, not just reporting ones: the operator retires
    # right after merging, when the entry is already silent.
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
        # No len>1 guard: `index` is keyed by "<batch>/<relpath>", which is unique
        # per filesystem walk, so a bucket cannot hold two. Ambiguity is caught
        # one line up instead — a bare basename matches NO key and the near-miss
        # hint names the qualified candidates.
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
    # No positional arg -> canonical resolver, never Path.cwd(): the cron path
    # invokes this from the repo. `migrate=False` -- read-only diagnostic.
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
        if n is None:
            where = "live file MISSING"
        else:
            total, absent, resectioned = n
            # Name WHICH kind, because the two need different responses and the
            # bare count could not distinguish them: `absent` may be real loss;
            # `under another heading` is present text whose ASSOCIATION changed,
            # which is both the benign re-section case AND the policy inversion
            # this reporter exists to catch. Neither is filtered out.
            parts = []
            if absent:
                parts.append(f"{absent} absent")
            if resectioned:
                parts.append(f"{resectioned} under another heading")
            where = f"{total} lines: " + ", ".join(parts)
        print(f"  {rel}  ({where})  <- {batch}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
