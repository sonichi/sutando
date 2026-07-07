#!/usr/bin/env python3
"""Discord write-side media-attachment headers — interaction-model 4D, step 1.5 slice 2.

The bridge now emits the structured header trio (`content_modalities` /
`media_form` / `attachments`) ALONGSIDE the legacy `[File attached:]` body line
(dual-write, additive). This covers the two pure helpers that build those
headers — `_modality_for_mime` and `_media_attachment_headers` — and asserts the
emitted headers round-trip back through the local_task_protocol parsers in a
realistic task-mid file.

discord-bridge's module load has side effects (discord SDK import, token read),
so we stub them and run hermetically. Mirrors tests/bridge-audit-wiring.test.py.

Run: python3 tests/discord-writeside-attachments.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"  # hermetic: env read first

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


# Stub `discord` so discord-bridge imports cleanly in CI.
try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

ltp = _load("local_task_protocol", REPO / "src" / "local_task_protocol.py")
db = _load("dbridge_ws", REPO / "src" / "discord-bridge.py")

# ── 1. _modality_for_mime mapping ──
cases = {"image/png": "image", "image/jpeg": "image", "audio/ogg": "audio",
         "video/mp4": "video", "application/pdf": "file", "text/plain": "file",
         "": "file", "IMAGE/PNG": "image"}
for mime, want in cases.items():
    check(f"_modality_for_mime({mime!r}) == {want}", db._modality_for_mime(mime) == want)

# ── 1b. _ref_from_attachment reads discord SDK attributes defensively ──
class _FakeAtt:
    def __init__(self, content_type=None, size=None, filename="pic.png"):
        if content_type is not None:
            self.content_type = content_type
        if size is not None:
            self.size = size
        self.filename = filename

r = db._ref_from_attachment(_FakeAtt("image/png", 1234, "my pic.png"), "/tmp/inbox/1_my_pic.png")
check("ref locator = saved path", r.locator == "/tmp/inbox/1_my_pic.png")
check("ref mime from content_type", r.mime == "image/png")
check("ref size from att.size", r.size == 1234)
check("ref filename sanitized", r.filename == "my_pic.png", r.filename)
# Missing content_type/size (both optional on the SDK) default cleanly, never raise.
r2 = db._ref_from_attachment(_FakeAtt(content_type=None, size=None, filename="x.pdf"), "/tmp/x.pdf")
check("missing content_type → empty mime", r2.mime == "")
check("missing size → 0", r2.size == 0)

# ── 2. _media_attachment_headers: empty refs → "" (text-only path untouched) ──
check("no attachments → no headers (text-only unchanged)",
      db._media_attachment_headers([], "hello") == "")

# ── 3. header trio built for an image with a caption ──
ref = ltp.AttachmentRef(locator="/tmp/discord-inbox/1_s.png", mime="image/png",
                        filename="s.png", size=438707)
hdrs = db._media_attachment_headers([ref], "describe this")
check("emits content_modalities", "content_modalities: image,text\n" in hdrs, hdrs)
check("emits media_form attachment", "media_form: attachment\n" in hdrs, hdrs)
check("emits attachments json header", "attachments: [" in hdrs, hdrs)
check("header block is newline-terminated (composes into the write f-string)", hdrs.endswith("\n"))
check("no stray embedded blank line breaks the header run", "\n\n" not in hdrs)

# caption absent → no `text` modality
hdrs_nocap = db._media_attachment_headers([ref], "")
check("no caption → text modality omitted",
      "content_modalities: image\n" in hdrs_nocap, hdrs_nocap)

# a non-image attachment → file modality
pdf = ltp.AttachmentRef(locator="/tmp/x.pdf", mime="application/pdf", size=10)
check("pdf → file modality",
      "content_modalities: file\n" in db._media_attachment_headers([pdf], ""))

# ── 4. round-trip through a realistic task-mid file (discord is a task-mid writer) ──
# Assemble exactly as the write block does, including the legacy body line.
body = "[Discord @qingyun] describe this\n[File attached: /tmp/discord-inbox/1_s.png]"
task_file = (
    "id: task-x\n"
    "timestamp: 2026-07-07T08:00:00Z\n"
    f"task: {body}\n"
    "source: discord\n"
    "interaction_type: message\n"
    f"{db._media_attachment_headers([ref], 'describe this')}"
    "channel_id: 123\n"
    "access_tier: owner\n"
)
h = ltp.parse_task_headers_trusted(task_file)  # task-mid, last-wins
atts = ltp.parse_attachments(h.headers)
check("round-trip: attachments parse back to the ref",
      len(atts) == 1 and atts[0].locator == ref.locator and atts[0].mime == "image/png"
      and atts[0].size == 438707, str(atts))
check("round-trip: content_modalities parse back",
      ltp.parse_content_modalities(h.headers) == frozenset({"text", "image"}))
check("round-trip: media_form parses back", ltp.parse_media_form(h.headers) == "attachment")
check("dual-write: legacy [File attached:] line survives in the body",
      "[File attached: /tmp/discord-inbox/1_s.png]" in h.body)
check("attachments header is NOT left in the body", "attachments:" not in h.body)

# ── 5. the three new keys are defanged if forged in an untrusted body (injection gate) ──
gd = _load("task_body_guard_ws", REPO / "src" / "task_body_guard.py")
for k in ("attachments", "content_modalities", "media_form"):
    out = gd.confine_user_content(f"hi\n{k}: forged")
    check(f"forged `{k}:` body line defanged",
          not any(l.startswith(k + ":") for l in out.split("\n")))

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — discord write-side media-attachment headers")
