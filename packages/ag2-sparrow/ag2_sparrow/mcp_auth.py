"""ag2-sparrow-mcp — the agent's own AG2 Space MCP credential, for the agent's MCP client.

Core exchanges the relay token this agent already holds for a short-lived MCP access
token AS the agent (POST <mint_url>, named by the relay's /v1/mcp/discovery). With it
the agent's MCP client reaches AG2 Space MCP, including Room Commons Actions, under the
agent's own room membership and grants.

    ag2-sparrow-mcp headers   # {"Authorization": "Bearer <access token>"}, for headersHelper
    ag2-sparrow-mcp setup     # the `claude mcp add-json` line that wires `headers` in
    ag2-sparrow-mcp acp       # one ACP `McpServer` (http, with that header), for an ACP client
    ag2-sparrow-mcp descriptor --env-file <the agent's .env>  # the stdio `ag2-mcp-proxy` descriptor for it

`headers` mints on every run and caches nothing: Claude Code runs it at connect and
again after a 401, so an expired token is replaced without the bearer touching disk.
`acp` is the same discovery and mint, printed as the HTTP `McpServer` an ACP client puts in
`session/new` (ACP carries headers, not a helper, so the Antigravity adapter runs it per turn).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Tuple

from . import gateway_instance

DISCOVERY_PATH = "/v1/mcp/discovery"
# Claude Code gives a headersHelper 10 s in all: the bridge import (~1.5 s) plus two calls fit.
HTTP_TIMEOUT_S = 3
USER_AGENT = "sutando-gateway-client/1.0"
SERVER_NAME = "ag2-space"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
# The tools a per-tool permission policy (the Antigravity adapter's) grants: rooms, Commons Actions, tasks.
# Never: agent link/unlink, setup steps, relay-token issuance, events, Space-level execution, ui.*.
OWNER_TOOLS = frozenset({
    "ag2.whoami", "room.list", "room.inspect", "room.actions.search", "room.actions.describe",
    "room.action.read", "room.action.execute", "room.action.execute_safe", "room.task.list",
    "room.task.create", "room.task.update", "operation.inspect", "approval.inspect", "space.action.read"})


def _tool_key(name: str) -> str:
    # A client may report a dotted MCP tool name with its dots as underscores (room_action_execute).
    return name.replace("_", ".")


_OWNER_TOOL_KEYS = frozenset(_tool_key(t) for t in OWNER_TOOLS)


def owner_tool_ok(server: str, tool: str) -> bool:
    """True for one of OWNER_TOOLS on the server this module names (SERVER_NAME), compared exactly."""
    return server == SERVER_NAME and _tool_key(tool) in _OWNER_TOOL_KEYS

Gateway = Tuple[str, str, str]  # (relay URL, bearer, the .env file that supplied it or "")


class McpAuthError(Exception):
    pass


def resolve_gateway() -> Gateway:
    """The relay URL and bearer exactly as the bridge resolves them (env, device .env, vault)."""
    from . import remote_gateway_bridge as bridge
    return bridge.URL, bridge.TOKEN, bridge._FOUND.source_file


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


def _call(method: str, url: str, bearer: str, gateway_url: str = "") -> Tuple[int, dict]:
    scrub, reason = gateway_url or url, None
    with gateway_instance.gateway_call(scrub):
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {bearer}",
                                              "User-Agent": USER_AGENT,
                                              "Accept": "application/json"})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=HTTP_TIMEOUT_S) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc))
    # Raised outside gateway_call so the request stays named, with the URL and reason redacted.
    if reason is not None:
        raise McpAuthError(f"{method} {gateway_instance.redact_url(url)}: "
                           f"{gateway_instance.redact_text(reason, scrub)}")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


MINT_PATH = "/api/v1/mcp/agent-access-tokens"   # the bridge's AGENT_ACCESS_MINT_PATH (a test keeps them equal)


def same_origin(url: str, base: str) -> bool:
    """The bridge's `_same_origin` (a test keeps them equal), without importing the bridge: its import resolves a
    token, and `descriptor` must read nothing but the file it is given."""
    try:
        u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
        if not u.scheme or not u.hostname or u.scheme != b.scheme:
            return False
        default = {"https": 443, "http": 80}.get(u.scheme)
        return u.hostname.lower() == (b.hostname or "").lower() and (u.port or default) == (b.port or default)
    except ValueError:
        return False


def discover(relay_url: str, bearer: str) -> Tuple[str, str]:
    AGENT_ACCESS_MINT_PATH, _same_origin = MINT_PATH, same_origin
    _safe_url("the relay URL", relay_url)  # the bearer goes to discovery first: same rule as mint_url
    status, body = _call("GET", relay_url.rstrip("/") + DISCOVERY_PATH, bearer, relay_url)
    if status != 200:
        detail = body.get("error") or "no detail"
        raise McpAuthError(f"MCP discovery answered {status} ({detail})")
    mint_url = _safe_url("mint_url", body.get("mint_url"))
    # The relay secret goes only to the relay's own origin, as the bridge's own mint does.
    if not _same_origin(mint_url, relay_url):
        raise McpAuthError("mint_url is not on the relay's origin; the relay token is not sent there")
    if urllib.parse.urlsplit(mint_url).path != AGENT_ACCESS_MINT_PATH:
        raise McpAuthError(f"mint_url is not the relay's {AGENT_ACCESS_MINT_PATH}; the relay token is not sent there")
    return mint_url, _safe_url("mcp_url", body.get("mcp_url"))


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


def _helper_command(env_file: str, source_root: str = "") -> str:
    parts = []
    if env_file:
        parts.append(f"AG2_DEVICE_ENV={shlex.quote(env_file)}")
    root = Path(__file__).resolve().parents[1]
    if root.is_file():  # running from the zipapp
        parts.append(f"PYTHONPATH={shlex.quote(str(root))}")
        parts += [shlex.quote(sys.executable), "-m", "ag2_sparrow.mcp_auth", "headers"]
        return " ".join(parts)
    if source_root:
        # A source checkout with no install: -I keeps the client's cwd and PYTHONPATH off sys.path.
        code = (f"import sys; sys.path.insert(0, {source_root!r}); "
                "from ag2_sparrow.mcp_auth import main; raise SystemExit(main(['headers']))")
        parts += [shlex.quote(sys.executable), "-I", "-c", shlex.quote(code)]
        return " ".join(parts)
    # The console script, not `python -m`: -m puts the client's cwd first on sys.path.
    script = Path(sys.executable).with_name("ag2-sparrow-mcp")
    if script.is_file():
        parts += [shlex.quote(str(script)), "headers"]
    else:
        parts += [shlex.quote(sys.executable), "-m", "ag2_sparrow.mcp_auth", "headers"]
    return " ".join(parts)


def setup_entry(gateway: Gateway, source_root: str = "") -> dict:
    """The Claude Code MCP entry: the discovered MCP URL, authorized by the `headers` helper.

    `source_root` is a source checkout's packages/ag2-sparrow, for a client with no install."""
    url, bearer, env_file = _require(gateway)
    _mint_url, mcp_url = discover(url, bearer)
    return {"type": "http", "url": mcp_url, "headersHelper": _helper_command(env_file, source_root)}


def acp_entry(gateway: Gateway) -> dict:
    """The ACP `McpServer` (HttpMcpServer: type, name, url, headers as name/value pairs) for this agent:
    the discovered MCP URL with a freshly minted access token. The relay token is not in it."""
    url, bearer, _ = _require(gateway)
    mint_url, mcp_url = discover(url, bearer)
    return {"type": "http", "name": SERVER_NAME, "url": mcp_url,
            "headers": [{"name": "Authorization", "value": f"Bearer {mint(mint_url, bearer)}"}]}


def stdio_descriptor(env_path: str) -> dict:
    """The non-secret descriptor the desktop's stdio `ag2-mcp-proxy` reads (AG2_MCP_DESCRIPTOR) for the agent's
    own `.env` at `env_path`: that file and the key its token line has, the mint and MCP URLs discovered with that
    very token, and `bearer_digest`, the sha256 of its secret the proxy checks a plain mint against (as the
    desktop writes it). A seat .env (only a token-file pointer) gives the `token_format: "raw"` form instead:
    `env_file` is the raw token file it names, `relay` that file's own relay half, and no `env_key`, so a proxy
    without raw support fails loudly. Nothing else is read: a token in the environment, a vault or another file
    is never the descriptor's, since the proxy mints from the file alone."""
    if not env_path:
        raise McpAuthError("descriptor needs --env-file, the agent's own .env")
    from .bounded_file_read import read_private
    from .gateway_credential_sources import plain_token, raw_token_line, seat_token_pointer
    text, why = read_private(env_path)   # this user's, 0600, no symlink: it holds or names the credential
    if text is None:
        raise McpAuthError(f"{env_path} was refused ({why or 'missing'})")
    key, url, bearer = plain_token(text)
    if not key:
        pointer = seat_token_pointer(env_path, text)
        if not pointer:
            raise McpAuthError(f"{env_path} has neither a REMOTE_TASK_TOKEN= line nor a seat's token-file pointer")
        raw, why = read_private(pointer)   # the path as named, so a symlinked token file is refused
        if raw is None:
            raise McpAuthError(f"the seat's token file {pointer} was refused ({why or 'missing'})")
        raw_path = os.path.realpath(pointer)
        relay, secret = raw_token_line(raw)
        if not relay or not secret.strip():
            raise McpAuthError(f"the seat's token file {raw_path} holds no <relay_url>|<secret> line")
        mint_url, mcp_url = discover(relay, secret)
        return {"version": 1, "lane": "primary", "token_format": "raw", "env_file": raw_path, "mint_url": mint_url,
                "mcp_url": mcp_url, "relay": relay,
                "bearer_digest": hashlib.sha256(secret.strip().encode("utf-8")).hexdigest()}
    if not url or not bearer:
        raise McpAuthError(f"{env_path}'s token names no relay URL (<relay_url>|<secret>)")
    mint_url, mcp_url = discover(url, bearer)
    return {"version": 1, "lane": "primary", "env_file": env_path, "env_key": key, "mint_url": mint_url,
            "mcp_url": mcp_url, "relay": url.rstrip("/"),
            "bearer_digest": hashlib.sha256(bearer.strip().encode("utf-8")).hexdigest()}


def seat_token_file(env_path: str) -> str:
    """The realpath of the raw token file the seat .env at `env_path` names, '' when it names none."""
    from .bounded_file_read import read_private
    from .gateway_credential_sources import seat_token_path
    text, _why = read_private(env_path)
    return seat_token_path(env_path, text) if text is not None else ""


def setup_lines(gateway: Gateway) -> list:
    _url, _bearer, env_file = _require(gateway)
    cfg = setup_entry(gateway)
    lines = ["# Run in the agent's project: its Claude Code then acts in s.space as this agent.",
             f"claude mcp add-json {SERVER_NAME} {shlex.quote(json.dumps(cfg))}"]
    if not env_file:
        lines.append("# The token came from the environment: start Claude Code with "
                     "REMOTE_TASK_TOKEN set too, or keep it in a mode-600 .env named by AG2_DEVICE_ENV.")
    return lines


def main(argv=None, resolve: Callable[[], Gateway] = resolve_gateway) -> int:
    parser = argparse.ArgumentParser(prog="ag2-sparrow-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("headers", "setup", "acp", "descriptor"))
    parser.add_argument("--env-file", default="", help="descriptor: the agent's own .env the stdio proxy reads")
    args = parser.parse_args(argv)
    try:
        if args.command == "descriptor":
            print(json.dumps(stdio_descriptor(args.env_file)))
            return 0
        gateway = resolve()
        if args.command == "headers":
            print(json.dumps(headers(gateway)))
        elif args.command == "acp":
            print(json.dumps(acp_entry(gateway)))
        else:
            print("\n".join(setup_lines(gateway)))
    except McpAuthError as exc:
        print(f"ag2-sparrow-mcp: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
