"""Record the Codex core's own checkout as a trusted project before launch.

Codex asks "Trust this folder?" the first time it opens a git repository and waits
for an answer. The core is headless, so that prompt holds every queued task until a
person notices it (2026-10-22: a PR checkout's Codex core sat on it while health
reported idle). `-c projects."<dir>".trust_level=...` does not answer it; only the
saved entry in `$CODEX_HOME/config.toml` does, so this writes that entry.

Only the launcher's own checkout is ever trusted, and an explicit choice already in
the file (trusted or untrusted) is left as it is. The Codex home is the one the launcher
resolved; it is never created here.
"""
import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_host_instructions  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: the launcher may resolve the system 3.9
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        import toml_fallback as tomllib


def git_root(path):
    """The repository root containing `path`, or None outside a git repository."""
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return Path(out.stdout.strip()).resolve()


_CHECKOUT = Path(__file__).resolve().parent.parent


def ensure_trusted(codex_home, working_dir, repo, home=None):
    """Returns "not-git", "foreign", "already-set" or "trusted".

    "foreign": the working directory is not inside the launcher's checkout `repo`;
    trusting an arbitrary configured directory is the operator's decision, not ours.
    On a desktop host the config is read and written relative to a descriptor opened
    through the no-follow owner guard, so a swapped home is refused, never followed.
    """
    root = git_root(working_dir)
    if root is None:
        return "not-git"
    if root != Path(repo).resolve():
        return "foreign"
    return _record(codex_home, root, repo, home)


def ensure_folder_trusted(codex_home, folder, repo=None, home=None):
    """Returns "already-set" or "trusted": `folder` itself is recorded, never the repository holding it.

    A pool worker's folder sits inside the owner's workspace repository; Codex's prompt would trust that
    whole root, while a saved entry for the folder's own path answers it for the folder alone.
    `repo` (default: this checkout) decides, as for ensure_trusted, whether the home is guarded.
    """
    return _record(codex_home, Path(folder).resolve(), _CHECKOUT if repo is None else repo, home)


@contextlib.contextmanager
def _locked_home(codex_home, guarded, home):
    """An exclusively locked descriptor of the existing Codex home, which is never created.
    Seats launched together each read, then replace, the one config.toml; the lock serializes them."""
    if guarded:
        fd = codex_host_instructions.open_home(codex_home, Path.home() if home is None else home)
    else:
        fd = os.open(codex_home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _record(codex_home, root, repo, home):
    """On a desktop host the config is reached only below the owner-guarded descriptor."""
    guarded = codex_host_instructions.host_instructions(repo) is not None
    with _locked_home(codex_home, guarded, home) as fd:
        if guarded:
            return _trust("config.toml", fd, root)
        return _trust(Path(codex_home) / "config.toml", None, root)


def _trust(config, dir_fd, root):
    """Record `root` in `config` (a path, or a name below `dir_fd`); no link is followed
    under `dir_fd`."""
    nofollow = getattr(os, "O_NOFOLLOW", 0) if dir_fd is not None else 0
    try:
        fd = os.open(config, os.O_RDONLY | nofollow, dir_fd=dir_fd)
        with os.fdopen(fd) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    projects = tomllib.loads(text).get("projects", {})
    if isinstance(projects, dict) and str(root) in projects:
        return "already-set"
    if text and not text.endswith("\n"):
        text += "\n"
    # json.dumps quotes a TOML basic-string key the same way (backslashes, quotes).
    text += f'\n[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n'
    tomllib.loads(text)  # never write a file Codex could not parse
    tmp = f"{config}.tmp.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            mode = os.stat(config, dir_fd=dir_fd).st_mode & 0o777
        except FileNotFoundError:
            mode = None
        if mode is not None:
            os.chmod(tmp, mode, dir_fd=dir_fd)
        os.replace(tmp, config, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise
    return "trusted"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("codex_home", help="the resolved Codex home; never created here")
    parser.add_argument("working_dir", type=Path)
    parser.add_argument("repo", type=Path, nargs="?")
    parser.add_argument("--folder-only", action="store_true",
                        help="record working_dir itself, not the repository that holds it")
    args = parser.parse_args(argv)
    if args.repo is None and not args.folder_only:
        parser.error("repo is required unless --folder-only")
    try:
        if not args.codex_home:
            raise ValueError("no Codex home given")
        print(ensure_folder_trusted(Path(args.codex_home), args.working_dir) if args.folder_only
              else ensure_trusted(Path(args.codex_home), args.working_dir, args.repo))
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        print(f"codex project trust: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
