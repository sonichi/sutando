"""Record the Codex core's own checkout as a trusted project before launch.

Codex asks "Trust this folder?" the first time it opens a git repository and waits
for an answer. The core is headless, so that prompt holds every queued task until a
person notices it (2026-10-22: a PR checkout's Codex core sat on it while health
reported idle). `-c projects."<dir>".trust_level=...` does not answer it; only the
saved entry in `$CODEX_HOME/config.toml` does, so this writes that entry.

Only the launcher's own checkout is ever trusted, and an explicit choice already in
the file (trusted or untrusted) is left as it is.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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


def ensure_trusted(codex_home, working_dir, repo):
    """Returns "not-git", "foreign", "already-set" or "trusted".

    "foreign": the working directory is not inside the launcher's checkout `repo`;
    trusting an arbitrary configured directory is the operator's decision, not ours.
    """
    root = git_root(working_dir)
    if root is None:
        return "not-git"
    if root != Path(repo).resolve():
        return "foreign"
    config = Path(codex_home) / "config.toml"
    try:
        text = config.read_text()
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
    config.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.with_name(f"{config.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if config.exists():
            os.chmod(tmp, config.stat().st_mode & 0o777)
        os.replace(tmp, config)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return "trusted"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("codex_home", type=Path)
    parser.add_argument("working_dir", type=Path)
    parser.add_argument("repo", type=Path)
    args = parser.parse_args(argv)
    try:
        print(ensure_trusted(args.codex_home, args.working_dir, args.repo))
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        print(f"codex project trust: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
