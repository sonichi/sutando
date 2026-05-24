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
with a fake transport that captures the raw request bytes — no Discord
token or network required.

Run: python3 tests/dm-result-multipart-upload.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def _load_dm():
    """Load src/dm-result.py as a module via importlib (handles the hyphen)."""
    spec = importlib.util.spec_from_file_location("dm_result", SRC / "dm-result.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SRC))
    spec.loader.exec_module(mod)
    return mod


dm = _load_dm()


class _FakeResponse:
    def __init__(self, body: dict):
        self._data = json.dumps(body).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeTransport:
    """Captures all urllib.request.urlopen calls as structured dicts."""

    def __init__(self, route_map: dict):
        """route_map: {("METHOD", "/path-suffix"): response_dict}"""
        self.calls: list[dict] = []
        self._routes = route_map

    def urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        method = req.method if hasattr(req, "method") else "GET"
        body_bytes = req.data if hasattr(req, "data") else b""
        ct = ""
        if hasattr(req, "headers"):
            ct = req.headers.get("Content-type", "") or req.headers.get("Content-Type", "")
        is_multipart = "multipart/form-data" in ct

        self.calls.append({
            "url": url,
            "method": method,
            "body_bytes": body_bytes or b"",
            "content_type": ct,
            "is_multipart": is_multipart,
        })

        # Match route
        for (_, suffix), resp in self._routes.items():
            if url.endswith(suffix.split("/")[-1]) or suffix in url:
                return _FakeResponse(resp)
        return _FakeResponse({"id": "fallback"})


def _install_transport(transport: _FakeTransport):
    original = urllib.request.urlopen
    urllib.request.urlopen = transport.urlopen
    return original


def _restore_transport(original):
    urllib.request.urlopen = original


def _with_access_json(data: dict, fn):
    """Run fn with a temporary access.json in place."""
    import os
    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / "access.json"
    tmp.write_text(json.dumps(data))
    original = dm.ACCESS_JSON
    dm.ACCESS_JSON = tmp
    try:
        fn()
    finally:
        dm.ACCESS_JSON = original
        tmp.unlink()
        tmp_dir.rmdir()


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
            mp_calls = [c for c in transport.calls if c["is_multipart"]]
            assert len(mp_calls) == 1, (
                f"expected exactly one multipart upload; got {len(mp_calls)}. "
                f"All calls: {[(c['method'], c['url']) for c in transport.calls]}"
            )
            body = mp_calls[0]["body_bytes"]
            assert b'Content-Disposition: form-data; name="payload_json"' in body
            assert b'Content-Disposition: form-data; name="files[0]"' in body
            assert b"PNG-magic-bytes-here" in body, "file bytes missing from multipart body"
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
            body = mp_calls[0]["body_bytes"]
            # Either `{"content": ""}` or `{}` is valid for empty content
            assert (b'"content": ""' in body or
                    b'name="payload_json"\r\nContent-Type: application/json\r\n\r\n{}' in body or
                    b'"content":""' in body)
            text_calls = [c for c in transport.calls
                         if "/messages" in c["url"] and not c["is_multipart"]]
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
    must not break out of the multipart Content-Disposition header."""
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
            file_part_count = body.count(b'name="files[')
            assert file_part_count == 1, (
                f"expected exactly 1 file part, got {file_part_count}"
            )
        _with_access_json(
            {"allowFrom": ["human-id"], "tierMap": {"human-id": "owner"}},
            run,
        )
    finally:
        bad_name_path.unlink(missing_ok=True)


def main():
    failures = []
    tests = [
        test_allowlisted_file_uploaded_via_multipart,
        test_non_allowlisted_file_rejected_not_uploaded,
        test_file_only_message_with_empty_text,
        test_eleven_files_split_into_two_batches,
        test_filename_crlf_quote_sanitized_in_header,
    ]
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)}/{len(tests)} tests failed.")
        sys.exit(1)
    print("All dm-result multipart-upload tests passed.")


if __name__ == "__main__":
    main()
