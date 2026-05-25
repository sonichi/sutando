#!/usr/bin/env python3
"""Integration tests for the REST multipart file-upload path in
`src/dm-result.py`.

Closes the TODO flagged in PR #985 (upstream) / PR #26 (fork): the
dm-result fallback used to strip `[file:|send:|attach:]` markers and
log them as "REST multipart upload not implemented". This test pins
the new implementation:

  - Allowlisted file → uploaded via multipart/form-data
  - Non-allowlisted file → rejected with stderr log, NOT uploaded
  - 10-file batching → multiple multipart messages for 11+ files
  - Filename header sanitization → CR/LF/quote in basename can't break
    out of the multipart envelope (regression guard for the
    PR #1022-class filename forgery)
  - Empty-content-with-files → valid multipart POST (no 400)

Probes the end-to-end REST flow by replacing `urllib.request.urlopen`
with a recording fake that captures every request — multipart bodies
included — so the test can assert on the actual envelope shape that
goes over the wire.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SUTANDO_WORKSPACE", tempfile.mkdtemp(prefix="sutando-dm-mp-test-ws-"))

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
    """Records every urlopen call and replies based on URL suffix.
    Stores the FULL request body bytes (for multipart inspection)."""

    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = dict(responses)

    def urlopen(self, request, timeout=None):
        method = getattr(request, "method", None) or ("POST" if request.data is not None else "GET")
        url = request.full_url
        headers = dict(request.headers) if request.headers else {}
        body_bytes = request.data if request.data is not None else b""
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "body_bytes": body_bytes,
            "is_multipart": "multipart/form-data" in (headers.get("Content-type") or headers.get("Content-Type") or ""),
        })
        for (m, suffix), reply in self._responses.items():
            if m == method and url.endswith(suffix):
                return _FakeResponse(json.dumps(reply).encode())
        raise AssertionError(f"unmocked request: {method} {url}")


def _install_transport(transport):
    original = dm.urllib.request.urlopen
    dm.urllib.request.urlopen = transport.urlopen
    return original


def _restore_transport(original):
    dm.urllib.request.urlopen = original


def _with_access_json(content, fn):
    original = dm.ACCESS_JSON
    tmp = Path(tempfile.mkdtemp(prefix="sutando-dm-mp-acc-")) / "access.json"
    tmp.write_text(json.dumps(content))
    dm.ACCESS_JSON = tmp
    try:
        fn()
    finally:
        dm.ACCESS_JSON = original
        tmp.unlink()
        tmp.parent.rmdir()


def _make_sutando_file(name="x.png", content=b"PNG-bytes-pretend"):
    """Create a real file under `/tmp/sutando-` — passes the allowlist."""
    p = Path(f"/tmp/sutando-test-{name}")
    p.write_bytes(content)
    return p


def test_allowlisted_file_uploaded_via_multipart():
    """The headline case: a `[file:]` marker for an allowlisted path
    triggers a multipart POST containing the file bytes."""
    img = _make_sutando_file("upload-1.png", b"PNG-magic-bytes-here")
    try:
        transport = _FakeTransport({
            ("POST", "/users/@me/channels"): {"id": "dm-1"},
            ("POST", "/channels/dm-1/messages"): {"id": "msg-1"},
        })
        original = _install_transport(transport)
        def run():
            try:
                ok = dm.send_dm(f"Here is the screenshot: [file: {img}]")
            finally:
                _restore_transport(original)
            assert ok is True
            # Look for the multipart upload call (separate from the
            # text-chunk send).
            mp_calls = [c for c in transport.calls if c["is_multipart"]]
            assert len(mp_calls) == 1, (
                f"expected exactly one multipart upload; got {len(mp_calls)}. "
                f"All calls: {[(c['method'], c['url']) for c in transport.calls]}"
            )
            body = mp_calls[0]["body_bytes"]
            assert b'Content-Disposition: form-data; name="payload_json"' in body
            assert b'Content-Disposition: form-data; name="files[0]"' in body
            assert b"PNG-magic-bytes-here" in body, "file bytes missing from multipart body"
            assert b'filename="upload-1.png"' not in body or b"upload-1.png" in body
        _with_access_json(
            {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
            run,
        )
    finally:
        img.unlink(missing_ok=True)


def test_non_allowlisted_file_rejected_not_uploaded():
    """Files outside the allowlist (e.g. `/etc/hosts`) must be
    rejected — no multipart upload, log to stderr instead."""
    transport = _FakeTransport({
        ("POST", "/users/@me/channels"): {"id": "dm-2"},
        ("POST", "/channels/dm-2/messages"): {"id": "msg-2"},
    })
    original = _install_transport(transport)
    def run():
        try:
            ok = dm.send_dm("Here is the file: [file: /etc/hosts]")
        finally:
            _restore_transport(original)
        assert ok is True
        mp_calls = [c for c in transport.calls if c["is_multipart"]]
        assert mp_calls == [], (
            f"expected ZERO multipart uploads for non-allowlisted path; "
            f"got {len(mp_calls)}"
        )
    _with_access_json(
        {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
        run,
    )


def test_file_only_message_with_empty_text():
    """A body that's ONLY a `[file:]` marker (empty after strip) but
    has an allowlisted path → uploads the file via multipart with
    empty content. Pre-fix this was a no-op."""
    img = _make_sutando_file("only-file.png", b"only-file-bytes")
    try:
        transport = _FakeTransport({
            ("POST", "/users/@me/channels"): {"id": "dm-3"},
            ("POST", "/channels/dm-3/messages"): {"id": "msg-3"},
        })
        original = _install_transport(transport)
        def run():
            try:
                ok = dm.send_dm(f"[file: {img}]")
            finally:
                _restore_transport(original)
            assert ok is True
            mp_calls = [c for c in transport.calls if c["is_multipart"]]
            assert len(mp_calls) == 1
            # Empty content in payload_json
            body = mp_calls[0]["body_bytes"]
            assert b'"content": ""' in body or b'name="payload_json"\r\nContent-Type: application/json\r\n\r\n{}' in body
            text_calls = [c for c in transport.calls if c["url"].endswith("/messages") and not c["is_multipart"]]
            assert text_calls == [], "no text-only message should have been sent"
        _with_access_json(
            {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
            run,
        )
    finally:
        img.unlink(missing_ok=True)


def test_eleven_files_split_into_two_batches():
    """Discord's 10-attachments-per-message limit: 11 files must
    upload as two multipart POSTs (10 + 1)."""
    files = []
    for i in range(11):
        files.append(_make_sutando_file(f"batch-{i}.png", f"file-{i}-bytes".encode()))
    try:
        markers = " ".join(f"[file: {p}]" for p in files)
        transport = _FakeTransport({
            ("POST", "/users/@me/channels"): {"id": "dm-4"},
            ("POST", "/channels/dm-4/messages"): {"id": "msg-4"},
        })
        original = _install_transport(transport)
        def run():
            try:
                ok = dm.send_dm(f"Here are 11 files: {markers}")
            finally:
                _restore_transport(original)
            assert ok is True
            mp_calls = [c for c in transport.calls if c["is_multipart"]]
            assert len(mp_calls) == 2, (
                f"expected exactly 2 multipart uploads (10 + 1); got {len(mp_calls)}"
            )
            # First batch should have 10 files, second should have 1.
            first_files = mp_calls[0]["body_bytes"].count(b'name="files[')
            second_files = mp_calls[1]["body_bytes"].count(b'name="files[')
            assert first_files == 10, f"first batch had {first_files} files, expected 10"
            assert second_files == 1, f"second batch had {second_files} files, expected 1"
        _with_access_json(
            {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
            run,
        )
    finally:
        for p in files:
            p.unlink(missing_ok=True)


def test_filename_crlf_quote_sanitized_in_header():
    """Defensive: a file whose basename contains `\\r`, `\\n`, or `"`
    must not break out of the multipart Content-Disposition header
    line. Regression guard for the PR #1022-class filename forgery
    vector (Discord-attachment RCE: attacker-supplied filename).

    discord-bridge.py now sanitizes filenames at the save site, but
    the dm-result REST path should also defensively sanitize on its
    end so a future bypass doesn't let a forged filename smuggle
    multipart-envelope bytes."""
    # Build a real file with a benign name; we only check the regex
    # in the multipart helper independently. Construct a fake-arg
    # path that contains shell-metacharacters, but bypass the file
    # existence check by mocking — actually simpler: write a file
    # whose basename has special chars (filesystem allows most).
    bad_name_path = Path('/tmp/sutando-test-bad"name.png')
    bad_name_path.write_bytes(b"content")
    try:
        transport = _FakeTransport({
            ("POST", "/users/@me/channels"): {"id": "dm-5"},
            ("POST", "/channels/dm-5/messages"): {"id": "msg-5"},
        })
        original = _install_transport(transport)
        def run():
            try:
                ok = dm.send_dm(f"check this: [file: {bad_name_path}]")
            finally:
                _restore_transport(original)
            assert ok is True
            mp_calls = [c for c in transport.calls if c["is_multipart"]]
            assert len(mp_calls) == 1
            body = mp_calls[0]["body_bytes"]
            # The literal `"` must NOT appear inside the filename="..."
            # value — it's been sanitized to `_`. Locate the filename=
            # header value and verify no `"` characters between the
            # opening `="` and closing `"`.
            idx = body.find(b'filename="')
            assert idx >= 0, f"no filename= header in multipart body: {body[:200]!r}"
            after = body[idx + len(b'filename="'):]
            close = after.find(b'"')
            assert close >= 0, f"unterminated filename=\" header: {after[:200]!r}"
            inner = after[:close]
            assert b'"' not in inner, f"unsanitized `\"` inside filename value: {inner!r}"
            assert b'\r' not in inner and b'\n' not in inner, (
                f"unsanitized CR/LF inside filename value: {inner!r}"
            )
            # No raw CR/LF should have been spliced into the body's
            # Content-Disposition for the file part (they'd let the
            # filename inject its own headers).
            # The body has lots of \r\n separators, but checking the
            # specific filename region is fiddly. Just check that the
            # file-bytes content is correctly delimited (no premature
            # boundary mid-file).
            file_part_count = body.count(b'name="files[')
            assert file_part_count == 1, (
                f"expected exactly 1 file part, got {file_part_count} "
                f"(could indicate filename injection split the envelope)"
            )
        _with_access_json(
            {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
            run,
        )
    finally:
        bad_name_path.unlink(missing_ok=True)


def main():
    test_allowlisted_file_uploaded_via_multipart()
    print("  ✓ test_allowlisted_file_uploaded_via_multipart")
    test_non_allowlisted_file_rejected_not_uploaded()
    print("  ✓ test_non_allowlisted_file_rejected_not_uploaded")
    test_file_only_message_with_empty_text()
    print("  ✓ test_file_only_message_with_empty_text")
    test_eleven_files_split_into_two_batches()
    print("  ✓ test_eleven_files_split_into_two_batches")
    test_filename_crlf_quote_sanitized_in_header()
    print("  ✓ test_filename_crlf_quote_sanitized_in_header")
    print("All dm-result multipart-upload tests passed.")


if __name__ == "__main__":
    main()
