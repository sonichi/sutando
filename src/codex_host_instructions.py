"""Give a Codex core the instructions its desktop host ships beside the checkout.

The AG2 Space desktop puts the checkout at `<app-support>/engine/<checkout>` and its own
instructions (first contact, the Getting started page) in `<app-support>/engine/AGENTS.md`.
Claude Code reads every CLAUDE.md above its working directory, so a Claude core sees the
host's copy. Codex reads AGENTS.md only from its project root down, so a Codex core never
sees the host file. Codex does read `$CODEX_HOME/AGENTS.md` for every session, so this
links the host file there.

It only ever writes into the app-private Codex home the launcher made: every existing
component of CODEX_HOME must be a real directory (no symlink anywhere, no `..`), each is
opened without following links, and the walk stops before entering or creating anything
that is, or would become, the owner's `~/.codex`. Nothing already at
`$CODEX_HOME/AGENTS.md` is removed or replaced.
"""
import argparse
import errno
import os
import stat
import sys
from pathlib import Path


def host_instructions(repo):
    """The host's AGENTS.md beside a desktop-hosted checkout, or None."""
    parent = Path(repo).resolve().parent
    if parent.name != "engine":
        return None
    path = parent / "AGENTS.md"
    return path if path.is_file() else None


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _parts(codex_home):
    """The components of an absolute, `..`-free CODEX_HOME; ValueError otherwise."""
    raw = os.path.expanduser(os.fspath(codex_home))
    if not os.path.isabs(raw):
        raise ValueError(f"{raw}: not an absolute path")
    parts = [p for p in raw.split(os.sep) if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"{raw}: contains '..'")
    return parts


def _owner_anchor(home):
    """The identity of `~/.codex`, or of its nearest existing ancestor plus the missing parts.

    Raises OSError when an identity cannot be read; callers treat that as the owner's home.
    """
    path, missing = Path(home) / ".codex", []
    while True:
        try:
            st = os.stat(path)
            return (st.st_dev, st.st_ino), missing
        except FileNotFoundError:
            if path == path.parent:
                raise
            missing.insert(0, path.name)
            path = path.parent


def _at_owner(fd, anchor, missing, rest):
    """True when the directory open at `fd`, with `rest` still below it, is or would be ~/.codex."""
    try:
        st = os.fstat(fd)
    except OSError:
        return True
    if (st.st_dev, st.st_ino) != anchor:
        return False
    return [p.casefold() for p in rest[:len(missing)]] == [p.casefold() for p in missing]


def _walk(parts, anchor, missing, create):
    """Descend from `/` one real directory at a time; returns (status, fd or None).

    status is "open", "owner-home", "invalid-home" (a symlink or non-directory component)
    or, without `create`, "absent" at the first missing component.
    """
    fd = os.open(os.sep, _DIR_FLAGS)
    try:
        for i in range(len(parts) + 1):
            if _at_owner(fd, anchor, missing, parts[i:]):
                return "owner-home", None
            if i == len(parts):
                opened, fd = fd, None
                return "open", opened
            try:
                child = os.open(parts[i], _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return "absent", None
                try:
                    os.mkdir(parts[i], dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(parts[i], _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return "invalid-home", None
        raise
    finally:
        if fd is not None:
            os.close(fd)


def is_owner_codex_home(codex_home, home):
    """True when `codex_home` is, or would be created as, `~/.codex` or a path in it, or
    cannot be told apart from one (not absolute, `..`, a symlink, an unreadable identity)."""
    try:
        parts = _parts(codex_home)
        anchor, missing = _owner_anchor(home)
        status, fd = _walk(parts, anchor, missing, create=False)
    except (ValueError, OSError):
        return True
    if fd is not None:
        os.close(fd)
    return status in ("owner-home", "invalid-home")


def _active_override(fd):
    """Codex honours AGENTS.override.md only as a regular file it can open; else AGENTS.md."""
    try:
        probe = os.open("AGENTS.override.md", os.O_RDONLY | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(probe).st_mode)
    except OSError:
        return False
    finally:
        os.close(probe)


def ensure_linked(codex_home, repo, home):
    """Returns "no-host", "invalid-home", "owner-home", "overridden", "foreign", "dangling",
    "already-set" or "linked".

    Never removes or replaces anything already at the target: only a missing target is linked.
    """
    source = host_instructions(repo)
    if source is None:
        return "no-host"
    try:
        parts = _parts(codex_home)
    except ValueError:
        return "invalid-home"
    try:
        anchor, missing = _owner_anchor(home)
    except OSError:
        return "owner-home"
    status, fd = _walk(parts, anchor, missing, create=True)
    if fd is None:
        return status
    try:
        if _active_override(fd):
            return "overridden"
        try:
            mode = os.stat("AGENTS.md", dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None:
            if not stat.S_ISLNK(mode):
                return "foreign"
            if Path(os.readlink("AGENTS.md", dir_fd=fd)) == source:
                return "already-set"
            try:
                os.stat("AGENTS.md", dir_fd=fd)
            except OSError:
                return "dangling"
            return "foreign"
        try:
            os.symlink(source, "AGENTS.md", dir_fd=fd)
        except FileExistsError:
            try:
                same = Path(os.readlink("AGENTS.md", dir_fd=fd)) == source
            except OSError:
                same = False
            return "already-set" if same else "foreign"
        return "linked"
    finally:
        os.close(fd)


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
