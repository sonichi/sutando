#!/usr/bin/env python3
"""Remove the hook entries older Sutando installers wrote into settings files.

Sutando's hooks are registered only through the core's launch-time ``--settings``
JSON (src/agent/claude/cli/build-core-settings.mjs). Copies an earlier installer
left in a project ``.claude/settings.json`` fire in every session opened there,
and copies in the core's config dir fire again in the core, so both are swept.

An entry is removed only when it is byte-identical to a command an installer
emitted for this checkout under that event, or when it runs one of those scripts
from a path that no longer exists. Anything else, an operator's edit included,
stays.

    python3 src/claude_hooks_settings.py sweep --repo <repo> [--settings <file>]... [--dry-run] [--no-core-config]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Optional

_INTERPRETERS = {"bash", "sh", "zsh", "python", "python3", "node"}

# Frozen history, not the live table: these must keep matching what was written
# even after the builder's own strings change.
_OWNED_SCRIPTS = (
    "check-pending-tasks.sh", "turn-start.sh", "session-handoff.sh", "archive-transcript.sh",
    "schedule-crons-session-hint.sh", "personal-claude-compact-hint.sh",
    "watcher-rearm-session-hint.sh",
)
_DESKTOP_ARCHIVE_CP = ('cp "$TRANSCRIPT_PATH" '
                       '"$HOME/Desktop/sutando-conversations/$(date +%Y-%m-%dT%H-%M-%S).jsonl"')


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


def emitted_commands(repo: Path) -> set[tuple[str, str]]:
    """Every (event, command) a Sutando installer has written for the checkout at ``repo``."""
    # An installer saw the checkout through whatever path it was run by, symlinked or not.
    out: set[tuple[str, str]] = set()
    for root in {Path(repo), Path(repo).resolve()}:
        out |= _emitted_for(root)
    return out


def _emitted_for(repo: Path) -> set[tuple[str, str]]:
    src = Path(repo) / "src"

    def sq(name: str) -> str:
        return _shq(str(src / name))

    def dq(name: str) -> str:
        return f'"{src / name}"'

    out = {
        ("Stop", f"bash {sq('check-pending-tasks.sh')}"),
        ("Stop", f"bash {dq('check-pending-tasks.sh')}"),
        ("Stop", "bash $HOME/Desktop/sutando/src/check-pending-tasks.sh"),
        ("UserPromptSubmit", f"bash {sq('turn-start.sh')}"),
        ("PreCompact", f"bash {sq('archive-transcript.sh')} \"$HOME/Desktop/sutando-conversations/\""),
        ("PreCompact", _DESKTOP_ARCHIVE_CP),
        ("SessionStart", f"bash {dq('schedule-crons-session-hint.sh')}"),
        ("SessionStart", f"bash {dq('personal-claude-compact-hint.sh')}"),
        ("SessionStart", f"bash {dq('watcher-rearm-session-hint.sh')}"),
        ("SessionEnd", f"bash {dq('session-handoff.sh')} \"${{TRANSCRIPT_PATH:-}}\""),
    }
    for event in ("PreCompact", "SessionEnd"):
        out.add((event, f"bash {sq('session-handoff.sh')} \"$TRANSCRIPT_PATH\""))
        out.add((event, f"bash {dq('session-handoff.sh')} \"$TRANSCRIPT_PATH\""))
        out.add((event, "bash $HOME/Desktop/sutando/src/session-handoff.sh \"$TRANSCRIPT_PATH\""))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from skill_hooks import discover
    for event, _token, command, prior in discover(Path(repo)):
        out.add((event, command))
        out.add((event, prior))
    return out


def _shq(s: str) -> str:
    """The installer's shq(): always single-quoted, `'` written as `'\\''`."""
    return "'" + s.replace("'", "'\\''") + "'"


def _is_dead_owned_copy(command: str) -> bool:
    """Runs one of our scripts from a path that no longer exists (and carries no variable)."""
    path = script_path_of(command)
    if not path or os.path.basename(path) not in _OWNED_SCRIPTS or "$" in path:
        return False
    return os.path.isabs(path) and not os.path.exists(path)


def sweep(settings: dict, owned: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop owned entries from ``settings`` in place; returns the (event, command) pairs removed."""
    removed: list[tuple[str, str]] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return removed
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        kept_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            kept = []
            for hook in entry["hooks"]:
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str) and ((event, command) in owned
                                                 or _is_dead_owned_copy(command)):
                    removed.append((event, command))
                    continue
                kept.append(hook)
            if kept:
                kept_entries.append(dict(entry, hooks=kept))
            elif not entry["hooks"]:
                kept_entries.append(entry)
        hooks[event] = kept_entries
    return removed


def sweep_file(path: Path, owned: set[tuple[str, str]], dry_run: bool = False) -> list[tuple[str, str]]:
    """Sweep one settings file; a missing file is a no-op and is never created."""
    if not path.is_file():
        return []
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"{path}: top level is not an object")
    removed = sweep(settings, owned)
    if removed and not dry_run:
        tmp = path.with_name(path.name + ".sweep.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return removed


def default_targets(repo: Path) -> list[Path]:
    """The project settings files Sutando installers wrote: the repo's and the core working dir's."""
    targets = [Path(repo) / ".claude" / "settings.json"]
    working = os.environ.get("SUTANDO_CLAUDE_WORKING_DIR", "")
    if working:
        wd = Path(os.path.expanduser(working)) / ".claude" / "settings.json"
        if wd.resolve() != targets[0].resolve():
            targets.append(wd)
    return targets


def core_config_settings(repo: Path) -> Optional[Path]:
    """<core CLAUDE_CONFIG_DIR>/settings.json for ``repo``, or None when it cannot be resolved."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from sutando_config import resolve_claude_sutando_config_dir
        return Path(resolve_claude_sutando_config_dir(Path(repo))) / "settings.json"
    except Exception:  # noqa: BLE001 — an unresolvable config dir is skipped, not fatal
        return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sweep")
    p.add_argument("--repo", required=True)
    p.add_argument("--settings", action="append", default=[],
                   help="extra settings file to sweep (repeatable)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-core-config", action="store_true",
                   help="skip the core CLAUDE_CONFIG_DIR settings.json")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    owned = emitted_commands(repo)
    rc = 0
    total = 0
    targets = default_targets(repo) + [Path(s) for s in args.settings]
    ccd = core_config_settings(repo) if not args.no_core_config else None
    if ccd is not None:
        targets.append(ccd)
    for target in targets:
        try:
            removed = sweep_file(target, owned, dry_run=args.dry_run)
        except (OSError, ValueError) as exc:
            print(f"claude-hooks sweep: {target}: {exc} — left untouched", file=sys.stderr)
            rc = 1
            continue
        verb = "would remove" if args.dry_run else "removed"
        for event, command in removed:
            print(f"claude-hooks sweep: {verb} {event} {command!r} from {target}")
        total += len(removed)
    print(f"claude-hooks sweep: {total} owned entr{'y' if total == 1 else 'ies'} "
          f"{'found' if args.dry_run else 'removed'}; Sutando hooks register only at core launch")
    return rc


if __name__ == "__main__":
    sys.exit(main())
