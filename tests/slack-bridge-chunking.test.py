#!/usr/bin/env python3
"""Slack delivery uses the shared fence-aware chunker — Result Router S3.

Covers the `_send_reply` change in slack-bridge.py: long results are now split
with `chunk_message(clean_text, 4000)` instead of a naive `range(0, len, 4000)`
byte-slice. Slack posts default to mrkdwn, so the old slice broke a ```-fenced
block that spanned a 4000-char boundary (half-open code block). This test drives
a >4000-char fenced body through `_send_reply` and asserts every posted message
is fence-balanced and within the limit.

slack-bridge.py's module load needs slack_bolt + a token or it `sys.exit(1)`s,
so we stub the SDK and run against a temp workspace (SUTANDO_TEST_MODE=1).

Run: python3 tests/slack-bridge-chunking.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- Stub slack_bolt so module import doesn't need the real SDK / a live socket.
class _RecordingClient:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        return {"ok": True}


class _FakeApp:
    def __init__(self, token=None):
        self.client = _RecordingClient()

    # Bolt registers handlers via decorators at module import; return pass-throughs.
    def _decorator(self, *a, **k):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


_bolt = types.ModuleType("slack_bolt")
_bolt.App = _FakeApp
sys.modules["slack_bolt"] = _bolt
_adapter = types.ModuleType("slack_bolt.adapter")
_socket = types.ModuleType("slack_bolt.adapter.socket_mode")
_socket.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
sys.modules["slack_bolt.adapter"] = _adapter
sys.modules["slack_bolt.adapter.socket_mode"] = _socket

# Hermetic: temp workspace + tokens so module load doesn't touch real state / exit.
os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"

spec = importlib.util.spec_from_file_location("slackbridge", REPO / "src" / "slack-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`~][^`~]*)?\s*$")

# CommonMark-aware balance check, using the chunker's own close rule.
_mc_spec = importlib.util.spec_from_file_location("message_chunking", REPO / "src" / "message_chunking.py")
_mc = importlib.util.module_from_spec(_mc_spec)
_mc_spec.loader.exec_module(_mc)


def ends_outside_fence(chunk: str) -> bool:
    opener = None
    for line in chunk.split("\n"):
        if not _FENCE.match(line):
            continue
        fl = line.strip()
        if opener is None:
            opener = fl
        elif _mc._closes_fence(fl, opener):
            opener = None
    return opener is None


client = mod.app.client

# 1. Long fenced body (>4000 chars) → multiple posts, each fence-balanced + <=4000.
client.calls.clear()
big = "big log:\n```\n" + "\n".join(f"row {i}: " + "x" * 80 for i in range(120)) + "\n```\ndone"
assert len(big) > 4000, "test fixture must exceed the 4000 cap"
mod._send_reply("C0FAKECHAN", None, big)
texts = [c["text"] for c in client.calls]
check("slack: long body posted as multiple messages", len(texts) > 1, f"got {len(texts)}")
check("slack: every message <= 4000 chars", all(len(t) <= 4000 for t in texts),
      "lens=" + str([len(t) for t in texts]))
check("slack: every message fence-balanced (the mrkdwn code-block fix)",
      all(ends_outside_fence(t) for t in texts))
check("slack: channel + no thread_ts preserved", all(c["channel"] == "C0FAKECHAN" for c in client.calls))
check("slack: full row content survives across messages",
      sum(t.count("row ") for t in texts) == 120)

# 2. Short body → single message, unchanged (no spurious chunking).
client.calls.clear()
mod._send_reply("C0FAKECHAN", None, "just a short reply")
check("slack: short body → one message", len(client.calls) == 1)
check("slack: short body text intact", client.calls[0]["text"] == "just a short reply")

# Suppression is conditional on link density and happens at the send: stripping
# links from a digest would destroy the one thing it exists to deliver.
check("slack: link-free body keeps unfurling on",
      all(c.get("unfurl_links") is True for c in client.calls), repr(client.calls[:1]))

client.calls.clear()
mod._send_reply("C0FAKECHAN", None, "one link https://example.com/a here")
check("slack: single-link body keeps its preview",
      client.calls[0].get("unfurl_links") is True
      and client.calls[0].get("unfurl_media") is True, repr(client.calls[:1]))

client.calls.clear()
mod._send_reply("C0FAKECHAN", None, "https://example.com/a and https://example.com/b")
check("slack: link-dense body suppresses unfurling",
      client.calls[0].get("unfurl_links") is False
      and client.calls[0].get("unfurl_media") is False, repr(client.calls[:1]))

# A digest chunked at 4000 chars must not unfurl piecewise: the flag is decided
# on the whole body, so every chunk of a dense body carries the same False.
client.calls.clear()
mod._send_reply("C0FAKECHAN", None,
                "https://example.com/a\n" + ("filler line\n" * 900) + "https://example.com/b\n")
check("slack: dense body stays suppressed across every chunk",
      len(client.calls) > 1 and all(c.get("unfurl_links") is False for c in client.calls),
      f"chunks={len(client.calls)} flags={[c.get('unfurl_links') for c in client.calls]}")

client.calls.clear()
mod._send_reply("C0FAKECHAN", None, "just a short reply")

# 3. thread_ts is threaded through to each chunk.
client.calls.clear()
mod._send_reply("C0FAKECHAN", "1699999999.000100", "line\n" * 2000)  # long, forces >1 chunk
check("slack: threaded reply keeps thread_ts on every chunk",
      len(client.calls) > 1 and all(c.get("thread_ts") == "1699999999.000100" for c in client.calls))

# S5 wiring — _send_reply records a §7 audit line to the temp workspace ledger.
_audit = Path(os.environ["SUTANDO_WORKSPACE"]) / "state" / "result-audit.log"
check("slack wiring: _send_reply wrote a result-audit line", _audit.exists())
_atext = _audit.read_text() if _audit.exists() else ""
check("slack wiring: audit line records surface=slack", "\tslack" in _atext)
check("slack wiring: successful sends recorded 'delivered'", "\tdelivered\tslack" in _atext)

# redirect disposition: a [channel:] marker routes to a target → 'redirected'.
client.calls.clear()
mod._send_reply("C0ORIG", None, "[channel: C0TARGET]\nrerouted body", task_id="task-r")
check("slack wiring: [channel:] redirect records 'redirected'",
      "\tredirected\tslack" in _audit.read_text())
check("slack wiring: redirect actually posts to the target channel",
      any(c["channel"] == "C0TARGET" for c in client.calls))

# Skip-marker audit (§7): [no-send] and [deduped:] results are resolved
# deliveries — not silent voids — and each must get one audit line.
# Two layers tested:
#   1. _record_skip_audit() directly (covers the helper function body)
#   2. result_watcher() skip path (covers the refactored call site + condition lines)
_audit.write_text("")  # fresh slate for skip checks
mod._record_skip_audit("task-noslack", "no-send")
check("slack wiring: [no-send] writes no_send audit line",
      "\ttask-noslack\tno_send\tslack" in _audit.read_text())
mod._record_skip_audit("task-dedupslack", "deduped")
check("slack wiring: [deduped:] writes deduped audit line",
      "\ttask-dedupslack\tdeduped\tslack" in _audit.read_text())

# result_watcher integration: exercise the actual skip-path branch in the
# poll loop (lines 933-934, 938 in slack-bridge.py). Set up a pending task
# whose result file has [no-send], run the watcher in a daemon thread for
# one iteration, then verify the audit line was written.
_audit.write_text("")
_ws_task = "task-rw-skip-test"
(mod.RESULTS_DIR / f"{_ws_task}.txt").write_text("[no-send]\n")
with mod.pending_replies_lock:
    mod.pending_replies[_ws_task] = {
        "channel": "C0FAKE", "thread_ts": None,
        "access_tier": "unknown", "submitted_at": time.time(),
    }
_rw_thread = threading.Thread(target=mod.result_watcher, daemon=True)
_rw_thread.start()
time.sleep(0.3)  # first iteration completes before the 1s sleep
check("slack wiring: result_watcher [no-send] writes no_send via skip path",
      "\t" + _ws_task + "\tno_send\tslack" in _audit.read_text(),
      _audit.read_text() if _audit.exists() else "(no audit file)")

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — slack-bridge chunking tests")
