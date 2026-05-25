"""WhatsApp action — Twilio Messages API with whatsapp: prefix.

Works with both Twilio's sandbox (join phrase required) and a production WABA number.
Reads from .env:
  TWILIO_ACCOUNT_SID    — same creds as SMS
  TWILIO_AUTH_TOKEN     — same creds as SMS
  TWILIO_WHATSAPP_NUMBER — sender; defaults to Twilio sandbox (+14155238886)
  OWNER_WHATSAPP_NUMBER  — recipient; falls back to OWNER_NUMBER if absent

Sandbox setup (one-time, 5 min):
  1. Open https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn
  2. Send the "join <word>" phrase to +14155238886 in WhatsApp.
  3. Set TWILIO_WHATSAPP_NUMBER=+14155238886 (default; omit the env var to use this).

Production WABA setup:
  Set TWILIO_WHATSAPP_NUMBER to your verified WABA sender number.

Closes #965 Track 2 — unblocks proactive-notify while A2P 10DLC registration (Track 1)
is pending.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

TWILIO_SANDBOX_NUMBER = "+14155238886"


def _load_env(repo_root: Path) -> dict[str, str]:
    env_path = repo_root / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if "#" in val and not (val.startswith("'") or val.startswith('"')):
            val = val.split("#", 1)[0].strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        out[key.strip()] = val
    return out


def _wa(number: str) -> str:
    """Prepend whatsapp: prefix if not already present."""
    num = number.strip()
    return num if num.startswith("whatsapp:") else f"whatsapp:{num}"


def send(ping, body: str) -> dict:
    repo_root = Path(__file__).resolve().parents[4]
    env = _load_env(repo_root)

    def _get(key: str) -> str:
        return env.get(key) or os.environ.get(key, "")

    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        if not _get(k):
            return {"ok": False, "error": f"missing {k}"}

    owner_num = _get("OWNER_WHATSAPP_NUMBER") or _get("OWNER_NUMBER")
    if not owner_num:
        return {"ok": False, "error": "missing OWNER_WHATSAPP_NUMBER (and OWNER_NUMBER fallback)"}

    sid = _get("TWILIO_ACCOUNT_SID")
    token = _get("TWILIO_AUTH_TOKEN")
    from_num = _get("TWILIO_WHATSAPP_NUMBER") or TWILIO_SANDBOX_NUMBER
    to_num = owner_num

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": _wa(from_num),
        "To": _wa(to_num),
        "Body": body,
    }).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
            return {"ok": True, "id": payload.get("sid", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
