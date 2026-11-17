"""One install connected to three AG2 Space servers, for gateway-client delegation tests.

ag2.space is the legacy unsuffixed row, s.space the primary, dev.s.space a third row. Each row's
token sits in the environment under its own token_ref, so a client that reads the process
environment's REMOTE_TASK_TOKEN gets ag2.space's, and only one that asks
`connections.room_credential` gets the room's own server's. The caller isolates the process
environment first (tests/_helpers/hermetic_env.py) and puts `<repo>/src` on sys.path.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PRIMARY = "s.space"
SERVERS = {
    "ag2.space": ("", "https://chat.ag2.space/relay", "ag2-secret"),
    "s.space": ("s-space", "https://chat.s.space/relay", "s-secret"),
    "dev.s.space": ("dev-s-space", "https://chat.dev.s.space/relay", "dev-secret"),
}
ROOMS = {server: f"!room:{server}" for server in SERVERS}
UNSERVED_ROOM = "!room:other.example.org"


def instance(server: str) -> str:
    return SERVERS[server][0]


def gateway(server: str) -> str:
    return SERVERS[server][1]


def secret(server: str) -> str:
    return SERVERS[server][2]


def token(server: str) -> str:
    return f"{gateway(server)}|{secret(server)}"


def origin(server: str) -> str:
    return gateway(server).rsplit("/", 1)[0]


class ThreeConnections:
    """Writes the record, the tokens and the channel home for `case`; undone at its cleanup."""

    def __init__(self, case: unittest.TestCase, primary: str = PRIMARY):
        import connections
        self.connections = connections
        self.root = Path(tempfile.mkdtemp(prefix="three-connections-"))
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.workspace = self.root / "workspace"
        self.config = self.root / "claude-config"
        (self.config / "channels").mkdir(parents=True)
        rows = [{"server": server, "instance": inst, "gateway_url": url,
                 "token_ref": connections.token_ref_for(inst), "primary": server == primary,
                 "added_by": "env", "added_at": "2026-10-01T00:00:00Z", "verified": True,
                 "server_source": "identity"} for server, (inst, url, _s) in SERVERS.items()]
        path = connections.record_path(self.workspace)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": connections.SCHEMA_VERSION, "connections": rows}))
        env = {connections.token_ref_for(instance(s)): token(s) for s in SERVERS}
        env["CLAUDE_CONFIG_DIR"] = str(self.config)
        for patcher in (mock.patch.dict("os.environ", env),
                        mock.patch("workspace_default.resolve_workspace", return_value=self.workspace)):
            patcher.start()
            case.addCleanup(patcher.stop)

    def channel_file(self, server: str, text: str) -> Path:
        """`channels/<the row's channel dir>/.env` holding `text`."""
        inst = instance(server)
        name = self.connections.channel_dir_for(inst) if inst else "ag2space"
        path = self.config / "channels" / name / ".env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
        return path
