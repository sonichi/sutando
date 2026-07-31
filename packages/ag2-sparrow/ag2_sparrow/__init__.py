"""ag2-sparrow — connect a local agent to AG2 Space (the AG2 task relay client).

Transport only: long-polls the AG2 Space task gateway for THIS agent's tasks
(identified by its relay token), drops each into a workspace, and posts results
back. No agent logic — a worker (e.g. ag2-space/agent-connect) turns each task
into an agent run.

The modules here are the canonical AG2 Space relay client, kept in lockstep with
sonichi/sutando `src/` via tools/sync_from_src.py (a drift-check test fails CI if
they diverge — single source of truth, no hand-maintained fork).
"""
__version__ = "0.3.0"
