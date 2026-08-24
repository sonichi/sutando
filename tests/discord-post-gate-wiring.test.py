#!/usr/bin/env python3
"""A refusing post-gate policy, installed through the REAL launch wiring,
blocks every migrated production sender with zero transport attempts.

The gate contract test proves a manually built client can refuse; this one
proves the PRODUCTION entrypoints consult the policy at all. The policy is a
real file on disk, named by $SUTANDO_DISCORD_POST_GATE (the launch wiring a
personal layer sets for these separate processes; `bridges.discord_post_gate`
in sutando.config.json[.local] is the per-clone equivalent), and every path
below constructs through discord_post_gate.make_client:

  bridge CLI send        discord-bridge.py:_send_via_rest -> _rest_client
  bridge proactive leg   discord-bridge.py:_proactive_provider
  dm-result / notify.sh  dm-result.py:_client
  bot2bot-post           post.py:post -> _client
  task-progress          notify.py:send_discord -> _rest_client

urlopen is patched with a recorder that WOULD succeed, so a gate that fails
to refuse shows up as a recorded attempt, not as an unrelated error.

Run: python3 tests/discord-post-gate-wiring.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import urllib.request
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-post-gate-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n", encoding="utf-8")
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import channels.discord.post_gate as dpg  # noqa: E402
from outbox import DeliveryOutcome  # noqa: E402

_policy_dir = Path(tempfile.mkdtemp(prefix="post-gate-policy-"))
_seen_file = _policy_dir / "seen.jsonl"
_policy = _policy_dir / "refuse_all.py"
_policy.write_text(
    "import json, pathlib\n"
    f"_SEEN = pathlib.Path({str(_seen_file)!r})\n"
    "def validate(channel_id, payload):\n"
    "    with _SEEN.open('a') as f:\n"
    "        f.write(json.dumps({'channel_id': channel_id, 'payload': payload}) + '\\n')\n"
    "    return 'refused by wiring-test policy'\n"
)

# --- resolver semantics ------------------------------------------------------
os.environ.pop("SUTANDO_DISCORD_POST_GATE", None)
with tempfile.TemporaryDirectory() as td:
    check("unconfigured -> validator is None (repo default, ungated)",
          dpg.resolve_validator(repo_root=Path(td)) is None)
    client = dpg.make_client("tok", repo_root=Path(td))
    check("unconfigured make_client -> ungated client", client._validator is None)

os.environ["SUTANDO_DISCORD_POST_GATE"] = str(_policy_dir / "does-not-exist.py")
v = dpg.resolve_validator()
check("configured-but-missing policy -> fail-closed refuser",
      callable(v) and "failed to load" in (v("c", {}) or ""))

_bad = _policy_dir / "not_callable.py"
_bad.write_text("validate = 42\n")
os.environ["SUTANDO_DISCORD_POST_GATE"] = str(_bad)
v = dpg.resolve_validator()
check("non-callable validate -> fail-closed refuser",
      callable(v) and "non-callable" in (v("c", {}) or ""))

# --- install the refusing policy through the real wiring ---------------------
os.environ["SUTANDO_DISCORD_POST_GATE"] = str(_policy)

attempts = []


class _OkResp:
    status = 200

    def read(self):
        return b'{"id": "should-never-be-sent"}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _recording_urlopen(req, timeout=None):
    attempts.append(getattr(req, "full_url", "?"))
    return _OkResp()


_real_urlopen = urllib.request.urlopen
urllib.request.urlopen = _recording_urlopen
try:
    # 1. dm-result (the same factory notify.sh's delegation rides)
    _spec = importlib.util.spec_from_file_location("dm_result_gate", REPO / "src" / "dm-result.py")
    dmr = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(dmr)
    r = dmr._client("tok").send_message("chan-dm", {"content": "x"})
    check("dm-result/notify.sh: refused", r.outcome is DeliveryOutcome.NOT_DELIVERED, r.detail)
    check("dm-result/notify.sh: refusal names the policy",
          "wiring-test policy" in r.detail, r.detail)

    # 2. bot2bot-post, full post() path
    _spec = importlib.util.spec_from_file_location("b2b_gate", REPO / "skills" / "bot2bot-post" / "post.py")
    b2b = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(b2b)
    msg = ""
    try:
        b2b.post("chan-b2b", "ping: hi", "tok")
        check("bot2bot: refused post exits", False)
    except SystemExit as e:
        msg = str(e)
        check("bot2bot: refused post exits", True)
    check("bot2bot: exit names NOT_DELIVERED + the policy",
          "NOT_DELIVERED" in msg and "wiring-test policy" in msg, msg)

    # 3. task-progress send_discord, full path
    _spec = importlib.util.spec_from_file_location(
        "notify_gate", REPO / "skills" / "task-progress" / "scripts" / "notify.py")
    ntf = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ntf)
    ntf._token = lambda source, var: "test-stub-token"
    err = io.StringIO()
    with redirect_stderr(err):
        ok = ntf.send_discord("chan-tp", "hello")
    check("task-progress: send_discord returns False", ok is False)
    check("task-progress: stderr names the refusal",
          "wiring-test policy" in err.getvalue(), err.getvalue())

    # 4 + 5. discord-bridge: CLI send and the proactive provider
    try:
        import discord  # noqa: F401
    except ImportError:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(
            lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                          "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {})
        stub.Message = type("Message", (), {})
        sys.modules["discord"] = stub
    _spec = importlib.util.spec_from_file_location("dbridge_gate", REPO / "src" / "discord-bridge.py")
    bridge = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(bridge)

    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            bridge._send_via_rest("chan-cli", "hello")
    except SystemExit as e:
        rc = e.code
    check("bridge CLI send: refused -> exit 1", rc == 1, out.getvalue())
    check("bridge CLI send: failure names the policy",
          "wiring-test policy" in out.getvalue(), out.getvalue())

    bridge._PROACTIVE_PROVIDER = None
    provider = bridge._proactive_provider()
    receipt = provider.deliver(
        "item-1", json.dumps({"channel_id": "chan-pro", "content": "x"}).encode(),
        "idem-1")
    check("proactive provider: refused", "NOT_DELIVERED" in str(receipt.outcome),
          str(receipt.outcome))

    check("ZERO transport attempts across all five paths", attempts == [],
          str(attempts))
finally:
    urllib.request.urlopen = _real_urlopen
    os.environ.pop("SUTANDO_DISCORD_POST_GATE", None)

# --- the policy really saw (channel_id, payload) from every path -------------
seen = [json.loads(line) for line in _seen_file.read_text().splitlines()]
seen_channels = {s["channel_id"] for s in seen}
check("policy saw every channel id",
      {"chan-dm", "chan-b2b", "chan-tp", "chan-cli", "chan-pro"} <= seen_channels,
      str(seen_channels))
check("policy saw dict payloads with content",
      all(isinstance(s["payload"], dict) and "content" in s["payload"] for s in seen))

# CR 2026-08-21 regressions: unreadable config fails CLOSED; relative
# config paths anchor to the repo root, not the process cwd.
import sutando_config as _sc
_orig_load = _sc.load_config
def _boom(repo_root=None):
    raise ValueError("malformed sutando.config.json")
_sc.load_config = _boom
os.environ.pop("SUTANDO_DISCORD_POST_GATE", None)
_v = dpg.resolve_validator()
check("unreadable config yields a refuser, not None", _v is not None)
check("refuser names the closed gate",
      _v is not None and "refusing unvalidated sends" in str(_v("1", {"content": "x"})))
_root = Path(tempfile.mkdtemp(prefix="post-gate-root-"))
(_root / "policy").mkdir()
(_root / "policy" / "gate_ok.py").write_text("def validate(channel_id, payload):\n    return None\n")
_sc.load_config = lambda repo_root=None: {"bridges": {"discord_post_gate": "policy/gate_ok.py"}}
_prev_cwd = os.getcwd(); os.chdir("/")
try:
    _v2 = dpg.resolve_validator(repo_root=_root)
finally:
    os.chdir(_prev_cwd)
check("relative config path anchors to repo_root (cwd wrong on purpose)",
      callable(_v2) and _v2("1", {"content": "x"}) is None)
_sc.load_config = _orig_load

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("post-gate wiring holds: one refusing policy, installed via launch wiring, "
      "blocks all five production sender paths with zero transport attempts")
