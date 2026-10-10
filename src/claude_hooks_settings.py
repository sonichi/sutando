#!/usr/bin/env python3
"""Remove the hook entries older Sutando installers wrote into settings files.

Sutando's hooks are registered only through the core's launch-time ``--settings``
JSON (src/agent/claude/cli/build-core-settings.mjs). Copies an earlier installer
left in a project ``.claude/settings.json`` fire in every session opened there,
and copies in the core's config dir fire again in the core, so both are swept.

An entry is removed only when its (event, matcher, command) is exactly a record an
installer wrote, for this checkout or for the moved checkout its missing script
path names. Anything else, an operator's edit included, stays.

    python3 src/claude_hooks_settings.py sweep --repo <repo> [--settings <file>]... [--dry-run] [--no-core-config]
    python3 src/claude_hooks_settings.py launch-check --repo <repo> < <launch-settings.json>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import stat
import sys
import tempfile
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
# A retired Stop hook that killed the live task watcher at every turn end.
_WATCHER_KILL_STOP = ('PID_FILE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/state/watch-tasks-stream.pid"; '
                      'if [ -f "$PID_FILE" ]; then PID=$(cat "$PID_FILE" 2>/dev/null); '
                      '[ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -f "$PID_FILE"; fi; exit 0')
OWNED_HOOKS_TABLE = Path(__file__).resolve().parent / "agent" / "claude" / "cli" / "owned-hooks.json"

Record = tuple[str, str, str]


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


def emitted_records(repo: Path) -> set[Record]:
    """Every (event, matcher, command) a Sutando installer has written for the checkout at ``repo``."""
    # An installer saw the checkout through whatever path it was run by, symlinked or not.
    out: set[Record] = set()
    for root in {Path(repo), Path(repo).resolve()}:
        out |= _emitted_for(root)
    return out


def _emitted_for(repo: Path, with_skills: bool = True) -> set[Record]:
    src = Path(repo) / "src"

    def sq(name: str) -> str:
        return _shq(str(src / name))

    def dq(name: str) -> str:
        return f'"{src / name}"'

    out = {
        ("Stop", "", f"bash {sq('check-pending-tasks.sh')}"),
        ("Stop", "", f"bash {dq('check-pending-tasks.sh')}"),
        ("Stop", "", "bash $HOME/Desktop/sutando/src/check-pending-tasks.sh"),
        ("Stop", "", _WATCHER_KILL_STOP),
        ("UserPromptSubmit", "", f"bash {sq('turn-start.sh')}"),
        ("PreCompact", "", f"bash {sq('archive-transcript.sh')} \"$HOME/Desktop/sutando-conversations/\""),
        ("PreCompact", "", _DESKTOP_ARCHIVE_CP),
        ("SessionStart", "", f"bash {dq('schedule-crons-session-hint.sh')}"),
        ("SessionStart", "compact", f"bash {dq('personal-claude-compact-hint.sh')}"),
        ("SessionStart", "compact|resume", f"bash {dq('watcher-rearm-session-hint.sh')}"),
        ("SessionEnd", "", f"bash {dq('session-handoff.sh')} \"${{TRANSCRIPT_PATH:-}}\""),
    }
    for event in ("PreCompact", "SessionEnd"):
        out.add((event, "", f"bash {sq('session-handoff.sh')} \"$TRANSCRIPT_PATH\""))
        out.add((event, "", f"bash {dq('session-handoff.sh')} \"$TRANSCRIPT_PATH\""))
        out.add((event, "", "bash $HOME/Desktop/sutando/src/session-handoff.sh \"$TRANSCRIPT_PATH\""))
    if with_skills:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from skill_hooks import discover
        for event, _token, command, prior in discover(Path(repo)):
            out.add((event, "", command))
            out.add((event, "", prior))
    return out


def owned_launch_records(repo: Path) -> set[Record]:
    """The lifecycle hooks build-core-settings.mjs registers for ``repo``, from the shared table."""
    rows = json.loads(OWNED_HOOKS_TABLE.read_text(encoding="utf-8"))
    return {(event, matcher, f"bash {_shq(os.path.normpath(os.path.join(str(repo), 'src', script)))}{args}")
            for event, matcher, script, args in rows}


def skill_launch_records(repo: Path) -> set[Record]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from skill_hooks import discover
    return {(event, "", command) for event, _t, command, _p in discover(Path(repo))}


def launch_records(repo: Path) -> set[Record]:
    """What the core's launch settings must carry before any settings-file copy may be swept."""
    return owned_launch_records(repo) | skill_launch_records(repo)


def records_in(settings: dict) -> set[Record]:
    """Every (event, matcher, command) a settings object registers."""
    out: set[Record] = set()
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for event, groups in (hooks.items() if isinstance(hooks, dict) else ()):
        for group in groups if isinstance(groups, list) else ():
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for hook in group["hooks"]:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    out.add((event, _matcher(group), hook["command"]))
    return out


def _matcher(group: dict) -> str:
    m = group.get("matcher", "")
    return m if isinstance(m, str) else repr(m)


def _shq(s: str) -> str:
    """The installer's shq(): always single-quoted, `'` written as `'\\''`."""
    return "'" + s.replace("'", "'\\''") + "'"


def _is_moved_checkout_copy(record: Record) -> bool:
    """Exactly a record an installer wrote for another checkout, whose script path is now gone."""
    path = script_path_of(record[2])
    if not path or "$" in path or not os.path.isabs(path) or os.path.exists(path):
        return False
    script = Path(path)
    if script.name not in _OWNED_SCRIPTS or script.parent.name != "src":
        return False
    return record in _emitted_for(script.parent.parent, with_skills=False)


def sweep(settings: dict, owned: set[Record]) -> list[tuple[str, str]]:
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
                record = (event, _matcher(entry), command)
                if isinstance(command, str) and (record in owned or _is_moved_checkout_copy(record)):
                    removed.append((event, command))
                    continue
                kept.append(hook)
            if kept:
                kept_entries.append(dict(entry, hooks=kept))
            elif not entry["hooks"]:
                kept_entries.append(entry)
        hooks[event] = kept_entries
    return removed


def write_json_keeping_mode(path: Path, data: dict) -> None:
    """Atomically replace ``path``; an existing file keeps its mode, a new one is 0600."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def sweep_file(path: Path, owned: set[Record], dry_run: bool = False) -> list[tuple[str, str]]:
    """Sweep one settings file; a missing file is a no-op and is never created."""
    if not path.is_file():
        return []
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"{path}: top level is not an object")
    removed = sweep(settings, owned)
    if removed and not dry_run:
        write_json_keeping_mode(path, settings)
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


def launch_check(repo: Path, settings_text: str) -> list[Record]:
    """The launch records ``settings_text`` lacks; raises when it is not a settings object."""
    settings = json.loads(settings_text)
    if not isinstance(settings, dict):
        raise ValueError("launch settings are not a JSON object")
    return sorted(launch_records(repo) - records_in(settings))


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
    c = sub.add_parser("launch-check", help="exit 0 only if the launch settings on stdin carry every Sutando hook")
    c.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    if args.cmd == "launch-check":
        try:
            missing = launch_check(repo, sys.stdin.read())
        except Exception as exc:  # noqa: BLE001 — any doubt means the copies are kept
            print(f"claude-hooks launch-check: {exc}", file=sys.stderr)
            return 1
        for event, matcher, command in missing:
            print(f"claude-hooks launch-check: missing {event} [{matcher}] {command!r}", file=sys.stderr)
        return 1 if missing else 0
    owned = emitted_records(repo)
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
