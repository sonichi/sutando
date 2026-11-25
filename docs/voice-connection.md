# Browser voice connection and bearer handling

The dashboard obtains the local voice bearer through `POST /voice-ws-token`
before the owner connects. The page caches the response so its click handler can
call the WebSocket transport synchronously within the browser's user gesture.
If the cache is empty, it fetches a token and connects asynchronously as a
fallback. After an authentication refusal, it refreshes the cached token.

The token route is for a direct local browser only. It requires a loopback
socket peer, a loopback `Host`, and a same-origin loopback `Origin`; requests
with forwarding headers are rejected. The route does not set permissive CORS
headers and its response is `no-store`.

The browser sends the bearer only to the local voice WebSocket. The WebSocket
server validates the owner token before attaching a session. A token is never
derived from a client-supplied WebSocket authorization header.

Native local clients that send no `Origin` and no bearer — the desktop app —
are admitted only when the socket peer is loopback; any presented credential
that fails to match stays refused. A `?probe=1` connection always passes that
same Origin check first, then receives a single agent-state frame and closes.

When `SUTANDO_API_TOKEN` is configured, that is the bearer. Otherwise the
per-install token is stored in `<workspace>/state/agent-api.token` with mode
`0600`. First-use publication uses a temporary file and a hard link so only one
concurrent writer creates the final token; losing writers read the published
file.
