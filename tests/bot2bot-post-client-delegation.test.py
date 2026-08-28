#!/usr/bin/env python3
"""bot2bot-post delivers through the shared DiscordRestClient — no private HTTP.

The skill used to hand-roll its own urlopen POST, which sat outside the
post-gate chokepoint (a validator injected on the client could never see it).
Pinned here: `post()` routes through the PRODUCTION client (scripted transport
only), a refusal surfaces as a loud SystemExit that names the tri-state
outcome, an OUTCOME_UNKNOWN warns the post MAY have landed, and the module
carries no urllib fallback to drift back to.

Run: python3 tests/bot2bot-post-client-delegation.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_POST = REPO / "skills" / "bot2bot-post" / "post.py"
_spec = importlib.util.spec_from_file_location("b2b_post_delegation", _POST)
b2b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b2b)

sys.path.insert(0, str(REPO / "src"))
from channels.discord.client import DiscordRestClient  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def bind(*script):
    """Point b2b._client at the PRODUCTION client with a scripted transport."""
    calls = []
    steps = list(script)

    def transport(req, timeout):
        calls.append({"url": req.full_url, "method": req.get_method(),
                      "body": json.loads(req.data.decode()) if req.data else None})
        step = steps.pop(0) if steps else (200, {"id": "777"})
        if isinstance(step, Exception):
            raise step
        return step

    b2b._client = lambda token: DiscordRestClient(token, transport=transport)
    return calls


# --- the real factory builds the shared client with the pre-client 10s cap ---
_real = b2b._client("tok")
check("_client() builds the shared DiscordRestClient",
      isinstance(_real, DiscordRestClient))
check("_client() preserves the pre-client 10s timeout", _real._timeout == 10)

# --- happy path: POST built by the client, message object returned -----------
calls = bind((200, {"id": "424242"}))
result = b2b.post("chan1", "ping: hi", "tok")
check("post() returns the created message object", result == {"id": "424242"})
check("exactly one delivery attempt", len(calls) == 1)
check("the request is a POST to the channel messages endpoint",
      calls[0]["method"] == "POST" and calls[0]["url"].endswith("/channels/chan1/messages"))
check("the payload carries the content", calls[0]["body"] == {"content": "ping: hi"})

# --- 4xx refusal: loud SystemExit naming NOT_DELIVERED, nothing retried ------
calls = bind(urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b"{}")))
msg = ""
try:
    b2b.post("chan1", "ping: hi", "tok")
    check("4xx raises SystemExit", False)
except SystemExit as e:
    msg = str(e)
    check("4xx raises SystemExit", True)
check("refusal names NOT_DELIVERED + says nothing was sent",
      "NOT_DELIVERED" in msg and "NOTHING WAS SENT" in msg, msg)
check("refusal: single attempt (no private retry)", len(calls) == 1)

# --- timeout: OUTCOME_UNKNOWN warns the post MAY have landed -----------------
calls = bind(TimeoutError("timed out"))
msg = ""
try:
    b2b.post("chan1", "ping: hi", "tok")
    check("timeout raises SystemExit", False)
except SystemExit as e:
    msg = str(e)
    check("timeout raises SystemExit", True)
check("unknown outcome warns it MAY have landed (2026-07-29 double-ping guard)",
      "OUTCOME_UNKNOWN" in msg and "MAY have landed" in msg, msg)
check("timeout: single attempt", len(calls) == 1)

# --- get_self_id also rides the shared client (no urllib left anywhere) ------
class _SelfClient:
    def get_json(self, path):
        assert path == "/users/@me"
        return {"id": 314}


b2b._client = lambda token: _SelfClient()
check("get_self_id resolves via client.get_json('/users/@me') as str",
      b2b.get_self_id("tok") == "314")
check("module imports no urllib (nothing to drift back to)",
      not any(name.startswith("urllib") for name in dir(b2b)))

print()
if _fails:
    print(f"{len(_fails)} FAILED: " + "; ".join(_fails))
    sys.exit(1)
print("all bot2bot-post client-delegation assertions passed")
