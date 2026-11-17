#!/usr/bin/env python3
"""Discovery for skill-declared Claude Code hooks (`hooks` in a skill manifest).

One owner: the installer registers what this returns and the health probe
verifies exactly that, so a drifted second copy cannot make them disagree.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

# Legacy shapes are joined with this in the 4th field so the installer's sweep can
# match every form an earlier revision wrote (a path may hold any byte but NUL).
LEGACY_SEP = "\x1e"


def python_runner_prefix(repo_dir: Path) -> str:
    """Shell that resolves the interpreter at EVENT time through the one owner of that policy,
    scripts/python-binary.sh (SUTANDO_PY only if executable, else the bundled interpreter beside
    the engine, else a PATH python3 that is not Apple's stub). A stale SUTANDO_PY or a tmux window
    spawned without one therefore cannot reach a broken PATH python. No runnable interpreter fails
    OPEN (exit 0), like the existence guard: a hook that cannot start must not block the tool."""
    repo = Path(repo_dir).resolve()
    q_resolver = shlex.quote(str(repo / "scripts" / "python-binary.sh"))
    q_repo = shlex.quote(str(repo))
    return (f". {q_resolver} 2>/dev/null || exit 0; "
            f"_py=\"$(resolve_python {q_repo})\"; [ -n \"$_py\" ] || exit 0; ")


def resolve_hook_command(skill_dir: Path, command: str) -> Path | None:
    """Resolved hook path, or None when it lands outside the declaring skill.

    An absolute command needs no `..` to escape: `skill_dir / "/bin/sh"` is
    `/bin/sh`, which would let a manifest point core at any host executable.
    """
    if not command or Path(command).is_absolute():
        return None
    root = Path(skill_dir).resolve()
    target = (root / command).resolve()
    return target if root in target.parents else None


def discover(repo_dir: Path) -> list[tuple[str, str, str, str]]:
    """(event, token, command, legacy_commands) per declared, present, enabled hook.
    legacy_commands joins with LEGACY_SEP every shape an earlier revision wrote for this
    hook, emitted (not derived by splitting on `exec `) so the installer's sweep replaces them."""
    out: list[tuple[str, str, str, str]] = []
    for manifest in sorted(Path(repo_dir).glob("skills/*/manifest.json")):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("enabled") is False:
            continue
        entries = data.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            event, command = entry.get("event"), entry.get("command")
            if not isinstance(event, str) or not isinstance(command, str):
                continue
            target = resolve_hook_command(manifest.parent, command)
            if target is None or not target.is_file():
                continue
            q = shlex.quote(str(target))
            # The path is in the working tree, so a checkout can delete it while the
            # registration survives; a hook that cannot start blocks the tool it gates.
            if target.suffix == ".py":
                cmd = f"[ -f {q} ] || exit 0; {python_runner_prefix(repo_dir)}exec \"$_py\" {q}"
                bare = "python3"
            else:
                cmd = f"[ -f {q} ] || exit 0; exec bash {q}"
                bare = "bash"
            legacy = [f"{bare} {q}", f"[ -f {q} ] || exit 0; exec {bare} {q}"]
            if bare == "python3":
                # The one-revision shape that expanded SUTANDO_PY unvalidated (never released).
                bundled = Path(repo_dir).resolve().parent / "runtime" / "python" / "bin" / "python3"
                for fb in ("python3", str(bundled)):
                    legacy.append(f'[ -f {q} ] || exit 0; exec "${{SUTANDO_PY:-{fb}}}" {q}')
            out.append((event, target.name, cmd, LEGACY_SEP.join(legacy)))
    return out


if __name__ == "__main__":
    import sys
    # NUL-framed: two fields carry a repo path, and a path may contain any byte
    # except NUL — including the `|` the reader would otherwise split on.
    out = sys.stdout.buffer
    for row in discover(Path(sys.argv[1])):
        for field in row:
            out.write(field.encode() + b"\0")
    out.flush()
