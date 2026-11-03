#!/usr/bin/env python3
"""Sutando's entry for `ag2-sparrow-claude-local`: the package CLI with the workspace injected.

It injects through `_dirs.set_dirs()`, the seam remote-gateway-bridge.py uses: the bridge state is
`<workspace>/state`, an agent's own root is `<workspace>/agents/<agent>`, and its gateway bridge is
declared for sparrowd to run rather than given a service job of its own. Every other subcommand reads
the persisted config and behaves as the console script does.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
for _p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workspace_default import resolve_workspace  # noqa: E402
from ag2_sparrow import _dirs  # noqa: E402

BRIDGE_HOST = "sparrowd"


def main(argv=None) -> int:
    ws = resolve_workspace()
    _dirs.set_dirs(state_dir=ws / "state", agents_dir=ws / "agents", bridge_host=BRIDGE_HOST)
    from ag2_sparrow.adapters.claude_code.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
