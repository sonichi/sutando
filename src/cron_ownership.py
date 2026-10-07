"""Which crons.json entries a given session may register.

crons.json is one shared per-host list, but two kinds of session register
jobs from it: the core (running /schedule-crons) and a worker-pool worker
(running its own startup pass). Left alone, both would register every
unmarked entry, double-firing anything meant for a specific worker. This is
the one place that answers "does THIS session own this entry" so the core's
registration pass and a worker's registration pass can never compute two
different answers from the same file — see docs/architecture-boundaries.md
"Shared adapter policy".

An entry's `owner` field is optional and defaults to CORE, so every existing
crons.json entry keeps registering exactly where it always did.
"""
from __future__ import annotations

CORE = "core"


def entry_owner(entry: dict) -> str:
    """The session id that owns this entry. Absent/blank/non-string is CORE
    — today's only behavior, kept as the default so old entries are unaffected."""
    owner = entry.get("owner")
    return owner if isinstance(owner, str) and owner else CORE


def entries_for_owner(entries: list, owner_id: str) -> list:
    """The entries session `owner_id` may register, in their original order.

    `owner_id` is CORE for the core session, or a worker's bare id (the same
    id pool_roster.py and worker_identity.py use) for a worker session.
    """
    return [e for e in entries if entry_owner(e) == owner_id]
