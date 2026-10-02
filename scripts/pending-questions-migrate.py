#!/usr/bin/env python3
"""Triage the per-host pending-questions.md before its open entries move to the
owner's "Pending questions" room database.

  python3 scripts/pending-questions-migrate.py [--dry-run]   # default: print only
  python3 scripts/pending-questions-migrate.py --apply       # after the owner reviewed a dry run

Each open entry (the reminder's own reading of the file, plus the entries it
already skips because their title says resolved) is classified, first match wins:
  already-migrated  — it carries an ask id or names its row; nothing to do
  self-resolved     — the title leads with RESOLVED / SELF-RESOLVED (the reader's rule)
  live              — an open PR it names, or none and asked within --window-days
  unknown-PR        — a PR it names could not be looked up; nothing is done (fail closed)
  stale-merged-PR   — every PR it names is closed and at least one merged
  stale-closed-PR   — every PR it names was closed unmerged
  past-window       — no PR decides it and it is older than --window-days
PR states come from `gh api repos/<repo>/pulls/<n>`. A 404 there is "not a PR"
only when `repos/<repo>/issues/<n>` returns a plain issue; otherwise it is
unknown, as is every other failure. A 403 backs off 3 minutes and retries. The dry run
prints the proposed action per entry and, with --plan-out, saves the plan;
it writes nothing else. --apply takes only a saved plan (--plan): it re-queries
nothing, and changes an entry only while its text still hashes to what the plan
saw (located by kind, title and occurrence, so duplicate headings are distinct).
It creates the live `## ` rows (ask id `legacy-<hash>`, idempotent) and marks
them moved, and marks self-resolved and stale `## ` entries resolved; past-window
too when the plan was made with --close-past-window. Bullet entries are listed,
never changed and never given a row. A second --apply changes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pending_questions_ledger as ledger  # noqa: E402
from pending_questions_store import (GuardFailed, active_region, entry_ask_id,  # noqa: E402
                                     legacy_ask_id, row_body, row_id)

CLASSES = ("already-migrated", "self-resolved", "live", "unknown-PR", "stale-merged-PR",
           "stale-closed-PR", "past-window")
UNKNOWN = "unknown"
BACKOFF_SEC = 180
TRIES = 3
_PR_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)|(?<![\w/&])#(\d{2,6})\b")
_DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)(?!\d)")


def _reader():
    spec = importlib.util.spec_from_file_location("cpq", REPO / "src" / "check-pending-questions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pr_refs(text: str, repo: str) -> list:
    out = []
    for m in _PR_RE.finditer(text):
        ref = (m.group(1), int(m.group(2))) if m.group(1) else (repo, int(m.group(3)))
        if ref not in out:
            out.append(ref)
    return out


def asked_on(title: str, body: str):
    m = _DATE_RE.search(title) or _DATE_RE.search(body)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class GhPrs:
    """PR state by (repo, number) through `gh api`, cached: open, merged, closed,
    None only when the issues endpoint proves the number is a plain issue, else
    UNKNOWN. A pulls 404 alone proves nothing: GitHub answers 404 for a private
    or unauthorized PR too."""

    def __init__(self, runner=subprocess.run, sleep=time.sleep, log=print):
        self.runner, self.sleep, self.log, self.cache = runner, sleep, log, {}

    def _get(self, path: str) -> tuple:
        """("ok", json) | ("404", None) | (UNKNOWN, None); a 403 backs off and retries."""
        for attempt in range(TRIES):
            try:
                r = self.runner(["gh", "api", path], capture_output=True, text=True)
            except OSError:
                return UNKNOWN, None
            err = (r.stderr or "") + (r.stdout if r.returncode else "")
            if r.returncode == 0:
                try:
                    d = json.loads(r.stdout)
                except ValueError:
                    return UNKNOWN, None
                return ("ok", d) if isinstance(d, dict) else (UNKNOWN, None)
            if "HTTP 404" in err or "Not Found" in err:
                return "404", None
            if "HTTP 403" in err or "rate limit" in err.lower():
                if attempt + 1 < TRIES:
                    self.log(f"gh: 403 on {path}; backing off {BACKOFF_SEC}s", file=sys.stderr)
                    self.sleep(BACKOFF_SEC)
                continue
            return UNKNOWN, None
        return UNKNOWN, None

    def state(self, repo: str, n: int):
        if (repo, n) in self.cache:
            return self.cache[(repo, n)]
        kind, d = self._get(f"repos/{repo}/pulls/{n}")
        if kind == "ok":
            st = "merged" if d.get("merged_at") else ("open" if d.get("state") == "open" else "closed")
        elif kind == "404":
            kind, d = self._get(f"repos/{repo}/issues/{n}")
            st = None if kind == "ok" and "pull_request" not in d else UNKNOWN
        else:
            st = UNKNOWN
        self.cache[(repo, n)] = st
        return st


SELF_RESOLVED_WHY = "its title says resolved"
PAST_WINDOW_WHY = "past its 14-day window (closed in cleanup)"


def classify(q: dict, prs, now: float, window_days: float, repo: str, title_resolved=None) -> tuple:
    """(class, reason) for one waiting entry."""
    if entry_ask_id(q.get("body", "")):
        return "already-migrated", "carries an ask id or names its row"
    if title_resolved is not None and title_resolved(q["title"]):
        return "self-resolved", SELF_RESOLVED_WHY
    text = f"{q['title']}\n{q.get('body', '')}"
    states = {f"{r}#{n}": prs.state(r, n) for r, n in pr_refs(text, repo)}
    known = {k: v for k, v in states.items() if v and v != UNKNOWN}
    unknown = [k for k, v in states.items() if v == UNKNOWN]
    if any(v == "open" for v in known.values()):
        return "live", "open PR " + ", ".join(k for k, v in known.items() if v == "open")
    if unknown:
        return "unknown-PR", "could not look up " + ", ".join(unknown)
    if known and any(v == "merged" for v in known.values()):
        return "stale-merged-PR", "PR(s) " + ", ".join(f"{k} {v}" for k, v in known.items())
    if known:
        return "stale-closed-PR", "PR(s) " + ", ".join(f"{k} {v}" for k, v in known.items())
    asked = asked_on(q["title"], q.get("body", ""))
    if asked is not None and now - asked > window_days * 86400:
        return "past-window", f"asked {datetime.fromtimestamp(asked, tz=timezone.utc):%Y-%m-%d}, " \
                              f"older than {window_days:g} days, no PR decides it"
    return "live", "no closed PR, within the window" if asked else "no closed PR, undated"


def _headings(text: str, title: str) -> list:
    return list(re.finditer(rf"^## {re.escape(title)}[ \t]*$", text, re.MULTILINE))


def _section_span(text: str, m) -> tuple:
    nxt = re.compile(r"^## |^# ", re.MULTILINE).search(text, m.end())
    return m.start(), nxt.start() if nxt else len(text)


def _bullet_spans(text: str, title: str) -> list:
    out = []
    for m in re.finditer(rf"^[ \t]*-[ \t]+\*\*\[{re.escape(title)}\]", text, re.MULTILINE):
        end = text.find("\n", m.end())
        out.append((m.start(), end if end != -1 else len(text)))
    return out


def spans(text: str, kind: str, title: str) -> list:
    if kind == "bullet":
        return _bullet_spans(text, title)
    return [_section_span(text, m) for m in _headings(text, title)]


def _sha(chunk: str) -> str:
    return hashlib.sha256(chunk.encode()).hexdigest()


def identify(text: str, q: dict, taken: set) -> tuple:
    """(nth occurrence, sha) of the entry `q` was read from: the first span of
    its kind and title, not already taken, whose body is q's."""
    for i, (a, b) in enumerate(spans(text, q["kind"], q["title"])):
        chunk = text[a:b]
        body = chunk.strip() if q["kind"] == "bullet" else chunk.partition("\n")[2].strip()
        if (q["kind"], q["title"], i) not in taken and body == q["body"]:
            taken.add((q["kind"], q["title"], i))
            return i, _sha(chunk)
    return None, None


ACTIONS = {
    "already-migrated": "nothing (already migrated)",
    "self-resolved": "mark resolved in the file (its title says resolved); no row",
    "live": "create an Open row in the room database; mark the file entry moved",
    "unknown-PR": "nothing: a PR state is unknown (fail closed); rerun later",
    "stale-merged-PR": "mark resolved in the file (its PR merged); no row",
    "stale-closed-PR": "mark resolved in the file (its PR closed unmerged); no row",
    "past-window": "leave open in the file; listed for the owner to resolve or keep",
}


def action_of(r: dict, close_past_window: bool) -> str:
    if r["kind"] == "bullet" and r["class"] != "already-migrated":
        return "nothing: a bullet entry is listed for the owner, never changed or given a row"
    if r["nth"] is None:
        return "nothing: the entry could not be located"
    if r["class"] == "past-window" and close_past_window:
        return f"mark resolved in the file ({PAST_WINDOW_WHY}); no row"
    return ACTIONS[r["class"]]


def triage(questions: list, prs, now: float, window_days: float, repo: str, title_resolved=None,
           text: str = "", host: Optional[str] = None) -> list:
    out, taken = [], set()
    for q in questions:
        q = {**q, "kind": q.get("kind", "section")}
        cls, why = classify(q, prs, now, window_days, repo, title_resolved)
        nth, sha = identify(text, q, taken)
        out.append({**q, "class": cls, "why": why, "ask_id": legacy_ask_id(q["title"], q["body"], host, nth or 0),
                    "nth": nth, "sha": sha})
    return out


def report(rows: list, ledger_file: Path, close_past_window: bool = False) -> list:
    counts = {c: sum(r["class"] == c for r in rows) for c in CLASSES}
    lines = [f"ledger: {ledger_file}", f"waiting entries: {len(rows)}",
             "counts: " + ", ".join(f"{c}={n}" for c, n in counts.items()), ""]
    for r in rows:
        lines.append(f"[{r['class']}] {r['title'][:100]} — {action_of(r, close_past_window)} ({r['why']})")
    live = [r for r in rows if r["class"] == "live" and action_of(r, close_past_window) == ACTIONS["live"]]
    lines += ["", f"rows it would create ({len(live)}):"]
    for r in live:
        lines.append(f"  Name={r['title'][:100]} | Status=Open | Priority=Medium | Ask id={r['ask_id']}"
                     f" | row={row_id(r['ask_id'])}")
    return lines


PLAN_VERSION = 2





KINDS = ("section", "bullet")


_DIGEST = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")


def _text(v) -> bool:
    """A string with no control character (C0, DEL, C1) and no lone surrogate, so it encodes as UTF-8."""
    return isinstance(v, str) and not _CONTROL.search(v)


def _line(v) -> bool:
    return _text(v) and bool(v)


_BODY_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


def safe_body(text: str) -> str:
    """A ledger body as the database may hold it: newlines and tabs kept, every other control
    character and lone surrogate replaced with U+FFFD."""
    return _BODY_UNSAFE.sub("\ufffd", text)


def _entry_ok(e) -> bool:
    return (isinstance(e, dict) and e.get("kind") in KINDS and e.get("class") in CLASSES
            and _line(e.get("title")) and e["title"] == e["title"].strip()
            and _text(e.get("why")) and _text(e.get("ask_id")) and isinstance(e.get("body"), str)
            and (e.get("sha") is None or (isinstance(e.get("sha"), str) and _DIGEST.fullmatch(e["sha"])))
            and "nth" in e and "sha" in e and (e["nth"] is None) == (e["sha"] is None)
            and (e["nth"] is None or (type(e["nth"]) is int and e["nth"] >= 0 and isinstance(e["sha"], str))))


def plan_fits(plan: dict, host: Optional[str]) -> bool:
    """This version, made on this (named) host, with every field well formed, checked before any write."""
    if not isinstance(plan, dict):
        return False
    entries = plan.get("entries")
    if not (type(plan.get("version")) is int and plan.get("version") == PLAN_VERSION and _line(host)
            and plan.get("host") == host and _line(plan.get("ledger")) and Path(plan["ledger"]).is_absolute()
            and isinstance(plan.get("ledger_sha256"), str) and _DIGEST.fullmatch(plan["ledger_sha256"])
            and type(plan.get("close_past_window")) is bool
            and isinstance(entries, list) and all(_entry_ok(e) for e in entries)):
        return False
    selectors = [(e["kind"], e["title"], e["nth"]) for e in entries if e["nth"] is not None]
    return len(selectors) == len(set(selectors))


def make_plan(rows: list, ledger_file: Path, text: str, close_past_window: bool,
              host: Optional[str] = None) -> dict:
    return {"version": PLAN_VERSION, "host": host,
            "ledger": str(Path(ledger_file).resolve()), "ledger_sha256": _sha(text), "close_past_window": close_past_window,
            "entries": [{k: r[k] for k in ("kind", "title", "nth", "sha", "class", "why", "ask_id", "body")}
                        for r in rows]}


def _with_status(text: str, r: dict, status: str) -> str:
    """The planned entry's status line set, only while it still hashes as planned
    and still sits in the active region (an archived entry is never rewritten)."""
    found = spans(text, r["kind"], r["title"])
    if r["nth"] is None or r["nth"] >= len(found) or _sha(text[slice(*found[r["nth"]])]) != r["sha"]:
        raise ledger.LedgerError(f"changed since the plan: {r['title'][:80]!r}")
    a, b = found[r["nth"]]
    if a >= len(active_region(text)):
        raise ledger.LedgerError(f"no longer in the active region: {r['title'][:80]!r}")
    line = f"**Status:** {status} (pending-questions-migrate)"
    st = re.compile(r"^\*\*Status:\*\*.*$", re.MULTILINE).search(text, a, b)
    if st:
        return text[:st.start()] + line + text[st.end():]
    return text[:b].rstrip("\n") + "\n\n" + line + "\n\n" + text[b:]


def _bind(r: dict, text: str, host: Optional[str]) -> dict:
    """The entry as the ledger holds it now: title and body from the planned span (sha-checked),
    the ask id recomputed, `why` on one line. The plan only selects; it never supplies content."""
    found = spans(text, r["kind"], r["title"]) if r["nth"] is not None else []
    if r["nth"] is None or r["nth"] >= len(found) or _sha(text[slice(*found[r["nth"]])]) != r["sha"]:
        return {**r, "unbound": True}
    chunk = text[slice(*found[r["nth"]])]
    raw = chunk.strip() if r["kind"] == "bullet" else chunk.partition("\n")[2].strip()
    return {**r, "body": safe_body(raw), "why": " ".join(str(r["why"]).split()), "span": found[r["nth"]][0],
            "ask_id": legacy_ask_id(r["title"], raw, host, r["nth"])}


def apply(plan: dict, ledger_file: Path, store, host: Optional[str] = None) -> list:
    """Carry out a saved plan, entry by entry, each guarded by its planned hash."""
    if not plan_fits(plan, host):
        return [f"refused: this plan is not a well-formed version {PLAN_VERSION} plan made on host {host!r}; "
                f"re-run the dry run there"]
    done, cpw = [], plan["close_past_window"]
    ledger_file = Path(ledger_file).resolve()
    if Path(plan["ledger"]).resolve() != ledger_file:
        return [f"refused: the plan is for {plan['ledger']}, not {ledger_file}"]
    if store is not None and getattr(store, "host", None) != host:
        return [f"refused: the store is for host {getattr(store, 'host', None)!r}, not {host!r}"]
    text = ledger_file.read_text(encoding="utf-8") if ledger_file.exists() else ""
    entries = [_bind(r, text, host) for r in plan["entries"]]
    acting = [r["ask_id"] for r in entries if not r.get("unbound") and r["class"] == "live"]
    targets = [r["span"] for r in entries if not r.get("unbound")]
    if len(acting) != len(set(acting)) or len(targets) != len(set(targets)):
        return ["refused: two planned entries resolve to one ledger entry or ask id; re-run the dry run"]
    for r in entries:
        if r.get("unbound") and not action_of(r, cpw).startswith("nothing"):
            done.append(f"skipped: changed since the plan: {r['title'][:80]!r}")
            continue
        action = action_of(r, cpw)
        if action.startswith("nothing") or action == ACTIONS["past-window"]:
            done.append(f"unchanged [{r['class']}]: {r['title'][:80]}")
            continue
        if r["class"] == "live":
            if store is None:
                done.append(f"unchanged (no room database) [live]: {r['title'][:80]}")
                continue
            done.append(_apply_live(r, ledger_file, store))
            continue
        status = f"resolved — {PAST_WINDOW_WHY if r['class'] == 'past-window' else r['why']}"
        err = ledger.update(ledger_file, lambda t, r=r, status=status: _with_status(t, r, status))
        done.append(f"{'skipped: ' + err if err else 'resolved'}: {r['title'][:80]}")
    return done


def _apply_live(r: dict, ledger_file: Path, store) -> str:
    """Two phases. The row is made Open outside the ledger lock; then, under the
    lock and with no network call, the target entry alone is re-checked (hash and
    active region) and moved. Unrelated edits are kept. A row whose move does not
    commit, or whose creation's outcome is unknown, is superseded: readers ignore
    it, the file entry stays visible, and the next reconciling pass supersedes any
    such row this run could not reach."""
    seen = ledger_file.read_text(encoding="utf-8")
    try:
        _with_status(seen, r, "check")
    except ledger.LedgerError as e:
        return f"skipped: {e}"
    try:
        made = store.insert_raw(r["ask_id"], r["title"],
                                row_body(r["body"], None, None, (), "**Sent:** (legacy entry)")) or {}
    except Exception as e:  # noqa: BLE001 — the outcome is unknown; supersede whatever exists
        return f"skipped: {type(e).__name__}: {e}" + _supersede(store, r)
    if not made.get("created"):
        try:
            store.rewrite_body(r["ask_id"], row_body(r["body"], None, None, (), "**Sent:** (legacy entry)"))
            store.restore(r["ask_id"])
        except GuardFailed as e:
            return f"skipped: the row exists and is not this host's to reuse ({e}); the file entry stays"
        except Exception as e:  # noqa: BLE001
            return f"skipped: {type(e).__name__}: {e}" + _supersede(store, r)
    moved = f"moved — kept in the room database as row {row_id(r['ask_id'])}"
    err = ledger.update(ledger_file, lambda t: _with_status(t, r, moved))
    if err:
        return f"skipped: {err}" + _supersede(store, r)
    return f"moved: {r['title'][:80]}"


def _supersede(store, r: dict) -> str:
    """Only this host's open row is marked; another host's row or a closed one is left as is."""
    try:
        store.supersede(r["ask_id"])
        return "; its row was superseded (the file entry stays the question)"
    except GuardFailed as e:
        return f"; its row was left as is ({e})" if e.current else "; no row was made"
    except Exception as e:  # noqa: BLE001
        return f"; its row could not be superseded now ({type(e).__name__}); the next reminder pass does it"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--ledger", type=Path, default=None, help="default: this host's pending-questions.md")
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--repo", default="sonichi/sutando")
    ap.add_argument("--window-days", type=float, default=14.0)
    ap.add_argument("--close-past-window", action="store_true",
                    help="plan past-window `## ` entries as resolved too")
    ap.add_argument("--plan-out", type=Path, default=None, help="dry run: save the plan here")
    ap.add_argument("--plan", type=Path, default=None, help="apply: the reviewed plan to carry out")
    ap.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    cpq = _reader()
    ws = args.workspace or cpq.WORKSPACE
    from util_paths import host_label  # noqa: PLC0415
    if args.ledger is None:
        from pending_questions_ask import ledger_path  # noqa: PLC0415
        args.ledger = ledger_path(ws, host_label())
    args.ledger = args.ledger.resolve()
    if args.apply:
        if args.plan is None:
            print("--apply carries out a reviewed plan: make one with --dry-run --plan-out, "
                  "then pass it with --plan", file=sys.stderr)
            return 2
        try:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = None
        if not plan_fits(plan, host_label()):
            print(f"refused: this plan is not a well-formed version {PLAN_VERSION} plan made on this host; "
                  f"re-run the dry run here", file=sys.stderr)
            return 2
        if Path(plan["ledger"]).resolve() != args.ledger.resolve():
            print(f"the plan is for {plan['ledger']}, not {args.ledger}", file=sys.stderr)
            return 2
        if args.ledger.exists() and _sha(args.ledger.read_text(encoding="utf-8")) != plan.get("ledger_sha256"):
            print("note: the ledger changed since the plan; each entry is still applied only while it is "
                  "byte-identical and in the active region", file=sys.stderr)
        from pending_questions_room_db import room_store  # noqa: PLC0415
        store, where = room_store(ws)
        if store is None:
            print(f"room database unavailable ({where}); live rows are not created", file=sys.stderr)
        done = apply(plan, args.ledger, store, host_label())
        if done and done[0].startswith("refused:"):
            print(done[0], file=sys.stderr)
            return 2
        print("\n".join(done))
        return 0
    text = args.ledger.read_text(encoding="utf-8") if args.ledger.exists() else ""
    rows = triage(cpq.parse_waiting(text, keep_title_resolved=True), GhPrs(), args.now or time.time(),
                  args.window_days, args.repo, cpq.title_says_resolved, text, host_label())
    print("\n".join(report(rows, args.ledger, args.close_past_window)))
    if args.plan_out:
        args.plan_out.write_text(json.dumps(make_plan(rows, args.ledger, text, args.close_past_window, host_label()),
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nplan saved: {args.plan_out} (apply it with --apply --plan {args.plan_out})")
    print("\n(dry run: nothing written to the ledger or the room)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
