#!/usr/bin/env python3
"""bee-actions — the TOOL half of the Bee integration (channels-vs-tools split).

The Bee CHANNEL (ag2-sparrow's sources/bee.py watcher) pushes captured events
AT the agent; this script is what the agent CALLS to act back on Bee: read the
data surfaces (todos, conversations + AI summaries, facts) and mutate todos or
facts. Verified live against a real authenticated proxy 2026-08-07: all three
GET surfaces return 200; PUT/DELETE/POST todo routes and DELETE fact route
exist (probed with nonexistent ids — no data touched).

The headline use is the CLOSED LOOP: a Bee-captured todo arrives as an ambient
task; once the agent has actually handled it (with owner approval where
privileged), `complete-todo` checks it off in the owner's Bee app.

Safety posture:
  - complete/create/edit act on the owner's behalf after the fact — low risk.
  - DELETE verbs are destructive on the owner's device data: they refuse
    without --yes, so the agent must surface the deletion for confirmation.

Config (env; CLI flags override; bearer prefers the vault):
  BEE_PROXY_URL   local authed proxy (after `bee login`), e.g. http://127.0.0.1:4470
  BEE_API_BASE    Bee cloud API base — used with BEE_API_TOKEN when set (wins
                  over the proxy; same endpoints, bearer auth)
  BEE_API_TOKEN   bearer for BEE_API_BASE (vault key of the same name preferred)

Usage:
  bee_actions.py list-todos [--limit N] [--all]
  bee_actions.py create-todo "text"
  bee_actions.py complete-todo <id>
  bee_actions.py edit-todo <id> "new text"
  bee_actions.py delete-todo <id> --yes
  bee_actions.py list-conversations [--limit N]
  bee_actions.py get-conversation <id>
  bee_actions.py list-facts [--limit N]
  bee_actions.py delete-fact <id> --yes

Output is JSON on stdout (machine-readable for the calling agent); errors exit
non-zero with a one-line reason on stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# vault_intercept lives in <repo>/src; scripts/ -> bee-actions -> skills -> repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


def _fail(msg: str, code: int = 1) -> "int":
    print(f"bee-actions: {msg}", file=sys.stderr)
    return code


def _resolve_base(args) -> "tuple[str, dict] | None":
    """(base_url, headers) — cloud bearer wins over local proxy, like the watcher."""
    api_base = (args.api_base or os.environ.get("BEE_API_BASE", "")).strip()
    token = (args.api_token or os.environ.get("BEE_API_TOKEN", "")).strip()
    if api_base and not token:
        try:  # vault is the preferred home for bearers
            from vault_intercept import get_vault_key
            token = get_vault_key("BEE_API_TOKEN")
        except Exception:
            token = ""
    if api_base and token:
        return api_base.rstrip("/"), {"Authorization": f"Bearer {token}"}
    proxy = (args.proxy_url or os.environ.get("BEE_PROXY_URL", "")).strip()
    if proxy:
        return proxy.rstrip("/"), {}
    return None


def _call(base: str, headers: dict, method: str, path: str,
          body: "dict | None" = None, query: "dict | None" = None) -> dict:
    url = base + path
    if query:
        from urllib.parse import urlencode
        url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **headers, "Content-Type": "application/json",
        "User-Agent": "sutando-bee-actions/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode() or "{}"
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proxy-url", help="override BEE_PROXY_URL")
    ap.add_argument("--api-base", help="override BEE_API_BASE")
    ap.add_argument("--api-token", help="override BEE_API_TOKEN (vault preferred)")
    sub = ap.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("list-todos"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="include completed")
    p = sub.add_parser("create-todo"); p.add_argument("text")
    p = sub.add_parser("complete-todo"); p.add_argument("id")
    p = sub.add_parser("edit-todo"); p.add_argument("id"); p.add_argument("text")
    p = sub.add_parser("delete-todo"); p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("list-conversations"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("get-conversation"); p.add_argument("id")
    p = sub.add_parser("list-facts"); p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("delete-fact"); p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    resolved = _resolve_base(args)
    if not resolved:
        return _fail("not configured: set BEE_PROXY_URL (local `bee login` proxy) "
                     "or BEE_API_BASE + BEE_API_TOKEN (cloud)", 2)
    base, headers = resolved

    # Destructive verbs refuse without explicit confirmation — the calling
    # agent must surface the deletion to the owner first.
    if args.verb in ("delete-todo", "delete-fact") and not args.yes:
        return _fail(f"{args.verb} is destructive; re-run with --yes after "
                     "the owner confirms", 3)

    try:
        if args.verb == "list-todos":
            out = _call(base, headers, "GET", "/v1/todos", query={"limit": args.limit})
            if not args.all and isinstance(out.get("todos"), list):
                out["todos"] = [t for t in out["todos"] if not t.get("completed")]
        elif args.verb == "create-todo":
            out = _call(base, headers, "POST", "/v1/todos", body={"text": args.text})
        elif args.verb == "complete-todo":
            out = _call(base, headers, "PUT", f"/v1/todos/{args.id}",
                        body={"completed": True})
        elif args.verb == "edit-todo":
            out = _call(base, headers, "PUT", f"/v1/todos/{args.id}",
                        body={"text": args.text})
        elif args.verb == "delete-todo":
            out = _call(base, headers, "DELETE", f"/v1/todos/{args.id}", body={})
        elif args.verb == "list-conversations":
            out = _call(base, headers, "GET", "/v1/conversations",
                        query={"limit": args.limit})
        elif args.verb == "get-conversation":
            out = _call(base, headers, "GET", f"/v1/conversations/{args.id}")
        elif args.verb == "list-facts":
            out = _call(base, headers, "GET", "/v1/facts", query={"limit": args.limit})
        elif args.verb == "delete-fact":
            out = _call(base, headers, "DELETE", f"/v1/facts/{args.id}", body={})
        else:  # pragma: no cover — argparse enforces the verb set
            return _fail(f"unknown verb {args.verb}")
    except urllib.error.HTTPError as e:
        return _fail(f"{args.verb} -> HTTP {e.code}: {e.read(200).decode(errors='replace')}")
    except (urllib.error.URLError, OSError) as e:
        return _fail(f"{args.verb} -> {e}")

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
