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

from protocol import (MAX_LINE_BYTES, ELICITATION_TYPES, ProtocolError,  # noqa: E402
                      error_frame, notification_frame, parse_line, result_frame)
from request_store import RequestStore, TERMINAL  # noqa: E402
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from rundir import socket_path, instance_id, lock_path  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402
from agents_view import AgentsView  # noqa: E402
from identity_view import IdentityView  # noqa: E402
from tasks_view import TasksView  # noqa: E402
from runtime_view import RuntimeView  # noqa: E402
from schedules_view import SchedulesView  # noqa: E402
import instance_registry  # noqa: E402
from capability_registry import (EphemeralCapabilityRegistry,  # noqa: E402
                                 compose_capability_registry)


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



def _enrolled_agent_id(state_dir) -> "str | None":
    if not state_dir:
        return None
    try:
        rec = json.loads((Path(state_dir) / "auth" / "ag2space.json").read_text())
        v = (rec.get("agent_id") or "").strip()
        return v or None
    except (OSError, ValueError):
        return None

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
        # request (approval / elicitation / human_action) needs a human — the
        # wearable's buzz-and-card trigger.
        self._request_subscribers: set[asyncio.StreamWriter] = set()
        self._state_dir = state_dir
        # Actor identity is resolved DAEMON-SIDE, here, and handed to the
        # dispatcher explicitly — a client parameter can never override it.
        # Env first, then the enrolled identity (same chain as the WSS leg),
        # so info/agent-list rows join on the real agent id, not a fallback.
        self.actor_id = (os.environ.get("SUTANDO_AGENT_ID")
                         or os.environ.get("AGENT_MXID")
                         or os.environ.get("AGENT_ID")
                         or _enrolled_agent_id(state_dir)
                         or "local-agent")
        # Request-domain orchestration (dispatch, approvals, governed
        # capabilities, idempotency, durable transitions, recovery) lives in
        # dispatcher.py. This class owns socket transport only.
        host_label = _host_label() if state_dir else None
        self.dispatcher = RuntimeDispatcher(
            self.store, self.ha, self.actor_id,
            agents_view=AgentsView(state_dir) if state_dir else None,
            identity_view=(IdentityView(state_dir, self.actor_id,
                                        channels_dir=_channels_dir(),
                                        host_label=host_label)
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
            # Same canonical crons.json the dashboard reads: workspace
            # (state_dir's parent, the TasksView convention) + host label —
            # never the bare hostname (#1745).
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
            # The launcher starts the WHOLE instance (Core + daemon). Default
            # is the repo's startup.sh; SUTANDO_LAUNCHER_EXECUTABLE/_ARGS
            # override it (tests inject a controlled launcher so they never run
            # the real startup.sh). Structured executable+args — no shell string.
            repo = _HERE.parent.parent
            launcher_exe = (os.environ.get("SUTANDO_LAUNCHER_EXECUTABLE")
                            or str(repo / "src" / "startup.sh"))
            try:
                launcher_args = json.loads(
                    os.environ.get("SUTANDO_LAUNCHER_ARGS") or "[]")
            except ValueError:
                launcher_args = []
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
            instance_registry.mark_stopped(self.actor_id)
        except Exception:  # noqa: BLE001
            pass

    def _pending_hitl_types(self, task_id: str) -> list:
        """Pending HITL request types for a task — the tasks view's window
        into the request store, bound here so the view stays store-free."""
        return [r["requestType"] for r in self.store.pending()
                if r.get("taskId") == task_id]

    # ── LAN WebSocket transport (SCP over the network — opt-in; cleartext ws) ─
    def _wss_token(self) -> str:
        """Resolve the bearer token: env wins, else a durable per-host token
        under state/auth/ (survives transient-state cleanup, like the other
        auth material there); generate + persist 0600 on first use."""
        env = os.environ.get("SUTANDO_SCP_WSS_TOKEN")
        if env:
            return env
        if not self._state_dir:
            import secrets  # noqa: PLC0415
            return secrets.token_urlsafe(32)  # ephemeral — no place to persist
        auth_dir = Path(self._state_dir) / "auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(auth_dir, 0o700)
        tok_path = auth_dir / "scp-wss.token"
        try:
            tok = tok_path.read_text().strip()
            if tok:
                return tok
        except OSError:
            pass
        import secrets  # noqa: PLC0415
        tok = secrets.token_urlsafe(32)
        tok_path.write_text(tok)
        os.chmod(tok_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        _log(f"generated SCP shared bearer token (legacy scp-wss.token name) at {tok_path}")
        return tok

    def _wss_ssl_context(self):
        """TLS for the WSS listener (SUTANDO_SCP_WSS_TLS truthy). Browsers gate
        the microphone behind a secure context, so phone-voice on the LAN needs
        https/wss — a self-signed cert is generated once (openssl, universally
        present on macOS) under state/auth/scp-tls/ and reused. The phone
        accepts the self-signed warning once. Returns an SSLContext or None."""
        if (os.environ.get("SUTANDO_SCP_WSS_TLS") or "").lower() not in (
                "1", "true", "yes", "on"):
            return None
        import ssl
        import subprocess
        tls_dir = Path(self._state_dir) / "auth" / "scp-tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(tls_dir, 0o700)
        cert, key = tls_dir / "cert.pem", tls_dir / "key.pem"
        san = self._tls_san_list()
        if not (cert.exists() and key.exists()) \
                or not self._cert_covers(cert, san):
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "825",
                 "-subj", "/CN=sutando-server",
                 "-addext", "subjectAltName=" + ",".join(san)],
                check=True, capture_output=True)
            os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
            _log(f"generated self-signed TLS cert at {tls_dir} "
                 f"(SAN: {', '.join(san)})")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        return ctx

    @staticmethod
    def _tls_san_list() -> "list[str]":
        """SAN entries for the self-signed cert. Browsers reject SAN-less
        certs outright (CN is ignored since ~2017), so the cert must name
        every way a phone reaches this host: the mDNS .local name (stable
        across DHCP), localhost, and current non-loopback IPv4s."""
        import socket
        names = {"DNS:localhost", "IP:127.0.0.1"}
        host = socket.gethostname().split(".")[0]
        if host:
            names.add(f"DNS:{host}.local")
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                addr = info[4][0]
                if not addr.startswith("127."):
                    names.add(f"IP:{addr}")
        except OSError:
            pass
        return sorted(names)

    @staticmethod
    def _cert_covers(cert: Path, san: "list[str]") -> bool:
        """True iff every wanted SAN entry already appears in the cert —
        a DHCP-moved LAN IP or renamed host triggers regeneration."""
        import subprocess
        try:
            out = subprocess.run(
                ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
                check=True, capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return False
        return all(e.split(":", 1)[1] in out for e in san)

    def _start_advertiser(self, agent: "str | None", port: int):
        """mDNS-announce the SCP listener (_sutando-scp._tcp) as a CHILD of this
        process, so the advertisement lives and dies with the listener it names.
        A standalone advertiser survives its server and keeps promising a dead
        port; a supervised server that respawns re-advertises automatically.
        macOS-only (dns-sd); other platforms skip silently. Best-effort."""
        if sys.platform != "darwin" or shutil.which("dns-sd") is None:
            return None
        # Instance name = the agent LOCALPART ("sutando-qingyun-001"), matching
        # what device firmware pins via the agent= TXT field. Full mxids don't
        # travel — the @/: characters are also hostile to DNS-SD names.
        name = (agent or "sutando").split(":")[0].lstrip("@") or "sutando"
        try:
            proc = subprocess.Popen(
                ["dns-sd", "-R", name, "_sutando-scp._tcp.", "local",
                 str(port), f"agent={name}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log(f"mDNS advertise: {name} on _sutando-scp._tcp :{port} "
                 f"(pid {proc.pid})")
            return proc
        except OSError as e:
            _log(f"mDNS advertise failed (non-fatal): {e}")
            return None

    async def _maybe_start_wss(self):
        """Start the LAN WebSocket transport iff SUTANDO_SCP_WSS_ENABLE is
        truthy. The primary listener is cleartext ws://; the TLS sibling
        (wss://) starts only when a certificate is available. Legacy _wss_*
        names kept for env/back-compat.
        Best-effort: any failure here must NOT stop the UDS daemon from
        serving. Returns the transport (for cleanup) or None."""
        if (os.environ.get("SUTANDO_SCP_WSS_ENABLE") or "").lower() not in (
                "1", "true", "yes", "on"):
            return None
        try:
            from ws_transport import WsTransport  # noqa: PLC0415
            host = os.environ.get("SUTANDO_SCP_WSS_HOST") or "127.0.0.1"
            try:
                port = int(os.environ.get("SUTANDO_SCP_WSS_PORT") or "8787")
            except ValueError:
                port = 8787
            device_store = None
            if self._state_dir:
                from device_store import DeviceStore  # noqa: PLC0415
                device_store = DeviceStore(Path(self._state_dir) / "auth")
            # Media plane: with SUTANDO_VOICE_HOST_URL set, streams bind to the
            # external voice-host (the Node process that owns VoiceSession);
            # otherwise the stub serves (lifecycle + loopback, no voice stack).
            # Same interface either way — the transport does not change.
            host_url = os.environ.get("SUTANDO_VOICE_HOST_URL")
            if host_url:
                from voice_host_bridge import NodeVoiceBridge  # noqa: PLC0415
                voice = NodeVoiceBridge(host_url, log=_log)
            else:
                from voice_bridge import StubVoiceBridge  # noqa: PLC0415
                voice = StubVoiceBridge()
            def make_transport(p):
                return WsTransport(self.dispatcher, token=self._wss_token(),
                                   device_store=device_store,
                                   result_subscribers=self._subscribers,
                                   activity_subscribers=self._activity_subscribers,
                                   request_subscribers=self._request_subscribers,
                                   voice_bridge=voice,
                                   agent_id=wss_agent,
                                   host=host, port=p, log=_log)

            started = []
            # Primary listener is PLAIN ws:// — embedded devices (the M5) speak
            # it. TLS is a SIBLING listener on its own port for browsers, whose
            # mic APIs require a secure context — never a switch on the primary.
            # The agent this WSS fronts (owner→agents→runtimes→endpoints model):
            # env identity first, then the enrolled ag2space identity — a
            # deliberate SEPARATE resolution from actor_id, which keeps its
            # dispatcher-approval semantics untouched.
            wss_agent = (os.environ.get("SUTANDO_AGENT_ID") or "").strip() or None
            if not wss_agent and self._state_dir:
                try:
                    enrolled = json.loads((Path(self._state_dir) / "auth"
                                           / "ag2space.json").read_text())
                    wss_agent = enrolled.get("agent_id") or None
                except (OSError, ValueError):
                    wss_agent = None
            wss = make_transport(port)
            await wss.start()
            started.append(wss)
            # Primary is live from here: the exposure warning fires and the
            # handle list is returned even if the TLS sibling fails below.
            if host not in ("127.0.0.1", "localhost", "::1"):
                _log(f"SCP plain-WS listener exposed beyond loopback on "
                     f"ws://{host}:{port}/scp (per-credential authz: shared "
                     f"bearer is read-only; paired devices may submit/cancel "
                     f"tasks)")
                self._advertiser = self._start_advertiser(wss_agent, port)
            try:
                ssl_ctx = self._wss_ssl_context()
                if ssl_ctx is not None:
                    try:
                        tls_port = int(os.environ.get("SUTANDO_SCP_WSS_TLS_PORT")
                                       or "8443")
                    except ValueError:
                        tls_port = 8443
                    tls = make_transport(tls_port)
                    await tls.start(ssl_context=ssl_ctx)
                    started.append(tls)
                    _log(f"SCP WSS TLS sibling on wss://{host}:{tls_port}/scp "
                         f"(browser/companion clients)")
            except Exception as e:  # noqa: BLE001 — sibling is optional
                _log(f"SCP TLS sibling failed (cleartext primary unaffected, "
                     f"still serving): {e}")
            return started
        except Exception as e:  # noqa: BLE001
            _log(f"SCP WSS start failed (non-fatal, UDS unaffected): {e}")
            return None

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
                if method == "task.subscribe":
                    # Transport mode-switch: this connection becomes a push
                    # channel. Registered here (the server owns writers); the
                    # dispatcher stays pure request/response. Streams are opt-in:
                    # results (default on) and/or activity (the step feed).
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
        # Near-instant: a short poll of a small dir is sub-perceptible and
        # dependency-free (env-tunable). A true fs-event watcher (kqueue/inotify)
        # would be zero-latency but platform-specific — this is the portable v0.
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
            seen.add(f.name)
            try:
                body = f.read_text()
            except OSError:
                continue
            frame = notification_frame("task.result", {
                "taskId": f.name.removesuffix(".txt"),
                "result": body, "ts": int(f.stat().st_mtime)})
            for w in list(self._subscribers):
                try:
                    w.write(frame)
                    await w.drain()
                except (ConnectionResetError, BrokenPipeError, RuntimeError):
                    self._subscribers.discard(w)

    async def serve(self) -> None:
        # Same-instance double start is illegal (different instances may run
        # in parallel — their locks live in different per-instance run dirs).
        # flock is held for the daemon's life; per-open-file-description, so
        # keep the fd referenced on self.
        import fcntl
        lp = lock_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(lp.parent, 0o700)
        self._lock_fd = open(lp, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"instance {instance_id()!r} already has an authoritative "
                f"server (lock held: {lp}) — refusing double start")
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
        socket_path=runtime_socket or socket_path(),
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
