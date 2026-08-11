"""sutando-runtime-server — local runtime-API daemon (v0).

JSON-RPC over a private Unix socket (see protocol.py) bridging a long-running
agent to human collaboration: approval.request / elicitation.request /
capability.execute, plus request.get/wait/cancel. Requests are durable
(request_store.py, SQLite) and the approve/answer transport is the existing
human-action card lifecycle (ha_adapter.py) — no new server API or UI in v0.

Identity: the actor is resolved DAEMON-SIDE from the environment
(SUTANDO_AGENT_ID > AGENT_MXID > AGENT_ID), never from CLI-supplied params —
a client cannot self-report who it is.

Security: socket dir 0700, socket 0600, stale-socket takeover only after a
connect probe fails, 256 KB frame cap, per-request timeouts. The socket is
same-user local RPC; any remote capability service must re-authorize fully.

Run:  python3 src/runtime-api/server.py
Env:  SUTANDO_RUNTIME_SOCKET  socket path (default <run dir>/sutando-runtime.sock)
      SUTANDO_RUNTIME_DB      sqlite path (default <state>/runtime-state.sqlite)
      SUTANDO_HA_DIR          human-actions dir (default <state>/human-actions)
      SUTANDO_RUN_DIR         run dir (platform default via rundir.py)

Supervision: launched by the supervisor layer (launch path or a tmux window),
deliberately NOT by start-cli.sh — the core-launch chokepoint stays free of
approval business logic.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import stat
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from protocol import (MAX_LINE_BYTES, ELICITATION_TYPES, ProtocolError,  # noqa: E402
                      error_frame, parse_line, result_frame)
from request_store import RequestStore, TERMINAL  # noqa: E402
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from rundir import socket_path  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402

def _state_dir() -> Path:
    ws = os.environ.get("SUTANDO_RUNTIME_STATE")
    if ws:
        return Path(ws)
    # Canonical workspace resolution (repo rule: use the helper, never a
    # guessed relative fallback) — workspace_default lives in src/, one level up.
    sys.path.insert(0, str(_HERE.parent))
    from workspace_default import resolve_workspace  # noqa: PLC0415
    return Path(resolve_workspace()) / "state"


def _log(msg: str) -> None:
    print(f"[runtime-api] {msg}", flush=True)


class RuntimeServer:
    def __init__(self, socket_path: str, db_path: str, ha_dir: str):
        self.socket_path = socket_path
        self.store = RequestStore(db_path)
        self.ha = HumanActionAdapter(ha_dir)
        # Actor identity is resolved DAEMON-SIDE, here, and handed to the
        # dispatcher explicitly — a client parameter can never override it.
        self.actor_id = (os.environ.get("SUTANDO_AGENT_ID")
                         or os.environ.get("AGENT_MXID")
                         or os.environ.get("AGENT_ID")
                         or "local-agent")
        # Request-domain orchestration (dispatch, approvals, governed
        # capabilities, idempotency, durable transitions, recovery) lives in
        # dispatcher.py. This class owns socket transport only.
        self.dispatcher = RuntimeDispatcher(self.store, self.ha, self.actor_id)

    # ── transport ──────────────────────────────────────────────────────────
    async def client(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    raw = await reader.readline()
                except (ValueError, ConnectionResetError):
                    break  # oversized or dropped — close the connection
                if not raw:
                    break
                try:
                    req_id, method, params = parse_line(raw)
                except ProtocolError as e:
                    writer.write(error_frame(e.req_id, e.code, e.message))
                    await writer.drain()
                    continue
                try:
                    result = await self.dispatcher.handle(method, params)
                    writer.write(result_frame(req_id, result))
                except ProtocolError as e:
                    writer.write(error_frame(req_id, e.code, e.message))
                except Exception as e:  # noqa: BLE001 — one bad request ≠ dead daemon
                    _log(f"handler error: {e}")
                    writer.write(error_frame(req_id, -32000, f"server error: {e}"))
                await writer.drain()
        finally:
            writer.close()

    async def serve(self) -> None:
        sp = Path(self.socket_path)
        sp.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(sp.parent, 0o700)
        if sp.exists():
            # Live daemon already there? Probe before stealing.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1.0)
                probe.connect(str(sp))
                probe.close()
                raise SystemExit(f"another runtime daemon is live on {sp} — not overwriting")
            except (ConnectionRefusedError, socket.timeout, FileNotFoundError, OSError):
                sp.unlink(missing_ok=True)  # stale socket
        server = await asyncio.start_unix_server(
            self.client, path=str(sp), limit=MAX_LINE_BYTES + 1024)
        os.chmod(sp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self.dispatcher.recover()
        _log(f"listening on {sp} (actor={self.actor_id})")
        async with server:
            await asyncio.gather(server.serve_forever(), self.dispatcher.resolver_loop())


def main() -> None:
    state = _state_dir()
    srv = RuntimeServer(
        # Canonical shared resolution (rundir.py) — daemon and CLI must agree
        # on the same default socket, on every platform (review blocker).
        socket_path=socket_path(),
        db_path=os.environ.get("SUTANDO_RUNTIME_DB")
        or str(state / "runtime-state.sqlite"),
        ha_dir=os.environ.get("SUTANDO_HA_DIR")
        or str(state / "human-actions"),
    )
    try:
        asyncio.run(srv.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
