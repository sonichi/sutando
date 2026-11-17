"""The one way core reads owner pending questions: through the store adapter an installed
skill declares in its manifest (`pending_questions_store`), loaded by the path an edge injects —
core names no skill, scans no root and carries no question schema. The edge (a thin entry, a
dashboard, agent-api) resolves that path with `skill_roots.declared(DECLARATION, workspace)`
(both installed roots; a conflict is a refusal, not a pick) or takes a `--store-adapter` flag,
and passes the result as `adapter` to every call here.

`gather(workspace, adapter)` returns {"waiting": [items], "done": n | None, "pending_close": [ids],
"unavailable": bool, "reason": str | None, "link": str | None, "notes": [...], "store": where}.
A store that cannot be read is NEVER a measured zero: `unavailable` is True, `done` is None.
With no adapter there is no store to read, so the result is unavailable with that reason —
what the adapter holds locally, lists or counts is the adapter's to say. `waiting`, `count` and
`resolve` are the thin views every core reader uses; a read never reconciles (`reconcile_pass`
does, on demand; the two are the adapter's separate entry points). Never read any file for this.

`resolve` closes through the adapter; with no adapter, or an adapter that raises, it returns
(False, why) and records nothing — the caller keeps the answer it holds (agent-api files the
answer task before it asks for the close).

`clicks` returns the owner's decision-card clicks after the adapter closed each clicked question's
row: [{ask_id, action_id, label, consequence, actionable, token, stale, deferred, handed_before,
closed, close_message, ...}]; only an `actionable` one is a decision, handed to this caller alone
until its lease runs out unacked (then re-offered once to the next caller with a new token; after
that it is listed as `needs_attention`, never offered again). A `chose` can therefore arrive twice:
the caller re-checks current state before acting.
`ack` with that token settles it once acted on; a superseded token is refused. With no adapter, or one without them, both refuse and change nothing.

CLI, for shell readers: `python3 src/pending_questions_reader.py list [--json] | count | summary`
(`summary` is the count and where the questions live, never their text); and the
writes a core caller makes through the same contract: `resolve <ask-id> [--answered]`
(Resolved, or Answered), which is `resolve` above and nothing else; `clicks [--json]`, run on a
`hitl_click: true` task (`--due` is the probe, exit 0 when a pass is owed; its only write is the
lease credit each pass earns, that
health-check's `decision-card-clicks` warn uses so a deferred or expired click is retried); and
`ack <ask-id> --token <token>` once the core has acted on a choice `clicks` handed it. `--store-adapter <path>`
injects an adapter file in place of the declared one.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple, Optional
from skill_roots import declared

# The manifest field an installed skill declares its adapter script with.
DECLARATION = "pending_questions_store"


_LOADED: dict = {}
_LOAD_LOCK = threading.Lock()
# A failed execution is served from the cache this long (one operation, the polls right behind
# it), then the file is executed again: a transient import failure is not process-lifetime state.
FAILURE_TTL_SEC = 30.0


class LoadFailed(RuntimeError):
    """The adapter file raised when executed; raised fresh from the remembered reason each time."""


class _Failed(NamedTuple):
    """What the cache holds for a failed execution: the reason and when, never the exception."""
    reason: str
    at: float


class Resolved(NamedTuple):
    """One operation's adapter: the module, or None with `reason`. Resolved exactly once per
    public call and handed to every phase, so reconcile and gather never see two modules."""
    module: object
    reason: Optional[str]


def _execute(path: Path, source: bytes):
    """The module from one execution of these bytes (compiled here: a stale __pycache__ entry for
    a same-size same-second rewrite would run the old code), or the failure as a `_Failed`."""
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("pq_store_adapter", str(path))
    mod = importlib.util.module_from_spec(spec)
    try:
        exec(compile(source, str(path), "exec", dont_inherit=True), mod.__dict__)
    except Exception as e:  # noqa: BLE001 — third-party code
        return _Failed(f"{type(e).__name__}: {e}", time.monotonic())
    return mod


def load_adapter(adapter):
    """The adapter module from its file, executed once per content under a lock — the identity
    is the path and the digest of the bytes read under the lock, the bytes then executed, so a
    file replaced while a reader waits or runs is never served under another version's key;
    a failure is served for FAILURE_TTL_SEC and then retried; None when there is none."""
    if not adapter:
        return None
    path = Path(adapter).resolve()
    with _LOAD_LOCK:
        source = path.read_bytes()
        key = (str(path), hashlib.sha256(source).hexdigest())
        got = _LOADED.get(key)
        if isinstance(got, _Failed) and time.monotonic() - got.at >= FAILURE_TTL_SEC:
            got = None
        if got is None:
            got = _execute(path, source)
            for stale in [k for k in _LOADED if k[0] == key[0] and k != key]:  # one version per path
                del _LOADED[stale]
            _LOADED[key] = got
    if isinstance(got, _Failed):
        raise LoadFailed(got.reason)
    return got


NO_ADAPTER = "no pending-questions store adapter installed (no skill declares one)"


def resolve_adapter(adapter=None) -> Resolved:
    """The one resolution of what an edge injected: a `Resolved` is returned as is; a loaded module
    is wrapped; a script path or skill_roots.Declaration is loaded (`load_adapter`); None, or a
    declaration of none, is NO_ADAPTER exactly."""
    if isinstance(adapter, Resolved):
        return adapter
    if hasattr(adapter, "gather"):  # an already-loaded module: no second execution
        return Resolved(adapter, getattr(adapter, "__file__", "<module>"))
    path, why = adapter if isinstance(adapter, tuple) else (adapter, None)  # a Declaration is a tuple
    if not path:
        return Resolved(None, why or NO_ADAPTER)
    try:
        return Resolved(load_adapter(path), str(path))
    except Exception as e:  # noqa: BLE001
        reason = str(e) if isinstance(e, LoadFailed) else f"{type(e).__name__}: {e}"
        return Resolved(None, f"adapter {path} failed to load ({reason})")


def _adapter(adapter=None):
    """(module, path) or (None, why) — `resolve_adapter` as a pair, for the phases."""
    r = resolve_adapter(adapter)
    return r.module, r.reason


def _unavailable(reason: str) -> dict:
    return {"waiting": [], "done": None, "pending_close": [], "unavailable": True, "reason": reason,
            "link": None, "store": None, "notes": [f"PENDING QUESTIONS UNAVAILABLE ({reason}); the count is unknown"]}


def gather(workspace, adapter=None) -> dict:
    mod, why = _adapter(adapter)
    if mod is None:
        return _unavailable(why)
    try:
        g = mod.gather(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return _unavailable(f"adapter failed: {type(e).__name__}: {e}")
    g.setdefault("unavailable", False)
    g.setdefault("reason", None)
    g.setdefault("link", None)
    g.setdefault("pending_close", [])
    return g


def waiting(workspace, adapter=None) -> list:
    return gather(workspace, adapter)["waiting"]


def count(workspace, adapter=None) -> dict:
    """{"open": n | None, "done": n | None, "pending_close": n, "unavailable", "reason"}; None is
    unknown, never 0."""
    g = gather(workspace, adapter)
    return {"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
            "pending_close": len(g["pending_close"]), "unavailable": g["unavailable"], "reason": g["reason"]}


def reconcile_pass(workspace, adapter=None) -> dict:
    """The explicit pass: outbox replay, local closes, the legacy ingest; {"errors": [why]} without a store."""
    mod, why = _adapter(adapter)
    if mod is None or not hasattr(mod, "reconcile_pass"):
        return {"flushed": [], "moved": [], "closed": [], "errors": [f"no store to reconcile with: {why}"]}
    try:
        return mod.reconcile_pass(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return {"flushed": [], "moved": [], "closed": [], "errors": [f"adapter failed: {type(e).__name__}: {e}"]}


def reconcile_then_gather(workspace, adapter=None) -> dict:
    """The reminder's pass: `reconcile_pass`, its errors as notes, then `gather` — one load, one
    failure policy (a raised step is a note or UNKNOWN, never a traceback)."""
    one = resolve_adapter(adapter)  # both phases see this module, or this one failure
    rec = reconcile_pass(workspace, one)
    g = gather(workspace, one)
    g["notes"] = [f"reconcile: FAILED — {e}" for e in rec.get("errors", [])] + list(g.get("notes", []))
    return g


def resolve(workspace, ask_id: str, status: str, adapter=None) -> tuple:
    """(closed, message): the adapter's `resolve`; (False, why) with nothing recorded when there
    is no adapter or it raises (see the module doc)."""
    mod, why = _adapter(adapter)
    if mod is None:
        return False, f"not closed: {why}; nothing records the close"
    try:
        return mod.resolve(Path(workspace), ask_id, status)
    except Exception as e:  # noqa: BLE001
        return False, f"not closed: adapter failed ({type(e).__name__}: {e}); nothing records the close"


def clicks(workspace, adapter=None) -> tuple:
    """(choices, None), or (None, why) when there is no adapter or it cannot take the clicks."""
    mod, why = _adapter(adapter)
    if mod is None or not hasattr(mod, "clicks"):
        return None, f"no store: {why}" if mod is None else f"adapter {why} takes no card clicks"
    try:
        return mod.clicks(Path(workspace)), None
    except Exception as e:  # noqa: BLE001
        return None, f"adapter failed ({type(e).__name__}: {e})"


def due_clicks(workspace, adapter=None) -> tuple:
    """(ask ids a clicks pass would act on now, None), or (None, why). Read-only."""
    mod, why = _adapter(adapter)
    if mod is None or not hasattr(mod, "due_clicks"):
        return None, f"no store: {why}" if mod is None else f"adapter {why} takes no card clicks"
    try:
        return mod.due_clicks(Path(workspace)), None
    except Exception as e:  # noqa: BLE001
        return None, f"adapter failed ({type(e).__name__}: {e})"


def ack(workspace, ask_id: str, token: str, adapter=None) -> tuple:
    """(done, message): the core acted on that question's choice; the adapter's `ack_click`."""
    mod, why = _adapter(adapter)
    if mod is None or not hasattr(mod, "ack_click"):
        return False, f"not acknowledged: {why if mod is None else f'adapter {why} takes no acks'}"
    try:
        return mod.ack_click(Path(workspace), ask_id, token)
    except Exception as e:  # noqa: BLE001
        return False, f"not acknowledged: adapter failed ({type(e).__name__}: {e})"


def click_line(c: dict) -> str:
    """Only an actionable click reads as `chose`, with its exact action id and consequence."""
    q = " ".join(c["question"].split())[:120]
    what = f"{c['action_id']} {c['label']!r}" + (f" -> {c['consequence']}" if c.get("consequence") else "")
    if c.get("retired"):
        return f"- [{c['ask_id']}] settled meanwhile — not a decision ({what}) — {q}"
    if c.get("stale"):
        return f"- [{c['ask_id']}] already answered — not a decision (a late click on {what}) — {q}"
    if c.get("actionable"):
        again = " (re-offered: the consumer it was handed to never acked)" if c.get("reoffered") else ""
        return f"- [{c['ask_id']}] chose {what}{again} — {q} — ack with --token {c['token']}"
    if c.get("needs_attention"):
        return (f"- [{c['ask_id']}] NEEDS ATTENTION — handed out twice, never acknowledged ({what}); not "
                f"a new decision: check whether it was acted on, then ack with --token {c['token']} or "
                f"resolve the question — {q}")
    if c.get("deferred"):
        return (f"- [{c['ask_id']}] deferred — the room cannot confirm the owner has not answered "
                f"otherwise; not a decision yet ({what}); retried next pass — {q}")
    return (f"- [{c['ask_id']}] handed out earlier, awaiting ack — not a new decision ({what}); "
            f"re-offered if no ack comes within the lease — {q}")


def _norm_title(text: str) -> str:
    from result_markers import neutralize_markers  # noqa: PLC0415 — the rows' title rule
    return " ".join(neutralize_markers(text or "").split())[:120].casefold()


def open_duplicate(workspace, question: str, adapter=None) -> Optional[dict]:
    """The open item whose title is this question's (whitespace and case aside), or None —
    also None when the store cannot be read: an unread store proves no duplicate."""
    g = gather(workspace, adapter)
    if g["unavailable"]:
        return None
    want = _norm_title(question)
    return next((it for it in g["waiting"] if _norm_title(it.get("title", "")) == want), None)


def summary_line(g: dict) -> str:
    """Where the owner's questions live and how many are open — never their text, so a snapshot
    that prints this cannot carry them forward as prose."""
    where = "the owner's Pending questions database" + (f" ({g['store']})" if g.get("store") else "")
    if g["unavailable"]:
        head = f"pending questions: count UNKNOWN — {g['reason']}; {len(g['waiting'])} held locally"
    else:
        head = f"{len(g['waiting'])} open in {where}"
    if g["pending_close"]:
        head += f", {len(g['pending_close'])} closed locally"
    return (f"{head}. Not listed here: read them with `python3 src/pending_questions_reader.py list`. "
            "File a new owner question with `python3 scripts/ask-owner.py \"<question>\" --unless-open` "
            "(an open duplicate is not filed again); never carry owner questions as text.")


def unknown_line(g: dict) -> str:
    return f"pending questions: UNKNOWN — room unreachable ({g['reason']}); {len(g['waiting'])} held locally"


def main(argv=None) -> int:
    import argparse  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="List or count the owner's pending questions (read-only), "
                                 "or resolve one, or take card clicks, through the declared store.")
    ap.add_argument("command", choices=("list", "count", "summary", "resolve", "clicks", "ack"))
    ap.add_argument("ask_id", nargs="?", default=None, help="resolve / ack: the question's ask id")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--answered", action="store_true", help="resolve: mark it Answered, not Resolved")
    ap.add_argument("--token", default=None, help="ack: the token `clicks` printed with the choice")
    ap.add_argument("--due", action="store_true",
                    help="clicks: only say whether a pass is owed (exit 0 when it is, 1 when not); "
                         "its only write is crediting this pass to each handed lease")
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--store-adapter", default=None, help="an adapter file in place of the declared one")
    # intermixed: an optional positional after `--workspace` is otherwise swallowed on 3.12
    args = ap.parse_intermixed_args(argv)
    if args.workspace is None:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        args.workspace = resolve_workspace(migrate=False)
    store = declared(DECLARATION, args.workspace, override=args.store_adapter)  # the CLI is an edge
    if args.command in ("resolve", "ack") and not args.ask_id:
        ap.error(f"{args.command} needs an ask id")
    if args.command == "ack":
        if not args.token:
            ap.error("ack needs the --token `clicks` printed with the choice")
        ok, msg = ack(args.workspace, args.ask_id, args.token, store)
        print(msg)
        return 0 if ok else 1
    if args.command == "clicks" and args.due:
        due, why = due_clicks(args.workspace, store)
        if due is None:
            print(f"clicks: not read — {why}")
            return 1
        print(f"{len(due)} decision-card click(s) owe a pass" + (f": {', '.join(due)}" if due else ""))
        return 0 if due else 1
    if args.command == "clicks":
        done, why = clicks(args.workspace, store)
        if done is None:
            print(f"clicks: not read — {why}")
            return 1
        if args.json:
            print(json.dumps(done, ensure_ascii=False, indent=1))
        elif not done:
            print("no question-card clicks waiting")
        for c in [] if args.json else done:
            print(click_line(c))
            print(f"    {c['close_message']}")
        return 0
    if args.command == "resolve":
        ok, msg = resolve(args.workspace, args.ask_id, "Answered" if args.answered else "Resolved", store)
        print(msg)
        return 0 if ok else 1
    g = gather(args.workspace, store)
    for note in g["notes"]:
        print(note, file=sys.stderr)
    if args.command == "summary":
        print(summary_line(g))
        return 0
    if args.command == "count":
        print(json.dumps({"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
                          "pending_close": len(g["pending_close"]), "unavailable": g["unavailable"],
                          "reason": g["reason"]}))
        return 0
    if args.json:
        print(json.dumps({"unavailable": g["unavailable"], "reason": g["reason"], "waiting": g["waiting"]}
                         if g["unavailable"] else g["waiting"], ensure_ascii=False, indent=1))
        return 0
    if g["unavailable"]:
        print(unknown_line(g))
    elif not g["waiting"] and not g["pending_close"]:
        print("0 pending questions" + (f" in {g['store']}" if g.get("store") else ""))
        return 0
    for it in g["waiting"]:
        print(f"- [{it['ask_id']}] {it['title']}" + ("" if it.get("in_room", True) else " (not yet in the room)"))
    for ask_id in g["pending_close"]:
        print(f"- [{ask_id}] closed locally; its row is not in view yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
