"""SMS action — Twilio Messages API.

Reads TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, OWNER_NUMBER
from the repo .env. POSTs to https://api.twilio.com/2010-04-01/Accounts/<SID>/Messages.json.
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))


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
        # Strip optional surrounding quotes and inline comments.
        val = val.strip()
        if "#" in val and not (val.startswith("'") or val.startswith('"')):
            val = val.split("#", 1)[0].strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        out[key.strip()] = val
    return out


def send(ping, body: str) -> dict:
    repo_root = Path(__file__).resolve().parents[4]
    env = _load_env(repo_root)
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "OWNER_NUMBER"):
        if not env.get(k) and not os.environ.get(k):
            return {"ok": False, "error": f"missing {k}"}
    sid = env.get("TWILIO_ACCOUNT_SID") or os.environ["TWILIO_ACCOUNT_SID"]
    token = env.get("TWILIO_AUTH_TOKEN") or os.environ["TWILIO_AUTH_TOKEN"]
    from_num = env.get("TWILIO_PHONE_NUMBER") or os.environ["TWILIO_PHONE_NUMBER"]
    to_num = env.get("OWNER_NUMBER") or os.environ["OWNER_NUMBER"]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From": from_num, "To": to_num, "Body": body}).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json
            payload = json.loads(resp.read().decode())
            return {"ok": True, "id": payload.get("sid", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
