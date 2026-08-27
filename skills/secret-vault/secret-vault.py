#!/usr/bin/env python3
"""secret-vault — manage secrets stored in macOS Keychain via Sutando's secret vault.

Subcommands:
  list                    Show all stored key names (no values)
  get KEY                 Print the value for KEY
  set KEY                 Store a value for KEY, read from stdin (not argv —
                          keeps the secret out of `ps`/shell history)
  delete KEY              Remove KEY (Keychain item + manifest entry). Idempotent:
                          deleting an absent key is success (exit 0)
  env KEY [KEY...] -- CMD Run CMD with vault keys injected as environment variables

Examples:
  secret-vault.py list
  secret-vault.py get OPENAI_API_KEY
  printf '%s' "$OPENAI_API_KEY" | secret-vault.py set OPENAI_API_KEY
  secret-vault.py delete OPENAI_API_KEY
  secret-vault.py env OPENAI_API_KEY STRIPE_KEY -- python3 my_script.py
"""

import os
import sys

# Allow running from any directory by adding src/ to path
_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vault_intercept import delete_vault_key, get_vault_key, list_vault_keys, set_vault_key


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


def cmd_set(key: str) -> None:
    # Value arrives on stdin, never argv: `secret-vault.py set KEY <value` would
    # leak through `ps` and shell history the way an argv value does. A single
    # trailing newline is stripped (the `printf '%s' | ...` form adds none; an
    # `echo | ...` form adds exactly one) — inner whitespace is preserved.
    value = sys.stdin.read()
    if value.endswith("\n"):
        value = value[:-1]
    try:
        set_vault_key(key, value)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(f"stored '{key}'")


def cmd_delete(key: str) -> None:
    # Reverse of cmd_set: Keychain item AND manifest entry. Idempotent — an
    # already-gone key is not an error; only an invalid name raises, as in cmd_set.
    try:
        delete_vault_key(key)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(f"deleted '{key}'")


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
    # exec, don't fork-and-wait — `env`-style wrappers replace themselves.
    #
    # With subprocess.run() this process lingers as a parent for the whole life of
    # CMD, which costs three things for a long-running service:
    #   1. Two processes carry CMD's name in their argv, so any `ps`-based probe
    #      counts double. health-check's telegram-bridge probe reported "multiple
    #      processes (2 PIDs)" for a single bridge launched this way (2026-08-04).
    #   2. A signal sent to the wrapper (SIGTERM from a supervisor, launchd, or a
    #      stop script) is delivered to the WRAPPER, not to CMD, so the service it
    #      is meant to stop keeps running.
    #   3. If the wrapper dies, CMD is orphaned rather than exiting with it.
    # execvpe replaces this image with CMD, so CMD's pid, signals and exit status
    # are the wrapper's by construction — the same contract /usr/bin/env provides.
    # sys.exit(returncode) is no longer needed: after a successful exec there is
    # no code here to run, and CMD's own status is what the caller sees.
    try:
        os.execvpe(cmd[0], cmd, env)
    except OSError as e:
        # Reached only when exec itself fails (command not found, not executable).
        # subprocess.run() raised here too, so this stays a non-zero exit — but say
        # which command failed rather than surfacing a bare traceback.
        print(f"vault env: cannot execute {cmd[0]!r}: {e}", file=sys.stderr)
        sys.exit(127)


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

    elif sub == "set":
        if len(args) < 2:
            print("vault set: missing KEY (value is read from stdin)", file=sys.stderr)
            sys.exit(1)
        if len(args) > 2:
            # A value on argv is exactly the leak this verb avoids — refuse
            # loudly rather than store something visible in ps/history.
            print("vault set: pass the value on stdin, not argv", file=sys.stderr)
            sys.exit(1)
        cmd_set(args[1])

    elif sub == "delete":
        if len(args) < 2:
            print("vault delete: missing KEY", file=sys.stderr)
            sys.exit(1)
        cmd_delete(args[1])

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
        print("Usage: vault.py list | get KEY | set KEY | delete KEY | env KEY... -- CMD", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
