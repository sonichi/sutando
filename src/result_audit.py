"""Result-delivery audit ledger (Result Router spec §7) — the append-only sink.

Answers "did the user ever actually see this result?" without grepping four
bridge logs. Each outbound result gets exactly one line:

    <iso_ts>\t<task_id>\t<disposition>\t<surface>

written to `<workspace>/state/result-audit.log`. The *line format* is the pure
`result_router.audit_line` (no I/O, no clock); this module is the thin I/O
wrapper that stamps the time and appends — the same separation `outbox_log`
uses for outbound-message visibility.

**Never raises.** Auditing must not block or break delivery: every failure
(unresolvable workspace, unwritable file, bad input) silently no-ops. Bridges
call `record(...)` immediately AFTER a delivery attempt resolves — logging what
actually happened, not what was attempted.

`disposition` should be one of `result_router.Disposition`
(delivered / redirected / deduped / no_send / late_result / failed).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from workspace_default import resolve_workspace  # noqa: E402
import delivery.router as result_router  # noqa: E402


def _audit_path() -> Path:
    return resolve_workspace() / "state" / "result-audit.log"


def record(task_id: str, disposition: str, surface: str, ts: str | None = None) -> None:
    """Append one §7 audit line for a resolved delivery. Never raises.

    `ts` defaults to now (ISO-8601 UTC); callers may pass an explicit stamp for
    determinism (tests) or to match the delivery's own timestamp.
    """
    try:
        if ts is None:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = result_router.audit_line(task_id or "unknown", disposition, surface, ts)
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Observability must never block or crash delivery.
        pass
