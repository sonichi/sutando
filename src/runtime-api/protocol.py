"""runtime-api protocol — NDJSON JSON-RPC 2.0 over a local Unix socket.

The runtime API is the request-response upstream between a long-running agent
process and its human owner: approval, elicitation, and governed capability
execution. It is deliberately NOT an activity/observability channel (tool
events and metrics already flow through the hooks collector) and NOT a task
channel (task/result files own that).

Framing: one JSON object per line (NDJSON). A connection may carry multiple
requests; responses are written in completion order with the caller's `id`.
Requests never block on a human: issuing methods return `{requestId,
status: "pending"}` immediately, and `request.wait` polls/blocks with a
timeout — so a CLI invocation, a socket connection, or an agent turn ending
never loses a pending request (they are durable in the store).

v0 methods:
  approval.request      {taskId?, action, resource?, reason?, expiresInS?}
  elicitation.request   {taskId?, question, type, options?, expiresInS?}
  capability.execute    {taskId?, action, resource?, input?, idempotencyKey?}
  request.get           {requestId}
  request.wait          {requestId, timeoutS?}
  request.cancel        {requestId}

Error codes follow JSON-RPC: -32700 parse, -32600 invalid request,
-32601 unknown method, -32602 invalid params, -32000 server error.
"""
from __future__ import annotations

import json

MAX_LINE_BYTES = 256 * 1024  # a single request has no business being bigger

ELICITATION_TYPES = ("free_text", "single_select", "multi_select", "confirmation")

METHODS = (
    "approval.request",
    "elicitation.request",
    "capability.execute",
    "request.get",
    "request.wait",
    "request.cancel",
)


def parse_line(raw: bytes):
    """One NDJSON frame → (id, method, params) or raises ProtocolError."""
    if len(raw) > MAX_LINE_BYTES:
        raise ProtocolError(-32600, "request too large")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError(-32700, f"parse error: {e}") from e
    if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
        raise ProtocolError(-32600, "invalid request: jsonrpc 2.0 object required")
    method = obj.get("method")
    if not isinstance(method, str) or method not in METHODS:
        raise ProtocolError(-32601, f"unknown method: {method!r}", req_id=obj.get("id"))
    params = obj.get("params") or {}
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "params must be an object", req_id=obj.get("id"))
    return obj.get("id"), method, params


def result_frame(req_id, result: dict) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                       ensure_ascii=False) + "\n").encode("utf-8")


def error_frame(req_id, code: int, message: str) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": code, "message": message}},
                       ensure_ascii=False) + "\n").encode("utf-8")


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, req_id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.req_id = req_id
