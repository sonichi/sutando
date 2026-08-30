#!/usr/bin/env python3
"""Regression guard for native EventSource reconnect handling."""

from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "src" / "web-client.ts").read_text()


def test_transient_sse_errors_use_native_reconnect():
    assert "function initRemoteToggle(force = false)" in SOURCE
    assert "if (_sseSource && !force) return;" in SOURCE
    assert "_sseSource.onerror = () => {};" in SOURCE
    assert "setTimeout(() => initRemoteToggle(), 5000)" not in SOURCE


if __name__ == "__main__":
    test_transient_sse_errors_use_native_reconnect()
    print("web-client SSE tests passed")