#!/usr/bin/env python3
"""The 5-minute poll ceiling must clear its interval and say so.

`/result` answers `pending` for a torn or empty body, so without the deadline
the poll runs forever and the owner is never told. Runs the EXACT `sendText`
source under a fake clock; a mutation disabling the deadline must fail this.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()


def _send_text_source() -> str:
    marker = "function sendText()"
    assert marker in SOURCE, "web-client has no sendText()"
    start = SOURCE.index(marker)
    end = SOURCE.index("\n}", start) + 2
    body = SOURCE[start:end]
    assert "deadline" in body, "extracted sendText has no deadline — wrong span"
    return body


HARNESS = r"""
let now = 1_000_000;
const Real = Date;
Date.now = () => now;
let timer = null, cleared = false, nextId = 1;
function setInterval(fn, ms) { timer = {fn, ms, id: nextId++}; return timer.id; }
function clearInterval(id) { if (timer && timer.id === id) cleared = true; }
const appended = [];
function mkEl() { return {className:'', textContent:'', innerHTML:'',
                          appendChild(){}, }; }
const document = { createElement: () => mkEl() };
const transcript = { appendChild: (e) => appended.push(e) };
const input = { value: 'hello', trim: () => 'hello' };
function $(id) { return id === 'textInput' ? input : transcript; }
function dbg() {} function scrollTranscript() {} function addCopyBtn() {}
let currentUserEl = null;
const voice = null;
const location = { hostname: 'localhost' };
const window = {};
let resultStatus = 'pending';
function fetch(url) {
  if (url.endsWith('/task')) return Promise.resolve({json: () => Promise.resolve({ok:true, task_id:'T1'})});
  return Promise.resolve({json: () => Promise.resolve(
      resultStatus === 'completed' ? {status:'completed', result:'THE ANSWER'} : {status:'pending'})});
}
const flush = () => new Promise(r => setImmediate(r));
__SEND_TEXT__
(async () => {
  sendText();
  await flush(); await flush(); await flush();
  if (!timer) { console.log(JSON.stringify({error:'poll never armed'})); return; }
  __SCENARIO__
  console.log(JSON.stringify({
    cleared,
    timedOut: appended.some(e => (e.textContent||'').includes('timed out after 5 minutes')),
    answered: appended.some(e => (e.textContent||'') === 'THE ANSWER'),
  }));
})();
"""

SCENARIOS = {
    "timeout": "now += 300001; timer.fn(); await flush();",
    "completion": "resultStatus = 'completed'; now += 2000; timer.fn(); await flush(); await flush();",
}


def run(scenario: str, disable_deadline: bool = False) -> dict:
    src = _send_text_source()
    if disable_deadline:
        old = "if (Date.now() > deadline) {"
        assert src.count(old) == 1, "deadline guard not found — mutation would be a no-op"
        src = src.replace(old, "if (false) {", 1)
    probe = (HARNESS.replace("__SEND_TEXT__", src)
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
check(r.get("cleared") is True, f"past the deadline the interval is cleared, got {r!r}")
check(r.get("timedOut") is True, f"...and the owner is told it timed out, got {r!r}")

r = run("completion")
check(r.get("cleared") is True, f"a pre-deadline completion clears the interval, got {r!r}")
check(r.get("answered") is True and not r.get("timedOut"),
      f"...and renders the answer without a timeout notice, got {r!r}")

# Control: disabling the deadline must break the first pair and nothing else.
r = run("timeout", disable_deadline=True)
check(r.get("cleared") is False and r.get("timedOut") is False,
      f"CONTROL: with the deadline disabled the poll never stops, got {r!r}")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
