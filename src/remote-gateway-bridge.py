#!/usr/bin/env python3
"""remote-gateway-bridge.py — sutando loader for the canonical ag2-sparrow client.

Post-#2082 (option A live-switch): the transport implementation lives
canonically in packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py
(published to PyPI as ag2-sparrow). This file keeps the sutando entrypoint +
workspace wiring:

  1. inject sutando's workspace dirs (tasks/, results/, state/) via
     ``set_dirs()`` BEFORE the client module evaluates its import-time config;
  2. pin MEDIA_DIR to ``<workspace>/data/remote-media`` — the transport's
     standalone default is ``STATE_DIR/remote-media``, sutando's pre-switch
     location was under ``data/`` and must not move;
  3. register sutando's extra send-allowed roots (notes/docs/owner asset dirs)
     on top of the transport's result-dir-only default;
  4. execute the canonical module source in THIS module's namespace with
     ``__package__`` pinned, so the package's relative imports resolve and
     every existing loader of this file — startup.sh's ``python3 src/...``,
     runpy via the deprecated remote-relay-bridge name, and the mock-gateway
     test harness (which loads the file several times under different env and
     mutates module attributes) — sees exactly the same module-level API and
     import-time env parsing as the pre-switch monolith.

Revert: restore this file's previous revision from git (the pre-switch
monolith is the parent commit of the one introducing this shim). No other
consumer changes are needed either way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent

# src/ first (workspace_default, util_paths), then the in-repo package root so
# ``import ag2_sparrow`` resolves without an install step (same code the PyPI
# dist ships; the in-repo copy is canonical for the live core).
for _p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workspace_default import resolve_workspace  # noqa: E402
from util_paths import shared_personal_path  # noqa: E402

WS = resolve_workspace()

from ag2_sparrow._dirs import set_dirs  # noqa: E402

set_dirs(task_dir=WS / "tasks", result_dir=WS / "results", state_dir=WS / "state")
os.environ.setdefault("REMOTE_MEDIA_DIR", str(WS / "data" / "remote-media"))

from ag2_sparrow import send_allowlist as _send_allowlist  # noqa: E402

# Same roots the pre-switch monolith allowed via src/send_allowlist.py
# (result_dir is the transport's own base root — not repeated here).
# Membership guard: the test harness loads this file several times in one
# process; the package module is cached, so unguarded registration would
# stack duplicates.
for _root in (
    WS / "notes",
    shared_personal_path("notes", WS),
    WS / "docs",
    Path.home() / "Desktop" / "iclr-backups",
    Path.home() / "Documents" / "sutando-launch-assets",
):
    if str(_root) not in _send_allowlist._EXTRA_ROOTS:
        _send_allowlist.register_extra_roots(_root)

# Run the canonical client source in-place. exec (not import) is deliberate:
# module-level config (tier fail-closed parsing, URL/TOKEN, dirs) must
# re-evaluate on EVERY load of this file — the mock-gateway test harness loads
# it repeatedly under different env — and attribute reads/writes
# (``rtc._ack_disabled_until = 0.0``) must hit the same namespace the running code
# uses. A cached ``import ag2_sparrow.remote_gateway_bridge`` gives neither.
_IMPL = _REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"
__package__ = "ag2_sparrow"  # PEP 328: makes the source's relative imports resolve
exec(compile(_IMPL.read_text(encoding="utf-8"), str(_IMPL), "exec"), globals())

# Sutando wiring: intercepted `vault set KEY VALUE` bodies store to the macOS
# Keychain vault (skills/secret-vault) instead of persisting plaintext — the
# same guarantee the Slack/Discord bridges give (owner gap-report 2026-08-06).
# Assigned AFTER the exec so the canonical module's default (None) is replaced
# in the very namespace the running code reads.
def _keychain_vault_sink(key: str, value: str) -> bool:
    import subprocess as _sp
    _p = _sp.run(
        [sys.executable, str(_REPO / "skills" / "secret-vault" / "secret-vault.py"),
         "set", key],
        input=value.encode(), capture_output=True, timeout=15)
    return _p.returncode == 0


VAULT_SINK = _keychain_vault_sink
