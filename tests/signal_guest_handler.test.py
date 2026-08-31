"""signal_guest_handler — the guest deep_dive worker's security contract.

SUPERSEDED IN PLACE. This file tested the codex-era worker (``codex exec --sandbox
read-only`` under ``SIGNAL_GUEST_CODEX_HOME``, output collected via ``-o <file>``).
The guest lane now runs the Claude CLI under a launcher-pinned no-local-tools profile
with a self-provisioned isolated config home, so every assertion about the old spawn
shape is obsolete — keeping them would fail the suite while proving nothing.

The same load-bearing properties are asserted against the CURRENT worker in
``tests/signal-guest-claude-worker.test.py``:

  * fail-closed availability (no binary / unsupported CLI / managed policy /
    unprovisioned or unauthenticated profile), each with a machine-readable reason;
  * the worker env is an allowlist, never the owner's full environment;
  * spawn shape: the SURFACE-restricting ``--tools`` (not ``--allowedTools``), MCP
    denied, ambient settings dropped, own process group, throwaway cwd;
  * the isolated profile is reconstructed from an allowlist and negative-synced;
  * output goes through the fail-closed egress guard, published to RESULT_DIR;
  * a bounded fleet -> "busy" rather than unbounded workers;
  * the module never writes TASK_DIR (the gateway routes guest before any owner
    write — proven behaviorally by the desktop's build-time contract gate).

This file remains as a discoverable entry point that runs that suite, so the
repository's all-Python test runner keeps exercising the contract under this name.

Run: `python3 tests/signal_guest_handler.test.py`
"""
import runpy
import sys
from pathlib import Path

_CURRENT = Path(__file__).resolve().parent / "signal-guest-claude-worker.test.py"

if not _CURRENT.exists():  # pragma: no cover - a missing suite must be loud
    print(f"FAIL: the current guest-worker suite is missing at {_CURRENT}")
    sys.exit(1)

print(f"signal_guest_handler.test.py -> delegating to {_CURRENT.name}")
runpy.run_path(str(_CURRENT), run_name="__main__")
