#!/usr/bin/env python3
"""skills/ag2-space-mcp/scripts/register.py: a thin wrapper over ag2-sparrow-mcp.

Discovery, the redirect refusal, the relay and mint_url rules and the token helper are
ag2_sparrow.mcp_auth's and are tested in packages/ag2-sparrow/tests/test_mcp_headers.py.
Here: the core's credential is what the entry points at, CLAUDE_CONFIG_DIR is never invented,
the secret never leaves the env file, and the real mcp_auth refuses a plaintext relay.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "ag2-space-mcp" / "scripts"))

import register  # noqa: E402

FAILS = []
SECRET = "s3cr3t-bearer-value"
MCP_URL = "https://mcp.example/mcp"


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  FAIL {name}: {e}")


def env_file(text, mode=0o600):
    p = Path(tempfile.mkdtemp()) / "relay-client.env"
    p.write_text(text)
    os.chmod(p, mode)
    return p


class FakeMcpAuth:
    """mcp_auth's surface register.py uses, recording what it was asked."""
    SERVER_NAME = "ag2-space"

    class McpAuthError(Exception):
        pass

    def __init__(self, fail=None):
        self.fail, self.seen = fail, []

    def resolve_gateway(self):
        return ("https://relay.example", SECRET, os.environ.get("AG2_DEVICE_ENV", ""))

    def setup_entry(self, gateway, source_root=""):
        self.seen.append((gateway, source_root))
        if self.fail:
            raise self.McpAuthError(self.fail)
        return {"type": "http", "url": MCP_URL,
                "headersHelper": f"AG2_DEVICE_ENV={gateway[2]} python -I -c '...{source_root}...'"}


def run_main(argv, fake=None, **patches):
    fake = fake or FakeMcpAuth()
    saved = {k: getattr(register, k) for k in patches}
    out, err = io.StringIO(), io.StringIO()
    try:
        for k, v in patches.items():
            setattr(register, k, v)

        def load(path):
            os.environ["AG2_DEVICE_ENV"] = str(path)
            return fake
        # patch.dict puts back what load() changes, so no run leaks into the next.
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = register.main(argv, load=load)
    finally:
        for k, v in saved.items():
            setattr(register, k, v)
    return rc, out.getvalue(), err.getvalue(), fake


def which(found):
    return lambda name: f"/usr/bin/{name}" if name in found else None


def the_entry_points_the_helper_at_the_cores_credential():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n")
    rc, out, err, fake = run_main(["--env-file", str(p)])
    assert rc == 0, (rc, err)
    gateway, root = fake.seen[0]
    assert gateway[2] == str(p), gateway
    assert root == str(register.SPARROW), root
    assert '"type": "http"' in out and MCP_URL in out and "not registered yet" in out, out
    assert SECRET not in out and SECRET not in err, "the secret was printed"


def apply_runs_claude_without_inventing_claude_config_dir():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n")
    seen = []
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    rc, out, err, _ = run_main(["--env-file", str(p), "--apply"], which=which({"claude"}),
                               run=lambda cmd, env: seen.append((cmd, env)) or types.SimpleNamespace(returncode=0))
    assert rc == 0, (rc, err)
    cmd, env = seen[0]
    assert cmd[:6] == ["claude", "mcp", "add-json", "--scope", "user", "ag2-space"], cmd
    assert "CLAUDE_CONFIG_DIR" not in env, env.get("CLAUDE_CONFIG_DIR")
    assert SECRET not in " ".join(cmd), "the secret is on the claude command line"
    assert "AG2_DEVICE_ENV" not in env, "claude got the narrowed resolution env instead of the caller's"


def apply_keeps_a_claude_config_dir_the_caller_set():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n")
    seen = []
    os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/the-callers-config"
    try:
        run_main(["--env-file", str(p), "--apply"], which=which({"claude"}),
                 run=lambda cmd, env: seen.append(env) or types.SimpleNamespace(returncode=0))
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    assert seen[0].get("CLAUDE_CONFIG_DIR") == "/tmp/the-callers-config", seen[0].get("CLAUDE_CONFIG_DIR")


def a_failed_add_says_to_remove_an_existing_entry():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n")
    rc, _, err, _ = run_main(["--env-file", str(p), "--apply"], which=which({"claude"}),
                             run=lambda cmd, env: types.SimpleNamespace(returncode=1))
    assert rc == 1 and "claude mcp remove --scope user ag2-space" in err, (rc, err)


def apply_without_claude_fails():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n")
    rc, _, err, _ = run_main(["--env-file", str(p), "--apply"], which=which(set()))
    assert rc == 1 and "claude is not on PATH" in err, (rc, err)


def without_a_credential_it_fails():
    rc, _, err, _ = run_main([], resolve_channel_env=lambda *a: None)
    assert rc == 1 and "no agent credential" in err, (rc, err)


def an_mcp_auth_refusal_is_a_register_line():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n")
    rc, _, err, _ = run_main(["--env-file", str(p)], fake=FakeMcpAuth(fail="mint_url is not on the relay's origin"))
    assert rc == 1 and err.startswith("register: mint_url is not on the relay's origin"), (rc, err)


def a_loose_env_file_gets_a_chmod_warning():
    p = env_file(f"REMOTE_TASK_TOKEN='https://relay.example|{SECRET}'\n", mode=0o644)
    _, _, err, _ = run_main(["--env-file", str(p)])
    assert "chmod 600" in err, err


def the_real_mcp_auth_refuses_a_plaintext_relay_with_nothing_sent():
    # End to end through the real ag2_sparrow.mcp_auth: http to a non-loopback host is refused
    # before any request, so the bearer never leaves this machine.
    p = env_file(f"REMOTE_TASK_TOKEN='http://relay.example|{SECRET}'\n")
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = register.main(["--env-file", str(p)])
    assert rc == 1 and "register:" in err.getvalue() and "the relay URL" in err.getvalue(), (rc, err.getvalue())
    assert SECRET not in out.getvalue() + err.getvalue()


for name, fn in list(globals().items()):
    helpers = ("check", "env_file", "run_main", "which")
    if callable(fn) and fn.__module__ == "__main__" and name not in helpers and not isinstance(fn, type):
        check(name, fn)

if FAILS:
    print("ag2-space-mcp register: FAIL")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("ag2-space-mcp register: ok")
