#!/usr/bin/env python3
"""
Behavioral tests for telegram-bridge inbound attachments over the Bot API's
20 MB download limit: the file is refused before download, the sender is told
why and what to do, and the agent's task names the file that did not arrive.

Run: python3 tests/telegram-bridge-inbound-too-large.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import io
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


# Isolate channel config before import: the bridge resolves it at module level.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-too-large-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')


def load_bridge_module():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-placeholder-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge", REPO / "src" / "telegram-bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


bridge = load_bridge_module()
failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


MB = 1024 * 1024
calls = []


def fake_api(result):
    def api(method, **params):
        calls.append(method)
        return result
    return api


# A known size over the limit is refused without asking Telegram for the file.
calls.clear()
bridge.api = fake_api({"ok": True, "result": {"file_path": "documents/x.mp4"}})
check("over the limit is too_large", bridge.fetch_inbound("f1", "talk.mp4", 25 * MB) == (None, "too_large"))
check("over the limit never calls getFile", calls == [])

# Exactly at the limit is still fetched.
calls.clear()
bridge._save_telegram_file = lambda file_path, name_hint="file": "/inbox/1.mp4"
check("at the limit downloads", bridge.fetch_inbound("f2", "clip.mp4", 20 * MB) == ("/inbox/1.mp4", None))
check("at the limit calls getFile", calls == ["getFile"])

# No size in the update: Telegram's own refusal is recognised.
bridge.api = fake_api({"ok": False, "description": "Bad Request: file is too big"})
check("getFile 'file is too big' is too_large", bridge.fetch_inbound("f3", "big.zip", 0) == (None, "too_large"))

# Any other failure is not reported as a size problem.
bridge.api = fake_api({"ok": False, "description": "Bad Request: wrong file_id"})
check("other getFile failure is failed", bridge.fetch_inbound("f4", "x.pdf", 1 * MB) == (None, "failed"))
bridge.api = fake_api({"ok": True, "result": {"file_path": "documents/x.pdf"}})
bridge._save_telegram_file = lambda file_path, name_hint="file": None
check("a failed save is failed", bridge.fetch_inbound("f5", "x.pdf", 1 * MB) == (None, "failed"))

# What the sender is told.
reply = bridge.skipped_files_reply([("talk.mp4", 25 * MB, "too_large")])
check("reply names the file and size", "talk.mp4 (25 MB)" in reply)
check("reply states the 20 MB limit", "20 MB" in reply)
check("reply says what to do", "link" in reply and "AG2 Space" in reply)
mixed = bridge.skipped_files_reply([("a.mov", 0, "too_large"), ("b.pdf", 2 * MB, "failed")])
check("unknown size is stated, not 0 MB", "a.mov (size unknown)" in mixed)
check("a failed download asks to resend", "couldn't download b.pdf" in mixed)

# What the agent sees.
note = bridge.skipped_files_note([("talk.mp4", 25 * MB, "too_large")])
check("task note names the file and the limit", "[File not received: talk.mp4 (25 MB, over Telegram's 20 MB bot download limit)]" in note)
check("no skipped files, no note", bridge.skipped_files_note([]) == "")

# Videos, audio and video notes are downloaded like documents.
kinds = {k for k, _ in bridge.PLAIN_FILE_KINDS}
check("video, audio and video_note are handled", {"document", "video", "audio", "video_note"} <= kinds)

# api() keeps Telegram's error description so the size refusal can be recognised.
def raise_http(req, timeout=30):
    raise urllib.error.HTTPError(
        "https://api.telegram.org", 400, "Bad Request", {},
        io.BytesIO(b'{"ok":false,"error_code":400,"description":"Bad Request: file is too big"}'),
    )

fresh = load_bridge_module()
fresh.urllib.request.urlopen = raise_http
try:
    out = fresh.api("getFile", file_id="x")
finally:
    fresh.urllib.request.urlopen = urllib.request.urlopen
check("api() returns the error description", out == {"ok": False, "description": "Bad Request: file is too big"})

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
