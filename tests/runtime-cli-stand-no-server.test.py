#!/usr/bin/env python3
"""`sutando stand` with no daemon behind the socket builds the card in-process.

A missing or refusing socket falls back to the same IdentityView the server
uses; the fallback never carries --details data (credentials, storage paths),
and --details without a server is refused.

Run: python3 tests/runtime-cli-stand-no-server.test.py   (stdlib only)
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

isolate_os_environ()

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "sutando"
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"
TMP = Path(tempfile.mkdtemp(prefix="stand-no-server-"))
STATE = TMP / "state"
CHANNELS = TMP / "claude-home" / "channels"

PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

ENV = {**isolated_child_env(),
       "SUTANDO_RUN_DIR": str(TMP / "run"),
       "SUTANDO_RUNTIME_SOCKET": str(TMP / "rt.sock"),
       "SUTANDO_RUNTIME_STATE": str(STATE),
       "SUTANDO_AGENT_ID": "@ns-test:example.org",
       "SUTANDO_HOST_LABEL": "stand-no-server-host",
       "CLAUDE_CONFIG_DIR": str(TMP / "claude-home"),
       "REMOTE_TASK_URL": "", "REMOTE_TASK_TOKEN": "t"}

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def cli(*args):
    return subprocess.run([*PYBASE, str(CLI), *args], capture_output=True,
                          text=True, timeout=60, env=ENV)


def keys_anywhere(obj) -> set:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in keys_anywhere(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in keys_anywhere(v)}
    return set()


def seed():
    (STATE / "auth" / "devices").mkdir(parents=True)
    (STATE / "auth" / "ag2space.json").write_text(json.dumps(
        {"agent_id": "@ns-test:example.org"}))
    (STATE / "auth" / "stand.json").write_text(json.dumps(
        {"display_name": "NoServer", "status": "active"}))
    (STATE / "auth" / "devices" / "d1.json").write_text(json.dumps(
        {"device_id": "d1", "token_sha256": "x", "granted_methods": ["a"]}))
    # an active, credentialed link: exactly what --details would expose
    (STATE / "auth" / "entrance-links.json").write_text(json.dumps([
        {"link_id": "l1", "provider": "discord", "status": "active",
         "stand_id": "@ns-test:example.org", "authorized_by": "@o:x",
         "provider_subject": {"type": "bot_user", "id": "42"},
         "credential": {"ref": "keychain:discord"},
         "verification": {"method": "discord_token_introspection"}}]))
    disc = CHANNELS / "discord"
    disc.mkdir(parents=True)
    (disc / ".env").write_text("DISCORD_TOKEN=secret\n")
    (disc / "access.json").write_text(json.dumps({"tofuOwner": "o-1"}))


def assert_safe_card(p, label):
    check(p.returncode == 0, f"{label}: exits 0 (rc={p.returncode}, "
                             f"stderr={p.stderr.strip()[:120]!r})")
    try:
        card = json.loads(p.stdout)
    except ValueError:
        check(False, f"{label}: stdout is a JSON card")
        return
    check({"stand", "channels"} <= set(card), f"{label}: has stand + channels")
    check(card.get("stand", {}).get("stand_id") == "@ns-test:example.org",
          f"{label}: stand_id from the enrolled record")
    check([c.get("provider") for c in card.get("channels", [])] == ["discord"],
          f"{label}: channel rows from the channels dir")
    leaked = keys_anywhere(card) & {"credential", "storage", "granted_methods"}
    check(not leaked, f"{label}: no --details keys anywhere ({sorted(leaked)})")
    check("secret" not in p.stdout and str(CHANNELS) not in p.stdout,
          f"{label}: no credential value or channel path in output")


def other_connect_errors():
    # Only a missing or refusing socket means "no server"; any other connect
    # error must surface, never become a silent in-process card.
    import contextlib
    import errno
    import importlib.util
    import io
    from unittest import mock
    os.environ.update(ENV)
    spec = importlib.util.spec_from_file_location("sutando_runtime_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for exc in (PermissionError(errno.EACCES, "Permission denied"),
                OSError(errno.ETIMEDOUT, "Operation timed out")):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(socket.socket, "connect", side_effect=exc), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(["sutando", "stand", "--json"])
        name = type(exc).__name__ + f"/{errno.errorcode[exc.errno]}"
        check(rc == 1 and str(exc.errno) in err.getvalue(),
              f"{name} on connect surfaces as an error (rc={rc})")
        check(out.getvalue() == "" and '"stand"' not in err.getvalue(),
              f"{name} on connect prints no stand card")


def desktop_install():
    # A desktop install has no ag2space.json: the agent is named only by the
    # gateway's channel .env, which must still give the card its stand_id.
    root = TMP / "desktop"
    lane = root / "claude-home" / "channels" / "ag2space"
    lane.mkdir(parents=True)
    (root / "state" / "auth").mkdir(parents=True)
    (lane / ".env").write_text("AGENT_ID=@desk-test:example.org\n"
                               "REMOTE_TASK_TOKEN=secret\n")
    env = {**ENV, "SUTANDO_RUNTIME_STATE": str(root / "state"),
           "CLAUDE_CONFIG_DIR": str(root / "claude-home"),
           "SUTANDO_RUNTIME_SOCKET": str(root / "rt.sock")}
    p = subprocess.run([*PYBASE, str(CLI), "sutando", "stand", "--json"],
                       capture_output=True, text=True, timeout=60, env=env)
    try:
        card = json.loads(p.stdout)
    except ValueError:
        card = {}
    check(card.get("stand", {}).get("stand_id") == "@desk-test:example.org",
          f"desktop install: stand_id from the lane .env (rc={p.returncode})")
    check("secret" not in p.stdout, "desktop install: no token in the card")


def main() -> int:
    seed()
    sys.path.insert(0, str(REPO / "src" / "runtime-api"))
    from identity_view import IdentityView
    full = IdentityView(STATE, "@ns-test:example.org",
                        channels_dir=CHANNELS).stand_card(details=True)
    check({"credential", "storage", "granted_methods"} <= keys_anywhere(full),
          "control: this seed does expose --details keys when asked")
    sock = ENV["SUTANDO_RUNTIME_SOCKET"]
    check(not Path(sock).exists(), "no socket file exists")
    assert_safe_card(cli("sutando", "stand", "--json"), "missing socket")

    human = cli("sutando", "stand")
    check(human.returncode == 0 and "@ns-test:example.org" in human.stdout,
          "human-mode card also falls back")

    p = cli("sutando", "stand", "--json", "--details")
    check(p.returncode != 0 and "--details" in p.stderr and not p.stdout,
          "--details with no server is refused")
    check("secret" not in p.stderr and "keychain" not in p.stderr,
          "--details refusal carries no credential data")

    # a bound-but-not-listening socket refuses the connection
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock)
    s.close()
    assert_safe_card(cli("sutando", "stand", "--json"), "refused socket")
    os.unlink(sock)

    # other subcommands keep surfacing the transport error
    p = cli("sutando", "info")
    check(p.returncode == 1 and "Errno 2" in p.stderr,
          "sutando info without a server still errors")

    other_connect_errors()
    desktop_install()

    if BIN.is_file():
        p = subprocess.run(["bash", str(BIN), "stand", "--json"],
                           capture_output=True, text=True, timeout=60, env=ENV)
        assert_safe_card(p, "bin/sutando front door")

    print(f"\n{'FAILED' if FAILS else 'PASS'} — stand card without a server "
          f"({len(FAILS)} failure(s))")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
