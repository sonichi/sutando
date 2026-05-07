#!/usr/bin/env python3
"""
Tests for the codex judge wrapper + prompt builder in discord-bridge.py.

PR2 of 3 — covers `_format_judge_prompt` (pure) and `_codex_judge_batch`
(async, subprocess invocation patched in tests). The buffer/flush state
machine + on_message hook + per-rule action dispatchers come in PR3.

Run: python3 tests/discord-bridge-mod-judge-codex.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Stub minimal discord module
_discord_stub = types.ModuleType("discord")


class _Intents:
    @classmethod
    def default(cls):
        i = cls()
        i.message_content = False
        i.members = False
        return i


class _Client:
    def __init__(self, *args, **kwargs):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)
    def event(self, fn): return fn
    def get_channel(self, _id): return None


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None


class _DMChannel: pass


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
# _format_judge_prompt
# ---------------------------------------------------------------------------

def case_format_prompt_basic() -> list[str]:
    fails = []
    msgs = [
        {"msg_id": "111", "channel_name": "general", "author_name": "alice",
         "content": "hello world", "is_reply": False, "parent_content": ""},
    ]
    out = bridge._format_judge_prompt(msgs)
    if "msg_id=111" not in out:
        fails.append("a) prompt should include msg_id")
    if "@alice" not in out or "#general" not in out:
        fails.append("a) prompt should include channel + author")
    if "hello world" not in out:
        fails.append("a) prompt should include content (repr ok)")
    if "STRICT JSON" not in out:
        fails.append("a) prompt should mandate strict JSON")
    if "rule_1" not in out or "rule_7" not in out:
        fails.append("a) prompt should enumerate rules 1-7")
    return fails


def case_format_prompt_reply_includes_parent() -> list[str]:
    fails = []
    msgs = [
        {"msg_id": "222", "channel_name": "geo-sf", "author_name": "bob",
         "content": "discord.gg/abc123", "is_reply": True,
         "parent_content": "what's your discord for the bay area meetup?"},
    ]
    out = bridge._format_judge_prompt(msgs)
    if "reply to:" not in out:
        fails.append("b) reply context should appear")
    if "bay area meetup" not in out:
        fails.append("b) parent content should be included")
    return fails


def case_format_prompt_truncates_long_content() -> list[str]:
    fails = []
    long = "x" * 5000
    msgs = [
        {"msg_id": "333", "channel_name": "x", "author_name": "y",
         "content": long, "is_reply": False, "parent_content": ""},
    ]
    out = bridge._format_judge_prompt(msgs)
    # 500-char cap for content; the prompt should not contain the full 5000
    if long in out:
        fails.append("c) content should be truncated to 500 chars")
    return fails


def case_format_prompt_with_rules_context() -> list[str]:
    fails = []
    msgs = [{"msg_id": "111", "channel_name": "x", "author_name": "y",
             "content": "z", "is_reply": False, "parent_content": ""}]
    out = bridge._format_judge_prompt(msgs, rules_context="user posted in 4 channels")
    if "user posted in 4 channels" not in out:
        fails.append("d) rules_context should appear in prompt")
    if "Additional context" not in out:
        fails.append("d) rules_context section header should appear")
    return fails


def case_format_prompt_empty_messages() -> list[str]:
    """Empty message list should still produce a well-formed prompt
    (even though caller shouldn't invoke judge with no messages)."""
    fails = []
    out = bridge._format_judge_prompt([])
    if "STRICT JSON" not in out:
        fails.append("e) empty-msgs prompt should still have schema directive")
    return fails


# ---------------------------------------------------------------------------
# _codex_judge_batch (subprocess patched)
# ---------------------------------------------------------------------------

def _patch_codex(bridge_mod, fake_stdout):
    """Replace `_run_codex_subprocess` with one that returns `fake_stdout`."""
    async def _stub(prompt, model, timeout_s):
        return fake_stdout
    bridge_mod._run_codex_subprocess = _stub


def case_judge_batch_happy_path() -> list[str]:
    fails = []
    fake = json.dumps({"verdicts": [
        {"msg_id": "111", "rule_match": "rule_1", "confidence": 0.92, "rationale": "crypto-job spam"},
        {"msg_id": "222", "rule_match": None, "confidence": 0.99, "rationale": "clean"},
    ]})
    _patch_codex(bridge, fake)
    msgs = [
        {"msg_id": "111", "channel_name": "x", "author_name": "y", "content": "z", "is_reply": False, "parent_content": ""},
        {"msg_id": "222", "channel_name": "x", "author_name": "y", "content": "z", "is_reply": False, "parent_content": ""},
    ]
    verdicts = asyncio.run(bridge._codex_judge_batch(msgs))
    if len(verdicts) != 2:
        fails.append(f"f) should return 2 verdicts, got {len(verdicts)}")
    if verdicts and verdicts[0]["rule_match"] != "rule_1":
        fails.append("f) verdict ordering should be preserved")
    return fails


def case_judge_batch_empty_messages() -> list[str]:
    """Empty msg list should short-circuit without invoking codex."""
    fails = []
    called = {"n": 0}
    async def _stub(prompt, model, timeout_s):
        called["n"] += 1
        return "[]"
    bridge._run_codex_subprocess = _stub
    verdicts = asyncio.run(bridge._codex_judge_batch([]))
    if verdicts != []:
        fails.append("g) empty-msgs should return []")
    if called["n"] != 0:
        fails.append("g) empty-msgs should NOT spawn codex subprocess")
    return fails


def case_judge_batch_malformed_codex_output() -> list[str]:
    fails = []
    _patch_codex(bridge, "not json garbage")
    msgs = [{"msg_id": "111", "channel_name": "x", "author_name": "y",
             "content": "z", "is_reply": False, "parent_content": ""}]
    verdicts = asyncio.run(bridge._codex_judge_batch(msgs))
    if verdicts != []:
        fails.append("h) malformed codex output should return []")
    return fails


def case_judge_batch_codex_empty_string() -> list[str]:
    """codex subprocess failure (timeout, non-zero exit) returns empty
    stdout; downstream should parse to []."""
    fails = []
    _patch_codex(bridge, "")
    msgs = [{"msg_id": "111", "channel_name": "x", "author_name": "y",
             "content": "z", "is_reply": False, "parent_content": ""}]
    verdicts = asyncio.run(bridge._codex_judge_batch(msgs))
    if verdicts != []:
        fails.append("i) empty codex output should return []")
    return fails


def case_judge_batch_passes_model_arg() -> list[str]:
    fails = []
    captured = {"model": None, "prompt": None}
    async def _capture(prompt, model, timeout_s):
        captured["model"] = model
        captured["prompt"] = prompt
        return "[]"
    bridge._run_codex_subprocess = _capture
    msgs = [{"msg_id": "111", "channel_name": "x", "author_name": "y",
             "content": "z", "is_reply": False, "parent_content": ""}]
    asyncio.run(bridge._codex_judge_batch(msgs, model="gpt-4o-mini"))
    if captured["model"] != "gpt-4o-mini":
        fails.append(f"j) model arg should pass through, got {captured['model']}")
    if "msg_id=111" not in (captured["prompt"] or ""):
        fails.append("j) prompt should be the formatted judge prompt")
    return fails


def main() -> int:
    cases = [
        ("a-format-basic", case_format_prompt_basic),
        ("b-format-reply", case_format_prompt_reply_includes_parent),
        ("c-format-trunc", case_format_prompt_truncates_long_content),
        ("d-format-ctx", case_format_prompt_with_rules_context),
        ("e-format-empty", case_format_prompt_empty_messages),
        ("f-batch-happy", case_judge_batch_happy_path),
        ("g-batch-empty", case_judge_batch_empty_messages),
        ("h-batch-malformed", case_judge_batch_malformed_codex_output),
        ("i-batch-empty-out", case_judge_batch_codex_empty_string),
        ("j-batch-model-arg", case_judge_batch_passes_model_arg),
    ]
    failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nAll codex-judge wrapper invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
