#!/usr/bin/env python3
"""secret-vault — manage secrets stored in macOS Keychain via Sutando's secret vault.

Subcommands:
  list                    Show all stored key names (no values)
  get KEY                 Print the value for KEY
  env KEY [KEY...] -- CMD Run CMD with vault keys injected as environment variables

Examples:
  secret-vault.py list
  secret-vault.py get OPENAI_API_KEY
  secret-vault.py env OPENAI_API_KEY STRIPE_KEY -- python3 my_script.py
"""

import os
import subprocess
import sys

# Allow running from any directory by adding src/ to path
_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vault_intercept import get_vault_key, list_vault_keys


def cmd_list() -> None:
    keys = list_vault_keys()
    if not keys:
        print("(no keys stored)")
        return
    for k in keys:
        print(k)


def cmd_get(key: str) -> None:
    try:
        print(get_vault_key(key))
    except KeyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def cmd_env(keys: list[str], cmd: list[str]) -> None:
    if not cmd:
        print("vault env: missing command after --", file=sys.stderr)
        sys.exit(1)
    env = os.environ.copy()
    for k in keys:
        try:
            env[k] = get_vault_key(k)
        except KeyError:
            print(f"vault env: key '{k}' not found — aborting", file=sys.stderr)
            sys.exit(1)
    result = subprocess.run(cmd, env=env)
    sys.exit(result.returncode)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    sub = args[0]

    if sub == "list":
        cmd_list()

    elif sub == "get":
        if len(args) < 2:
            print("vault get: missing KEY", file=sys.stderr)
            sys.exit(1)
        cmd_get(args[1])

    elif sub == "env":
        rest = args[1:]
        try:
            sep = rest.index("--")
        except ValueError:
            print("vault env: missing -- separator before command", file=sys.stderr)
            sys.exit(1)
        cmd_env(rest[:sep], rest[sep + 1:])

    else:
        print(f"vault: unknown subcommand '{sub}'", file=sys.stderr)
        print("Usage: vault.py list | get KEY | env KEY... -- CMD", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
