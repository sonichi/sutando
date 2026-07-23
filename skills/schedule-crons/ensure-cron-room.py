#!/usr/bin/env python3
"""ensure-cron-room.py — attach a dedicated AG2 Space room to a cron's output.

Idempotent: a cron entry in crons.json opts in with `"room": "auto"`; this
helper creates ONE dedicated room, invites the owner, posts a self-identifying
first message, and rewrites the entry's `room` to the concrete room id. On the
next run the entry already has a `!id:...` — it is skipped. So re-running
/schedule-crons never makes duplicate rooms (the failure mode of ad-hoc
creation, 2026-07-11).

Connectivity-gated: if the agent is NOT connected to AG2 Space (no gateway
token resolvable), every entry is left untouched and the script exits 0 — a
non-ag2space install just never grows rooms.

Design constraints baked in (learned 2026-07-11):
  - The gateway has NO room-list API and `op:state` 502s (can't set/read a
    room name post-create). So identity rides on (a) the create-time `name`
    and (b) the identifying first message — never on a post-hoc state write.
  - Short per-call timeout + no long retries, so a slow gateway can't hang a
    whole batch past a caller's deadline. Each entry is written back to disk
    immediately, so a mid-batch abort never loses an already-created room id.

Usage:
  ensure-cron-room.py --crons-file <path> [--name <cron> | --all]
                      [--owner <mxid>] [--repo <repo-root>] [--dry-run]

Token: resolved from $GATEWAY_TOKEN / $REMOTE_TASK_TOKEN / $AG2_REMOTE_TOKEN
(env first) else the `AG2_REMOTE_TOKEN=` line in <repo>/.env. Combined
`https://<gateway>|<secret>` form is split; a bare secret needs $GATEWAY_URL.
Owner mxid: --owner, else $AG2_OWNER_MXID, else the token's own account is not
discoverable here so we require one of those when creating.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error


def resolve_token(repo):
    # Resolve gateway creds from the SAME alias set the existing clients use
    # (src/remote-gateway-bridge.py, skills/agent-room-ops/_gateway.py) — read
    # from BOTH process env and <repo>/.env (the persistent bridge/startup
    # setup), process env winning. Parsing only AG2_REMOTE_TOKEN from .env
    # mis-detected a split-token install (e.g. REMOTE_TASK_URL + REMOTE_TASK_TOKEN)
    # as "not connected" (review #2079).
    TOKEN_KEYS = ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
    URL_KEYS = ("GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL", "AG2_REMOTE_URL")
    vals = {k: os.environ.get(k) for k in TOKEN_KEYS + URL_KEYS}
    envp = os.path.join(repo, ".env")
    if os.path.isfile(envp):
        for line in open(envp):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in vals and not vals.get(k):  # process env wins over .env
                vals[k] = v.strip().strip('"').strip("'")
    raw = next((vals[k] for k in TOKEN_KEYS if vals.get(k)), "")
    if not raw:
        return None, None
    # Combined onboarding form "https://<gateway>|<secret>" carries the URL in
    # the token; otherwise it's a bare secret and the URL comes from env.
    if "|" in raw and raw.split("|", 1)[0].startswith(("http://", "https://")):
        url_from_token, secret = raw.split("|", 1)
    else:
        url_from_token, secret = "", raw
    # URL precedence (same as the existing clients): explicit
    # GATEWAY_URL > RELAY_URL > REMOTE_TASK_URL > AG2_REMOTE_URL > url-from-token.
    url = next((vals[k] for k in URL_KEYS if vals.get(k)), url_from_token or "").rstrip("/")
    return (url or None), (secret or None)


def call(url, secret, payload, timeout=10):
    req = urllib.request.Request(
        f"{url}/v1/room", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {secret}",
                 "User-Agent": "sutando-core/1.0",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read(200).decode(errors="replace")}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}


def ensure_one(url, secret, owner, entry, dry_run):
    """Return (status_str, detail). Mutates entry['room'] on success."""
    name = entry["name"]
    room = entry.get("room")
    if not room or room in (False, None):
        return "no-room-opt-in", ""
    if isinstance(room, str) and room.startswith("!"):
        return "exists", room  # already ensured — idempotent skip
    # room == "auto" (or truthy sentinel) → create a dedicated sub-room.
    if dry_run:
        return "would-create", f"Sutando · {name}"
    # CREATE-WITH-INVITE: op:create accepts an `invite:[mxid]` list and invites
    # at creation (owner E2E-confirmed 2026-07-11). This bypasses the separate
    # op:invite op, which is broker-broken (hangs ~30s → 502). Never call
    # op:invite here — it's unreliable and can queue duplicate invites.
    payload = {"op": "create", "name": f"Sutando · {name}"}
    if owner:
        payload["invite"] = [owner]
    s, res = call(url, secret, payload)
    rid = res.get("room_id") if isinstance(res, dict) else None
    if not rid:
        return "CREATE_FAIL", f"{s} {res}"
    body = (f"**[Sutando cron room: {name}]**\n"
            f"Schedule: `{entry.get('cron', 'dynamic')}`\n"
            f"This room receives the output of the **{name}** cron. "
            f"Created by ensure-cron-room (schedule-crons), invited at creation.")
    call(url, secret, {"op": "message", "room_id": rid, "body": body})
    entry["room"] = rid
    return "created", rid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crons-file", required=True)
    ap.add_argument("--name", help="ensure only this cron; default = all opted-in")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--owner", default=os.environ.get("AG2_OWNER_MXID"))
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    url, secret = resolve_token(a.repo)
    if not (url and secret):
        print("ensure-cron-room: not connected to AG2 Space (no gateway token) — skipping, no rooms created.")
        return 0

    data = json.load(open(a.crons_file))
    targets = [e for e in data if (a.name is None or e.get("name") == a.name)]
    opted = [e for e in targets if e.get("room")]
    if not opted:
        print("ensure-cron-room: no entries opted in (`\"room\": \"auto\"`) — nothing to do.")
        return 0

    # owner required only when an actual create is pending
    pending_create = [e for e in opted if not str(e.get("room", "")).startswith("!")]
    if pending_create and not a.owner and not a.dry_run:
        print("ensure-cron-room: --owner <mxid> (or $AG2_OWNER_MXID) required to invite for new rooms.", file=sys.stderr)
        return 2

    for e in opted:
        status, detail = ensure_one(url, secret, a.owner, e, a.dry_run)
        print(f"  {e['name']:22s} {status:14s} {detail}")
        if status == "created" and not a.dry_run:
            # write back immediately — a later hang can't lose this room id
            json.dump(data, open(a.crons_file, "w"), indent=2)
            time.sleep(0.2)
    if not a.dry_run:
        json.dump(data, open(a.crons_file, "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
