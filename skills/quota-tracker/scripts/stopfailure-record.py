#!/usr/bin/env python3
"""StopFailure hook: a turn ended on `error: "rate_limit"` records a 429 and a
rejected status in state/quota-state.json (src/quota_record.py). Output is ignored."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "null")
        if not isinstance(event, dict) or event.get("error") != "rate_limit":
            return
        import statusline_config as cfg
        import quota_record
        detail = event.get("error_details")
        quota_record.record_rate_limit(cfg.workspace(), detail=detail if isinstance(detail, str) else "")
    except Exception:  # noqa: BLE001 — a hook must never fail the session
        pass


if __name__ == "__main__":
    main()
    os._exit(0)
