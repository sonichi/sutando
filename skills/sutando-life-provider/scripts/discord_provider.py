#!/usr/bin/env python3
"""Bounded read-only Discord provider edge for Sutando Life."""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO / "src"))

from channel_token import resolve_channel_token  # noqa: E402
from discord_http import request_json  # noqa: E402
from util_paths import channel_access_path, claude_home_path  # noqa: E402


CAPABILITY_ID = "discord.activity"
OPERATIONS = ("identity.get", "context.get", "channel.messages.delta")
MAX_ITEMS = 100
OVERLAP_SECONDS = 300
MAX_CONTENT_CHARS = 400
MAX_ATTACHMENTS = 2
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/sonichi/sutando, 1.0)"
DISCORD_EPOCH_MS = 1420070400000
_SNOWFLAKE_RE = re.compile(r"^[1-9][0-9]{5,19}$")
_Requester = Callable[..., Any]


class ProviderFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool,
                 setup: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.setup = setup


def _token_setup() -> dict:
    return {
        "kind": "credential",
        "label": "Store the Discord bot token in the local Sutando vault",
        "command": ["vault", "set", "DISCORD_BOT_TOKEN"],
        "restartRequired": True,
    }


def _access_setup() -> dict:
    return {
        "kind": "configuration",
        "label": "Authorize the Discord channel with /discord:access",
        "restartRequired": False,
    }


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        return MAX_ITEMS
    try:
        return max(1, min(MAX_ITEMS, int(value)))
    except (TypeError, ValueError):
        return MAX_ITEMS


def _text(value: Any, maximum: int = 240) -> Optional[str]:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    return clean[:maximum] if clean else None


def _is_snowflake(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _SNOWFLAKE_RE.fullmatch(value) is not None
        and int(value) < 2 ** 64
    )


def _failure_from_http(error: urllib.error.HTTPError) -> ProviderFailure:
    if error.code == 401:
        return ProviderFailure(
            "authorization_required",
            "Discord authorization is required on this Sutando host.",
            retryable=False,
            setup=_token_setup(),
        )
    if error.code == 429:
        return ProviderFailure(
            "rate_limited", "Discord temporarily rate-limited this read.", retryable=True,
        )
    if error.code in (403, 404):
        return ProviderFailure(
            "permission_limited",
            "The local Discord bot cannot read this authorized context.",
            retryable=False,
        )
    if 500 <= error.code < 600:
        return ProviderFailure(
            "provider_unavailable", "Discord is temporarily unavailable.", retryable=True,
        )
    return ProviderFailure(
        "provider_error", "Discord could not complete this read.", retryable=True,
    )


def _api_get(requester: _Requester, token: str, path: str,
             query: Optional[Mapping[str, str]] = None) -> Any:
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": USER_AGENT,
    })
    try:
        return requester(req, timeout=4, max_retries=0)
    except urllib.error.HTTPError as exc:
        raise _failure_from_http(exc) from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise ProviderFailure(
            "provider_unavailable", "Discord is temporarily unavailable.", retryable=True,
        ) from None
    except (TypeError, ValueError):
        raise ProviderFailure(
            "invalid_provider_response", "Discord returned an invalid response.", retryable=True,
        ) from None
    except Exception:
        raise ProviderFailure(
            "provider_error", "Discord could not complete this read.", retryable=True,
        ) from None


def _error(operation: str, failure: ProviderFailure) -> dict:
    error = {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
    }
    if failure.setup:
        error["setup"] = failure.setup
    return {
        "ok": False,
        "capabilityId": CAPABILITY_ID,
        "operation": operation,
        "partial": False,
        "items": [],
        "error": error,
    }


def _invalid(operation: str, code: str, message: str) -> dict:
    return _error(operation, ProviderFailure(code, message, retryable=False))


def _avatar_url(row: Mapping[str, Any]) -> Optional[str]:
    user_id = row.get("id")
    avatar = row.get("avatar")
    if user_id is None or not avatar:
        return None
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"


def _identity(row: Mapping[str, Any]) -> dict:
    user_id = str(row.get("id"))
    return {
        "id": f"discord-user:{user_id}",
        "providerId": user_id,
        "username": _text(row.get("username"), 100),
        "globalName": _text(row.get("global_name"), 160),
        "bot": bool(row.get("bot")),
        "avatarUrl": _avatar_url(row),
        "evidenceUrl": f"https://discord.com/users/{user_id}",
    }


def _load_access(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, TypeError, ValueError):
        raise ProviderFailure(
            "local_authorization_unavailable",
            "Discord local authorization is missing or unreadable.",
            retryable=False,
            setup=_access_setup(),
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get("allowFrom"), list):
        raise ProviderFailure(
            "local_authorization_unavailable",
            "Discord local authorization is missing or unreadable.",
            retryable=False,
            setup=_access_setup(),
        )
    return data


def _channel_resource(params: Mapping[str, Any]) -> str:
    resource = params.get("resource")
    if not isinstance(resource, Mapping) or set(resource) != {"channelId"}:
        raise ProviderFailure(
            "invalid_resource", "resource.channelId must be a Discord channel ID.",
            retryable=False,
        )
    channel_id = resource.get("channelId")
    if not _is_snowflake(channel_id):
        raise ProviderFailure(
            "invalid_resource", "resource.channelId must be a Discord channel ID.",
            retryable=False,
        )
    return channel_id


def _authorization_kind(access: Mapping[str, Any], channel: Mapping[str, Any],
                        bot_id: str) -> Optional[str]:
    channel_id = str(channel.get("id") or "")
    groups = access.get("groups") if isinstance(access.get("groups"), Mapping) else {}
    configured = groups.get(channel_id)
    if configured is True or isinstance(configured, Mapping):
        return "configured_channel"

    channel_type = channel.get("type")
    recipients = channel.get("recipients")
    if channel_type not in (1, 3) or not isinstance(recipients, list):
        return None
    recipient_ids = {
        str(row.get("id"))
        for row in recipients
        if isinstance(row, Mapping) and row.get("id") is not None
        and str(row.get("id")) != bot_id
    }
    allowed = {str(value) for value in access.get("allowFrom", [])}
    if recipient_ids and recipient_ids.issubset(allowed):
        return "allowlisted_dm"
    return None


def _authorized_channel(requester: _Requester, token: str, access_path: Path,
                        params: Mapping[str, Any], bot_id: str) -> tuple[dict, str]:
    channel_id = _channel_resource(params)
    access = _load_access(access_path)
    row = _api_get(requester, token, f"/channels/{channel_id}")
    if not isinstance(row, Mapping) or str(row.get("id") or "") != channel_id:
        raise ProviderFailure(
            "invalid_provider_response", "Discord returned an invalid channel.", retryable=True,
        )
    kind = _authorization_kind(access, row, bot_id)
    if kind is None:
        raise ProviderFailure(
            "permission_limited",
            "This Discord channel is not authorized by the local Sutando access policy.",
            retryable=False,
            setup=_access_setup(),
        )
    return dict(row), kind


def _channel_kind(value: Any) -> str:
    return {
        0: "guild_text",
        1: "dm",
        3: "group_dm",
        5: "announcement",
        10: "announcement_thread",
        11: "public_thread",
        12: "private_thread",
        15: "forum",
    }.get(value, "other")


def _channel_evidence(channel: Mapping[str, Any]) -> str:
    channel_id = str(channel.get("id"))
    guild_id = channel.get("guild_id")
    scope = str(guild_id) if guild_id is not None else "@me"
    return f"https://discord.com/channels/{scope}/{channel_id}"


def _context(channel: Mapping[str, Any], authorization_kind: str) -> dict:
    channel_id = str(channel.get("id"))
    guild_id = channel.get("guild_id")
    return {
        "id": f"discord-channel:{channel_id}",
        "providerId": channel_id,
        "name": _text(channel.get("name"), 160),
        "kind": _channel_kind(channel.get("type")),
        "guild": ({
            "id": f"discord-guild:{guild_id}",
            "providerId": str(guild_id),
        } if guild_id is not None else None),
        "localAuthorization": authorization_kind,
        "evidenceUrl": _channel_evidence(channel),
    }


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _decode_cursor(value: Any) -> Optional[tuple[datetime, str]]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"version", "ts", "id"}:
        raise ValueError("unsupported cursor")
    provider_id = value.get("id")
    if value.get("version") != 1 or not isinstance(provider_id, str):
        raise ValueError("invalid cursor")
    if not _is_snowflake(provider_id):
        raise ValueError("invalid cursor")
    return _parse_time(value.get("ts")), provider_id


def _snowflake_at(value: datetime) -> str:
    millis = int(value.timestamp() * 1000)
    return str(max(0, millis - DISCORD_EPOCH_MS) << 22)


def _snowflake_time(value: str) -> str:
    millis = (int(value) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _message_content(row: Mapping[str, Any]) -> Optional[str]:
    body = str(row.get("content") or "").strip()
    snapshots = row.get("message_snapshots")
    if isinstance(snapshots, list) and snapshots:
        snapshot = snapshots[0] if isinstance(snapshots[0], Mapping) else {}
        forwarded = snapshot.get("message") if isinstance(snapshot, Mapping) else {}
        forwarded_body = (
            str(forwarded.get("content") or "").strip()
            if isinstance(forwarded, Mapping) else ""
        )
        if forwarded_body:
            body = " ".join(part for part in (body, f"[forwarded] {forwarded_body}") if part)
    return _text(body, MAX_CONTENT_CHARS)


def _message(row: Mapping[str, Any], channel: Mapping[str, Any]) -> dict:
    channel_id = str(channel.get("id"))
    message_id = str(row.get("id"))
    guild_id = channel.get("guild_id")
    scope = str(guild_id) if guild_id is not None else "@me"
    evidence = f"https://discord.com/channels/{scope}/{channel_id}/{message_id}"
    author = row.get("author") if isinstance(row.get("author"), Mapping) else {}
    author_id = str(author.get("id")) if author.get("id") is not None else None
    reference = row.get("message_reference")
    reply_id = (
        str(reference.get("message_id"))
        if isinstance(reference, Mapping) and reference.get("message_id") is not None else None
    )
    attachments = row.get("attachments") if isinstance(row.get("attachments"), list) else []
    return {
        "id": f"discord-message:{message_id}",
        "providerId": message_id,
        "source": "discord",
        "channelId": f"discord-channel:{channel_id}",
        "createdAt": row.get("timestamp") or _snowflake_time(message_id),
        "editedAt": row.get("edited_timestamp"),
        "content": _message_content(row),
        "author": {
            "id": f"discord-user:{author_id}" if author_id else None,
            "providerId": author_id,
            "username": _text(author.get("username"), 80),
            "globalName": _text(author.get("global_name"), 120),
            "bot": bool(author.get("bot")),
            "avatarUrl": _avatar_url(author),
        },
        "replyToId": f"discord-message:{reply_id}" if reply_id else None,
        "replyToEvidenceUrl": (
            f"https://discord.com/channels/{scope}/{channel_id}/{reply_id}"
            if reply_id else None
        ),
        "attachments": [
            {
                "id": f"discord-attachment:{item.get('id')}",
                "filename": _text(item.get("filename"), 100),
                "contentType": _text(item.get("content_type"), 60),
                "size": item.get("size") if isinstance(item.get("size"), int) else None,
            }
            for item in attachments[:MAX_ATTACHMENTS]
            if isinstance(item, Mapping) and item.get("id") is not None
        ],
        "evidenceUrl": evidence,
    }


def _next_cursor(rows: list[Mapping[str, Any]],
                 previous: Optional[tuple[datetime, str]]) -> Optional[dict]:
    if rows:
        newest = max(rows, key=lambda row: int(str(row.get("id"))))
        provider_id = str(newest.get("id"))
        if previous and int(provider_id) <= int(previous[1]):
            return {
                "version": 1,
                "ts": previous[0].isoformat().replace("+00:00", "Z"),
                "id": previous[1],
            }
        try:
            timestamp = _parse_time(newest.get("timestamp")).isoformat().replace(
                "+00:00", "Z"
            )
        except (TypeError, ValueError):
            timestamp = _snowflake_time(provider_id)
        return {"version": 1, "ts": timestamp, "id": provider_id}
    if previous:
        return {
            "version": 1,
            "ts": previous[0].isoformat().replace("+00:00", "Z"),
            "id": previous[1],
        }
    return None


def _reader(requester: _Requester, token: str, access_path: Path,
            availability: str, identity_snapshot: Optional[Mapping[str, Any]]) -> Callable[[dict], dict]:
    def read(params: dict) -> dict:
        operation = str(params.get("operation") or "")
        if operation not in OPERATIONS:
            return _invalid(operation, "unsupported_operation", "Unsupported Discord operation.")
        if availability == "authorization_required":
            return _error(operation, ProviderFailure(
                "authorization_required",
                "Discord authorization is required on this Sutando host.",
                retryable=False,
                setup=_token_setup(),
            ))
        if availability != "ready" or identity_snapshot is None:
            return _error(operation, ProviderFailure(
                "provider_unavailable", "Discord is temporarily unavailable.", retryable=True,
            ))
        try:
            if operation == "identity.get":
                return {
                    "ok": True,
                    "capabilityId": CAPABILITY_ID,
                    "operation": operation,
                    "partial": False,
                    "items": [_identity(identity_snapshot)],
                    "limitations": [],
                }
            if operation == "context.get":
                channel, auth_kind = _authorized_channel(
                    requester, token, access_path, params, str(identity_snapshot.get("id")),
                )
                return {
                    "ok": True,
                    "capabilityId": CAPABILITY_ID,
                    "operation": operation,
                    "partial": False,
                    "items": [_context(channel, auth_kind)],
                    "limitations": [],
                }
            return _read_messages(
                requester, token, access_path, params,
                str(identity_snapshot.get("id")), operation,
            )
        except ProviderFailure as failure:
            return _error(operation, failure)

    return read


def _read_messages(requester: _Requester, token: str, access_path: Path,
                   params: dict, bot_id: str, operation: str) -> dict:
    try:
        previous = _decode_cursor(params.get("cursor"))
    except (TypeError, ValueError):
        return _invalid(operation, "invalid_cursor", "Message cursor is invalid or unsupported.")
    channel, _ = _authorized_channel(requester, token, access_path, params, bot_id)
    if channel.get("type") not in (0, 1, 3, 5, 10, 11, 12):
        return _invalid(operation, "unsupported_resource", "Discord context has no messages.")
    limit = _bounded_limit(params.get("limit"))
    query = {"limit": str(limit)}
    floor_id = None
    if previous:
        floor = previous[0] - timedelta(seconds=OVERLAP_SECONDS)
        floor_id = _snowflake_at(floor)
        query["after"] = floor_id
    first_rows = _api_get(
        requester, token, f"/channels/{channel.get('id')}/messages", query,
    )
    if not isinstance(first_rows, list):
        raise ProviderFailure(
            "invalid_provider_response", "Discord returned an invalid message list.",
            retryable=True,
        )
    pages = [first_rows]
    full = len(first_rows) >= limit
    if previous and full:
        page_ids = [
            int(row["id"])
            for row in first_rows[:limit]
            if isinstance(row, Mapping) and isinstance(row.get("id"), str)
            and _is_snowflake(row["id"])
        ]
        forward_after = str(max([int(previous[1]), *page_ids]))
        forward_rows = _api_get(
            requester, token, f"/channels/{channel.get('id')}/messages",
            {"limit": str(limit), "after": forward_after},
        )
        if not isinstance(forward_rows, list):
            raise ProviderFailure(
                "invalid_provider_response", "Discord returned an invalid message list.",
                retryable=True,
            )
        pages.append(forward_rows)
    rows = [row for page in pages for row in page]
    bounded = [row for page in pages for row in page[:limit]]
    valid = [
        row for row in bounded
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
        and _is_snowflake(row["id"])
    ]
    if floor_id is not None:
        valid = [row for row in valid if int(row["id"]) >= int(floor_id)]
    valid.sort(key=lambda row: int(row["id"]))
    omitted = len(bounded) - len(valid)
    valid = valid[-limit:]
    limitations = []
    if full:
        limitations.append({
            "code": "response_limit_reached",
            "message": "Additional channel messages may exist beyond this bounded read.",
        })
    if omitted:
        limitations.append({
            "code": "invalid_items_omitted",
            "message": "Discord returned message rows that could not be represented safely.",
        })
    return {
        "ok": True,
        "capabilityId": CAPABILITY_ID,
        "operation": operation,
        "partial": full or bool(omitted),
        "items": [_message(row, channel) for row in valid],
        "nextCursor": _next_cursor(valid, previous),
        "coverage": {
            "overlapSeconds": OVERLAP_SECONDS,
            "gapPossible": full,
            "received": len(rows),
            "returned": len(valid),
            "omitted": omitted,
        },
        "limitations": limitations,
    }


def registry_inputs(
    *, requester: Optional[_Requester] = None, token: Optional[str] = None,
    access_path: Optional[Path] = None, env_file: Optional[Path] = None,
    vault_get=None,
) -> tuple[dict[str, dict], dict[str, Callable[[dict], dict]]]:
    api_request = requester or request_json
    resolved_access = Path(access_path) if access_path is not None else channel_access_path(
        "discord"
    )
    resolved_env = Path(env_file) if env_file is not None else claude_home_path(
        "channels", "discord", ".env"
    )
    resolved_token = (
        resolve_channel_token(
            "DISCORD_BOT_TOKEN", env_file=resolved_env, vault_get=vault_get,
        ) if token is None else token.strip()
    )
    identity_snapshot: Optional[Mapping[str, Any]] = None
    setup = None
    if not resolved_token:
        availability = "authorization_required"
        setup = _token_setup()
    else:
        try:
            probe = _api_get(api_request, resolved_token, "/users/@me")
            if not isinstance(probe, Mapping) or probe.get("id") is None or not probe.get(
                "username"
            ):
                raise ProviderFailure(
                    "invalid_provider_response", "Discord returned an invalid identity.",
                    retryable=True,
                )
            identity_snapshot = {
                key: probe.get(key)
                for key in ("id", "username", "global_name", "avatar", "bot")
            }
            availability = "ready"
        except ProviderFailure as failure:
            availability = (
                "authorization_required"
                if failure.code == "authorization_required" else "unavailable"
            )
            setup = _token_setup() if availability == "authorization_required" else None
    descriptor = {
        "id": CAPABILITY_ID,
        "version": 1,
        "availability": availability,
        "description": (
            "Read the local Sutando Discord bot identity, authorized context, "
            "and recent channel messages."
        ),
        "operations": list(OPERATIONS),
        "constraints": {
            "readOnly": True,
            "maxItems": MAX_ITEMS,
            "messageOverlapSeconds": OVERLAP_SECONDS,
            "maxContentChars": MAX_CONTENT_CHARS,
            "maxAttachmentsPerMessage": MAX_ATTACHMENTS,
            "localAuthorizationRequired": True,
            "availabilitySnapshot": "runtime-start",
        },
        "setup": setup or {},
        "metadata": {"provider": "discord", "credentialOwner": "sutando"},
    }
    if identity_snapshot is not None:
        descriptor["identity"] = {
            "id": f"discord-user:{identity_snapshot.get('id')}",
            "username": _text(identity_snapshot.get("username"), 100),
        }
    return (
        {CAPABILITY_ID: descriptor},
        {CAPABILITY_ID: _reader(
            api_request, resolved_token, resolved_access, availability, identity_snapshot,
        )},
    )


__all__ = ["CAPABILITY_ID", "MAX_ITEMS", "OPERATIONS", "OVERLAP_SECONDS", "registry_inputs"]
