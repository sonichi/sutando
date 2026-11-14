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
that is, or would become, the owner's `~/.codex`, judged both by its lexical path and by
directory identity. Nothing already at
`$CODEX_HOME/AGENTS.md` is removed or replaced.
"""
import argparse
import codecs
import errno
import os
import stat
import sys
import unicodedata
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


_OWNER_LINK_HOPS = 32


def _key(part):
    """A path component as a case- and normalization-insensitive filesystem (APFS) compares it."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", part).casefold())


def _owner_spellings(home):
    """Every path `~/.codex` names, created or not: `$HOME/.codex`, `realpath($HOME)/.codex`,
    and each target its symlink chain names, read textually; ValueError past 32 links, as
    Darwin's MAXSYMLINKS."""
    found = []
    for base in (os.fspath(home), os.path.realpath(home)):
        path = os.path.normpath(os.path.join(base, ".codex"))
        for _ in range(_OWNER_LINK_HOPS + 1):
            found += [path, os.path.realpath(path)]
            if not os.path.islink(path):
                break
            path = os.path.normpath(os.path.join(os.path.dirname(path), os.readlink(path)))
        else:
            raise ValueError("~/.codex: too many symlinks to follow")
    return found


def _lexically_owner(parts, home):
    """True when `parts`, compared by `_key`, are any spelling of `~/.codex`, below it or above it,
    or the spellings cannot be read. The walk follows no link, so the path it opens is this one."""
    folded = [_key(p) for p in parts]
    try:
        spellings = _owner_spellings(home)
    except (ValueError, OSError):
        return True
    for spelled in spellings:
        owner = [_key(p) for p in spelled.split(os.sep) if p not in ("", ".")]
        # Above too: any entry later made below `parts` could become the owner's home.
        if folded[:len(owner)] == owner or owner[:len(folded)] == folded:
            return True
    return False


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
    return [_key(p) for p in rest[:len(missing)]] == [_key(p) for p in missing]


def _owner_moved(home, anchor, missing):
    """True when ~/.codex's identity is no longer the one checked against, or cannot be read."""
    try:
        return _owner_anchor(home) != (anchor, missing)
    except OSError:
        return True


def _walk(parts, home, anchor, missing, create):
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
                if _owner_moved(home, anchor, missing):
                    return "owner-home", None
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
        if _lexically_owner(parts, home):
            return True
        anchor, missing = _owner_anchor(home)
        status, fd = _walk(parts, home, anchor, missing, create=False)
    except (ValueError, OSError):
        return True
    if fd is not None:
        os.close(fd)
    return status in ("owner-home", "invalid-home")


# Rust's char::is_whitespace (Unicode White_Space), which Codex uses to call a file blank.
_WHITE_SPACE = frozenset(map(chr, [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680,
                                   *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F,
                                   0x3000]))


def _active_override(fd):
    """Codex honours AGENTS.override.md only as a regular file it reads whole whose text is not
    blank; bytes that are not UTF-8 count as content. A read error anywhere means no override."""
    try:
        probe = os.open("AGENTS.override.md", os.O_RDONLY | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    content = False
    try:
        if not stat.S_ISREG(os.fstat(probe).st_mode):
            return False
        while True:
            chunk = os.read(probe, 65536)
            if not content:
                try:
                    text = decoder.decode(chunk, final=not chunk)
                except UnicodeDecodeError:
                    content = True
                else:
                    content = any(c not in _WHITE_SPACE for c in text)
            if not chunk:
                return content
    except OSError:
        return False
    finally:
        os.close(probe)


def check(codex_home, repo, home):
    """What ensure_linked would refuse, without creating or writing anything.

    Returns "no-host", "invalid-home", "owner-home" or "ok".
    """
    if host_instructions(repo) is None:
        return "no-host"
    try:
        parts = _parts(codex_home)
    except ValueError:
        return "invalid-home"
    if _lexically_owner(parts, home):
        return "owner-home"
    try:
        anchor, missing = _owner_anchor(home)
    except OSError:
        return "owner-home"
    status, fd = _walk(parts, home, anchor, missing, create=False)
    if fd is not None:
        os.close(fd)
    return status if status in ("owner-home", "invalid-home") else "ok"


def create_home(codex_home, home):
    """Create `codex_home` through the same no-follow walk as ensure_linked.

    Returns "ok", "invalid-home" or "owner-home"; nothing is followed, so a component swapped
    for a symlink since check() is refused instead of creating a directory elsewhere.
    """
    try:
        parts = _parts(codex_home)
    except ValueError:
        return "invalid-home"
    if _lexically_owner(parts, home):
        return "owner-home"
    try:
        anchor, missing = _owner_anchor(home)
    except OSError:
        return "owner-home"
    status, fd = _walk(parts, home, anchor, missing, create=True)
    if fd is not None:
        os.close(fd)
    return "ok" if status == "open" else status


def open_home(codex_home, home):
    """Open an existing `codex_home` through the no-follow walk; the caller closes the fd.

    Raises ValueError for an owner or invalid home and FileNotFoundError when it is absent.
    """
    parts = _parts(codex_home)
    if _lexically_owner(parts, home):
        raise ValueError(f"{codex_home}: is, or leads into, the owner's Codex home")
    try:
        anchor, missing = _owner_anchor(home)
    except OSError as exc:
        raise ValueError(f"{codex_home}: owner home unreadable ({exc})") from exc
    status, fd = _walk(parts, home, anchor, missing, create=False)
    if status == "open":
        return fd
    if status == "absent":
        raise FileNotFoundError(codex_home)
    raise ValueError(f"{codex_home}: {status}")


def ensure_child_dir(codex_home, name, home):
    """Create, if absent, the directory `name` directly in an existing `codex_home`.

    Returns "ok", or "invalid-child" when `name` is a symlink or not a directory; nothing is
    followed. Raises like open_home, ValueError for a `name` that is not one component, and
    ValueError when the child is, or would be, the owner's `~/.codex`.
    """
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"{name!r}: not a single path component")
    owned = ValueError(f"{codex_home}/{name}: is, or leads into, the owner's Codex home")
    if _lexically_owner(_parts(codex_home) + [name], home):
        raise owned
    fd = open_home(codex_home, home)
    try:
        try:
            anchor, missing = _owner_anchor(home)
        except OSError as exc:
            raise ValueError(f"{codex_home}: owner home unreadable ({exc})") from exc
        if _at_owner(fd, anchor, missing, [name]):
            raise owned
        try:
            os.mkdir(name, 0o755, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
        except OSError:
            return "invalid-child"
        try:
            if _at_owner(child, anchor, missing, []):
                raise owned
        finally:
            os.close(child)
        return "ok"
    finally:
        os.close(fd)


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
    if _lexically_owner(parts, home):
        return "owner-home"
    try:
        anchor, missing = _owner_anchor(home)
    except OSError:
        return "owner-home"
    status, fd = _walk(parts, home, anchor, missing, create=True)
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
        if _owner_moved(home, anchor, missing):
            return "owner-home"
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
    parser.add_argument("--check", action="store_true",
                        help="only say whether the home would be refused; create nothing")
    parser.add_argument("codex_home", type=Path)
    parser.add_argument("repo", type=Path)
    args = parser.parse_args(argv)
    action = check if args.check else ensure_linked
    try:
        print(action(args.codex_home, args.repo, Path.home()))
    except OSError as exc:
        print(f"codex host instructions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
