---
name: vault
description: "Store and retrieve secrets securely via macOS Keychain. Use for passing API keys, tokens, or credentials to Sutando on the go."
user-invocable: false
---

# Vault

Store secrets securely in macOS Keychain and retrieve them at runtime.

## Commands

```bash
# Store a secret (e.g. you send "vault set APOLLO_KEY sk_abc123" via Slack/Discord)
python3 skills/vault/vault.py set APOLLO_KEY sk_abc123

# Retrieve at runtime (e.g. before calling an integration)
python3 skills/vault/vault.py get APOLLO_KEY

# List stored key names (no values)
python3 skills/vault/vault.py list

# Remove a key
python3 skills/vault/vault.py delete APOLLO_KEY

# Check existence (exits 0 if found, 1 if not)
python3 skills/vault/vault.py exists APOLLO_KEY
```

## How it works

- Secrets stored under account `sutando` in the macOS login Keychain
- Keychain encrypts at rest, per-user — only your macOS user account can read it
- `set` uses the `-U` flag (update-or-create) — safe to call repeatedly

## Task bridge integration

When Bassil sends a task like `vault set APOLLO_KEY sk_abc123` via Slack or Discord:

1. Sutando calls `python3 skills/vault/vault.py set APOLLO_KEY sk_abc123 --redact-task <task-file>`
2. The `--redact-task` flag overwrites the task file to replace the raw value with `[REDACTED]`
3. The secret never persists on disk beyond the ~2s processing window
4. Sutando replies confirming the key was stored

## Using stored secrets in integrations

```python
import subprocess
result = subprocess.run(
    ["python3", "skills/vault/vault.py", "get", "APOLLO_KEY"],
    capture_output=True, text=True, check=True,
)
api_key = result.stdout.strip()
```

Or via shell:
```bash
APOLLO_KEY=$(python3 skills/vault/vault.py get APOLLO_KEY)
```

## Handling vault tasks

When a task arrives with the pattern `vault set KEY value` or `vault get KEY`:

```python
import re, subprocess

VAULT_SET_RE = re.compile(r"vault\s+set\s+(\w+)\s+(\S+)", re.IGNORECASE)
VAULT_GET_RE = re.compile(r"vault\s+(get|list|delete|exists)\s*(\w*)", re.IGNORECASE)

m = VAULT_SET_RE.search(task_text)
if m:
    key, value = m.group(1), m.group(2)
    subprocess.run(
        ["python3", "skills/vault/vault.py", "set", key, value,
         "--redact-task", str(task_file)],
        check=True,
    )
    # reply: f"Stored '{key}' in vault."
```
