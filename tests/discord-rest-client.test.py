#!/usr/bin/env python3
"""DiscordRestClient contract: retry semantics per request class, canonical receipts.

The load-bearing assertions: a delivery POST is attempted EXACTLY ONCE whatever
the failure (a transparent retry is a duplicate-send machine), outcomes come
from outbox_adapter.classify_response (id -> CONFIRMED, 4xx -> NOT_DELIVERED,
timeout/5xx/2xx-without-id -> OUTCOME_UNKNOWN), the receipt id key is pinned to
Discord's `id` (an `event_id` is not proof), and edit receipts carry
RetrySafety.SAFE while send receipts stay UNSAFE.

Run: python3 tests/discord-rest-client.test.py
"""
import io
import json
import sys
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import channels.discord.client as drc  # noqa: E402
from outbox import DeliveryOutcome, RetrySafety  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


class Transport:
    """Scripted transport: each call pops the next behavior."""
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout):
        self.calls.append(req)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def http_error(code, body=None):
    return urllib.error.HTTPError(
        "https://discord.com/x", code, "err", {},
        io.BytesIO(json.dumps(body or {}).encode()))


def main() -> int:
    # 1. Confirmed send: 200 + id.
    t = Transport((200, {"id": "111"}))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c", {"content": "hi"})
    check("send 200+id -> CONFIRMED", r.outcome is DeliveryOutcome.CONFIRMED)
    check("send receipt id", r.receipt_id == "111")
    check("send safety UNSAFE (caller must not blind-retry)",
          r.safety is RetrySafety.UNSAFE)

    # 2. 4xx -> NOT_DELIVERED, one attempt.
    t = Transport(http_error(400, {"message": "bad"}))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c", {})
    check("send 400 -> NOT_DELIVERED", r.outcome is DeliveryOutcome.NOT_DELIVERED)
    check("send 400: exactly one attempt", len(t.calls) == 1)

    # 3. Timeout -> OUTCOME_UNKNOWN, exactly one attempt (the core rule).
    t = Transport(TimeoutError("timed out"))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c", {})
    check("send timeout -> OUTCOME_UNKNOWN", r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN)
    check("send timeout: exactly one attempt — NO transparent retry",
          len(t.calls) == 1)

    # 4. 5xx -> OUTCOME_UNKNOWN (may have applied), exactly one attempt.
    t = Transport(http_error(502))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c", {})
    check("send 502 -> OUTCOME_UNKNOWN", r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN)
    check("send 502: exactly one attempt", len(t.calls) == 1)

    # 5. 2xx WITHOUT id -> OUTCOME_UNKNOWN (accepted, unproven).
    t = Transport((204, None))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c", {})
    check("send 2xx-no-id -> OUTCOME_UNKNOWN", r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN)

    # 6. id key pinned: a foreign id key is not proof.
    t = Transport((200, {"event_id": "zzz"}))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c", {})
    check("send 200+event_id-only -> OUTCOME_UNKNOWN (id key pinned)",
          r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN)

    # 7. Edit: same classification, safety SAFE, single attempt, PATCH verb.
    t = Transport((200, {"id": "111"}))
    r = drc.DiscordRestClient("tok", transport=t).edit_message("c", "111", {})
    check("edit 200+id -> CONFIRMED + SAFE",
          r.outcome is DeliveryOutcome.CONFIRMED and r.safety is RetrySafety.SAFE)
    check("edit uses PATCH", t.calls[0].get_method() == "PATCH")
    t = Transport(TimeoutError("t"))
    r = drc.DiscordRestClient("tok", transport=t).edit_message("c", "111", {})
    check("edit timeout -> UNKNOWN + SAFE + one attempt",
          r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN
          and r.safety is RetrySafety.SAFE and len(t.calls) == 1)

    # 8. upload_files: multipart body carries payload + file bytes; id -> CONFIRMED.
    t = Transport((200, {"id": "222"}))
    r = drc.DiscordRestClient("tok", transport=t).upload_files(
        "c", {"content": "see file"}, [("a.txt", b"BLOB-BYTES")])
    check("upload 200+id -> CONFIRMED", r.outcome is DeliveryOutcome.CONFIRMED)
    req = t.calls[0]
    body = req.data
    check("upload multipart carries file bytes", b"BLOB-BYTES" in body)
    check("upload multipart carries payload_json", b"see file" in body)
    check("upload content-type is multipart",
          "multipart/form-data" in (req.get_header("Content-type") or ""))

    # 8b. Adversarial filename: quote+CRLF must not become part headers
    #     (reviewer's control on 7d3e80e5, now permanent).
    t = Transport((200, {"id": "333"}))
    evil = 'safe.txt"\r\nX-Sutando-Injected: yes\r\nContent-Type: text/html'
    drc.DiscordRestClient("tok", transport=t).upload_files("c", {}, [(evil, b"B")])
    body = t.calls[0].data
    check("filename injection neutralized (no injected header line)",
          b"\r\nX-Sutando-Injected:" not in body)
    check("filename injection: quotes/CRLF replaced",
          b'filename="safe.txt___X-Sutando-Injected: yes__Content-Type: text/html"' in body)
    empty = drc.DiscordRestClient("tok", transport=Transport((200, {"id": "3"})))
    t2 = Transport((200, {"id": "3"}))
    drc.DiscordRestClient("tok", transport=t2).upload_files("c", {}, [("", b"B")])
    check("empty filename falls back", b'filename="file-0"' in t2.calls[0].data)

    # 9. Reads delegate to the retried helper (429/5xx policy lives there).
    seen = {}
    real = drc.request_json
    def _stub_rj(req, timeout=10):
        seen["req"] = req
        return {"id": "ch"}
    drc.request_json = _stub_rj
    try:
        out = drc.DiscordRestClient("tok").get_channel("123")
        check("get_channel delegates to request_json (retried read path)",
              out == {"id": "ch"} and "/channels/123" in seen["req"].full_url)
    finally:
        drc.request_json = real

    # Coverage for the read/control wrappers and the default transport —
    # the seam-injected tests above never execute them.
    real = drc.request_json
    calls = []
    def _rj2(req, timeout=None):
        calls.append(req.full_url)
        return {"id": "77"}
    drc.request_json = _rj2
    try:
        c = drc.DiscordRestClient("tok")
        c.get_user("9")
        c.list_messages("5", limit=2)
        dm_id = c.create_dm_channel("42")
        check("get_user/list_messages hit their paths",
              "/users/9" in calls[0] and "/channels/5/messages?limit=2" in calls[1])
        check("create_dm_channel returns the id as str", dm_id == "77")
        drc.request_json = lambda req, timeout=None: {}
        check("create_dm_channel without id returns None",
              drc.DiscordRestClient("tok").create_dm_channel("42") is None)
    finally:
        drc.request_json = real

    class _Resp:
        def __init__(self, body, status=200):
            self._b, self.status = body, status
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    real_open = drc.urllib.request.urlopen
    try:
        drc.urllib.request.urlopen = lambda req, timeout=None: _Resp(b'{"ok": 1}')
        check("_default_transport parses JSON",
              drc._default_transport(object(), 5) == (200, {"ok": 1}))
        drc.urllib.request.urlopen = lambda req, timeout=None: _Resp(b"plain text")
        check("_default_transport falls back to raw text",
              drc._default_transport(object(), 5) == (200, "plain text"))
    finally:
        drc.urllib.request.urlopen = real_open

    def _raise_httperror(req, timeout):
        raise urllib.error.HTTPError(
            "https://discord.com/x", 403, "err", {}, io.BytesIO(b"not json {"))
    r = drc.DiscordRestClient("tok", transport=_raise_httperror).send_message("1", {"content": "x"})
    check("HTTPError with unparsable body -> NOT_DELIVERED, body None survives",
          r.outcome.name == "NOT_DELIVERED")

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: DiscordRestClient — single-attempt delivery, canonical receipts, "
          "pinned id key, SAFE edits, retried reads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
