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
      SUTANDO_RUN_DIR         run dir (default ~/Library/Application Support/
                              space.ag2.app/run)

Supervision: launched by the supervisor layer (launch path or a tmux window),
deliberately NOT by start-cli.sh — the core-launch chokepoint stays free of
approval business logic.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from protocol import (MAX_LINE_BYTES, ELICITATION_TYPES, ProtocolError,  # noqa: E402
                      error_frame, parse_line, result_frame)
from request_store import RequestStore, TERMINAL  # noqa: E402
from ha_adapter import HumanActionAdapter  # noqa: E402

RESOLVER_POLL_S = float(os.environ.get("SUTANDO_RUNTIME_RESOLVE_POLL", "2"))
WAIT_POLL_S = 0.5
DEFAULT_WAIT_TIMEOUT_S = 30.0


def _run_dir() -> Path:
    return Path(os.environ.get("SUTANDO_RUN_DIR")
                or Path.home() / "Library" / "Application Support"
                / "space.ag2.app" / "run")


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
        self.actor_id = (os.environ.get("SUTANDO_AGENT_ID")
                         or os.environ.get("AGENT_MXID")
                         or os.environ.get("AGENT_ID")
                         or "local-agent")
        # request_id → ha action_id, rebuilt at boot for crash recovery.
        self._ha_of: dict = {}

    # ── boot ────────────────────────────────────────────────────────────────
    def recover(self) -> None:
        """Re-link pending requests to their ha actions after a restart."""
        n = 0
        for rec in self.store.pending():
            if rec["requestType"] in ("approval", "elicitation"):
                self._ha_of[rec["requestId"]] = (
                    "ha_" + rec["requestId"].replace("-", "")[:24])
                n += 1
        if n:
            _log(f"recovered {n} pending request(s)")

    # ── dispatch ───────────────────────────────────────────────────────────
    async def handle(self, method: str, params: dict) -> dict:
        if method == "approval.request":
            return self._issue("approval", method, params,
                               required=("action",))
        if method == "elicitation.request":
            etype = params.get("type", "free_text")
            if etype not in ELICITATION_TYPES:
                raise ProtocolError(-32602, f"type must be one of {ELICITATION_TYPES}")
            return self._issue("elicitation", method, params,
                               required=("question",))
        if method == "capability.execute":
            return self._capability(params)
        if method == "request.get":
            return self._get(params)
        if method == "request.wait":
            return await self._wait(params)
        if method == "request.cancel":
            return self._cancel(params)
        raise ProtocolError(-32601, f"unknown method {method}")

    def _issue(self, rtype: str, method: str, params: dict, required=()) -> dict:
        for k in required:
            if not params.get(k):
                raise ProtocolError(-32602, f"missing required param: {k}")
        rec = self.store.create(rtype, method, self.actor_id, params,
                                task_id=params.get("taskId"),
                                expires_in_s=params.get("expiresInS"))
        opener = (self.ha.open_approval if rtype == "approval"
                  else self.ha.open_elicitation)
        self._ha_of[rec["requestId"]] = opener(rec)
        return {"requestId": rec["requestId"], "status": "pending"}

    def _capability(self, params: dict) -> dict:
        action = params.get("action")
        if not action:
            raise ProtocolError(-32602, "missing required param: action")
        rec = self.store.create("capability", "capability.execute",
                                self.actor_id, params,
                                task_id=params.get("taskId"))
        # v0: fake executor — records and completes immediately so the request
        # lifecycle is E2E-testable. Slice ② replaces this with the governed
        # gateway path (message.send verified by event_id, per #2324/#207).
        result = {"executed": False, "echo": {"action": action,
                                              "input": params.get("input")},
                  "note": "capability execution lands in slice ②"}
        self.store.transition(rec["requestId"], "completed", result=result,
                              resolved_by="fake-executor")
        return {"requestId": rec["requestId"], "status": "completed",
                "result": result}

    def _get(self, params: dict) -> dict:
        rec = self._require(params)
        return self._public(rec)

    async def _wait(self, params: dict) -> dict:
        rec = self._require(params)
        timeout = float(params.get("timeoutS") or DEFAULT_WAIT_TIMEOUT_S)
        deadline = time.monotonic() + timeout
        while True:
            self._settle(rec["requestId"])
            rec = self.store.get(rec["requestId"])
            if rec["status"] in TERMINAL:
                return self._public(rec)
            if time.monotonic() >= deadline:
                return {**self._public(rec), "timedOut": True}
            await asyncio.sleep(WAIT_POLL_S)

    def _cancel(self, params: dict) -> dict:
        rec = self._require(params)
        self.store.transition(rec["requestId"], "cancelled",
                              resolved_by=self.actor_id)
        return self._public(self.store.get(rec["requestId"]))

    def _require(self, params: dict) -> dict:
        rid = params.get("requestId")
        rec = self.store.get(rid) if rid else None
        if rec is None:
            raise ProtocolError(-32602, f"unknown requestId: {rid!r}")
        return rec

    @staticmethod
    def _public(rec: dict) -> dict:
        out = {"requestId": rec["requestId"], "status": rec["status"]}
        if rec.get("result") is not None:
            out["result"] = rec["result"]
        if rec.get("resolvedBy"):
            out["resolvedBy"] = rec["resolvedBy"]
        return out

    # ── resolution mapping (ha decision → runtime terminal state) ──────────
    def _settle(self, request_id: str) -> None:
        rec = self.store.get(request_id)
        if not rec or rec["status"] != "pending":
            return
        aid = self._ha_of.get(request_id)
        if not aid:
            return
        res = self.ha.poll_resolution(aid)
        if res is None:
            return
        status, answers, resolved_by = res
        if status == "expired":
            self.store.transition(request_id, "expired", resolved_by=resolved_by)
            return
        if rec["requestType"] == "approval":
            chosen = self.ha.first_answer(answers or {},
                                          [{"label": "Approve"}, {"label": "Deny"}])
            labels = chosen if isinstance(chosen, list) else [chosen]
            approved = any(str(c).strip().lower() == "approve" for c in labels if c)
            self.store.transition(request_id,
                                  "approved" if approved else "denied",
                                  resolved_by=resolved_by)
        else:
            p = rec["params"]
            options = [{"label": str(o)} for o in (p.get("options") or [])]
            if p.get("type") == "confirmation" and not options:
                options = [{"label": "Yes"}, {"label": "No"}]
            answer = self.ha.first_answer(answers or {}, options)
            if isinstance(answer, list) and p.get("type") == "single_select":
                answer = answer[0] if answer else None
            self.store.transition(request_id, "resolved",
                                  result={"answer": answer},
                                  resolved_by=resolved_by)

    async def resolver_loop(self) -> None:
        while True:
            try:
                for rec in self.store.pending():
                    self._settle(rec["requestId"])
            except Exception as e:  # noqa: BLE001 — resolver must never die
                _log(f"resolver error (isolated): {e}")
            await asyncio.sleep(RESOLVER_POLL_S)

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
                    result = await self.handle(method, params)
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
        self.recover()
        _log(f"listening on {sp} (actor={self.actor_id})")
        async with server:
            await asyncio.gather(server.serve_forever(), self.resolver_loop())


def main() -> None:
    run_dir = _run_dir()
    state = _state_dir()
    srv = RuntimeServer(
        socket_path=os.environ.get("SUTANDO_RUNTIME_SOCKET")
        or str(run_dir / "sutando-runtime.sock"),
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
