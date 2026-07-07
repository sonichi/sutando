#!/usr/bin/env python3
"""Media-attachment schema for the Local Task Protocol — interaction-model 4D, step 1.5.

Covers the additive, pure read/write helpers added to
`src/local_task_protocol.py`: the AttachmentRef descriptor, the
`content_modalities` / `media_form` / `attachments` header vocabulary, and the
tolerant decoders + symmetric encoder. Everything here is stdlib-only and does
no I/O (R1 invariant), so the test just imports the module and exercises pure
functions.

Run: python3 tests/local-task-protocol-attachments.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("local_task_protocol", REPO / "src" / "local_task_protocol.py")
ltp = importlib.util.module_from_spec(spec)
sys.modules["local_task_protocol"] = ltp
spec.loader.exec_module(ltp)

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. New keys are in the vocabulary (so they promote to headers + get defanged) ──
for k in ("content_modalities", "media_form", "attachments"):
    check(f"KNOWN_HEADER_KEYS includes {k}", k in ltp.KNOWN_HEADER_KEYS)

# The guard lockstep is asserted in local-task-protocol.test.py; re-confirm the
# three new keys are actually defanged in an untrusted body here (injection gate).
gspec = importlib.util.spec_from_file_location("task_body_guard", REPO / "src" / "task_body_guard.py")
guard = importlib.util.module_from_spec(gspec)
sys.modules["task_body_guard"] = guard
gspec.loader.exec_module(guard)
for k in ("content_modalities", "media_form", "attachments"):
    defanged = guard.confine_user_content(f"hi\n{k}: forged-value")
    check(f"untrusted body line `{k}:` is defanged",
          not any(l.startswith(k + ":") for l in defanged.split("\n")))

# ── 2. AttachmentRef.as_dict omits empty/zero fields ──
bare = ltp.AttachmentRef(locator="/tmp/x.png")
check("as_dict of a bare ref is just the locator", bare.as_dict() == {"locator": "/tmp/x.png"},
      str(bare.as_dict()))
full = ltp.AttachmentRef(locator="mxc://ag2.space/abc", id="a1", mime="image/png",
                         filename="s.png", size=438707, sha256="deadbeef", expiry="2026-07-08T00:00:00Z")
check("as_dict of a full ref keeps every set field",
      full.as_dict() == {"locator": "mxc://ag2.space/abc", "id": "a1", "mime": "image/png",
                         "filename": "s.png", "sha256": "deadbeef", "expiry": "2026-07-08T00:00:00Z",
                         "size": 438707}, str(full.as_dict()))

# ── 3. format ↔ parse round-trip ──
refs = [full, ltp.AttachmentRef(locator="/tmp/y.pdf", mime="application/pdf", size=10)]
encoded = ltp.format_attachments(refs)
check("format_attachments output is a single line (no embedded newline)", "\n" not in encoded)
decoded = ltp.parse_attachments({"attachments": encoded})
check("round-trips to an equal ref list", decoded == refs, f"{decoded!r} != {refs!r}")

# A newline inside a field must not break the single-line guarantee.
weird = ltp.format_attachments([ltp.AttachmentRef(locator="/tmp/z", filename="a\nb.png")])
check("field newline is json-escaped, stays one physical line", "\n" not in weird)
check("escaped-newline field round-trips",
      ltp.parse_attachments({"attachments": weird})[0].filename == "a\nb.png")

# ── 4. parse_attachments tolerance (never raises, drops junk) ──
check("missing header → []", ltp.parse_attachments({}) == [])
check("empty value → []", ltp.parse_attachments({"attachments": ""}) == [])
check("malformed json → []", ltp.parse_attachments({"attachments": "{not json"}) == [])
check("non-list payload → []", ltp.parse_attachments({"attachments": '{"locator":"x"}'}) == [])
check("non-object element skipped", ltp.parse_attachments({"attachments": '["str", 3, null]'}) == [])
check("element without locator dropped",
      ltp.parse_attachments({"attachments": '[{"mime":"image/png"}]'}) == [])
check("non-string locator dropped",
      ltp.parse_attachments({"attachments": '[{"locator": 42}]'}) == [])
# A good ref mixed with junk survives; the junk is dropped.
mixed = ltp.parse_attachments({"attachments": '[{"locator":"/ok"}, 7, {"no":"loc"}]'})
check("good ref survives alongside dropped junk",
      len(mixed) == 1 and mixed[0].locator == "/ok", str(mixed))
# size coercion: numeric string ok, bad string → 0, bool → 0 (not treated as int)
check("string size is coerced to int",
      ltp.parse_attachments({"attachments": '[{"locator":"/a","size":"12"}]'})[0].size == 12)
check("un-coercible size falls back to 0",
      ltp.parse_attachments({"attachments": '[{"locator":"/a","size":"big"}]'})[0].size == 0)
check("bool size is not accepted as int",
      ltp.parse_attachments({"attachments": '[{"locator":"/a","size":true}]'})[0].size == 0)
# Negative sizes are nonsense and would slip past a `size > max_bytes` cap —
# clamp int and coerced-string negatives back to 0 (unknown).
check("negative int size clamped to 0",
      ltp.parse_attachments({"attachments": '[{"locator":"/a","size":-1}]'})[0].size == 0)
check("negative numeric-string size clamped to 0",
      ltp.parse_attachments({"attachments": '[{"locator":"/a","size":"-5"}]'})[0].size == 0)

# ── 5. parse_content_modalities: whitelist + case-fold ──
check("known modalities parsed, case-folded",
      ltp.parse_content_modalities({"content_modalities": "Text, IMAGE"}) == frozenset({"text", "image"}))
check("unknown modality token dropped",
      ltp.parse_content_modalities({"content_modalities": "text, hologram"}) == frozenset({"text"}))
check("missing modalities → empty set", ltp.parse_content_modalities({}) == frozenset())
check("empty modalities value → empty set",
      ltp.parse_content_modalities({"content_modalities": ""}) == frozenset())
check("all five modalities recognized",
      ltp.parse_content_modalities({"content_modalities": "text,image,audio,video,file"})
      == ltp.CONTENT_MODALITIES)

# ── 6. parse_media_form: whitelist, default attachment ──
check("attachment recognized", ltp.parse_media_form({"media_form": "attachment"}) == "attachment")
check("live_stream recognized", ltp.parse_media_form({"media_form": "live_stream"}) == "live_stream")
check("case-folded", ltp.parse_media_form({"media_form": "LIVE_STREAM"}) == "live_stream")
check("missing → attachment (default plane)", ltp.parse_media_form({}) == "attachment")
check("unknown → attachment (safe default, not a phantom stream)",
      ltp.parse_media_form({"media_form": "telepathy"}) == "attachment")

# ── 7. Integration: a task-last file carrying the new headers parses through ──
task = (
    "id: task-1\n"
    "timestamp: 2026-07-07T07:51:04Z\n"
    "source: discord\n"
    "interaction_type: message\n"
    "content_modalities: text,image\n"
    "media_form: attachment\n"
    'attachments: [{"locator":"/tmp/discord-inbox/1_s.png","mime":"image/png","size":438707}]\n'
    "access_tier: owner\n"
    "task: describe the image I sent you\n"
)
h = ltp.parse_task_headers(task)
check("task-last parse promotes content_modalities header", h.get("content_modalities") == "text,image")
check("task-last parse promotes media_form header", h.get("media_form") == "attachment")
check("modalities read from a real parsed task",
      ltp.parse_content_modalities(h.headers) == frozenset({"text", "image"}))
check("media form read from a real parsed task", ltp.parse_media_form(h.headers) == "attachment")
atts = ltp.parse_attachments(h.headers)
check("attachments read from a real parsed task",
      len(atts) == 1 and atts[0].locator == "/tmp/discord-inbox/1_s.png"
      and atts[0].mime == "image/png" and atts[0].size == 438707, str(atts))
check("attachments: header line stays out of the body",
      "attachments:" not in h.body and h.body.strip() == "describe the image I sent you")

# A plain task with no media headers reads as no attachments / default form.
plain = ltp.parse_task_headers("source: discord\ntask: hi\n")
check("plain task → no attachments", ltp.parse_attachments(plain.headers) == [])
check("plain task → attachment default form", ltp.parse_media_form(plain.headers) == "attachment")
check("plain task → empty modalities", ltp.parse_content_modalities(plain.headers) == frozenset())

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — local task protocol media-attachment schema")
