"""Give a Codex core the instructions its desktop host ships beside the checkout.

The AG2 Space desktop puts the checkout at `<app-support>/engine/<checkout>` and its own
instructions (first contact, the Getting started page) in `<app-support>/engine/AGENTS.md`.
Claude Code reads every CLAUDE.md above its working directory, so a Claude core sees the
host's copy. Codex reads AGENTS.md only from its project root down, so a Codex core never
sees the host file. Codex does read `$CODEX_HOME/AGENTS.md` for every session, so this
links the host file there.

The desktop gives the core its own Codex home. The owner's `~/.codex` is never written,
and an AGENTS.md already in the home that is not this link is left as it is.
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


def ensure_linked(codex_home, repo, home):
    """Returns "no-host", "owner-home", "foreign", "already-set" or "linked"."""
    source = host_instructions(repo)
    if source is None:
        return "no-host"
    codex_home = Path(codex_home).expanduser()
    if codex_home.resolve() == (Path(home) / ".codex").resolve():
        return "owner-home"
    target = codex_home / "AGENTS.md"
    if target.is_symlink():
        if Path(os.readlink(target)) == source:
            return "already-set"
        if target.exists():
            return "foreign"
        target.unlink()  # a dangling link left by a moved install
    elif target.exists():
        return "foreign"
    codex_home.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)
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
