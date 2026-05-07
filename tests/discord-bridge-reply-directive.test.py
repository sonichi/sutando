#!/usr/bin/env python3
"""
Test the [reply: <message_id>] directive in discord-bridge poll_results.

Purpose: catch the class of bug where the MessageReference construction
references an out-of-scope variable (e.g. `int(channel_id)` when the
function scope only has `channel`). The original v1 of PR #620 had this
exact bug — the ternary short-circuit meant tests with no directive
passed green, but the moment a result file contained `[reply: <id>]`
the bridge would NameError.

Run: python3 tests/discord-bridge-reply-directive.test.py
"""

from __future__ import annotations
import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Stub minimal discord module — same pattern as other bridge tests
_discord_stub = types.ModuleType("discord")


class _Intents:
    @classmethod
    def default(cls):
        i = cls()
        i.message_content = False
        i.members = False
        return i


class _Client:
    def __init__(self, *a, **kw):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)
    def event(self, fn): return fn
    def get_channel(self, _): return None


class _AllowedMentions:
    def __init__(self, *a, **kw): pass


class _MessageReference:
    """Captures the constructor args so tests can assert on them."""
    def __init__(self, message_id=None, channel_id=None, fail_if_not_exists=True):
        self.message_id = message_id
        self.channel_id = channel_id
        self.fail_if_not_exists = fail_if_not_exists


class _DMChannel: pass


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.AllowedMentions = _AllowedMentions
_discord_stub.MessageReference = _MessageReference
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None
_discord_stub.DMChannel = _DMChannel

sys.modules["discord"] = _discord_stub


def load_bridge():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    fake_env_dir = Path.home() / ".claude" / "channels" / "discord"
    fake_env = fake_env_dir / ".env"
    if not fake_env.exists():
        fake_env_dir.mkdir(parents=True, exist_ok=True)
        fake_env.write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(REPO / "src" / "discord-bridge.py")
    exec(src, bridge.__dict__)
    return bridge


bridge = load_bridge()


# ---------------------------------------------------------------------------
# Mock channel that captures send() calls
# ---------------------------------------------------------------------------

class _MockChannel:
    def __init__(self, channel_id=987654321):
        self.id = channel_id
        self.sent = []  # list of {content, reference, file}
    async def send(self, content=None, *, reference=None, file=None, allowed_mentions=None):
        self.sent.append({
            "content": content,
            "reference": reference,
            "file": file,
            "allowed_mentions": allowed_mentions,
        })
        return types.SimpleNamespace(id=12345)


# ---------------------------------------------------------------------------
# Drive one iteration of poll_results
# ---------------------------------------------------------------------------

async def _run_one_poll_iteration(task_id, channel, result_text):
    """Set up state, write the result file, drive poll_results until first
    sleep, then cancel. Returns the captured channel.sent list."""
    # Stage state in the bridge module
    bridge.pending_replies.clear()
    bridge.pending_replies[task_id] = channel
    # Write the result file under bridge's RESULTS_DIR
    result_file = bridge.RESULTS_DIR / f"{task_id}.txt"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(result_text)
    # Stub client.is_ready so heartbeat block is skipped cleanly
    bridge.client.is_ready = lambda: True
    # Stub save_pending_replies so we don't write to disk during tests
    bridge.save_pending_replies = lambda: None
    # archive_file uses real fs; let it run — RESULTS_DIR is shared with prod
    # but the file we wrote is fully synthetic and gets archived

    # Run poll_results with a tight timeout — we only need 1 iteration
    try:
        await asyncio.wait_for(bridge.poll_results(), timeout=0.5)
    except asyncio.TimeoutError:
        pass
    return channel.sent


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_a_directive_present_constructs_reference():
    """When result text contains `[reply: <id>]`, channel.send must be called
    with reference=MessageReference(message_id=<id>, channel_id=channel.id)."""
    fails = []
    ch = _MockChannel(channel_id=111222333)
    parent_msg_id = "1500000000000000001"
    result_text = f"[reply: {parent_msg_id}] hello world"
    sent = asyncio.run(_run_one_poll_iteration("test-task-a", ch, result_text))
    if not sent:
        fails.append("a) no channel.send call observed")
        return fails
    first = sent[0]
    if first["reference"] is None:
        fails.append(f"a) first send should have reference set (got None)")
        return fails
    ref = first["reference"]
    if not hasattr(ref, "message_id") or str(ref.message_id) != parent_msg_id:
        fails.append(f"a) reference.message_id should be {parent_msg_id} (got {getattr(ref, 'message_id', None)})")
    if not hasattr(ref, "channel_id") or ref.channel_id != ch.id:
        fails.append(f"a) reference.channel_id should be channel.id={ch.id} (got {getattr(ref, 'channel_id', None)})")
    if not hasattr(ref, "fail_if_not_exists") or ref.fail_if_not_exists is not False:
        fails.append(f"a) reference.fail_if_not_exists should be False (got {getattr(ref, 'fail_if_not_exists', None)})")
    # Directive should be stripped from the sent content
    if "[reply:" in (first["content"] or ""):
        fails.append(f"a) [reply:] directive should be stripped from sent content (got {first['content']!r})")
    return fails


def case_b_no_directive_no_reference():
    """Backwards-compat: a result text without `[reply: <id>]` must NOT have
    a reference attached. This is what made the v1 bug invisible."""
    fails = []
    ch = _MockChannel(channel_id=444555666)
    sent = asyncio.run(_run_one_poll_iteration("test-task-b", ch, "plain text reply"))
    if not sent:
        fails.append("b) no channel.send call observed")
        return fails
    if sent[0]["reference"] is not None:
        fails.append(f"b) without directive, reference should be None (got {sent[0]['reference']})")
    return fails


def case_c_directive_only_first_chunk():
    """For multi-chunk text, only the first chunk should carry the reference
    (Discord allows one reply-anchor per send)."""
    fails = []
    ch = _MockChannel(channel_id=777888999)
    parent = "1500000000000000002"
    # Force multi-chunk by exceeding the chunker's max_len (1900 chars)
    body = "x" * 2200
    result_text = f"[reply: {parent}] {body}"
    sent = asyncio.run(_run_one_poll_iteration("test-task-c", ch, result_text))
    if len(sent) < 2:
        fails.append(f"c) expected ≥2 chunks for 2200-char body (got {len(sent)})")
        return fails
    if sent[0]["reference"] is None:
        fails.append("c) first chunk should have reference set")
    if sent[1]["reference"] is not None:
        fails.append(f"c) second chunk should NOT have reference (got {sent[1]['reference']})")
    return fails


def main():
    cases = [
        ("a-directive-present-constructs-reference", case_a_directive_present_constructs_reference),
        ("b-no-directive-no-reference", case_b_no_directive_no_reference),
        ("c-directive-only-first-chunk", case_c_directive_only_first_chunk),
    ]
    failures = []
    for label, fn in cases:
        fs = fn()
        if fs:
            failures.extend([(label, msg) for msg in fs])
            print(f"  ✗ case {label}")
            for f in fs:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nAll [reply: <id>] directive invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
