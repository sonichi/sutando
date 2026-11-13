#!/usr/bin/env python3
"""Sutando-owned hook entries in a project-level Claude Code settings.json: install one idempotently, prune dead copies of the same hook, and re-point hooks a moved install left on its old path.

Two installers (``scripts/install-personal-claude-hook.sh``,
``scripts/install-session-start-hook.sh``) carried their own inline merge. Each
could add its entry but neither could remove one, so a test run from a copy of
the repo under a temp dir left the live settings pointing at three deleted
``/var/folders/…/repo/src/…`` scripts, and every compaction fired all four.

Pruning is scoped to the SAME FAMILY as the hook being installed — entries whose
command runs a script with the same basename — and only when that script no
longer exists. A user's own hooks, dead or alive, are never touched.

Why a bare existence check is enough: the two costs are asymmetric. A false
prune (the script is merely unreachable at launch) costs ONE launch without that
hook, because every installer re-adds its own command at the next core launch.
A missed prune (a dead copy left in place) costs every compaction until a human
notices — which is how this reached the owner. So the predicate stays broad and
the blast radius is bounded by family, not by path shape — with one exception: a
path carrying an unexpanded variable is skipped, because the installer would re-add
its own ABSOLUTE command, never the portable one, so that false prune is permanent
rather than costing one launch.

Re-pointing is a separate step from any installer: it rewrites only the old root
of a hook whose script is gone and whose repo-relative remainder exists in this
checkout, so it never adds a hook (the opt-in transcript archiver included).

``install`` also re-points its own hook when an older checkout that still exists on
disk left it registered: an entry that is the installed command exactly, except for
the checkout root before ``src/``/``scripts/``/``skills/``, under the same matcher,
is rewritten in place rather than kept beside the new one. Any other difference,
the matcher included, marks it as not ours. Two live checkouts sharing one settings
file therefore hand the hook to whichever launched last.

    python3 src/claude_hooks_settings.py install --settings <path> \\
        --event SessionStart --command 'bash "<repo>/src/x.sh"' \\
        [--matcher compact] [--prepend] [--label "x hook"]
    python3 src/claude_hooks_settings.py repair-paths --repo <repo> --settings <path> [...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

_INTERPRETERS = {"bash", "sh", "zsh", "python", "python3", "node"}


def script_path_of(command: str) -> Optional[str]:
    """The script a hook command runs: the first token after an interpreter, else the first token."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    for index, token in enumerate(tokens):
        if os.path.basename(token) in _INTERPRETERS:
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if not token.startswith("-"):
            return token
    return None


def family_of(command: str) -> str:
    path = script_path_of(command)
    return os.path.basename(path) if path else ""


def load(settings_path: Path) -> dict:
    if not settings_path.is_file():
        return {"hooks": {}}
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path}: top level is not an object")
    data.setdefault("hooks", {})
    return data


def save(settings_path: Path, settings: dict) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)


def _judgeable(path: str) -> bool:
    """False when the path carries an unexpanded variable, which no existence check can decide."""
    return "$" not in path


def _exists(path: str, project_dir: Optional[Path]) -> bool:
    """A relative script path is relative to the project the settings file belongs to."""
    candidate = Path(path)
    if not candidate.is_absolute() and project_dir is not None:
        candidate = project_dir / candidate
    return candidate.exists()


def prune_dead(settings: dict, event: str, family: str,
               project_dir: Optional[Path] = None) -> list[str]:
    """Remove hooks under ``event`` that run a script named ``family`` which no longer exists.

    Returns the removed commands. Entries left with no hooks are dropped too. A relative
    script path is checked against ``project_dir`` (the settings file's project), never the
    caller's cwd, so a live relative hook cannot read as dead.
    """
    if not family:
        return []
    removed: list[str] = []
    kept_entries = []
    for entry in settings.get("hooks", {}).get(event, []) or []:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        kept_hooks = []
        for hook in entry.get("hooks", []) or []:
            command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
            path = script_path_of(command)
            if (family_of(command) == family and path and _judgeable(path)
                    and not _exists(path, project_dir)):
                removed.append(command)
                continue
            kept_hooks.append(hook)
        if kept_hooks:
            kept_entries.append(dict(entry, hooks=kept_hooks))
    if removed:
        settings.setdefault("hooks", {})[event] = kept_entries
    return removed


def is_other_checkout_copy(existing: str, command: str) -> bool:
    """True when ``existing`` is ``command`` exactly, except that its script sits in another checkout.

    The script paths must share a repo-relative tail (``src/…``, ``scripts/…``, ``skills/…``) and
    differ only in the root before it; any other difference (a flag, a wrapper) is not ours.
    """
    ours, theirs = script_path_of(command), script_path_of(existing)
    if not ours or not theirs or ours == theirs or existing == command:
        return False
    if not (ours.startswith("/") and theirs.startswith("/")) or not _judgeable(theirs):
        return False
    a, b = ours.split("/"), theirs.split("/")
    n = 0
    while n < min(len(a), len(b)) - 1 and a[-1 - n] == b[-1 - n]:
        n += 1
    if not any(part in _REPO_TOP_DIRS for part in a[len(a) - n:-1]):
        return False
    our_root, their_root = "/".join(a[:len(a) - n]), "/".join(b[:len(b) - n])
    return any(command.replace(o + "/", t + "/") == existing
               for o, t in zip(_spellings(our_root), _spellings(their_root)))


def repoint_other_checkouts(settings: dict, event: str, command: str, matcher: str = "") -> list[str]:
    """Rewrite, in place, ``event`` hooks under ``matcher`` that are ``command`` on another checkout's path.

    A copy is rewritten to ``command`` where it stands; one beside an existing ``command`` under
    the same matcher is dropped instead. An entry that already IS ``command`` is preferred over
    every copy, in whichever order they sit, so its own timeout and async settings survive.
    Entries under any other matcher are not ours and stay. Returns the old commands.
    """
    def _hooks_under(matcher: str):
        for entry in settings.get("hooks", {}).get(event, []) or []:
            if isinstance(entry, dict) and entry.get("matcher", "") == matcher:
                for hook in entry.get("hooks", []) or []:
                    yield hook

    changed: list[str] = []
    # The current entry owns its settings, so a copy before it is dropped, never re-pointed
    # over it; without this the copy's own timeout would replace the current one's.
    exact = any(isinstance(h, dict) and h.get("command") == command for h in _hooks_under(matcher))
    kept = False
    kept_entries = []
    for entry in settings.get("hooks", {}).get(event, []) or []:
        if not isinstance(entry, dict) or entry.get("matcher", "") != matcher:
            kept_entries.append(entry)
            continue
        kept_hooks = []
        for hook in entry.get("hooks", []) or []:
            current = hook.get("command", "") if isinstance(hook, dict) else ""
            if isinstance(current, str) and is_other_checkout_copy(current, command):
                changed.append(current)
                if exact or kept:
                    continue
                hook = dict(hook, command=command)
            if isinstance(hook, dict) and hook.get("command") == command:
                if kept:
                    continue
                kept = True
            kept_hooks.append(hook)
        if kept_hooks:
            kept_entries.append(dict(entry, hooks=kept_hooks))
    if changed:
        settings.setdefault("hooks", {})[event] = kept_entries
    return changed


def install(
    settings_path: Path,
    *,
    event: str,
    command: str,
    matcher: str = "",
    prepend: bool = False,
) -> tuple[str, list[str]]:
    """Prune dead same-family entries, re-point copies from another checkout, then add ``command`` under ``matcher`` once.

    Returns (status, removed); status is "re-pointed" when an existing copy was rewritten.
    """
    settings = load(settings_path)
    # <project>/.claude/settings.json → the project is two levels up.
    project_dir = settings_path.resolve().parent.parent
    removed = prune_dead(settings, event, family_of(command), project_dir=project_dir)
    repointed = repoint_other_checkouts(settings, event, command, matcher)
    entries = settings.setdefault("hooks", {}).setdefault(event, [])
    present = any(
        isinstance(h, dict) and h.get("command", "") == command
        for entry in entries if isinstance(entry, dict) and entry.get("matcher", "") == matcher
        for h in entry.get("hooks", []) or []
    )
    if repointed:
        status = "re-pointed"
    elif present:
        status = "already installed"
    else:
        new_entry = {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
        if prepend:
            entries.insert(0, new_entry)
        else:
            entries.append(new_entry)
        status = "installed"
    if status == "installed" or removed or repointed or not settings_path.is_file():
        save(settings_path, settings)
    return status, removed


_REPO_TOP_DIRS = ("src", "scripts", "skills")


def _stale_root(token: str, repo: str) -> Optional[str]:
    """The old checkout root of a missing absolute path whose remainder exists under ``repo``."""
    if not token.startswith("/") or not _judgeable(token) or os.path.exists(token):
        return None
    parts = token.split("/")
    for index in range(2, len(parts)):
        if parts[index] not in _REPO_TOP_DIRS:
            continue
        root = "/".join(parts[:index])
        # A root still on disk is a live checkout missing one script (mid-rebase, other branch).
        if root != repo and not os.path.exists(root) and os.path.exists(os.path.join(repo, *parts[index:])):
            return root
    return None


def _spellings(text: str) -> list[str]:
    """How a path can appear in a stored command: bare, single-quoted (two escapes), double-quoted."""
    return [text, text.replace("'", "'\\''"), text.replace("'", "'\"'\"'"),
            re.sub(r'(["$`\\])', r"\\\1", text)]


def repoint_command(command: str, repo: str) -> Optional[str]:
    """``command`` with its one stale checkout root replaced by ``repo``, or None to leave it."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    roots = {r for r in (_stale_root(t, repo) for t in tokens) if r}
    if len(roots) != 1:
        return None
    root = roots.pop()
    want = [t.replace(root + "/", repo + "/") for t in tokens]
    for old, new in zip(_spellings(root), _spellings(repo)):
        candidate = command.replace(old + "/", new + "/")
        if candidate == command:
            continue
        try:
            if shlex.split(candidate) == want:
                return candidate
        except ValueError:
            continue
    return None


def repoint_moved(settings: dict, repo: str) -> list[tuple[str, str, str]]:
    """Re-point, in place, every hook left on a moved checkout's path. Returns (event, old, new)."""
    changed: list[tuple[str, str, str]] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return changed
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for hook in (entry.get("hooks") if isinstance(entry, dict) else None) or []:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    continue
                new = repoint_command(command, repo)
                if new is not None:
                    hook["command"] = new
                    changed.append((event, command, new))
    return changed


def _rewrite_in_place(text: str, changed: list[tuple[str, str, str]], settings: dict) -> str:
    """The file text with only the changed command strings swapped, or a fresh dump if that fails."""
    out = text
    for _event, old, new in changed:
        for ascii_only in (True, False):
            out = out.replace(json.dumps(old, ensure_ascii=ascii_only), json.dumps(new, ensure_ascii=ascii_only))
    if json.loads(out) == settings:
        return out
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def repair_paths(settings_path: Path, repo: Path) -> list[tuple[str, str, str]]:
    """Re-point a moved install's hooks in ``settings_path`` at ``repo``; writes only on a change."""
    if not settings_path.is_file():
        return []
    text = settings_path.read_text(encoding="utf-8")
    settings = json.loads(text)
    if not isinstance(settings, dict):
        raise ValueError(f"{settings_path}: top level is not an object")
    changed = repoint_moved(settings, str(repo))
    if changed:
        fd, tmp = tempfile.mkstemp(dir=str(settings_path.parent), prefix=settings_path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_rewrite_in_place(text, changed, settings))
            os.chmod(tmp, settings_path.stat().st_mode & 0o7777)
            os.replace(tmp, settings_path)
        except BaseException:
            os.unlink(tmp)
            raise
    return changed


def _describe_removed(removed: Iterable[str], family: str) -> str:
    items = list(removed)
    noun = "entry" if len(items) == 1 else "entries"
    paths = ", ".join(script_path_of(c) or c for c in items)
    return f"  ✂ removed {len(items)} dead {family} {noun}: {paths}"


def _main_repair(args: argparse.Namespace) -> int:
    rc = 0
    for path in args.settings:
        try:
            changed = repair_paths(Path(path), Path(args.repo))
        except (OSError, ValueError) as exc:
            print(f"  ✗ hook path repair: {exc}", file=sys.stderr)
            rc = 1
            continue
        for event, _old, new in changed:
            print(f"  ✓ re-pointed {event} hook in {path}: {new}")
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--settings", required=True)
    p.add_argument("--event", default="SessionStart")
    p.add_argument("--command", required=True)
    p.add_argument("--matcher", default="")
    p.add_argument("--prepend", action="store_true")
    p.add_argument("--label", default=None)
    r = sub.add_parser("repair-paths")
    r.add_argument("--repo", required=True)
    r.add_argument("--settings", action="append", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "repair-paths":
        return _main_repair(args)
    label = args.label or f"{family_of(args.command)} {args.event} hook"
    try:
        status, removed = install(
            Path(args.settings), event=args.event, command=args.command,
            matcher=args.matcher, prepend=args.prepend,
        )
    except (OSError, ValueError) as exc:
        print(f"  ✗ {label}: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(_describe_removed(removed, family_of(args.command)))
    print(f"  ✓ {label} ({status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
