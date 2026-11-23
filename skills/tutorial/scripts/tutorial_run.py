#!/usr/bin/env python3
"""The tutorial playbook's one step: send the greeting, publish the Getting started page, record how it went.

A task tagged `playbook: tutorial` (the owner's DM message with a `/tutorial` line, see
src/agent_triggers.py) runs this once:

    python3 skills/tutorial/scripts/tutorial_run.py --room '<channel_id>' --owner '<user_id>' \\
        --body-file <file holding the task's body> --greeting-file <file holding the reply>

With `--greeting-file` it posts that reply to the room itself (room-ops `say`) and starts the publish
only once the reply is proven to be in the room, so the reply and the page's card leave by one path, in
that order, and neither waits on the result file's delivery. Proof is the send's own event id, or the
reply found by reading the room back (room-ops `read`) as an event that was not there before the send.
The line's `greeting` is one of three settled states:
  sent      proven in the room: the page is published; the task's result is `[REPLIED]`.
  not_sent  proven absent (never sent, or the gateway answered and the read-back does not hold it):
            nothing is published; the agent sends the reply and `owner_line` as the task's one result.
  unknown   neither could be proven (the send got no answer, or the room could not be read back):
            nothing is published and the reply is not sent again, since it may have landed; the
            task's result is `owner_line` alone, which says the reply could not be confirmed.

It then runs `getting_started.py publish` with the task's own room and owner, so whether the page
is made never rests on anything else. The page it publishes is generic: no roles, no tailored prompts
and never the owner's name (publish uses the agent's own, `--name auto`). A room's Commons page cannot
yet be limited to its owner, so nothing the owner said about themselves is put on it.

Every run records its outcome in <workspace>/state/tutorial/last-run.json, the record this skill's
manifest declares as `last_run`, so health-check shows it as the `skill:tutorial` row; it
prints one JSON line. The verdict is read from what publish reports, not from its exit code alone: the
`action`, the page's `kind`, and `summoned` whenever this run owed a card (a `/tutorial` sent alone, or
a page just created). Whenever the owner would otherwise hear nothing (a failed publish, a run that
left the page as it was, a page whose card did not reach them, an outcome this script cannot read) that
line carries `owner_line`, the one plain sentence the agent sends. Recording is best-effort: a status
file that cannot be written never hides that line. Exit 0 when publish reported a page.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

PUBLISH_SCRIPT = HERE / "getting_started.py"
SAY_SCRIPT = REPO / "skills" / "agent-room-ops" / "room_ops.py"
OWNER_LINE = "I couldn't make your Getting started page just now. Send /tutorial to try again."
# The page exists but its card did not reach the owner, so nothing in the DM points at it.
CARD_LINE = ("Your Getting started page is ready in this room's Commons, but I couldn't send you its card. "
             "Send /tutorial to get it.")

# The reply may or may not be in the room: the line claims neither, and is safe beside either.
UNKNOWN_LINE = ("I sent you a reply but couldn't confirm it arrived, so I held your Getting started page. "
                "If you don't see my reply above, send /tutorial and I'll try again.")
SENT, NOT_SENT, UNKNOWN = "sent", "not_sent", "unknown"
READ_LIMIT = 50
READ_BACK_WAITS = (0.0, 1.0, 2.0)

# Nothing was made or changed: a retry would end the same way, so the line does not ask for one.
SKIPPED_LINE = "I didn't make or change a Getting started page this time, so there is no card for it."

# getting_started.py publish's exit-0 actions that report a page in the room, and the one that leaves it be.
PAGE_ACTIONS = ("created", "updated", "unchanged", "kept", "resumed")
SKIPPED_ACTION = "skipped"
PAGE_KIND_PREFIX = "html-"
CARD_NOT_SENT = "page-made-card-not-sent"
PAGE_OUTCOMES = ("ok", CARD_NOT_SENT)

# getting_started.py's exit codes (its main): what went wrong, in words a health row can carry.
OUTCOMES = {
    0: "ok",
    2: "no-room-or-bad-input",
    3: "room-commons-not-installed",
    4: "commons-call-failed",
}
REASON_MAX = 300


def _workspace() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace()


def record_path(workspace: Path) -> Path:
    return workspace / "state" / "tutorial" / "last-run.json"


def _last_json(text: str) -> Dict[str, object]:
    for line in reversed((text or "").strip().splitlines()):
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            return got
    return {}


def read_outcome(result: Dict[str, object], card_asked: bool) -> tuple:
    """(outcome, reason) for a publish that exited 0, from what it reported. A page counts only when
    the action is one that reports a page and names it; a card is owed when asked for or on a creation."""
    action, kind = result.get("action"), result.get("kind")
    reason = result.get("reason") if isinstance(result.get("reason"), str) else ""
    if action == SKIPPED_ACTION:
        return "page-left-as-is", reason
    if action not in PAGE_ACTIONS:
        return "publish-outcome-unknown", f"publish exited 0 with action {action!r}"[:REASON_MAX]
    if not (isinstance(kind, str) and kind.startswith(PAGE_KIND_PREFIX)):
        return "publish-outcome-unknown", f"publish reported {action} but named no page"
    if result.get("summon_error"):
        return CARD_NOT_SENT, str(result["summon_error"])
    if (card_asked or action == "created") and result.get("summoned") is not True:
        return CARD_NOT_SENT, f"publish reported the page {action} and no card sent"
    return "ok", ""


def _room_ops(script: Path, *args: str, timeout: int = 120) -> tuple:
    """(answer, error): room-ops' JSON answer, or None and why there is none."""
    try:
        proc = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        answer = json.loads(proc.stdout)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return None, (proc.stderr or proc.stdout or "").strip()[-200:] or f"exit {proc.returncode} with no answer"
    return answer, ""


def read_room(room: str, script: Path) -> Optional[list]:
    """The room's newest messages as room-ops `read` gives them, or None when it cannot be read."""
    answer, _ = _room_ops(script, "read", room, "--limit", str(READ_LIMIT), timeout=60)
    if not answer or answer.get("ok") is not True or not isinstance(answer.get("messages"), list):
        return None
    return [m for m in answer["messages"] if isinstance(m, dict)]


def read_back(room: str, text: str, before: list, script: Path, sleep=time.sleep) -> Optional[bool]:
    """True: the reply is in the room as an event that was not there before. False: the read reaches
    back to what was there before and does not hold it. None: the room does not show either."""
    seen = {m.get("event_id") for m in before if m.get("event_id")}
    covered = None
    for wait in READ_BACK_WAITS:
        if wait > 0:
            sleep(wait)
        now = read_room(room, script)
        if now is None:
            continue
        ids = {m.get("event_id") for m in now if m.get("event_id")}
        if any(isinstance(m.get("body"), str) and m["body"].strip() == text.strip()
               and m.get("event_id") and m["event_id"] not in seen for m in now):
            return True
        covered = bool(seen & ids)
    return False if covered else None


def send_greeting(room: str, text: str, script: Path = SAY_SCRIPT) -> tuple:
    """(state, reason): SENT, NOT_SENT or UNKNOWN, settled by the send's event id or a read-back."""
    if not (text or "").strip():
        return NOT_SENT, "the reply is empty or its file could not be read"
    if not Path(script).is_file():
        return NOT_SENT, f"say script not found: {script}"
    before = read_room(room, script)
    if before is None:
        # Without the room as it was, an unanswered send could never be settled: do not send.
        return NOT_SENT, "the room could not be read before sending, so the reply was not sent"
    fd, path = tempfile.mkstemp(prefix="tutorial-greeting-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        said, err = _room_ops(script, "say", room, "--body-file", path)
    finally:
        Path(path).unlink(missing_ok=True)
    if said and said.get("ok") is True and said.get("event_id"):
        return SENT, ""
    why = str((said or {}).get("reason") or err or "the send returned no event id")
    landed = read_back(room, text, before, script)
    if landed:
        return SENT, ""
    # Absence settles only a send the gateway answered; one with no answer may still land.
    answered = said is not None and not why.startswith("network error")
    if landed is False and answered:
        return NOT_SENT, why
    return UNKNOWN, why


def health(outcome: str, reason: str, action: object, summoned: bool = False) -> Dict[str, str]:
    """The record's `status` and `detail`: what health-check's `skill:tutorial` row shows."""
    if outcome == "ok":
        card = "its card was sent" if summoned else "no card was owed"
        return {"status": "ok", "detail": f"last Getting started page run: page {action}, {card}"}
    if outcome == CARD_NOT_SENT:
        return {"status": "warn", "detail": "the Getting started page was made but its card did not reach the owner"}
    if outcome == "page-left-as-is":
        return {"status": "warn",
                "detail": ("the last Getting started page run made or changed nothing: publish skipped it"
                           + (f" ({reason[:200]})" if reason else "")
                           + " — the owner was told; see skills/tutorial/SKILL.md")}
    if outcome == "greeting-unconfirmed":
        return {"status": "warn",
                "detail": ("the reply's send could not be confirmed either way, so the Getting started page was "
                           "held and the reply was not sent again"
                           + (f" ({reason[:200]})" if reason else "")
                           + " — the owner was told; see skills/tutorial/SKILL.md")}
    if outcome == "greeting-not-sent":
        return {"status": "warn",
                "detail": ("the reply could not be posted ahead of the Getting started page, so the page was not run"
                           + (f" ({reason[:200]})" if reason else "")
                           + " — the owner was told to send /tutorial to retry; see skills/tutorial/SKILL.md")}
    return {"status": "warn",
            "detail": (f"the last Getting started page run failed: {outcome}"
                       + (f" ({reason[:200]})" if reason else "")
                       + " — the owner was told to send /tutorial to retry; see skills/tutorial/SKILL.md")}


def write_record(workspace: Path, record: Dict[str, object]) -> None:
    """Replace last-run.json atomically: a reader never sees half a record."""
    path = record_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".last-run.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run(room: str, owner: str, body: str, workspace: Path,
        script: Path = PUBLISH_SCRIPT, now: Optional[float] = None,
        greeting: Optional[str] = None, say_script: Path = SAY_SCRIPT) -> Dict[str, object]:
    """Send the greeting if given, publish once and record the outcome. Never raises for a failed
    publish: it is recorded. A greeting not proven in the room publishes nothing, so no card precedes it."""
    cmd = [sys.executable, str(script), "publish", "--room", room, "--name", "auto"]
    if owner:
        cmd += ["--owner", owner]
    # Sent on its own, `/tutorial` asks for the page again: summon the owner to it even on a refresh.
    card_asked = (body or "").strip() == "/tutorial"
    if card_asked:
        cmd.append("--summon")
    greeted, why = send_greeting(room, greeting, say_script) if greeting is not None else (None, "")
    if greeted in (NOT_SENT, UNKNOWN):
        code, out, err = None, "", why
    elif not Path(script).is_file():
        # Checked first: a missing script would exit 2 from the interpreter, which reads as publish's own 2.
        code, out, err = -1, "", f"publish script not found: {script}"
    else:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            code, out, err = -1, "", f"{type(exc).__name__}: {exc}"
    result = _last_json(out)
    if code == 0:
        # Exit 0 is not a page: the verdict is what publish says it did.
        outcome, reason = read_outcome(result, card_asked)
    else:
        outcome = ("greeting-unconfirmed" if greeted == UNKNOWN else "greeting-not-sent" if code is None
                   else OUTCOMES.get(code, "publish-did-not-finish"))
        reason = result.get("reason") if isinstance(result.get("reason"), str) else ""
        if not reason:
            reason = (err or "").strip().splitlines()[-1] if (err or "").strip() else f"exit {code}"
    summoned = result.get("summoned") is True
    record: Dict[str, object] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
        "room": room,
        "owner": owner,
        "exit": code,
        "outcome": outcome,
    }
    if reason:
        record["reason"] = reason[:REASON_MAX]
    for key in ("action", "kind"):
        if isinstance(result.get(key), str):
            record[key] = result[key]
    if code == 0:
        record["summoned"] = summoned
    if greeted:
        record["greeting"] = greeted
    record.update(health(outcome, reason, record.get("action"), summoned))
    shown: Dict[str, object] = {"ok": outcome in PAGE_OUTCOMES, **record}
    try:
        write_record(workspace, record)
    except OSError as exc:
        # The owner's notice and the run's verdict never depend on the status file.
        shown["record_error"] = f"{type(exc).__name__}: {exc}"[:REASON_MAX]
    if outcome == CARD_NOT_SENT:
        shown["owner_line"] = CARD_LINE
    elif outcome == "page-left-as-is":
        shown["owner_line"] = SKIPPED_LINE
    elif outcome == "greeting-unconfirmed":
        shown["owner_line"] = UNKNOWN_LINE
    elif outcome != "ok":
        shown["owner_line"] = OWNER_LINE
    return shown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--room", required=True, help="the task's channel_id: the owner's DM")
    ap.add_argument("--owner", default="", help="the task's user_id: the owner")
    body = ap.add_mutually_exclusive_group()
    body.add_argument("--body-file", type=Path, help="a file holding the task's body")
    body.add_argument("--body", default="", help="the task's body, inline")
    ap.add_argument("--greeting-file", type=Path,
                    help="a file holding the reply to post in the room before the page is published")
    ap.add_argument("--publish-script", type=Path, default=PUBLISH_SCRIPT, help=argparse.SUPPRESS)
    ap.add_argument("--say-script", type=Path, default=SAY_SCRIPT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    text = args.body
    if args.body_file is not None:
        try:
            text = args.body_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
    greeting = None
    if args.greeting_file is not None:
        try:
            greeting = args.greeting_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            greeting = ""
    shown = run(args.room, args.owner, text, _workspace(), script=args.publish_script,
                greeting=greeting, say_script=args.say_script)
    print(json.dumps(shown))
    return 0 if shown["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
