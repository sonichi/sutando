#!/usr/bin/env python3
"""Sutando vault — store and retrieve secrets via macOS Keychain.

Usage:
    python3 skills/vault/vault.py set KEY value   # store a secret
    python3 skills/vault/vault.py get KEY          # retrieve a secret (prints value)
    python3 skills/vault/vault.py list             # list key names (no values)
    python3 skills/vault/vault.py delete KEY       # remove a key
    python3 skills/vault/vault.py exists KEY       # exits 0 if key exists, 1 if not

All secrets are stored under the "sutando" account in the login keychain.
The service name is the KEY; the value is the secret. macOS enforces access
control — only your user account can read it.

Integration with the task bridge:
    When Sutando receives a task like "vault set APOLLO_KEY sk_abc123",
    it calls this script, then overwrites the task file to redact the value
    so the raw secret doesn't persist on disk beyond the processing window.
"""

import argparse
import subprocess
import sys


_ACCOUNT = "sutando"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def vault_set(key: str, value: str) -> None:
    """Store or update a secret in the Keychain."""
    result = _run(
        ["security", "add-generic-password",
         "-a", _ACCOUNT, "-s", key, "-w", value, "-U"],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"vault: failed to store '{key}': {result.stderr.strip()}")
    print(f"vault: stored '{key}'")


def vault_get(key: str) -> str:
    """Retrieve a secret. Prints value to stdout. Exits non-zero if not found."""
    result = _run(
        ["security", "find-generic-password",
         "-a", _ACCOUNT, "-s", key, "-w"],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"vault: key '{key}' not found")
    value = result.stdout.strip()
    print(value)
    return value


def vault_list() -> list[str]:
    """List all key names stored under the sutando account. Never prints values."""
    result = _run(
        ["security", "dump-keychain"],
        check=False,
    )
    keys = []
    current_account = None
    current_service = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if '"acct"<blob>=' in line:
            current_account = line.split('"acct"<blob>=')[1].strip().strip('"')
        if '"svce"<blob>=' in line:
            current_service = line.split('"svce"<blob>=')[1].strip().strip('"')
        if current_account == _ACCOUNT and current_service:
            if current_service not in keys:
                keys.append(current_service)
            current_account = None
            current_service = None
    if not keys:
        print("(no keys stored)")
    else:
        for k in sorted(keys):
            print(k)
    return keys


def vault_delete(key: str) -> None:
    """Remove a secret from the Keychain."""
    result = _run(
        ["security", "delete-generic-password",
         "-a", _ACCOUNT, "-s", key],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"vault: key '{key}' not found or could not be deleted")
    print(f"vault: deleted '{key}'")


def vault_exists(key: str) -> bool:
    """Exit 0 if key exists, 1 if not. No output."""
    result = _run(
        ["security", "find-generic-password",
         "-a", _ACCOUNT, "-s", key, "-w"],
        check=False,
    )
    exists = result.returncode == 0
    sys.exit(0 if exists else 1)


def redact_task_file(task_path: str, key: str) -> None:
    """Overwrite a task file to redact the secret value for KEY.

    Replaces the first line matching `vault set KEY <value>` with
    `vault set KEY [REDACTED]` so the raw secret doesn't persist on disk.
    """
    from pathlib import Path
    import re
    p = Path(task_path)
    if not p.exists():
        return
    text = p.read_text()
    pattern = re.compile(
        rf"(vault\s+set\s+{re.escape(key)}\s+)\S+",
        re.IGNORECASE,
    )
    redacted = pattern.sub(r"\1[REDACTED]", text)
    if redacted != text:
        p.write_text(redacted)


def main():
    parser = argparse.ArgumentParser(description="Sutando secret vault (macOS Keychain)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="Store a secret")
    p_set.add_argument("key", help="Key name (e.g. APOLLO_KEY)")
    p_set.add_argument("value", help="Secret value")
    p_set.add_argument("--redact-task", metavar="PATH", help="Task file to redact after storing")

    p_get = sub.add_parser("get", help="Retrieve a secret (prints value)")
    p_get.add_argument("key", help="Key name")

    sub.add_parser("list", help="List stored key names")

    p_del = sub.add_parser("delete", help="Remove a secret")
    p_del.add_argument("key", help="Key name")

    p_ex = sub.add_parser("exists", help="Exit 0 if key exists, 1 if not")
    p_ex.add_argument("key", help="Key name")

    args = parser.parse_args()

    if args.cmd == "set":
        vault_set(args.key, args.value)
        if args.redact_task:
            redact_task_file(args.redact_task, args.key)
    elif args.cmd == "get":
        vault_get(args.key)
    elif args.cmd == "list":
        vault_list()
    elif args.cmd == "delete":
        vault_delete(args.key)
    elif args.cmd == "exists":
        vault_exists(args.key)


if __name__ == "__main__":
    main()
