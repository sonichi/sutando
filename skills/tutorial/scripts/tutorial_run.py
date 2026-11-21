#!/usr/bin/env python3
"""The tutorial playbook's one step after the greeting: publish the Getting started page, record how it went.

A task tagged `playbook: tutorial` (the owner's DM message with a `/tutorial` line, see
src/agent_triggers.py) runs this once, after the greeting has gone out:

    python3 skills/tutorial/scripts/tutorial_run.py --room '<channel_id>' --owner '<user_id>' \\
        --body-file <file holding the task's body>

It always runs `getting_started.py publish` with the task's own room and owner, so whether the page
is made never rests on anything else. The setup app's tutorial inputs file is not a gate: it is only
a source of roles, used when it names this same room. Otherwise the roles come from the body's own
sentence, in either form ("I want to use <product> for A, B." or the older "I picked: A, B."), else
none. The owner's name never appears on the page: publish uses the agent's own (`--name auto`).

Every run records its outcome in <workspace>/state/tutorial/last-run.json (health-check reads it) and
prints one JSON line. Whenever the owner would otherwise hear nothing (a failed publish, or a page whose
card did not reach them) that line carries `owner_line`, the one plain sentence the agent sends. Recording
is best-effort: a status file that cannot be written never hides that line. Exit 0 when a page exists.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
import dotspace  # noqa: E402  (the product's name, as the setup app writes it)

PUBLISH_SCRIPT = HERE / "getting_started.py"
OWNER_LINE = "I couldn't make your Getting started page just now. Send /tutorial to try again."
# The page exists but its card did not reach the owner, so nothing in the DM points at it.
CARD_LINE = ("Your Getting started page is ready in this room's Commons, but I couldn't send you its card. "
             "Send /tutorial to get it.")

# getting_started.py's exit codes (its main): what went wrong, in words a health row can carry.
OUTCOMES = {
    0: "ok",
    2: "no-room-or-bad-input",
    3: "room-commons-not-installed",
    4: "commons-call-failed",
}
REASON_MAX = 300

# The roles sentence in either form, anywhere: the bridge folds the body to one line. The product word
# is exact, so the owner's own "I want to use it for ..." is not roles.
ROLES_SENTENCE = re.compile(
    r"(?:I\s+want\s+to\s+use\s+" + re.escape(dotspace.SUCCESSOR_SERVER) + r"\s+for\b|I\s+picked\s*:)"
    r"\s*[^\r\n]*?\S(?:\.(?=\s|$)|(?=\s*[\r\n])|$)", re.I)


def _workspace() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace()


def record_path(workspace: Path) -> Path:
    return workspace / "state" / "tutorial" / "last-run.json"


def inputs_path(workspace: Path) -> Path:
    return workspace / "state" / "onboarding" / "tutorial-inputs.json"


def roles_from_body(body: str) -> str:
    """The body's roles sentence, as written, or "" when it has none."""
    found = ROLES_SENTENCE.search(body or "")
    return found.group(0).strip() if found else ""


def saved_roles(workspace: Path, room: str) -> str:
    """The setup app's saved roles, only when its file names this room; "" otherwise."""
    try:
        data = json.loads(inputs_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("room") != room:
        return ""
    roles = data.get("roles")
    if isinstance(roles, list):
        roles = ", ".join(r.strip() for r in roles if isinstance(r, str) and r.strip())
    return roles.strip() if isinstance(roles, str) and "\n" not in roles else ""


def choose_roles(workspace: Path, room: str, body: str) -> tuple[str, str]:
    """(roles, where they came from): the inputs file for this room, else the body, else none."""
    saved = saved_roles(workspace, room)
    if saved:
        return saved, "inputs"
    said = roles_from_body(body)
    return (said, "message") if said else ("", "none")


def _last_json(text: str) -> Dict[str, object]:
    for line in reversed((text or "").strip().splitlines()):
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            return got
    return {}


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
        script: Path = PUBLISH_SCRIPT, now: Optional[float] = None) -> Dict[str, object]:
    """Publish once and record the outcome. Never raises for a failed publish: it is recorded."""
    roles, roles_from = choose_roles(workspace, room, body)
    cmd = [sys.executable, str(script), "publish", "--room", room, "--name", "auto"]
    if owner:
        cmd += ["--owner", owner]
    if roles:
        cmd += ["--roles", roles]
    # Sent on its own, `/tutorial` asks for the page again: summon the owner to it even on a refresh.
    if (body or "").strip() == "/tutorial":
        cmd.append("--summon")
    if not Path(script).is_file():
        # Checked first: a missing script would exit 2 from the interpreter, which reads as publish's own 2.
        code, out, err = -1, "", f"publish script not found: {script}"
    else:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            code, out, err = -1, "", f"{type(exc).__name__}: {exc}"
    result = _last_json(out)
    outcome = OUTCOMES.get(code, "publish-did-not-finish")
    reason = result.get("reason") if isinstance(result.get("reason"), str) else ""
    if code != 0 and not reason:
        reason = (err or "").strip().splitlines()[-1] if (err or "").strip() else f"exit {code}"
    if code == 0 and result.get("summon_error"):
        # Published, but the summon that carries the page to the owner failed: not a silent success.
        outcome, reason = "page-made-card-not-sent", str(result["summon_error"])
    record: Dict[str, object] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
        "room": room,
        "owner": owner,
        "exit": code,
        "outcome": outcome,
        "roles_from": roles_from,
    }
    if reason:
        record["reason"] = reason[:REASON_MAX]
    for key in ("action", "kind"):
        if isinstance(result.get(key), str):
            record[key] = result[key]
    shown: Dict[str, object] = {"ok": code == 0, **record}
    try:
        write_record(workspace, record)
    except OSError as exc:
        # The owner's notice and the run's verdict never depend on the status file.
        shown["record_error"] = f"{type(exc).__name__}: {exc}"[:REASON_MAX]
    if code != 0:
        shown["owner_line"] = OWNER_LINE
    elif outcome == "page-made-card-not-sent":
        shown["owner_line"] = CARD_LINE
    return shown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--room", required=True, help="the task's channel_id: the owner's DM")
    ap.add_argument("--owner", default="", help="the task's user_id: the owner")
    body = ap.add_mutually_exclusive_group()
    body.add_argument("--body-file", type=Path, help="a file holding the task's body")
    body.add_argument("--body", default="", help="the task's body, inline")
    ap.add_argument("--publish-script", type=Path, default=PUBLISH_SCRIPT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    text = args.body
    if args.body_file is not None:
        try:
            text = args.body_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
    shown = run(args.room, args.owner, text, _workspace(), script=args.publish_script)
    print(json.dumps(shown))
    return 0 if shown["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
