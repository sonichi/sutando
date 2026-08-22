#!/usr/bin/env python3
"""DiscordRestClient post-gate contract: the injected validator is structural.

The client is the ONE thing that POSTs messages to Discord, so a validator
injected here covers every sender by construction. Pinned:
  * no validator (the repo default) -> no gating, sends proceed;
  * validator sees (channel_id, payload) and a falsy return allows;
  * a refusal reason blocks EVERY delivery method (send / send_with_response /
    edit / edit_with_response / upload_files) with a NOT_DELIVERED receipt and
    ZERO transport attempts;
  * a validator that CRASHES fails CLOSED (refuses), never open;
  * reads and create_dm_channel are not part of the gated delivery class;
  * send_message/edit_message stay the first element of their _with_response
    twins (one code path, so the gate cannot be bypassed by method choice);
  * _default_transport maps a body that dies mid-read AFTER a 2xx to
    (status, None) — committed, not "no response" (the duplicate-send trap).

Run: python3 tests/discord-rest-client-gate.test.py
"""
import io
import sys
import urllib.error
import urllib.request
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
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout):
        self.calls.append(req)
        step = self.script.pop(0) if self.script else (200, {"id": "1"})
        if isinstance(step, Exception):
            raise step
        return step


def main() -> int:
    # 1. Default: no validator -> delivery proceeds untouched.
    t = Transport((200, {"id": "10"}))
    r = drc.DiscordRestClient("tok", transport=t).send_message("c1", {"content": "x"})
    check("no validator -> send proceeds", r.outcome is DeliveryOutcome.CONFIRMED)

    # 2. Allowing validator: called with (channel_id, payload), send proceeds.
    seen = []
    t = Transport((200, {"id": "11"}))
    client = drc.DiscordRestClient(
        "tok", transport=t,
        validator=lambda cid, payload: seen.append((cid, payload)) and None)
    r = client.send_message("chan-9", {"content": "hello"})
    check("allowing validator -> CONFIRMED", r.outcome is DeliveryOutcome.CONFIRMED)
    check("validator saw the channel id", seen and seen[0][0] == "chan-9")
    check("validator saw the payload", seen and seen[0][1] == {"content": "hello"})

    # 3. Refusal blocks every delivery method with zero attempts.
    def refuse(cid, payload):
        return "ruleset says no"

    t = Transport()
    client = drc.DiscordRestClient("tok", transport=t, validator=refuse)
    receipts = {
        "send_message": client.send_message("c", {"content": "x"}),
        "send_message_with_response": client.send_message_with_response(
            "c", {"content": "x"})[0],
        "edit_message": client.edit_message("c", "m", {"content": "x"}),
        "edit_message_with_response": client.edit_message_with_response(
            "c", "m", {"content": "x"})[0],
        "upload_files": client.upload_files("c", {"content": ""}, [("f.txt", b"x")]),
    }
    for name, receipt in receipts.items():
        check(f"refusal: {name} -> NOT_DELIVERED",
              receipt.outcome is DeliveryOutcome.NOT_DELIVERED)
        check(f"refusal: {name} names the reason",
              "ruleset says no" in receipt.detail, receipt.detail)
    check("refusal: ZERO transport attempts across all five", len(t.calls) == 0,
          f"{len(t.calls)} attempts")

    # 4. A crashing validator fails CLOSED, with zero attempts.
    def crash(cid, payload):
        raise RuntimeError("policy module exploded")

    t = Transport()
    client = drc.DiscordRestClient("tok", transport=t, validator=crash)
    r = client.send_message("c", {"content": "x"})
    check("crashing validator -> refused (fail closed)",
          r.outcome is DeliveryOutcome.NOT_DELIVERED)
    check("crashing validator: the crash is named", "policy module exploded" in r.detail)
    check("crashing validator: zero attempts", len(t.calls) == 0)

    # 5. Scope pin: reads and the open-DM control call are NOT the gated
    #    delivery class (a validator must not break owner-DM resolution).
    calls = []

    def gate_recorder(cid, payload):
        calls.append(cid)
        return None

    client = drc.DiscordRestClient("tok", validator=gate_recorder)
    orig = drc.request_json
    drc.request_json = lambda req, timeout=None: {"id": "dm-1"}
    try:
        cid = client.create_dm_channel("42")
        got = client.get_channel("7")
    finally:
        drc.request_json = orig
    check("create_dm_channel still works under a validator", cid == "dm-1")
    check("get_channel still works under a validator", got == {"id": "dm-1"})
    check("validator was NOT consulted for reads/control", calls == [])

    # 6. Delegation: the plain methods ARE their _with_response twins' receipt.
    t = Transport((200, {"id": "77"}), (200, {"id": "77"}))
    client = drc.DiscordRestClient("tok", transport=t)
    r1 = client.send_message("c", {"content": "x"})
    r2, status, body = client.send_message_with_response("c", {"content": "x"})
    check("send twins agree on receipt", r1 == r2)
    check("with_response surfaces the status", status == 200)
    check("with_response surfaces the body", body == {"id": "77"})

    t = Transport(urllib.error.HTTPError("u", 403, "f", {}, io.BytesIO(b"{}")))
    r, status, _ = drc.DiscordRestClient(
        "tok", transport=t).send_message_with_response("c", {})
    check("with_response: 403 -> NOT_DELIVERED with status",
          r.outcome is DeliveryOutcome.NOT_DELIVERED and status == 403)

    t = Transport((200, {"id": "88"}))
    r, status, body = drc.DiscordRestClient(
        "tok", transport=t).edit_message_with_response("c", "m", {"content": "x"})
    check("edit_with_response keeps RetrySafety.SAFE", r.safety is RetrySafety.SAFE)
    check("edit_with_response surfaces status+body",
          status == 200 and body == {"id": "88"})

    # 7. _default_transport: a read dying AFTER the 2xx is (status, None) —
    #    committed server-side; "no response" would invite a duplicating retry.
    class _DyingRead:
        status = 200

        def read(self):
            raise ConnectionResetError("peer reset mid-body")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _DyingRead()
    try:
        status, body = drc._default_transport(object(), 5)
    finally:
        urllib.request.urlopen = real
    check("mid-read death -> (200, None), not a raise", status == 200 and body is None)
    r = drc.classify_response(status, body, id_keys=drc._DISCORD_ID_KEYS)
    check("...which classifies as accepted-unproven, NOT no-response",
          r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN and "accepted" in r.detail,
          r.detail)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S)")
        return 1
    print("PASS: DiscordRestClient post-gate — structural coverage, fail-closed, "
          "delivery-class scope, committed-read honesty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
