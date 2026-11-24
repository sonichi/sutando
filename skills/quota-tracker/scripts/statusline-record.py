#!/usr/bin/env python3
"""Claude Code status line command: records the input's `rate_limits` into
state/quota-state.json (src/quota_record.py), then prints the passthrough status
line's output unchanged, or a short line of its own. Never fails, never waits long.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASSTHROUGH_TIMEOUT_S = 5


def _short_line(status) -> str:
    parts = []
    model = status.get("model") if isinstance(status, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name:
        parts.append(name)
    limits = status.get("rate_limits") if isinstance(status, dict) else None
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        win = limits.get(key) if isinstance(limits, dict) else None
        used = win.get("used_percentage") if isinstance(win, dict) else None
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            parts.append(f"{label} {used:g}%")
    return " · ".join(parts)


def main() -> None:
    raw = b""
    status = None
    try:
        raw = sys.stdin.buffer.read()
        status = json.loads(raw.decode("utf-8") or "null")
    except Exception:  # noqa: BLE001 — the status line renders whatever the input
        pass
    cfg = None
    try:
        import statusline_config as cfg
        import quota_record
        quota_record.record_statusline(cfg.workspace(), status)
    except Exception:  # noqa: BLE001
        pass
    try:
        cmd = cfg.passthrough_command(os.environ.get("CLAUDE_CONFIG_DIR")) if cfg else None
        if cmd:
            out = subprocess.run(cmd, shell=True, input=raw, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=PASSTHROUGH_TIMEOUT_S)
            sys.stdout.buffer.write(out.stdout)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        print(_short_line(status))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        os._exit(0)
