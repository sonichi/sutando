#!/usr/bin/env python3
"""connections — manage the install's gateway connections (src/connections.py).

  list [--json]                rows joined with bridge status and state; no secrets
  add TOKEN [--yes --expected-server NAME] [--added-by owner] [--instance SLUG]
                               TOKEN is 'https://<gateway>|<secret>', or '-' to read it from stdin.
                               Without --yes: print the preview and ask before saving.
  set-primary INSTANCE         move proactive/owner-DM defaults to this connection; refused on a
                               disconnected row (disabled_cannot_be_primary); a running bridge
                               takes the room at its next reconnect ("reconnect it" is printed)
  promote INSTANCE --repo DIR --logs-dir DIR
                               make this connection primary while the old primary's bridge runs (the
                               legacy one included): start this bridge, stop the old one, park its row.
                               Refused by code: disabled_cannot_be_primary, legacy_bridge_not_running,
                               new_primary_not_started, bridge_still_running (rolled back)
  unpark INSTANCE --repo DIR --logs-dir DIR
                               clear a parked row and start its bridge ('' = the legacy row, which its
                               launchers resume)
  legacy-parked                exit 0 when a primary move parked the legacy row, else 1
  remove INSTANCE [--force]    drop the row and its vault key; the primary needs --force
  disconnect INSTANCE          stop that bridge, then keep the row, token and launch record out of
                               the plan until reconnect; refused on the primary, on the legacy
                               row (legacy_row_unmanaged) and while the bridge will not stop
                               (bridge_still_running: nothing changed)
  reconnect INSTANCE --repo DIR --logs-dir DIR   stop and relaunch that one bridge (re-enables it)
  update-token INSTANCE - --repo DIR --logs-dir DIR
                               replace the row's secret with a new code for the same server and
                               agent, re-enable and relaunch. '-' reads the code from stdin, which
                               keeps it out of the process list; a literal code in its place works
                               for scripts. Refused by code: token_source_shadowed (the row's
                               AG2_REMOTE_TOKEN_<INST> is set in the environment: unset it first),
                               token_server_mismatch, token_agent_mismatch, token_unverified (the
                               gateway did not confirm the row's agent), token_rejected,
                               legacy_row_unmanaged, bridge_still_running
  known-servers [--json]       the resolver's DotSpace instances, each with its row if one exists
  launch INSTANCE --repo DIR --logs-dir DIR [--env-only]
                               start one bridge from the launch plan (--env-only: from the
                               AG2_REMOTE_TOKEN_<INST> variable alone, when the record is unreadable)
  start INSTANCE --repo DIR --logs-dir DIR
                               launch unless a bridge already holds the lease; exit 1 naming the
                               reason when it did not start
  import-env                   rows once for REMOTE_TASK_TOKEN and each AG2_REMOTE_TOKEN_<INST>
  launch-plan                  one 'instance<TAB>token_ref<TAB>channel_dir<TAB>primary' line per bridge
  primary                      the primary row's instance ('' = the legacy bridge); exit 2 when none

Each verb parses, calls one unit and prints; policy lives in the module.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))

import connections  # noqa: E402


def _table(rows: list) -> str:
    head = ("INSTANCE", "SERVER", "PRIMARY", "STATE", "VERIFIED", "ADDED_BY", "LAST_OK", "TOKEN_REF")
    body = []
    for r in rows:
        st = r.get("status") or {}
        state = r.get("state", "?") + (f" ({r['reason']})" if r.get("reason") else "")
        body.append((r["instance"] or "(legacy)", r["server"] or "?", "yes" if r["primary"] else "no", state,
                     "yes" if r.get("verified") else "no", r["added_by"], str(st.get("last_ok_ts") or "-"),
                     r["token_ref"]))
    widths = [max(len(str(line[i])) for line in [head, *body]) for i in range(len(head))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    return "\n".join(fmt.format(*line).rstrip() for line in [head, *body])


def _preview_text(p: dict) -> str:
    identity = p["agent_mxid"] or "(the gateway did not name one agent)"
    auth = {True: "accepted", False: "REJECTED", None: "not checked"}[p["authenticated"]]
    lines = [f"server:    {p['server']}" + ("" if p["verified"] else "  (unverified: from the gateway host)"),
             f"agent:     {identity}", f"gateway:   {p['gateway_url']}", f"token:     {auth}"]
    if p.get("error"):
        lines.append(f"error:     {p['error']}")
    return "\n".join(lines)


def cmd_list(args) -> int:
    rows = [connections.status_for(r, args.workspace) for r in connections.list_connections(args.workspace)]
    print(json.dumps(rows, indent=2, sort_keys=True) if args.json else (_table(rows) if rows else "no connections"))
    return 0


def cmd_add(args) -> int:
    token = sys.stdin.read().strip() if args.token == "-" else args.token
    preview = connections.preview_connection(token)
    if args.yes:
        if not args.expected_server:
            print("connections: --yes needs --expected-server <name from the preview>", file=sys.stderr)
            return 2
        expected = args.expected_server
    else:
        print(_preview_text(preview))
        answer = input(f"Save connection to {preview['server']}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("not saved")
            return 1
        expected = preview["server"]
    row, created = connections.add_connection(token, added_by=args.added_by, expected_server=expected,
                                              instance=args.instance, preview=preview, workspace=args.workspace)
    verb = "added" if created else "already present:"
    print(f"{verb} {row['instance']} for {row['server']} (token in vault as {row['token_ref']})"
          + (" — primary" if row["primary"] else ""))
    return 0


def cmd_set_primary(args) -> int:
    row = connections.set_primary(args.instance, workspace=args.workspace)
    hint = "; its running bridge carries the proactive room after you reconnect it" if row.get("relaunch_needed") else ""
    print(f"primary is now {row['instance'] or '(legacy)'} ({row['server']}){hint}")
    return 0


def cmd_promote(args) -> int:
    row = connections.promote(args.instance, repo=args.repo, logs_dir=args.logs_dir, workspace=args.workspace)
    if not row["changed"]:
        print(f"{row['instance'] or '(legacy)'} is already primary")
        return 0
    parked = row["previous"]
    tail = "" if parked is None else f"; parked {parked or '(legacy)'}" + (
        f" (stopped pid {row['stopped_pid']})" if row.get("stopped_pid") else "")
    if row.get("legacy_stopped_pid"):
        tail += f"; parked the rowless legacy bridge (stopped pid {row['legacy_stopped_pid']})"
    if row.get("relaunch_needed"):
        tail += "; its bridge runs without the proactive room: restart it"
    print(f"primary is now {row['instance'] or '(legacy)'} ({row['server']}){tail}")
    return 0


def cmd_unpark(args) -> int:
    row = connections.unpark(args.instance, repo=args.repo, logs_dir=args.logs_dir, workspace=args.workspace)
    launch = row.get("launch")
    if launch is None:
        tail = "its launchers resume it" if not row["instance"] else "not started"
    elif launch.get("ok"):
        tail = f"bridge pid {launch['pid']}"
    else:
        tail = f"start failed: {launch.get('reason') or 'launch_failed'}"
    print(f"unparked {row['instance'] or '(legacy)'} ({tail})")
    return 0 if launch is None or launch.get("ok") else 1


def cmd_legacy_parked(args) -> int:
    return 0 if connections.legacy_parked(args.workspace) else 1


def cmd_remove(args) -> int:
    row = connections.remove_connection(args.instance, force=args.force, workspace=args.workspace)
    print(f"removed {row['instance'] or '(legacy)'} ({row['token_ref']} deleted from the vault)")
    return 0


def cmd_disconnect(args) -> int:
    row = connections.disconnect(args.instance, workspace=args.workspace)
    stopped = f"stopped pid {row['stopped_pid']}; " if row.get("stopped_pid") else ""
    print(f"disconnected {args.instance} ({stopped}token kept in vault as {row['token_ref']}; "
          "reconnect to resume)")
    return 0


def cmd_known_servers(args) -> int:
    servers = connections.known_servers(args.workspace)
    if args.json:
        print(json.dumps(servers, indent=2, sort_keys=True))
        return 0
    head = ("SERVER", "CONNECTED", "INSTANCE", "PRIMARY", "DISABLED")
    body = [(s["server"], "yes" if s["connected"] else "no", s["instance"] or "-",
             "yes" if s["primary"] else "no", "yes" if s["disabled"] else "no") for s in servers]
    widths = [max(len(str(line[i])) for line in [head, *body]) for i in range(len(head))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print("\n".join(fmt.format(*line).rstrip() for line in [head, *body]) if servers else "no known servers")
    return 0


def cmd_launch(args) -> int:
    record = connections.launch(args.instance, repo=args.repo, logs_dir=args.logs_dir, workspace=args.workspace,
                                env_only=args.env_only)
    print(f"launched {args.instance} (pid {record['pid']})")
    return 0


def cmd_start(args) -> int:
    record = connections.start(args.instance, repo=args.repo, logs_dir=args.logs_dir, workspace=args.workspace)
    if not record.get("ok"):
        print(f"connections: {args.instance} did not start: {record.get('reason') or 'launch_failed'}", file=sys.stderr)
        return 1
    print(f"{'already running' if record.get('lease') == 'already_running' else 'started'} {args.instance} (pid {record['pid']})")
    return 0


def cmd_reconnect(args) -> int:
    record = connections.reconnect(args.instance, repo=args.repo, logs_dir=args.logs_dir, workspace=args.workspace)
    stopped = f"stopped pid {record['stopped_pid']}, " if record.get("stopped_pid") else ""
    print(f"reconnected {args.instance} ({stopped}pid {record['pid']})")
    return 0


def cmd_update_token(args) -> int:
    token = sys.stdin.read().strip() if args.token == "-" else args.token
    row = connections.update_token(args.instance, token, repo=args.repo, logs_dir=args.logs_dir,
                                   workspace=args.workspace)
    launched = row.get("launch") or {}
    relaunch = (f"relaunched, pid {launched['pid']}" if launched.get("ok") else
                f"relaunch failed: {launched.get('reason') or 'launch_failed'}")
    print(f"updated token of {args.instance} ({row['server']}; vault key {row['token_ref']} replaced; {relaunch})")
    return 0


def cmd_import_env(args) -> int:
    created = connections.import_from_env(os.environ, workspace=args.workspace)
    for row in created:
        print(f"imported {row['instance'] or '(legacy)'} from {row['token_ref']}")
        if row.get("relaunch_needed"):
            print(f"{row['instance']} is now primary; its running bridge has no proactive room until it restarts")
    for ref in connections.removed_env_refs(args.workspace):
        if os.environ.get(ref, "").strip():
            print(f"{ref}: connection removed earlier; unset the variable")
    if not created:
        print("nothing to import")
    return 0


def cmd_primary(args) -> int:
    """Print the primary row's instance ('' for the legacy bridge); exit 2 when none is primary."""
    inst = connections.primary_instance(args.workspace)
    if inst is None:
        return 2
    print(inst)
    return 0


def cmd_launch_plan(args) -> int:
    for e in connections.launch_plan(os.environ, workspace=args.workspace):
        print(f"{e['instance']}\t{e['token_ref']}\t{e['channel_dir']}\t{1 if e['primary'] else 0}")
    return 0


def _launch_args(s) -> None:
    s.add_argument("instance")
    s.add_argument("--repo", type=Path, required=True)
    s.add_argument("--logs-dir", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="connections", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", type=Path, default=None, help="override the resolved workspace")
    sub = p.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("list"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("add")
    s.add_argument("token")
    s.add_argument("--yes", action="store_true", help="save without asking; needs --expected-server")
    s.add_argument("--expected-server", default=None)
    s.add_argument("--added-by", default="owner", choices=connections.ADDED_BY)
    s.add_argument("--instance", default=None)
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("set-primary"); s.add_argument("instance"); s.set_defaults(fn=cmd_set_primary)
    s = sub.add_parser("promote"); _launch_args(s); s.set_defaults(fn=cmd_promote)
    s = sub.add_parser("unpark"); _launch_args(s); s.set_defaults(fn=cmd_unpark)
    sub.add_parser("legacy-parked").set_defaults(fn=cmd_legacy_parked)
    s = sub.add_parser("remove"); s.add_argument("instance"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("disconnect"); s.add_argument("instance"); s.set_defaults(fn=cmd_disconnect)
    s = sub.add_parser("known-servers"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_known_servers)
    s = sub.add_parser("launch"); _launch_args(s); s.add_argument("--env-only", action="store_true")
    s.set_defaults(fn=cmd_launch)
    s = sub.add_parser("start"); _launch_args(s); s.set_defaults(fn=cmd_start)
    s = sub.add_parser("reconnect"); _launch_args(s); s.set_defaults(fn=cmd_reconnect)
    s = sub.add_parser("update-token"); s.add_argument("instance")
    s.add_argument("token", metavar="TOKEN|-",
                   help="'-' reads the new code from stdin (keeps it out of the process list); "
                        "a literal 'https://<gateway>|<secret>' works for scripts")
    s.add_argument("--repo", type=Path, required=True); s.add_argument("--logs-dir", type=Path, required=True)
    s.set_defaults(fn=cmd_update_token)
    sub.add_parser("import-env").set_defaults(fn=cmd_import_env)
    sub.add_parser("launch-plan").set_defaults(fn=cmd_launch_plan)
    sub.add_parser("primary").set_defaults(fn=cmd_primary)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except connections.ConnectionsError as e:
        print(f"connections: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
