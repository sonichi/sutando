#!/usr/bin/env python3
"""gmail-write-guard — PreToolUse hook that denies the claude.ai Gmail MCP
connector's WRITE-scoped tools and routes writes to the IMAP/SMTP path.

Why (field report 05cb849a, michael@actoneventures.com, 2026-07-13): every
Gmail WRITE operation through the claude.ai connector is unreliable or broken
while reads work fine —

  * ``create_draft`` caused 7 documented incidents over ~5 weeks on one
    install (drafts not reflecting what actually gets sent — one
    wrong-recipient send), and the connector exposes no delete-draft tool, so
    each cleanup needed raw IMAP ``UID SEARCH`` + ``STORE \\Deleted`` +
    ``EXPUNGE``.
  * ``label_thread`` (and the other label/unlabel tools) fail outright with
    "Request had insufficient authentication scopes" — the connector's OAuth
    flow doesn't actually grant the Gmail write scopes it needs, even when
    read tools (search_threads / get_thread / list_labels) work.

Nothing in the tools' own descriptions warns about this; an install can only
discover it by getting burned. This hook is the generalized version of the
per-install block Michael built: deny the connector's Gmail write tools BEFORE
they run, with a reason that points the model at the app-password IMAP/SMTP
path (see docs/built-in-tools.md → Email) that actually works.

Scope — deliberately narrow:
  * Only MCP tools (``mcp__…``) whose server/tool name mentions gmail.
  * Only WRITE verbs (create/send/label/unlabel/delete/trash/archive/modify/
    update/move/apply/remove/mark). Read tools — search_threads, get_thread,
    list_labels, get_message, … — pass through untouched (they work fine).
  * Non-Gmail tools: no-op (exit 0), safe to register under a broad matcher.

Escape hatch: set ``SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES=1`` to disable the
guard (e.g. if/when the connector's OAuth scopes are fixed upstream).

Fail-OPEN on any error — a crashing hook must never wedge the core (same
contract as skip-ask-user-question.py).

Registration: manual per-node deploy like context-source-guard.py — see
hooks/README.md.
"""
import json
import os
import sys

# Write-verb tokens. Matched against the '_'-split tokens of the MCP tool's
# trailing tool-name segment, so `list_labels` (token "labels") stays allowed
# while `label_thread` / `create_label` / `unlabel_thread` are denied.
WRITE_TOKENS = {
    "create", "send", "delete", "trash", "archive", "modify", "update",
    "move", "apply", "remove", "mark", "label", "unlabel", "insert",
    "batchmodify", "batchdelete", "untrash",
}

REASON = (
    "Gmail writes through the claude.ai MCP connector are blocked on this install: "
    "the connector's write scopes are broken/unreliable (label/archive fail with "
    "'insufficient authentication scopes'; create_draft has a documented history of "
    "drafts not matching what gets sent, incl. a wrong-recipient send — field report "
    "05cb849a). Gmail READS through the connector are fine and remain allowed. "
    "For this write, use the app-password IMAP/SMTP path instead (docs/built-in-tools.md "
    "-> Email: scripts using imaplib/smtplib with the vaulted app password). "
    "If the connector's OAuth scopes get fixed upstream, set "
    "SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES=1 to lift this guard. [gmail-write-guard]"
)


def is_gmail_connector_write(tool_name: str) -> bool:
    """True only for MCP Gmail tools whose name carries a write verb."""
    if not tool_name.startswith("mcp__"):
        return False
    lowered = tool_name.lower()
    if "gmail" not in lowered:
        return False
    # Trailing segment = the tool itself (server names also split on __).
    tool_part = lowered.rsplit("__", 1)[-1]
    tokens = set(tool_part.split("_"))
    return bool(tokens & WRITE_TOKENS)


def main() -> None:
    if os.environ.get("SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES", "").strip() == "1":
        sys.exit(0)
    data = json.loads(sys.stdin.read())
    tool_name = str(data.get("tool_name") or "")
    if is_gmail_connector_write(tool_name):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }}))
    # Everything else (and the deny above) exits 0; PreToolUse only blocks
    # when a deny decision is present in the JSON payload.
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[gmail-write-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
