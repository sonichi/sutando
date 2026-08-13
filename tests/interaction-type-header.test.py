#!/usr/bin/env python3
"""Every task-file producer that writes a `source:` header must also write an
`interaction_type:` header (additive schema field, interaction-planes refactor
step 1). A producer is a code site that serializes a `source: <value>` line
into a task file; the interaction_type line must appear within a few lines of
it so the two fields stay paired at the write site.

Scope: scans the known producer files. The values themselves are free-form at
this stage (message | realtime_audio | realtime_video | tool_initiated |
system_event | self_reflective per the producer table); this test only
enforces presence, not vocabulary — vocabulary is enforced once the schema
moves into local_task_protocol.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# file -> list of source values whose write sites must carry interaction_type.
# (Read sites like `startswith("source:")` or docstrings don't match the
# serialization patterns below.)
PRODUCERS = {
    "src/slack-bridge.py": ["slack"],
    "src/telegram-bridge.py": ["telegram"],
    "src/discord-bridge.py": ["discord"],
    "src/health-check.py": ["health-check"],
    "src/agent-api.py": ["twilio_voice", "twilio_sms", "twilio_voicemail", "api"],
    "src/github-webhook.py": ["github"],
    "src/task-bridge.ts": ["chat", "voice", "context-drop"],
    "src/Sutando/main.swift": ["context-drop"],
    "CLAUDE.md": ["chat"],
}

# Serialized `source: <value>` at a write site: inside an f-string/template
# string/heredoc/Swift multiline string. Matches `source: slack\n`,
# "`source: chat`," and a bare `source: chat` template line.
def write_sites(text: str, value: str):
    # Two write shapes: serialized header lines in string literals, and the
    # centralized write_task_file tuple form ("source", "<value>").
    pat = re.compile(
        rf"^.*\bsource: {re.escape(value)}(\\n|`|\n|$)"
        rf"|^.*\(\s*[\"']source[\"']\s*,\s*[\"']{re.escape(value)}[\"']\s*\)",
        re.MULTILINE)
    return [text[: m.start()].count("\n") for m in pat.finditer(text)]


failures = []
checked = 0
for rel, values in PRODUCERS.items():
    path = REPO / rel
    text = path.read_text()
    lines = text.split("\n")
    for value in values:
        sites = [
            ln for ln in write_sites(text, value)
            # skip pure-comment/docstring mentions ("source: voice)." etc.)
            if not lines[ln].lstrip().startswith(("#", "*", "//", "'"))
        ]
        if not sites:
            failures.append(f"{rel}: no write site found for source: {value} "
                            f"(producer moved? update PRODUCERS)")
            continue
        for ln in sites:
            window = "\n".join(lines[max(0, ln - 6): ln + 7])
            checked += 1
            if "interaction_type:" not in window and '"interaction_type"' not in window:
                failures.append(
                    f"{rel}:{ln + 1}: `source: {value}` write site has no "
                    f"interaction_type: header within 6 lines")

# conversation-server.ts phone writers: delegateTask carries the full
# source+interaction_type pair (source: phone activates the dormant
# DM-fallback + urgent-priority consumers); the call-end summary writer
# deliberately has NO source: header (source: phone would double-DM — the
# task instructions already have the core DM the owner, and the bridge
# fallback would deliver the same result a second time), so it is asserted
# on interaction_type alone.
cs = (REPO / "skills/phone-conversation/scripts/conversation-server.ts").read_text()
for marker, expect, want_source in (
        ("`task-phone-${", "realtime_audio", True),
        ("`task-summary-${", "system_event", False)):
    checked += 1
    seg_start = cs.find(marker)  # the taskId assignment at the write site
    window = cs[seg_start: seg_start + 2500]
    if seg_start == -1 or f"interaction_type: {expect}" not in window:
        failures.append(
            f"conversation-server.ts: {marker} writer missing interaction_type: {expect}")
    # Serialized forms only (`\n`-joined template or backtick array line) —
    # prose mentions of "source: phone" in comments must not match.
    has_source = bool(re.search(r"source: phone(\\n|`)", window))
    if want_source != has_source:
        failures.append(
            f"conversation-server.ts: {marker} writer source: phone "
            f"{'missing' if want_source else 'present (double-DM regression — see comment at the writer)'}")

# remote-gateway-bridge serializes from _TASK_FIELDS — assert pass-through is
# wired, vocabulary-whitelisted, and defaults to message. The implementation is
# canonical in the ag2-sparrow package (src/remote-gateway-bridge.py is a thin
# loader shim post-#2082), so the guard reads the package source.
gw = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()
checked += 1
if ('"interaction_type"' not in gw or "_INTERACTION_TYPES" not in gw
        or 'it = "message"' not in gw):
    failures.append("remote-gateway-bridge.py: interaction_type whitelist/default missing")

if failures:
    for f in failures:
        print(f"  FAIL {f}")
    sys.exit(1)
print(f"PASS — {checked} producer write sites carry interaction_type")
