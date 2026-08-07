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

Config — CLI > env > manifest.json `config` block (per skills/MANIFEST.md;
empty strings are unset at every tier); bearer prefers the vault:
  BEE_PROXY_URL   local authed proxy (after `bee login`), e.g. http://127.0.0.1:4470
  BEE_API_BASE    Bee cloud API base — used with BEE_API_TOKEN when set (wins
                  over the proxy; same endpoints, bearer auth)
  BEE_API_TOKEN   bearer for BEE_API_BASE (vault key of the same name preferred)

Usage:
  bee_actions.py list-todos [--limit N] [--all]
  bee_actions.py create-todo "text"
  bee_actions.py complete-todo <id> [--how "how it was done"]
  bee_actions.py edit-todo <id> "new text"
  bee_actions.py delete-todo <id> --yes
  bee_actions.py list-conversations [--limit N]
  bee_actions.py get-conversation <id>
  bee_actions.py list-facts [--limit N]
  bee_actions.py delete-fact <id> --yes
  bee_actions.py register-room [--room !id:server] [--invite @owner:server]
  bee_actions.py post-room "message" [--room !id:server]

`complete-todo --how` also appends " — done: <how>" to the todo text (Bee has
no notes field, so the text IS the record) — visible in the owner's Bee app.

`post-room` is the ag2space half of the loop: Bee has no inbound reply surface,
so Sutando's replies about Bee items go to a registered Bee room on ag2space.
Room resolution: --room > BEE_ROOM_ID env > manifest > <workspace>/state/bee-room.json;
if none is registered and an owner is known (--invite / BEE_ROOM_OWNER), the
room is auto-registered first: a private room with just the owner invited —
effectively a DM thread — created via gateway op:create and persisted, so the
user never has to register by hand. `register-room --room` records a room the
user picked instead. Gateway creds via the shared contract (GATEWAY_TOKEN >
RELAY_TOKEN > REMOTE_TASK_TOKEN > AG2_REMOTE_TOKEN; combined url|secret ok).

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
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "shared"))

_MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"


def _manifest_config() -> dict:
    """The skill's manifest `config` block; {} when absent/unreadable."""
    try:
        cfg = json.loads(_MANIFEST.read_text()).get("config", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _cfg(args, attr: str, key: str) -> str:
    """One setting under the skills/MANIFEST.md precedence the SKILL.md
    documents: CLI > env > manifest (state stays each caller's last resort).
    Empty strings are unset at every tier."""
    for val in (getattr(args, attr, None), os.environ.get(key),
                _manifest_config().get(key)):
        if val and str(val).strip():
            return str(val).strip()
    return ""


def _gateway_creds() -> "tuple[str, dict] | None":
    """(base_url, headers) for the ag2space gateway, or None if unconfigured."""
    from ag2_gateway_credentials import normalize_credentials, resolve_alias_precedence
    raw, _ = resolve_alias_precedence(
        os.environ, ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN",
                     "AG2_REMOTE_TOKEN"))
    url, _ = resolve_alias_precedence(
        os.environ, ("GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL"))
    creds = normalize_credentials(raw, explicit_url=url,
                                  source="resolved" if raw else "none")
    if not (creds.base_url and creds.token):
        return None
    return creds.base_url, {"Authorization": f"Bearer {creds.token}",
                            "User-Agent": "sutando-bee-actions/1.0"}


def _room_state_path() -> Path:
    from workspace_default import resolve_workspace
    return Path(resolve_workspace()) / "state" / "bee-room.json"


def _bee_room(args) -> str:
    """--room > BEE_ROOM_ID env > manifest > workspace state file. '' if unresolved."""
    room = _cfg(args, "room", "BEE_ROOM_ID")
    if room:
        return room
    try:
        return str(json.loads(_room_state_path().read_text())
                   .get("room_id", "")).strip()
    except Exception:
        return ""


def _persist_room(room_id: str) -> None:
    p = _room_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"room_id": room_id}) + "\n")


def _register_room(gbase: str, gheaders: dict, invite: str) -> str:
    """Create the Bee room (private, owner invited — a DM thread in practice),
    persist its id, and return it. Raises HTTPError on gateway failure."""
    payload = {"op": "create", "name": "Sutando · Bee"}
    if invite:
        payload["invite"] = [invite]
    out = _call(gbase, gheaders, "POST", "/v1/room", body=payload)
    room_id = str(out.get("room_id") or "").strip()
    if not room_id:
        raise RuntimeError(f"gateway create returned no room_id: {out}")
    _persist_room(room_id)
    return room_id


def _fail(msg: str, code: int = 1) -> "int":
    print(f"bee-actions: {msg}", file=sys.stderr)
    return code


def _resolve_base(args) -> "tuple[str, dict] | None":
    """(base_url, headers) — cloud bearer wins over local proxy, like the watcher."""
    api_base = _cfg(args, "api_base", "BEE_API_BASE")
    token = _cfg(args, "api_token", "BEE_API_TOKEN")
    if api_base and not token:
        try:  # vault is the preferred home for bearers
            from vault_intercept import get_vault_key
            token = get_vault_key("BEE_API_TOKEN")
        except Exception:
            token = ""
    if api_base and token:
        return api_base.rstrip("/"), {"Authorization": f"Bearer {token}"}
    proxy = _cfg(args, "proxy_url", "BEE_PROXY_URL")
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
    # charset is load-bearing: the Bee proxy's parser 415s on bare
    # "application/json" (verified live 2026-08-06).
    req = urllib.request.Request(url, data=data, method=method, headers={
        **headers, "Content-Type": "application/json; charset=utf-8",
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
    p.add_argument("--how", help="append ' — done: <how>' to the todo text")
    p = sub.add_parser("edit-todo"); p.add_argument("id"); p.add_argument("text")
    p = sub.add_parser("delete-todo"); p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("list-conversations"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("get-conversation"); p.add_argument("id")
    p = sub.add_parser("list-facts"); p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("delete-fact"); p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("post-room"); p.add_argument("text")
    p.add_argument("--room", help="override BEE_ROOM_ID / state file")
    p.add_argument("--invite", help="owner mxid for auto-registration")
    p = sub.add_parser("register-room")
    p.add_argument("--room", help="record this existing room instead of creating")
    p.add_argument("--invite", help="owner mxid to invite on create")
    args = ap.parse_args()

    # Room verbs talk to the ag2space gateway, not the Bee API — resolve and
    # return before the Bee-base gate so they work with no Bee config at all.
    if args.verb in ("post-room", "register-room"):
        gw = _gateway_creds()
        if not gw:
            return _fail("no gateway configured: set GATEWAY_TOKEN / "
                         "REMOTE_TASK_TOKEN (combined url|secret ok)", 2)
        gbase, gheaders = gw
        invite = _cfg(args, "invite", "BEE_ROOM_OWNER")
        try:
            if args.verb == "register-room":
                if args.room:  # user picked a room — record it, create nothing
                    _persist_room(args.room.strip())
                    print(json.dumps({"room_id": args.room.strip(),
                                      "registered": "existing"}))
                    return 0
                if not invite:
                    return _fail(
                        "refusing to create a room with no owner to invite: "
                        "pass --invite or set BEE_ROOM_OWNER (an uninvited "
                        "room would be invisible to you)", 2)
                room = _register_room(gbase, gheaders, invite)
                print(json.dumps({"room_id": room, "registered": "created"}))
                return 0
            room = _bee_room(args)
            if not room:
                if not invite:
                    return _fail(
                        "no Bee room registered: run register-room, pass --room,"
                        " or set BEE_ROOM_OWNER for auto-registration", 2)
                room = _register_room(gbase, gheaders, invite)
            out = _call(gbase, gheaders, "POST", "/v1/room",
                        body={"op": "message", "room_id": room, "body": args.text})
        except urllib.error.HTTPError as e:
            return _fail(f"{args.verb} -> HTTP {e.code}: "
                         f"{e.read(200).decode(errors='replace')}")
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            return _fail(f"{args.verb} -> {e}")
        print(json.dumps({"room_id": room, **out}, ensure_ascii=False))
        return 0

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
            if args.how:
                # Bee todos have no notes field — the text IS the record.
                cur = _call(base, headers, "GET", f"/v1/todos/{args.id}")
                text = str(cur.get("text") or "").rstrip()
                if text:
                    out = _call(base, headers, "PUT", f"/v1/todos/{args.id}",
                                body={"text": f"{text} — done: {args.how}"})
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
