#!/usr/bin/env python3
"""Send a task result to Discord DM if voice client is disconnected.

Usage:
    python3 src/dm-result.py "Result text here"
    python3 src/dm-result.py --file results/task-123.txt

Checks http://localhost:8080/sse-status for voiceConnected.
If voice is connected, does nothing (voice agent will speak the result).
If voice is disconnected, sends the result to the owner's Discord DM.

Requires DISCORD_BOT_TOKEN in .env (or in $CLAUDE_CONFIG_DIR/channels/discord/.env)
and the Discord bridge running.

Owner resolution:
    1. $SUTANDO_DM_OWNER_ID env var (explicit override).
    2. First non-bot user in $CLAUDE_CONFIG_DIR/channels/discord/access.json → allowFrom.
The bot's own user ID is discovered via Discord's GET /users/@me so that
multi-owner allowFrom lists still resolve to the human.

Per-node correctness:
    The DM channel ID is NOT hardcoded — each node creates/opens its own
    DM channel on demand via POST /users/@me/channels (idempotent per
    Discord docs). This fixes the HTTP 403 seen on Mac Mini when the old
    hardcoded channel ID belonged to MacBook's bot's DM with the owner.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util_paths import claude_home_path  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
import discord_config  # noqa: E402  — workspace-local Sutando discord config (#1147)
from result_markers import parse_markers  # noqa: E402  — skip markers ([no-send] etc.)
REPO = resolve_workspace()
ACCESS_JSON = claude_home_path("channels", "discord", "access.json")
SSE_STATUS_URL = "http://localhost:8080/sse-status"
USAGE = "Usage: python3 src/dm-result.py 'text' | --file path"

# Path allowlist for `[file: ...]` markers — sourced from
# `src/send_allowlist.py` so this REST-fallback path uses the SAME
# policy as the WS-connected live bridge (`src/discord-bridge.py`).
# Per @liususan091219 review on PR #1029: a copied allowlist will
# drift even with a comment claiming they're in sync — the extract
# removes that hazard at the boundary. Pre-extract, the dm-result
# copy was already missing the personal-notes / Desktop / Documents
# roots that discord-bridge had; the shared import fixes that drift.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_allowlist import (  # noqa: E402
    is_path_sendable as _is_path_sendable,
    SEND_ALLOWED_PREFIXES as _SEND_ALLOWED_PREFIXES,
    SEND_ALLOWED_ROOTS as _SEND_ALLOWED_ROOTS,
)
from message_chunking import chunk_message, _is_fence_open_line  # noqa: E402  (Result Router S3 — shared fence-aware chunker; was a 4th private copy)




def _chunk_for_discord(text: str, max_len: int = 1900):
    """Alias for the shared fence-aware chunker (Result Router S3).

    Was a private mirror of discord-bridge's copy; now delegates to
    src/message_chunking.py:chunk_message so this REST-fallback delivery
    path shares the exact same fence-preservation logic.
    """
    yield from chunk_message(text, max_len)


def voice_connected() -> bool:
    """Check if a voice client is currently connected."""
    try:
        with urllib.request.urlopen(SSE_STATUS_URL, timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("voiceConnected", False)
    except Exception:
        return False


def _load_token() -> str:
    """Read DISCORD_BOT_TOKEN from the first env file that has it."""
    for env_path in [
        claude_home_path("channels", "discord", ".env"),
        REPO / ".env",
    ]:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _discord_api(method, path, token, body=None):
    """Small wrapper around urllib for Discord's REST API. Returns parsed JSON
    on 2xx, raises on other statuses. No retries — caller handles failure."""
    url = f"https://discord.com/api/v10{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Sutando/1.0",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _send_message_with_files(channel_id: str, token: str, content: str,
                             file_paths: "list[str]"):
    """POST a message to a channel as multipart/form-data with attached
    files. Used by `send_dm()` when an agent-emitted result body
    contains `[file:|send:|attach:]` markers — the WS-connected live
    bridge calls `discord.File(path)` directly; this REST path needs
    to assemble the same multipart payload by hand.

    `content` may be empty (file-only message). `file_paths` are the
    already-allowlisted absolute paths. Raises on non-2xx; caller
    handles per-file errors.

    Discord docs: POST /channels/{id}/messages with
    multipart/form-data, parts named `payload_json` (the message body)
    and `files[0]`, `files[1]`, ... each with a `filename=` in the
    `Content-Disposition`.
    """
    # Random boundary that won't appear in any payload we send. uuid is
    # already imported (used for outbox_log); reuse here.
    import uuid
    boundary = f"----SutandoBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    # payload_json part
    payload = {"content": content} if content else {}
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n".encode()
    )
    parts.append(json.dumps(payload).encode())
    parts.append(b"\r\n")
    # files[N] parts
    for i, fpath in enumerate(file_paths):
        filename = os.path.basename(fpath)
        # Sanitize filename header — same shape as
        # discord-bridge.py's _safe_attachment_basename (PR #1022). A
        # filename containing CR/LF here would let the file inject its
        # own headers into the multipart envelope.
        safe_name = (
            filename
            .replace("\r", "_")
            .replace("\n", "_")
            .replace('"', "_")
        )[:80] or f"file-{i}"
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[{i}]"; '
            f'filename="{safe_name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        with open(fpath, "rb") as fh:
            parts.append(fh.read())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "User-Agent": "Sutando/1.0",
        },
        method="POST",
    )
    # 30s timeout — multipart uploads can be slower than JSON; cap to
    # bound the dm-result.py invocation time.
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _resolve_owner_id(token):
    """Return the Discord user ID for the human owner.

    Delegates the config-driven resolution chain to
    `discord_config.resolve_owner_id` (#1147) so this fallback delivery
    path and the live bridge (`discord-bridge.py:_poll_dm_fallback`)
    agree on a single owner. Drift between the two sites was the failure
    mode #846 created; the shared helper prevents it from recurring.

    The bot-filtering step (walk `allowFrom`, skip Discord bot accounts)
    stays here because it requires `GET /users/{id}` REST calls. Keeping
    the helper pure-Python lets both callers (this sync REST path and
    the bridge's async discord.py path) share the same chain.

    Set SUTANDO_DM_OWNER_ID in .env to skip even the helper's lookup
    (saves 1 API call per dm-result invocation); the env var is honored
    inside the helper as the first resolution step."""
    if not ACCESS_JSON.exists():
        # No plugin access.json — still try the helper for env override
        # or workspace-config `owner` field.
        owner = discord_config.resolve_owner_id({})
        return owner or ""
    try:
        data = json.loads(ACCESS_JSON.read_text())
    except Exception:
        data = {}

    owner = discord_config.resolve_owner_id(data)
    if owner:
        return owner

    allow = data.get("allowFrom") or []
    if not allow:
        return ""

    # Step 6: bot-filtered walk of allowFrom. Helper intentionally omits
    # this step (REST-bound). The first non-bot wins. If lookups all fail
    # (rate limit, network, bad token), fall through to allow[0] as a
    # degraded default so send_dm() produces an honest error later.
    for uid in allow:
        try:
            user = _discord_api("GET", f"/users/{uid}", token)
            if isinstance(user, dict) and not user.get("bot", False):
                return str(uid)
        except Exception:
            continue
    return str(allow[0])


def _open_dm_channel(owner_id: str, token: str) -> str:
    """Create/open a DM channel between this bot and owner_id. Returns the
    channel ID. Per Discord docs this endpoint is idempotent: if a DM already
    exists between the bot and the user, it returns that channel rather than
    creating a new one, so repeated calls are cheap."""
    resp = _discord_api("POST", "/users/@me/channels", token, {"recipient_id": owner_id})
    if isinstance(resp, dict) and "id" in resp:
        return str(resp["id"])
    raise RuntimeError(f"unexpected /users/@me/channels response: {resp!r}")


def send_dm(text: str) -> bool:
    """Send text to the resolved owner's Discord DM."""
    token = _load_token()
    if not token:
        print("dm-result: DISCORD_BOT_TOKEN not found in .env", file=sys.stderr)
        return False

    owner_id = _resolve_owner_id(token)
    if not owner_id:
        print("dm-result: could not resolve owner user ID (set SUTANDO_DM_OWNER_ID or populate access.json allowFrom)", file=sys.stderr)
        return False

    try:
        channel_id = _open_dm_channel(owner_id, token)
    except Exception as e:
        print(f"dm-result: failed to open DM channel with {owner_id}: {e}", file=sys.stderr)
        return False

    # Extract [file:|send:|attach:] markers. The WS-connected live
    # bridge calls `discord.File(path)` for each marker; this REST
    # path now builds the equivalent multipart upload (see
    # `_send_message_with_files`). Each marker path is allowlist-
    # checked against `_is_path_sendable` — same policy as
    # discord-bridge.py to bound exfil if an attacker-controlled marker
    # ever reaches a result body.
    parsed = parse_markers(text)
    clean_text = parsed.body.strip()
    marker_files = [
        action.value
        for action in parsed.actions
        if action.kind == "attach"
    ]
    expanded_files = [os.path.expanduser(p.strip()) for p in marker_files]
    sendable_files = [p for p in expanded_files if _is_path_sendable(p)]
    rejected_files = [p for p in expanded_files if not _is_path_sendable(p)]
    if rejected_files:
        print(
            f"dm-result: {len(rejected_files)} file marker(s) rejected by "
            f"allowlist (would deliver via [file:] but path is outside "
            f"_SEND_ALLOWED_ROOTS / _SEND_ALLOWED_PREFIXES): {rejected_files}",
            file=sys.stderr,
        )

    # Tell the RECIPIENT, not just the log. The comment that used to sit here
    # claimed "same security signal as discord-bridge", and that parity did not
    # exist: discord-bridge.py sends `(file not allowed: <path>)` into the
    # channel, while this path logged to stderr only. So an attachment could
    # silently never arrive — body delivered, task archived, nothing anywhere
    # telling the recipient a file was meant to be there.
    #
    # Worst exactly here: dm-result is the REST FALLBACK, used when the live
    # bridge is down, so its stderr is the least-watched output in the system.
    #
    # Split the same way discord-bridge does, because the two cases mean
    # different things:
    #   * path does not exist  -> almost always a `[file:/path]` substring
    #     inside prose (a quoted example). discord-bridge logs it and
    #     deliberately does NOT surface it; a notice here would fire on
    #     ordinary text that merely mentions a path.
    #   * path EXISTS but is outside the allowlist -> a real file the author
    #     meant to attach and the policy refused. That one the recipient needs.
    blocked = [p for p in rejected_files if os.path.isfile(p)]
    if blocked:
        # The path itself is NOT echoed. discord-bridge prints it, but a
        # rejected marker is by definition outside the allowlist and could be
        # attacker-chosen if a marker ever reaches a result body from untrusted
        # input. The count plus the reason tells the recipient an attachment was
        # dropped without echoing an arbitrary string back out; paths stay in
        # stderr above.
        plural = "" if len(blocked) == 1 else "s"
        notice = ("_({} attachment{} not sent — outside the send allowlist; "
                  "path{} in the dm-result log)_").format(
                      len(blocked), plural, plural)
        clean_text = f"{clean_text}\n\n{notice}" if clean_text else notice

    # An all-marker / all-whitespace body becomes empty after strip.
    # Sending `""` to Discord returns 400 ("Cannot send an empty
    # message") for a text-only request. For a multipart upload with
    # files, an empty `content` is valid.
    if not clean_text and not sendable_files:
        print(
            f"dm-result: body is empty after marker-strip and no sendable "
            f"files; nothing to send (channel {channel_id})"
        )
        return True  # not an error — the input had no deliverable payload

    # Chunk text into Discord-safe pieces, preserving code fences
    # across boundaries.
    chunks = list(_chunk_for_discord(clean_text)) if clean_text else []
    for i, chunk in enumerate(chunks):
        try:
            _discord_api("POST", f"/channels/{channel_id}/messages", token, {"content": chunk})
        except Exception as e:
            print(
                f"dm-result: failed to send DM chunk {i+1}/{len(chunks)} to channel {channel_id}: {e}",
                file=sys.stderr,
            )
            return False

    # Attach files in batches of 10 (Discord per-message attachment
    # cap). Each batch goes as a separate multipart message with
    # empty content — the text was already delivered as chunks above.
    DISCORD_FILES_PER_MESSAGE = 10
    for batch_start in range(0, len(sendable_files), DISCORD_FILES_PER_MESSAGE):
        batch = sendable_files[batch_start : batch_start + DISCORD_FILES_PER_MESSAGE]
        try:
            _send_message_with_files(channel_id, token, "", batch)
        except Exception as e:
            print(
                f"dm-result: failed to upload file batch "
                f"{batch_start // DISCORD_FILES_PER_MESSAGE + 1} "
                f"({len(batch)} file(s)) to channel {channel_id}: {e}",
                file=sys.stderr,
            )
            return False

    file_summary = f", {len(sendable_files)} file(s)" if sendable_files else ""
    print(
        f"dm-result: sent to DM ({len(clean_text)} chars in {len(chunks)} chunk(s)"
        f"{file_summary}) via channel {channel_id}"
    )
    try:
        import outbox_log
        outbox_log.append(
            channel_type="discord_dm",
            recipient=str(owner_id),
            body=text,
            recipient_label="owner DM (via dm-result.py)",
        )
    except Exception:
        pass
    return True


def main():
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    # This script intentionally accepts free-form positional text, so a normal
    # argparse parser would reject legitimate messages beginning with a dash.
    # Still honor the two conventional help flags before any voice/network
    # checks: otherwise `--help` is interpreted as message text and delivered
    # to the owner's DM.
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        return

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Usage: python3 src/dm-result.py --file path", file=sys.stderr)
            sys.exit(1)
        text = Path(sys.argv[2]).read_text().strip()
    else:
        text = " ".join(sys.argv[1:])

    # Honor the skip markers before any delivery path. This script is the LAST
    # consumer in the result chain (poll_dm_fallback shells out to it only when
    # nothing else claimed the file), so a marker it ignores becomes exactly the
    # DM the marker existed to prevent:
    #   [no-send]      internally handled, no user-visible reply
    #   [REPLIED]      already delivered through another path
    #   [deduped: …]   superseded by another task's result
    # The file markers below are already parsed for the same stated reason —
    # "without parsing these markers it would deliver the literal text" — and
    # that argument is stronger here, since these do not merely look wrong in a
    # DM, they mean do-not-deliver.
    skip = next((a for a in parse_markers(text).actions if a.kind == "skip"), None)
    if skip:
        print(f"dm-result: [{skip.value}] marker — not delivering")
        return

    if (REPO / "state" / "dm-ban.sentinel").exists():
        print("dm-result: dm-ban.sentinel present — not delivering")
        return

    if voice_connected():
        print("dm-result: voice client connected, skipping DM (voice will deliver)")
        return

    print("dm-result: voice client disconnected, sending to Discord DM")
    if send_dm(text):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
