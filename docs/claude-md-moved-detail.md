# Detail moved verbatim from CLAUDE.md (2026-08-17 context-budget diet)

## Result-marker parser migration status

*Migration status: all four Python consumers conform, and the guard enforces it.*
`discord-bridge.py`, `dm-result.py`, `telegram-bridge.py`, and `slack-bridge.py` all
obtain marker grammar from `parse_markers()`, and `tests/bridge-marker-no-leak.test.py`
fails if any of them declares the grammar itself — matching the grammar in any regex
literal, so a renamed private parser cannot slip past. Telegram's `send_reply()` used to
compile its own `file|send|attach` regex and Slack declared the same regex dead at module
scope; both are gone. Add any new consumer to that guard when it starts handling markers.
A consumer may apply
only the actions its transport supports, but must NOT recognise, strip, or prioritise
markers with local regexes or `startswith` checks. Attachment-path authorization is a
separate concern owned by `src/send_allowlist.py`, applied immediately before the
upload sink. 
## Vault python usage

```python
import sys
from pathlib import Path

# Make the repo's src/ importable from any script stored inside this checkout.
repo = next(p for p in Path(__file__).resolve().parents
            if (p / "src" / "vault_intercept.py").is_file())
sys.path.insert(0, str(repo / "src"))

from vault_intercept import get_vault_key, list_vault_keys

keys = list_vault_keys()  # returns list of stored key names
api_key = get_vault_key("OPENAI_API_KEY")  # raises KeyError if not found
```

## Core liveness payload schema

Payload schema:
```json
{"host": "...", "pid": ..., "started_at": ..., "last_beat_at": ..., "status": "...", "socket": "...", "locality": {"kind": "local|cloud", "host": "..."}, "schema_version": 2}
```

This is foundation for the lease-based multi-core scheduler — workers consult
the alive directory to know who's available before assigning a claim. For
single-machine use today it also gives `health-check.py` and the dashboard a
cleaner liveness probe than scanning `pgrep -f claude`.

`locality` is the core's self-reported {kind: local|cloud, host} (Track 10) —
additive and informational; mtime remains the liveness signal, so readers that
don't know the field are unaffected.

`socket` records the tmux socket the core launched on (its own
`${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}`). It's the **runtime-authored**
answer to "which socket?" — read by `sutando-config.sh runtime` so the
AgentRuntime descriptor reports the real socket (custom sockets included)
without trusting a foreign caller's ambient env.

## Durable per-host install state: `state/auth/`

`<workspace>/state/auth/` holds **per-host install/identity state**
that survives across upgrades and MUST NOT be wiped by transient-state cleanup
jobs (or by clear-on-restart logic that targets `state/*.json` generically).
Current contents (the protection is DIRECTORY-level — everything under
`state/auth/` is exempt, including additions after this list):
- `cloud-auth.json` — per-host cloud-side auth credentials
- `device.json` — per-host device identity (UUID + provisioning metadata)
- `ag2space.json` — enrolled agent identity (stand_id source)
- `stand.json` — Stand record + OwnerBinding (owner-confirmed, 2026-08-23)
- `entrance-links.json` — verified EntranceLink records (I2)
- `task-hmac.key` — task-envelope trust root
- `devices/`, `pairing/`, `scp-tls/`, `scp-wss.token` — SCP device auth

Both are placed via M1 Part 2 (`scripts/sutando-migrate.sh`); pre-M1 they
were loose at workspace root, mistreated as transient JSON snapshots and
sometimes wiped. Treat `state/auth/` like `state/cores/<hostname>.alive` —
per-host, structural, never overwritten by newest-mtime resolution across
sources. Codex + Mini confirmed the destination + the exemption from cleanup
in #design 2026-06-02.
