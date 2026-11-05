#!/usr/bin/env python3
"""A pool worker's own agent identity: give one, show it, rotate it, take it away.

A worker can run as an agent of its own instead of through the core's. That agent is a
connection row naming the worker (src/connections.py, "a pool worker's own identity"): a lane
of its own that answers only what is addressed to that agent, hands every task it queues to the
worker, and posts nothing proactively. Retiring the worker (pool_manage retire) takes it away.

    python3 pool_identity.py [--workspace WS] add <worker> --repo REPO --logs-dir DIR [TOKEN|-] [--yes --expected-server NAME]
    python3 pool_identity.py [--workspace WS] show <worker>
    python3 pool_identity.py [--workspace WS] rotate <worker> --repo REPO --logs-dir DIR [TOKEN|-]
    python3 pool_identity.py [--workspace WS] remove <worker>

TOKEN is the agent's access token from its settings page in AG2 Space. Leave it out to type it
at a hidden prompt, or pass '-' to read it from stdin; it is stored in the secret vault, never in
a file, and never printed. `add` shows what the token's gateway says before anything is saved.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import connections
import pool_roster as pr
from workspace_default import resolve_workspace


def live_worker(workspace: Path, name: str) -> str:
    """The roster id `name` (an id or a label) names; SystemExit when no live worker answers to it."""
    roster = pr.load_roster(workspace) or {}
    wid = pr.resolve_label(roster, name)
    row = (roster.get("workers") or {}).get(wid)
    if wid == pr.CORE or not isinstance(row, dict) or row.get("state") == "retired":
        raise SystemExit(f"pool_identity: no live pool worker {name!r}")
    return wid


def read_token(arg, prompt: str) -> str:
    if arg == "-":
        return sys.stdin.read().strip()
    if arg:
        return arg.strip()
    import getpass
    return getpass.getpass(prompt).strip()


def preview_text(p: dict) -> str:
    return "\n".join([f"server:  {p.get('server')}" + ("" if p.get("verified") else "  (unverified)"),
                      f"agent:   {p.get('agent_mxid') or '(the gateway did not name one agent)'}",
                      f"owner:   {p.get('owner_mxid') or '(the gateway named no owner)'}",
                      f"token:   {({True: 'accepted', False: 'REJECTED', None: 'not checked'})[p.get('authenticated')]}"]
                     + ([f"error:   {p['error']}"] if p.get("error") else []))


def cmd_add(args, ws: Path) -> int:
    wid = live_worker(ws, args.worker)
    token = read_token(args.token, f"Access token for {args.worker}'s own agent (hidden): ")
    preview = connections.preview_connection(token)
    if args.yes:
        if not args.expected_server:
            print("pool_identity: --yes needs --expected-server <the server the preview names>", file=sys.stderr)
            return 2
        expected = args.expected_server
    else:
        print(preview_text(preview))
        if input(f"Save this agent as {args.worker}'s own identity? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not saved")
            return 1
        expected = str(preview.get("server") or "")
    row, created = connections.add_worker_identity(wid, token, expected_server=expected, preview=preview,
                                                   workspace=ws)
    print(f"{'added' if created else 'already present:'} {row['instance']}: worker {wid} runs as "
          f"{row.get('agent_mxid') or '?'} (token in the vault as {row['token_ref']})")
    launch = connections.start_added(row, repo=args.repo, logs_dir=args.logs_dir, workspace=ws)
    if launch is not None and not launch.get("ok"):
        print(f"lane {row['instance']} not started: {launch.get('reason')}", file=sys.stderr)
        return 1
    print(f"lane {row['instance']} running")
    return 0


def cmd_show(args, ws: Path) -> int:
    row = connections.worker_identity(live_worker(ws, args.worker), ws)
    if row is None:
        print(f"{args.worker} has no agent of its own; it answers through the core's")
        return 1
    keep = ("instance", "server", "agent_mxid", "agent_display_name", "token_ref", "added_by", "added_at", "disabled")
    print(json.dumps({k: row.get(k) for k in keep}, indent=2, sort_keys=True))
    return 0


def cmd_rotate(args, ws: Path) -> int:
    row = connections.worker_identity(live_worker(ws, args.worker), ws)
    if row is None:
        print(f"pool_identity: {args.worker} has no agent of its own to rotate", file=sys.stderr)
        return 1
    token = read_token(args.token, f"New access token for {args.worker}'s own agent (hidden): ")
    out = connections.update_token(str(row["instance"]), token, repo=args.repo, logs_dir=args.logs_dir, workspace=ws)
    print(f"rotated {out['instance']}; lane relaunched")
    return 0


def cmd_remove(args, ws: Path) -> int:
    roster = pr.load_roster(ws) or {}
    wid = pr.resolve_label(roster, args.worker)
    row = connections.retire_worker_identity(wid, workspace=ws)
    if row is None:
        print(f"{args.worker} has no agent of its own")
        return 0
    print(f"removed {row['instance']} ({row.get('agent_mxid') or '?'}): lane stopped, row and vault secret gone; "
          "the agent stays on its server until the owner deletes it there")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pool_identity", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", type=Path, default=None)
    sub = p.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("add"); s.add_argument("worker"); s.add_argument("token", nargs="?", default=None)
    s.add_argument("--repo", type=Path, required=True); s.add_argument("--logs-dir", type=Path, required=True)
    s.add_argument("--yes", action="store_true"); s.add_argument("--expected-server", default=None)
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("show"); s.add_argument("worker"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("rotate"); s.add_argument("worker"); s.add_argument("token", nargs="?", default=None)
    s.add_argument("--repo", type=Path, required=True); s.add_argument("--logs-dir", type=Path, required=True)
    s.set_defaults(fn=cmd_rotate)
    s = sub.add_parser("remove"); s.add_argument("worker"); s.set_defaults(fn=cmd_remove)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ws = args.workspace if args.workspace is not None else resolve_workspace()
    try:
        return args.fn(args, Path(ws))
    except pr.AmbiguousWorkerName as e:
        print(f"pool_identity: {e}", file=sys.stderr)
        return 2
    except connections.Refusal as e:
        print(f"pool_identity: refused ({e.CODE}): {e}", file=sys.stderr)
        return 1
    except connections.ConnectionsError as e:
        print(f"pool_identity: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
