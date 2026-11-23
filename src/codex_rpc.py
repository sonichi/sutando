"""JSON-RPC 2.0 over a child's stdio, the client every Codex app-server caller uses (the seat, the
account lookup): one JSON object per line, responses matched to requests by id."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import List, Optional


class Rpc:
    """JSON-RPC 2.0 over a child's stdio, one JSON object per line."""

    def __init__(self, argv: List[str], env: Optional[dict] = None, cwd: Optional[str] = None, stderr=None):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                  text=True, bufsize=1, cwd=cwd, stderr=stderr)
        self.events: "queue.Queue" = queue.Queue()
        self.pending: dict = {}
        self.lock = threading.Lock()
        self.next_id = 1
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _reader(self) -> None:
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if "id" in m and "method" not in m:
                waiter = self.pending.pop(m["id"], None)
                if waiter is not None:
                    waiter.put(m)
            else:
                self.events.put(m)
        for waiter in list(self.pending.values()):
            waiter.put({"error": {"message": "app-server exited"}})
        self.events.put(None)

    def _send(self, obj: dict) -> None:
        with self.lock:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 120):
        """The response's `result` ({} when absent); raises RuntimeError on an error or no answer."""
        m = self.call(method, params, timeout)
        if "error" in m:
            raise RuntimeError(f"{method}: {json.dumps(m['error'])[:300]}")
        return m.get("result") or {}

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 120) -> dict:
        """The whole response envelope, unvalidated; raises RuntimeError when none arrives."""
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        waiter: "queue.Queue" = queue.Queue()
        self.pending[rid] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            m = waiter.get(timeout=timeout)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"{method}: app-server pipe closed ({e})")
        except queue.Empty:
            raise RuntimeError(f"{method}: no response in {timeout}s")
        finally:
            self.pending.pop(rid, None)
        return m

    def send(self, obj: dict) -> bool:
        """Best effort: False when the child is gone."""
        try:
            self._send(obj)
            return True
        except (OSError, ValueError):
            return False

    def alive(self) -> bool:
        return self.p.poll() is None

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.reader.join(timeout=5)
        self.p.stdout.close()
