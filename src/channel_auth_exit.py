"""Exit contract for a channel bridge whose provider rejected its credential.

channel-bridge-wrapper.sh holds a bridge that exits EXIT_AUTH_REJECTED instead of relaunching it.
"""
from __future__ import annotations

import sys

# 78 == sysexits EX_CONFIG. Mirrored as a literal in channel-bridge-wrapper.sh.
EXIT_AUTH_REJECTED = 78


def exit_auth_rejected(channel: str, key: str) -> "None":
    """Log one line naming the credential key (never its value), then exit 78."""
    print(f"[{channel}-bridge] provider rejected {key}; fix: vault set {key} <value>, "
          "then restart (bash src/restart.sh)", file=sys.stderr, flush=True)
    raise SystemExit(EXIT_AUTH_REJECTED)
