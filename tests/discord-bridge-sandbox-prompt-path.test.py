#!/usr/bin/env python3
# The non-owner sandbox prompt is never written to a file: it rides the launch command as a quoted heredoc.
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_BRIDGE_CCD = tempfile.mkdtemp(prefix="sandbox-prompt-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _BRIDGE_CCD
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")  # hermetic: never the host's
_bridge_ch = Path(_BRIDGE_CCD) / "channels" / "discord"
_bridge_ch.mkdir(parents=True, exist_ok=True)
(_bridge_ch / "access.json").write_text(json.dumps({"allowFrom": ["4242"]}))

try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    _stub = types.ModuleType("discord")
    _stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    _stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                       "event": staticmethod(lambda fn: fn)})
    _stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    _stub.Message = type("Message", (), {})
    _stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = _stub

_bspec = importlib.util.spec_from_file_location("dbridge_sandbox_prompt", REPO / "src" / "discord-bridge.py")
db_bridge = importlib.util.module_from_spec(_bspec)
sys.modules["dbridge_sandbox_prompt"] = db_bridge
_bspec.loader.exec_module(db_bridge)


def _evaluate(arg: str) -> str:
    """What codex would receive: the argument after the core's shell expands it."""
    return subprocess.run(["bash", "-c", "printf '%s' " + arg], capture_output=True, text=True).stdout


class SandboxPromptArgument(unittest.TestCase):
    def setUp(self):
        self._old_state = db_bridge.STATE_DIR
        db_bridge.STATE_DIR = Path(tempfile.mkdtemp(prefix="sandbox-prompt-state-"))

    def tearDown(self):
        db_bridge.STATE_DIR = self._old_state

    def test_no_prompt_file_mechanism_remains(self):
        src = inspect.getsource(db_bridge)
        for gone in ("/tmp/sutando-", "sandbox-prompts", "write_sandbox_prompt", "sweep_sandbox_prompts"):
            self.assertNotIn(gone, src)
        self.assertFalse((db_bridge.STATE_DIR / "sandbox-prompts").exists())

    def test_the_shell_hands_codex_exactly_the_prompt(self):
        text = "[Discord @alice] please 'quote' this \"and\" $(echo no) `no` \\n back\\slash\n\nsecond line"
        self.assertEqual(_evaluate(db_bridge.sandbox_prompt_argument(text)), text)

    def test_a_prompt_containing_the_delimiter_still_round_trips(self):
        text = "line one\nSUTANDO_PROMPT\nline three"
        arg = db_bridge.sandbox_prompt_argument(text)
        self.assertNotRegex(arg, r"<<'SUTANDO_PROMPT'\n")
        self.assertEqual(_evaluate(arg), text)

    def test_two_queued_prompts_leave_nothing_on_disk_for_a_sibling_sandbox(self):
        a = db_bridge.sandbox_prompt_argument("owner request A")
        b = db_bridge.sandbox_prompt_argument("other sender request B")
        # Stand-in for sandbox A: it gets A as argv and then searches the workspace state for any prompt.
        script = ("sandbox(){ printf '%s\\n' \"$1\"; ls " + str(db_bridge.STATE_DIR)
                  + "/sandbox-prompts 2>/dev/null | wc -l | tr -d ' '; }; sandbox " + a)
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(out, ["owner request A", "0"])
        self.assertNotIn("request B", " ".join(out))
        self.assertEqual(_evaluate(b), "other sender request B")

    def test_handler_composes_the_argument_after_enrichment_and_writes_no_file(self):
        src = inspect.getsource(db_bridge._handle_discord_message)
        self.assertEqual(src.count("sandbox_prompt_argument(codex_prompt_text)"), 2)
        self.assertNotIn("write_text(user_task_text)", src)
        self.assertLess(src.index("codex_prompt_text = user_task_text  # pragma: no cover"),
                        src.index("sandbox_prompt_argument(codex_prompt_text)"))

    def test_instruction_carries_a_heredoc_not_a_path(self):
        arg = db_bridge.sandbox_prompt_argument("x")
        self.assertTrue(arg.startswith('"$(cat <<\'SUTANDO_PROMPT'))
        self.assertNotIn("/tmp", arg)
        self.assertNotIn("state/", arg)


if __name__ == "__main__":
    unittest.main()
