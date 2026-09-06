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
  notify_reviewers.py --reviewers rui,kewei --body-file ask.md [--send]

Use --body-file for any prose carrying backticks, $ or an apostrophe: the shell
rewrites those before argv reaches this process, so no validation here can
recover the original. Same policy as bot2bot-post and discord-bridge.

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
import contextlib
import fcntl
import json
import os
import datetime
import re
import subprocess
import threading
import sys
from pathlib import Path
from urllib.parse import quote

_REPO = Path(__file__).resolve().parents[3]
# Bare `python3` can resolve to the Xcode CLT stub on a clean macOS host, which
# raises an install modal and makes the probe fail open. Reuse this interpreter.
_PY = sys.executable or "python3"
sys.path.insert(0, str(_REPO / "src"))


sys.path.insert(0, str(Path(__file__).resolve().parent))
from roster_union import host_rosters, roster_login, roster_union

_ROSTER_LEAF = Path("data") / "collaboration-intelligence" / "reviewer-stands.json"


def _host_label() -> str:
    """The canonical per-host slug, from the ONE helper that defines it.

    Re-deriving the precedence here would be a second copy of a policy that
    already drifted once; a failure to read it is not a licence to guess.
    """
    import subprocess
    out = subprocess.run(["bash", "scripts/sutando-config.sh", "host-label"],
                         capture_output=True, text=True, cwd=str(_REPO))
    return out.stdout.strip()


def roster_path() -> Path:
    """The path THIS host writes. Reads union over peers (see roster_paths)."""
    override = os.environ.get("SUTANDO_SCI_ROSTER")
    if override:
        return Path(override)
    from workspace_default import resolve_workspace
    ws = Path(resolve_workspace())
    host = _host_label()
    if host:
        per_host = ws / "hosts" / host / _ROSTER_LEAF
        if per_host.is_file() or not (ws / _ROSTER_LEAF).is_file():
            return per_host
    return ws / _ROSTER_LEAF          # legacy shared path, until the move lands


def roster_paths() -> "list[tuple[str, Path]]":
    """(host, path) for every roster on disk, LOCAL FIRST.

    An override names one file and means it: globbing past it would let a
    peer's rows answer a lookup a test pinned to a fixture.
    """
    override = os.environ.get("SUTANDO_SCI_ROSTER")
    if override:
        # An absent override is a REFUSAL, not an empty union: falling through
        # to the glob would let host rosters answer a lookup pinned to a fixture.
        p = Path(override)
        return [("", p)] if p.is_file() else []
    from workspace_default import resolve_workspace
    ws = Path(resolve_workspace())
    local = roster_path()
    # Label from the PATH, as host_rosters does: a second `host-label` subprocess here
    # made a refused ask spawn a process before refusing (sci-notify-reviewers-shorthand-refusal).
    label = local.parents[2].name if local.parent.parent.parent.parent.name == "hosts" else "legacy"
    out = [(label, local)] if local.is_file() else []
    out += [(h, p) for h, p in host_rosters(ws) if p != local]
    return out


def load_roster() -> dict:
    """Union across hosts; the merge policy is roster_union's, not restated here."""
    paths = roster_paths()
    if not paths:
        where = os.environ.get("SUTANDO_SCI_ROSTER") or "any host"
        raise SystemExit(f"no roster at {where} — seed it from the map before "
                         "notifying (never guess Stand identities)")
    return roster_union(paths)


def durable_endpoint(entry: dict) -> "str | None":
    """The transport's immutable recipient id, or None if the entry has no
    route. One owner: a second copy drifts from the one the park writes."""
    if not isinstance(entry, dict):
        return None
    stand, room = entry.get("stand"), entry.get("room")
    dm_id = entry.get("discord_id") or entry.get("stand_discord_id")
    # A non-string becomes a hash key downstream, so ONE malformed row starves
    # every requested reviewer — against resolve()'s one-bad-entry isolation.
    if not isinstance(stand, (str, type(None))):
        stand = None
    if not isinstance(dm_id, (str, int, type(None))):
        dm_id = None
    if stand and room:
        return stand
    if dm_id and entry.get("home_channel"):
        return f"discord:{dm_id}"
    return None


def stated_reason(entry: dict) -> str:
    """The roster's own words for why an entry refuses, if it gave any.

    A blank `stand` can be missing data OR a deliberate DO-NOT-ROUTE. Only the
    entry knows which, and a refusal that omits it invites the repair that
    overrides it (#3468)."""
    for key in ("refusal_basis", "note"):
        v = entry.get(key)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())
    return ""


def resolve(names: "list[str]", roster: dict) -> "tuple[list[dict], int]":
    """(targets, refusal_rc): one bad entry must never starve the rest of the
    batch — resolvable reviewers are still notified, the worst refusal code
    is carried to the exit so the caller sees somebody was skipped."""
    out, worst = [], 0
    actor_of, covered = _actor_map(roster), {}
    # The component comes from the WHOLE roster, so a connector row that is
    # unrequested, unroutable or off-allowlist still joins the people it links.
    component = identity_components(roster)
    cands = []

    for name in names:
        entry = roster.get(name)
        if entry is None:
            print(f"UNKNOWN reviewer '{name}' — not in {roster_path()}; "
                  "add them from the map, do not guess", file=sys.stderr)
            worst = max(worst, 2)
            continue
        stand, room = entry.get("stand"), entry.get("room")
        why = stated_reason(entry)
        # A caveat nobody prints is a note, not a step. Derived from the entry:
        # a named field list misses the next caveat silently.
        for field in sorted(k for k in entry if k.endswith("_caveat")):
            if entry.get(field):
                label = field[: -len("_caveat")].upper().replace("_", " ")
                print(f"{label} CAVEAT '{name}': {entry[field]}", file=sys.stderr)
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
            # The roster's own words, so the obvious repair (populate the
            # fields) cannot silently override a stated refusal.
            if why:
                print(f"  roster says: {why}", file=sys.stderr)
            worst = max(worst, 3)
            continue
        if entry.get("allowlisted") is False:
            # From main: state the FLAG, never a cause — nothing sets
            # allowlisted=False after a bounce, so a history claim is a guess.
            who = stand or dm_id
            print(f"OFF-ALLOWLIST '{name}': {who} is not allowlisted for mentions"
                  + (f" — {stated_reason(entry)}" if stated_reason(entry) else "")
                  + " — route through the owner instead of re-sending",
                  file=sys.stderr)
            worst = max(worst, 4)
            continue
        endpoint = durable_endpoint(entry)
        # One person can hold several roster keys, so counting NAMES lets the
        # two-reviewer gate in main() pass on one recipient addressed twice.
        actor = actor_of.get(name, name)
        cands.append((component.get(name, ("actor", actor)), name, {
            "name": name, "transport": transport, "stand": stand,
            "room": room, "discord_id": dm_id, "channel": channel,
            "endpoint": endpoint, "human": entry.get("human")}))

    for root, name, target in cands:
        prior = covered.get(root)
        if prior is not None:
            print(f"DUPLICATE '{name}': same person as '{prior}' "
                  f"({target['endpoint'] or actor_of.get(name, name)}) — "
                  "already covered, not a second reviewer", file=sys.stderr)
            continue
        covered[root] = name
        out.append(target)
    return out, worst


def gate_capability(repo: str, login: str) -> "tuple[bool | None, str]":
    """(can this login's approval discharge repo's approval gate?, why).

    Asked of GitHub, not of the roster: a cached tier goes stale silently and an
    approval from a read-only account is indistinguishable in the UI from one
    that counts. None = could not determine; the caller prints, never refuses.
    """
    try:
        p = subprocess.run(
            ["gh", "api", f"repos/{repo}/collaborators/{login}/permission",
             "-q", ".permission"],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:                     # noqa: BLE001 - probe must not raise
        return None, f"unverified ({type(exc).__name__})"
    if p.returncode != 0:
        return None, f"unverified (gh rc={p.returncode})"
    perm = p.stdout.strip()
    if perm in ("write", "admin", "maintain"):
        return True, perm
    if perm in ("read", "none", "triage"):
        # `read` is also what this endpoint returns for someone who is not a
        # collaborator at all, so the reason needs the membership check.
        return False, perm if _is_collaborator(repo, login) else "not a collaborator"
    return None, f"unverified (permission={perm!r})"



def _is_collaborator(repo: str, login: str) -> bool:
    try:
        p = subprocess.run(["gh", "api", f"repos/{repo}/collaborators/{login}"],
                           capture_output=True, text=True, timeout=60)
    except Exception:                            # noqa: BLE001 - probe must not raise
        return True                              # unknown -> the milder wording
    return p.returncode == 0



def _github_login(name: str, roster: dict) -> "tuple[str, str]":
    """(login GitHub can answer for, why) — a roster key is not always one.

    Explicit roster fields win over the key itself, unconditionally: a roster
    key can coincide with an unrelated real login, and then the key is not
    evidence. Which field declares that login is roster_union.roster_login's
    call, not this reader's.

    `johnm-desktop` is a Stand handle, not a login; probing it 404s and the
    capability check degrades to a silent no-op on exactly the aliased keys
    `_actor_map` exists to normalize. Follow same_actor_as to a sibling that is.
    """
    entry = (roster or {}).get(name) or {}
    # `or {}` keeps a truthy non-dict, and a hand-edited roster produces one.
    entry = entry if isinstance(entry, dict) else {}
    gh, field = roster_login(entry)
    # Not probed: _is_github_user collapses "no such user" and "probe failed",
    # so a timeout would discard owner-stated identity for the colliding key.
    if gh:
        return gh, f"roster {field} -> {gh}"
    sib = entry.get("same_actor_as")
    if sib:
        return sib, f"via same_actor_as -> {sib}"
    if _is_github_user(name):
        return name, "key is a login"
    return name, "no login found for this key"



def _is_github_user(login: str) -> bool:
    try:
        p = subprocess.run(["gh", "api", f"users/{login}", "-q", ".login"],
                           capture_output=True, text=True, timeout=60)
    except Exception:                            # noqa: BLE001 - probe must not raise
        return False
    return p.returncode == 0 and p.stdout.strip().lower() == login.lower()

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
    """Can this id be checked at all? Never a positive absence.

    `allowFrom` is INBOUND AUTHORIZATION -- who may send to a channel -- not
    membership, and the bridge also grants via a global superset this file
    cannot see. So an omission is not evidence of absence and never returns one.
    """
    try:
        # Inside the probe's own try, not above it: a tree without src/ raises
        # ModuleNotFoundError, and this probe must answer UNVERIFIED, never raise.
        from util_paths import claude_home_path
        access = claude_home_path("channels", "discord", "access.json")
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
            # [{"id": "111"}], [None], [True] all stringify, so a check on
            # the container alone yields a verdict computed from garbage.
            items = list(raw or [])
            if any(not isinstance(x, (str, int)) or isinstance(x, bool) for x in items):
                return True, f"unverified ({section} allowFrom holds non-scalar entries)"
            allowed = {str(x) for x in items}
            if not allowed:
                return True, f"unverified ({section} entry has no allowFrom)"
            if str(target["discord_id"]) in allowed:
                # A HIT is no more a membership witness than a miss: the field
                # answers "who may send TO this channel", not who reads it.
                return True, (f"unverified (listed in {section} allowFrom, which is "
                              "inbound authorization rather than membership)")
            # NOT an absence. allowFrom answers "who may send", and the bridge
            # grants via a global superset this file cannot see.
            return True, (f"unverified (not in {section} allowFrom, which is inbound "
                          "authorization rather than membership)")
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


def _canon_repo(r):
    """GitHub owner/name is case-insensitive, so the park must be too: a
    re-cased URL is the SAME pull request, not a second one to ask about."""
    return r.lower() if isinstance(r, str) else r


def _refs(message: str) -> set:
    """Case-canonical (repo, pr) refs. One reader so a writer and a checker
    cannot disagree about which PR a URL names."""
    return {(_canon_repo(m.group(1)), int(m.group(2)))
            for m in _PR_URL.finditer(message or "")}


def _refs_spelled(message: str) -> list:
    """(as-written repo, pr) per distinct canonical ref, for PERSISTENCE only.

    Rows keep the request's own spelling so a pre-canonicalization reader
    (rollback) still recognizes them by exact match; every reader in THIS
    revision canonicalizes through _row(), so both revisions dedup the row.
    """
    out, seen = [], set()
    for m in _PR_URL.finditer(message or ""):
        key = (_canon_repo(m.group(1)), int(m.group(2)))
        if key in seen:
            continue
        seen.add(key)
        out.append((m.group(1), int(m.group(2))))
    return out


# `owner/repo#12` and a bare `#12`. Two digits minimum is a DELIBERATE trade, not
# an oversight: `#7` is a real PR and passes silently, but `#1` is usually prose.
_PR_SHORTHAND = re.compile(
    r"(?:(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#|(?<![\w/#])#)(?P<num>[0-9]{2,})")


def unrecordable_pr_refs(message: str) -> list:
    """Shorthand PR references record_asks() will not log: (token, form that works).

    Absence is never reported — an ask need not concern a PR. Only shorthand is,
    because that is the case that reads as a PR reference and records nothing.
    """
    pairs = set(_PR_URL.findall(message))
    nums = {num for _, num in pairs}
    out = []
    for m in _PR_SHORTHAND.finditer(message):
        repo, num = m.group("repo"), m.group("num")
        # Pair-keyed when the repo is known: a URL to one repo must not suppress
        # another repo's same-numbered shorthand. Bare `#n` can only match a number.
        if (repo, num) in pairs if repo is not None else num in nums:
            continue
        out.append((m.group(0), f"https://github.com/{repo or '<owner>/<repo>'}/pull/{num}"))
    return out

def ledger_path() -> Path:
    # Overridable so a test can exercise the real reserve/settle path against a
    # scratch file. Two cases sharing one ledger park each other.
    override = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
    if override:
        return Path(override)
    from workspace_default import resolve_workspace
    return Path(resolve_workspace()) / "state" / "review-asks.jsonl"


#: Outcomes that must block a repeat. `pending` is a reservation written BEFORE
#: the spawn, so a crash between POST and outcome still parks.
_UNSAFE_OUTCOMES = {"pending", "unknown"}

#: A definite non-delivery. Nothing was posted, so it is neither a park nor an
#: ask — counting it refuses the retry it exists to permit.
_NOT_AN_ASK = {"failed"}

#: The child's codes for proven non-delivery. Everything else — a crash, a
#: signal, an unassigned code — is ambiguous and must park.
_PROVEN_NOT_DELIVERED = {2, 10}

#: Outcomes proving a post reached the channel, or may have.
_DID_ASK = {"confirmed", "unknown"}


#: Every outcome `record_asks` writes. A value outside this set is a malformed
#: row, never a settlement.
_KNOWN_OUTCOMES = {"pending", "unknown", "confirmed", "failed"}

#: Delivery history — a post that landed or may have. ONLY these carry
#: `reviewer`, the field a pre-outcome reader mistakes for a completed ask.
_DELIVERY_OUTCOMES = {"unknown", "confirmed"}

#: The one comparable form. Every accepted stamp is normalized to it, so the
#: lexical comparisons downstream order correctly across writers.
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _norm_ts(ts: str) -> "str | None":
    """A real instant rendered as fixed-width UTC, or None.

    Shape is not enough: `0000-00-00T00:00:00Z` and `+99:99` match a regex and
    are not instants, and mixed offsets do not sort against Z at all.
    """
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None                 # naive: no instant, only a wall clock

        # OverflowError raises HERE, not at parse; uncaught it crashes the
        # reader instead of preserving a park.
        return dt.astimezone(datetime.timezone.utc).strftime(_TS_FMT)
    except (ValueError, OverflowError, OSError):
        return None

#: Accepted row schema. A field of the wrong type is a malformed ROW, not a
#: reason to crash a reader or to misattribute the stream it belongs to.
def _row(d) -> "tuple | None":
    """(repo, pr, identity, outcome, ts) for a well-formed record, else None."""
    if not isinstance(d, dict):
        return None                     # valid JSON, not a record
    repo, pr = d.get("repo"), d.get("pr")
    actor = d.get("actor")
    if actor is not None and not (isinstance(actor, str) and actor):
        return None                     # PRESENT but wrong: not an absent actor
    # EVERY present identity field: a valid endpoint must not smuggle a list.
    for f in ("endpoint", "reviewer"):
        v = d.get(f)
        if v is not None and not (isinstance(v, str) and v):
            return None
    # The endpoint is durable identity; a roster alias is a renameable spelling.
    who = d.get("endpoint") or actor or d.get("reviewer")
    outcome, ts = d.get("outcome"), d.get("ts")
    if not isinstance(repo, (str, type(None))) or not isinstance(who, str) or not who:
        return None
    if not isinstance(pr, (str, int)) or isinstance(pr, bool):
        return None
    # Checked BEFORE coercion: `x or ""` turns 0, False, [] and {} into an
    # accepted empty string, so the type check never sees what was written.
    if ts is not None and not isinstance(ts, str):
        return None
    # Normalized, not just shaped: an impossible date matches a regex, and a
    # mixed offset does not order against a Z stamp at all.
    if ts:
        ts = _norm_ts(ts)
        if ts is None:
            return None
    # A closed set. An unrecognised outcome must not become the latest one and
    # settle a possibly-landed post; the row is malformed, so the park holds.
    if outcome is not None and (not isinstance(outcome, str)
                                or outcome not in _KNOWN_OUTCOMES):
        return None                     # type first: a list is unhashable
    # Malformed persisted identity drops the row; a park is never guessed.
    if "membership" in d and valid_tags(d.get("membership")) is None:
        return None
    return _canon_repo(repo), str(pr), who, outcome, ts or ""


def _streams(led: Path) -> dict:
    """(repo, pr, RAW spelling) -> compact state, folded line by line.

    THE one owner of the ledger's read contract: file access, malformed-line and
    malformed-ROW skipping, identity, key shape, and order. Retaining the rows
    made memory grow with the ledger for a result of fixed size.
    """
    out = {}
    if not led.exists():
        return out
    with open(led) as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            row = _row(d)
            if row is None:
                continue                # one bad row never hides a later good one
            repo, pr, who, outcome, ts = row
            st = out.setdefault((repo, pr, who),
                                {"last": None, "first_ask": None,
                                 "first_ask_outcome": None, "n": 0,
                                 "identity": {}, "first_identity": {},
                                 "last_identity": {}, "by_reviewer": {},
                                 "membership": []})
            # PER EVENT, not per stream: one slot stamped the newest spelling
            # onto every retained row, losing asks made under an older alias.
            ident = {f: d[f] for f in ("reviewer", "actor", "endpoint")
                     if isinstance(d.get(f), str) and d.get(f)}
            # As-written spelling rides the EVENT, so compaction can re-emit
            # it and a pre-canonicalization reader (rollback) still matches.
            if isinstance(d.get("repo"), str) and d.get("repo"):
                ident["spelled_repo"] = d["repo"]
            # REPLACES on a newer claim, matching the uncompacted reader that
            # `_membership_overlap` uses: a union revives retired links.
            mem = valid_tags(d.get("membership"))
            if mem is not None:
                st["membership"] = sorted(mem)
            st["identity"].update(ident)
            st["last_identity"] = ident
            # One delivery row per distinct legacy spelling survives compaction.
            if (outcome is None or outcome in _DELIVERY_OUTCOMES) and ident.get("reviewer"):
                st["by_reviewer"][ident["reviewer"]] = (outcome, ts, ident)
            st["last"] = (outcome, ts)
            st["n"] += 1
            # A row predating the outcome field records a send that happened:
            # absence is legacy, not a claim that nothing was posted.
            if (outcome is None or outcome in _DID_ASK) and (
                    st["first_ask"] is None or ts < st["first_ask"]):
                st["first_ask"], st["first_ask_outcome"] = ts, outcome
                st["first_identity"] = ident
    return out


#: Compaction trigger and hard ceiling. Compaction alone cannot bound BREADTH,
#: so settled streams are evicted oldest-first to reach the ceiling.
_COMPACT_ABOVE = 2000
_MAX_ROWS = 4000

#: Never evicted: an unresolved reservation or a possibly-landed post is the
#: safety state the park is made of. Retention gives way to it, not the reverse.
_ACTIVE = _UNSAFE_OUTCOMES


def _physical_rows(led: Path) -> int:
    """Every line on disk, malformed ones included — the file is what grows."""
    if not led.exists():
        return 0
    with open(led) as fh:
        return sum(1 for _ in fh)


def _retained(st: dict) -> list:
    """THE retained-row set: (outcome, ts, identity) per row compaction keeps.

    One owner so `_rows_for` and `_rewrite` cannot disagree about the cost."""
    keep = []
    if st["first_ask"] is not None:
        keep.append((st["first_ask_outcome"], st["first_ask"],
                     st.get("first_identity") or st.get("identity") or {}))
    if st["last"] and (not keep or st["last"] != (keep[0][0], keep[0][1])):
        keep.append((st["last"][0], st["last"][1],
                     st.get("last_identity") or st.get("identity") or {}))
    spelt = {k[2].get("reviewer") for k in keep if k[2].get("reviewer")}
    extra = [(o, t, i) for r, (o, t, i) in sorted((st.get("by_reviewer") or {}).items())
             if r not in spelt]
    if not keep:
        return extra or [(None, "", st.get("identity") or {})]
    # The SEMANTIC last event must stay the LAST physical row: `_streams` reads
    # latest state by line order, so an alias row after it flips the verdict.
    return keep[:-1] + extra + keep[-1:]


def _rows_for(st: dict) -> int:
    """Rows this stream costs after compaction."""
    return len(_retained(st))


def _maybe_compact(led: Path) -> None:
    """Caller MUST hold the ledger lock. Compacts, then evicts SETTLED streams
    oldest-first until the file is under _MAX_ROWS. Active safety state is never
    evicted, and failing to reach the ceiling is reported rather than hidden."""
    if _physical_rows(led) <= _COMPACT_ABOVE:
        return
    compact(led)
    if _physical_rows(led) <= _MAX_ROWS:
        return
    streams = _streams(led)
    cost = {k: _rows_for(st) for k, st in streams.items()}
    total = sum(cost.values())
    # str() on every component: `repo` may legitimately be None, and a tuple
    # sort then compares None with a string when timestamps tie.
    settled = sorted(((st["last"][1] if st["last"] else "",
                       tuple(str(x) for x in k), k)
                      for k, st in streams.items()
                      if not (st["last"] and st["last"][0] in _ACTIVE)))
    for _ts, _sortkey, k in settled:
        if total <= _MAX_ROWS:
            break
        total -= cost[k]
        streams.pop(k)
    _rewrite(led, streams)
    if _physical_rows(led) > _MAX_ROWS:
        print(f"  WARNING: ask ledger holds {_physical_rows(led)} rows, above the "
              f"{_MAX_ROWS} ceiling; every remaining stream carries active park "
              "state, which is never evicted", file=sys.stderr)


def _rewrite(led: Path, streams: dict) -> int:
    """Atomically replace the ledger with the rows these streams imply.
    Caller MUST hold the ledger lock."""
    rows = []
    for (repo, num, who), st in sorted(
            streams.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        for outcome, ts, identity in _retained(st):
            # The reader's normalized string: int() renamed "007" to "7".
            row = {"repo": identity.get("spelled_repo") or repo, "pr": num,
                   "ts": ts, "channel": "room", "outcome": outcome}
            row["actor"] = identity.get("actor") or who
            if identity.get("endpoint"):
                row["endpoint"] = identity["endpoint"]
            # Same rule as the append, plus legacy: a row predating the outcome
            # field IS a delivery, so compacting it must not drop `reviewer`.
            if outcome is None or outcome in _DELIVERY_OUTCOMES:
                row["reviewer"] = identity.get("reviewer") or who
            # Only the UNRESOLVED rows need it: those are what a later retry
            # admission reads, and a settled row's component is spent.
            if outcome in _ACTIVE and st.get("membership"):
                row["membership"] = st["membership"]
            rows.append(json.dumps(row))
    tmp = led.with_suffix(led.suffix + ".compact")
    mode = led.stat().st_mode & 0o777 if led.exists() else _LEDGER_MODE
    # Opened at its final private mode: writing under umask and chmod-ing after
    # leaves the payload readable for the whole write-and-fsync window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(r + "\n" for r in rows))
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)                 # umask can still have narrowed the open
    os.replace(tmp, led)                # atomic: a reader sees one file or the other
    return len(rows)


def compact(led: Path) -> int:
    """Rewrite to the smallest history the projections cannot tell from the
    original: per raw stream, the earliest real ask and the latest outcome.
    Caller MUST hold the ledger lock. Returns rows written."""
    return _rewrite(led, _streams(led))


def _fold(streams: dict, per_stream, combine, canonical=None) -> dict:
    """Project each RAW stream's state, then fold onto the canonical actor.

    Reducing before folding is the invariant both readers need: one alias's
    definite failure settles only its own reservation.
    """
    canon = canonical or (lambda w: w)
    out = {}
    for (repo, num, who), st in streams.items():
        v = per_stream(st)
        if v is None:
            continue
        k = (repo, num, canon(who))
        out[k] = combine(out[k], v) if k in out else v
    return out


def _latest_outcomes(led: Path) -> dict:
    """(repo, pr, RAW spelling) -> (outcome, ts) from the LAST row of that stream.

    Raw keys only. Folding this onto a canonical actor is the defect the park
    was fixed for, so the API does not offer it rather than leaving it callable.
    """
    return _fold(_streams(led), lambda st: st["last"], lambda a, b: b)


def _latest_with_identity(led: Path) -> dict:
    """(repo, pr, RAW spelling) -> ((outcome, ts), identity-as-written).

    The identity rides along so a reader can compare on the AXIS the row used.
    A raw spelling alone cannot: one person's endpoint and another's roster key
    can be the same text, and resolving that by a fixed axis order aliases them.
    """
    return _fold(_streams(led),
                 lambda st: (st["last"], dict(st.get("last_identity") or {})),
                 lambda a, b: b)


def _first_ask(led: Path, canonical=None) -> dict:
    """(repo, pr, actor) -> earliest ts at which an ask actually reached them."""
    def per_stream(st):
        if st["first_ask"] is not None:
            return st["first_ask"]
        # A standing reservation may have posted, so it counts as an ask at its
        # own ts until it settles; a settled `failed` never posted.
        return st["last"][1] if st["last"] and st["last"][0] == "pending" else None

    def earliest(a, b):
        return min(x for x in (a, b) if x) if (a and b) else (a or b)

    return _fold(_streams(led), per_stream, earliest, canonical=canonical)


def retry_clause(kind: str) -> str:
    """What an unsafe send may truthfully promise about repeating itself.
    Only an ask claims a park, so only an ask has protection to report."""
    if kind == "ask":
        return ("the park holds, so a repeat is refused — check the channel "
                "before clearing one")
    return ("NO retry record was written (a notice does not park). Re-running "
            "will attempt another post and MAY duplicate a delivered notice — "
            "check the channel before resending")


def unknown_parked(message: str, reviewer: str, actor: str = None,
                   canonical=None, endpoint: str = None) -> bool:
    """True when this ACTOR's latest row for this PR is unsafe to repeat.

    Keyed by canonical actor, not roster spelling: two aliases of one person are
    one endpoint, and keying by spelling lets the second alias resend.

    The ledger is append-only, so a later row supersedes an earlier one; the
    verdict is the LAST row per (repo, pr, actor), never any matching row.
    """
    who = actor or reviewer
    refs = _refs(message)
    led = ledger_path()
    if not refs or not led.exists():
        return False
    try:
        # Per RAW spelling, then OR across the actor: alpha's definite failure
        # settles alpha's reservation and proves nothing about beta's post.
        latest = _latest_with_identity(led)
    except OSError:
        # Cannot read the park state, so cannot prove this was NOT parked. Fail
        # closed: refusing a send is recoverable, a duplicated unsafe post is not.
        return True
    canon = canonical or (lambda w: w)
    for (repo, num, row_who), ((outcome, _ts), ident) in latest.items():
        # Compare on the axis the ROW recorded: a bare `row_who` is ambiguous,
        # since one person's endpoint can be another's roster key.
        row_endpoint = ident.get("endpoint")
        row_names = {ident.get(f) for f in ("reviewer", "actor")} - {None}
        if row_endpoint and endpoint:
            # Both sides name a recipient: the endpoint decides, alone. Falling
            # back to names here re-aliases two people who share a roster key.
            if row_endpoint != endpoint:
                continue
        elif row_endpoint or row_names:
            cands = row_names or {row_endpoint}
            if not any(canon(n) == canon(who) or n in (who, reviewer)
                       for n in cands):
                continue
        # Legacy rows carry no identity fields at all; fall back to the raw key.
        elif canon(row_who) != canon(who) and row_who not in (who, reviewer):
            continue
        if any(_canon_repo(repo) in (r, None) and num == str(n)
               for r, n in refs):
            if outcome in _UNSAFE_OUTCOMES:
                return True
    return False


#: The ledger holds who was asked about what; it is user state, not world
#: readable. A fresh file under umask 022 would be 0644.
_LEDGER_MODE = 0o600

#: flock is per-fd, so a nested take deadlocks. Re-entrant PER THREAD AND PER
#: LEDGER: a process-global counter let another thread skip flock entirely.
_LOCK_STATE = threading.local()


@contextlib.contextmanager
def _ledger_lock(led: Path):
    """The ledger's ONE mutual-exclusion point. Every writer takes it, so an
    append can never land inside a compactor's snapshot-then-replace window."""
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = _LOCK_STATE.held = set()
    key = str(led.resolve() if led.exists() else led)
    if key in held:
        yield                           # this thread already holds THIS ledger
        return
    led.parent.mkdir(parents=True, exist_ok=True)
    lock = led.with_suffix(led.suffix + ".lock")
    with open(lock, "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def reserve_ask(a, t, who, person_of, roster, require_ref=True):
    """Reserve the retry park for one ask. TRANSPORT-INDEPENDENT by contract.

    Returns (proceed, bucket, note). A park that only one transport honours is
    not a park: an alias on the other transport walks past a landed send."""
    if a.kind != "ask":
        return True, None, None
    if not _PR_URL.search(a.message):
        if require_ref:
            return False, "failure", (
                f"{t['name']}: REFUSED — the message carries no full PR URL, so an "
                "unknown outcome could not be recorded and a repeat could duplicate "
                "it. Use the full URL, not a short #ref.")
        # Unkeyable: no park can exist for it, so return BEFORE the ledger
        # rather than adding a dependency this path does not need.
        return True, None, None
    try:
        reserved = claim_park(a.message, t["name"], who, canonical=person_of,
                              endpoint=t.get("endpoint"),
                              membership=component_tags(roster, t["name"]))
    except MembershipTooLarge as e:
        return False, "failure", (
            f"{t['name']}: REFUSED — {e}; the no-repeat contract cannot be upheld "
            "for an identity that will not persist. Nothing was sent.")
    except OSError as e:
        return False, "failure", (
            f"{t['name']}: REFUSED — could not reserve the park ({e}); sending now "
            "would be unrepeatable-but-unrecorded. Nothing was sent.")
    if reserved is None:
        return False, "unknown", (
            f"{t['name']}: PARKED — a previous send to {who} is UNSAFE to repeat "
            "(it landed, or may have); check the channel")
    if not reserved:
        if not require_ref:
            return True, None, None
        return False, "failure", (
            f"{t['name']}: REFUSED — no PR reference to key the park on; "
            "nothing was sent")
    return True, None, None


def settler(a, t, who):
    """The matching settlement for reserve_ask, on either transport."""
    def _settle(outcome, detail):
        # A NOTICE never claimed, so it has nothing to supersede — and a row
        # here would be projected into ask history by `_first_ask`.
        if a.kind != "ask":
            return 0
        try:
            return record_asks(a.message, t["name"], outcome=outcome, actor=who,
                               detail=detail, endpoint=t.get("endpoint")) or 0
        except OSError as err:
            # The reservation still stands, so the park holds and the next run
            # refuses rather than repeating. Say which way it fails.
            print(f"  WARNING: {t['name']} stayed PENDING ({err}) — the park holds, "
                  "so a repeat is blocked until it is cleared", file=sys.stderr)
            return None
    return _settle


def claim_park(message: str, reviewer: str, actor: str = None,
               canonical=None, endpoint: str = None,
               membership=None) -> "int | None":
    """Atomically claim the park, or None if someone else already holds it.

    Check-then-append is not a claim: two callers both read "not parked", both
    append `pending`, and both post. The check and the write happen under one
    exclusive lock so exactly one caller can win.
    """
    who = actor or reviewer
    led = ledger_path()
    led.parent.mkdir(parents=True, exist_ok=True)
    if not led.exists():
        os.close(os.open(led, os.O_CREAT | os.O_WRONLY, _LEDGER_MODE))
    with _ledger_lock(led):
        # Overlap-then-UNION runs BEFORE the spelling check: returning early
        # on a spelling match skips the union, losing the history it carries.
        hit = _membership_overlap(led, message, membership) if membership else None
        if hit is not None:
            prior, ident, matched = hit
            # Only the streams that overlapped may learn `unknown`: a PR in the
            # same message that was never sent has nothing possibly-landed.
            record_asks(message, ident.get("reviewer") or reviewer,
                        outcome="unknown", actor=ident.get("actor"),
                        endpoint=ident.get("endpoint"),
                        membership=prior | set(membership), only=matched)
            return None
        if unknown_parked(message, reviewer, who, canonical=canonical,
                          endpoint=endpoint):
            return None
        return record_asks(message, reviewer, outcome="pending", actor=who,
                           endpoint=endpoint, membership=membership)


def _membership_overlap(led: Path, message: str, cand) -> "tuple | None":
    """An unresolved OUTCOME_UNKNOWN record for this notice whose persisted
    membership intersects the candidate's component: (tags, identity, matched),
    where `matched` is the set of canonical (repo, pr) refs that overlapped."""
    refs = {(r, str(n)) for r, n in _refs(message)}
    if not refs or not led.exists():
        return None
    # _streams() is the one reader: folded state, string-validated identity —
    # a raw shape cannot crash this or be re-emitted (44 KB vs 6.7 MB reparse).
    best, matched, all_tags = None, set(), set()
    try:
        streams = _streams(led)
    except OSError:
        return None
    for (repo, pr, _who), st in streams.items():
        if (repo, pr) not in refs and (None, pr) not in refs:
            continue
        outcome = (st["last"] or (None, None))[0]
        if outcome not in _UNSAFE_OUTCOMES:
            continue
        tags = set(st["membership"] or ())
        if not tags or not (tags & set(cand)):
            continue
        matched.add((repo, int(pr)))
        all_tags |= tags
        best = {k: st["last_identity"].get(k) for k in ("reviewer", "actor", "endpoint")}
    if best is None:
        return None
    return all_tags, best, matched

def record_asks(message: str, reviewer: str, outcome: str = "confirmed",
                actor: str = None, detail: str = None,
                endpoint: str = None, membership=None, only=None) -> int:
    """Locked public writer. Every append serialises against the compactor.
    `only`: canonical (repo, pr) set; when given, refs outside it are not written."""
    if not _PR_URL.search(message or ""):
        return 0        # nothing to write, so no path to resolve and no lock
    led = ledger_path()
    with _ledger_lock(led):
        return _append(led, message, reviewer, outcome, actor, detail,
                       endpoint, membership, only)


def _append(p: Path, message: str, reviewer: str, outcome: str,
            actor: str = None, detail: str = None,
            endpoint: str = None, membership=None, only=None) -> int:
    """Log a room ask so pr-unattended can see it. GitHub's timeline records only
    review_requested events, and the owner's rule is to ask in the room and never
    via GitHub — so without this every correctly-routed PR reads NOBODY_EVER_ASKED.

    `outcome="unknown"` records a send that MAY have landed. It must be written:
    the receipt is RetrySafety.UNSAFE, so an unrecorded unknown invites the
    repeat that duplicates the ping."""
    refs = _refs_spelled(message)
    if only is not None:
        refs = [(r, n) for r, n in refs if (_canon_repo(r), n) in only]
    if not refs:
        return 0
    if not isinstance(outcome, str) or outcome not in _KNOWN_OUTCOMES:
        # Type first: a list is unhashable and raised instead of validating.
        # The writer holds the reader's schema, so an unwritable outcome fails.
        raise ValueError(f"outcome {outcome!r} is not one of {sorted(_KNOWN_OUTCOMES)}")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime(_TS_FMT)
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = ""
    for repo, num in sorted(refs):
        row = {"repo": repo, "pr": num,
               "ts": ts, "channel": "room", "outcome": outcome}
        if outcome in _DELIVERY_OUTCOMES:
            row["reviewer"] = reviewer
        if actor:
            row["actor"] = actor
        if endpoint:
            row["endpoint"] = endpoint
        if detail:
            row["detail"] = detail
        if membership:
            row["membership"] = sorted(membership)
        # The COMPLETE row, through the reader's own validator: appending one
        # the reader drops is a silent write, and a list outcome raised instead.
        if _row(row) is None:
            raise ValueError(f"refusing to append a row the reader rejects: {row!r}")
        payload += json.dumps(row) + "\n"
    # One write under O_APPEND: a reader never sees half a batch, and two
    # writers never interleave rows within one.

    # Created private: a fresh ledger under umask 022 would be 0644.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _LEDGER_MODE)
    with os.fdopen(fd, "a") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        _maybe_compact(p)
    except Exception as exc:            # noqa: BLE001 - the append already succeeded

        # Maintenance after a durable write: failing it must not convert that
        # write into a failed claim, parking an ask nobody sent.
        print(f"  WARNING: ledger compaction failed ({type(exc).__name__}: {exc}); "
              "the append stands", file=sys.stderr)
    return len(refs)


def identity_components(roster) -> dict:
    """name -> canonical identity key, over the WHOLE roster and BOTH axes.

    Built before ANY selection, route, capability or allowlist filtering:
    filtering decides which endpoint may send, never who the person is.
    """
    actor_of = _actor_map(roster or {})
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
        return find(a)

    rows = [(k, v) for k, v in (roster or {}).items()
            if isinstance(v, dict) and not k.startswith("_")]
    for name, entry in rows:
        akey = ("actor", actor_of.get(name, name))
        endpoint = durable_endpoint(entry)
        # An off-allowlist or unroutable row still NAMES its person, so it still
        # carries the link; dropping it here is what let a connector disappear.
        union(akey, ("endpoint", endpoint)) if endpoint else find(akey)
    return {name: find(("actor", actor_of.get(name, name))) for name, _ in rows}


#: A persisted membership is attacker-adjacent state read back into a safety
#: decision, so it is bounded and typed; anything else fails closed.
_MAX_TAGS = 512
#: Values are percent-encoded, so `:` is structure and never content — an
#: mxid-shaped actor used to build a tag its own reader rejected.
_TAG_RE = re.compile(r"^(actor:[^\s:]+|endpoint:(?:discord|mx):[^\s:]+)$")
#: Rows written before the encoding: the endpoint tail carried a raw mxid.
_LEGACY_TAG_RE = re.compile(r"^endpoint:(discord|mx):(\S+)$")


class MembershipTooLarge(Exception):
    """A component cannot be represented within the persisted tag bound."""


def _tag(kind: str, *parts: str) -> str:
    """One typed tag whose value segments cannot escape their slot."""
    return kind + ":" + ":".join(quote(p, safe="") for p in parts)


def _canon_tag(t: str) -> "str | None":
    """`t` in canonical encoded form, or None when it is malformed."""
    if _TAG_RE.match(t):
        return t
    m = _LEGACY_TAG_RE.match(t)
    return _tag("endpoint", m.group(1), m.group(2)) if m else None


def valid_tags(value) -> "set | None":
    """The persisted tag set canonicalized, or None when malformed (park stays on).

    Legacy rows are re-encoded so they still overlap a freshly computed set.
    """
    if not isinstance(value, list) or len(value) > _MAX_TAGS:
        return None
    out = set()
    for t in value:
        if not isinstance(t, str):
            return None
        c = _canon_tag(t)
        if c is None:
            return None
        out.add(c)
    return out


def component_tags(roster, name: str) -> set:
    """The candidate's full-roster component, as bounded typed tags.

    Namespaced so an actor named like an id cannot collide with an endpoint.
    """
    comp = identity_components(roster)
    actor_of = _actor_map(roster or {})
    root = comp.get(name)
    tags = {_tag("actor", actor_of.get(name, name))}
    for other, r in comp.items():
        # No root means the row vanished between roster reads. Admitting every
        # component there associates one ask with every remaining reviewer.
        if root is None or r != root:
            continue
        tags.add(_tag("actor", actor_of.get(other, other)))
        endpoint = durable_endpoint((roster or {}).get(other) or {})
        if endpoint:
            scheme, _, rest = endpoint.partition(":")
            tags.add(_tag("endpoint", "discord", rest)
                     if scheme == "discord" else _tag("endpoint", "mx", endpoint))
    if len(tags) > _MAX_TAGS:
        # Truncating discards arbitrary identities and accepts the claim anyway,
        # so a later send to the same person reads as un-parked.
        raise MembershipTooLarge(
            f"{name}: {len(tags)} identity tags exceeds the {_MAX_TAGS} bound")
    return tags


def component_resolver(roster):
    """Any roster name, canonical actor or durable endpoint -> ONE person key.

    The ledger stores raw spellings — sometimes a name, sometimes an endpoint —
    so both axes must land on the same key or an alias steps around a park.
    """
    comp = identity_components(roster)
    actor_of = _actor_map(roster or {})
    axes = {"name": {}, "actor": {}, "endpoint": {}}
    for name, root in comp.items():
        key = f"person:{root[0]}:{root[1]}"
        axes["name"][name] = key
        axes["actor"].setdefault(actor_of.get(name, name), key)
        endpoint = durable_endpoint((roster or {}).get(name) or {})
        if endpoint:
            axes["endpoint"].setdefault(endpoint, key)

    def canon(w):
        # Most specific axis wins: one person's roster key equalling another's
        # endpoint used to overwrite it, so both resolved to the second person.
        for axis in ("name", "actor", "endpoint"):
            if w in axes[axis]:
                return axes[axis][w]
        return w
    return canon


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
        # A non-string is unhashable in the union map, and ONE unrelated bad
        # row used to raise for every reviewer in the batch.
        if isinstance(other, str) and other:
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
    # Through `_refs()`, like the writer: parsing raw here let an upper-cased
    # URL name a "different" PR and permit a repeat ask.
    refs = sorted(_refs(message))
    if not refs:
        return False, ""
    repo, num = refs[0]
    ledger = ledger_path()
    if not ledger.exists():
        return False, ""
    actor_of = _actor_map(roster)
    prior, earliest = set(), None
    try:
        # Canonical actors on one axis: mixing spellings makes the subset test
        # below answer about names rather than people.
        asked = _first_ask(ledger, canonical=lambda w: actor_of.get(w, w))
    except OSError:
        return False, ""
    per_actor = {}
    for (r, n, who), ts in asked.items():
        if n != str(num) or r not in (repo, None):
            continue
        prior.add(who)
        if ts and (who not in per_actor or ts < per_actor[who]):
            per_actor[who] = ts
    if not prior:
        return False, ""
    # Every endpoint in an actor's component, because two aliases of one person
    # can hold different ones and either may be the recorded spelling.
    by_actor: dict = {}
    for k, v in (roster or {}).items():
        ep = durable_endpoint(v)
        if ep:
            by_actor.setdefault(actor_of.get(k, k), set()).add(ep)

    def _ids(t):
        # From the ROSTER too: a caller may pass a bare {"name": ...} target,
        # and deriving from the dict alone left those on the name axis only.
        actor = actor_of.get(t["name"], t["name"])
        got = {actor, t["name"], t.get("endpoint"),
               durable_endpoint((roster or {}).get(t["name"]))}
        got |= by_actor.get(actor, set())
        return {i for i in got if i}

    tids = [_ids(x) for x in targets]
    if not tids or not all(s & prior for s in tids):
        return False, ""            # at least one NEW target -> this IS widening

    # Per target its EARLIEST ask, then the NEWEST across targets: someone
    # else's older ask says nothing here.
    ours = []
    for s in tids:
        got = [per_actor[i] for i in s if per_actor.get(i)]
        if got:
            ours.append(min(got))
    earliest = max(ours) if ours else None
    if earliest is None:
        return False, ""
    try:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(earliest.replace("Z", "+00:00")))
    except ValueError:
        return False, ""
    if age.total_seconds() < minutes * 60:
        return False, ""
    # One human can hold several roster keys (jsun-m IS johnm-desktop). Listing
    # both overstates the pool and re-asks one person under two names.
    seen_actors, unasked = set(), []
    for k, v in sorted((roster or {}).items()):
        if not isinstance(v, dict) or k.startswith("_"):
            continue
        actor = actor_of.get(k, k)
        # The SAME component-wide set the verdict uses: this row's own endpoint
        # alone offered an already-asked person under an earlier-sorting alias.
        ids = {actor, k, durable_endpoint(v)} | by_actor.get(actor, set())
        # keweichen is deliberately never offered as a widen target; the
        # exclusion is pinned by test_keweichen_is_never_offered_as_the_widen_target.
        if (ids & prior) or k == "keweichen":
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


def resolve_body(message, body_file) -> str:
    """The ask body, from argv or from a file — exactly one of the two.

    A body that reached argv has already been through the shell and cannot be
    recovered, so --body-file is the only path that preserves backticks and $.
    """
    if (message is None) == (body_file is None):
        raise SystemExit("ERROR: give exactly one of --message or --body-file")
    if body_file is None:
        return message
    # Imported here, not at module scope: `_REPO` is positional, so a copy run
    # from elsewhere has no src/ path and a top-level import breaks --message too.
    from body_file import read_body_file
    text = read_body_file(body_file)
    if not text.strip():
        raise SystemExit(f"ERROR: --body-file {body_file!r} is empty — refusing to send")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewers", required=True,
                    help="comma-separated roster keys")
    ap.add_argument("--message", default=None)
    ap.add_argument("--body-file", dest="body_file", default=None,
                    help="read the message from a FILE instead of argv. Use it for any "
                         "prose containing backticks, $ or an apostrophe — the shell "
                         "mangles those before this script can see them.")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--allow-single", metavar="REASON", default="",
                    help="deliberately notify ONE reviewer; requires a reason")
    ap.add_argument("--widen-override", metavar="REASON", default="",
                    help="deliberately re-ask the SAME reviewers after 30min")
    ap.add_argument("--kind", choices=("ask", "notice"), default="ask",
                    help="ask (default) requests review; notice tells reviewers "
                         "something about a PR without asking for anything")
    ap.add_argument("--room", default=None,
                    help="room the conversation is actually in. When given, a reviewer whose "
                         "Stand is not a member THERE is REFUSED rather than silently notified "
                         "in their recorded room — correctly addressed, wrong venue.")
    a = ap.parse_args()
    a.message = resolve_body(a.message, a.body_file)
    names = list(dict.fromkeys(n.strip() for n in a.reviewers.split(",") if n.strip()))
    roster = load_roster()
    # resolve() dedups per person, so the two-reviewer gate counts PEOPLE, not
    # roster rows; gates then run on RESOLVED targets, never a partial batch.
    targets, refusal_rc = resolve(names, roster)
    # A read-only approval looks identical in the UI and discharges nothing, so
    # ask the repo named in the message rather than trusting a cached tier.
    if a.kind == "ask" and targets:
        refs = _PR_URL.findall(a.message or "")
        if not refs:
            # Every other refusal path here prints; the one case that cannot be
            # checked must not be the one case that is silent.
            print("gate capability NOT CHECKED: the message names no "
                  "github.com/<owner>/<repo>/pull/<n> URL, so there is no repo to "
                  "ask about — an unchecked send is not a checked one",
                  file=sys.stderr)
        if refs:
            repo = refs[0][0]
            roster_now = load_roster()
            kept = []
            for t in targets:
                login, why_login = _github_login(t["name"], roster_now)
                can, why_cap = gate_capability(repo, login)
                if login != t["name"]:
                    print(f"{t['name']}: probing GitHub as {login} ({why_login})",
                          file=sys.stderr)
                if can is False:
                    detail = (why_cap if why_cap == "not a collaborator"
                              else f"{why_cap}-only")
                    print(f"CANNOT GATE '{t['name']}': {detail} on {repo} — an "
                          f"approval from this account does not count toward the "
                          f"required approvals", file=sys.stderr)
                    refusal_rc = max(refusal_rc, 7)
                    continue
                if can is None:
                    print(f"{t['name']}: gate capability {why_cap} on {repo} — "
                          f"sending, but this is not a confirmation it can approve",
                          file=sys.stderr)
                kept.append(t)
            targets = kept
    # Gates run on RESOLVED targets before any send, so no partial batch notifies
    # one person; plan mode is exempt because only a real ASK can strand a PR.
    # The two-reviewer rule exists so one person being busy cannot stall a PR.
    # A notice asks for nothing, so it cannot stall anything by going to one.
    if a.send and a.kind == "ask" and len(targets) < 2 and not a.allow_single:
        print(f"REFUSED: {len(targets)} reviewer(s) resolved from {names!r}; the rule is at "
              "least TWO, so one being busy cannot stall the PR. Name another reviewer, "
              "or pass --allow-single '<reason>'.", file=sys.stderr)
        # A failed name is WHY the count is short and is the actionable half.
        # `> 0` not `or`: refusal codes are positive, 0 means nothing refused.
        return refusal_rc if refusal_rc > 0 else 5
    if a.allow_single and len(targets) < 2:
        print(f"single-reviewer ask allowed: {a.allow_single}", file=sys.stderr)
    if a.send and a.kind == "ask":
        loose = unrecordable_pr_refs(a.message)
        if loose:
            print("REFUSED: this ask would DELIVER and record NOTHING. record_asks() logs "
                  "only full github.com/<owner>/<repo>/pull/<n> URLs, so pr-unattended would "
                  "read the PR as never asked. Refused reference(s), and the form that works: "
                  + "; ".join(f"{tok} -> {fix}" for tok, fix in loose), file=sys.stderr)
            return 7
    stale, why = _stale_repeat_ask(a.message, targets, load_roster()) if a.kind == "ask" else (False, "")
    if stale and not a.widen_override:
        print(f"REFUSED: {why} Re-asking the same people is not escalation — "
              "name someone new, or pass --widen-override '<reason>'.", file=sys.stderr)
        return 6
    failures = unlogged = unknowns = 0
    # One person may hold several roster spellings; the park keys the endpoint,
    # so resolve the canonical actor once rather than per send.
    actors = _actor_map(load_roster())
    # Retry admission keys the PERSON, not a spelling: an outcome-unknown park
    # must block every alias and endpoint in that person's component.
    person_of = component_resolver(load_roster())
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
            who = actors.get(t["name"], t["name"])
            # Claim BEFORE the POST: a later reservation cannot cover a crash
            # between the two. Shared with the Matrix path — see reserve_ask.
            proceed, bucket, note = reserve_ask(a, t, who, person_of, load_roster())
            if not proceed:
                print(note, file=sys.stderr)
                if bucket == "unknown":
                    unknowns += 1
                else:
                    failures += 1
                continue
            _settle = settler(a, t, who)

            try:
                p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            except OSError as e:
                # No child ran, so no POST was possible: release the reservation
                # rather than stranding an ask that never started.
                _settle("failed", f"no spawn: {type(e).__name__}")
                print(f"{t['name']}: SEND FAILED before spawn ({type(e).__name__}: {e})"
                      " — nothing was sent; safe to retry", file=sys.stderr)
                failures += 1
                continue
            except subprocess.TimeoutExpired as e:
                # A timeout is not a failure: the post may have landed, so the
                # reservation settles to UNKNOWN and the batch continues.
                _settle("unknown", f"timeout: {type(e).__name__}")
                print(f"{t['name']}: UNKNOWN outcome ({type(e).__name__}) — the post "
                      "may have landed; not retrying", file=sys.stderr)
                unknowns += 1
                continue
            if p.returncode == 3:
                # A CONFIRMED receipt with a message id: the post LANDED and
                # merely missed the mention, so a repeat duplicates it.
                _settle("unknown", "posted without the target in mentions")
                print(f"{t['name']}: LANDED BUT DID NOT TRIGGER on channel "
                      f"{t['channel']} — the post exists and must not be repeated; "
                      "the mention resolved to someone else, or to nobody. "
                      f"{(p.stderr or '').strip() or 'no stderr'}", file=sys.stderr)
                unknowns += 1
                continue
            if p.returncode == 0:
                # The child prints the message id. Swallowing it leaves no
                # artifact naming what actually landed.
                mid = (p.stdout or "").strip()
                print(f"{t['name']}: SENT to channel {t['channel']}"
                      + (f" as message {mid}" if mid else ""))
                # Same bookkeeping as the Matrix path: without it a delivered
                # Discord ask reads as NOBODY_EVER_ASKED to pr-unattended.
                try:
                    n_logged = (record_asks(a.message, t["name"], actor=who,
                                            endpoint=t.get("endpoint"))
                                if a.kind == "ask" else 0)
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
                # OUTCOME_UNKNOWN: the post may have landed, and the receipt is
                # UNSAFE to retry, so the reservation settles to UNKNOWN.
                _settle("unknown", "child reported an unknown outcome")
                unknowns += 1
                print(f"{t['name']}: OUTCOME UNKNOWN on channel {t['channel']} — "
                      f"the post may have landed; {retry_clause(a.kind)}. "
                      f"{(p.stderr or '').strip() or 'no stderr'}",
                      file=sys.stderr)
            elif p.returncode in _PROVEN_NOT_DELIVERED:
                # Only these prove nothing was posted. rc 1 does not: the
                # interpreter exits 1 on any uncaught exception, post-POST too.
                _settle("failed", f"child rc={p.returncode}")
                print(f"{t['name']}: SEND FAILED rc={p.returncode} "
                      f"{(p.stderr or '').strip() or 'no stderr'}", file=sys.stderr)
                failures += 1
            else:
                # A crash, a signal, or a code nobody assigned: the post MAY
                # have landed, so it parks rather than releasing.
                _settle("unknown", f"ambiguous child exit rc={p.returncode}")
                unknowns += 1
                print(f"{t['name']}: AMBIGUOUS EXIT rc={p.returncode} — the post may "
                      f"have landed; {retry_clause(a.kind)}. "
                      f"{(p.stderr or '').strip() or 'no stderr'}",
                      file=sys.stderr)
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
        who = actors.get(t["name"], t["name"])
        # Same reservation as the Discord path: an alias on this transport must
        # not walk past a park an ask on the other one is still holding.
        proceed, bucket, note = reserve_ask(a, t, who, person_of, load_roster(),
                                            require_ref=False)
        if not proceed:
            print(note, file=sys.stderr)
            if bucket == "unknown":
                unknowns += 1
            else:
                failures += 1
            continue
        _settle = settler(a, t, who)
        # Per-target boundary: a raise here would drop every remaining target
        # AND skip the return, so the caller sees no asks and no failure code.
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            # May have landed. Park it rather than counting a clean failure —
            # the Discord path has always done this; this one used to not.
            _settle("unknown", "timeout: TimeoutExpired")
            print(f"{t['name']}: UNKNOWN outcome (room_ops exceeded the 60s timeout)"
                  f" — the post may have landed; {retry_clause(a.kind)}",
                  file=sys.stderr)
            unknowns += 1
            continue
        except OSError as e:
            _settle("failed", f"no spawn: {type(e).__name__}")
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
        state = ""
        if isinstance(payload, dict):
            ok = bool(payload.get("ok"))
            event = str(payload.get("event_id") or "")
            reason = str(payload.get("reason") or "")
            state = str(payload.get("state") or "")
            fallback = "no reason reported"
        else:
            fallback = "unparseable room_ops output"
        # The receipt tri-state outranks `ok`: a timeout inside room_ops and a
        # 200 without an event id both MAY have landed, so the park must hold.
        if isinstance(payload, dict) and (state == "unknown" or (ok and not event)):
            detail = reason or ("posted without an event id" if ok else fallback)
            _settle("unknown", f"room_ops {state or 'unconfirmed'}: {detail[:80]}")
            print(f"{t['name']}: UNKNOWN outcome ({detail[:80]}) — the post may have "
                  f"landed; {retry_clause(a.kind)}", file=sys.stderr)
            unknowns += 1
            continue
        # room_ops reports refusals in-band: rc 0, empty stderr, ok:false + reason.
        # Printing stderr alone renders every such refusal as a blank line.
        if ok:
            print(f"{t['name']}: ok=True event={event[:24]}")
            # The ask already happened; a lost ledger write makes pr-unattended
            # report NOBODY_EVER_ASKED for someone who was asked. Loud, not fatal.
            # Supersede the reservation this send claimed, same as Discord.
            n_logged = _settle("confirmed", f"event={event[:24]}")
            if n_logged is None:
                unlogged += 1
                print(f"  WARNING: the ask to {t['name']} SUCCEEDED but was NOT recorded "
                      "— pr-unattended will under-report this PR as unasked",
                      file=sys.stderr)
            else:
                if a.kind == "notice":
                    print(f"  notice (not an ask) — nothing recorded for {t['name']}",
                          file=sys.stderr)
                elif n_logged:
                    print(f"  logged {n_logged} PR ask(s) for {t['name']}", file=sys.stderr)
                else:
                    # Not counted as a failure: an ask need not concern a PR. But it
                    # must not be silent -- an unrecorded PR ask reads as never-asked.
                    print(f"  note: nothing recorded for {t['name']} — the message "
                          f"names no github.com/<owner>/<repo>/pull/<n> URL, so any "
                          f"PR it refers to will read as unasked", file=sys.stderr)
        elif isinstance(payload, dict):
            # room_ops refuses in-band at rc 0: a parsed ok=false PROVES nothing
            # was posted, so the reservation releases and a retry stays allowed.
            detail = reason or p.stderr.strip()[:120] or fallback
            _settle("failed", f"room_ops ok=false: {detail[:80]}")
            print(f"{t['name']}: ok=False reason={detail}", file=sys.stderr)
            failures += 1
        else:
            # Unreadable output says nothing about whether the post landed: settle
            # UNKNOWN so the park holds. The failure exit code is pinned elsewhere.
            detail = reason or p.stderr.strip()[:120] or fallback
            _settle("unknown", f"unparseable room_ops output: {detail[:60]}")
            print(f"{t['name']}: ok=False reason={detail}", file=sys.stderr)
            failures += 1
    if unlogged:
        print(f"{unlogged} ask(s) were delivered but not recorded — the ledger "
              "under-reports and pr-unattended will read this PR as unasked",
              file=sys.stderr)
    if unknowns:
        print(f"{unknowns} send(s) are UNSAFE to repeat — each landed or may have; "
              f"{retry_clause(a.kind)}.", file=sys.stderr)
    # Unknown outranks a definite failure in a mixed batch: a failure is safe to
    # retry and an unknown is not, so collapsing to 1 invites the duplicate.
    if unknowns:
        return 4
    if failures or unlogged:
        return 1
    return refusal_rc


if __name__ == "__main__":
    sys.exit(main())
