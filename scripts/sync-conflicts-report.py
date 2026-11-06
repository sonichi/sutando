#!/usr/bin/env python3
"""Report peer content that sync discarded and nobody has merged back."""
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
from workspace_default import resolve_workspace, status_path, write_status  # noqa: E402

CACHE_NAME = "sync-conflicts-report-cache.json"
LAST_RUN_NAME = "sync-conflicts-report-last-run.json"
DAILY_SECONDS = 24 * 3600
# A change to this file invalidates every cached row, so a fixed comparison rule
# never serves results computed by the old one.
_SCANNER_VERSION = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:16]

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")

# Distinct from None: None means the live path is absent, this means it exists
# and is not a file, so no line-level comparison is possible either way.
NOT_A_FILE = "not-a-file"


def _by_section(text: str):
    """Yield (heading, line) for every line, heading = nearest one ABOVE it."""
    head = ""
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            head = " ".join(line.split())
        yield head, line


def _present_bounded(needle: str, hay: str) -> bool:
    """`needle` occurs in `hay` with a non-word character (or an edge) on both
    sides. A plain find with a boundary check, not a compiled regex per line:
    measured 18 ms vs 0.13 ms per line on a 2 MB haystack, 144x."""
    i = hay.find(needle)
    n = len(needle)
    while i != -1:
        j = i + n
        left_ok = i == 0 or not (hay[i - 1].isalnum() or hay[i - 1] == "_")
        right_ok = j == len(hay) or not (hay[j].isalnum() or hay[j] == "_")
        if left_ok and right_ok:
            return True
        i = hay.find(needle, i + 1)
    return False


def _new_content(saved: str, live: str) -> "list[str]":
    """Lines in the preserved copy whose TEXT is absent from live IN THE SAME SECTION.
    Blind to a renamed heading (falls back to global) and to reordering within a section."""
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
        if _present_bounded(needle, hay):
            continue  # same text, laid out differently
        out.append(line)
    return out


def _split_by_reason(extra: "list[str]", live: str) -> "tuple[list[str], list[str]]":
    """Split reported lines into (absent-entirely, present-under-another-heading)."""
    haystack = " ".join(live.split())
    absent, resectioned = [], []
    for line in extra:
        needle = " ".join(line.split())
        if _present_bounded(needle, haystack):
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
    The digest is in the key so retiring one copy cannot silence a later, different one."""
    digest = hashlib.sha256(saved_text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{batch}/{rel}@{digest}"


def _stat_key(p: pathlib.Path):
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_ctime_ns, st.st_size]


def _load_cache(workspace: pathlib.Path) -> dict:
    try:
        data = json.loads(status_path(CACHE_NAME, workspace).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCANNER_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _row_to_json(n):
    return list(n) if isinstance(n, tuple) else n


def _row_from_json(n):
    return tuple(n) if isinstance(n, list) else n


def unmerged(workspace: pathlib.Path, full: bool = False):
    """Rows of unreconciled preserved copies. Unless `full`, a copy whose saved and
    live stat keys match the previous run reuses that run's row without a read."""
    if not workspace.is_dir():
        return None, f"no such directory: {workspace}"
    # `git rev-parse` SEARCHES ANCESTORS, so a non-repo workspace answers about
    # its ancestor -- hence the toplevel-equality check below.
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
    cache = {} if full else _load_cache(workspace)
    fresh: "dict[str, dict]" = {}
    out = []
    for batch in sorted(p for p in root.iterdir() if p.is_dir()):
        for saved in batch.rglob("*"):
            if not saved.is_file():
                continue
            rel = saved.relative_to(batch)
            live = workspace / rel
            if not live.exists():
                live_key = "missing"
            elif not live.is_file():
                live_key = NOT_A_FILE
            else:
                live_key = _stat_key(live)
            saved_key = _stat_key(saved)
            ck = f"{batch.name}/{rel}"
            hit = cache.get(ck)
            if (hit and saved_key is not None and live_key is not None
                    and hit.get("saved") == saved_key and hit.get("live") == live_key):
                key, n = hit["key"], _row_from_json(hit["row"])
            else:
                key, n = _scan_one(batch.name, rel, saved, live)
            if saved_key is not None and live_key is not None:
                fresh[ck] = {"saved": saved_key, "live": live_key, "key": key,
                             "row": _row_to_json(n)}
            # A RETIRED entry stays silent whatever the live file does later:
            # "never merged" and "merged then retracted" are identical on disk.
            if key in retired or n == "clean":
                continue
            out.append((batch.name, rel, n))
    try:
        write_status(CACHE_NAME, {"version": _SCANNER_VERSION, "entries": fresh}, workspace)
    except OSError:
        pass  # a cache that cannot be written costs speed, never correctness
    return out, None


def _scan_one(batch_name: str, rel: pathlib.PurePath, saved: pathlib.Path,
              live: pathlib.Path):
    """(retirement key, row) for one preserved copy; row "clean" means nothing to report."""
    saved_text = saved.read_text(errors="replace")
    key = _entry_key(batch_name, rel, saved_text)
    if not live.exists():
        return key, None
    # A saved path can resolve to a directory, which read_text() cannot
    # read. Non-comparable is not reconciled, so the row stays.
    if not live.is_file():
        return key, NOT_A_FILE
    live_text = live.read_text(errors="replace")
    extra = _new_content(saved_text, live_text)
    if not extra:
        return key, "clean"
    absent, resectioned = _split_by_reason(extra, live_text)
    return key, (len(extra), len(absent), len(resectioned))


def retire(workspace: pathlib.Path, targets: "list[str]"):
    """Mark preserved copies as dealt with — merged, or deliberately declined.
    Keys are batch-qualified: a bare basename match would retire another batch's copy."""
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
    # Index by batch-qualified path FIRST, so ambiguity is detectable before
    # any write.
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
        # No len>1 guard: the key "<batch>/<relpath>" is unique per walk, so a
        # bucket cannot hold two; a bare basename matches NO key instead.
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


def _seconds_since_last_run(ws: pathlib.Path):
    try:
        return int(time.time()) - int(json.loads(status_path(LAST_RUN_NAME, ws).read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main() -> int:
    # No positional arg -> canonical resolver, never Path.cwd(): the cron path
    # invokes this from the repo. `migrate=False` -- read-only diagnostic.
    argv = sys.argv[1:]
    targets: "list[str]" = []
    if "--retire" in argv:
        i = argv.index("--retire")
        targets = argv[i + 1:] or []
        argv = argv[:i]
    # --daily: the sync cron's gate. --force: run despite it. --full: ignore the cache.
    flags = {a for a in argv if a in ("--daily", "--force", "--full")}
    argv = [a for a in argv if a not in flags]
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
    if "--daily" in flags and "--force" not in flags:
        age = _seconds_since_last_run(ws)
        if age is not None and 0 <= age < DAILY_SECONDS:
            print(f"sync-conflicts: skipped (last report {age // 3600}h ago; at most "
                  f"once per 24h; --force runs it now)")
            return 0
    rows, err = unmerged(ws, full=True) if "--full" in flags else unmerged(ws)
    if err:
        print(f"sync-conflicts: {err}")
        return 2
    try:
        write_status(LAST_RUN_NAME, {"ts": int(time.time())}, ws)
    except OSError:
        pass
    if not rows:
        # Name the workspace even on the clean path: "no unmerged peer content"
        # means nothing unless the reader can see WHICH workspace was examined.
        print(f"sync-conflicts: no unmerged peer content ({ws})")
        return 0
    print(f"sync-conflicts: {len({rel for _batch, rel, _n in rows})} file(s) hold peer content not in the live copy")
    for batch, rel, n in rows:
        if n is None:
            where = "live file MISSING"
        elif n == NOT_A_FILE:
            where = "live path is NOT A FILE — not comparable, not reconciled"
        else:
            total, absent, resectioned = n
            # Name WHICH kind: `absent` may be real loss, `under another
            # heading` is present text whose ASSOCIATION changed. Neither filtered.
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
