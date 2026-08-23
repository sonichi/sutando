"""sutando-runtime-server — local runtime-API daemon (v0).

JSON-RPC over a private Unix socket (see protocol.py) bridging a long-running
agent to human collaboration: approval.request / elicitation.request /
capability.execute, bounded ephemeral capability.list/read, plus
request.get/wait/cancel. Approval, elicitation and execution requests are
durable (request_store.py, SQLite); list/read are not. The approve/answer
transport is the existing human-action card lifecycle (ha_adapter.py) — no new
server API or UI in v0.

Identity: the actor is resolved DAEMON-SIDE from the environment
(SUTANDO_AGENT_ID > AGENT_MXID > AGENT_ID), never from CLI-supplied params —
a client cannot self-report who it is.

Security: socket dir 0700, socket 0600, stale-socket takeover only after a
connect probe fails, 256 KB frame cap, per-request timeouts. The socket is
same-user local RPC; any remote capability service must re-authorize fully.

Run:  python3 src/runtime-api/server.py
Env:  SUTANDO_RUNTIME_SOCKET  socket path (default resolved by rundir.py:
                              <run dir>/<(agent, instance) key>/runtime.sock)
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
import shutil
import socket
import subprocess
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))  # src/ — shared policy modules

from protocol import (MAX_LINE_BYTES, ELICITATION_TYPES, ProtocolError,  # noqa: E402
                      error_frame, notification_frame, parse_line, result_frame)
from request_store import RequestStore, TERMINAL  # noqa: E402
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from rundir import (agent_id as _resolve_agent_id, instance_id,  # noqa: E402
                    lock_path, runtime_state_dir, socket_path)

from dispatcher import RuntimeDispatcher  # noqa: E402
from agents_view import AgentsView  # noqa: E402

from delivery.readiness import read_ready_result  # noqa: E402
from identity_view import IdentityView  # noqa: E402

from tasks_view import TasksView  # noqa: E402
from runtime_view import RuntimeView  # noqa: E402

from schedules_view import SchedulesView  # noqa: E402

import instance_registry  # noqa: E402
from capability_registry import (EphemeralCapabilityRegistry,  # noqa: E402
                                 compose_capability_registry)


def _state_dir() -> Path:
    # Same resolution the CLI's actor chain uses (rundir.py) — a second copy
    # here would let daemon and client read different enrolled identities.
    return runtime_state_dir()


def _log(msg: str) -> None:
    print(f"[runtime-api] {msg}", flush=True)


def _channels_dir() -> str | None:
    """Channel access configs via the canonical resolver (util_paths); None
    if unresolvable — the identity surface then simply omits channel data."""
    try:
        sys.path.insert(0, str(_HERE.parent))
        from util_paths import claude_home_path  # noqa: PLC0415
        return str(claude_home_path("channels"))
    except Exception:  # noqa: BLE001
        return None


def _host_label() -> str | None:
    """This host's per-host label (matches the cores heartbeat basename).
    SUTANDO_HOST_LABEL wins, else the canonical resolver script; best-effort."""
    env = os.environ.get("SUTANDO_HOST_LABEL")
    if env:
        return env
    try:
        import subprocess  # noqa: PLC0415
        out = subprocess.run(
            ["bash", str(_HERE.parent.parent / "scripts" / "sutando-config.sh"),
             "host-label"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def resolve_actor_id(state_dir) -> str:
    """The daemon's own actor identity — delegated to the shared chain in
    rundir.py so the CLI and the shell descriptor resolve the SAME actor, and
    therefore the same socket (review P1 regression)."""
    return _resolve_agent_id(state_dir or None)


class RuntimeServer:
    def __init__(self, socket_path: str, db_path: str, ha_dir: str,
                 state_dir: str | None = None,
                 capability_registry: Optional[EphemeralCapabilityRegistry] = None):
        self.socket_path = socket_path
        self.store = RequestStore(db_path)
        self.ha = HumanActionAdapter(ha_dir)
        # Push mode: writers that called task.subscribe get a live `task.result`
        # notification per new result. Best-effort tail — not a durable outbox.
        self._subscribers: set[asyncio.StreamWriter] = set()
        # --activity subscribers get `activity` frames (core-status step feed).
        self._activity_subscribers: set[asyncio.StreamWriter] = set()
        # `requests` subscribers get `request.pending` frames when a HITL
        # request needs a human (the wearable's buzz-and-card trigger).
        self._request_subscribers: set[asyncio.StreamWriter] = set()
        self._state_dir = state_dir
        # Actor identity is resolved DAEMON-SIDE, here, and handed to the
        # dispatcher explicitly — a client parameter can never override it.
        self.actor_id = resolve_actor_id(state_dir)
        # Request-domain orchestration (approvals, capabilities, idempotency,
        # durable transitions) lives in dispatcher.py; this class = transport.
        host_label = _host_label() if state_dir else None
        self.dispatcher = RuntimeDispatcher(
            self.store, self.ha, self.actor_id,
            agents_view=AgentsView(state_dir) if state_dir else None,
            identity_view=(IdentityView(state_dir, self.actor_id,
                                        channels_dir=_channels_dir(),
                                        host_label=host_label,
                                        instance=instance_id())
                           if state_dir else None),
            tasks_view=(TasksView(Path(state_dir).parent / "tasks",
                                  Path(state_dir).parent / "results",
                                  self.actor_id,
                                  hitl_lookup=self._pending_hitl_types,
                                  instance=instance_id())
                        if state_dir else None),
            runtime_view=(RuntimeView(state_dir, host_label=host_label,
                                      runtime_socket=socket_path)
                          if state_dir else None),
            # Same canonical crons.json the dashboard reads: workspace +
            # host label — never the bare hostname.
            schedules_view=(SchedulesView(Path(state_dir).parent / "hosts"
                                          / host_label / "crons.json")
                            if state_dir and host_label else None),
            capability_registry=capability_registry)

    def _register_instance(self) -> None:
        """Boot-time manifest write (registry M1). Best-effort: a registry
        problem must never stop the daemon from serving."""
        try:
            ws = None
            try:
                sys.path.insert(0, str(_HERE.parent))
                from workspace_default import resolve_workspace  # noqa: PLC0415
                ws = str(resolve_workspace())
            except Exception:  # noqa: BLE001
                pass
            # Launcher starts the WHOLE instance; SUTANDO_LAUNCHER_EXECUTABLE
            # /_ARGS override (tests inject one). Structured argv, no shell.
            repo = _HERE.parent.parent
            # default must RESTORE THIS DAEMON: startup.sh never reaches
            # server.py, so a manifest recording it cannot bring us back
            launcher_exe = (os.environ.get("SUTANDO_LAUNCHER_EXECUTABLE")
                            or str(repo / "bin" / "sutando"))
            try:
                launcher_args = json.loads(
                    os.environ.get("SUTANDO_LAUNCHER_ARGS") or '["serve"]')
            except ValueError:
                launcher_args = ["serve"]
            launcher = {"type": "process", "executable": launcher_exe,
                        "args": launcher_args,
                        "working_directory": str(repo)}
            # tmux backend coordinates (v1 attach target): the socket the core
            # launched on (same value the heartbeat records) + its session.
            tmux_socket = (os.environ.get("SUTANDO_TMUX_SOCKET")
                           or "/tmp/sutando-tmux.sock")
            session = os.environ.get("SUTANDO_TMUX_SESSION") or "sutando-core"
            instance_registry.write_manifest(
                self.actor_id, workspace=ws, endpoint=self.socket_path,
                backend="tmux", host_label=_host_label(), launcher=launcher,
                instance=instance_id(), tmux_socket=tmux_socket, session=session,
                config_dir=os.environ.get("CLAUDE_CONFIG_DIR"),
                status="running")
        except Exception as e:  # noqa: BLE001
            _log(f"instance-registry write failed (non-fatal): {e}")

    def mark_stopped(self) -> None:
        try:
            instance_registry.mark_stopped(self.actor_id, instance_id())
        except Exception:  # noqa: BLE001
            pass

    def _pending_hitl_types(self, task_id: str) -> list:
        """Pending HITL request types for a task — the tasks view's window
        into the request store, bound here so the view stays store-free."""
        return [r["requestType"] for r in self.store.pending()
                if r.get("taskId") == task_id]

    # ── LAN WSS transport ships with the SCP device-plane PR ─────────────
    async def _maybe_start_wss(self):
        return None
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
                if method == "task.subscribe":
                    # Mode-switch to push channel; server owns writers, the
                    # dispatcher stays pure request/response. Streams opt-in.
                    streams = []
                    if params.get("results", True):
                        self._subscribers.add(writer)
                        streams.append("results")
                    if params.get("activity"):
                        self._activity_subscribers.add(writer)
                        streams.append("activity")
                    if params.get("requests"):
                        self._request_subscribers.add(writer)
                        streams.append("requests")
                    writer.write(result_frame(req_id, {"subscribed": True,
                                                       "streams": streams}))
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
            self._subscribers.discard(writer)
            self._activity_subscribers.discard(writer)
            self._request_subscribers.discard(writer)
            writer.close()

    async def _results_watcher(self) -> None:
        """Tail results/ and push a `task.result` notification to every
        subscriber as new results land. Seeds `seen` from what already exists
        so subscribers get NEW results, never a boot-time backlog blast."""
        tasks = self.dispatcher.tasks
        if tasks is None:
            return
        # Short poll of a small dir: sub-perceptible, dependency-free,
        # portable — a real fs-event watcher would be platform-specific.
        try:
            interval = float(os.environ.get("SUTANDO_RESULT_POLL_S") or 0.2)
        except ValueError:
            interval = 0.2
        seen = {f.name for f in tasks._result_files()}
        while True:
            await asyncio.sleep(interval)
            await self._emit_new_results(tasks, seen)

    async def _push_activity(self, params: dict) -> None:
        frame = notification_frame("activity", params)
        for w in list(self._activity_subscribers):
            try:
                w.write(frame)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError, RuntimeError):
                self._activity_subscribers.discard(w)

    @staticmethod
    def _request_summary(rec: dict) -> dict:
        """The client-facing shape of a pending HITL request — enough for a
        wearable to render a card (what + why + deadline), no store internals."""
        p = rec.get("params") or {}
        return {"requestId": rec.get("requestId"),
                "requestType": rec.get("requestType"),
                "taskId": rec.get("taskId"),
                "action": p.get("action"), "question": p.get("question"),
                "reason": p.get("reason"), "instructions": p.get("instructions"),
                "createdAt": rec.get("createdAt"),
                "expiresAt": rec.get("expiresAt")}

    async def _push_request(self, params: dict) -> None:
        frame = notification_frame("request.pending", params)
        for w in list(self._request_subscribers):
            try:
                w.write(frame)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError, RuntimeError):
                self._request_subscribers.discard(w)

    async def _requests_watcher(self) -> None:
        """Push a `request.pending` notification when a NEW HITL request needs a
        human — the wearable's interrupt/buzz channel. Seeds `seen` from the
        current pending set so a subscriber gets NEW requests, not a boot
        backlog. Resolved requests simply stop being pending; no resolve push in
        v0 (the responder path is a later slice)."""
        try:
            interval = float(os.environ.get("SUTANDO_REQUEST_POLL_S") or 0.3)
        except ValueError:
            interval = 0.3
        try:
            seen = {r["requestId"] for r in self.store.pending()}
        except Exception:  # noqa: BLE001
            seen = set()
        while True:
            await asyncio.sleep(interval)
            if not self._request_subscribers:
                try:                              # keep `seen` current while idle
                    seen = {r["requestId"] for r in self.store.pending()}
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                pend = self.store.pending()
            except Exception:  # noqa: BLE001
                continue
            live = {r["requestId"] for r in pend}
            for rec in pend:
                if rec["requestId"] not in seen:
                    await self._push_request(self._request_summary(rec))
            seen = live

    async def _activity_watcher(self) -> None:
        """Push `activity` frames to activity subscribers from two sources:
        core-status.json step changes (kind='step' — the coarse 'what I'm doing
        now' feed) and activity-feed.jsonl lines (kind='tool' — the per-tool
        feed written by the PostToolUse hook). Curated; the raw tmux firehose
        stays opt-in + client-side. The client filters by kind for its
        quiet/activity/verbose level."""
        if not self._state_dir:
            return
        status_path = Path(self._state_dir) / "core-status.json"
        feed_path = Path(self._state_dir) / "activity-feed.jsonl"
        last_step = None
        try:
            feed_pos = feed_path.stat().st_size  # seed to EOF: no backlog blast
        except OSError:
            feed_pos = 0
        while True:
            await asyncio.sleep(0.3)
            if not self._activity_subscribers:
                try:                              # keep the tail current while idle
                    feed_pos = feed_path.stat().st_size
                except OSError:
                    pass
                continue
            try:
                data = json.loads(status_path.read_text())
                step = data.get("step")
                if step and step != last_step:
                    last_step = step
                    await self._push_activity({"kind": "step", "step": step,
                                               "status": data.get("status"),
                                               "ts": data.get("ts")})
            except (OSError, ValueError):
                pass
            try:
                size = feed_path.stat().st_size
                if size < feed_pos:               # rotated/truncated
                    feed_pos = 0
                if size > feed_pos:
                    with feed_path.open() as fh:
                        fh.seek(feed_pos)
                        chunk = fh.read()
                        feed_pos = fh.tell()
                    for line in chunk.splitlines():
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        await self._push_activity({
                            "kind": rec.get("kind", "tool"),
                            "step": rec.get("step"), "ts": rec.get("ts"),
                            **({"detail": rec["detail"]} if rec.get("detail") else {})})
            except OSError:
                pass

    async def _emit_new_results(self, tasks, seen: set) -> None:
        """One watcher pass: push a `task.result` notification for each result
        not in `seen`, oldest-first so the stream stays in order. Mutates
        `seen`. Extracted from the loop so it's unit-testable."""
        try:
            files = tasks._result_files()  # newest first
        except OSError:
            return
        for f in reversed([f for f in files if f.name not in seen]):
            # readiness: unreadable/mid-write/empty = not-yet — the name only
            # enters `seen` after a ready read, so a transient race retries
            body = read_ready_result(f)
            if body is None:
                continue
            seen.add(f.name)
            try:
                ts = int(f.stat().st_mtime)
            except OSError:
                ts = int(time.time())  # archived between read and stat
            frame = notification_frame("task.result", {
                "taskId": f.name.removesuffix(".txt"),
                "result": body, "ts": ts})
            for w in list(self._subscribers):
                try:
                    w.write(frame)
                    await w.drain()
                except (ConnectionResetError, BrokenPipeError, RuntimeError):
                    self._subscribers.discard(w)

    async def serve(self) -> None:
        # Same-instance double start is illegal; flock held for the daemon's
        # life (per-open-file-description — keep the fd referenced on self).
        import fcntl
        # Identity-scoped: two actors sharing an instance_id are two instances,
        # not a double start of one.
        lp = lock_path(instance_id(), agent=self.actor_id)
        lp.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(lp.parent, 0o700)
        self._lock_fd = open(lp, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"instance {self.actor_id!r}/{instance_id()!r} already has an "
                f"authoritative server (lock held: {lp}) — refusing double start")
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()
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
        self._register_instance()
        _log(f"listening on {sp} (actor={self.actor_id})")
        wss = await self._maybe_start_wss()
        try:
            async with server:
                await asyncio.gather(server.serve_forever(),
                                     self.dispatcher.resolver_loop(),
                                     self._results_watcher(),
                                     self._activity_watcher(),
                                     self._requests_watcher())
        finally:
            for t in (wss or []):
                await t.cleanup()
            adv = getattr(self, "_advertiser", None)
            if adv is not None:
                adv.terminate()


def build_runtime_server(provider_factories=(), *, state_dir=None,
                         runtime_socket=None) -> RuntimeServer:
    state = Path(state_dir) if state_dir is not None else _state_dir()
    registry = compose_capability_registry(provider_factories)
    return RuntimeServer(
        # Canonical shared resolution (rundir.py) — daemon and CLI must agree
        # on the same default socket, on every platform (review blocker).
        socket_path=runtime_socket or socket_path(
            agent=resolve_actor_id(state)),
        db_path=os.environ.get("SUTANDO_RUNTIME_DB")
        or str(state / "runtime-state.sqlite"),
        ha_dir=os.environ.get("SUTANDO_HA_DIR")
        or str(state / "human-actions"),
        state_dir=str(state),
        capability_registry=registry,
    )


def main(provider_factories=()) -> None:
    srv = build_runtime_server(provider_factories)
    import signal

    def _term(_sig, _frm):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        asyncio.run(srv.serve())
    except KeyboardInterrupt:
        pass
    finally:
        # Clean shutdown only — a crash never reaches this, leaving
        # status "running" + dead socket = the stale_or_crashed signal.
        srv.mark_stopped()


if __name__ == "__main__":
    main()
