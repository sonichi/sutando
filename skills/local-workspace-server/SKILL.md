---
name: local-workspace-server
description: Hardened loopback static server for local presentation drafts — capability-URL, read-only GET/HEAD, Host-header gate, nosniff + sandbox CSP. Use instead of a plain http.server when the Presentation panel's dev mode loads a local deck.
---

# Local workspace server

Serves ONE directory, read-only, to the trusted Presentation panel's dev mode.
Loopback binding is not treated as trust: the owner's hardening checklist
(2026-08-26) is the spec, and every guard is pinned in
`tests/local-workspace-server.test.py` over real HTTP.

```bash
python3 skills/local-workspace-server/scripts/serve.py --root <deck-dir> [--port 8899] [--ttl 3600]
```

Prints a capability URL (`http://127.0.0.1:<port>/<token>/`). Paste it into the
panel's device-local dev override — it never goes into room state (logical
`local_workspace` descriptors only; see the Presentation protocol thread).

Guards: 127.0.0.1 bind only · random capability path segment (constant-time
compare) · TTL expiry → 403 · GET/HEAD only, anything else 405 · no directory
listing · traversal-safe resolve containment · explicit MIME + nosniff ·
`sandbox allow-scripts` CSP on HTML · Host header must be loopback (DNS-
rebinding defense, computed from the BOUND port) · no-store caching · stdout
logging only.
