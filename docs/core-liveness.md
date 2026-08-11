# Core liveness signal — payload schema and rationale

Extended reference for `CLAUDE.md` § Core liveness signal. The liveness rules
(per-host `.alive` file, 30s beat, mtime-is-the-signal, unlink on graceful
shutdown) live inline in `CLAUDE.md`; this file carries the payload schema and
field semantics.

## Payload schema

```json
{"host": "...", "pid": ..., "started_at": ..., "last_beat_at": ..., "status": "...", "socket": "...", "locality": {"kind": "local|cloud", "host": "..."}, "schema_version": 2}
```

## Why it exists

This is foundation for the lease-based multi-core scheduler — workers consult
the alive directory to know who's available before assigning a claim. For
single-machine use today it also gives `health-check.py` and the dashboard a
cleaner liveness probe than scanning `pgrep -f claude` / `pgrep -f codex`
(per runtime).

## Field semantics

`locality` is the core's self-reported {kind: local|cloud, host} (Track 10) —
additive and informational; mtime remains the liveness signal, so readers that
don't know the field are unaffected.

`socket` records the tmux socket the core launched on (its own
`${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}`). It's the **runtime-authored**
answer to "which socket?" — read by `sutando-config.sh runtime` so the
AgentRuntime descriptor reports the real socket (custom sockets included)
without trusting a foreign caller's ambient env.
