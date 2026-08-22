#!/usr/bin/env python3
"""Outcome-aware Discord REST client — one transport, three request classes.

Five Discord call sites grew their own HTTP (discord_http.request_json,
dm-result._discord_api, the bridge's _send_via_rest/_edit_via_rest, the gated
reader's _api_get), and the drift that matters is not headers or timeouts but
RETRY SEMANTICS. A GET can be retried on 429/5xx freely. A message POST cannot:
Discord may have created the message before the connection died, so a
transparent retry is a duplicate-send machine. The classes:

  read      GET            retried (429 Retry-After + transient 5xx)
  control   open-DM        retried (idempotent server-side: same DM returned)
  delivery  send/edit/file SINGLE attempt -> outbox_adapter.classify_response

Delivery methods never retry privately (outbox_adapter.DeliveryAdapter states
why: a private retry is invisible to the core's attempt budget). They return
the canonical DeliveryReceipt; `edit_message` marks RetrySafety.SAFE because
the target message id is fixed, so the CALLER's budget may repeat it.

Every hand-rolled sender now binds this client (census pinned by
tests/discord-post-census.test.py), which is what makes the injected
post-gate `validator` structural: covering the client covers every sender.
"""
from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from channels.discord.http import request_json
from outbox_adapter import DeliveryReceipt, classify_response
from outbox import DeliveryOutcome, RetrySafety

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/sonichi/sutando, 1.0)"

# Discord's message-create/edit response names its receipt `id`. Pinned per
# classify_response's instruction: a broad default widens what counts as proof.
_DISCORD_ID_KEYS = ("id",)


def _default_transport(req, timeout):
    """One urlopen -> (status, parsed-json-or-text). Raises on transport failure.

    A read that dies AFTER urlopen returned still yields (status, None): the
    server committed the write, and reporting "no response" there invites the
    caller-side retry that duplicates it."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.status
        try:
            raw = resp.read().decode("utf-8", "replace")
        except Exception:
            return status, None
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw


class DiscordRestClient:
    """`transport` is injectable (callable(req, timeout) -> (status, body)) so
    the contract tests drive every outcome without a network.

    `validator` is the post-gate injection point: callable(channel_id, payload)
    returning a falsy value to allow or a refusal reason (str) to block. The
    repo ships the MECHANISM only — the policy (e.g. a chain-check ruleset) is
    injected by the personal layer, and its config may select WHICH ruleset
    applies to a channel, never WHETHER one does. A validator that raises
    fails CLOSED: an unvalidated post is the thing the gate exists to stop."""

    def __init__(self, token: str, transport=None, timeout: int = 15,
                 validator=None):
        self._token = token
        self._transport = transport or _default_transport
        self._timeout = timeout
        self._validator = validator

    def _gate_refusal(self, channel_id, payload: dict):
        """The refusing DeliveryReceipt when the injected validator blocks this
        (channel_id, payload), else None. No validator -> no refusal."""
        if self._validator is None:
            return None
        try:
            reason = self._validator(str(channel_id), payload)
        except Exception as e:  # noqa: BLE001 — a broken gate must not fail open
            return DeliveryReceipt(
                DeliveryOutcome.NOT_DELIVERED,
                detail=f"validator crashed ({type(e).__name__}: {e}) — "
                       "refusing the unvalidated send")
        if reason:
            return DeliveryReceipt(DeliveryOutcome.NOT_DELIVERED,
                                   detail=f"refused by validator: {reason}")
        return None

    def _headers(self, content_type: str | None = "application/json"):
        h = {"Authorization": f"Bot {self._token}", "User-Agent": UA}
        if content_type:
            h["Content-Type"] = content_type
        return h

    # ── reads: retried freely (429 Retry-After + transient 5xx) ─────────────
    def get_json(self, path: str):
        req = urllib.request.Request(API + path, headers=self._headers(None))
        return request_json(req, timeout=self._timeout)

    def get_channel(self, channel_id):
        return self.get_json(f"/channels/{channel_id}")

    def get_user(self, user_id):
        return self.get_json(f"/users/{user_id}")

    def list_messages(self, channel_id, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.get_json(f"/channels/{channel_id}/messages{q}")

    # ── control: bounded retry is safe (server-side idempotent) ─────────────
    def create_dm_channel(self, recipient_id) -> str | None:
        """Open (or fetch the existing) DM channel; returns its id or None."""
        req = urllib.request.Request(
            f"{API}/users/@me/channels",
            data=json.dumps({"recipient_id": str(recipient_id)}).encode(),
            headers=self._headers(), method="POST")
        body = request_json(req, timeout=self._timeout)
        return str(body.get("id")) if isinstance(body, dict) and body.get("id") else None

    # ── delivery: SINGLE attempt, canonical receipt, never a private retry ──
    def _deliver(self, req) -> tuple[int | None, object]:
        try:
            return self._transport(req, self._timeout)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
                body = json.loads(raw) if raw else None
            except Exception:
                body = None
            return e.code, body
        except Exception:
            # timeout / connection reset / DNS: no status reached us.
            return None, None

    def send_message(self, channel_id, payload: dict) -> DeliveryReceipt:
        return self.send_message_with_response(channel_id, payload)[0]

    def send_message_with_response(self, channel_id, payload: dict):
        """-> (receipt, http_status_or_None, parsed_body). For senders that must
        inspect the created message (mention resolution) or report commitment
        honestly (a 2xx without an id is committed, not failed). The receipt is
        the same canonical one `send_message` returns."""
        refused = self._gate_refusal(channel_id, payload)
        if refused:
            return refused, None, None
        req = urllib.request.Request(
            f"{API}/channels/{channel_id}/messages",
            data=json.dumps(payload).encode(), headers=self._headers(), method="POST")
        status, body = self._deliver(req)
        return classify_response(status, body, id_keys=_DISCORD_ID_KEYS), status, body

    def edit_message(self, channel_id, message_id, payload: dict) -> DeliveryReceipt:
        return self.edit_message_with_response(channel_id, message_id, payload)[0]

    def edit_message_with_response(self, channel_id, message_id, payload: dict):
        """Edit-shaped `send_message_with_response` (same tuple contract)."""
        refused = self._gate_refusal(channel_id, payload)
        if refused:
            return refused, None, None
        req = urllib.request.Request(
            f"{API}/channels/{channel_id}/messages/{message_id}",
            data=json.dumps(payload).encode(), headers=self._headers(), method="PATCH")
        status, body = self._deliver(req)
        receipt = classify_response(status, body, id_keys=_DISCORD_ID_KEYS)
        # The target id is fixed: a repeat cannot create a second message, so
        # the caller's budget MAY retry. The attempt itself is still single.
        return dataclasses.replace(receipt, safety=RetrySafety.SAFE), status, body

    def upload_files(self, channel_id, payload: dict, files) -> DeliveryReceipt:
        """`files` = [(filename, bytes)]. Multipart per Discord's files[n] form."""
        refused = self._gate_refusal(channel_id, payload)
        if refused:
            return refused
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="payload_json"\r\nContent-Type: application/json\r\n\r\n'
                 f"{json.dumps(payload)}\r\n".encode()]
        for i, (name, blob) in enumerate(files):
            # CR/LF/quote in a caller-supplied filename would inject multipart
            # headers; same policy as dm-result / _safe_attachment_basename.
            safe = (str(name).replace("\r", "_").replace("\n", "_")
                    .replace('"', "_"))[:80] or f"file-{i}"
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="files[{i}]"; filename="{safe}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode()
                + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{API}/channels/{channel_id}/messages", data=b"".join(parts),
            headers=self._headers(f"multipart/form-data; boundary={boundary}"),
            method="POST")
        status, body = self._deliver(req)
        return classify_response(status, body, id_keys=_DISCORD_ID_KEYS)
