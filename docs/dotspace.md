# DotSpace

A DotSpace is an agent-native Matrix homeserver operated by AG2; ag2.space and s.space are two
DotSpace instances. Sutando connects to any of them through the ag2-sparrow bridge, and one
install may connect to several at once, each through its own bridge, token and gateway.

`src/dotspace.py` is the one place that knows which homeservers are DotSpaces and what each one
looks like (gateway host, `matrix.to` via server, Sutando Cloud origin, IPv4 preference). No
other behaviour-bearing code carries a homeserver literal; `tests/dotspace-literal-guard.test.py`
refuses a new one. ".space" is a public TLD, so a suffix is never evidence.

## Resolution is per server, not per install

Every caller passes the server it is acting for and asks `dotspace_for(server_or_host)`: a room
id's server part (`!x:server`), an mxid's, a gateway host or URL. Links take their via from the
room's own server; the bridge's IPv4 preference asks per host. Only Sutando Cloud auth
(`src/cloud_auth.py`) and the Station probe (`src/runtime-health.py`) use `current_server()`:
Sutando Cloud is per install today, so an install on several DotSpaces reaches one cloud, that
of the server its identity (`AGENT_MXID` / `AG2SPACE_USER_ID`) or onboarding token names, else
ag2.space's. `current_server()` never raises and never picks between bridges: it reads one
process's environment; a multi-DotSpace install passes the server explicitly.

## Trust model

".space" is a public TLD, and HTTPS proves control of a domain, not who operates it. So whether a
server **is** a DotSpace is decided by the registry alone (source 1 below). A well-known
declaration is capability discovery for a server the registry already trusts; on its own it
never admits a server, and nothing in it ever adds a host a Sutando Cloud bearer may be sent to.

Future path, not implemented: an AG2-signed declaration (the well-known block signed by an AG2
key that ships in config) could admit a server without a registry entry; until then a new
instance is a config entry.

## Sources, in order

1. **Config (the registry)** — `core.dotspace.instances` in `sutando.config.json` /
   `sutando.config.local.json`:

   ```json
   {"core": {"dotspace": {"instances": [
     {"server": "ag2.space", "gateway_host": "chat.ag2.space",
      "cloud_origin": "https://sutando.ag2.space"},
     {"server": "s.space", "gateway_host": "chat.s.space"}
   ]}}}
   ```

   Optional per entry: `cloud_origin`, `mcp_url`, `link_via` (default: server), `prefer_ipv4`
   (default: true). When the section is absent the built-in list applies — ag2.space,
   dev.ag2.space, s.space, dev.s.space with their `chat.` / `dev-chat.` gateways and their MCP
   endpoints (`mcp.` / `dev-mcp.` under ag2.space and s.space, path `/mcp`); `cloud_origin` only
   on ag2.space (`https://sutando.ag2.space`) and dev.ag2.space (`https://dev-sutando.ag2.space`)
   — so today's installs behave as before. The registry is the one place a per-server endpoint
   is written down: a caller takes `dotspace_for(<its primary connection's server>).mcp_url`,
   never a literal URL (`docs/codex-core.md` shows the lookup). The most specific server wins (`chat.dev.s.space`
   is dev.s.space's gateway). A registered instance is trusted without any round trip; an
   explicit `dotspace_for(server)` with the real fetch still reads its well-known, only to
   fill the fields the entry leaves unset.

   **Sutando Cloud is undecided** (per DotSpace or shared), so the s.space defaults carry no
   `cloud_origin`: `cloud_origin_for("s.space")` is `""` and callers fall back to the shared
   production Cloud, `PRODUCTION_CLOUD_ORIGIN` (`https://sutando.ag2.space`) — a constant, so
   an instances list that registers ag2.space without a `cloud_origin` cannot empty it. An
   operator opts an instance into its own Cloud by setting `cloud_origin` on its entry;
   `trusted_cloud_hosts()` is then those configured hosts plus the shared production host and
   the retired `sutando.ag2.ai`, nothing else.

2. **Well-known (capability discovery)** — the homeserver's declaration at
   `https://<host>/.well-known/matrix/client`, in a block under `space.ag2.dotspace`:

   ```json
   {"m.homeserver": {"base_url": "https://s.space"},
    "space.ag2.dotspace": {
      "operator": "ag2",
      "server": "s.space",
      "gateway_url": "https://chat.s.space",
      "mcp_url": "https://mcp.s.space/mcp",
      "link_via": "s.space",
      "prefer_ipv4": true}}
   ```

   `operator` must read `ag2` for the block to parse, and it is self-asserted. `server` is
   optional (default: the host fetched) and must be that host or its parent domain, one label
   up and itself dotted (`chat.s.space` may claim `s.space`; `attacker.space` cannot claim
   `space`), and a parent claim counts only for a **registered** server (`x.co.uk` cannot
   declare `co.uk`; an unregistered host may declare itself alone); `gateway_url`, `link_via`,
   `cloud_origin` and `mcp_url` must sit at or under that server, or the whole block is refused. Every value is validated as strictly as a typed name: `link_via` is a server name (a declared port stays), the URL fields are http(s) with a server-name host, no userinfo and no backslash, and `mcp_url` is https with no query or fragment. An IP literal or a single-label name is never fetched. For a **registered** server the block fills what
   config left unset: `gateway_host`, `mcp_url`, `link_via`, `prefer_ipv4` — never
   `cloud_origin`. For an **unregistered** server it yields operator `""`, source
   `well-known-unverified`, the discovered `gateway_host` and `mcp_url`, and no AG2 behaviour
   (no IPv4 preference, no cloud, links via itself). The fetch is over TLS, follows no redirect
   (the document is attributed to the host asked, never to one it points at), is bounded
   (1.5 s), cached (1 h found, 15 min not found) and single-flight per process (a concurrent
   caller waits and reads the same answer); any failure falls through. Only an explicit
   per-server `dotspace_for(server)` fetches: the install-wide defaults read at import time by
   `cloud_auth`, `runtime-health` and `report-usage`, the bridge's DNS hook and link building
   all pass `fetch=NO_FETCH` and never touch the network. No server publishes this block yet.

3. **Token** — the gateway this process was onboarded to (the URL embedded in
   `REMOTE_TASK_TOKEN`, or `REMOTE_TASK_URL`) is a DotSpace gateway by construction: the bridge
   talks to no other host.

4. **None** — a plain Matrix server is not a DotSpace: operator `""`, source `"none"`, no cloud,
   `prefer_ipv4` false, links via itself, port included (`!r:localhost:8448` →
   `?via=localhost:8448`). A name carrying `/`, `?`, `#`, `@` or whitespace is no server at
   all: nothing is fetched for it and it never reaches a `?via=` (a room id is attacker-chosen).
   `prefer_ipv4` in config or a declaration is a real boolean or the strings `"true"` /
   `"false"`; anything else is the default (true).

## Credentials stay bound to their origin

`src/cloud_auth.py` pairs a stored Sutando Cloud token only with the origin it was stored for.
The retired production origin (`sutando.ag2.ai`), the bare pre-scoping Keychain key, and a
stored record that names no origin (a `cloud-auth.json` without `apiBase`, a metering env
without endpoint — `credential_base`) are production credentials: they normalise to and pair
with `https://sutando.ag2.space` on every install, never with the current instance's Cloud. An
s.space or dev.ag2.space install therefore never sends a production bearer to `sutando.s.space`
or `dev-sutando.ag2.space`. Only a NEW session (`normalize_base("")`, the env override unset)
takes this install's own Cloud.

## Adding a sibling

A new DotSpace is one config entry; never a release. Its well-known block then fills the
capabilities the entry leaves out. A block without an entry is discovery only (see Trust model). The ag2-sparrow package bundles `dotspace.py` verbatim (`tools/sync_from_src.py`);
there the config loader is absent, so the built-in list, well-known and the token decide.

## Not covered here

The desktop app's default homeserver URL (`agent_instances.rs`) lives in another repository, and
publishing the well-known block is a backend/ingress change on each DotSpace.
