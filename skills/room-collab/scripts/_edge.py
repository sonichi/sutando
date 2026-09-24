"""The Sutando side of the room-collab package: puts it on sys.path and fills
the two slots the package leaves to its host (workspace, room-ops script)."""
from __future__ import annotations

import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
REPO = SKILL.parent.parent
PACKAGE = REPO / "packages" / "room-collab"


def install():
    # The entry shims share the package's module names, so the package goes first.
    if str(PACKAGE) in sys.path:
        sys.path.remove(str(PACKAGE))
    sys.path.insert(0, str(PACKAGE))
    for name in ("room_collab", "presence_daemon"):
        mod = sys.modules.get(name)
        if mod is not None and Path(getattr(mod, "__file__", "") or "").resolve().parent != PACKAGE:
            del sys.modules[name]
    import room_collab

    def _resolve():
        if str(REPO / "src") not in sys.path:
            sys.path.insert(0, str(REPO / "src"))
        from workspace_default import resolve_workspace
        return resolve_workspace()

    room_collab.WORKSPACE_RESOLVER = _resolve
    room_collab.ROOM_OPS_SCRIPT = SKILL.parent / "agent-room-ops" / "room_ops.py"
    return room_collab
