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

import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Isolate the channel config BEFORE importing the bridge, and SEED it.
#
# This file used to write a fake DISCORD_BOT_TOKEN into the operator's real
# `~/.claude/channels/discord/.env` whenever that file was absent. On a machine
# that has the file the write is a no-op, which is why it survived — the damage
# lands only on a host that has LOST its token, which is the host you least want
# a fake one planted on. A fake token also satisfies startup.sh's
# `grep -q "DISCORD_BOT_TOKEN="` and defeats the #2638 vault fallback, which
# fires only when no `.env` value exists.
#
# Seeding matters as much as redirecting: `channel_access_path()` falls back to
# the legacy real-home `access.json` when the canonical path is missing, so an
# EMPTY temp config dir still reads the operator's live allowlist.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-dm-result-send-dm-")
_ccd_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_ccd_discord.mkdir(parents=True, exist_ok=True)
(_ccd_discord / "access.json").write_text('{"allowFrom": []}')
(_ccd_discord / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")

_channels_env = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord" / ".env"
if not _channels_env.exists():
    _channels_env.parent.mkdir(parents=True, exist_ok=True)
    _channels_env.write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dm = _load("dm_result", REPO / "src" / "dm-result.py")
import channels.discord.client as _rest  # noqa: E402  — the seam the fakes install into


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


_SEAM = None


def _install_transport(transport):
    """Route the shared client through the fake (dm-result delivers via
    DiscordRestClient now, so the module's urlopen is no longer the seam)."""
    global _SEAM

    def _tuple(req, timeout):
        resp = transport.urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8", "replace")
        return getattr(resp, "status", 200), (json.loads(raw) if raw else None)

    def _read_json(req, timeout=None):
        resp = transport.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8", "replace"))

    _SEAM = (dm._client, _rest.request_json)
    dm._client = lambda token: _rest.DiscordRestClient(
        token, transport=_tuple, timeout=30)
    _rest.request_json = _read_json
    return _SEAM


def _restore_transport(_original=None):
    global _SEAM
    if _SEAM is not None:
        dm._client, _rest.request_json = _SEAM
        _SEAM = None


def _with_access_json(content, fn):
    """Patch dm.ACCESS_JSON and discord_config.load_config for isolation.

    The real discord-config.json at $SUTANDO_WORKSPACE/state/discord-config.json
    may have an `owner` field set, which bleeds into resolve_owner_id() step 2
    and overrides the tierMap / allowFrom under test. Patch load_config → {}
    so only the access_data fixture drives resolution.
    """
    original = dm.ACCESS_JSON
    original_load_config = dm.discord_config.load_config
    tmp = Path(tempfile.mkdtemp(prefix="sutando-dm-test-")) / "access.json"
    tmp.write_text(json.dumps(content))
    dm.ACCESS_JSON = tmp
    dm.discord_config.load_config = lambda: {}
    try:
        fn()
    finally:
        dm.ACCESS_JSON = original
        dm.discord_config.load_config = original_load_config
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


@contextlib.contextmanager
def _outbox_redirected():
    """Send audit-log appends to a throwaway file for the duration.

    `dm-result.py` late-imports `outbox_log` and calls `append()` after every
    successful send. `outbox_log._outbox_path()` resolves
    `resolve_workspace()/state/outbox.log` AT CALL TIME, and nothing in this
    suite rebound it — so each run appended real rows to the owner's live
    delivery audit log. Measured on the live workspace before this fix: running
    the suite changed `state/outbox.log` (2,025,726 B) every time.

    Patching `_outbox_path` rather than `resolve_workspace` is deliberate: the
    former is what `append()` actually calls, and re-resolving is done per call,
    so a function swap is sufficient here. That is NOT a general rule — the
    sibling fix in #2615 had to rebind a constant because it was bound at
    IMPORT time. Which seam works is a property of the module, so it is checked
    by exercise below, not assumed.

    Restore is in a `finally` so an assertion failure mid-suite cannot leave the
    global patched for whatever runs next in the same interpreter (#2614).
    """
    import outbox_log

    original = outbox_log._outbox_path
    tmpdir = Path(tempfile.mkdtemp(prefix="sutando-outbox-test-"))
    redirected = tmpdir / "outbox.log"
    outbox_log._outbox_path = lambda: redirected
    try:
        yield redirected
    finally:
        outbox_log._outbox_path = original
        shutil.rmtree(tmpdir, ignore_errors=True)


def _live_outbox():
    from workspace_default import resolve_workspace

    return resolve_workspace() / "state" / "outbox.log"


def test_suite_never_appends_to_the_live_outbox_log(live, before):
    """THE regression: a test run must not write to the owner's audit log.

    Asserts on the RESOLVED live path — whatever `resolve_workspace()` returns
    in this environment — rather than on a fixture path, because the defect was
    precisely that the suite escaped to wherever that resolves.

    `before` is captured by `main()` BEFORE any test runs, and deliberately not
    here. Taking the baseline inside this function would only cover this
    function's own sends: if the redirect around the other five tests were
    removed, their rows would already be in `before` and this would still pass.
    A regression that cannot fail for the mutation it exists to catch is
    decoration. Verified by mutation, both ways — see the PR body.

    Also proves the redirect is live rather than vacuous: the throwaway file
    must have GROWN. Without that half, simply deleting the `append()` call
    would turn this green.
    """
    with _outbox_redirected() as redirected:
        transport = _FakeTransport({
            ("POST", "/users/@me/channels"): {"id": "dm-channel-audit"},
            ("POST", "/channels/dm-channel-audit/messages"): {"id": "msg-audit"},
        })
        original_urlopen = _install_transport(transport)
        try:
            _with_access_json(
                {"allowFrom": ["555"], "tierMap": {"555": "owner"}},
                lambda: dm.send_dm("audit-path probe"),
            )
        finally:
            _restore_transport(original_urlopen)

        wrote = redirected.is_file() and redirected.stat().st_size > 0
        assert wrote, (
            "redirect target is empty — the suite either stopped calling "
            "outbox_log.append() or the patched seam is not the one append() uses, "
            "so this regression would pass without proving anything"
        )

    after = live.read_bytes() if live.is_file() else None
    assert after == before, (
        f"the suite appended to the LIVE outbox log at {live} "
        f"({'absent' if before is None else f'{len(before)} B'} -> "
        f"{'absent' if after is None else f'{len(after)} B'})"
    )
    print("  OK: live outbox.log untouched; audit rows went to the redirect")


def test_blocked_attachment_is_announced_to_the_recipient():
    """A file that EXISTS but sits outside the allowlist must produce a
    user-visible notice, not stderr alone.

    dm-result is the REST FALLBACK — it runs when the live bridge is down — and
    it used to log the rejection only, so the attachment silently never arrived:
    body delivered, task archived, nothing telling the recipient a file was
    meant to be there. discord-bridge.py has always sent
    `(file not allowed: <path>)` into the channel; the comment here claimed
    parity with it and did not have it.

    The fixture file is REAL (tempfile), so a pass can only come from the
    allowlist decision and never from "no such file" — which is the separate
    branch asserted below.
    """
    import tempfile
    fd, real_but_blocked = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        transport = _FakeTransport({
            ("POST", "/users/@me/channels"): {"id": "dm-6"},
            ("POST", "/channels/dm-6/messages"): {"id": "msg-6"},
        })
        original_urlopen = dm.urllib.request.urlopen

        def run():
            _install_transport(transport)
            try:
                ok = dm.send_dm(f"Here you go: [file: {real_but_blocked}]")
            finally:
                _restore_transport(original_urlopen)
            assert ok is True
            msg_calls = [c for c in transport.calls if "/messages" in c["url"]]
            assert len(msg_calls) == 1
            body = msg_calls[0]["body"]["content"]
            assert "not sent" in body, (
                f"a blocked attachment must be announced; body was {body!r}")
            assert "Here you go:" in body, "the real body must survive"
            assert real_but_blocked not in body, (
                "the rejected path must NOT be echoed back to the recipient")

        _with_access_json(
            {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}}, run)
    finally:
        os.unlink(real_but_blocked)


def test_missing_path_stays_silent_like_discord_bridge():
    """The OTHER half of the parity, and the reason the notice is split.

    A `[file:/path]` substring inside prose (a quoted example) resolves to no
    file. discord-bridge logs those and deliberately does not surface them
    (`elif not os.path.isfile(fpath)`), because a notice would then fire on
    ordinary text that merely mentions a path. Without this case the fix would
    look correct while making every prose mention of a path produce a spurious
    "attachment not sent" line.
    """
    transport = _FakeTransport({
        ("POST", "/users/@me/channels"): {"id": "dm-7"},
        ("POST", "/channels/dm-7/messages"): {"id": "msg-7"},
    })
    original_urlopen = dm.urllib.request.urlopen

    def run():
        _install_transport(transport)
        try:
            ok = dm.send_dm("Use the marker like [file: /tmp/sutando-nope.png] to attach.")
        finally:
            _restore_transport(original_urlopen)
        assert ok is True
        msg_calls = [c for c in transport.calls if "/messages" in c["url"]]
        assert len(msg_calls) == 1
        body = msg_calls[0]["body"]["content"]
        assert "not sent" not in body, (
            f"a non-existent path is a prose quotation — no notice; got {body!r}")

    _with_access_json(
        {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}}, run)


def main():
    # Baseline taken before ANY test runs, so the final assertion covers every
    # send in this file — not just the ones the regression itself makes.
    live = _live_outbox()
    before = live.read_bytes() if live.is_file() else None

    with _outbox_redirected():
        test_tier_map_resolution_skips_bot_lookup()
        test_bot_filter_fallback_still_works_without_tier_map()
        test_file_markers_stripped_from_body()
        test_empty_body_after_marker_strip_does_not_post_messages()
        test_env_override_skips_access_json_entirely()
        test_blocked_attachment_is_announced_to_the_recipient()
        test_missing_path_stays_silent_like_discord_bridge()

    test_suite_never_appends_to_the_live_outbox_log(live, before)
    print("All send_dm integration tests passed.")


if __name__ == "__main__":
    main()
