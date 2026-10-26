#!/usr/bin/env python3
"""Connections surface for the Sutando Server: connection.list / add / remove / set_primary / reconnect.

Thin binding over the connections DOMAIN module (src/connections.py), the one
writer of `state/connections.json`. The composer injects the resolved workspace,
the repo and the logs dir; this view never resolves them, never touches the vault
itself (the module does, through the injected `vault`), and returns rows that
carry a vault key name, never a secret. The methods are the daemon-native form
of the HTTP resource: GET = list, POST without confirm = preview, POST with
confirm = create (201 ↔ created true, 200 ↔ existing row), DELETE = remove,
POST <instance>/primary = set_primary, POST <instance>/reconnect = reconnect;
a confirm whose identity moved answers CONFLICT, the 409.

Mutation sits on the same footing as task.submit: the Unix socket is the
owner's, and no device grant is required.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # src/
import connections  # noqa: E402

from protocol import ProtocolError  # noqa: E402

CONFLICT = -32009


class ConnectionsView:
    def __init__(self, workspace: str | Path, repo: str | Path, logs_dir: str | Path,
                 vault=None, fetch=None, spawn=None, kill=None, getaddrinfo=None):
        self.workspace = Path(workspace)
        self.repo = Path(repo)
        self.logs_dir = Path(logs_dir)
        self.vault, self.fetch, self.spawn, self.kill = vault, fetch, spawn, kill
        self.getaddrinfo = getaddrinfo

    def _instance(self, params: dict) -> str:
        instance = params.get("instance")
        if not isinstance(instance, str):
            raise ProtocolError(-32602, "missing required param: instance")
        return instance

    # ── connection.list ─────────────────────────────────────────────────────
    def list_connections(self) -> dict:
        """{connections: [...]} — every row joined with status and state; [] without a record."""
        try:
            rows = connections.list_connections(self.workspace, vault=self.vault)
        except connections.SchemaError as e:
            raise ProtocolError(-32603, str(e))
        return {"connections": [connections.status_for(r, self.workspace) for r in rows]}

    # ── connection.add ──────────────────────────────────────────────────────
    def add(self, params: dict) -> dict:
        """confirm false (default): {preview}. confirm true + expected_server: {created, connection}."""
        token = params.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ProtocolError(-32602, "missing required param: token")
        confirm = params.get("confirm", False)
        if not isinstance(confirm, bool):
            raise ProtocolError(-32602, "confirm must be a boolean")
        try:
            preview = connections.preview_connection(token, fetch=self.fetch, getaddrinfo=self.getaddrinfo)
            if not confirm:
                return {"preview": preview}
            expected = params.get("expected_server")
            if not isinstance(expected, str) or not expected.strip():
                raise ProtocolError(-32602, "confirm needs expected_server (the server the preview showed)")
            try:
                row, created = connections.add_connection(
                    token, added_by=str(params.get("added_by", "owner")), expected_server=expected,
                    instance=params.get("instance"), preview=preview,
                    workspace=self.workspace, vault=self.vault)
            except connections.ConnectionMismatch as e:
                raise ProtocolError(CONFLICT, f"mismatch: {e}")
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"created": created, "connection": connections.status_for(row, self.workspace)}

    # ── connection.remove ───────────────────────────────────────────────────
    def remove(self, params: dict) -> dict:
        instance = self._instance(params)
        try:
            row = connections.remove_connection(instance, force=bool(params.get("force", False)),
                                                workspace=self.workspace, vault=self.vault)
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"removed": row}

    # ── connection.set_primary ──────────────────────────────────────────────
    def set_primary(self, params: dict) -> dict:
        instance = self._instance(params)
        try:
            row = connections.set_primary(instance, workspace=self.workspace)
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"connection": connections.status_for(row, self.workspace)}

    # ── connection.reconnect ────────────────────────────────────────────────
    def reconnect(self, params: dict) -> dict:
        instance = self._instance(params)
        try:
            record = connections.reconnect(instance, repo=self.repo, logs_dir=self.logs_dir,
                                           workspace=self.workspace, vault=self.vault,
                                           spawn=self.spawn, kill=self.kill, fetch=self.fetch,
                                           getaddrinfo=self.getaddrinfo)
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"launch": record}
