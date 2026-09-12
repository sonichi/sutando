#!/usr/bin/env python3
"""The chat-reply poll ceiling must stop the timer and say so.

`/result` answers `pending` for a torn or empty body, so without the ceiling the
poll runs forever and the owner is never told. Runs the EXACT `pollChatReply`
source under a fake clock; a mutation disabling the ceiling must fail this.

The ceiling used to sit inline in `sendText` as a `deadline`; it now lives in
`pollChatReply` behind `CHAT_POLL_MAX_MS`, which also survives a page reload.
The property is unchanged — only where it is asserted moved with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()


def _poll_source() -> str:
    marker = "function pollChatReply("
    assert marker in SOURCE, "web-client has no pollChatReply()"
    start = SOURCE.index(marker)
    end = SOURCE.index("\n}", start) + 2
    body = SOURCE[start:end]
    assert "CHAT_POLL_MAX_MS" in body, "extracted pollChatReply has no ceiling — wrong span"
    return body


HARNESS = r"""
let now = 1_000_000;
Date.now = () => now;
let timer = null, stopped = false, nextId = 1;
function setTimeout(fn, ms) { timer = {fn, ms, id: nextId++}; return timer.id; }
function clearTimeout(id) { if (timer && timer.id === id) stopped = true; }
const CHAT_POLL_FAST_MS = 2 * 1000;
const CHAT_POLL_SLOW_MS = 15 * 1000;
const CHAT_POLL_FAST_WINDOW_MS = 2 * 60 * 1000;
const CHAT_POLL_MAX_MS = 30 * 60 * 1000;
const placeholder = {
  className: 't-entry t-assistant t-working',
  textContent: 'working…',
  classList: {
    contains(c) { return placeholder.className.includes(c); },
    remove(c) { placeholder.className = placeholder.className.replace(c, '').trim(); },
  },
};
let rendered = null;
let removedPending = false;
function renderChatReply(el, text) { rendered = text; }
function removePendingChatSend() { removedPending = true; }
function scrollTranscript() {}
const location = { hostname: 'localhost' };
let resultStatus = 'pending';
function fetch(url) {
  return Promise.resolve({json: () => Promise.resolve(
      resultStatus === 'completed' ? {status:'completed', result:'THE ANSWER'} : {status:'pending'})});
}
const flush = () => new Promise(r => setImmediate(r));
__POLL_CHAT_REPLY__
(async () => {
  pollChatReply('T1', placeholder);
  await flush(); await flush(); await flush();
  if (!timer) { console.log(JSON.stringify({error:'poll never armed'})); return; }
  __SCENARIO__
  console.log(JSON.stringify({
    stopped,
    timedOut: (placeholder.textContent || '').includes('Still working'),
    answered: rendered === 'THE ANSWER',
  }));
})();
"""

SCENARIOS = {
    # past the 30-minute ceiling
    "timeout": "now += 30 * 60 * 1000 + 1; timer.fn(); await flush();",
    "completion": "resultStatus = 'completed'; now += 2000; timer.fn(); await flush(); await flush();",
}


def run(scenario: str, disable_ceiling: bool = False) -> dict:
    src = _poll_source()
    if disable_ceiling:
        old = "if (elapsed > CHAT_POLL_MAX_MS) {"
        assert src.count(old) == 1, "ceiling guard not found — mutation would be a no-op"
        src = src.replace(old, "if (false) {", 1)
    probe = (HARNESS.replace("__POLL_CHAT_REPLY__", src)
                    .replace("__SCENARIO__", SCENARIOS[scenario]))
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


failures = []


def check(ok: bool, msg: str) -> None:
    print(("ok: " if ok else "FAIL: ") + msg)
    if not ok:
        failures.append(msg)


r = run("timeout")
check(r.get("stopped") is True, f"past the ceiling the timer is stopped, got {r!r}")
check(r.get("timedOut") is True, f"...and the owner is told it is still working, got {r!r}")

r = run("completion")
check(r.get("stopped") is True, f"a pre-ceiling completion stops the timer, got {r!r}")
check(r.get("answered") is True and not r.get("timedOut"),
      f"...and renders the answer without the still-working notice, got {r!r}")

# Control: disabling the ceiling must break the first pair and nothing else.
r = run("timeout", disable_ceiling=True)
check(r.get("stopped") is False and r.get("timedOut") is False,
      f"CONTROL: with the ceiling disabled the poll never stops, got {r!r}")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
