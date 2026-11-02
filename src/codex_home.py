"""Resolve, check and create the Codex core's home once, for every launcher that uses it.

The configured home is the `core_config_dirs` entry of type `codex`. On a checkout that is
not desktop-hosted it is created as it always was, and the effective home is the one Codex
reads (`$CODEX_HOME`, else `~/.codex`). On a desktop host it must be named `CODEX_HOME`,
the only name Codex reads, and `codex_host_instructions.check` must answer `ok` or
`no-host` before it is created; anything else, including a check that fails, refuses it,
and then no later step may write there.

Shell adapters call `prepare <repo>`, which prints three lines: the env name, the configured
value, and the effective home (empty when refused). Exit 0 when the home may be used, 3
when it was refused, 1 when the configuration cannot be read.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_host_instructions  # noqa: E402

PERMITTED = ("ok", "no-host")


class Refused(Exception):
    """The configured home must not be created or written; the message says why."""


def configured(repo):
    """(env name, value) of the codex `core_config_dirs` entry, or ("", "")."""
    root = Path(repo)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from src.sutando_config import find_core_config_dir
    entry = find_core_config_dir(type_="codex", repo_root=root)
    if not entry:
        return "", ""
    return str(entry.get("env_name") or ""), str(entry.get("value") or "")


def _check(value, repo, home):
    """The helper's mutation-free verdict; a failure is a verdict of its own, never a pass."""
    try:
        return codex_host_instructions.check(value, repo, home)
    except Exception as exc:  # noqa: BLE001 - any failure must refuse, not permit
        return f"error: {exc}"


def prepare(repo, home, environ=None):
    """Return (env, value, effective home); create the home only when permitted.

    Raises Refused when it must not be created or written.
    """
    environ = os.environ if environ is None else environ
    env, value = configured(repo)
    if "\n" in env or "\n" in value:
        raise Refused("the configured Codex home holds a newline")
    if codex_host_instructions.host_instructions(repo) is None:
        if env and value:
            os.makedirs(value, exist_ok=True)
        current = value if env == "CODEX_HOME" and value else environ.get("CODEX_HOME", "")
        return env, value, current or os.path.join(os.fspath(home), ".codex")
    if env != "CODEX_HOME" or not value:
        raise Refused(f"the configured Codex home ({env or 'no env'}={value or 'unset'}) "
                      "is not a CODEX_HOME, the only name Codex reads")
    verdict = _check(value, repo, home)
    if verdict == "owner-home":
        raise Refused(f"CODEX_HOME={value} is, or leads into, the owner's own Codex home")
    if verdict == "invalid-home":
        raise Refused(f"CODEX_HOME={value} is relative, contains '..', or has a symlink or "
                      "non-directory component")
    if verdict not in PERMITTED:
        raise Refused(f"CODEX_HOME={value} could not be checked ({verdict or 'no answer'})")
    os.makedirs(value, exist_ok=True)
    return env, value, value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("repo", type=Path)
    args = parser.parse_args(argv)
    try:
        env, value, path = prepare(args.repo, Path.home())
    except Refused as exc:
        try:
            env, value = configured(args.repo)
        except Exception:  # noqa: BLE001
            env, value = "", ""
        print(f"{env}\n{value}\n")
        print(f"codex home: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - unreadable config or a failed mkdir
        print(f"codex home: {exc}", file=sys.stderr)
        return 1
    print(f"{env}\n{value}\n{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
