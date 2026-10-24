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

## Sources, in order

1. **Config** — `core.dotspace.instances` in `sutando.config.json` /
   `sutando.config.local.json`:

   ```json
   {"core": {"dotspace": {"instances": [
     {"server": "ag2.space", "gateway_host": "chat.ag2.space",
      "cloud_origin": "https://sutando.ag2.space"},
     {"server": "s.space", "gateway_host": "chat.s.space",
      "cloud_origin": "https://sutando.s.space"}
   ]}}}
   ```

   Optional per entry: `link_via` (default: server), `prefer_ipv4` (default: true). When the
   section is absent the built-in list applies (ag2.space, dev.ag2.space, s.space, dev.s.space
   with their `chat.` / `dev-chat.` gateways and `sutando.` / `dev-sutando.` clouds), so
   today's installs behave as before. The most specific server wins (`chat.dev.s.space` is
   dev.s.space's gateway). The owner's config outranks a remote declaration, and a configured
   instance costs no network round trip. Sutando Cloud bearers are sent only to configured
   instances' cloud hosts (plus the retired `sutando.ag2.ai`); a well-known declaration never
   widens that set.

2. **Well-known** — the homeserver's own declaration at
   `https://<host>/.well-known/matrix/client`, in a block under `space.ag2.dotspace`:

   ```json
   {"m.homeserver": {"base_url": "https://s.space"},
    "space.ag2.dotspace": {
      "operator": "ag2",
      "server": "s.space",
      "gateway_url": "https://chat.s.space",
      "cloud_origin": "https://sutando.s.space",
      "mcp_url": "https://mcp.s.space/mcp",
      "link_via": "s.space",
      "prefer_ipv4": true}}
   ```

   `operator` must be `ag2`. `server` is optional (default: the host fetched) and must be that
   host or a parent domain of it; a gateway host serves the same document with `server` naming
   its homeserver. The fetch is bounded (1.5 s), cached (1 h found, 15 min not found), one in
   flight per process, and any failure falls through to the next source. Hot paths (the
   bridge's DNS hook, link building) pass `fetch=NO_FETCH` and never touch the network. No
   server publishes this block yet.

3. **Token** — the gateway this process was onboarded to (the URL embedded in
   `REMOTE_TASK_TOKEN`, or `REMOTE_TASK_URL`) is a DotSpace gateway by construction: the bridge
   talks to no other host.

4. **None** — a plain Matrix server is not a DotSpace: operator `""`, source `"none"`, no cloud,
   `prefer_ipv4` false, links via itself.

## Adding a sibling

A new DotSpace is one config entry, or a well-known block on the new homeserver; never a
release. The ag2-sparrow package bundles `dotspace.py` verbatim (`tools/sync_from_src.py`);
there the config loader is absent, so the built-in list, well-known and the token decide.

## Not covered here

The desktop app's default homeserver URL (`agent_instances.rs`) lives in another repository, and
publishing the well-known block is a backend/ingress change on each DotSpace.
