#!/usr/bin/env python3
"""Gateway access.json must resolve from the active launcher config only."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

SAVED_ENV = {
    key: os.environ.get(key)
    for key in (
        "AG2_DEVICE_ENV",
        "CLAUDE_CONFIG_DIR",
        "HOME",
        "REMOTE_TASK_TOKEN",
    )
}
os.environ["REMOTE_TASK_TOKEN"] = "https://gw.example/relay|test-token"

import ag2_sparrow.remote_gateway_bridge as bridge  # noqa: E402


failures = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        failures.append(f"{name}: {detail}")


root = Path(tempfile.mkdtemp(prefix="gateway-access-path-test-"))
home = root / "home"
stale_channel = home / ".claude" / "channels" / "ag2space"
stale_channel.mkdir(parents=True)
(stale_channel / "access.json").write_text(
    json.dumps({"tierMap": {"@teammate:ag2.space": "team"}})
)

device_channel = root / "device" / "channels" / "ag2space"
device_channel.mkdir(parents=True)
device_env = device_channel / ".env"
device_env.write_text("REMOTE_TASK_TOKEN='https://gw.example/relay|device-token'\n")
(device_channel / "access.json").write_text(
    json.dumps({"tierMap": {"@teammate:ag2.space": "other"}})
)

config_root = root / "config"
config_channel = config_root / "channels" / "ag2space"
config_channel.mkdir(parents=True)
(config_channel / "access.json").write_text(
    json.dumps({"tierMap": {"@teammate:ag2.space": "team"}})
)

try:
    os.environ["HOME"] = str(home)
    os.environ["AG2_DEVICE_ENV"] = str(device_env)
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_root)

    bridge._ACCESS_PATH_LOGGED = None
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        resolved = bridge._ag2space_access_path()
    check(
        "AG2_DEVICE_ENV resolves sibling access.json",
        resolved == str(device_channel / "access.json"),
        resolved,
    )
    check(
        "resolved access path is logged",
        str(device_channel / "access.json") in stderr.getvalue(),
        stderr.getvalue(),
    )

    os.environ.pop("AG2_DEVICE_ENV")
    bridge._ACCESS_PATH_LOGGED = None
    check(
        "CLAUDE_CONFIG_DIR is the non-desktop fallback",
        bridge._ag2space_access_path() == str(config_channel / "access.json"),
    )

    os.environ.pop("CLAUDE_CONFIG_DIR")
    bridge._ACCESS_PATH_LOGGED = None
    check(
        "no launcher config never guesses stale ~/.claude",
        bridge._ag2space_access_path() == "",
    )

    os.environ["AG2_DEVICE_ENV"] = str(device_env)
    bridge.LOCAL_TIER = "owner"
    bridge._TIER_MAP_CACHE = {"path": None, "ident": None, "map": {}}
    check(
        "tier map comes from active device config, not stale home",
        bridge._tier_for("@teammate:ag2.space", "owner") == "guest",
    )

    # A runtime path switch must re-read even when both files happen to have the
    # same mtime. The old cache tracked only mtime and could retain the prior map.
    second_channel = root / "second" / "channels" / "ag2space"
    second_channel.mkdir(parents=True)
    second_env = second_channel / ".env"
    second_env.write_text("REMOTE_TASK_TOKEN='https://gw.example/relay|second'\n")
    second_access = second_channel / "access.json"
    second_access.write_text(
        json.dumps({"tierMap": {"@teammate:ag2.space": "team"}})
    )
    first_access = device_channel / "access.json"
    first_stat = first_access.stat()
    os.utime(second_access, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    second_stat = second_access.stat()
    bridge._TIER_MAP_CACHE["ident"] = (
        second_stat.st_mtime_ns,
        second_stat.st_size,
        second_stat.st_ino,
    )
    os.environ["AG2_DEVICE_ENV"] = str(second_env)
    check(
        "path change invalidates an identity-colliding cache",
        bridge._tier_for("@teammate:ag2.space", "owner") == "team",
    )

    # A different configured path is a different trust boundary. If its map is
    # absent, never reuse the prior install's cached decisions.
    missing_channel = root / "missing" / "channels" / "ag2space"
    missing_channel.mkdir(parents=True)
    missing_env = missing_channel / ".env"
    missing_env.write_text("REMOTE_TASK_TOKEN='https://gw.example/relay|missing'\n")
    os.environ["AG2_DEVICE_ENV"] = str(missing_env)
    check(
        "missing map after path switch clears the previous install's tiers",
        bridge._tier_for("@teammate:ag2.space", "owner") == bridge.LOCAL_TIER,
    )

    # Once this configured path has loaded successfully, a transient same-path
    # read failure retains its own last-known-good map.
    missing_access = missing_channel / "access.json"
    missing_access.write_text(
        json.dumps({"tierMap": {"@teammate:ag2.space": "other"}})
    )
    check(
        "new path loads after its map appears",
        bridge._tier_for("@teammate:ag2.space", "owner") == "guest",
    )
    missing_access.unlink()
    check(
        "same-path transient failure retains its own last-known-good map",
        bridge._tier_for("@teammate:ag2.space", "owner") == "guest",
    )

    # Removing both launcher pointers is an explicit disable, not a transient
    # read failure, so no prior trust decision may survive it.
    os.environ.pop("AG2_DEVICE_ENV")
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    check(
        "removing launcher config clears a previously loaded tier map",
        bridge._tier_for("@teammate:ag2.space", "owner") == bridge.LOCAL_TIER,
    )
finally:
    for key, value in SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


if failures:
    print("\nFAILED")
    for failure in failures:
        print(" - " + failure)
    raise SystemExit(1)
print(f"\nPASS — {10} access-path checks")
