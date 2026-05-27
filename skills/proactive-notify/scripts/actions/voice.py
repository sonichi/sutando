"""Voice action — speaks the ping body via the voice agent's result bridge.

Writes $WORKSPACE/results/proactive-{ts}.txt so task-bridge picks it up and
narrates it through the active voice session.

The channel router only routes here when presence.voice_client_connected is
True, but send() is tolerant of the case where it isn't.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

_RESULTS_DIR = resolve_workspace() / "results"


def send(ping, body: str) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = _RESULTS_DIR / f"proactive-{ts}.txt"
    try:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        result_path.write_text(body)
        return {"ok": True, "id": ts}
    except OSError as e:
        return {"ok": False, "error": str(e)}
