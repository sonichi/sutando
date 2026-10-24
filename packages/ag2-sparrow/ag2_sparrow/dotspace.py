"""DotSpaces: the agent-native Matrix homeservers AG2 operates, and what each one looks like.

A DotSpace is an agent-native Matrix homeserver operated by AG2; ag2.space and s.space are two
DotSpace instances. Sutando connects to any of them, and one install may hold several such
connections at once, each through its own sparrow bridge, token and gateway. So the resolver is
PER SERVER: every caller passes the server it is acting for (a room id's server part, a gateway
host, an mxid's server) and asks `dotspace_for(...)`. `current_server()` is the one-install
convenience for the few places that are per install today (Sutando Cloud auth, the Station
probe).

".space" is a public TLD, so a suffix proves nothing. A DotSpace is resolved, in order, from

  1. the config section `core.dotspace.instances` (sutando.config.json), a list of
     {server, gateway_host, cloud_origin, link_via?, prefer_ipv4?}. `DEFAULT_INSTANCES`
     applies when the section is absent, so today's installs behave as before. The owner's
     config outranks a remote declaration, and a known instance costs no network round trip.
  2. the homeserver's own declaration, `https://<host>/.well-known/matrix/client`, in an AG2
     block under the key `space.ag2.dotspace`::

         {"m.homeserver": {"base_url": "https://s.space"},
          "space.ag2.dotspace": {
              "operator": "ag2",                        # required; only "ag2" is trusted
              "server": "s.space",                      # optional; defaults to the host fetched
              "gateway_url": "https://chat.s.space",    # the sparrow gateway / relay
              "cloud_origin": "https://sutando.s.space",# this instance's Sutando Cloud, or ""
              "mcp_url": "https://mcp.s.space/mcp",     # this instance's MCP endpoint, or ""
              "link_via": "s.space",                    # matrix.to `?via=`; defaults to server
              "prefer_ipv4": true}}                     # the gateway publishes AAAA; see bridge

     Fetched from the host asked about only (a gateway host serves the same document, with
     `server` naming its homeserver). `server`, when present, must be that host or a parent
     domain of it; any other claim is ignored. No server publishes this block yet: the fetch is
     injectable, cached and bounded (`WELL_KNOWN_TIMEOUT_S`), and any failure falls through.
     Callers on a hot path pass `fetch=NO_FETCH` and skip the network entirely.
  3. the gateway this process was onboarded to (REMOTE_TASK_TOKEN's embedded URL): the bridge
     talks to no other host, so that host is a DotSpace gateway by construction
     (`source="token"`).
  4. nothing: a plain Matrix server is not a DotSpace (operator "", source "none"): no cloud,
     link via itself.

Standard library only: skills import it without the rest of core, and the ag2-sparrow package
bundles it verbatim (tools/sync_from_src.py), where the config loader is absent and 2. is the
built-in default alone.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

OPERATOR_AG2 = "ag2"
WELL_KNOWN_KEY = "space.ag2.dotspace"
WELL_KNOWN_PATH = "/.well-known/matrix/client"
WELL_KNOWN_TIMEOUT_S = 1.5
WELL_KNOWN_TTL_S = 3600.0
WELL_KNOWN_NEGATIVE_TTL_S = 900.0
# The instance installs ran against before this resolver existed: the fallback whenever the
# install's own server is unknown.
LEGACY_SERVER = "ag2.space"
LEGACY_CLOUD_HOST = "sutando.ag2.ai"
CONFIG_SECTION = "dotspace"
CONFIG_LIST = "instances"

DEFAULT_INSTANCES: Tuple[Dict[str, str], ...] = (
    {"server": "ag2.space", "gateway_host": "chat.ag2.space",
     "cloud_origin": "https://sutando.ag2.space"},
    {"server": "dev.ag2.space", "gateway_host": "dev-chat.ag2.space",
     "cloud_origin": "https://dev-sutando.ag2.space"},
    {"server": "s.space", "gateway_host": "chat.s.space",
     "cloud_origin": "https://sutando.s.space"},
    {"server": "dev.s.space", "gateway_host": "dev-chat.s.space",
     "cloud_origin": "https://dev-sutando.s.space"},
)

MXID_ENV_KEYS = ("AGENT_MXID", "AG2SPACE_USER_ID")
TOKEN_ENV_KEYS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_ENV_KEYS = ("REMOTE_TASK_URL", "AG2_REMOTE_URL")

Fetch = Callable[[str, float], Optional[str]]


@dataclass(frozen=True)
class DotSpace:
    server: str
    operator: str = ""
    gateway_host: str = ""
    link_via: str = ""
    cloud_origin: str = ""
    mcp_url: str = ""
    prefer_ipv4: bool = False
    source: str = "none"

    @property
    def is_dotspace(self) -> bool:
        return self.operator == OPERATOR_AG2


# ---------------------------------------------------------------- parsing

def server_of_mxid(mxid: str) -> str:
    """The server part of an `@user:server` id — after the FIRST colon, since the server may
    carry a port or an IPv6 bracket; "" for anything that is not an @id."""
    mxid = (mxid or "").strip()
    return mxid.split(":", 1)[1] if mxid.startswith("@") and ":" in mxid else ""


def server_of_matrix_id(matrix_id: str) -> str:
    """The server part of any sigil id (`@user:`, `!room:`, `#alias:`); "" otherwise."""
    matrix_id = (matrix_id or "").strip()
    if matrix_id[:1] in ("@", "!", "#") and ":" in matrix_id:
        return matrix_id.split(":", 1)[1]
    return ""


def server_of_gateway_url(url: str) -> str:
    """The host of a gateway/token URL (`https://chat.ag2.space/...` -> `chat.ag2.space`)."""
    try:
        return (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def gateway_url_of_token(token: str) -> str:
    """The URL a combined `https://<gateway>|<secret>` onboarding token embeds, else "".
    Mirrors shared/ag2_gateway_credentials.parse_onboarding_token without importing it."""
    raw = (token or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return ""
    i = raw.find("|")
    if i == -1:
        m = re.search(r"%7[Cc]", raw)
        if m is None:
            return ""
        i = m.start()
    return raw[:i]


def _host_only(server: str) -> str:
    """A server name without its port, brackets stripped, lower-cased, for matching."""
    s = (server or "").strip().lower().rstrip(".")
    if s.startswith("["):
        return s[1:s.index("]")] if "]" in s else s[1:]
    if s.count(":") == 1:
        s = s.split(":", 1)[0]
    return s


# ------------------------------------------------------------- well-known

_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()
# One fetch in flight per process: a nested or parallel lookup (urllib resolves hosts through
# the bridge's patched getaddrinfo) falls through to config instead of fetching again.
_fetch_slot = threading.Lock()


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def NO_FETCH(url: str, timeout: float) -> Optional[str]:
    """The fetch for hot paths: never touches the network, so well-known never applies."""
    return None


def default_fetch(url: str, timeout: float) -> Optional[str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            return None
        return resp.read(64 * 1024).decode("utf-8", "replace")


def parse_well_known(text: Optional[str], fetched_host: str) -> Optional[Dict[str, Any]]:
    """The AG2 block of a well-known document, normalised, or None when absent, malformed, not
    AG2's, or claiming a server that is neither `fetched_host` nor a parent domain of it."""
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    block = doc.get(WELL_KNOWN_KEY) if isinstance(doc, dict) else None
    if not isinstance(block, dict) or block.get("operator") != OPERATOR_AG2:
        return None
    server = _host_only(str(block.get("server") or fetched_host))
    if not server or not (fetched_host == server or fetched_host.endswith("." + server)):
        return None
    gateway_host = server_of_gateway_url(str(block.get("gateway_url") or ""))
    return {
        "server": server,
        "gateway_host": gateway_host,
        "cloud_origin": _origin(block.get("cloud_origin")),
        "mcp_url": str(block.get("mcp_url") or "").strip(),
        "link_via": _host_only(str(block.get("link_via") or "")) or server,
        "prefer_ipv4": bool(block.get("prefer_ipv4", True)),
    }


def _origin(value: Any) -> str:
    v = str(value or "").strip().rstrip("/")
    return v if v.lower().startswith(("http://", "https://")) else ""


def _well_known(host: str, fetch: Fetch) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(host)
        if hit and hit[0] > now:
            return hit[1]
    if fetch is NO_FETCH or not _fetch_slot.acquire(blocking=False):
        return None
    try:
        try:
            text = fetch("https://" + host + WELL_KNOWN_PATH, WELL_KNOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — any failure is "no declaration", next source
            text = None
        block = parse_well_known(text, host)
    finally:
        _fetch_slot.release()
    ttl = WELL_KNOWN_TTL_S if block else WELL_KNOWN_NEGATIVE_TTL_S
    with _cache_lock:
        _cache[host] = (time.monotonic() + ttl, block)
    return block


# ----------------------------------------------------------------- config

def _config_entries(config: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The `instances` list of `config` (the section), of the loaded sutando config when
    `config` is None, else the built-in default."""
    section = config
    if section is None:
        section = _loaded_section()
    listed = section.get(CONFIG_LIST) if isinstance(section, Mapping) else None
    if not isinstance(listed, list):
        return [dict(e) for e in DEFAULT_INSTANCES]
    out = []
    for e in listed:
        if isinstance(e, Mapping) and _host_only(str(e.get("server") or "")):
            out.append(dict(e))
    return out


def _loaded_section() -> Optional[Mapping[str, Any]]:
    """`core.dotspace` from the canonical loader; None where the loader is absent
    (the bundled package) or fails, so the built-in default applies."""
    try:
        import sutando_config  # type: ignore
    except Exception:  # noqa: BLE001 — stdlib-only consumers have no loader on the path
        return None
    try:
        core = sutando_config.load_config().get("core") or {}
        section = core.get(CONFIG_SECTION)
    except Exception:  # noqa: BLE001 — a broken config must not take links down
        return None
    return section if isinstance(section, Mapping) else None


def _entry_dotspace(entry: Mapping[str, Any], source: str) -> DotSpace:
    server = _host_only(str(entry.get("server") or ""))
    return DotSpace(
        server=server,
        operator=OPERATOR_AG2,
        gateway_host=_host_only(str(entry.get("gateway_host") or "")),
        link_via=_host_only(str(entry.get("link_via") or "")) or server,
        cloud_origin=_origin(entry.get("cloud_origin")),
        mcp_url=str(entry.get("mcp_url") or "").strip(),
        prefer_ipv4=bool(entry.get("prefer_ipv4", True)),
        source=source,
    )


def _matches(entry: Mapping[str, Any], host: str) -> bool:
    server = _host_only(str(entry.get("server") or ""))
    gateway = _host_only(str(entry.get("gateway_host") or ""))
    return bool(server) and (host == server or host == gateway or host.endswith("." + server))


# ------------------------------------------------------------- resolution

def dotspace_for(server_or_host: str, *, fetch: Optional[Fetch] = None,
               config: Optional[Mapping[str, Any]] = None,
               env: Optional[Mapping[str, str]] = None) -> DotSpace:
    """The DotSpace of a homeserver name, gateway host, sigil id or URL, by the order in the
    module docstring; a plain server comes back with operator "" and source "none". `fetch=None` is the real well-known fetch; pass `NO_FETCH` on a hot path.
    `config` is the `core.dotspace` section; None reads the loaded config."""
    raw = (server_or_host or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        raw = server_of_gateway_url(raw)
    host = _host_only(server_of_matrix_id(raw) or raw)
    if not host:
        return DotSpace(server="")
    # The most specific server wins: `chat.dev.s.space` is dev.s.space's gateway, not s.space's.
    matched = [e for e in _config_entries(config) if _matches(e, host)]
    if matched:
        return _entry_dotspace(max(matched, key=lambda e: len(str(e.get("server")))), "config")
    block = _well_known(host, default_fetch if fetch is None else fetch)
    if block and _matches(block, host):
        return _entry_dotspace(block, "well-known")
    gateway = server_of_gateway_url(_own_gateway_url(os.environ if env is None else env))
    if gateway and host == gateway:
        return DotSpace(server=host, operator=OPERATOR_AG2, gateway_host=host, link_via=host,
                      prefer_ipv4=True, source="token")
    return DotSpace(server=host, link_via=host)


def is_dotspace(server_or_host: str, *, fetch: Optional[Fetch] = None) -> bool:
    return dotspace_for(server_or_host, fetch=fetch).is_dotspace


def link_via_for(matrix_id_or_server: str, *, fetch: Optional[Fetch] = None) -> str:
    """The matrix.to `?via=` server for a room id, mxid or server name; "" when it names none."""
    fam = dotspace_for(matrix_id_or_server, fetch=fetch)
    return fam.link_via or fam.server


def cloud_origin_for(server: str, *, fetch: Optional[Fetch] = None) -> str:
    return dotspace_for(server, fetch=fetch).cloud_origin


def trusted_cloud_hosts(config: Optional[Mapping[str, Any]] = None) -> frozenset:
    """Hosts a Sutando Cloud bearer may be sent to: every CONFIGURED instance's cloud origin (a
    well-known declaration never widens this) plus the retired production host."""
    hosts = {LEGACY_CLOUD_HOST}
    for entry in _config_entries(config):
        host = server_of_gateway_url(_origin(entry.get("cloud_origin")))
        if host:
            hosts.add(host)
    return frozenset(hosts)


def legacy_dotspace(config: Optional[Mapping[str, Any]] = None) -> DotSpace:
    """`LEGACY_SERVER`'s DotSpace from config alone: the fallback for an unknown install."""
    for entry in _config_entries(config):
        if _matches(entry, LEGACY_SERVER):
            return _entry_dotspace(entry, "config")
    return _entry_dotspace(DEFAULT_INSTANCES[0], "config")


def default_link_via(env: Optional[Mapping[str, str]] = None) -> str:
    """`?via=` for a link whose room names no server: the install's DotSpace, else the legacy one."""
    return link_via_for(current_server(env), fetch=NO_FETCH) or legacy_dotspace().link_via


def default_cloud_origin(env: Optional[Mapping[str, str]] = None) -> str:
    """Sutando Cloud for this install: its server's DotSpace's, else the legacy one's.
    Per install, not per connection: an install on several DotSpaces reaches one cloud."""
    return cloud_origin_for(current_server(env)) or legacy_dotspace().cloud_origin


# ----------------------------------------------------------------- install

def _own_gateway_url(env: Mapping[str, str]) -> str:
    for key in TOKEN_ENV_KEYS:
        url = gateway_url_of_token(env.get(key, ""))
        if url:
            return url
    for key in URL_ENV_KEYS:
        if (env.get(key) or "").strip():
            return env[key].strip()
    return ""


def current_server(env: Optional[Mapping[str, str]] = None) -> str:
    """This process's homeserver: the server of AGENT_MXID / AG2SPACE_USER_ID, else the host of
    the gateway URL REMOTE_TASK_TOKEN embeds (or REMOTE_TASK_URL names), else "". One process
    serves one connection; an install on several passes the server to `dotspace_for` itself."""
    env = os.environ if env is None else env
    for key in MXID_ENV_KEYS:
        server = server_of_mxid(env.get(key, ""))
        if server:
            return server
    return server_of_gateway_url(_own_gateway_url(env))


if __name__ == "__main__":  # pragma: no cover — `python3 dotspace.py <server|host>`
    for arg in sys.argv[1:] or [current_server()]:
        print(json.dumps(dotspace_for(arg).__dict__, indent=1))
