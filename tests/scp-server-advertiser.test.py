#!/usr/bin/env python3
"""The SCP server owns its mDNS advertisement: spawned on WSS start with the
agent LOCALPART as the instance name (device firmware pins the agent= TXT
field — a full mxid would never match), terminated with the server. Pins the
argv contract so the advertisement can't silently drift from what devices pin.
"""
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

import server  # noqa: E402


def _spawn(agent, port, platform="darwin", which="/usr/bin/dns-sd"):
    calls = []

    def fake_popen(argv, **kw):
        calls.append(argv)
        return mock.Mock(pid=4242)

    with mock.patch.object(server.sys, "platform", platform), \
         mock.patch.object(server.shutil, "which", return_value=which), \
         mock.patch.object(server.subprocess, "Popen", side_effect=fake_popen):
        proc = server.RuntimeServer._start_advertiser(None, agent, port)
    return proc, calls


def test_name_is_the_agent_localpart():
    proc, calls = _spawn("@sutando-qingyun-001:ag2.space", 8787)
    assert proc is not None
    [argv] = calls
    assert argv[:3] == ["dns-sd", "-R", "sutando-qingyun-001"]
    assert "_sutando-scp._tcp." in argv
    assert "8787" in argv
    assert "agent=sutando-qingyun-001" in argv


def test_no_agent_falls_back_to_sutando():
    _, calls = _spawn(None, 8787)
    [argv] = calls
    assert argv[2] == "sutando"
    assert "agent=sutando" in argv


def test_non_darwin_and_missing_dnssd_skip():
    proc, calls = _spawn("@a:b", 8787, platform="linux")
    assert proc is None and calls == []
    proc, calls = _spawn("@a:b", 8787, which=None)
    assert proc is None and calls == []


def test_spawn_failure_is_nonfatal():
    with mock.patch.object(server.sys, "platform", "darwin"), \
         mock.patch.object(server.shutil, "which", return_value="/usr/bin/dns-sd"), \
         mock.patch.object(server.subprocess, "Popen",
                           side_effect=OSError("spawn denied")):
        proc = server.RuntimeServer._start_advertiser(None, "@a:b", 8787)
    assert proc is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
