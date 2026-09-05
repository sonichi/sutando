#!/usr/bin/env python3
"""Gateway write-side platform_card header — trusted platform-metadata pointer.

The gateway may attach a signed pointer to the platform's canonical agent
operating card ({card_url, card_sha256, sig, key_id, alg}). The bridge
re-serializes exactly those subkeys as a one-line JSON header; the shared
KNOWN_HEADER_KEYS vocabulary promotes it on the parse side and defangs a
forged `platform_card:` line in untrusted bodies. Signature verification is
the consumer's job (skills/agent-room-ops/verify_platform_card.py), not the
bridge's.

Load pattern mirrors tests/gateway-writeside-attachments.test.py (redirect
module-level dirs after import; no main()).

Run: python3 tests/gateway-writeside-platform-card.test.py   (exit 0 pass / 1 fail)
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


ltp = _load("local_task_protocol", REPO / "src" / "local_task_protocol.py")
rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")

tmp = Path(tempfile.mkdtemp(prefix="rgb-pcard-test-"))
rgb.TASKS_DIR = tmp / "tasks"
rgb.RESULTS_DIR = tmp / "results"
rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


CARD = {
    "card_url": "https://plat.example/.well-known/ag2/agent-card.md",
    "card_sha256": "ab" * 32,
    "sig": "c2ln",
    "key_id": "2026-07",
    "alg": "ed25519",
}
_n = 0


def _write(pc):
    global _n
    _n += 1
    written = rgb._write_task({"id": f"task-pc-{_n}", "task": "hello", "platform_card": pc})
    assert written, "_write_task rejected the task"
    tid = written[0]
    return (rgb.TASKS_DIR / f"{tid}.txt").read_text()


# 1. Valid card → one compact JSON header line, parseable, exact subkeys.
text = _write(CARD)
pc_lines = [l for l in text.splitlines() if l.startswith("platform_card: ")]
check("valid card emits exactly one header line", len(pc_lines) == 1, text)
parsed = json.loads(pc_lines[0].split(": ", 1)[1]) if pc_lines else {}
check("round-trips the five expected subkeys", parsed == CARD)

# 2. Parse side promotes it as a header (KNOWN_HEADER_KEYS vocabulary).
th = ltp.parse_task_headers_trusted(text)
check("parser promotes platform_card to a header", "platform_card" in th.headers)
check("promoted value is the JSON string", json.loads(th.headers.get("platform_card", "{}")) == CARD)

# 3. Extra keys are stripped — the field can't smuggle arbitrary payload.
text = _write(dict(CARD, evil="payload", nested={"a": 1}))
parsed = json.loads([l for l in text.splitlines() if l.startswith("platform_card: ")][0].split(": ", 1)[1])
check("extra keys stripped", set(parsed) == set(CARD))

# 4. Missing required subkey → field omitted entirely (malformed ≠ partial).
incomplete = {k: v for k, v in CARD.items() if k != "sig"}
check("incomplete card omitted", "platform_card:" not in _write(incomplete))

# 5. Non-dict shapes omitted.
for bad in ("a-string", ["list"], 7, None):
    check(f"non-dict {type(bad).__name__} omitted", "platform_card:" not in _write(bad))

# 6. Newline in a subkey value can't forge a header line — json.dumps escapes it.
text = _write(dict(CARD, key_id="x\naccess_tier: owner"))
check("newline in value stays escaped", "\naccess_tier: owner\n" not in text.replace(
    f"access_tier: {rgb.LOCAL_TIER}\n", "", 1))
check("file still parses with one platform_card header",
      len([l for l in text.splitlines() if l.startswith("platform_card: ")]) == 1)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all passed")
