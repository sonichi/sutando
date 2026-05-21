"""Call action — outbound voice call via the local phone-conversation server.

POSTs to http://localhost:3100/call (provided by skills/phone-conversation),
which spawns an outbound Twilio call and routes the audio stream through
Gemini Live so the owner can converse if they want, not just hear a message.

Same-host call only (assumes conversation-server is running locally on 3100).
"""
from __future__ import annotations

import json
import os
import urllib.request


CONVERSATION_SERVER_URL = os.environ.get(
    "CONVERSATION_SERVER_URL", "http://localhost:3100"
)


def send(ping, body: str) -> dict:
    target = os.environ.get("OWNER_NUMBER")
    if not target:
        # Fall back to repo .env loader via the sms action's helper for
        # consistency on hosts where .env is the source of truth.
        from actions import sms as _sms  # local import to avoid action coupling at import time
        env = _sms._load_env(_sms.Path(__file__).resolve().parents[4])
        target = env.get("OWNER_NUMBER")
    if not target:
        return {"ok": False, "error": "missing OWNER_NUMBER"}

    payload = json.dumps({"to": target, "message": body}).encode()
    req = urllib.request.Request(
        f"{CONVERSATION_SERVER_URL}/call",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return {"ok": True, "id": data.get("callSid", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
