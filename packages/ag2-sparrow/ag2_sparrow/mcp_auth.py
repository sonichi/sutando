"""ag2-sparrow-mcp — the agent's own AG2 Space MCP credential, for the agent's MCP client.

Core exchanges the relay token this agent already holds for a short-lived MCP access
token AS the agent (POST <mint_url>, named by the relay's /v1/mcp/discovery). With it
the agent's MCP client reaches AG2 Space MCP, including Room Commons Actions, under the
agent's own room membership and grants.

    ag2-sparrow-mcp headers   # {"Authorization": "Bearer <access token>"}, for headersHelper
    ag2-sparrow-mcp setup     # the `claude mcp add-json` line that wires `headers` in

`headers` mints on every run and caches nothing: Claude Code runs it at connect and
again after a 401, so an expired token is replaced without the bearer touching disk.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Tuple

DISCOVERY_PATH = "/v1/mcp/discovery"
# Claude Code gives a headersHelper 10 s in all; the bridge import takes ~1.5 s of it.
HTTP_TIMEOUT_S = 4
USER_AGENT = "sutando-gateway-client/1.0"
SERVER_NAME = "ag2-space"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

Gateway = Tuple[str, str, str]  # (relay URL, bearer, the .env file that supplied it or "")


class McpAuthError(Exception):
    pass


def resolve_gateway() -> Gateway:
    """The relay URL and bearer exactly as the bridge resolves them (env, device .env, vault)."""
    from . import remote_gateway_bridge as bridge
    return bridge.URL, bridge.TOKEN, bridge._TOKEN_FILE_FALLBACK


def _safe_url(field: str, value: object) -> str:
    # The bearer is sent to mint_url: only TLS, or plain http to this machine.
    if not isinstance(value, str) or not value:
        raise McpAuthError(f"discovery answered without {field}")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK:
        return value
    raise McpAuthError(f"{field} must be https (or http to loopback): {parsed.scheme}://{parsed.hostname}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies Authorization onto the redirect, which would carry the bearer past _safe_url.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _call(method: str, url: str, bearer: str) -> Tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {bearer}",
                                          "User-Agent": USER_AGENT,
                                          "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise McpAuthError(f"{method} {url}: {getattr(exc, 'reason', exc)}") from None
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def discover(relay_url: str, bearer: str) -> Tuple[str, str]:
    status, body = _call("GET", relay_url.rstrip("/") + DISCOVERY_PATH, bearer)
    if status != 200:
        detail = body.get("error") or "no detail"
        raise McpAuthError(f"MCP discovery answered {status} ({detail})")
    return _safe_url("mint_url", body.get("mint_url")), _safe_url("mcp_url", body.get("mcp_url"))


def mint(mint_url: str, bearer: str) -> str:
    status, body = _call("POST", mint_url, bearer)
    token = body.get("access_token")
    if 200 <= status < 300 and isinstance(token, str) and token:
        return token
    code = body.get("code") or "no code"
    raise McpAuthError(f"the MCP access-token exchange answered {status} ({code})")


def _require(gateway: Gateway) -> Gateway:
    url, bearer, env_file = gateway
    if not url or not bearer:
        raise McpAuthError("no relay token: set REMOTE_TASK_TOKEN (<relay_url>|<secret>), "
                           "or AG2_DEVICE_ENV to the .env that holds it, as for ag2-sparrow")
    return url, bearer, env_file


def headers(gateway: Gateway) -> dict:
    url, bearer, _ = _require(gateway)
    mint_url, _mcp_url = discover(url, bearer)
    return {"Authorization": f"Bearer {mint(mint_url, bearer)}"}


def _helper_command(env_file: str) -> str:
    parts = []
    if env_file:
        parts.append(f"AG2_DEVICE_ENV={shlex.quote(env_file)}")
    root = Path(__file__).resolve().parents[1]
    if root.is_file():  # running from the zipapp
        parts.append(f"PYTHONPATH={shlex.quote(str(root))}")
    parts += [shlex.quote(sys.executable), "-m", "ag2_sparrow.mcp_auth", "headers"]
    return " ".join(parts)


def setup_lines(gateway: Gateway) -> list:
    url, bearer, env_file = _require(gateway)
    _mint_url, mcp_url = discover(url, bearer)
    cfg = {"type": "http", "url": mcp_url, "headersHelper": _helper_command(env_file)}
    lines = ["# Run in the agent's project: its Claude Code then acts in AG2 Space as this agent.",
             f"claude mcp add-json {SERVER_NAME} {shlex.quote(json.dumps(cfg))}"]
    if not env_file:
        lines.append("# The token came from the environment: start Claude Code with "
                     "REMOTE_TASK_TOKEN set too, or keep it in a mode-600 .env named by AG2_DEVICE_ENV.")
    return lines


def main(argv=None, resolve: Callable[[], Gateway] = resolve_gateway) -> int:
    parser = argparse.ArgumentParser(prog="ag2-sparrow-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("headers", "setup"))
    args = parser.parse_args(argv)
    try:
        gateway = resolve()
        if args.command == "headers":
            print(json.dumps(headers(gateway)))
        else:
            print("\n".join(setup_lines(gateway)))
    except McpAuthError as exc:
        print(f"ag2-sparrow-mcp: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
