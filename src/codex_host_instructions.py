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


def _nearest_existing(path):
    """The nearest existing ancestor of an absolute, `..`-free `path` and the parts below it."""
    path, rest = Path(path), []
    while not os.path.lexists(path) and path != path.parent:
        rest.insert(0, path.name)
        path = path.parent
    return path, rest


def canonical(path):
    """`path` as the OS resolves it: symlinks before `..`, then the missing rest appended.

    Raises ValueError when the missing rest holds `..`, which has no physical meaning yet.
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)  # joined, never normalized
    parts = raw.split(os.sep)
    for cut in range(len(parts), 0, -1):
        prefix = os.sep.join(parts[:cut]) or os.sep
        if os.path.lexists(prefix):
            break
    rest = [p for p in parts[cut:] if p not in ("", ".")]
    if ".." in rest:
        raise ValueError(f"{raw}: '..' below a directory that does not exist")
    return Path(os.path.realpath(prefix)).joinpath(*rest)


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_owner_codex_home(codex_home, home):
    """True when `codex_home` is, or would be created as, the owner's `~/.codex` or a path in it.

    Compared on the physical path by filesystem identity, so an alias or case variant counts.
    """
    owner = Path(home) / ".codex"
    try:
        path = canonical(codex_home)
    except ValueError:
        return True
    for candidate in (path, *path.parents):
        if os.path.exists(candidate) and _same(candidate, owner):
            return True
    have, rest = _nearest_existing(path)
    base, missing = _nearest_existing(canonical(owner))
    folded = [p.casefold() for p in rest[:len(missing)]]
    return _same(have, base) and folded == [p.casefold() for p in missing]


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _fd_path(fd):
    """Where an open directory is now, or None when the platform cannot say."""
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    except OSError:
        return None
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _open_home(path):
    """Open `path` without following a symlink at any step below its existing canonical base."""
    have, rest = _nearest_existing(path)
    fd = os.open(have, _DIR_FLAGS)
    try:
        for part in rest:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lexists(name, fd):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def ensure_linked(codex_home, repo, home):
    """Returns "no-host", "invalid-home", "owner-home", "overridden", "foreign", "dangling",
    "already-set" or "linked".

    Never removes or replaces anything already at the target: only a missing target is linked.
    """
    source = host_instructions(repo)
    if source is None:
        return "no-host"
    try:
        path = canonical(os.path.expanduser(os.fspath(codex_home)))
    except ValueError:
        return "invalid-home"
    if is_owner_codex_home(path, home):
        return "owner-home"
    try:
        fd = _open_home(path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return "foreign"  # a component turned into a symlink after the check
        raise
    try:
        # Re-check the directory actually opened; its path may differ from the one checked.
        where = _fd_path(fd)
        if is_owner_codex_home(where or path, home):
            return "owner-home"
        if where is None and not os.path.samestat(os.fstat(fd), os.stat(path)):
            return "foreign"
        # Codex reads an AGENTS.override.md in place of AGENTS.md; the override is the owner's.
        if _lexists("AGENTS.override.md", fd):
            return "overridden"
        if _lexists("AGENTS.md", fd):
            if not stat.S_ISLNK(os.stat("AGENTS.md", dir_fd=fd, follow_symlinks=False).st_mode):
                return "foreign"
            if Path(os.readlink("AGENTS.md", dir_fd=fd)) == source:
                return "already-set"
            try:
                os.stat("AGENTS.md", dir_fd=fd)
            except FileNotFoundError:
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
