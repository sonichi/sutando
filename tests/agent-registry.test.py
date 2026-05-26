#!/usr/bin/env python3
"""Structural tests for the agent-registry skill (PR #1039).

## Background

PR #1039 added a local Agent Registry: a dependency-free HTTP service that
tracks running Claude Code instances. It uses stdlib only (http.server +
sqlite3), binds to 127.0.0.1 only, writes a discovery file so clients find
the port without hardcoding it, and heartbeats entries to detect stale agents.

Files under test:
  skills/agent-registry/scripts/registry-service.py
  skills/agent-registry/scripts/registry-client.py
  skills/agent-registry/hooks/session-start.sh

Run: python3 tests/agent-registry.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICE = REPO / "skills" / "agent-registry" / "scripts" / "registry-service.py"
CLIENT = REPO / "skills" / "agent-registry" / "scripts" / "registry-client.py"
HOOK = REPO / "skills" / "agent-registry" / "hooks" / "session-start.sh"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:500], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    missing = [f for f in [SERVICE, CLIENT, HOOK] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    svc = SERVICE.read_text()
    cli = CLIENT.read_text()
    hook = HOOK.read_text()

    # ── registry-service.py: design constraints ───────────────────────────────

    # 1. stdlib-only — no third-party imports
    expect(
        "import requests" not in svc and "import flask" not in svc and "import aiohttp" not in svc,
        "registry-service.py: must use stdlib only (no requests, flask, aiohttp)",
    )

    # 2. Uses http.server (ThreadingHTTPServer) not a third-party HTTP lib
    expect(
        "ThreadingHTTPServer" in svc and "http.server" in svc,
        "registry-service.py: must use ThreadingHTTPServer from http.server",
    )

    # 3. Uses sqlite3 for persistence
    expect(
        "import sqlite3" in svc,
        "registry-service.py: must use sqlite3 for agent storage",
    )

    # 4. Binds to 127.0.0.1 only (never 0.0.0.0)
    expect(
        '"127.0.0.1"' in svc or "'127.0.0.1'" in svc,
        "registry-service.py: must bind to 127.0.0.1 only",
    )
    expect(
        '"0.0.0.0"' not in svc and "'0.0.0.0'" not in svc,
        "registry-service.py: must NOT bind to 0.0.0.0 (security: local-only service)",
    )

    # 5. HTTP API: all five expected endpoints present
    for endpoint in ("/register", "/heartbeat", "/deregister", "/agents", "/health"):
        expect(
            f'"{endpoint}"' in svc or f"'{endpoint}'" in svc,
            f"registry-service.py: must expose {endpoint} endpoint",
        )

    # 6. Port range: tries DEFAULT_PORT through DEFAULT_PORT+PORT_RANGE
    expect(
        "PORT_RANGE" in svc,
        "registry-service.py: must define PORT_RANGE to try a range of ports",
    )
    expect(
        "for port in range(" in svc,
        "registry-service.py: must iterate over a port range to find a free port",
    )

    # 7. Discovery file written at startup (so clients find the port)
    expect(
        "write_discovery" in svc,
        "registry-service.py: must call write_discovery() to publish the bound port",
    )
    expect(
        "agent-registry.json" in svc,
        "registry-service.py: discovery file must be named agent-registry.json",
    )

    # 8. Discovery file removed on shutdown (clean exit)
    expect(
        "remove_discovery" in svc,
        "registry-service.py: must call remove_discovery() in the finally block",
    )

    # 9. DB path lives under workspace data/ dir
    expect(
        '"data"' in svc and "agent-registry.db" in svc,
        "registry-service.py: DB must be stored under <workspace>/data/agent-registry.db",
    )

    # 10. STALE_SECS defined (heartbeat-staleness window)
    expect(
        "STALE_SECS" in svc,
        "registry-service.py: must define STALE_SECS for heartbeat staleness detection",
    )

    # 11. PRUNE_SECS defined (old stopped/stale rows deleted)
    expect(
        "PRUNE_SECS" in svc,
        "registry-service.py: must define PRUNE_SECS to bound DB growth",
    )

    # 12. agents table schema has expected columns
    for col in ("id", "name", "cwd", "pid", "host", "started_at", "last_heartbeat", "status"):
        expect(
            col in svc,
            f"registry-service.py: agents table must have column '{col}'",
        )

    # 13. ON CONFLICT upsert in INSERT (re-registration is idempotent)
    expect(
        "ON CONFLICT" in svc,
        "registry-service.py: INSERT must use ON CONFLICT upsert so re-registration is idempotent",
    )

    # 14. Threading lock guards all DB operations
    expect(
        "_db_lock" in svc and "threading.Lock()" in svc,
        "registry-service.py: must use a threading.Lock to guard all DB operations",
    )

    # 15. Discovery write uses atomic rename (no partial-read window)
    expect(
        "os.replace(" in svc,
        "registry-service.py: discovery file must be written atomically via os.replace()",
    )

    # 16. Workspace resolution via SUTANDO_WORKSPACE env var with fallback
    expect(
        "SUTANDO_WORKSPACE" in svc,
        "registry-service.py: must respect SUTANDO_WORKSPACE env var for workspace path",
    )
    expect(
        ".sutando/workspace" in svc,
        "registry-service.py: must default to ~/.sutando/workspace when env var absent",
    )

    # ── registry-client.py: core invariants ──────────────────────────────────

    # 17. Client reads discovery file to locate service (no hardcoded port)
    expect(
        "agent-registry.json" in cli,
        "registry-client.py: must locate service via discovery file (not a hardcoded port)",
    )

    # 18. --autostart: client can launch service when not running
    expect(
        "autostart" in cli,
        "registry-client.py: must support --autostart to launch service on demand",
    )

    # 19. watch subcommand: registers + heartbeats until PID exits + deregisters
    expect(
        "cmd_watch" in cli or "def watch" in cli,
        "registry-client.py: must implement a 'watch' command (register + heartbeat until PID exits)",
    )

    # 20. start_new_session=True when launching service (survives parent exit)
    expect(
        "start_new_session=True" in cli,
        "registry-client.py: service subprocess must use start_new_session=True so it survives the Claude session",
    )

    # 21. SIGTERM handler for clean deregistration
    expect(
        "signal.signal" in cli and "SIGTERM" in cli,
        "registry-client.py: must install a SIGTERM handler so watch deregisters when Claude exits",
    )

    # 22. Uses stdlib only (urllib, not requests)
    expect(
        "import urllib" in cli and "import requests" not in cli,
        "registry-client.py: must use stdlib urllib (not requests) for HTTP calls",
    )

    # ── session-start.sh: hook wiring ────────────────────────────────────────

    # 23. Hook backgrounds the registration (non-blocking session start)
    expect(
        ">/dev/null 2>&1 &" in hook or "2>&1 &" in hook,
        "session-start.sh: registry-client watch must be backgrounded (non-blocking)",
    )

    # 24. Uses --autostart so service starts automatically
    expect(
        "--autostart" in hook,
        "session-start.sh: must pass --autostart to registry-client watch",
    )

    # 25. Passes CLAUDE_PROJECT_DIR as cwd (or falls back to PWD)
    expect(
        "CLAUDE_PROJECT_DIR" in hook,
        "session-start.sh: must pass CLAUDE_PROJECT_DIR (or $PWD fallback) as --cwd",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 25
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
