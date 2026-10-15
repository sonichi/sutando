"""Invoke dispatcher-injected room capabilities without resolving credentials."""
import datetime
import json
import subprocess

from collection_pass import collect_pass
from window_state import _timestamp


def transport_timestamp(ms):
    encoded = datetime.datetime.fromtimestamp(_timestamp(ms) / 1000, datetime.timezone.utc).isoformat()
    return datetime.datetime.fromisoformat(encoded).timestamp() * 1000


def collect_commands(directory, capabilities, bootstrap_ms, until_ms, pages=20, timeout=600, runner=None):
    bootstrap_ms, until_ms = (transport_timestamp(ms) for ms in (bootstrap_ms, until_ms))
    if not isinstance(capabilities, dict) or not capabilities:
        raise ValueError("configured scope capabilities required")
    for scope, argv in capabilities.items():
        if not isinstance(scope, str) or not scope or not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("capability must be an explicit argument vector")
    if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 100:
        raise ValueError("invalid page budget")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 1200:
        raise ValueError("invalid command timeout")
    runner = runner or subprocess.run

    def invoke(scope, args):
        result = runner(capabilities[scope] + ["--strict"] + args, capture_output=True, text=True, timeout=timeout)
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or result.returncode not in (0, 1):
            raise ValueError("capability failed or returned malformed receipt")
        if result.returncode == 1 and value.get("ok") is not False:
            raise ValueError("inconsistent capability outcome")
        if result.returncode == 0 and value.get("ok") is not True:
            raise ValueError("missing strict capability success")
        return value

    def enumerate_rooms(scope):
        value = invoke(scope, ["rooms"])
        if value.get("ok") is not True:
            raise ValueError("membership enumeration unavailable")
        return value.get("rooms")

    def collect(scope, since, until):
        def iso(ms):
            return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()
        return invoke(scope, ["history", "--since", iso(since), "--until", iso(until), "--pages", str(pages)])

    return collect_pass(directory, list(capabilities), bootstrap_ms, until_ms, enumerate_rooms, collect)
