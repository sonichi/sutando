#!/usr/bin/env python3
"""Integration tests for `send_dm` in src/dm-result.py.

Probes the end-to-end REST flow by replacing `urllib.request.urlopen`
with a recording fake — every request issued by `send_dm` lands in a
captured list so the test can assert ordering, URLs, and payload bodies
were what the real Discord API would have seen.

Three real bugs are covered as regression guards:

  - `_resolve_owner_id` now honors `tierMap[uid] == "owner"`. Pre-fix
    the resolver only knew about $SUTANDO_DM_OWNER_ID and the
    bot-filter fallback; admins who tier-tagged an owner in
    access.json saw their notifications routed by the bot-filter
    instead.
  - `send_dm` now strips `[file:|send:|attach:]` markers from the
    body before chunking. Pre-fix the markers landed verbatim in the
    user's DM because dm-result is REST-only and has no multipart
    upload path. Captures the file list and logs it so the lossy
    delivery is visible.
  - A body that becomes empty after marker-strip must NOT POST `""`
    to /messages (Discord 400, error code 50006).
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")

_channels_env = Path.home() / ".claude" / "channels" / "discord" / ".env"
if not _channels_env.exists():
    _channels_env.parent.mkdir(parents=True, exist_ok=True)
    _channels_env.write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dm = _load("dm_result", REPO / "src" / "dm-result.py")


class _FakeResponse:
    def __init__(self, body_bytes: bytes):
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeTransport:
    """Records every request and replies with canned responses keyed on
    `(method, url-suffix)`. Anything unmapped raises so the test fails
    loudly instead of silently hanging or returning None."""

    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = dict(responses)

    def urlopen(self, request, timeout=None):
        method = getattr(request, "method", None) or (
            "POST" if request.data is not None else "GET"
        )
        url = request.full_url
        body = None
        if request.data is not None:
            body = json.loads(request.data.decode())
        self.calls.append({"method": method, "url": url, "body": body})
        for (m, suffix), reply in self._responses.items():
            if m == method and url.endswith(suffix):
                return _FakeResponse(json.dumps(reply).encode())
        raise AssertionError(f"unmocked request: {method} {url}")


def _install_transport(transport):
    dm.urllib.request.urlopen = transport.urlopen


def _restore_transport(original):
    dm.urllib.request.urlopen = original


def _with_access_json(content, fn):
    original = dm.ACCESS_JSON
    tmp = Path(tempfile.mkdtemp(prefix="sutando-dm-test-")) / "access.json"
    tmp.write_text(json.dumps(content))
    dm.ACCESS_JSON = tmp
    try:
        fn()
    finally:
        dm.ACCESS_JSON = original
        tmp.unlink()
        tmp.parent.rmdir()


def test_tier_map_resolution_skips_bot_lookup():
    """Bug A regression guard. allowFrom is `[non-owner, owner]` AND
    tierMap tags `owner`. The resolver MUST return `owner` directly,
    without calling `/users/{id}` on either ID — the tierMap signal is
    authoritative and the network round-trip is wasted work."""
    transport = _FakeTransport({
        ("POST", "/users/@me/channels"): {"id": "dm-channel-1"},
        ("POST", "/channels/dm-channel-1/messages"): {"id": "msg-1"},
    })
    original_urlopen = dm.urllib.request.urlopen

    def run():
        _install_transport(transport)
        try:
            ok = dm.send_dm("hello")
        finally:
            _restore_transport(original_urlopen)
        assert ok is True
        open_calls = [c for c in transport.calls if c["url"].endswith("/users/@me/channels")]
        assert len(open_calls) == 1, transport.calls
        assert open_calls[0]["body"] == {"recipient_id": "tier-owner-id"}
        bot_lookups = [c for c in transport.calls if "/users/" in c["url"] and not c["url"].endswith("/users/@me/channels")]
        assert bot_lookups == [], f"unexpected bot lookups: {bot_lookups}"

    _with_access_json(
        {
            "allowFrom": ["bot-id-A", "tier-owner-id", "bot-id-B"],
            "tierMap": {"tier-owner-id": "owner"},
        },
        run,
    )


def test_bot_filter_fallback_still_works_without_tier_map():
    """Pre-existing behavior preserved: with no tierMap, the resolver
    walks allowFrom, queries `/users/{id}.bot`, and picks the first
    non-bot."""
    transport = _FakeTransport({
        ("GET", "/users/bot-id"): {"id": "bot-id", "bot": True},
        ("GET", "/users/human-id"): {"id": "human-id", "bot": False},
        ("POST", "/users/@me/channels"): {"id": "dm-channel-2"},
        ("POST", "/channels/dm-channel-2/messages"): {"id": "msg-2"},
    })
    original_urlopen = dm.urllib.request.urlopen

    def run():
        _install_transport(transport)
        try:
            ok = dm.send_dm("hi")
        finally:
            _restore_transport(original_urlopen)
        assert ok is True
        open_calls = [c for c in transport.calls if c["url"].endswith("/users/@me/channels")]
        assert open_calls[0]["body"] == {"recipient_id": "human-id"}

    _with_access_json(
        {"allowFrom": ["bot-id", "human-id"]},
        run,
    )


def test_file_markers_stripped_from_body():
    """Bug D regression guard. A result body containing a file marker
    must deliver the clean text to Discord — not the literal
    `[file: /path]` string."""
    transport = _FakeTransport({
        ("POST", "/users/@me/channels"): {"id": "dm-3"},
        ("POST", "/channels/dm-3/messages"): {"id": "msg-3"},
    })
    original_urlopen = dm.urllib.request.urlopen

    def run():
        _install_transport(transport)
        try:
            ok = dm.send_dm(
                "Here's the screenshot you asked about: [file: /tmp/sutando-x.png]"
            )
        finally:
            _restore_transport(original_urlopen)
        assert ok is True
        msg_calls = [c for c in transport.calls if "/messages" in c["url"]]
        assert len(msg_calls) == 1
        sent_body = msg_calls[0]["body"]["content"]
        assert "[file:" not in sent_body, f"marker leaked into DM: {sent_body!r}"
        assert "Here's the screenshot you asked about:" in sent_body

    _with_access_json(
        {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
        run,
    )


def test_empty_body_after_marker_strip_does_not_post_messages():
    """Bug C: a body that's ONLY a file marker becomes empty after
    strip. Discord 400 prevention — skip the /messages call entirely;
    report no-op."""
    transport = _FakeTransport({
        ("POST", "/users/@me/channels"): {"id": "dm-4"},
        # If /messages is called, the test fails because we didn't
        # register a response — _FakeTransport raises AssertionError.
    })
    original_urlopen = dm.urllib.request.urlopen

    def run():
        _install_transport(transport)
        try:
            ok = dm.send_dm("[file: /tmp/sutando-x.png]")
        finally:
            _restore_transport(original_urlopen)
        assert ok is True  # No-op is not an error.
        msg_calls = [c for c in transport.calls if "/messages" in c["url"]]
        assert msg_calls == [], (
            f"expected NO /messages POSTs for an all-marker body; got {msg_calls}"
        )

    _with_access_json(
        {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
        run,
    )


def test_env_override_skips_access_json_entirely():
    """Existing behavior preserved: $SUTANDO_DM_OWNER_ID short-circuits
    all of access.json + tierMap + bot-lookup."""
    transport = _FakeTransport({
        ("POST", "/users/@me/channels"): {"id": "dm-5"},
        ("POST", "/channels/dm-5/messages"): {"id": "msg-5"},
    })
    original_urlopen = dm.urllib.request.urlopen
    os.environ["SUTANDO_DM_OWNER_ID"] = "env-override-id"

    def run():
        _install_transport(transport)
        try:
            ok = dm.send_dm("hi")
        finally:
            _restore_transport(original_urlopen)
            del os.environ["SUTANDO_DM_OWNER_ID"]
        assert ok is True
        open_calls = [c for c in transport.calls if c["url"].endswith("/users/@me/channels")]
        assert open_calls[0]["body"] == {"recipient_id": "env-override-id"}

    _with_access_json(
        {
            "allowFrom": ["other-human-id"],
            "tierMap": {"other-human-id": "owner"},
        },
        run,
    )


def main():
    test_tier_map_resolution_skips_bot_lookup()
    test_bot_filter_fallback_still_works_without_tier_map()
    test_file_markers_stripped_from_body()
    test_empty_body_after_marker_strip_does_not_post_messages()
    test_env_override_skips_access_json_entirely()
    print("All send_dm integration tests passed.")


if __name__ == "__main__":
    main()
