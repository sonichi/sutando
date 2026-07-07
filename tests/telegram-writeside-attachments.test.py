#!/usr/bin/env python3
"""Telegram write-side media-attachment integration — interaction-model 4D, step 1.5.

After the rule-of-three dedup, telegram builds its refs inline and uses the
shared local_task_protocol.media_attachment_headers (the header logic + mime
mapping are tested in tests/local-task-protocol-attachments.test.py). Telegram
has no remaining bridge-specific pure helper, so this asserts the module loads
cleanly after the dedup (import + shared-helper use) and that a telegram-shaped
task-mid file round-trips through the parsers with the legacy body line intact.

telegram-bridge only warns (doesn't exit) without a token, so the load is easy.

Run: python3 tests/telegram-writeside-attachments.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


ltp = _load("local_task_protocol", REPO / "src" / "local_task_protocol.py")
tg = _load("tgbridge_ws", REPO / "src" / "telegram-bridge.py")

# ── 1. telegram no longer carries local header helpers (folded into LTP) ──
check("telegram module loads after dedup", tg is not None)
check("local _media_attachment_headers removed", not hasattr(tg, "_media_attachment_headers"))
check("local _modality_for_mime removed", not hasattr(tg, "_modality_for_mime"))

# ── 2. round-trip through a realistic telegram task-mid file (shared helper) ──
photo = ltp.AttachmentRef(locator="/tmp/tg/1_photo.jpg", mime="image/jpeg",
                          filename="1_photo.jpg", size=88123)
doc = ltp.AttachmentRef(locator="/tmp/tg/2_report.pdf", mime="application/pdf",
                        filename="report.pdf", size=40100)
task = (
    "id: task-tg\n"
    "timestamp: 2026-07-07T08:00:00Z\n"
    "task: [Telegram @qingyun] look at this\n[Photo attached: /tmp/tg/1_photo.jpg]\n"
    "source: telegram\n"
    "interaction_type: message\n"
    f"{ltp.media_attachment_headers([photo], True)}"
    "chat_id: 55\n"
    "priority: normal\n"
)
hp = ltp.parse_task_headers_trusted(task)
atts = ltp.parse_attachments(hp.headers)
check("round-trip: attachment parses back",
      len(atts) == 1 and atts[0].locator == photo.locator and atts[0].mime == "image/jpeg"
      and atts[0].size == 88123, str(atts))
check("round-trip: modalities", ltp.parse_content_modalities(hp.headers) == frozenset({"text", "image"}))
check("round-trip: media_form", ltp.parse_media_form(hp.headers) == "attachment")
check("dual-write: legacy [Photo attached:] survives in body",
      "[Photo attached: /tmp/tg/1_photo.jpg]" in hp.body)
check("attachments header not left in body", "attachments:" not in hp.body)
# document → file modality (the owner's "what about files?" case) via the shared helper
check("document → file modality",
      "content_modalities: file\n" in ltp.media_attachment_headers([doc], False))

# ── 3. injection gate: the three keys defanged if forged in an untrusted body ──
gd = _load("task_body_guard_tg", REPO / "src" / "task_body_guard.py")
for k in ("attachments", "content_modalities", "media_form"):
    out = gd.confine_user_content(f"hi\n{k}: forged")
    check(f"forged `{k}:` body line defanged",
          not any(l.startswith(k + ":") for l in out.split("\n")))

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — telegram write-side media-attachment integration")
