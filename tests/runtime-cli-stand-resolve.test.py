#!/usr/bin/env python3
"""`sutando stand resolve` HUMAN + JSON dispatch — the real CLI exit contract.

0 = authorized hit, 1 = absent (with the verified-unlinked hint), 3 =
multi-Stand conflict, 4 = store corrupt. Corruption must render explicitly
in both modes — never collapse into "not linked" (kewei's P2 control).

Run: python3 tests/runtime-cli-stand-resolve.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"
TMP = tempfile.mkdtemp(prefix="stand-resolve-")

PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "runtime-state.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "ha"),
       "SUTANDO_RUNTIME_STATE": str(Path(TMP) / "state"),
       "SUTANDO_AGENT_ID": "@sr-test:example.org",
       "SUTANDO_HOST_LABEL": "stand-resolve-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       # hermetic channels dir for the entrance rows on the stand card
       "CLAUDE_CONFIG_DIR": str(Path(TMP) / "claude-home"),
       "REMOTE_TASK_URL": "", "REMOTE_TASK_TOKEN": "t"}

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def cli(*args):
    return subprocess.run([*PYBASE, str(CLI), *args], capture_output=True,
                          text=True, timeout=30, env=ENV)


def wait_socket(path, timeout=10):
    import socket as _s
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        try:
            s.connect(path)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def write_links(links):
    auth = Path(ENV["SUTANDO_RUNTIME_STATE"]) / "auth"
    auth.mkdir(parents=True, exist_ok=True)
    (auth / "entrance-links.json").write_text(
        links if isinstance(links, str) else json.dumps(links))


def main() -> int:
    (Path(TMP) / "state" / "auth").mkdir(parents=True)
    (Path(TMP) / "state" / "auth" / "ag2space.json").write_text(json.dumps(
        {"agent_id": "@sr-test:example.org"}))
    daemon = subprocess.Popen([*PYBASE, str(SERVER)], env=ENV,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    if not wait_socket(ENV["SUTANDO_RUNTIME_SOCKET"]):
        daemon.kill()
        print(daemon.stdout.read())
        raise AssertionError("daemon socket never came up")
    try:
        drive()
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
    print(f"\n{'FAILED' if FAILS else 'PASS'} — stand resolve CLI "
          f"({len(FAILS)} failure(s))")
    return 1 if FAILS else 0


def drive():
    write_links([{"link_id": "l1", "provider": "discord", "status": "active",
                  "stand_id": "@sr-test:example.org",
                  "authorized_by": "@o:x",
                  "provider_subject": {"type": "bot_user", "id": "42"},
                  "display": {"name": "SutandoBot"},
                  "verification": {"method": "discord_token_introspection",
                                   "verified_at": "2026-08-23T00:00:00Z"}}])
    p = cli("sutando", "stand", "resolve", "discord", "42")
    check(p.returncode == 0 and "@sr-test:example.org" in p.stdout,
          "authorized hit exits 0")
    check("SutandoBot" in p.stdout and "discord_token_introspection" in p.stdout,
          "resolve human output renders display + verification")

    write_links([{"link_id": "l2", "provider": "discord", "status": "active",
                  "stand_id": "@sr-test:example.org",
                  "provider_subject": {"type": "bot_user", "id": "42"}}])
    p = cli("sutando", "stand", "resolve", "discord", "42")
    check(p.returncode == 1 and "awaiting owner authorization" in p.stderr,
          "verified-unlinked exits 1 and SAYS why (human)")

    write_links([
        {"link_id": "a", "provider": "discord", "status": "active",
         "stand_id": "@s1:x", "authorized_by": "@o:x",
         "provider_subject": {"type": "bot_user", "id": "42"}},
        {"link_id": "b", "provider": "discord", "status": "active",
         "stand_id": "@s2:x", "authorized_by": "@o:x",
         "provider_subject": {"type": "bot_user", "id": "42"}}])
    p = cli("sutando", "stand", "resolve", "discord", "42")
    check(p.returncode == 3, "conflict exits 3 (human)")
    p = cli("sutando", "stand", "resolve", "discord", "42", "--json")
    check(p.returncode == 3 and json.loads(p.stdout).get("conflict"),
          "conflict exits 3 with JSON body")

    write_links("{broken ledger")
    p = cli("sutando", "stand", "resolve", "discord", "42")
    check(p.returncode == 4 and "unreadable" in p.stderr,
          "corrupt store exits 4 and names corruption (human) — never "
          "'not linked'")
    check("not linked" not in p.stderr,
          "corrupt rendering does not collapse into absence")
    p = cli("sutando", "stand", "resolve", "discord", "42", "--json")
    check(p.returncode == 4 and json.loads(p.stdout).get("store_corrupt"),
          "corrupt store exits 4 with JSON body")

    p = cli("sutando", "stand", "resolve", "discord")
    check(p.returncode == 2 and "usage" in p.stderr,
          "resolve arity error exits 2 with usage")

    # ---- stand card rendering (the `sutando stand` human surface) ----
    write_links([{
        "link_id": "l9", "provider": "discord", "status": "active",
        "stand_id": "@sr-test:example.org", "authorized_by": "@own:x",
        "provider_subject": {"type": "bot_user", "id": "42"},
        "display": {"name": "SutandoBot"},
        "verification": {"method": "discord_token_introspection",
                         "verified_at": "2026-08-23T00:00:00Z"},
        "credential": {"kind": "bot_token", "status": "verified",
                       "fingerprint": "sha256:abcd"}}])

    p = cli("sutando", "stand")  # before channel/device/stand records exist
    check(p.returncode == 0 and "Owner   Not established" in p.stdout,
          "card: owner reads Not established without a binding record")
    check("none configured" in p.stdout, "card: empty channels are explicit")
    check("No enrolled devices" in p.stdout, "card: empty devices are explicit")

    dc = Path(ENV["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
    dc.mkdir(parents=True)
    (dc / ".env").write_text("DISCORD_TOKEN=sk-hush")
    (dc / "access.json").write_text(json.dumps({"tofuOwner": "777"}))
    ag = Path(ENV["CLAUDE_CONFIG_DIR"]) / "channels" / "ag2space"
    ag.mkdir(parents=True)
    (ag / "access.json").write_text(json.dumps({"tofuOwner": "@own:x"}))

    p = cli("sutando", "stand")
    check(p.returncode == 0 and "SutandoBot   bot_user:42" in p.stdout,
          "card: active entrance renders display + identity")
    check("discord:user:777 via discord" in p.stdout
          and "@own:x via ag2space" in p.stdout,
          "card: owner evidence renders in both subject forms")
    check("sk-hush" not in p.stdout, "card: credential material never leaks")

    auth = Path(ENV["SUTANDO_RUNTIME_STATE"]) / "auth"
    (auth / "stand.json").write_text(json.dumps({
        "display_name": "SR Stand", "status": "active",
        "owners": [{"person_id": "@own:x", "display_name": "Kew",
                    "role": "owner"}]}))
    devs = auth / "devices"
    devs.mkdir()
    (devs / "d1.json").write_text(json.dumps({
        "device_id": "d1", "label": "phone", "device_type": "mobile",
        "token_sha256": "aa"}))

    p = cli("sutando", "stand")
    check(p.returncode == 0 and "SR Stand" in p.stdout, "card: display name")
    check("Owner   Kew (@own:x)   owner" in p.stdout, "card: owner row")
    check("phone" in p.stdout and "enrolled" in p.stdout, "card: device row")

    p = cli("sutando", "stand", "id")
    check(p.returncode == 0 and p.stdout.strip() == "@sr-test:example.org",
          "stand id prints the bare id")

    p = cli("sutando", "stand", "entrances", "--details")
    check(p.returncode == 0 and "sha256:abcd" in p.stdout
          and "discord_token_introspection" in p.stdout,
          "entrances --details: fingerprint + verification method")
    check(str(dc) in p.stdout, "entrances --details: storage directory")


if __name__ == "__main__":
    sys.exit(main())
