#!/usr/bin/env python3
"""human-action-bridge — PreToolUse hook that turns `AskUserQuestion` into a
REMOTE question instead of a hard deny (human-action bridge v1, step 1).

Today `hooks/skip-ask-user-question.py` denies AskUserQuestion outright because
the core runs headless — a rendered prompt would hang the session forever. That
protects the session but lobotomizes the interaction: every question the model
judged worth asking is silently skipped.

This hook upgrades deny → remote-ask:

    AskUserQuestion fires
      → durable pending-action file      (<workspace>/state/human-actions/)
      → card file for the owner         (results/proactive-ha-<id>.txt — the
                                          sanctioned proactive path; the channel
                                          bridge delivers it to the owner)
      → bounded wait, polling the action file for a decision
      → decision arrives  → allow + updatedInput carrying the owner's answers
                            (Claude continues as if answered locally)
      → timeout / no path → deny with the same decide-autonomously guidance as
                            skip-ask-user-question (EXACTLY today's behavior)

Decisions are written into the action file by whoever holds the return path:
the sparrow DecisionHandler (v1 step 3) when SPARROW_EVENTS is live, or the
core itself when the owner's reply arrives as a normal task (works today).
A decision is only honored while `status` is "pending"; terminal states are
immutable and late answers are ignored by the hook (the writer may still record
them for audit).

Safety invariants (design: notes/tasks-events/human_action_bridge_design.md):
  - timeout NEVER approves — no response ⇒ deny-with-reason, never consent
  - fail-OPEN for the session (any hook error ⇒ exit 0 allow-passthrough;
    a crashing hook must never wedge the core) but fail-CLOSED for the
    decision (only an explicit resolved decision produces an allow)
  - every state transition is stamped in the action file for audit

Registration: PreToolUse with matcher "AskUserQuestion", INSTEAD OF
skip-ask-user-question.py (it subsumes it — the timeout branch IS that hook).
See hooks/README.md. Test-only env overrides (documented here, used by
tests/human-action-bridge.test.py): SUTANDO_HA_DIR (action-store dir),
SUTANDO_HA_CARD_DIR (card dir), SUTANDO_HA_TIMEOUT (seconds, default 120),
SUTANDO_HA_POLL (seconds, default 2).
"""
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

TOOL = "AskUserQuestion"

# Timeout branch = the exact guidance skip-ask-user-question ships today, so the
# fallback behavior is indistinguishable from the current hook.
TIMEOUT_REASON = (
    "AskUserQuestion could not be answered remotely in time: Sutando's core agent "
    "runs headless and the owner did not respond to the question card within the "
    "window. Do NOT ask again — decide autonomously: pick the option you judge "
    "best, or state a clear assumption and proceed. If a choice is genuinely "
    "blocking AND irreversible, surface the question through a normal channel "
    "(per-host pending-questions.md or an owner notification) and keep working "
    "on other things. [human-action-bridge:{action_id}]"
)


def _workspace() -> Path:
    """Derive the workspace from CLAUDE_CONFIG_DIR — the Claude Code project
    tree lives at `<workspace>/.claude-sutando` per the workspace contract, so
    the nearest `.claude-sutando` ancestor's parent IS the workspace. Same
    pattern as context-source-guard.py: no subprocess (hooks are hot-path) and
    no __file__ walk (the bundled-symlink anti-pattern; also keeps this hook
    standalone-deployable). Falls back to the system's canonical last-ditch
    default, ~/sutando-workspace."""
    p = os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR")
                         or os.path.expanduser("~/.claude"))
    while True:
        if os.path.basename(p) == ".claude-sutando":
            return Path(os.path.dirname(p))
        parent = os.path.dirname(p)
        if parent == p:  # filesystem root — no `.claude-sutando` ancestor
            return Path(os.path.expanduser("~/sutando-workspace"))
        p = parent


def _store_dir() -> Path:
    override = os.environ.get("SUTANDO_HA_DIR")
    return Path(override) if override else _workspace() / "state" / "human-actions"


def _card_dir() -> Path:
    override = os.environ.get("SUTANDO_HA_CARD_DIR")
    return Path(override) if override else _workspace() / "results"


def _atomic_write(path: Path, payload: dict) -> None:
    """Durable atomic write: unique per-writer temp (the resolver may write the
    same action concurrently — a fixed name could be clobbered mid-write),
    fsync the data before rename and the directory entry after, so a crash can
    never lose pending-action state (review blocker)."""
    tmp = path.parent / f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _transition_lock(path: Path):
    """flock shared by EVERY writer of an action file — this hook's expiry path
    AND the sparrow resolver (ActionStore.transition_lock uses the same
    `<action_id>.lock` file). Serializes read→check→write so exactly one
    terminal transition wins (review blocker: the timeout could overwrite a
    decision that landed between its read and its write)."""
    lock_file = open(path.parent / (path.stem + ".lock"), "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def _new_action(data: dict) -> dict:
    tool_input = data.get("tool_input") or {}
    questions = tool_input.get("questions") or []
    now = time.time()
    digest = hashlib.sha1(
        (json.dumps(questions, sort_keys=True, ensure_ascii=False)
         + str(data.get("session_id")) + str(now)).encode()
    ).hexdigest()[:12]
    timeout = float(os.environ.get("SUTANDO_HA_TIMEOUT", "120"))
    return {
        "action_id": f"ha_{digest}",
        "kind": "clarification",
        "status": "pending",
        "claude_session_id": data.get("session_id"),
        "tool_input": tool_input,
        "questions": questions,
        "decision": None,
        "resolved_by": None,
        "created_at": now,
        "expires_at": now + timeout,
        "audit": [{"at": now, "event": "created"}],
    }


def _render_card(action: dict) -> str:
    lines = [
        "**Claude is asking you a question** (reply within "
        f"{int(action['expires_at'] - action['created_at'])}s or it decides on its own)",
        "",
    ]
    for qi, q in enumerate(action["questions"], 1):
        lines.append(f"Q{qi}. {q.get('question', '?')}")
        for oi, opt in enumerate(q.get("options") or [], 1):
            label = opt.get("label", "?")
            desc = opt.get("description") or ""
            lines.append(f"  {oi}. {label}" + (f" — {desc}" if desc else ""))
        lines.append("")
    lines.append(f"Reply: `answer {action['action_id']} <option number per question, "
                 "comma-separated>` — or answer in your own words naming the option.")
    return "\n".join(lines)


def _poll_for_decision(path: Path, action: dict) -> "dict | None":
    poll = float(os.environ.get("SUTANDO_HA_POLL", "2"))
    while time.time() < action["expires_at"]:
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            current = None
        if current:
            if current.get("status") == "resolved" and current.get("decision"):
                return current
            if current.get("status") in ("cancelled", "expired"):
                return None
        time.sleep(poll)
    # Timeout: expire ONLY if still pending, with read→check→write under the
    # shared transition lock — a resolver landing in the gap keeps its win, and
    # a late resolver can never overwrite `expired` (terminal immutability).
    try:
        lk = _transition_lock(path)
        try:
            current = json.loads(path.read_text())
            if current.get("status") == "pending":
                current["status"] = "expired"
                current.setdefault("audit", []).append({"at": time.time(), "event": "expired"})
                _atomic_write(path, current)
                return None
            if current.get("status") == "resolved" and current.get("decision"):
                return current  # resolved in the final poll gap — honor it
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
            lk.close()
    except (OSError, ValueError):
        pass
    return None


def _emit(decision_obj: dict) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", **decision_obj}}))


def main() -> None:
    data = json.loads(sys.stdin.read())
    if data.get("tool_name") != TOOL:
        sys.exit(0)  # not ours — allow-passthrough

    action = _new_action(data)
    store = _store_dir()
    store.mkdir(parents=True, exist_ok=True)
    path = store / (action["action_id"] + ".json")
    _atomic_write(path, action)

    # Card: best-effort. The proactive results path is the sanctioned way to
    # speak to the owner; if it fails we still wait (a decision can arrive via
    # any writer that knows the store), then fall back to deny-on-timeout.
    try:
        card = _card_dir() / f"proactive-ha-{action['action_id']}.txt"
        card.parent.mkdir(parents=True, exist_ok=True)
        # the proactive path is consumed by a bridge poller — write via temp +
        # rename so it can never observe a half-written card (review P2)
        card_tmp = card.parent / f"{card.name}.{os.getpid()}.tmp"
        card_tmp.write_text(_render_card(action))
        os.replace(card_tmp, card)
    except OSError as e:
        print(f"[human-action-bridge] card write failed (still waiting): {e}",
              file=sys.stderr)

    resolved = _poll_for_decision(path, action)
    if resolved:
        answers = resolved["decision"].get("answers") or {}
        _emit({
            "permissionDecision": "allow",
            "updatedInput": {**(data.get("tool_input") or {}), "answers": answers},
        })
        sys.exit(0)

    _emit({
        "permissionDecision": "deny",
        "permissionDecisionReason":
            TIMEOUT_REASON.format(action_id=action["action_id"]),
    })
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[human-action-bridge] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
