#!/usr/bin/env python3
"""Tests for skills/ag2-space-mcp/scripts/register.py: the descriptor it writes for the
proxy names where the credential is and never carries it; discovery URLs must be https.

Pure, no network: discovery is a fake. Run: python3 tests/ag2-space-mcp-register.test.py
"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "ag2-space-mcp" / "scripts"))

import register  # noqa: E402

FAILS = []
SECRET = "s3cr3t-bearer-value"
URLS = {"mint_url": "https://chat.example/api/v1/mcp/agent-access-tokens", "mcp_url": "https://mcp.example/mcp"}


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def env_file(text):
    d = Path(tempfile.mkdtemp())
    p = d / ".env"
    p.write_text(text)
    return p


def fake_discover(seen):
    def discover(relay, secret):
        seen.append((relay, secret))
        return dict(URLS)
    return discover


def compound_token():
    seen = []
    p = env_file(f"REMOTE_TASK_TOKEN='https://chat.example/relay|{SECRET}'\n")
    d = register.build_descriptor(p, fake_discover(seen))
    assert seen == [("https://chat.example/relay", SECRET)], seen
    assert d["env_file"] == str(p) and d["env_key"] == "REMOTE_TASK_TOKEN", d
    assert d["mint_url"] == URLS["mint_url"] and d["mcp_url"] == URLS["mcp_url"], d
    assert d["version"] == 1, d


def bare_secret_uses_task_url():
    seen = []
    p = env_file(f"REMOTE_TASK_URL=https://chat.example/relay\nREMOTE_TASK_TOKEN={SECRET}\n")
    register.build_descriptor(p, fake_discover(seen))
    assert seen == [("https://chat.example/relay", SECRET)], seen


def legacy_key_is_found():
    p = env_file(f'AG2_REMOTE_TOKEN="https://chat.example/relay|{SECRET}"\n')
    d = register.build_descriptor(p, fake_discover([]))
    assert d["env_key"] == "AG2_REMOTE_TOKEN", d


def no_relay_is_refused():
    p = env_file(f"REMOTE_TASK_TOKEN={SECRET}\n")
    try:
        register.build_descriptor(p, fake_discover([]))
    except register.RegisterError as e:
        assert SECRET not in str(e), "the error must not echo the secret"
        return
    raise AssertionError("a bare secret with no relay URL must be refused")


def plaintext_discovery_is_refused():
    p = env_file(f"REMOTE_TASK_TOKEN='https://chat.example/relay|{SECRET}'\n")
    try:
        register.build_descriptor(p, lambda r, s: {"mint_url": "http://evil.example/mint", "mcp_url": URLS["mcp_url"]})
    except register.RegisterError:
        return
    raise AssertionError("an http mint_url would send the bearer in clear; it must be refused")


def nothing_written_holds_the_secret():
    p = env_file(f"REMOTE_TASK_TOKEN='https://chat.example/relay|{SECRET}'\n")
    d = register.build_descriptor(p, fake_discover([]))
    out = Path(tempfile.mkdtemp()) / "state" / "ag2-mcp" / "descriptor.json"
    register.write_descriptor(out, d)
    assert SECRET not in out.read_text(), "descriptor carries the secret"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600, oct(os.stat(out).st_mode)
    entry = register.server_entry("/usr/bin/node", out, Path("/ws"))
    blob = json.dumps(entry) + register.codex_toml(entry)
    assert SECRET not in blob, "MCP config entry carries the secret"
    assert entry["args"] == [str(register.PROXY)] and register.PROXY.is_file(), entry
    assert entry["env"]["AG2_MCP_DESCRIPTOR"] == str(out), entry
    assert entry["env"]["AG2_MCP_ROOM_ACTIONS"] == "/ws/state/room-actions.jsonl", entry


def descriptor_matches_what_the_proxy_reads():
    # The proxy refuses a descriptor missing any of these, or a non-https URL.
    p = env_file(f"REMOTE_TASK_TOKEN='https://chat.example/relay|{SECRET}'\n")
    d = register.build_descriptor(p, fake_discover([]))
    for k in ("env_file", "env_key", "mint_url", "mcp_url"):
        assert isinstance(d[k], str) and d[k], k


for name, fn in list(globals().items()):
    if callable(fn) and fn.__module__ == "__main__" and name not in ("check", "env_file", "fake_discover"):
        check(name, fn)

if FAILS:
    print("ag2-space-mcp register: FAIL")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("ag2-space-mcp register: ok")
