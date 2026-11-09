"""A minimal Agent Client Protocol client over a child's stdio. Stdlib only.

This process is the ACP Client; `openclaw acp` (or any ACP agent) is the ACP Agent. The wire is
newline-delimited JSON-RPC 2.0. The client offers no filesystem or terminal methods: the agent does its
own I/O and asks through `session/request_permission`, which `on_permission` answers. Every
`session/update` notification reaches `on_update`; agent requests this client does not implement are
answered with a JSON-RPC method-not-found error, so the agent is never left waiting.
"""
from __future__ import annotations

import collections
import json
import queue
import subprocess
import threading

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "ag2-sparrow-openclaw", "version": "0.1.0"}
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
METHOD_NOT_FOUND = -32601
AUTH_REQUIRED = -32000


class AcpError(Exception):
    """A JSON-RPC error from the agent, or a broken connection to it."""

    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class AcpGone(AcpError):
    """The agent process exited, or its stdout closed, with a call outstanding."""


class AcpTimeout(AcpError):
    """No response within the call's timeout."""


class AcpClient:
    """One spawned ACP agent. `on_permission(params) -> result dict` and `on_update(params)` run on the
    reader thread, so they must not block on this client."""

    def __init__(self, argv, cwd=None, env=None, stderr_path=None, on_permission=None, on_update=None,
                 popen=subprocess.Popen):
        self.err = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        try:
            self.p = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, cwd=cwd,
                           env=env, text=True, bufsize=1)
        except OSError:
            if self.err is not subprocess.DEVNULL:
                self.err.close()
            raise
        self.on_permission = on_permission
        self.on_update = on_update
        self.pending = {}
        self.lock = threading.Lock()
        self.next_id = 1
        self.gone = threading.Event()
        self.unhandled = collections.Counter()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self.p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                if "method" not in m:
                    q = self.pending.pop(m.get("id"), None)
                    if q is not None:
                        q.put(m)
                elif "id" in m:
                    self._answer_request(m)
                else:
                    self._notification(m)
        except (OSError, ValueError):
            pass
        finally:
            self.gone.set()
            for q in list(self.pending.values()):
                q.put(None)

    def _answer_request(self, m):
        method, rid, params = m.get("method"), m.get("id"), m.get("params") or {}
        if method == "session/request_permission" and self.on_permission is not None:
            try:
                result = self.on_permission(params)
            except Exception:  # noqa: BLE001 - a failing policy refuses, never hangs the agent
                result = {"outcome": {"outcome": "cancelled"}}
            self._send({"jsonrpc": "2.0", "id": rid, "result": result})
            return
        self.unhandled[method] += 1
        self._send({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": METHOD_NOT_FOUND, "message": "%s is not offered by this client" % method}})

    def _notification(self, m):
        if m.get("method") == "session/update" and self.on_update is not None:
            try:
                self.on_update(m.get("params") or {})
            except Exception:  # noqa: BLE001 - progress is best effort
                pass

    def _send(self, obj):
        with self.lock:
            try:
                self.p.stdin.write(json.dumps(obj) + "\n")
                self.p.stdin.flush()
            except (OSError, ValueError) as e:
                raise AcpGone("the ACP agent's stdin closed (%s)" % e)

    def call(self, method, params, timeout):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        q = queue.Queue()
        self.pending[rid] = q
        if self.gone.is_set():
            self.pending.pop(rid, None)
            raise AcpGone("%s: the ACP agent has exited" % method)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            m = q.get(timeout=timeout)
        except queue.Empty:
            self.pending.pop(rid, None)
            raise AcpTimeout("%s: no response in %ss" % (method, timeout))
        if m is None:
            raise AcpGone("%s: the ACP agent exited before answering" % method)
        if "error" in m:
            err = m["error"] if isinstance(m["error"], dict) else {}
            raise AcpError("%s: %s" % (method, err.get("message") or json.dumps(m["error"])),
                           err.get("code"), err.get("data"))
        return m.get("result") or {}

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, timeout):
        return self.call("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                        "clientCapabilities": CLIENT_CAPABILITIES, "clientInfo": CLIENT_INFO},
                         timeout)

    def close(self, grace=5.0):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        if self.err is not subprocess.DEVNULL:
            self.err.close()
