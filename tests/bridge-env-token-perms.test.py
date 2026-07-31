#!/usr/bin/env python3
"""
Behavioral test for the token-.env permission tightening in
src/discord-bridge.py and src/slack-bridge.py.

Regression context (PR #1983): both bridges used to nest the
`os.chmod(<channel .env>, 0o600)` INSIDE the token-missing branch
(`if not TOKEN` / `if not BOT_TOKEN or not APP_TOKEN`). When the tokens
were already present in the process env, that branch was skipped and a
world-readable `.env` (mode 0644) survived startup — a token file left
group/other-readable. The fix hoists the existence-check + chmod above
the token-load guard so perms are tightened whenever the file exists,
regardless of env state, then reads the tokens out only when they aren't
already in env.

The existing bridge-access tests are source-match / string-grep and never
EXECUTE the module-import token path, so the chmod + parse lines showed as
uncovered under the diff-coverage gate. This test drives the real import
path hermetically:

  * temp CLAUDE_CONFIG_DIR with a `channels/<discord|slack>/.env` written
    at mode 0644,
  * env tokens UNSET so the parse loop runs (and, for the chmod-always
    contract, a second load with env tokens SET so the guard is skipped
    but chmod still fires),
  * stub the SDK (discord / slack_bolt) so no network / no real client,
  * SUTANDO_TEST_MODE + SUTANDO_WORKSPACE redirect the workspace to a temp
    dir so the bridge's TASKS_DIR.mkdir(...) side effects stay hermetic,
  * assert the .env is 0600 after import AND the tokens were parsed.

Run: python3 tests/bridge-env-token-perms.test.py
Exit code: 0 on pass / skip, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _stub_discord() -> None:
    """Install a minimal `discord` stub so `discord.Client(...)` at
    discord-bridge import time doesn't need the real library / network."""
    stub = types.ModuleType("discord")

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

        def event(self, fn):
            return fn

        def get_channel(self, _):
            return None

    class _AllowedMentions:
        def __init__(self, *a, **kw):
            pass

    stub.Intents = _Intents
    stub.Client = _Client
    stub.AllowedMentions = _AllowedMentions
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    stub.TextChannel = type("TextChannel", (), {})
    stub.Thread = type("Thread", (), {})
    stub.errors = types.SimpleNamespace(
        HTTPException=type("HTTPException", (Exception,), {}),
        Forbidden=type("Forbidden", (Exception,), {}),
        NotFound=type("NotFound", (Exception,), {}),
    )
    sys.modules["discord"] = stub


def _stub_slack_bolt() -> None:
    """Install a minimal `slack_bolt` stub so `App(token=...)` at
    slack-bridge import time doesn't fire the real auth.test network call."""

    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace()

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

    try:
        import slack_bolt as _real_bolt  # noqa: F401
        _real_bolt.App = _StubApp
    except ImportError:
        m = types.ModuleType("slack_bolt")
        m.App = _StubApp
        sys.modules["slack_bolt"] = m
        adapter = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter
        sm = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm


def _load_bridge(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, REPO / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run_case(
    *,
    label: str,
    channel: str,
    filename: str,
    env_contents: str,
    env_tokens: dict,
    token_setup,
    assert_tokens,
    chmod_should_fail: bool = False,
) -> list[str]:
    """Set up a hermetic env, import the bridge, and check the .env perms.

    `token_setup` receives the os.environ dict pre-import (to set/unset the
    bridge's token env vars). `assert_tokens(mod)` returns a list of failure
    strings for the parsed-token expectations.

    When `chmod_should_fail` is True, `os.chmod` is patched to raise OSError for
    the channel `.env` (simulating a read-only volume / wrong-ownership /
    ACL-restricted file). The import MUST still succeed (the bridge warns and
    continues) and the tokens MUST still parse — the fix's whole point is that a
    non-chmod-able but readable token file no longer crashes the bridge at
    startup. In that mode the file stays 0644 (chmod never applied)."""
    fails: list[str] = []
    saved_env = dict(os.environ)
    saved_modules = {
        k: sys.modules.get(k)
        for k in ("discord", "slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode")
    }
    # Drop any already-imported copy of the bridge module so exec runs fresh.
    for k in ("discord_bridge_perms_ut", "slack_bridge_perms_ut"):
        sys.modules.pop(k, None)

    try:
        cfg_dir = Path(tempfile.mkdtemp(prefix=f"sutando-{channel}-perms-ccd-"))
        ws_dir = Path(tempfile.mkdtemp(prefix=f"sutando-{channel}-perms-ws-"))
        chan_dir = cfg_dir / "channels" / channel
        chan_dir.mkdir(parents=True, exist_ok=True)
        env_file = chan_dir / ".env"
        env_file.write_text(env_contents)
        os.chmod(env_file, 0o644)  # world-readable — the condition the fix must fix

        if _mode(env_file) != 0o644:
            # Some filesystems clamp perms; skip rather than false-fail.
            print(f"  SKIP [{label}]: fs won't honor 0644 (got {oct(_mode(env_file))})")
            return fails

        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
        os.environ.pop("CLAUDE_HOME", None)
        os.environ["SUTANDO_TEST_MODE"] = "1"
        os.environ["SUTANDO_WORKSPACE"] = str(ws_dir)
        token_setup(os.environ)

        # Optionally simulate a chmod-hostile filesystem: os.chmod raises for
        # the channel .env only (other chmods — access.json, workspace setup —
        # pass through untouched).
        _real_chmod = os.chmod
        if chmod_should_fail:
            def _chmod_shim(path, mode, *a, **k):
                if str(path) == str(env_file):
                    raise OSError(30, "Read-only file system (simulated)")
                return _real_chmod(path, mode, *a, **k)
            os.chmod = _chmod_shim

        try:
            if channel == "discord":
                _stub_discord()
                mod = _load_bridge("discord_bridge_perms_ut", filename)
            else:
                _stub_slack_bolt()
                mod = _load_bridge("slack_bridge_perms_ut", filename)
        except Exception as e:  # noqa: BLE001 — the whole point is import must NOT raise
            os.chmod = _real_chmod
            fails.append(f"{label}: bridge import crashed ({type(e).__name__}: {e}) — chmod failure must be non-fatal")
            return fails
        finally:
            os.chmod = _real_chmod

        after = _mode(env_file)
        if chmod_should_fail:
            # chmod raised → perms unchanged; the win is that import survived.
            if after != 0o644:
                fails.append(f"{label}: expected .env to stay 0644 when chmod fails, got {oct(after)}")
            else:
                print(f"  OK [{label}]: chmod failure survived — import continued, .env still 0644")
        elif after != 0o600:
            fails.append(f"{label}: .env perms should be 0600 after import, got {oct(after)}")
        else:
            print(f"  OK [{label}]: .env tightened 0644 -> 0600")

        for msg in assert_tokens(mod):
            fails.append(f"{label}: {msg}")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        for k, v in saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return fails


def main() -> int:
    fails: list[str] = []

    # ---- discord: tokens NOT in env -> chmod fires + parse loop runs ----
    def _discord_no_env(env):
        env.pop("DISCORD_BOT_TOKEN", None)

    def _discord_assert(mod):
        out = []
        if getattr(mod, "TOKEN", None) != "discord-token-from-file":
            out.append(f"TOKEN should be parsed from .env, got {getattr(mod, 'TOKEN', None)!r}")
        return out

    fails += _run_case(
        label="discord/no-env-token",
        channel="discord",
        filename="discord-bridge.py",
        env_contents="DISCORD_BOT_TOKEN=discord-token-from-file\n",
        env_tokens={},
        token_setup=_discord_no_env,
        assert_tokens=_discord_assert,
    )

    # ---- discord: token ALREADY in env -> chmod still fires (the fix) ----
    def _discord_env_present(env):
        env["DISCORD_BOT_TOKEN"] = "discord-token-from-env"

    def _discord_assert_env(mod):
        out = []
        # Env token wins; parse loop is skipped but chmod must still have run.
        if getattr(mod, "TOKEN", None) != "discord-token-from-env":
            out.append(f"env TOKEN should win, got {getattr(mod, 'TOKEN', None)!r}")
        return out

    fails += _run_case(
        label="discord/env-token-present",
        channel="discord",
        filename="discord-bridge.py",
        env_contents="DISCORD_BOT_TOKEN=discord-token-from-file\n",
        env_tokens={},
        token_setup=_discord_env_present,
        assert_tokens=_discord_assert_env,
    )

    # ---- slack: tokens NOT in env -> chmod fires + parse loop runs ----
    def _slack_no_env(env):
        env.pop("SLACK_BOT_TOKEN", None)
        env.pop("SLACK_APP_TOKEN", None)

    def _slack_assert(mod):
        out = []
        if getattr(mod, "BOT_TOKEN", None) != "xoxb-from-file":
            out.append(f"BOT_TOKEN should be parsed from .env, got {getattr(mod, 'BOT_TOKEN', None)!r}")
        if getattr(mod, "APP_TOKEN", None) != "xapp-from-file":
            out.append(f"APP_TOKEN should be parsed from .env, got {getattr(mod, 'APP_TOKEN', None)!r}")
        return out

    fails += _run_case(
        label="slack/no-env-token",
        channel="slack",
        filename="slack-bridge.py",
        env_contents='SLACK_BOT_TOKEN="xoxb-from-file"\nSLACK_APP_TOKEN="xapp-from-file"\n',
        env_tokens={},
        token_setup=_slack_no_env,
        assert_tokens=_slack_assert,
    )

    # ---- slack: tokens ALREADY in env -> chmod still fires (the fix) ----
    def _slack_env_present(env):
        env["SLACK_BOT_TOKEN"] = "xoxb-from-env"
        env["SLACK_APP_TOKEN"] = "xapp-from-env"

    def _slack_assert_env(mod):
        out = []
        if getattr(mod, "BOT_TOKEN", None) != "xoxb-from-env":
            out.append(f"env BOT_TOKEN should win, got {getattr(mod, 'BOT_TOKEN', None)!r}")
        if getattr(mod, "APP_TOKEN", None) != "xapp-from-env":
            out.append(f"env APP_TOKEN should win, got {getattr(mod, 'APP_TOKEN', None)!r}")
        return out

    fails += _run_case(
        label="slack/env-token-present",
        channel="slack",
        filename="slack-bridge.py",
        env_contents='SLACK_BOT_TOKEN="xoxb-from-file"\nSLACK_APP_TOKEN="xapp-from-file"\n',
        env_tokens={},
        token_setup=_slack_env_present,
        assert_tokens=_slack_assert_env,
    )

    # ---- discord: chmod FAILS (read-only fs) -> import survives + tokens parse ----
    fails += _run_case(
        label="discord/chmod-fails",
        channel="discord",
        filename="discord-bridge.py",
        env_contents="DISCORD_BOT_TOKEN=discord-token-from-file\n",
        env_tokens={},
        token_setup=_discord_no_env,
        assert_tokens=_discord_assert,
        chmod_should_fail=True,
    )

    # ---- slack: chmod FAILS (read-only fs) -> import survives + tokens parse ----
    fails += _run_case(
        label="slack/chmod-fails",
        channel="slack",
        filename="slack-bridge.py",
        env_contents='SLACK_BOT_TOKEN="xoxb-from-file"\nSLACK_APP_TOKEN="xapp-from-file"\n',
        env_tokens={},
        token_setup=_slack_no_env,
        assert_tokens=_slack_assert,
        chmod_should_fail=True,
    )

    if fails:
        for f in fails:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("all bridge-env-token-perms cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
