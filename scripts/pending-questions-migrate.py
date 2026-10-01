#!/usr/bin/env python3
"""Triage the per-host pending-questions.md before its open entries move to the
owner's "Pending questions" room database.

  python3 scripts/pending-questions-migrate.py [--dry-run]   # default: print only
  python3 scripts/pending-questions-migrate.py --apply       # after the owner reviewed a dry run

Each open entry (the reminder's own reading of the file, plus the entries it
already skips because their title says resolved) is classified, first match wins:
  self-resolved     — the title leads with RESOLVED / SELF-RESOLVED (the reader's rule)
  live              — an open PR it names, or none and asked within --window-days
  stale-merged-PR   — every PR it names is closed and at least one merged
  stale-closed-PR   — every PR it names was closed unmerged
  past-window       — no PR decides it and it is older than --window-days
PR states come from `gh api repos/<repo>/pulls/<n>`; a 403 backs off 3 minutes
and retries. The dry run prints the proposed action per entry and the rows it
would create; it writes nothing. --apply creates the live rows (ask id
`legacy-<hash>`, idempotent) and marks self-resolved and stale `## ` entries
resolved in the file; past-window `## ` entries too with --close-past-window.
Bullet entries are listed for the owner, never changed.
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

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pending_questions_ledger as ledger  # noqa: E402
from pending_questions_store import row_body, row_id  # noqa: E402

CLASSES = ("self-resolved", "live", "stale-merged-PR", "stale-closed-PR", "past-window")
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
    or None when the number is not a PR or gh could not say."""

    def __init__(self, runner=subprocess.run, sleep=time.sleep, log=print):
        self.runner, self.sleep, self.log, self.cache = runner, sleep, log, {}

    def state(self, repo: str, n: int):
        if (repo, n) in self.cache:
            return self.cache[(repo, n)]
        st = None
        for attempt in range(TRIES):
            r = self.runner(["gh", "api", f"repos/{repo}/pulls/{n}"], capture_output=True, text=True)
            err = (r.stderr or "") + (r.stdout if r.returncode else "")
            if r.returncode == 0:
                try:
                    d = json.loads(r.stdout)
                except ValueError:
                    break
                st = "merged" if d.get("merged_at") else ("open" if d.get("state") == "open" else "closed")
                break
            if "HTTP 403" in err or "rate limit" in err.lower():
                if attempt + 1 < TRIES:
                    self.log(f"gh: 403 on {repo}#{n}; backing off {BACKOFF_SEC}s", file=sys.stderr)
                    self.sleep(BACKOFF_SEC)
                continue
            break  # 404 (an issue, not a PR) or another refusal: no PR state
        self.cache[(repo, n)] = st
        return st


SELF_RESOLVED_WHY = "its title says resolved"
PAST_WINDOW_WHY = "past its 14-day window (closed in cleanup)"


def classify(q: dict, prs, now: float, window_days: float, repo: str, title_resolved=None) -> tuple:
    """(class, reason) for one waiting entry."""
    if title_resolved is not None and title_resolved(q["title"]):
        return "self-resolved", SELF_RESOLVED_WHY
    text = f"{q['title']}\n{q.get('body', '')}"
    refs = pr_refs(text, repo)
    states = {f"{r}#{n}": prs.state(r, n) for r, n in refs}
    known = {k: v for k, v in states.items() if v}
    if any(v == "open" for v in known.values()):
        return "live", "open PR " + ", ".join(k for k, v in known.items() if v == "open")
    if known and any(v == "merged" for v in known.values()):
        return "stale-merged-PR", "PR(s) " + ", ".join(f"{k} {v}" for k, v in known.items())
    if known:
        return "stale-closed-PR", "PR(s) " + ", ".join(f"{k} {v}" for k, v in known.items())
    asked = asked_on(q["title"], q.get("body", ""))
    if asked is not None and now - asked > window_days * 86400:
        return "past-window", f"asked {datetime.fromtimestamp(asked, tz=timezone.utc):%Y-%m-%d}, " \
                              f"older than {window_days:g} days, no PR decides it"
    return "live", "no closed PR, within the window" if asked else "no closed PR, undated"


def legacy_ask_id(q: dict) -> str:
    return "legacy-" + hashlib.sha256(f"{q['title']}\n{q.get('body', '')}".encode()).hexdigest()[:12]


ACTIONS = {
    "self-resolved": "mark resolved in the file (its title says resolved); no row",
    "live": "create an Open row in the room database; mark the file entry moved",
    "stale-merged-PR": "mark resolved in the file (its PR merged); no row",
    "stale-closed-PR": "mark resolved in the file (its PR closed unmerged); no row",
    "past-window": "leave open in the file; listed for the owner to resolve or keep",
}


def triage(questions: list, prs, now: float, window_days: float, repo: str, title_resolved=None) -> list:
    out = []
    for q in questions:
        cls, why = classify(q, prs, now, window_days, repo, title_resolved)
        out.append({**q, "class": cls, "why": why, "ask_id": legacy_ask_id(q)})
    return out


def report(rows: list, ledger_file: Path, close_past_window: bool = False) -> list:
    counts = {c: sum(r["class"] == c for r in rows) for c in CLASSES}
    lines = [f"ledger: {ledger_file}", f"waiting entries: {len(rows)}",
             "counts: " + ", ".join(f"{c}={n}" for c, n in counts.items()), ""]
    for r in rows:
        action = ACTIONS[r["class"]]
        if r["class"] == "past-window" and close_past_window:
            action = f"mark resolved in the file ({PAST_WINDOW_WHY}); no row"
        lines.append(f"[{r['class']}] {r['title'][:100]} — {action} ({r['why']})")
    live = [r for r in rows if r["class"] == "live"]
    lines += ["", f"rows it would create ({len(live)}):"]
    for r in live:
        lines.append(f"  Name={r['title'][:100]} | Status=Open | Priority=Medium | Ask id={r['ask_id']}"
                     f" | row={row_id(r['ask_id'])}")
    return lines


def _set_section_status(text: str, title: str, status: str) -> str:
    """The `## <title>` section's status line set; a section without one gains one."""
    m = re.search(rf"^## {re.escape(title)}[ \t]*$", text, re.MULTILINE)
    if not m:
        raise ledger.LedgerError(f"no section titled {title!r}")
    nxt = re.compile(r"^## |^# ", re.MULTILINE).search(text, m.end())
    end = nxt.start() if nxt else len(text)
    line = f"**Status:** {status} (pending-questions-migrate)"
    st = re.compile(r"^\*\*Status:\*\*.*$", re.MULTILINE).search(text, m.end(), end)
    if st:
        return text[:st.start()] + line + text[st.end():]
    return text[:end].rstrip("\n") + "\n\n" + line + "\n\n" + text[end:]


def apply(rows: list, ledger_file: Path, store, close_past_window: bool = False) -> list:
    done = []
    headings = ledger_file.read_text(encoding="utf-8") if ledger_file.exists() else ""
    for r in rows:
        is_section = re.search(rf"^## {re.escape(r['title'])}[ \t]*$", headings, re.MULTILINE) is not None
        if r["class"] == "live" and store is not None:
            store.insert_raw(r["ask_id"], r["title"], row_body(r["body"], None, None, (), "**Sent:** (legacy entry)"))
            err = ledger.update(ledger_file, lambda t, r=r: _set_section_status(
                t, r["title"], f"moved — kept in the room database as row {row_id(r['ask_id'])}")) \
                if is_section else "a bullet entry: resolve it in the file by hand"
            done.append(f"row {row_id(r['ask_id'])}{'; file: ' + err if err else ''}: {r['title'][:80]}")
        elif is_section and (r["class"].startswith("stale-") or r["class"] == "self-resolved"
                             or (r["class"] == "past-window" and close_past_window)):
            why = PAST_WINDOW_WHY if r["class"] == "past-window" else r["why"]
            err = ledger.update(ledger_file, lambda t, r=r, why=why: _set_section_status(
                t, r["title"], f"resolved — {why}"))
            done.append(f"{'FAILED ' + err if err else 'resolved'}: {r['title'][:80]}")
        else:
            done.append(f"left for the owner [{r['class']}]: {r['title'][:80]}")
    return done


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
                    help="with --apply, also mark past-window `## ` entries resolved")
    ap.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    cpq = _reader()
    ws = args.workspace or cpq.WORKSPACE
    if args.ledger is None:
        from pending_questions_ask import ledger_path  # noqa: PLC0415
        from util_paths import host_label  # noqa: PLC0415
        args.ledger = ledger_path(ws, host_label())
    text = args.ledger.read_text(encoding="utf-8") if args.ledger.exists() else ""
    rows = triage(cpq.parse_waiting(text, keep_title_resolved=True), GhPrs(), args.now or time.time(),
                  args.window_days, args.repo, cpq.title_says_resolved)
    print("\n".join(report(rows, args.ledger, args.close_past_window)))
    if not args.apply:
        print("\n(dry run: nothing written; --apply after the owner has reviewed this)")
        return 0
    from pending_questions_room_db import room_store  # noqa: PLC0415
    store, where = room_store(ws)
    if store is None:
        print(f"\nroom database unavailable ({where}); live rows are not created", file=sys.stderr)
    print("\n" + "\n".join(apply(rows, args.ledger, store, args.close_past_window)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
