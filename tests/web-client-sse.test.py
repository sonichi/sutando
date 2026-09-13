#!/usr/bin/env python3
"""Behavioral regression coverage for native EventSource reconnect handling."""

import json
import subprocess
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "src" / "web-client.ts").read_text()


def run_probe() -> dict:
        start = SOURCE.index("let _sseSource = null;")
        end = SOURCE.index("// ─── State", start)
        browser_code = SOURCE[start:end]
        probe = r"""
const sources = [];
class EventSource {
    constructor() { this.closed = false; sources.push(this); }
    addEventListener() {}
    close() { this.closed = true; }
}
globalThis.EventSource = EventSource;
globalThis.document = { visibilityState: 'hidden', addEventListener(name, callback) { this.callback = callback; } };
globalThis.toggle = () => {};
globalThis.toggleMute = () => {};
""" + browser_code + r"""
const first = sources[0];
first.onerror();
const countAfterError = sources.length;
document.visibilityState = 'visible';
document.callback();
process.stdout.write(JSON.stringify({countAfterError, total: sources.length, firstClosed: first.closed}));
"""
        result = subprocess.run(["node", "-e", probe], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        return json.loads(result.stdout)


def test_transient_sse_errors_use_native_reconnect():
    assert run_probe() == {"countAfterError": 1, "total": 2, "firstClosed": True}


if __name__ == "__main__":
    test_transient_sse_errors_use_native_reconnect()
    print("web-client SSE tests passed")