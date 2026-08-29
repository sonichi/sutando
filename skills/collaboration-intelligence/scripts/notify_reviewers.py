#!/usr/bin/env python3
"""Rule 9 as a tool: notify reviewers by their Sutando STAND, from the map.

The rule failed in prose because acting happens from momentum while rules
load on invocation — so the correct path must be the easy path. This script
resolves each reviewer through the collaboration-intelligence roster and
refuses everything rule 9 forbids: unknown names (no guessing), bare human
mentions (a person-mention triggers no Stand), and known-off-allowlist
sends (a bounced mention notifies no one).

Usage:
  notify_reviewers.py --reviewers rui,kewei --message "re-review #3303" [--send]

Without --send it prints the exact room_ops commands (plan mode). A refused
entry never starves the batch: resolvable reviewers are still notified and
the worst refusal becomes the exit — 0 all resolved; 2 unknown reviewer;
3 entry unusable (no stand/room); 4 allowlist known-false (route via owner).

Roster: <workspace>/data/collaboration-intelligence/reviewer-stands.json
  {"rui": {"human": "@rui:ag2.space", "stand": "@sutando-rui:ag2.space",
           "room": "!triage:ag2.space", "allowlisted": true, "gh": "john-the-dev"}}
`allowlisted` is evidence, not hope: true (a mention has triggered this
Stand), false (it bounced), null/absent (never observed — send, then record).
"""
from __future__ import annotations

import argparse
import json
import os
import datetime
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
# Bare `python3` can resolve to the Xcode CLT stub on a clean macOS host, which
# raises an install modal and makes the probe fail open. Reuse this interpreter.
_PY = sys.executable or "python3"
sys.path.insert(0, str(_REPO / "src"))


def roster_path() -> Path:
    override = os.environ.get("SUTANDO_SCI_ROSTER")
    if override:
        return Path(override)
    from workspace_default import resolve_workspace
    return (Path(resolve_workspace()) / "data" / "collaboration-intelligence"
            / "reviewer-stands.json")


def load_roster() -> dict:
    p = roster_path()
    if not p.is_file():
        raise SystemExit(f"no roster at {p} — seed it from the map before "
                         "notifying (never guess Stand identities)")
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"roster at {p} is not an object")
    return data


def resolve(names: "list[str]", roster: dict) -> "tuple[list[dict], int]":
    """(targets, refusal_rc): one bad entry must never starve the rest of the
    batch — resolvable reviewers are still notified, the worst refusal code
    is carried to the exit so the caller sees somebody was skipped."""
    out, worst = [], 0
    for name in names:
        entry = roster.get(name)
        if entry is None:
            print(f"UNKNOWN reviewer '{name}' — not in {roster_path()}; "
                  "add them from the map, do not guess", file=sys.stderr)
            worst = max(worst, 2)
            continue
        stand, room = entry.get("stand"), entry.get("room")
        dm_id = entry.get("discord_id") or entry.get("stand_discord_id")
        channel = entry.get("home_channel")
        if stand and room:
            transport = "matrix"
        elif dm_id and channel:
            transport = "discord"
        else:
            # Distinguish NO ROUTE from A ROUTE THIS TOOL CANNOT DRIVE. Collapsing
            # them made every refusal read as "this reviewer is unreachable".
            if stand and not room:
                detail = (f"has a Stand ({stand}) but no 'room' — a Stand mxid is "
                          "room-scoped, so it cannot be addressed without one")
            elif dm_id and not channel:
                detail = (f"has a Discord id ({dm_id}) but no 'home_channel' — "
                          "no channel to mention them in")
            else:
                detail = ("carries no addressable route at all (a human handle "
                          "alone triggers no Stand)")
            print(f"UNUSABLE entry '{name}': {detail}", file=sys.stderr)
            worst = max(worst, 3)
            continue
        if entry.get("allowlisted") is False:
            who = stand or dm_id
            print(f"OFF-ALLOWLIST '{name}': {who} bounced a mention before —"
                  " route through the owner instead of re-sending",
                  file=sys.stderr)
            worst = max(worst, 4)
            continue
        out.append({"name": name, "transport": transport, "stand": stand,
                    "room": room, "discord_id": dm_id, "channel": channel,
                    "human": entry.get("human")})
    return out, worst


def stand_present_in_room(target: dict) -> "tuple[bool, str]":
    """Is this stand actually a member of the room we are about to mention it in?

    A Stand mxid is scoped to a ROOM, not to a person — the same human can hold a
    different Stand per room. room_ops has no unknown-handle branch, so mentioning
    an absent mxid resolves to nothing and reports ok, which is indistinguishable
    from a delivered mention. Returns (present, reason); an UNVERIFIABLE roster is
    not treated as absent — we refuse to send only on a positive absence.
    """
    argv = [_PY, str(_REPO / "skills" / "agent-room-ops" / "room_ops.py"),
            "members", target["room"]]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception as exc:                     # noqa: BLE001 - probe must not raise
        return True, f"unverified ({type(exc).__name__})"
    if p.returncode != 0:
        return True, f"unverified (members rc={p.returncode})"
    try:
        payload = json.loads(p.stdout)
    except ValueError:
        return True, "unverified (unparseable members)"
    # A non-object payload has no .get — the same shape the refusal-reason fix
    # guards downstream. An unusable roster is UNVERIFIED, never an absence.
    if not isinstance(payload, dict):
        return True, "unverified (non-object members payload)"
    # `ok` is the instrument's own verdict. Only a true one licenses reading the
    # list as fact; without it an empty list is a failure, not an empty room.
    if payload.get("ok") is not True:
        return True, f"unverified (room_ops ok={payload.get('ok')!r})"
    members = payload.get("members")
    if not isinstance(members, list):
        return True, "unverified (members not a list)"
    ids = {m.get("user_id") for m in members if isinstance(m, dict)}
    return target["stand"] in ids, f"{len(ids)} members"


def discord_reachable(target: dict) -> "tuple[bool, str]":
    """Is this id actually in the channel we are about to mention it in?

    Same shape as stand_present_in_room and the same failure discipline: an
    UNREADABLE access map is UNVERIFIED, never a positive absence, so a broken
    config can never be reported as "this reviewer is unreachable".
    """
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg_dir) if cfg_dir else Path.home() / ".claude"
    access = base / "channels" / "discord" / "access.json"
    try:
        data = json.loads(access.read_text())
    except Exception as exc:                     # noqa: BLE001 - probe must not raise
        return True, f"unverified ({type(exc).__name__})"
    if not isinstance(data, dict):
        return True, "unverified (non-object access map)"
    for section in ("groups", "channels"):
        sect = data.get(section)
        # A truthy non-object has no .get, and a scalar allowFrom ITERATES:
        # "1" answers per character. Unusable shapes are unverified, not answers.
        if sect is not None and not isinstance(sect, dict):
            return True, f"unverified ({section} is {type(sect).__name__}, not an object)"
        entry = (sect or {}).get(str(target["channel"]))
        if isinstance(entry, dict):
            raw = entry.get("allowFrom")
            if raw is not None and not isinstance(raw, (list, tuple, set)):
                return True, f"unverified (allowFrom is {type(raw).__name__}, not a list)"
            allowed = {str(x) for x in (raw or [])}
            if not allowed:
                return True, f"unverified ({section} entry has no allowFrom)"
            return str(target["discord_id"]) in allowed, f"{len(allowed)} in allowFrom"
    return True, "unverified (channel not in the access map)"


def discord_command_for(target: dict, message: str) -> "list[str]":
    """Post into the channel discord_reachable actually validated.

    bot2bot-post always resolves the bot2bot channel and `--to` only picks the
    mention, so routing through it validated one channel and delivered to another.
    """
    return [_PY, str(_REPO / "skills" / "collaboration-intelligence" / "scripts"
                     / "send_channel_message.py"),
            str(target["channel"]), str(target["discord_id"]),
            f"<@{target['discord_id']}> {message}"]


def command_for(target: dict, message: str) -> "list[str]":
    body = message
    # Roster "human" is a room handle for some entries and a structured record
    # (discord id, username) for others; only the former is addressable here.
    human = target.get("human")
    if isinstance(human, str) and human and human not in body:
        body = f"{body} (cc {human})"
    return [_PY, str(_REPO / "skills" / "agent-room-ops" / "room_ops.py"),
            "mention", target["stand"], body, target["room"]]


_PR_URL = re.compile("github[.]com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/pull/([0-9]+)")

def ledger_path() -> Path:
    from workspace_default import resolve_workspace
    return Path(resolve_workspace()) / "state" / "review-asks.jsonl"


def record_asks(message: str, reviewer: str) -> int:
    """Log a room ask so pr-unattended can see it. GitHub's timeline records only
    review_requested events, and the owner's rule is to ask in the room and never
    via GitHub — so without this every correctly-routed PR reads NOBODY_EVER_ASKED."""
    refs = {(m.group(1), int(m.group(2))) for m in _PR_URL.finditer(message)}
    if not refs:
        return 0
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        for repo, num in sorted(refs):
            fh.write(json.dumps({"repo": repo, "pr": num, "reviewer": reviewer,
                                 "ts": ts, "channel": "room"}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(refs)


def _actor_map(roster) -> dict:
    """key -> canonical actor, over the CONNECTED COMPONENT of same_actor_as.

    The links are mutual (a<->b) and can chain (c->b), so neither end is canonical
    and following one hop splits a chain: with a<->b and c->b, min() sends a,b to
    `a` but c to `b`, and one human is listed twice. Union by smallest key over
    the whole component instead.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    for k, v in (roster or {}).items():
        if not isinstance(v, dict) or k.startswith("_"):
            continue
        find(k)
        other = v.get("same_actor_as")
        if other:
            union(k, other)
    return {k: find(k) for k in parent}

def _stale_repeat_ask(message: str, targets, roster, minutes: int = 30):
    """(refuse, detail) — refuse re-asking the SAME non-responders after `minutes`.

    The owner's rule is "if they didn't get back in 30min, ask OTHERS — do NOT
    block". Re-requesting the same names is not escalation; the routing tool says
    so about GitHub and it is equally true in-room. Enforced here because a rule
    I have to remember is one I demonstrably do not: on sonichi#3511 I asked the
    same two people twice, 8 minutes apart, and neither ever reviewed.

    Fails OPEN on any uncertainty — a notifier that blocks on its own bug is
    worse than one that over-notifies.
    """
    refs = _PR_URL.findall(message or "")
    if not refs:
        return False, ""
    repo, num = refs[0]
    ledger = ledger_path()
    if not ledger.exists():
        return False, ""
    import json as _j
    prior, earliest = set(), None
    try:
        for line in ledger.read_text().splitlines():
            try:
                d = _j.loads(line)
            except ValueError:
                continue
            if str(d.get("pr")) != str(num) or d.get("repo") not in (repo, None):
                continue
            prior.add(d.get("reviewer"))
            ts = d.get("ts") or ""
            if ts and (earliest is None or ts < earliest):
                earliest = ts
    except OSError:
        return False, ""
    if not prior or earliest is None:
        return False, ""
    names = {x["name"] for x in targets}
    if not names or not names.issubset(prior):
        return False, ""            # at least one NEW name -> this IS widening
    try:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(earliest.replace("Z", "+00:00")))
    except ValueError:
        return False, ""
    if age.total_seconds() < minutes * 60:
        return False, ""
    # One human can hold several roster keys (jsun-m IS johnm-desktop). Listing
    # both overstates the pool and re-asks one person under two names.
    actor_of = _actor_map(roster)
    seen_actors, unasked = set(), []
    for k, v in sorted((roster or {}).items()):
        if not isinstance(v, dict) or k.startswith("_"):
            continue
        actor = actor_of.get(k, k)
        if k in prior or k == "keweichen":
            seen_actors.add(actor)
            continue
        if actor in seen_actors:
            continue
        seen_actors.add(actor)
        unasked.append(k)
    # This reads the ledger only; it cannot know review state, and a refusal
    # reason gets quoted onward as fact.
    return True, (f"every target was already asked on {repo}#{num} "
                  f"{int(age.total_seconds() // 60)} min ago (review state not "
                  f"checked here — read it before reporting anyone unresponsive). "
                  f"Not yet asked: {', '.join(unasked) or '<roster exhausted>'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewers", required=True,
                    help="comma-separated roster keys")
    ap.add_argument("--message", required=True)
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--allow-single", metavar="REASON", default="",
                    help="deliberately notify ONE reviewer; requires a reason")
    ap.add_argument("--widen-override", metavar="REASON", default="",
                    help="deliberately re-ask the SAME reviewers after 30min")
    ap.add_argument("--room", default=None,
                    help="room the conversation is actually in. When given, a reviewer whose "
                         "Stand is not a member THERE is REFUSED rather than silently notified "
                         "in their recorded room — correctly addressed, wrong venue.")
    a = ap.parse_args()
    names = [n.strip() for n in a.reviewers.split(",") if n.strip()]
    targets, refusal_rc = resolve(names, load_roster())
    # Gates run on RESOLVED targets before any send, so no partial batch notifies
    # one person; plan mode is exempt because only a real ASK can strand a PR.
    if a.send and len(targets) < 2 and not a.allow_single:
        print(f"REFUSED: {len(targets)} reviewer(s) resolved from {names!r}; the rule is at "
              "least TWO, so one being busy cannot stall the PR. Name another reviewer, "
              "or pass --allow-single '<reason>'.", file=sys.stderr)
        # A failed name is WHY the count is short and is the actionable half.
        # `> 0` not `or`: refusal codes are positive, 0 means nothing refused.
        return refusal_rc if refusal_rc > 0 else 5
    if a.allow_single and len(targets) < 2:
        print(f"single-reviewer ask allowed: {a.allow_single}", file=sys.stderr)
    stale, why = _stale_repeat_ask(a.message, targets, load_roster())
    if stale and not a.widen_override:
        print(f"REFUSED: {why} Re-asking the same people is not escalation — "
              "name someone new, or pass --widen-override '<reason>'.", file=sys.stderr)
        return 6
    failures = unlogged = 0
    for t in targets:
        if t["transport"] == "discord":
            # No room-relocation branch: a Discord mention is channel-scoped and
            # --room names a Matrix room, so it cannot apply to this target.
            here, why = discord_reachable(t)
            if not here:
                print(f"{t['name']}: ABSENT from channel {t['channel']} ({why}) — "
                      f"{t['discord_id']} is not on its allowFrom; a mention there "
                      "reaches nobody. Resolve this person's channel.", file=sys.stderr)
                failures += 1
                continue
            if why.startswith("unverified"):
                print(f"{t['name']}: UNVERIFIED for channel {t['channel']} ({why}) — "
                      "sending unchecked; this is not a confirmation.", file=sys.stderr)
            argv = discord_command_for(t, a.message)
            if not a.send:
                print("PLAN:", " ".join(argv))
                continue
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                print(f"{t['name']}: SENT to channel {t['channel']}")
                # Same bookkeeping as the Matrix path: without it a delivered
                # Discord ask reads as NOBODY_EVER_ASKED to pr-unattended.
                try:
                    n_logged = record_asks(a.message, t["name"])
                except OSError as e:
                    unlogged += 1
                    print(f"  WARNING: the ask to {t['name']} SUCCEEDED but was NOT "
                          f"recorded ({e}) — pr-unattended will under-report this PR "
                          "as unasked", file=sys.stderr)
                else:
                    if n_logged:
                        print(f"  logged {n_logged} PR ask(s) for {t['name']}",
                              file=sys.stderr)
            elif p.returncode == 4:
                # OUTCOME_UNKNOWN: the post may have landed. Counting it as a
                # failure invites the retry the receipt says is unsafe.
                print(f"{t['name']}: OUTCOME UNKNOWN on channel {t['channel']} — "
                      f"{(p.stderr or '').strip() or 'no stderr'}", file=sys.stderr)
                failures += 1
            else:
                print(f"{t['name']}: SEND FAILED rc={p.returncode} "
                      f"{(p.stderr or '').strip() or 'no stderr'}", file=sys.stderr)
                failures += 1
            continue
        if a.room and t["room"] != a.room:
            # Not an error: the pair is valid, but the Stand does not live in
            # THIS room, and sending would relocate the thread and report ok.
            here, why = stand_present_in_room({"stand": t["stand"], "room": a.room})
            if not here:
                # Naming the recorded room as a fallback is itself a presence
                # claim; check it, or this refusal redirects to a second nobody.
                there, why2 = stand_present_in_room(t)
                # The probe fails OPEN, so `there` is True for an unreadable roster:
                # test unverified FIRST or this asserts presence it never measured.
                if why2.startswith("unverified"):
                    where = (f"{t['stand']}'s recorded room {t['room']} could not be "
                             f"checked ({why2}) — do not assume they are reachable there")
                elif there:
                    where = (f"{t['stand']} IS a member of {t['room']} ({why2}) — "
                             "post there deliberately, or route via the human")
                else:
                    where = (f"{t['stand']} is absent from its recorded room {t['room']} "
                             f"too ({why2}) — the roster entry is stale; resolve this "
                             "person's Stand before addressing them anywhere")
                print(f"{t['name']}: NOT REACHABLE in {a.room} ({why}) — {where}. "
                      "Not sending.", file=sys.stderr)
                refusal_rc = max(refusal_rc, 5)
                continue
            if why.startswith("unverified"):
                # Distinct from a checked PLAN: relocating on an unread roster is
                # a guess, and it must not print the same line as a verified send.
                print(f"{t['name']}: UNVERIFIED for {a.room} ({why}) — sending anyway; "
                      "presence could not be checked, so this is not a confirmation.",
                      file=sys.stderr)
            t = {**t, "room": a.room}   # reachable here: address them HERE, not elsewhere
        present, why = stand_present_in_room(t)
        if present and why.startswith("unverified"):
            print(f"{t['name']}: UNVERIFIED for {t['room']} ({why}) — sending unchecked.",
                  file=sys.stderr)
        if not present:
            # Positive absence: sending would resolve to nothing and report ok.
            print(f"{t['name']}: ABSENT from {t['room']} ({why}) — "
                  f"{t['stand']} is not a member; a mention here reaches nobody. "
                  "Resolve the room's own Stand for this person.", file=sys.stderr)
            failures += 1
            continue
        argv = command_for(t, a.message)
        if not a.send:
            print("PLAN:", " ".join(argv))
            continue
        # Per-target boundary: a raise here would drop every remaining target
        # AND skip the return, so the caller sees no asks and no failure code.
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"{t['name']}: ok=False reason=room_ops exceeded the 60s timeout",
                  file=sys.stderr)
            failures += 1
            continue
        except OSError as e:
            print(f"{t['name']}: ok=False reason=could not run room_ops ({e})",
                  file=sys.stderr)
            failures += 1
            continue
        ok, event, reason = False, "", ""
        try:
            payload = json.loads(p.stdout)
        except ValueError:
            payload = None
        # A non-object payload has no .get and a non-string event_id breaks
        # the slice; an unusable one must not occupy `reason` and hide stderr.
        if isinstance(payload, dict):
            ok = bool(payload.get("ok"))
            event = str(payload.get("event_id") or "")
            reason = str(payload.get("reason") or "")
            fallback = "no reason reported"
        else:
            fallback = "unparseable room_ops output"
        # room_ops reports refusals in-band: rc 0, empty stderr, ok:false + reason.
        # Printing stderr alone renders every such refusal as a blank line.
        if ok:
            print(f"{t['name']}: ok=True event={event[:24]}")
            # The ask already happened; a lost ledger write makes pr-unattended
            # report NOBODY_EVER_ASKED for someone who was asked. Loud, not fatal.
            try:
                n_logged = record_asks(a.message, t["name"])
            except OSError as e:
                unlogged += 1
                print(f"  WARNING: the ask to {t['name']} SUCCEEDED but was NOT recorded "
                      f"({e}) — pr-unattended will under-report this PR as unasked",
                      file=sys.stderr)
            else:
                if n_logged:
                    print(f"  logged {n_logged} PR ask(s) for {t['name']}", file=sys.stderr)
        else:
            detail = reason or p.stderr.strip()[:120] or fallback
            print(f"{t['name']}: ok=False reason={detail}", file=sys.stderr)
            failures += 1
    if unlogged:
        print(f"{unlogged} ask(s) were delivered but not recorded — the ledger "
              "under-reports and pr-unattended will read this PR as unasked",
              file=sys.stderr)
    if failures or unlogged:
        return 1
    return refusal_rc


if __name__ == "__main__":
    sys.exit(main())
