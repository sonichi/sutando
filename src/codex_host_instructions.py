"""Give a Codex core the instructions its desktop host ships beside the checkout.

The AG2 Space desktop puts the checkout at `<app-support>/engine/<checkout>` and its own
instructions (first contact, the Getting started page) in `<app-support>/engine/AGENTS.md`.
Claude Code reads every CLAUDE.md above its working directory, so a Claude core sees the
host's copy. Codex reads AGENTS.md only from its project root down, so a Codex core never
sees the host file. Codex does read `$CODEX_HOME/AGENTS.md` for every session, so this
links the host file there.

The desktop gives the core its own Codex home. The owner's `~/.codex` is never written,
recognised by filesystem identity, and nothing already at `$CODEX_HOME/AGENTS.md` (a file,
another link, a dangling link) or an AGENTS.override.md is removed or replaced.
"""
import argparse
import os
import sys
from pathlib import Path


def host_instructions(repo):
    """The host's AGENTS.md beside a desktop-hosted checkout, or None."""
    parent = Path(repo).resolve().parent
    if parent.name != "engine":
        return None
    path = parent / "AGENTS.md"
    return path if path.is_file() else None


def _nearest_existing(path):
    """The nearest existing ancestor of `path` (itself included) and the parts below it."""
    path, rest = Path(os.path.abspath(path)), []
    while not os.path.lexists(path) and path != path.parent:
        rest.insert(0, path.name)
        path = path.parent
    return path, rest


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_owner_codex_home(codex_home, home):
    """True when `codex_home` is, or would be created as, the owner's `~/.codex` or a path in it.

    Compared by filesystem identity, so a case-variant or symlinked spelling is caught too.
    """
    owner = Path(home) / ".codex"
    path = Path(os.path.abspath(codex_home))
    for candidate in (path, *path.parents):
        if os.path.exists(candidate) and _same(candidate, owner):
            return True
    have, rest = _nearest_existing(path)
    base, missing = _nearest_existing(owner)
    folded = [p.casefold() for p in rest[:len(missing)]]
    return _same(have, base) and folded == [p.casefold() for p in missing]


def ensure_linked(codex_home, repo, home):
    """Returns "no-host", "owner-home", "overridden", "foreign", "dangling", "already-set" or "linked".

    Never removes or replaces anything already at the target: only a missing target is linked.
    """
    source = host_instructions(repo)
    if source is None:
        return "no-host"
    codex_home = Path(codex_home).expanduser()
    if is_owner_codex_home(codex_home, home):
        return "owner-home"
    # Codex reads an AGENTS.override.md in place of AGENTS.md; the override is the owner's.
    if (codex_home / "AGENTS.override.md").exists():
        return "overridden"
    target = codex_home / "AGENTS.md"
    if target.is_symlink():
        if Path(os.readlink(target)) == source:
            return "already-set"
        return "foreign" if target.exists() else "dangling"
    if target.exists():
        return "foreign"
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source)
    except FileExistsError:
        return "already-set" if target.is_symlink() and Path(os.readlink(target)) == source else "foreign"
    return "linked"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("codex_home", type=Path)
    parser.add_argument("repo", type=Path)
    args = parser.parse_args(argv)
    try:
        print(ensure_linked(args.codex_home, args.repo, Path.home()))
    except OSError as exc:
        print(f"codex host instructions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
