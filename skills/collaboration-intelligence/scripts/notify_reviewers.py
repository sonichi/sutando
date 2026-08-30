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
                                 "first_ask_outcome": None, "n": 0})
            st["last"] = (outcome, ts)
            st["n"] += 1
            # A row predating the outcome field records a send that happened:
            # absence is legacy, not a claim that nothing was posted.
            if (outcome is None or outcome in _DID_ASK) and (
                    st["first_ask"] is None or ts < st["first_ask"]):
                st["first_ask"], st["first_ask_outcome"] = ts, outcome
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


def _rows_for(st: dict) -> int:
    """Rows this stream costs after compaction: its first ask and its last."""
    n = 1 if st["first_ask"] is not None else 0
    if st["last"] and (not n or st["last"] != (st["first_ask_outcome"], st["first_ask"])):
        n += 1
    return n or 1


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
        keep = []
        if st["first_ask"] is not None:
            keep.append((st["first_ask_outcome"], st["first_ask"]))
        if st["last"] and (not keep or st["last"] != keep[0]):
            keep.append(st["last"])
        for outcome, ts in keep:
            # The reader's normalized string: int() renamed "007" to "7".
            rows.append(json.dumps({"repo": repo, "pr": num, "reviewer": who,
                                    "actor": who, "ts": ts, "channel": "room",
                                    "outcome": outcome}))
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
        latest = _latest_outcomes(led)
    except OSError:
        # Cannot read the park state, so cannot prove this was NOT parked. Fail
        # closed: refusing a send is recoverable, a duplicated unsafe post is not.
        return True
    canon = canonical or (lambda w: w)
    for (repo, num, row_who), (outcome, _ts) in latest.items():
        # Endpoint first — it names the recipient under any spelling; the name
        # comparison stays for legacy rows carrying only `reviewer`.
        if not (endpoint and row_who == endpoint):
            if canon(row_who) != canon(who) and row_who not in (who, reviewer):
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


def claim_park(message: str, reviewer: str, actor: str = None,
               canonical=None, endpoint: str = None) -> "int | None":
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
        if unknown_parked(message, reviewer, who, canonical=canonical,
                          endpoint=endpoint):
            return None
        return record_asks(message, reviewer, outcome="pending", actor=who,
                           endpoint=endpoint)


def record_asks(message: str, reviewer: str, outcome: str = "confirmed",
                actor: str = None, detail: str = None,
                endpoint: str = None) -> int:
    """Locked public writer. Every append serialises against the compactor."""
    if not _PR_URL.search(message or ""):
        return 0        # nothing to write, so no path to resolve and no lock
    led = ledger_path()
    with _ledger_lock(led):
        return _append(led, message, reviewer, outcome, actor, detail,
                       endpoint)


def _append(p: Path, message: str, reviewer: str, outcome: str,
            actor: str = None, detail: str = None,
            endpoint: str = None) -> int:
    """Log a room ask so pr-unattended can see it. GitHub's timeline records only
    review_requested events, and the owner's rule is to ask in the room and never
    via GitHub — so without this every correctly-routed PR reads NOBODY_EVER_ASKED.

    `outcome="unknown"` records a send that MAY have landed. It must be written:
    the receipt is RetrySafety.UNSAFE, so an unrecorded unknown invites the
    repeat that duplicates the ping."""
    refs = _refs(message)
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
        row = {"repo": repo, "pr": num, "reviewer": reviewer,
               "ts": ts, "channel": "room", "outcome": outcome}
        if actor:
            row["actor"] = actor
        if endpoint:
            row["endpoint"] = endpoint
        if detail:
            row["detail"] = detail
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
    refs = _PR_URL.findall(message or "")
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
    names = {actor_of.get(x["name"], x["name"]) for x in targets}
    if not names or not names.issubset(prior):
        return False, ""            # at least one NEW name -> this IS widening

    # NEWEST among the targets: someone else's older ask says nothing here.
    ours = [per_actor[n] for n in names if per_actor.get(n)]
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
        # keweichen is deliberately never offered as a widen target; the
        # exclusion is pinned by test_keweichen_is_never_offered_as_the_widen_target.
        if actor in prior or k == "keweichen":
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
    names = list(dict.fromkeys(n.strip() for n in a.reviewers.split(",") if n.strip()))
    roster = load_roster()
    targets, refusal_rc = resolve(names, roster)
    # "At least TWO" means two PEOPLE, not two roster rows: `--reviewers d,d`
    # and two same_actor_as aliases share one endpoint and ping one Stand twice.
    _actors = _actor_map(roster)
    # EITHER axis is one person: a composite key collapses only when BOTH
    # match, which is the easy case and not the one that pings a Stand twice.
    _seen_actor, _seen_endpoint, _deduped = set(), set(), []
    for t in targets:
        actor = _actors.get(t["name"], t["name"])
        endpoint = (t.get("channel") or t.get("room"),
                    t.get("discord_id") or t.get("stand"))
        if actor in _seen_actor or endpoint in _seen_endpoint:
            continue
        _seen_actor.add(actor)
        _seen_endpoint.add(endpoint)
        _deduped.append(t)
    targets = _deduped
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
    failures = unlogged = unknowns = 0
    # One person may hold several roster spellings; the park keys the endpoint,
    # so resolve the canonical actor once rather than per send.
    actors = _actor_map(load_roster())
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
            if not _PR_URL.search(a.message):
                # An UNKNOWN outcome here could not be parked, so a retry would
                # duplicate a post that may have landed. Refuse before sending.
                print(f"{t['name']}: REFUSED — the message carries no full PR URL, "
                      "so an unknown outcome could not be recorded and a repeat "
                      "could duplicate it. Use the full URL, not a short #ref.",
                      file=sys.stderr)
                failures += 1
                continue
            who = actors.get(t["name"], t["name"])
            # Claim BEFORE the POST can happen: a reservation written after it
            # cannot cover a crash, or a write failure, between the two.
            try:
                reserved = claim_park(a.message, t["name"], who,
                                      canonical=lambda w: actors.get(w, w),
                                      endpoint=t.get("stand"))
            except OSError as e:
                reserved = 0
                print(f"{t['name']}: REFUSED — could not reserve the park ({e}); "
                      "sending now would be unrepeatable-but-unrecorded. Nothing "
                      "was sent.", file=sys.stderr)
                failures += 1
                continue
            if reserved is None:
                print(f"{t['name']}: PARKED — a previous send to {who} is UNSAFE "
                      "to repeat (it landed, or may have); check the channel",
                      file=sys.stderr)
                unknowns += 1
                continue
            if not reserved:
                print(f"{t['name']}: REFUSED — no PR reference to key the park on; "
                      "nothing was sent", file=sys.stderr)
                failures += 1
                continue

            def _settle(outcome, detail):
                """Supersede the reservation. Append-only, so this is atomic."""
                try:
                    record_asks(a.message, t["name"], outcome=outcome, actor=who,
                                detail=detail)
                except OSError as err:
                    # The reservation still stands, so the park holds and the
                    # next run refuses rather than repeating. Say which way it fails.
                    print(f"  WARNING: {t['name']} stayed PENDING ({err}) — the "
                          "park holds, so a repeat is blocked until it is cleared",
                          file=sys.stderr)

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
                    n_logged = record_asks(a.message, t["name"], actor=who)
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
                      "the post may have landed; the park holds so a repeat does "
                      f"not duplicate it. {(p.stderr or '').strip() or 'no stderr'}",
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
                      f"have landed; parked. {(p.stderr or '').strip() or 'no stderr'}",
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
    if unknowns:
        print(f"{unknowns} send(s) are UNSAFE to repeat — each landed or may have. "
              "The park is reserved before the post, so a repeat is refused; check "
              "the channel before clearing one.", file=sys.stderr)
    # Unknown outranks a definite failure in a mixed batch: a failure is safe to
    # retry and an unknown is not, so collapsing to 1 invites the duplicate.
    if unknowns:
        return 4
    if failures or unlogged:
        return 1
    return refusal_rc


if __name__ == "__main__":
    sys.exit(main())
