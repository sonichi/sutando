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


_MAX_LINKS = 40


def _resolve_strict(path):
    """Resolve an existing absolute path one component at a time, as the OS traverses it.

    Raises OSError where the OS would: a missing or non-directory component before `..` or
    another component, or too many links. Unlike realpath, nothing is collapsed lexically.
    """
    todo, resolved, links = path.split(os.sep)[1:], os.sep, 0
    while todo:
        part = todo.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            resolved = os.path.dirname(resolved)
            continue
        step = os.path.join(resolved, part)
        mode = os.lstat(step).st_mode
        if stat.S_ISLNK(mode):
            links += 1
            if links > _MAX_LINKS:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", step)
            target = os.readlink(step)
            if os.path.isabs(target):
                resolved = os.sep
            todo[:0] = target.split(os.sep)
            continue
        if any(p not in ("", ".") for p in todo) and not stat.S_ISDIR(mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", step)
        resolved = step
    return resolved


def canonical(path):
    """`path` as the OS resolves it: symlinks before `..`, then the missing rest appended.

    Raises ValueError for a relative path or a `..` in the missing rest, and OSError when the
    existing part cannot be traversed (a missing or non-directory component, a link loop).
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raise ValueError(f"{raw}: not an absolute path")
    parts = raw.split(os.sep)
    for cut in range(len(parts), 0, -1):
        prefix = os.sep.join(parts[:cut]) or os.sep
        if os.path.lexists(prefix):
            break
    rest = [p for p in parts[cut:] if p not in ("", ".")]
    if ".." in rest:
        raise ValueError(f"{raw}: '..' below a directory that does not exist")
    resolved = _resolve_strict(prefix)
    if rest and not stat.S_ISDIR(os.stat(resolved).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", resolved)
    return Path(resolved).joinpath(*rest)


def _maybe_same(candidate, owner):
    """True when `candidate` is `owner`, or when that cannot be told; False only when known not."""
    try:
        cst = os.stat(candidate)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        ost = os.stat(owner)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return os.path.samestat(cst, ost)


def is_owner_codex_home(codex_home, home):
    """True when `codex_home` is, or would be created as, the owner's `~/.codex` or a path in it.

    Compared on the physical path by filesystem identity, so an alias or case variant counts;
    a path that cannot be resolved, or an identity that cannot be read, counts too.
    """
    owner = Path(home) / ".codex"
    try:
        path = canonical(codex_home)
        for candidate in (path, *path.parents):
            if _maybe_same(candidate, owner):
                return True
        have, rest = _nearest_existing(path)
        base, missing = _nearest_existing(canonical(owner))
    except (ValueError, OSError):
        return True
    folded = [p.casefold() for p in rest[:len(missing)]]
    return folded == [p.casefold() for p in missing] and _maybe_same(have, base)


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


def _active_override(fd):
    """Codex honours AGENTS.override.md only as a regular file, or a link to one."""
    try:
        return stat.S_ISREG(os.stat("AGENTS.override.md", dir_fd=fd).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True  # unreadable: assume the owner's override is there


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
    except (ValueError, OSError):
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
        if _active_override(fd):
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
