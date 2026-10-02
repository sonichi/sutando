#!/usr/bin/env python3
"""Tests for skills/ag2-space-mcp/scripts/register.py: the descriptor it writes for the
proxy names where the credential is and never carries it; discovery URLs must be https.

Pure, no network: discovery is a fake. Run: python3 tests/ag2-space-mcp-register.test.py
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import urllib.error
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
    paths = register.settings(Path("/ws"), environ={"AG2_MCP_DESCRIPTOR": str(out)})
    entry = register.server_entry("/usr/bin/node", paths)
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


class Patch:
    """Swap attributes on the register module for one test, then put them back."""

    def __init__(self, **attrs):
        self.attrs, self.saved = attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(register, k)
            setattr(register, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(register, k, v)


def run_main(argv, **attrs):
    out, err = io.StringIO(), io.StringIO()
    discover = attrs.pop("_discover", fake_discover([]))
    with Patch(**attrs), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = register.main(argv, discover=discover)
    return rc, out.getvalue(), err.getvalue()


def ws_and_env():
    ws = Path(tempfile.mkdtemp())
    p = env_file(f"REMOTE_TASK_TOKEN='https://chat.example/relay|{SECRET}'\n")
    return ws, p


def which(found):
    return lambda name: f"/bin/{name}" if name in found else None


def main_dry_run_prints_the_entry_and_writes_the_descriptor():
    ws, p = ws_and_env()
    rc, out, _ = run_main(["--env-file", str(p)], resolve_workspace=lambda: ws, which=which({"node"}))
    assert rc == 0, rc
    assert "dry run" in out and '"AG2_MCP_DESCRIPTOR"' in out, out
    assert (ws / "state" / "ag2-mcp" / "descriptor.json").is_file()
    assert SECRET not in out, "dry run printed the secret"


def main_codex_prints_toml():
    ws, p = ws_and_env()
    rc, out, _ = run_main(["--env-file", str(p), "--runtime", "codex"], resolve_workspace=lambda: ws,
                          which=which({"node"}))
    assert rc == 0 and "[mcp_servers.ag2-space]" in out, out


def main_apply_runs_claude_with_the_config_dir():
    ws, p = ws_and_env()
    calls = []

    def run(cmd, env):
        calls.append((cmd, env.get("CLAUDE_CONFIG_DIR")))
        return type("Done", (), {"returncode": 0})()
    rc, out, _ = run_main(["--env-file", str(p), "--apply"], resolve_workspace=lambda: ws,
                          which=which({"node", "claude"}), run=run, claude_home_path=lambda *a: Path("/ccd", *a))
    assert rc == 0 and "registered" in out, (rc, out)
    cmd, ccd = calls[0]
    assert cmd[:6] == ["claude", "mcp", "add-json", "--scope", "user", "ag2-space"], cmd
    assert ccd == "/ccd", ccd
    assert SECRET not in " ".join(cmd), "the command line carries the secret"


def main_apply_without_claude_fails():
    ws, p = ws_and_env()
    rc, _, err = run_main(["--env-file", str(p), "--apply"], resolve_workspace=lambda: ws, which=which({"node"}))
    assert rc == 1 and "claude is not on PATH" in err, (rc, err)


def main_without_node_fails():
    ws, p = ws_and_env()
    rc, _, err = run_main(["--env-file", str(p)], resolve_workspace=lambda: ws, which=which(set()))
    assert rc == 1 and "node" in err, (rc, err)


def main_without_credential_fails():
    rc, _, err = run_main([], resolve_channel_env=lambda d, s: None, which=which({"node"}),
                          claude_home_path=lambda *a: Path("/ccd", *a))
    assert rc == 1 and "REMOTE_TASK_TOKEN" in err, (rc, err)


def main_reports_a_discovery_refusal():
    ws, p = ws_and_env()

    def refuse(relay, secret):
        raise register.RegisterError("the relay refused the agent credential; reconnect the agent")
    rc, _, err = run_main(["--env-file", str(p)], resolve_workspace=lambda: ws, which=which({"node"}),
                          _discover=refuse)
    assert rc == 1 and "refused" in err, (rc, err)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def http_discover_sends_the_bearer_and_parses():
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), req.get_header("User-agent")))
        return FakeResponse(json.dumps(URLS).encode())
    with Patch(urlopen=urlopen):
        got = register.http_discover("https://chat.example/relay/", SECRET)
    assert got == URLS, got
    assert seen[0][0] == "https://chat.example/relay/v1/mcp/discovery", seen
    assert seen[0][1] == f"Bearer {SECRET}" and seen[0][2], seen


def http_discover_maps_errors():
    for code, words in ((404, "no hosted MCP"), (401, "refused"), (500, "HTTP 500")):
        def urlopen(req, timeout, code=code):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        with Patch(urlopen=urlopen):
            try:
                register.http_discover("https://chat.example/relay", SECRET)
            except register.RegisterError as e:
                assert words in str(e) and SECRET not in str(e), (code, str(e))
                continue
        raise AssertionError(f"HTTP {code} was not reported")


def empty_env_file_is_refused():
    p = env_file("OTHER=1\n")
    try:
        register.build_descriptor(p, fake_discover([]))
    except register.RegisterError as e:
        assert "REMOTE_TASK_TOKEN" in str(e), str(e)
        return
    raise AssertionError("an env file without a credential must be refused")


def settings_default_to_the_manifest_under_the_workspace():
    declared = json.loads(register.MANIFEST.read_text())["config"]
    assert set(declared) == set(register.PATH_SETTINGS), declared
    got = register.settings(Path("/ws"), environ={})
    for key, rel in declared.items():
        assert got[key] == Path("/ws") / rel, (key, got[key])


def settings_env_overrides_the_manifest():
    got = register.settings(Path("/ws"), environ={"AG2_MCP_LOG": "/var/log/p.log", "AG2_MCP_ROOM_ACTIONS": "x/r.jsonl"})
    assert got["AG2_MCP_LOG"] == Path("/var/log/p.log"), got
    assert got["AG2_MCP_ROOM_ACTIONS"] == Path("/ws/x/r.jsonl"), got


def settings_refuse_an_undeclared_key():
    with Patch(PATH_SETTINGS=register.PATH_SETTINGS + ("AG2_MCP_NOT_DECLARED",)):
        try:
            register.settings(Path("/ws"), environ={})
        except register.RegisterError as e:
            assert "AG2_MCP_NOT_DECLARED" in str(e), str(e)
            return
    raise AssertionError("an undeclared setting must be refused, not invented")


def loose_env_file_gets_a_chmod_warning():
    ws, p = ws_and_env()
    os.chmod(p, 0o644)
    rc, _, err = run_main(["--env-file", str(p)], resolve_workspace=lambda: ws, which=which({"node"}))
    assert rc == 0 and "accessible by other users" in err and "chmod 600" in err, (rc, err)
    os.chmod(p, 0o600)
    rc, _, err = run_main(["--env-file", str(p)], resolve_workspace=lambda: ws, which=which({"node"}))
    assert rc == 0 and "chmod" not in err, (rc, err)


for name, fn in list(globals().items()):
    helpers = ("check", "env_file", "fake_discover", "run_main", "ws_and_env", "which")
    if callable(fn) and fn.__module__ == "__main__" and name not in helpers and not isinstance(fn, type):
        check(name, fn)

if FAILS:
    print("ag2-space-mcp register: FAIL")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("ag2-space-mcp register: ok")
