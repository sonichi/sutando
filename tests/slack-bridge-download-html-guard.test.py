#!/usr/bin/env python3
"""
Behavioral test for src/slack-bridge.py's _download_slack_file() HTML guard.

Regression: when the Slack bot token lacks the `files:read` scope, Slack does
NOT return an HTTP error for a url_private_download fetch — it 200s with an HTML
sign-in page. The bridge previously wrote that page straight to disk under the
attachment's name (e.g. "…-voice.m4a"), silently corrupting the attachment:
downstream transcription then "transcribed" a login page ("Please provide the
voice note you would like me to transcribe.").

The guard: detect an HTML response (Content-Type text/html OR a `<!doctype html`
body) and return None WITHOUT persisting the file, so the caller cleanly treats
it as "no attachment" and can surface the real cause (missing files:read).

This test monkey-patches urllib.request.urlopen so no network is touched.

Run: python3 tests/slack-bridge-download-html-guard.test.py
Exit code: 0 on pass / skip, 1 on fail.
"""

import io
import os
import sys
import tempfile
import types
import urllib.request
from pathlib import Path


class _StubApp:
    def __init__(self, *a, **kw):
        self.client = types.SimpleNamespace()

    def event(self, _name):
        def decorator(fn):
            return fn
        return decorator


def _load_module():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-for-helper-only")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token-for-helper-only")
    os.environ.setdefault(
        "SUTANDO_WORKSPACE", tempfile.mkdtemp(prefix="sutando-test-slack-dl-")
    )
    try:
        import slack_bolt as _real_bolt
        _real_bolt.App = _StubApp
    except ImportError:
        stub_bolt = types.ModuleType("slack_bolt")
        stub_bolt.App = _StubApp
        sys.modules["slack_bolt"] = stub_bolt
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod
    if "slack_bolt.adapter.socket_mode" not in sys.modules:
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod

    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    bridge_path = repo / "src" / "slack-bridge.py"
    if not bridge_path.exists():
        print(f"FAIL: {bridge_path} not found", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_under_test", bridge_path
    )
    sys.path.insert(0, str(repo / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """Minimal context-manager stand-in for an http.client.HTTPResponse."""

    def __init__(self, body: bytes, content_type: str):
        self._body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(mod, resp: _FakeResp):
    def fake_urlopen(req, timeout=None):
        return resp
    mod.urllib.request.urlopen = fake_urlopen


def main() -> int:
    try:
        mod = _load_module()
    except Exception as e:
        print(f"FAIL: could not load slack-bridge.py for testing: {e}", file=sys.stderr)
        return 1

    download = mod._download_slack_file
    inbox = Path(mod.INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)
    orig_urlopen = mod.urllib.request.urlopen
    failures = []

    file_dict = {
        "url_private_download": "https://files.slack.com/files-pri/T-x/download/voice.m4a",
        "name": "Audio Clip.m4a",
        "id": "F123",
    }

    try:
        # --- Case 1: missing files:read → Slack serves an HTML sign-in page.
        html = (
            b'<!DOCTYPE html><html lang="en-US"><head><title>Slack</title></head>'
            b"<body>sign in</body></html>"
        )
        _patch_urlopen(mod, _FakeResp(html, "text/html; charset=utf-8"))
        before = set(inbox.glob("*"))
        result = download(dict(file_dict))
        after = set(inbox.glob("*"))
        if result is not None:
            failures.append(
                f"HTML response should return None, got {result!r}"
            )
        if after - before:
            failures.append(
                f"HTML response must not persist a file; new files: {after - before}"
            )

        # --- Case 1b: HTML detected by body sniff even when Content-Type lies
        # (Slack CDN sometimes serves the login page as application/octet-stream).
        _patch_urlopen(mod, _FakeResp(html, "application/octet-stream"))
        before = set(inbox.glob("*"))
        result = download(dict(file_dict))
        after = set(inbox.glob("*"))
        if result is not None:
            failures.append(
                f"HTML body (octet-stream ctype) should return None, got {result!r}"
            )
        if after - before:
            failures.append(
                f"HTML body must not persist a file; new files: {after - before}"
            )

        # --- Case 2: real audio bytes → persisted and path returned.
        audio = b"\x00\x00\x00\x20ftypM4A " + b"\x11" * 512  # not HTML
        _patch_urlopen(mod, _FakeResp(audio, "audio/mp4"))
        result = download(dict(file_dict))
        if result is None:
            failures.append("real audio bytes should be saved, got None")
        else:
            saved = Path(result)
            try:
                if not saved.exists():
                    failures.append(f"returned path does not exist: {result}")
                elif saved.read_bytes() != audio:
                    failures.append("saved bytes differ from downloaded bytes")
            finally:
                # INBOX_DIR resolves to the real workspace inbox (SUTANDO_WORKSPACE
                # is no longer honored), so clean up the file we just wrote.
                saved.unlink(missing_ok=True)
    finally:
        mod.urllib.request.urlopen = orig_urlopen

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("PASS: _download_slack_file rejects HTML sign-in pages, saves real files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
