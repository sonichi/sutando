#!/usr/bin/env python3
"""Gateway agent-profile push: the instance PUTs its identity card once per
content change to /v1/agents/{mxid}/profile — no identity is a no-op, an
unchanged card never re-puts, and a 404 broker backs off instead of
hammering.

Run: python3 tests/gateway-agent-profile-push.test.py   (stdlib only)
"""
import importlib.util
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from pathlib import Path

FAILS = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILS.append(msg)


MXID = "@sutando-test:ag2.space"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="profpush-test-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = tmp
    Path(tmp, ".notes-migrated").touch()
    Path(tmp, ".build_log-migrated").touch()
    os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:9"  # never contacted
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
    os.environ.pop("AGENT_MXID", None)
    os.environ.pop("AGENT_ID", None)

    spec = importlib.util.spec_from_file_location(
        "rtc_prof",
        Path(__file__).resolve().parent.parent / "src"
        / "remote-gateway-bridge.py")
    rtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rtc)

    # hermetic: the channel .env fallback must not leak this host's identity
    rtc._config_from_channel_env = lambda *a, **kw: ""

    calls = []

    def fake_req(method, path, payload=None, timeout=35):
        calls.append((method, path, payload))
        return {}

    rtc._req = fake_req

    check(rtc._maybe_push_agent_profile() is False and not calls,
          "no identity: no-op, no request")

    os.environ["AGENT_MXID"] = MXID
    check(rtc._maybe_push_agent_profile() is True, "identity present: pushes")
    method, path, card = calls[0]
    check(method == "PUT"
          and path == f"/v1/agents/{urllib.parse.quote(MXID)}/profile",
          "PUT to the mxid-quoted profile route")
    check(card["display"]["name"] == "Sutando"
          and card["host"]["kind"] == "local"
          and card["host"]["host_id"],
          "card carries display name + host block")
    check("appearance" not in card.get("display", {}),
          "instance card never carries appearance fields")

    check(rtc._maybe_push_agent_profile() is False and len(calls) == 1,
          "unchanged card never re-puts")

    os.environ["SUTANDO_DISPLAY_NAME"] = "Sutando Dev"
    check(rtc._maybe_push_agent_profile() is True and len(calls) == 2,
          "changed card pushes again")
    check(calls[1][2]["display"]["name"] == "Sutando Dev",
          "the changed name is what ships")

    def req_404(method, path, payload=None, timeout=35):
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)

    rtc._req = req_404
    os.environ["SUTANDO_DISPLAY_NAME"] = "Sutando Three"
    check(rtc._maybe_push_agent_profile() is False and len(calls) == 2,
          "404 broker: push attempted once, reported deferred")
    check(rtc._profile_push_retry_at > time.time() + 3000,
          "404 backoff is about an hour")
    rtc._req = fake_req
    check(rtc._maybe_push_agent_profile() is False and len(calls) == 2,
          "inside the 404 backoff no further request is made")

    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
