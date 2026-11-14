#!/usr/bin/env python3
"""A pool worker's own agent identity: give one, show it, rotate it, take it away.

A worker can run as an agent of its own instead of through the core's. That agent is a
connection row naming the worker (src/connections.py, "a pool worker's own identity"): a lane
of its own that answers only what is addressed to that agent, hands every task it queues to the
worker, and posts nothing proactively. Retiring the worker (pool_manage retire) takes it away.

A worker holds at most one agent per server, so it can have one on each homeserver the core is
connected to: `--device-flow --lane INSTANCE` adds it on that core lane's server. `show` lists them
all; `rotate` and `remove` act on the one on `--server`, which may be left out while there is one.

    python3 pool_identity.py [--workspace WS] add <worker> --repo REPO --logs-dir DIR [TOKEN|-] [--yes --expected-server NAME]
    python3 pool_identity.py [--workspace WS] add <worker> --device-flow --label LABEL [--display-name TEXT] [--lane INSTANCE]
    python3 pool_identity.py [--workspace WS] show <worker> [--server SERVER]
    python3 pool_identity.py [--workspace WS] rotate <worker> --repo REPO --logs-dir DIR [TOKEN|-] [--server SERVER]
    python3 pool_identity.py [--workspace WS] rotate <worker> --device-flow --label LABEL [--lane INSTANCE]
    python3 pool_identity.py [--workspace WS] remove <worker> [--server SERVER]
    python3 pool_identity.py [--workspace WS] provision-begin <worker> [--role ROLE] [--lane INSTANCE]
    python3 pool_identity.py [--workspace WS] provision-wait <worker> --operation-id ID --name NAME
                                              [--display-name TEXT] [--role TEXT] [--timeout SECONDS] [--lane INSTANCE]
    python3 pool_identity.py [--workspace WS] provision-finish <worker> --claim-id ID --agent-id MXID [--lane INSTANCE]
    python3 pool_identity.py [--workspace WS] provision-abort <worker>
    python3 pool_identity.py [--workspace WS] retire-remote <worker> [--local-anyway]
    python3 pool_identity.py [--workspace WS] card-reply --task-file PATH

TOKEN is the agent's access token from its settings page in AG2 Space. Leave it out to type it
at a hidden prompt, or pass '-' to read it from stdin; it is stored in the secret vault, never in
a file, and never printed. `add` shows what the token's gateway says before anything is saved.

`--device-flow` needs no pasted token either (pool_device_connect.py): the core starts s.space's
device-connect flow for agent LABEL, sends the owner the approval link in their DM, waits until they
approve it in a browser (or the link expires, which it reports), checks the agent and its owner, and
saves the token in the vault. It prints one JSON line with the agent id and a status. The owner sees an
approval card (the text note when no card can be posted); once the link expires, the card's "Get a new
link" button replies "Get a new link for <worker>", and `card-reply` on that task starts the same flow again
in the background, for the owner only; a click that matches no card this agent posted prints
`forged_or_stale_click` with the one line to say, and nothing acts on it.

The `provision-*` verbs need no pasted token (pool_provision.py). `provision-begin` prints the
SHA-256 the `agent.create` Action takes and a suggested name. `provision-wait` makes that call as
the core's own agent, asks again until the owner has approved it, and redeems the claim at once;
`provision-finish` redeems a claim an earlier call already returned, for the agent id it returned.
Both print the agent id and a status. `retire-remote` removes the identity once its server rejects
the token. Each prints one JSON line and never a secret.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import agent_card
import connections
import pool_device_connect
import pool_provision
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


def reseat_after_add(ws: Path, repo, wid: str) -> dict:
    """A seat started before the worker had its own agent keeps acting as the core's until it restarts."""
    import pool_manage
    try:
        out = pool_manage.reseat(ws, repo or _REPO, wid)
    except Exception as e:  # noqa: BLE001 — the identity is saved; the restart is reported, never raised
        out = {"result": "failed", "why": f"{type(e).__name__}: {e}"}
    if out.get("result") not in ("reseated", "skipped"):
        print(f"worker {wid}'s seat still acts as the core's agent: {out.get('why') or out.get('result')}; "
              f"restart it with: pool_manage.py reseat {wid}", file=sys.stderr)
    return out


def cmd_device_flow(args, ws: Path, *, rotate: bool) -> int:
    """The device flow; a resend attempt (--resend-id) that ends without a new card says why and frees its button."""
    resend_id = getattr(args, "resend_id", None) or ""
    try:
        rc, why = _device_flow(args, ws, rotate=rotate)
    except Exception as e:
        pool_device_connect.resend_failed(ws, args.worker, resend_id, f"it stopped ({type(e).__name__})")
        raise
    if rc != 0:
        pool_device_connect.resend_failed(ws, args.worker, resend_id, why or "the approval flow did not start")
    return rc


def _device_flow(args, ws: Path, *, rotate: bool) -> tuple:
    wid = live_worker(ws, args.worker)
    if not args.label:
        print("pool_identity: --device-flow needs --label <the agent's name>", file=sys.stderr)
        return 2, "it was started without the agent's name"
    label = str(((pr.load_roster(ws) or {}).get("workers") or {}).get(wid, {}).get("label") or args.worker)
    def start_lane(row):
        if args.no_start:
            return None
        try:
            return connections.start_added(row, repo=args.repo or _REPO, logs_dir=args.logs_dir or ws / "logs",
                                           workspace=ws)
        except connections.ConnectionsError as e:
            return {"ok": False, "reason": str(e)}
    try:
        out = pool_device_connect.connect(
            wid, label=args.label, display_name=args.display_name or "", worker_label=label, rotate=rotate,
            workspace=ws, lane=args.lane, base=args.base or "", repo=args.repo, logs_dir=args.logs_dir,
            start_lane=start_lane, say=lambda text: print(text, file=sys.stderr, flush=True))
    except pool_device_connect.DeviceConnectError as e:
        print(f"pool_identity: {e}", file=sys.stderr)
        print(json.dumps({"status": e.status}, sort_keys=True))
        return 1, str(e)
    launch = out.get("launch")
    started = launch is None or bool(launch.get("ok"))
    if not started:
        print(f"lane {out['row']['instance']} not started: {launch.get('reason')}", file=sys.stderr)
    status = out["status"] if started else "stored_lane_not_started"
    line = {"agent_id": out["agent_id"], "status": status}
    if not rotate and not args.no_start:
        line["seat"] = reseat_after_add(ws, args.repo, wid).get("result")
    print(json.dumps(line, sort_keys=True))
    return (0, "") if started else (1, "")


def cmd_add(args, ws: Path) -> int:
    if args.device_flow:
        return cmd_device_flow(args, ws, rotate=False)
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
    if created:
        print(f"seat: {reseat_after_add(ws, args.repo, wid).get('result')}")
    if launch is not None and not launch.get("ok"):
        print(f"lane {row['instance']} not started: {launch.get('reason')}", file=sys.stderr)
        return 1
    print(f"lane {row['instance']} running")
    return 0


def cmd_show(args, ws: Path) -> int:
    wid = live_worker(ws, args.worker)
    rows = [connections.worker_identity(wid, ws, server=args.server)] if args.server \
        else connections.worker_identities(wid, ws)
    rows = [r for r in rows if r is not None]
    if not rows:
        print(f"{args.worker} has no agent of its own{' on ' + args.server if args.server else ''}; "
              "it answers through the core's")
        return 1
    keep = ("instance", "server", "agent_mxid", "agent_display_name", "token_ref", "added_by", "added_at", "disabled")
    shown = [{k: r.get(k) for k in keep} for r in rows]
    print(json.dumps(shown[0] if len(shown) == 1 else shown, indent=2, sort_keys=True))
    return 0


def cmd_rotate(args, ws: Path) -> int:
    if args.device_flow:
        return cmd_device_flow(args, ws, rotate=True)
    row = connections.worker_identity(live_worker(ws, args.worker), ws, server=args.server)
    if row is None:
        print(f"pool_identity: {args.worker} has no agent of its own to rotate"
              f"{' on ' + args.server if args.server else ''}", file=sys.stderr)
        return 1
    token = read_token(args.token, f"New access token for {args.worker}'s own agent (hidden): ")
    out = connections.update_token(str(row["instance"]), token, repo=args.repo, logs_dir=args.logs_dir, workspace=ws)
    print(f"rotated {out['instance']}; lane relaunched")
    return 0


def cmd_remove(args, ws: Path) -> int:
    roster = pr.load_roster(ws) or {}
    wid = pr.resolve_label(roster, args.worker)
    row = connections.remove_worker_identity(wid, server=args.server, workspace=ws)
    if row is None:
        print(f"{args.worker} has no agent of its own{' on ' + args.server if args.server else ''}")
        return 0
    print(f"removed {row['instance']} ({row.get('agent_mxid') or '?'}): lane stopped, row and vault secret gone; "
          "the agent stays on its server until the owner deletes it there")
    return 0


def cmd_provision_begin(args, ws: Path) -> int:
    wid = live_worker(ws, args.worker)
    row = ((pr.load_roster(ws) or {}).get("workers") or {}).get(wid) or {}
    out = pool_provision.begin(wid, role=args.role or str(row.get("label") or ""), workspace=ws, lane=args.lane)
    print(json.dumps(out, sort_keys=True))
    return 0


def _provisioned(args, ws: Path, out: dict, wid: str) -> int:
    """Start the new identity's lane and print the agent id and a status."""
    try:
        launch = None if args.no_start else connections.start_added(
            out["row"], repo=args.repo or _REPO, logs_dir=args.logs_dir or ws / "logs", workspace=ws)
    except connections.ConnectionsError as e:
        launch = {"ok": False, "reason": str(e)}
    started = launch is None or bool(launch.get("ok"))
    if not started:
        print(f"lane {out['row']['instance']} not started: {launch.get('reason')}", file=sys.stderr)
    line = {"agent_id": out["agent_id"], "status": "ok" if started else "stored_lane_not_started"}
    if not args.no_start:
        line["seat"] = reseat_after_add(ws, args.repo, wid).get("result")
    print(json.dumps(line, sort_keys=True))
    return 0 if started else 1


def cmd_provision_wait(args, ws: Path) -> int:
    wid = live_worker(ws, args.worker)
    return _provisioned(args, ws, pool_provision.provision(
        wid, args.operation_id, args.name, display_name=args.display_name or "", role=args.role or "",
        timeout=args.timeout, workspace=ws, lane=args.lane, say=lambda text: print(text, file=sys.stderr, flush=True)), wid)


def cmd_provision_finish(args, ws: Path) -> int:
    wid = live_worker(ws, args.worker)
    return _provisioned(args, ws, pool_provision.finish(wid, args.claim_id, args.agent_id, workspace=ws, lane=args.lane), wid)


def cmd_provision_abort(args, ws: Path) -> int:
    pool_provision.abort(pr.resolve_label(pr.load_roster(ws) or {}, args.worker))
    print(json.dumps({"status": "aborted"}))
    return 0


def cmd_retire_remote(args, ws: Path) -> int:
    roster = pr.load_roster(ws) or {}
    try:
        out = pool_provision.retire(pr.resolve_label(roster, args.worker), workspace=ws,
                                    local_anyway=args.local_anyway)
    except pool_provision.NotRetired as e:
        print(f"pool_identity: {e}", file=sys.stderr)
        return 3
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] in ("retired", "no_identity") else 1


def cmd_card_reply(args, ws: Path) -> int:
    """A click on a worker approval card, read from the task it arrived as; the owner's only. Any other
    task prints agent_card's answer for it: a forged or stale click is refused in fixed words."""
    try:
        text = Path(args.task_file).read_text(encoding="utf-8")
    except OSError as e:
        print(json.dumps({"status": "refused", "why": f"the task file is unreadable ({type(e).__name__})"}))
        return 2
    headers, words = agent_card.reply_words(text)
    try:
        owner = str(pool_provision.core_lane(ws, None).get("owner_mxid") or "")
    except pool_provision.ProvisionError:
        owner = ""  # a card records its own lane; only one without it needs the core's owner
    verdict = agent_card.click_verdict(headers, words, ws, agent_card.lane_owner(ws, owner))
    other = agent_card.click_answer(verdict, words, (pool_device_connect.CARD_KIND,))
    if other is not None:
        print(json.dumps(other, sort_keys=True))
        return 1
    said = verdict.command
    row = ((pr.load_roster(ws) or {}).get("workers") or {}).get(said.target)
    if not isinstance(row, dict) or row.get("state") == "retired":
        print(json.dumps({"status": "refused", "why": f"no live pool worker {said.target!r}"}, sort_keys=True))
        return 2
    out = pool_device_connect.resend(said.target, workspace=ws)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] == "started" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pool_identity", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", type=Path, default=None)
    sub = p.add_subparsers(dest="verb", required=True)
    a = sub.add_parser("add"); a.add_argument("worker"); a.add_argument("token", nargs="?", default=None)
    a.add_argument("--yes", action="store_true"); a.add_argument("--expected-server", default=None)
    a.set_defaults(fn=cmd_add)
    s = sub.add_parser("show"); s.add_argument("worker"); s.set_defaults(fn=cmd_show)
    s.add_argument("--server", default=None, help="only the agent on this server (default: every one)")
    r = sub.add_parser("rotate"); r.add_argument("worker"); r.add_argument("token", nargs="?", default=None)
    r.set_defaults(fn=cmd_rotate)
    r.add_argument("--server", default=None, help="the agent on this server, for a worker with several "
                   "(--device-flow takes the server of --lane)")
    for s in (a, r):
        # A pasted token needs --repo and --logs-dir; --device-flow defaults them.
        s.add_argument("--repo", type=Path, default=None); s.add_argument("--logs-dir", type=Path, default=None)
        s.add_argument("--device-flow", action="store_true"); s.add_argument("--label", default=None)
        s.add_argument("--display-name", default=None); s.add_argument("--lane", default=None)
        s.add_argument("--base", default=None, help=argparse.SUPPRESS); s.add_argument("--no-start", action="store_true")
        s.add_argument("--resend-id", default=None, help=argparse.SUPPRESS)
    s = sub.add_parser("remove"); s.add_argument("worker"); s.set_defaults(fn=cmd_remove)
    s.add_argument("--server", default=None, help="the agent on this server, for a worker with several")
    s = sub.add_parser("provision-begin"); s.add_argument("worker"); s.add_argument("--role", default=None)
    s.add_argument("--lane", default=None); s.set_defaults(fn=cmd_provision_begin)
    s = sub.add_parser("provision-wait"); s.add_argument("worker"); s.add_argument("--operation-id", required=True)
    s.add_argument("--name", required=True); s.add_argument("--display-name", default=None)
    s.add_argument("--role", default=None)
    s.add_argument("--timeout", type=float, default=pool_provision.APPROVAL_TIMEOUT_S)
    s.set_defaults(fn=cmd_provision_wait)
    f = sub.add_parser("provision-finish"); f.add_argument("worker"); f.add_argument("--claim-id", required=True)
    f.add_argument("--agent-id", required=True); f.set_defaults(fn=cmd_provision_finish)
    for s in (s, f):
        s.add_argument("--lane", default=None); s.add_argument("--no-start", action="store_true")
        s.add_argument("--repo", type=Path, default=None); s.add_argument("--logs-dir", type=Path, default=None)
    s = sub.add_parser("provision-abort"); s.add_argument("worker"); s.set_defaults(fn=cmd_provision_abort)
    s = sub.add_parser("retire-remote"); s.add_argument("worker"); s.add_argument("--local-anyway", action="store_true")
    s.set_defaults(fn=cmd_retire_remote)
    s = sub.add_parser("card-reply"); s.add_argument("--task-file", required=True); s.set_defaults(fn=cmd_card_reply)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb in ("add", "rotate") and not args.device_flow and (args.repo is None or args.logs_dir is None):
        parser.error(f"{args.verb} with a token needs --repo and --logs-dir")
    ws = args.workspace if args.workspace is not None else resolve_workspace()
    try:
        return args.fn(args, Path(ws))
    except pr.AmbiguousWorkerName as e:
        print(f"pool_identity: {e}", file=sys.stderr)
        return 2
    except connections.Refusal as e:
        print(f"pool_identity: refused ({e.CODE}): {e}", file=sys.stderr)
        return 1
    except (connections.ConnectionsError, pool_provision.ProvisionError) as e:
        print(f"pool_identity: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
