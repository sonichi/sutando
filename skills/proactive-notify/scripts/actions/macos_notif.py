"""macOS notification action — fires a desktop notification via osascript.

Suitable for fyi-tier pings that should surface as a banner without
interrupting a call or sending a message.

Env:
  SUTANDO_NOTIF_TITLE — notification title (default: "Sutando")
"""
from __future__ import annotations

import os
import subprocess

_DEFAULT_TITLE = "Sutando"


def send(ping, body: str) -> dict:
    title = os.environ.get("SUTANDO_NOTIF_TITLE") or _DEFAULT_TITLE
    # Escape for AppleScript string literal (double-quoted).
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{_esc(body)}" with title "{_esc(title)}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return {"ok": True, "id": "notif"}
    except FileNotFoundError:
        return {"ok": False, "error": "osascript not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "osascript timed out"}
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode().strip()
        return {"ok": False, "error": stderr or f"osascript exit {e.returncode}"}
