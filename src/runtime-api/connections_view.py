#!/usr/bin/env python3
"""Connections surface for the Sutando Server: connection.list / known_servers / add / remove /
set_primary / promote / park / park_legacy / unpark / disconnect / reconnect / update_token.

Thin binding over the connections DOMAIN module (src/connections.py), the one
writer of `state/connections.json`. The composer injects the resolved workspace,
the repo and the logs dir; this view never resolves them, never touches the vault
itself (the module does, through the injected `vault`), and returns rows that
carry a vault key name, never a secret. The methods are the daemon-native form
of the HTTP resource: GET = list, POST without confirm = preview, POST with
confirm = create and start the bridge (201 ↔ created true, 200 ↔ existing row), DELETE = remove,
POST <instance>/primary = set_primary, POST <instance>/promote = promote,
POST <instance>/park = park, POST park-legacy = park_legacy, POST <instance>/unpark = unpark, POST <instance>/disconnect = disconnect,
POST <instance>/reconnect = reconnect, POST <instance>/token = update_token,
GET known-servers = known_servers; a confirm whose identity moved and every state
refusal the module codes (`connections.Refusal`: a disconnect of the primary, a
disabled row made primary, the legacy row, a shadowed token source, a new code for
another server or agent or one unconfirmed or rejected, a bridge that will not stop)
answer CONFLICT, the 409, with the module's own message.

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
        """{connections: [...], capabilities: [...]} — every row joined with status and state ([] without
        a record), and what this engine's connections support (`connections.CAPABILITIES`)."""
        try:
            rows = connections.list_connections(self.workspace, vault=self.vault)
        except connections.SchemaError as e:
            raise ProtocolError(-32603, str(e))
        return {"connections": [connections.status_for(r, self.workspace) for r in rows],
                "capabilities": list(connections.CAPABILITIES)}

    # ── connection.known_servers ────────────────────────────────────────────
    def known_servers(self) -> dict:
        """{servers: [...]} — the resolver's DotSpace instances joined with the rows; nothing fetched."""
        try:
            return {"servers": connections.known_servers(self.workspace)}
        except connections.SchemaError as e:
            raise ProtocolError(-32603, str(e))

    # ── connection.add ──────────────────────────────────────────────────────
    def add(self, params: dict) -> dict:
        """confirm false (default): {preview}. confirm true + expected_server: {created, launch, connection}."""
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
                    workspace=self.workspace, vault=self.vault, kill=self.kill)
            except connections.ConnectionMismatch as e:
                raise ProtocolError(CONFLICT, f"mismatch: {e}")
            launch = connections.start_added(row, repo=self.repo, logs_dir=self.logs_dir,
                                             workspace=self.workspace, vault=self.vault, spawn=self.spawn)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"created": created, "launch": launch, "connection": connections.status_for(row, self.workspace)}

    # ── connection.remove ───────────────────────────────────────────────────
    def remove(self, params: dict) -> dict:
        instance = self._instance(params)
        try:
            row = connections.remove_connection(instance, force=bool(params.get("force", False)),
                                                workspace=self.workspace, vault=self.vault, kill=self.kill)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"removed": row}

    # ── connection.set_primary ──────────────────────────────────────────────
    def set_primary(self, params: dict) -> dict:
        instance = self._instance(params)
        try:
            row = connections.set_primary(instance, workspace=self.workspace)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"connection": connections.status_for(row, self.workspace)}

    # ── connection.promote ──────────────────────────────────────────────────
    def promote(self, params: dict) -> dict:
        """{connection, previous, kept, stopped_pid, launch, changed, relaunch_needed}: the primary moved; the
        old one keeps running unless `park: true`."""
        instance = self._instance(params)
        park = params.get("park", False)
        if not isinstance(park, bool):
            raise ProtocolError(-32602, "park must be a boolean")
        try:
            row = connections.promote(instance, repo=self.repo, logs_dir=self.logs_dir, workspace=self.workspace,
                                      vault=self.vault, spawn=self.spawn, kill=self.kill, park=park)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"connection": connections.status_for(row, self.workspace), "previous": row["previous"],
                "kept": row["kept"], "stopped_pid": row["stopped_pid"], "launch": row["launch"],
                "changed": row["changed"], "relaunch_needed": row["relaunch_needed"]}

    # ── connection.park / connection.park_legacy ────────────────────────────
    def park(self, params: dict) -> dict:
        """{connection, stopped_pid}: a secondary's bridge stopped and its row parked."""
        instance = self._instance(params)
        try:
            row = connections.park(instance, workspace=self.workspace, kill=self.kill)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"connection": connections.status_for(row, self.workspace), "stopped_pid": row["stopped_pid"]}

    def park_legacy(self, params: dict) -> dict:
        """{primary, parked, stopped}: every connection but the primary parked (the cutover)."""
        try:
            return connections.park_legacy(workspace=self.workspace, kill=self.kill)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))

    # ── connection.unpark ───────────────────────────────────────────────────
    def unpark(self, params: dict) -> dict:
        """{connection, launch}: the row no longer parked, its bridge started (launch null for the legacy row)."""
        instance = self._instance(params)
        try:
            row = connections.unpark(instance, repo=self.repo, logs_dir=self.logs_dir, workspace=self.workspace,
                                     vault=self.vault, spawn=self.spawn)
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"connection": connections.status_for(row, self.workspace), "launch": row["launch"]}

    # ── connection.disconnect ───────────────────────────────────────────────
    def disconnect(self, params: dict) -> dict:
        instance = self._instance(params)
        try:
            row = connections.disconnect(instance, workspace=self.workspace, kill=self.kill)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
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
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"launch": record}

    # ── connection.update_token ─────────────────────────────────────────────
    def update_token(self, params: dict) -> dict:
        """{instance, token}: the row on the new code, relaunched; every refusal by code is CONFLICT."""
        instance = self._instance(params)
        token = params.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ProtocolError(-32602, "missing required param: token")
        try:
            row = connections.update_token(instance, token, repo=self.repo, logs_dir=self.logs_dir,
                                           workspace=self.workspace, vault=self.vault, spawn=self.spawn,
                                           kill=self.kill, fetch=self.fetch, getaddrinfo=self.getaddrinfo)
        except connections.Refusal as e:
            raise ProtocolError(CONFLICT, str(e))
        except connections.ConnectionsError as e:
            raise ProtocolError(-32602, str(e))
        return {"connection": connections.status_for(row, self.workspace)}
