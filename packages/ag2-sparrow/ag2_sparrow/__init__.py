"""ag2-sparrow — the local reliable I/O runtime for persistent AI agents.

A channel-neutral delivery runtime between an agent workspace and external
messaging systems: it persists inbound tasks and room events into a durable
local workspace and posts results back to the gateway. It also ships a
transactional-outbox delivery core (single-drainer claims, crash recovery,
bounded retry, three-state delivery outcomes, provider adapters) as a library
contract the outbound path is migrating onto — the default relay path does
not yet route through it. No agent logic, routing policy, or room
lifecycle semantics — a worker (e.g. ag2-space/agent-connect) turns each task
into an agent run. AG2 Space is the first transport profile.

The shared utility modules are kept in lockstep with sonichi/sutando `src/`
via tools/sync_from_src.py (a drift-check test fails CI if they diverge —
single source of truth, no hand-maintained fork).
"""
__version__ = "0.3.1"
