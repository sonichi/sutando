"""DotSpaces: the agent-native Matrix homeservers AG2 operates, and what each one looks like.

A DotSpace is an agent-native Matrix homeserver operated by AG2; ag2.space and s.space are two
DotSpace instances. Sutando connects to any of them, and one install may hold several such
connections at once, each through its own sparrow bridge, token and gateway. So the resolver is
PER SERVER: every caller passes the server it is acting for (a room id's server part, a gateway
host, an mxid's server) and asks `dotspace_for(...)`. `current_server()` is the one-install
convenience for the few places that are per install today (Sutando Cloud auth, the Station
probe).

".space" is a public TLD, so a suffix proves nothing, and HTTPS proves control of a domain, not
who operates it. Whether a server IS a DotSpace is therefore decided by the registry alone:

  1. the config section `core.dotspace.instances` (sutando.config.json), a list of
     {server, gateway_host, cloud_origin?, mcp_url?, link_via?, prefer_ipv4?}. `DEFAULT_INSTANCES`
     applies when the section is absent, so today's installs behave as before. A registered
     server is trusted without any round trip; an explicit `dotspace_for(server)` with the
     real fetch still reads its well-known, only to fill the fields config left unset.
     `cloud_origin` is set only where that instance has its own Sutando Cloud (today
     ag2.space and dev.ag2.space); whether Cloud is per DotSpace or shared is undecided, so
     the other defaults carry none and callers fall back to the shared production Cloud,
     `PRODUCTION_CLOUD_ORIGIN`, which no instances list can empty.
  2. the homeserver's own declaration, `https://<host>/.well-known/matrix/client`, in an AG2
     block under the key `space.ag2.dotspace`, is CAPABILITY DISCOVERY, never classification::

         {"m.homeserver": {"base_url": "https://s.space"},
          "space.ag2.dotspace": {
              "operator": "ag2",                        # required; only "ag2" is trusted
              "server": "s.space",                      # optional; defaults to the host fetched
              "gateway_url": "https://chat.s.space",    # the sparrow gateway / relay
              "cloud_origin": "https://sutando.s.space",# this instance's Sutando Cloud, or ""
              "mcp_url": "https://mcp.s.space/mcp",     # this instance's MCP endpoint, or ""
              "link_via": "s.space",                    # matrix.to `?via=`; defaults to server
              "prefer_ipv4": true}}                     # the gateway publishes AAAA; see bridge

     Fetched from the host asked about only, over TLS, following no redirect, so the document
     is attributed to that host alone (a gateway host serves the same document, with `server`
     naming its homeserver). `server`, when present, must be that host or its parent domain,
     one label up and itself dotted (`attacker.space` cannot claim `space`); `gateway_url`,
     `link_via`, `cloud_origin` and `mcp_url` must sit at or under that server; any other block
     is refused. For a REGISTERED server the block only fills what config left unset
     (gateway_host, mcp_url, link_via, prefer_ipv4); cloud_origin never comes from it, so the
     hosts a bearer may be sent to are config's alone. For an unregistered server it yields operator "" and
     source "well-known-unverified": the discovered gateway_host and mcp_url, no AG2
     behaviour. No server publishes this block yet: the fetch is injectable, cached and
     bounded (`WELL_KNOWN_TIMEOUT_S`), and any failure falls through. Only an explicit
     per-server `dotspace_for(server)` fetches; the install-wide defaults and every hot path
     pass `fetch=NO_FETCH` and never touch the network.
  3. the gateway this process was onboarded to, when the caller passes it (`own_gateway=`, the
     URL the credential owner resolved): the bridge talks to no other host, so that host is a
     DotSpace gateway by construction (`source="token"`). The resolver reads no token variable.
  4. nothing: a plain Matrix server is not a DotSpace (operator "", source "none"): no cloud,
     link via itself (port included). A name carrying `/`, `?`, `#`, `@` or whitespace is no
     server at all: nothing is fetched for it and it never reaches a URL.

Standard library only: skills import it without the rest of core, and the ag2-sparrow package
bundles it verbatim (tools/sync_from_src.py), where the config loader is absent and 2. is the
built-in default alone.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.error
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
# The shared production Sutando Cloud: the fallback of every instance without a Cloud of its
# own and the home of every pre-scoping credential. A constant, so no instances list empties it.
PRODUCTION_CLOUD_ORIGIN = "https://sutando.ag2.space"
CONFIG_SECTION = "dotspace"
CONFIG_LIST = "instances"
# A Matrix server name and nothing else (DNS labels or an IPv6 literal, an optional port): what a
# URL could read as a path, query, fragment, userinfo, scheme, escape or whitespace never matches.
_SERVER_NAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
                          r"|\[(?P<v6>[0-9a-f:.]+)\])(?::(?P<port>[1-9][0-9]{0,4}))?")


def is_server_name(name: str) -> bool:
    """`name` is exactly a Matrix server name: the allowlist above, a port in 1..65535 and, in
    brackets, an address that parses as IPv6."""
    m = _SERVER_NAME.fullmatch(name or "")
    if not m:
        return False
    if m.group("port") is not None and not 1 <= int(m.group("port")) <= 65535:
        return False
    if m.group("v6") is not None:
        try:
            ipaddress.IPv6Address(m.group("v6"))
        except ValueError:
            return False
    return True


def _strict_url(value: Any, *, endpoint: bool = False) -> Optional[str]:
    """An http(s) URL whose host is a server name, with no userinfo and no backslash (parsers
    disagree on `\\`); `endpoint` also demands https and no query or fragment, since the value is
    dialled, not displayed. "" when absent, None when present and not that."""
    v = str(value or "")  # unstripped: str.strip() would hide U+2028 or U+0085 at the edges
    if not v:
        return ""
    schemes = ("https://",) if endpoint else ("http://", "https://")
    # urlsplit deletes tab, CR and LF before parsing, so they are refused on the raw string.
    if "\\" in v or not v.isascii() or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in v) \
            or not v.lower().startswith(schemes):
        return None
    try:
        parts = urllib.parse.urlsplit(v)
    except ValueError:
        return None
    netloc = parts.netloc.lower()
    if parts.username is not None or parts.password is not None or not is_server_name(netloc):
        return None
    if endpoint and ("?" in v or "#" in v):
        return None
    return v


def _via(value: Any) -> str:
    """A declared `link_via`, port kept, when it is a server name; "" otherwise."""
    v = str(value or "").lower().rstrip(".")
    return v if v and is_server_name(v) else ""


def _never_fetched(host: str) -> bool:
    """An IP literal or a single-label name serves no operator's declaration: no fetch, so a
    room id can never aim the resolver at an address."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    last = host.rsplit(".", 1)[-1]
    # 127.1, 0x7f.1 and 127.0.0.01 are addresses to the resolver library, never a domain.
    return "." not in host or last.isdigit() or (last.startswith("0x") and len(last) > 2)

DEFAULT_INSTANCES: Tuple[Dict[str, str], ...] = (
    {"server": "ag2.space", "gateway_host": "chat.ag2.space",
     "cloud_origin": PRODUCTION_CLOUD_ORIGIN, "mcp_url": "https://mcp.ag2.space/mcp"},
    {"server": "dev.ag2.space", "gateway_host": "dev-chat.ag2.space",
     "cloud_origin": "https://dev-sutando.ag2.space", "mcp_url": "https://dev-mcp.ag2.space/mcp"},
    {"server": "s.space", "gateway_host": "chat.s.space", "mcp_url": "https://mcp.s.space/mcp"},
    {"server": "dev.s.space", "gateway_host": "dev-chat.s.space", "mcp_url": "https://dev-mcp.s.space/mcp"},
)

MXID_ENV_KEYS = ("AGENT_MXID", "AG2SPACE_USER_ID")

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


def _bool(value: Any, default: bool) -> bool:
    """A real boolean, or the strings "true"/"false" in any case; anything else is `default`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return default


# ------------------------------------------------------------- well-known

_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()
# One fetch in flight per process; a parallel caller waits for it and reads the cached answer.
_fetch_slot = threading.Lock()
_fetching = threading.local()


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def NO_FETCH(url: str, timeout: float) -> Optional[str]:
    """The fetch for hot paths: never touches the network, so well-known never applies."""
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A declaration is attributed to the host asked, never to one it redirects to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _gateway_call(url: str):
    """The package's URL-scrubbing context for every call that takes a URL; a no-op where the
    package is absent (the src tree, skills)."""
    try:
        from . import gateway_instance  # type: ignore
    except ImportError:
        try:
            import gateway_instance  # type: ignore
        except ImportError:
            return contextlib.nullcontext()
    return gateway_instance.gateway_call(url)


def default_fetch(url: str, timeout: float) -> Optional[str]:
    with _gateway_call(url):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read(64 * 1024).decode("utf-8", "replace")


def parse_well_known(text: Optional[str], fetched_host: str) -> Optional[Dict[str, Any]]:
    """The AG2 block of a well-known document, normalised, or None when absent, malformed, not
    AG2's, claiming a server `fetched_host` may not speak for, or naming hosts outside it."""
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
    if not server or not _may_speak_for(fetched_host, server):
        return None
    # Every value is validated as strictly as a typed name, or the whole block is refused: these
    # strings reach `?via=`, the relay URL and a Cloud origin on another DotSpace's say-so.
    gateway_url = _strict_url(block.get("gateway_url"))
    cloud_url, mcp_url = (_strict_url(block.get(k), endpoint=True) for k in ("cloud_origin", "mcp_url"))
    via_raw = str(block.get("link_via") or "").strip().lower().rstrip(".")
    if None in (gateway_url, cloud_url, mcp_url) or (via_raw and not is_server_name(via_raw)):
        return None
    gateway_host = server_of_gateway_url(gateway_url)
    cloud_origin = _origin(cloud_url)
    link_via = via_raw or server  # a declared port stays, as it does for a typed server
    for host in (gateway_host, _host_only(link_via), server_of_gateway_url(cloud_origin), server_of_gateway_url(mcp_url)):
        if host and not _under(host, server):
            return None
    return {
        "server": server,
        "gateway_host": gateway_host,
        "cloud_origin": cloud_origin,
        "mcp_url": mcp_url,
        "link_via": link_via,
        "prefer_ipv4": _bool(block.get("prefer_ipv4"), True),
    }


def _under(host: str, server: str) -> bool:
    return host == server or host.endswith("." + server)


def _may_speak_for(fetched_host: str, server: str) -> bool:
    """A host declares itself, or its parent domain when that parent is dotted and the host is
    exactly one label below it (`chat.s.space` for `s.space`; never `attacker.space` for `space`)."""
    if fetched_host == server:
        return True
    return "." in server and fetched_host.endswith("." + server) \
        and "." not in fetched_host[: -len(server) - 1]


def _origin(value: Any) -> str:
    """`scheme://host[:port]` of an http(s) URL, nothing after the host; "" otherwise."""
    v = str(value or "").strip()
    if not v.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urllib.parse.urlsplit(v)
    except ValueError:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts.netloc else ""


def _well_known(host: str, fetch: Fetch) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(host)
        if hit and hit[0] > now:
            return hit[1]
    if fetch is NO_FETCH or getattr(_fetching, "active", False) or _never_fetched(host):
        return None  # a lookup made by the fetch itself (DNS hook) never fetches again
    with _fetch_slot:
        with _cache_lock:
            hit = _cache.get(host)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        _fetching.active = True
        try:
            text = fetch("https://" + host + WELL_KNOWN_PATH, WELL_KNOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — any failure is "no declaration", next source
            text = None
        finally:
            _fetching.active = False
        block = parse_well_known(text, host)
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
        link_via=_via(entry.get("link_via")) or server,
        cloud_origin=_origin(entry.get("cloud_origin")),
        mcp_url=str(entry.get("mcp_url") or "").strip(),
        prefer_ipv4=_bool(entry.get("prefer_ipv4"), True),
        source=source,
    )


def _matches(entry: Mapping[str, Any], host: str) -> bool:
    server = _host_only(str(entry.get("server") or ""))
    gateway = _host_only(str(entry.get("gateway_host") or ""))
    return bool(server) and (host == server or host == gateway or host.endswith("." + server))


# ------------------------------------------------------------- resolution

def dotspace_for(server_or_host: str, *, fetch: Optional[Fetch] = None,
               config: Optional[Mapping[str, Any]] = None,
               own_gateway: str = "") -> DotSpace:
    """The DotSpace of a homeserver name, gateway host, sigil id or URL, by the order in the
    module docstring; a plain server comes back with operator "" and source "none". `fetch=None`
    is the real well-known fetch; pass `NO_FETCH` on a hot path. `config` is the `core.dotspace`
    section; None reads the loaded config."""
    raw = (server_or_host or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        raw = server_of_gateway_url(raw)
    name = (server_of_matrix_id(raw) or raw).strip().lower().rstrip(".")
    host = _host_only(name)
    if not host or not is_server_name(name):
        return DotSpace(server="")  # no server: nothing to fetch, nothing to put in a URL
    fetch = default_fetch if fetch is None else fetch
    # The most specific server wins: `chat.dev.s.space` is dev.s.space's gateway, not s.space's.
    matched = [e for e in _config_entries(config) if _matches(e, host)]
    if matched:
        entry = dict(max(matched, key=lambda e: len(str(e.get("server")))))
        block = _well_known(host, fetch)
        if block and block["server"] == _host_only(str(entry["server"])):
            for key in ("gateway_host", "mcp_url", "link_via", "prefer_ipv4"):
                if key not in entry or entry[key] in ("", None):
                    entry[key] = block[key]  # discovery fills what the registry left unset
        return _entry_dotspace(entry, "config")
    block = _well_known(host, fetch)
    # Unregistered: only a declaration for the fetched host itself; a parent claim (x.co.uk for
    # co.uk) counts for a registered server alone, and a registered parent matched above.
    if block and block["server"] == host:
        return DotSpace(server=block["server"], gateway_host=block["gateway_host"],
                        link_via=block["link_via"], mcp_url=block["mcp_url"],
                        source="well-known-unverified")
    gateway = server_of_gateway_url(own_gateway)
    if gateway and host == gateway:
        return DotSpace(server=host, operator=OPERATOR_AG2, gateway_host=host, link_via=host,
                      prefer_ipv4=True, source="token")
    return DotSpace(server=host, link_via=name)  # a plain server links via itself, port kept


def is_dotspace(server_or_host: str, *, fetch: Optional[Fetch] = None,
                config: Optional[Mapping[str, Any]] = None) -> bool:
    return dotspace_for(server_or_host, fetch=fetch, config=config).is_dotspace


def link_via_for(matrix_id_or_server: str, *, fetch: Optional[Fetch] = None,
                 config: Optional[Mapping[str, Any]] = None) -> str:
    """The matrix.to `?via=` server for a room id, mxid or server name; "" when it names none."""
    d = dotspace_for(matrix_id_or_server, fetch=fetch, config=config)
    return d.link_via or d.server


def cloud_origin_for(server: str, *, fetch: Optional[Fetch] = None,
                     config: Optional[Mapping[str, Any]] = None) -> str:
    return dotspace_for(server, fetch=fetch, config=config).cloud_origin


def trusted_cloud_hosts(config: Optional[Mapping[str, Any]] = None) -> frozenset:
    """Hosts a Sutando Cloud bearer may be sent to: every CONFIGURED instance's cloud origin (a
    well-known declaration never widens this), the shared production Cloud that an instance with
    no cloud of its own falls back to (`PRODUCTION_CLOUD_ORIGIN`, and the legacy entry's when
    config gives it one, so the fallback and this allowlist always agree), and the retired
    production host."""
    hosts = {LEGACY_CLOUD_HOST, server_of_gateway_url(PRODUCTION_CLOUD_ORIGIN)}
    shared = server_of_gateway_url(legacy_dotspace(config).cloud_origin)
    if shared:
        hosts.add(shared)
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


def registered_instances(config: Optional[Mapping[str, Any]] = None) -> List[DotSpace]:
    """Every DotSpace the registry names (the configured `instances` list, else the built-in
    default), one per server in registry order, from config alone: nothing is fetched."""
    out: List[DotSpace] = []
    seen = set()
    for entry in _config_entries(config):
        d = _entry_dotspace(entry, "config")
        if d.server and d.server not in seen:
            seen.add(d.server)
            out.append(d)
    return out


def default_link_via(env: Optional[Mapping[str, str]] = None) -> str:
    """`?via=` for a link whose room names no server: the install's DotSpace, else the legacy one."""
    return link_via_for(current_server(env), fetch=NO_FETCH) or legacy_dotspace().link_via


def default_cloud_origin(env: Optional[Mapping[str, str]] = None,
                         config: Optional[Mapping[str, Any]] = None) -> str:
    """Sutando Cloud for this install: its server's DotSpace's, else the legacy entry's, else
    `PRODUCTION_CLOUD_ORIGIN` (an instance with no cloud of its own uses the shared Cloud;
    `trusted_cloud_hosts` lists it). Config only, never a fetch (callers read this at import)."""
    own = cloud_origin_for(current_server(env), fetch=NO_FETCH, config=config)
    return own or legacy_dotspace(config).cloud_origin or PRODUCTION_CLOUD_ORIGIN


# ----------------------------------------------------------------- install


def current_server(env: Optional[Mapping[str, str]] = None, own_gateway: str = "") -> str:
    """This process's homeserver: the server of AGENT_MXID / AG2SPACE_USER_ID, else the host of
    `own_gateway` (the gateway URL the credential owner resolved, passed by the caller), else "".
    One process serves one connection; an install on several passes the server itself."""
    env = os.environ if env is None else env
    for key in MXID_ENV_KEYS:
        server = server_of_mxid(env.get(key, ""))
        if server:
            return server
    return server_of_gateway_url(own_gateway)


if __name__ == "__main__":  # pragma: no cover — `python3 dotspace.py <server|host>`
    for arg in sys.argv[1:] or [current_server()]:
        print(json.dumps(dotspace_for(arg).__dict__, indent=1))
