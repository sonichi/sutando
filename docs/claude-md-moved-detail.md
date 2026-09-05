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
{"host": "...", "pid": ..., "heartbeat_pid": ..., "started_at": ..., "last_beat_at": ..., "status": "...",
 "socket": "...", "session": "sutando-core", "locality": {"kind": "local|cloud", "host": "..."},
 "backend": "tmux", "tmux_binary": "/opt/homebrew/bin/tmux", "tmux_version": "3.6b", "tmux_server_version": "3.6b",
 "tmux_verified": true, "tmux_candidates": ["/opt/homebrew/bin/tmux"], "schema_version": 4}
```

This is foundation for the lease-based multi-core scheduler — workers consult
the alive directory to know who's available before assigning a claim. For
single-machine use today it also gives `health-check.py` and the dashboard a
cleaner liveness probe than scanning `pgrep -f claude`.

`backend` / `tmux_binary` / `tmux_version` / `tmux_server_version` / `tmux_verified` / `tmux_candidates` (schema 4): a tmux client **verified** to speak to this core's server — the first of the PATH `tmux` (what the launchers run) and `SUTANDO_TMUX_BIN` (what the app exports) whose `display-message` on the recorded socket and observed session answers with the **server's own socket path and that session name** (an arbitrary executable that exits 0 is rejected; a `-V` that does not start with `tmux` records no version) — with its `-V`, and the version the **server itself** reports. Every probe the writer makes (session discovery, core pid) goes through that same client, so a protocol-refused PATH `tmux` beside a compatible exported one still finds the session. `restart.sh` and the Codex launcher's `--restart` run `core_heartbeat.py --stop` first, so an upgrade hands `.alive` to the new writer instead of keeping the old one (and its old schema) alive. Recorded because tmux versions with incompatible protocols cannot talk to each other, and a client that cannot connect reads a live core as absent; a reader starts from the recorded client instead of guessing. `tmux_binary` is a compatible client, **not** a claim about who created the server; when nothing speaks the fields are null and `tmux_verified` is false. Re-verified on every beat (nothing memoized). A reader should still re-verify before trusting a recorded path — it may have moved. `pid` is the core's, `heartbeat_pid` the writer's (schema 3); `session` is the observed tmux session.

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
## Result-marker semantics (full detail)

Moved verbatim from CLAUDE.md "Task bridge" (2026-08-21 context-budget diet).
The bridge handles delivery specially when the result body STARTS with one of
these markers. Use them when multiple related tasks should produce ONE
user-facing reply instead of N separate ones:

- `[deduped: task-<other-id>]` — both voice (task-bridge) and Discord (discord-bridge) silently archive this task as done, no narration, no DM. Put the full reply in the other task's result file and put this marker in each superseded task's result. The canonical way to handle thread-consolidated replies (e.g. when voice over-delegates 3 tasks for the same continuation utterance — see `src/task-bridge.ts:527`).
- `[no-send]` — Discord bridge skips delivery for this task (still archives). Use when the task is internally handled but produces no user-visible reply.
- `[REPLIED]` — Discord bridge skips delivery (already sent through another path).
- `[channel: <channel-id>]` — when this is the first non-empty line of the body, the bridge delivers the rest of the body to `<channel-id>` instead of the originating channel (and drops `thread_ts` since the post is moving threads). Discord ids are 17-20 digits; Slack ids match `[CDG][A-Z0-9]+`. Use when a task arrives in a noisy channel but the reply belongs somewhere else (e.g. #dev). Telegram silently drops it — no concept of "channels" on that surface.
- `[dm-only]` — privacy guard: suppresses any `[channel:]` redirect on the same body (regardless of marker order), so a body carrying private data can never be *redirected* out to a shared channel. It marks dm-only intent but does not by itself force a DM — that stays the consumer's job. In practice the private producer (the morning briefing's calendar + email) is emitted as a proactive result (`results/proactive-*.txt`), which every bridge already delivers to the owner's DM; `[dm-only]` reinforces that by guaranteeing no stray `[channel:]` redirect overrides it. **Detected anywhere in the body** — that is what makes the guard undefeatable by marker order, and over-triggering it fails safe. **Stripped only when the marker stands alone on its line**, before delivery and before voice speaks it; a marker mentioned inline in prose is detected but the text is delivered verbatim. Parsed by `result_markers.parse_markers`.
- `[file: /path]` / `[send: /path]` / `[attach: /path]` — Discord bridge extracts and attaches the file alongside the text body.

Why private parsers are forbidden (incident history): private copies drift —
`discord-bridge.py` and `dm-result.py` each carried a regex that only matched
`/...` or `~/...` values, so a marker every other consumer stripped was
delivered to the owner as literal text. Guarded by
`tests/bridge-marker-no-leak.test.py`; see also "Result-marker parser migration
status" above and `docs/architecture-boundaries.md` "HTTP route boundaries".

## Workspace env-var deprecation + historic fallback anti-pattern

Moved verbatim from CLAUDE.md "Workspace contract" (2026-08-21 context-budget diet):

> The `$SUTANDO_WORKSPACE` env var is no longer honored for workspace resolution
> as of v0.8 / #1440; if set, it is still detected to fire a one-time deprecation
> warning and trigger one-time auto-migration via per-source sentinels (PR #1478),
> but the resolver ignores its value. Historic anti-pattern: bridges fell back to
> the script's repo root via `Path(__file__).resolve().parent.parent`, which
> polluted `git status` and — when invoked from an app-bundled `src/` symlink —
> stranded owner DMs in a bundle-tasks/ dir while the watcher polled
> workspace-tasks/.

Current policy + protection layers: `docs/workspace-config.md`.
