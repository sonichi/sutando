#!/usr/bin/env python3
"""
remote-gateway-bridge.py — generic client that bridges a REMOTE task gateway to the
local Sutando file queue, so the local core processes remote tasks unchanged.

This is the OPEN, provider-agnostic half of the "agent as a service" design: a
gateway service holds the platform connection and
exposes a tiny HTTP protocol; this client pulls *your* tasks down into the local
`tasks/` queue and pushes results back up. No provider-specific logic lives here
— that's the gateway's job.

Full spec: docs/remote-gateway-protocol.md

  Protocol (versioned, Bearer-auth):
  GET  {REMOTE_TASK_URL}/v1/tasks?wait=<sec>
       → 200 {"tasks": [ {<task fields...>}, ... ]}   (long-poll; [] on timeout)
  POST {REMOTE_TASK_URL}/v1/tasks/<task-id>/ack
       → body {"id": "<task-id>"}  → 200 on accepted
  POST {REMOTE_TASK_URL}/v1/results
       → body {"id": "<task-id>", "body": "<result text>"}  → 200 on accepted
  POST {REMOTE_TASK_URL}/v1/heartbeat
       → body {"client": "...", "inflight": N, ...}  → 200 on accepted

Each task object uses the same schema Sutando's other bridges write, so this
client just serializes it to `tasks/task-<id>.txt` and the core handles it like
any Discord/Telegram/Slack task. When `results/task-<id>.txt` appears, its body
is POSTed back and the result file is archived. Ack/heartbeat are best-effort:
if an older gateway returns 404/405, the client keeps working against the
original pull/result protocol.

Config (env / .env):
  REMOTE_TASK_TOKEN      the onboarding string — the ONLY required setting
                        (combined "https://<gateway>|<secret>" or a bare secret)
  REMOTE_TASK_URL        gateway base URL (only needed with a bare secret)
  REMOTE_TASK_URL/_TOKEN  legacy aliases
  REMOTE_TASK_PROVIDER  label used for the task `source:` field (default "remote")
  REMOTE_TASK_CHANNEL_DIR  name of this instance's config dir under
                        $CLAUDE_CONFIG_DIR/channels/ (default "ag2space") —
                        selects which .env fallback and access.json a bridge
                        instance reads, so a second instance (e.g. a dev
                        homeserver's "dev-ag2space") cannot inherit prod's
                        credentials or tier map. Env-only by necessity: the
                        .env file cannot name its own directory.
  REMOTE_TASK_POLL_WAIT long-poll seconds (default 25)
  REMOTE_OUTBOUND_SCAN_S outbound worker scan period seconds (default 1.0)

Stdlib only (urllib) — no new dependencies.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import uuid
import re
import shlex
import signal
import socket
import sys
import tempfile
import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

# Prefer IPv4 for gateway/relay connections. The relay host (e.g. chat.ag2.space)
# publishes AAAA records, but some hosts have IPv6 black-holed at the network
_DNS_TIMEOUT_S = float(os.environ.get("REMOTE_GATEWAY_DNS_TIMEOUT") or "8")
_PREFER_V4 = os.environ.get("REMOTE_GATEWAY_ALLOW_IPV6") != "1"
# Reload-safe original capture: on module re-exec/reload, socket.getaddrinfo is
# already our wrapper — capturing it blindly makes _resolve_bounded call itself
_orig_getaddrinfo = getattr(socket.getaddrinfo, "_ag2_orig_getaddrinfo", socket.getaddrinfo)


class _InflightResolve:
    """One outstanding getaddrinfo call: waiters share its Event + outcome."""

    __slots__ = ("done", "result", "err")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.err = None


# Single-flight registry: at most ONE resolver thread exists per distinct
# (host, args) key. While a call is outstanding — including one wedged on a
_INFLIGHT: dict = {}
_INFLIGHT_LOCK = threading.Lock()


def _resolve_bounded(host, *args, **kwargs):
    """socket.getaddrinfo with a hard wall-clock bound.

    getaddrinfo cannot be interrupted, so the actual call runs in a daemon
    thread; the caller waits up to _DNS_TIMEOUT_S on its completion Event and
    raises gaierror on overrun (urllib surfaces that as the URLError the poll
    loop's reconnect branch already handles). The thread is shared single-
    flight per (host, args) key — see _INFLIGHT — so repeated retries against
    a wedged resolver never accumulate threads.
    """
    if _DNS_TIMEOUT_S <= 0:
        return _orig_getaddrinfo(host, *args, **kwargs)
    try:
        key = (host, args, tuple(sorted(kwargs.items())))
    except TypeError:  # unhashable arg — never true of real getaddrinfo calls
        key = None

    with _INFLIGHT_LOCK:
        call = _INFLIGHT.get(key) if key is not None else None
        if call is None:
            call = _InflightResolve()
            if key is not None:
                _INFLIGHT[key] = call

            def _run(call=call, key=key):
                try:
                    call.result = _orig_getaddrinfo(host, *args, **kwargs)
                except BaseException as e:  # noqa: BLE001 — re-raised to waiters
                    call.err = e
                finally:
                    # Clear the slot BEFORE signalling: a waiter woken by the
                    # Event must never re-attach to a completed call.
                    if key is not None:
                        with _INFLIGHT_LOCK:
                            _INFLIGHT.pop(key, None)
                    call.done.set()

            threading.Thread(target=_run, name="dns-resolve", daemon=True).start()

    if not call.done.wait(_DNS_TIMEOUT_S):
        raise socket.gaierror(
            f"DNS resolution for {host!r} exceeded {_DNS_TIMEOUT_S}s (resolver hung)"
        )
    if call.err is not None:
        raise call.err
    return call.result


def _getaddrinfo_prefer_v4(host, *args, **kwargs):
    infos = _resolve_bounded(host, *args, **kwargs)
    if _PREFER_V4 and host and "ag2.space" in str(host):
        v4 = [i for i in infos if i[0] == socket.AF_INET]
        return v4 or infos
    return infos


_getaddrinfo_prefer_v4._ag2_orig_getaddrinfo = _orig_getaddrinfo
socket.getaddrinfo = _getaddrinfo_prefer_v4

# resolve_workspace lives alongside this file in src/ — put THIS directory on
# the path (no repo-walking; the old triple-parent form predated the move into
from ._dirs import task_dir as _task_dir, result_dir as _result_dir, state_dir as _state_dir
from .chat_secret_filter import filter_chat_secrets, secret_handling_instruction
from .task_archive import find_task_file
from .local_task_protocol import find_archived_task
from . import local_task_protocol
from .result_markers import parse_markers
from .team_guardrail import (team_guardrail_lines, engage_rulebook,
                             AG2SPACE_PROVENANCE, sandboxed_delegation_lines)
from . import team_result_guard
from .outbox import DeliveryOutcome, record_delivered
from .outbox_adapter import classify_response
from .send_failure_policy import MAX_TRANSIENT_ATTEMPTS, resolve_failed_send
from .delivery_core import (DeliveryCore, DesignAClaimBackend, DrainStatus,
                            RetryPolicy)
from .delivery_core import DeliveryOutcome as CoreDeliveryOutcome
from .delivery_core.provider_ag2space import AG2SpaceResultProvider
from .result_ready import read_ready_result
from .dedup_recovery import plan_dedup_recovery
from .send_allowlist import is_path_sendable
from .workspace_lock import acquire as _ws_acquire, heartbeat as _ws_heartbeat, release as _ws_release

TASKS_DIR = _task_dir()
# Written by THIS bridge on replay, not by an agent — the guard must not
# mistake its own dedup control for collaborator output.
GATEWAY_REDELIVERY_RESULT = "[no-send] gateway redelivery of already-handled task\n"


RESULTS_DIR = _result_dir()
_STATE = _state_dir()
_WITHHELD_TASK_OUTPUT: "dict[str, tuple]" = {}
_WITHHELD_DM_CACHE = _STATE / "withheld-review-dm.json"
_WITHHELD_CONTROL_DIR = _STATE / "withheld-review-control-results"
_GATEWAY_OWNER_DM_HINT = ""
ARCHIVE_RESULTS_DIR = RESULTS_DIR / "archive"
# Transient-failure count per polled `.txt` name; _resolve_send_failure bounds
# retries at MAX_TRANSIENT_ATTEMPTS then parks. In-memory: resets on restart.
_PROACTIVE_ATTEMPTS: "dict[str, int]" = {}
# tids THIS process redelivered. Not a file: the collaborator path has full
# workspace write, so any sidecar it can create is provenance it can forge.
_REDELIVERED: "set[str]" = set()
try:  # pragma: no cover - exercised by whichever context imports it
    from .send_failure_policy import UnconfirmedDelivery as _UnconfirmedDelivery
except ImportError:  # pragma: no cover - flat src/ import path
    from send_failure_policy import UnconfirmedDelivery as _UnconfirmedDelivery

# Terminal resting place for proactive nudges that can never be delivered.
# results/undelivered/ is the repo-wide quarantine convention — health-check's
UNDELIVERABLE_RESULTS_DIR = RESULTS_DIR / "undelivered"
# Named-instance support (multi-gateway): one core may run SEVERAL bridge
# processes, each pointed at a different gateway (e.g. prod + dev homeservers)
_INSTANCE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")

GATEWAY_INSTANCE = (os.environ.get("GATEWAY_INSTANCE") or "").strip()
if GATEWAY_INSTANCE and not _INSTANCE_RE.fullmatch(GATEWAY_INSTANCE):
    sys.exit("FATAL: GATEWAY_INSTANCE must match "
             f"{_INSTANCE_RE.pattern} (ASCII only; got {GATEWAY_INSTANCE!r})")
_INST_SUFFIX = f".{GATEWAY_INSTANCE}" if GATEWAY_INSTANCE else ""


def _local_tid(broker_tid: str) -> str:
    """Instance-namespaced LOCAL task id (P1 fixes, PR review 2026-08-02 ×2).

    Broker ids are only unique WITHIN a gateway, so named instances must not
    share the primary's local id space. The encoding is `task-<inst>~<broker_id>`
    (broker id embedded VERBATIM after `~`):
    - INJECTIVE across every (instance, broker_id) pair including the
      unsuffixed primary: `~` is outside the broker id alphabet
      (`[A-Za-z0-9._-]`, `_TID_RE`) and outside the GATEWAY_INSTANCE charset
      (import guard), so no primary pass-through id can equal a named-instance
      encoding, and the first `~` splits unambiguously. The earlier
      `task-<inst>.<rest>` scheme was NOT injective — a primary broker id
      `task-dev.X` collided with dev's mapping of `task-X` (review P1 #2).
    - Still matches every consumer's `task-*` glob (watcher, archiver).
    - May exceed _TID_RE's 64-char wire bound (review P1 #1) — local
      consumers therefore gate on `_valid_local_tid`, and the wire id is
      re-derived and `_valid_tid`-checked at the ack/result egress points.
    Unset instance → identity (legacy single-gateway installs unchanged)."""
    if not GATEWAY_INSTANCE:
        return broker_tid
    return f"task-{GATEWAY_INSTANCE}~{broker_tid}"


def _broker_tid(local_tid: str) -> str:
    """Reverse of `_local_tid` — the id the GATEWAY knows this task by."""
    if not GATEWAY_INSTANCE:
        return local_tid
    prefix = f"task-{GATEWAY_INSTANCE}~"
    if local_tid.startswith(prefix):
        return local_tid[len(prefix):]
    return local_tid


_LOCAL_TID_RE = re.compile(rf"task-{_INSTANCE_RE.pattern}~([A-Za-z0-9._-]{{1,64}})")


def _valid_local_tid(tid: str) -> bool:
    """A safe LOCAL id: either a plain wire-valid id (primary/legacy) or a
    named-instance encoding whose embedded broker id is itself wire-valid.
    Centralizes the widened bound so every local consumer accepts what
    `_local_tid` can produce (max ≈ 5+32+1+64 chars — filename-safe)."""
    if _valid_tid(tid):
        return True
    m = _LOCAL_TID_RE.fullmatch(tid)
    return bool(m) and m.group(1) not in (".", "..")


def _task_pending(tid: str) -> bool:
    """Is this task still live in tasks/, under ANY of its names?

    A pooled task is renamed twice -- unassigned -> `.assigned-<core>` (lead
    picked a core) -> `.claimed-<core>` (core took it). Every caller asking
    "is this still being worked?" must accept all three, so the question has
    one owner: a state missed here reads as finished, which drops a reply
    mid-flight or re-queues work another core already holds."""
    return ((TASKS_DIR / f"{tid}.txt").exists()
            or any(TASKS_DIR.glob(f"{tid}.assigned-*"))
            or any(TASKS_DIR.glob(f"{tid}.claimed-*")))

# Persist the in-flight set (tasks pulled from the gateway, awaiting result-POST)
# so a client restart between pull and POST doesn't strand the result. Scoped to
INFLIGHT_FILE = _STATE / f"remote-task-inflight{_INST_SUFFIX}.json"
# Sidecar map {task id → origin room id}, recorded at queue time. Outbound
# file-attach needs the room because media uploads go to the room-scoped
TASK_ROOMS_FILE = _STATE / f"remote-task-rooms{_INST_SUFFIX}.json"
# Re-asked task id -> the id the broker is waiting on. A dedup re-ask gets a
# fresh local id, but the delivery it answers is still the original one.
DEDUP_ALIAS_FILE = _STATE / f"remote-dedup-alias{_INST_SUFFIX}.json"
# Liveness of the gateway *connection* itself (distinct from _post_heartbeat,
# which pings the broker). A local supervisor (e.g. the desktop app's
GATEWAY_STATUS_FILE = _STATE / f"gateway-status{_INST_SUFFIX}.json"

# Launch provenance + in-bridge file log. A supervisor that persists stdout
# (sutando's startup.sh redirects it to logs/remote-gateway-bridge.log) exports
_LAUNCHED_VIA = "supervised" if os.environ.get("SUTANDO_SUPERVISED") else "bare"
_LOG_DIR = _STATE.parent / "logs"
_LOG_FILE = _LOG_DIR / "gateway-bridge.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024

# AWP P0: the persistent event channel (if enabled) — a module-level handle so
# gateway-status can report per-channel health. None until _maybe_start_event_channel.
_EVENT_CHANNEL = None

# Back-compat: instances onboarded before the AG2_REMOTE_* → REMOTE_TASK_*
# rename still export the legacy names in their .env. Honor them as DEPRECATED
_warned_legacy = set()


def _unquote_env(v):
    """Whitespace and surrounding quotes off a credential value.

    The ONE definition every reader uses, so the env tier cannot disagree with
    the file tiers about a quoted `.env` line.
    """
    return v.strip().strip("'\"") if v else v


def _env_compat(new, old):
    v = os.environ.get(new)
    if v:
        return _unquote_env(v)
    v = os.environ.get(old)
    if v and old not in _warned_legacy:
        _warned_legacy.add(old)
        print(f"[remote-gateway-bridge] {old} is deprecated — rename to {new} in your .env",
              file=sys.stderr, flush=True)
    return _unquote_env(v)

# One-token onboarding: REMOTE_TASK_TOKEN alone is enough. The onboarding
# string may be the combined "https://<gateway>|<secret>" form (the URL travels
_ENCODED_SEPARATOR_RE = re.compile(r"%7[Cc]")


def _parse_onboarding_token(raw):
    """Split the onboarding string into (url_from_token, secret).

    NEVER mutates the token bytes — it only *splits* at the separator, so the
    secret is returned verbatim (a bearer that itself contains "%7C" or "|" is
    preserved intact; #2307 review). Disambiguation: only the combined form —
    which begins with an http(s):// scheme — carries a separator to split on; a
    bare secret is opaque and returned untouched even if it contains "%7C".

    Handled at the single parse point, so every caller (startup.sh, direct env,
    legacy AG2_REMOTE_TOKEN alias) is covered regardless of the onboarding writer.
    """
    if not raw.lower().startswith(("http://", "https://")):
        return "", raw  # bare secret — opaque, never touched
    i = raw.find("|")
    if i != -1:
        return raw[:i], raw[i + 1:]  # literal pipe wins; URL + secret verbatim
    m = _ENCODED_SEPARATOR_RE.search(raw)
    if m is None:
        return "", raw  # scheme but no separator; the URL-less guard in main() speaks
    return raw[:m.start()], raw[m.end():]  # URL + secret, both verbatim


# Which channels/<dir>/ this instance reads (.env fallback + access.json).
# Env-only — the .env file can't name its own directory. Default preserves the
CHANNEL_DIR = os.environ.get("REMOTE_TASK_CHANNEL_DIR") or "ag2space"


def _channel_env_candidates():
    """Readable channel-.env candidates in precedence order, as [(path, vals)].
    A candidate lacking a key must not shadow a later one that carries it."""
    candidates = [os.environ.get("AG2_DEVICE_ENV")]
    _cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if _cfg:
        candidates.append(os.path.join(_cfg, "channels", CHANNEL_DIR, ".env"))
    out = []
    for path in candidates:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        # Every candidate is read eagerly now, so one the old early-return never
        # opened can be undecodable — and that is not an OSError.
        except (OSError, UnicodeDecodeError):
            continue
        vals = {}
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            key, _, val = ln.partition("=")
            vals[key.strip()] = _unquote_env(val)
        out.append((path, vals))
    return out


def _config_from_channel_env(key: str) -> str:
    """First candidate CARRYING `key`, else "". Presence decides, not truth: an
    explicit blank is a decision here exactly as it is in the environment."""
    for _path, vals in _channel_env_candidates():
        if key in vals:
            return vals[key]
    return ""


def _token_from_ag2space_env():
    """Fallback token source when the launcher didn't export it into the env.

    `connect` writes the relay token to the channel .env, but not every launcher
    gets it into the process environment. The desktop-spawned core is the case
    that matters: its supervisor spawns the core (and the gateway window) with a
    fixed env whitelist, and the window sources the .env only once at start — so
    if connect writes the token after that (or the export step is skipped), the
    bridge sees an empty token and never connects (every new desktop-only user
    can reproduce this). Read the file directly so the bridge connects regardless
    of who launched it, and so a bridge already looping when connect wrote the
    token picks it up on its next start.

    Returns (token, url). A combined url|secret token embeds the URL (split
    downstream by _parse_onboarding_token), but a split-layout file (bare token +
    separate REMOTE_TASK_URL) does not — so the file's REMOTE_TASK_URL is returned
    alongside for the caller to feed into the URL chain. Returns ("", "") when no
    candidate file holds a token.

    Candidates, in order:
      1. AG2_DEVICE_ENV — the absolute path the desktop launcher (launch-sutando.sh)
         lays into the gateway window, pointing straight at the file connect wrote;
         the ONLY one that reaches the bridge in the desktop-spawned case.
      2. $CLAUDE_CONFIG_DIR/channels/ag2space/.env — for non-desktop launchers that
         do export CLAUDE_CONFIG_DIR into the bridge's environment.
    We deliberately do NOT guess ~/.claude: a bare-home guess is the one path that
    could silently pick up a token from an UNRELATED/old install and connect as the
    WRONG identity (reinstall, account switch, leftover config). Both real launchers
    are covered above; the bare-home guess only adds a footgun.
    """
    for path, vals in _channel_env_candidates():
        # Truthiness here, presence in _config_from_channel_env: a blank secret is
        # absence and must fall through to the legacy alias; a blank room is a choice.
        tok = vals.get("REMOTE_TASK_TOKEN") or vals.get("AG2_REMOTE_TOKEN")
        if tok:
            # Name the exact file — which .env supplied the token is load-bearing
            # for diagnosis (and for spotting a wrong-file bind).
            print(f"[remote-gateway-bridge] token not in env; loaded from {path}",
                  file=sys.stderr, flush=True)
            # Carry the file's REMOTE_TASK_URL too. A combined url|secret token
            # embeds the URL (parsed downstream), but a SPLIT layout (bare token +
            url = vals.get("REMOTE_TASK_URL") or vals.get("AG2_REMOTE_URL") or ""
            # Carry REMOTE_MEDIA_MARKER from the same file too. The bridge derives
            # its marker tag from os.environ at import (MEDIA_MARKER_TAG below), and
            _mm = vals.get("REMOTE_MEDIA_MARKER")
            if _mm and not os.environ.get("REMOTE_MEDIA_MARKER"):
                os.environ["REMOTE_MEDIA_MARKER"] = _mm
            # Return the source file path too (main #2323): it is the durable token
            # source the auth-recovery path re-reads on rejection. In the desktop-
            return tok, url, path
    return "", "", ""


def _token_from_vault_ag2space(vault_get=None):
    """Vault tier for the ag2space onboarding token — parity with the channel
    bridges (#2638).

    Before this, sparrow resolved its token from the process env and the channel
    `.env`, but NEVER the Keychain vault (`get_vault_key` occurrences in this
    module: 0). So `vault set REMOTE_TASK_TOKEN <value>` stored the secret
    correctly and changed nothing for ag2space — the operator spent the secret
    and saw no effect, exactly the failure #2638 fixed for discord/slack/telegram
    (@qingyun-air's 2026-08-04 bridge-parity finding). This closes that gap.

    Reuses the shared core policy `channel_token.token_from_vault` rather than
    copying it (the read is total-failure-safe and never surfaces the value).
    sparrow ships standalone (`pyproject.toml`), so the monorepo `src/` may be
    absent; when `channel_token` can't be located/imported we degrade to the
    pre-#2638 behavior — no vault tier — rather than crash a bridge at startup.
    Tries the current name, then the legacy `AG2_REMOTE_TOKEN` alias. Returns ''
    on any failure. `vault_get` is injectable so the tier is testable hermetically
    without touching a real Keychain.
    """
    try:
        cur = os.path.dirname(os.path.abspath(__file__))
        src = ""
        while True:
            if os.path.isfile(os.path.join(cur, "src", "channel_token.py")):
                src = os.path.join(cur, "src")
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        if not src:
            return ""
        if src not in sys.path:
            sys.path.insert(0, src)
        from channel_token import token_from_vault
    except Exception:
        return ""
    tok = (token_from_vault("REMOTE_TASK_TOKEN", vault_get=vault_get)
           or token_from_vault("AG2_REMOTE_TOKEN", vault_get=vault_get))
    if tok:
        # Name the source — which layer supplied the token is load-bearing for
        # diagnosis. Never print the value.
        print("[remote-gateway-bridge] token not in env or .env; loaded from vault",
              file=sys.stderr, flush=True)
    return tok


def _team_guard_fns():
    """Load the BUNDLED guard; an installed wheel has no monorepo src/."""
    from .team_result_guard import (
        classify_result_for_tier,
        is_guarded_tier,
        is_suppression_only,
        journal_suppressed_result,
        materialize_withheld_verdict,
        resolve_access_tier,
        sensitive_data_filter_enabled,
        withheld_review_path,
    )
    return (classify_result_for_tier, materialize_withheld_verdict,
            resolve_access_tier, sensitive_data_filter_enabled,
            withheld_review_path, journal_suppressed_result,
            is_suppression_only, is_guarded_tier)


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_private_json(path: Path) -> "dict | None":
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _gateway_owner() -> str:
    global _GATEWAY_OWNER_DM_HINT
    identity = _reenroll_identity()
    answer = _req("GET", "/v1/agents")
    agents = answer.get("agents") if isinstance(answer, dict) else None
    if not isinstance(agents, list):
        return ""
    row = next((row for row in agents
                if isinstance(row, dict) and row.get("id") == identity), None)
    owner = str((row or {}).get("owner") or "")
    hint = str((row or {}).get("owner_dm_room") or "")
    _GATEWAY_OWNER_DM_HINT = hint if hint.startswith("!") else ""
    return owner if owner.startswith("@") and ":" in owner else ""


def _owner_review_dm(owner: str) -> str:
    if _GATEWAY_OWNER_DM_HINT:
        _atomic_private_json(_WITHHELD_DM_CACHE,
                             {"owner": owner, "room_id": _GATEWAY_OWNER_DM_HINT})
        return _GATEWAY_OWNER_DM_HINT
    cached = _read_private_json(_WITHHELD_DM_CACHE) or {}
    room = str(cached.get("room_id") or "")
    if cached.get("owner") == owner and room.startswith("!"):
        return room
    answer = _req("POST", "/v1/room", {
        "op": "create", "invite": [owner], "is_direct": True,
        "name": "Private result reviews",
        "topic": "Owner-only review of results withheld from shared rooms.",
    }, timeout=20)
    room = str((answer or {}).get("room_id") or "")
    if not room.startswith("!"):
        raise RuntimeError("gateway did not return a private review room")
    _atomic_private_json(_WITHHELD_DM_CACHE, {"owner": owner, "room_id": room})
    return room


def _review_messages(record: dict) -> list[str]:
    rid = str(record.get("review_id") or "")
    context = record.get("context") or {}
    origin = _one_line(context.get("channel_id") or "").strip()
    room_name = _one_line(context.get("room_name") or "").strip()
    # Keep room-controlled values in code spans and neutralize backticks so
    # room metadata cannot alter the review message's Markdown structure.
    origin_label = origin.replace("`", "'")
    room_label = room_name.replace("`", "'")
    room_info = (
        f"`{origin_label}` (name: `{room_label}`)" if room_label else f"`{origin_label}`"
    )
    body = str(record.get("withheld_body") or "")
    header = (
        f"**Private result review `{rid}`**\n\n"
        "This result was withheld from the shared room because it may contain "
        "sensitive information or delivery-control markers.\n\n"
        f"Original room: {room_info}\n\n")
    decision = (
        "Reply directly to this message with **Yes** to confirm it should stay "
        "private, or **No** to mark it as a false positive and publish it to the "
        f"original room. You can also reply `Yes {rid}` or `No {rid}`."
    )
    # Keep each /v1/room event below the homeserver ceiling. The last event is
    # the decision prompt and bare Yes/No reply anchor.
    chunk_chars = 12000
    chunks = [body[i:i + chunk_chars] for i in range(0, len(body), chunk_chars)] or [""]
    if len(chunks) == 1:
        # The buttons renderer replaces its fallback body; keep the candidate
        # in a preceding event so it remains visible for review.
        return [header + "---\n" + chunks[0] + "\n---", decision]
    messages = [header + f"Candidate result follows in {len(chunks)} parts."]
    messages.extend(
        f"**`{rid}` — part {index}/{len(chunks)}**\n\n{chunk}"
        for index, chunk in enumerate(chunks, 1))
    messages.append(f"**`{rid}` — review decision**\n\n{decision}")
    return messages


def _review_buttons(review_id: str) -> dict:
    """A2UI button macros for the existing owner-decision grammar."""
    return {
        "version": "0.9",
        "type": "buttons",
        "prompt": "Does this result contain sensitive information?",
        "options": [
            {"label": "Yes — keep private", "action": f"Yes {review_id}"},
            {"label": "No — publish to room", "action": f"No {review_id}"},
        ],
    }


def _route_withheld_review(path: Path) -> bool:
    record = _read_private_json(path)
    if not record:
        return False
    if record.get("status") != "pending_dm":
        return True
    owner = _gateway_owner()
    if not owner:
        raise RuntimeError("gateway returned no registered owner")
    room = _owner_review_dm(owner)
    answer = None
    messages = _review_messages(record)
    for index, message in enumerate(messages, 1):
        payload = {
            "op": "message", "room_id": room, "body": message,
            "mentions": [owner] if index == len(messages) else [],
            "dedupe_key": f"withheld-review:{record['review_id']}:{index}",
        }
        if index == len(messages):
            # Same additive content mechanism as the existing room-invite card.
            # Non-AG2 clients ignore it and retain the typed Yes/No fallback.
            payload["extra_content"] = {
                "space.ag2.a2ui": _review_buttons(str(record["review_id"]))}
        answer = _req("POST", "/v1/room", payload, timeout=20)
        if not isinstance(answer, dict) or not answer.get("ok"):
            raise RuntimeError(f"private review DM part {index}/{len(messages)} was not accepted")
    record.update({"status": "awaiting_owner", "owner": owner, "dm_room_id": room,
                   "dm_event_id": str(answer.get("event_id") or ""),
                   "dm_sent_at": time.time()})
    _atomic_private_json(path, record)
    return True


_REVIEW_DECISION_RE = re.compile(
    r"^(yes|no)(?:\s+(wr_[0-9a-f]{16}))?[.!]?$", re.IGNORECASE)


def _pending_review_records() -> list[tuple[Path, dict]]:
    out = []
    directory = _STATE / "withheld-team-results"
    try:
        paths = sorted(directory.glob("wr_*.json"))
    except OSError:
        return out
    for path in paths:
        record = _read_private_json(path)
        if record and record.get("status") in (
                "awaiting_owner", "publish_pending", "kept_private", "published",
                "publish_failed"):
            out.append((path, record))
    return out


def _archive_resolved_review(path: Path, record: dict) -> bool:
    if record.get("status") not in ("kept_private", "published", "publish_failed"):
        return False
    if record.get("card_resolution_pending"):
        return False
    archive = path.parent / "archive"
    try:
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(archive, 0o700)
        path.replace(archive / path.name)
    except OSError:
        return False
    return True


def _decision_text(task: dict) -> str:
    text = str(task.get("task") or "").strip()
    # Button replies can follow the broker's quoted context envelope; only text
    # after its exact closing marker is the decision.
    text = re.sub(
        r"^\[AG2 Space reply context;[^\]]*\].*?"
        r"\[End AG2 Space reply context\]\s*",
        "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"^\[AG2Space\s+[^\]]+\]\s*", "", text, count=1)
    return text.strip()


def _match_review_decision(task: dict) -> "tuple[Path, dict, str] | None":
    # Re-resolve the owner tier, require the private room, and bind bare Yes/No
    # to its review message. An explicit review id is still room-bound.
    tier = _tier_for(task.get("user_id"), _normalized_tier(task.get("access_tier")))
    if tier != "owner":
        return None
    match = _REVIEW_DECISION_RE.fullmatch(_decision_text(task))
    if not match:
        return None
    answer, explicit_id = match.group(1).lower(), match.group(2)
    room = str(task.get("channel_id") or "")
    reply_to = str(task.get("reply_to_event") or "")
    candidates = []
    for path, record in _pending_review_records():
        if room != str(record.get("dm_room_id") or ""):
            continue
        if str(task.get("user_id") or "") != str(record.get("owner") or ""):
            continue
        if explicit_id:
            if explicit_id == record.get("review_id"):
                candidates.append((path, record))
        elif reply_to and reply_to == str(record.get("dm_event_id") or ""):
            candidates.append((path, record))
    return (*candidates[0], answer) if len(candidates) == 1 else None


def _publish_review(path: Path, record: dict) -> bool:
    context = record.get("context") or {}
    room = str(context.get("channel_id") or "")
    body = str(record.get("withheld_body") or "")
    if not room.startswith("!") or not body:
        record.update({"status": "publish_failed", "publish_error": "invalid origin/body",
                       "card_resolution_pending": True})
        _atomic_private_json(path, record)
        return False
    answer = _req("POST", "/v1/room", {
        "op": "message", "room_id": room, "body": body,
        "dedupe_key": f"withheld-publish:{record['review_id']}",
    }, timeout=20)
    if not isinstance(answer, dict) or not answer.get("ok"):
        return False
    record.update({"status": "published", "published_at": time.time(),
                   "published_event_id": str(answer.get("event_id") or ""),
                   "card_resolution_pending": True})
    _atomic_private_json(path, record)
    return True


def _resolved_review_body(record: dict) -> str:
    rid = str(record.get("review_id") or "")
    decision = str(record.get("decision") or "")
    status = str(record.get("status") or "")
    if decision == "sensitive":
        outcome = "Kept private — the owner confirmed it contains sensitive information."
    elif status == "published":
        outcome = "Published to the original room — the owner marked it as a false positive."
    elif status == "publish_failed":
        outcome = "False positive recorded, but publication failed and requires attention."
    else:
        outcome = "False positive recorded; publication to the original room is pending."
    return f"**Private result review `{rid}` resolved**\n\n✓ {outcome}"


def _resolve_review_card(path: Path, record: dict) -> bool:
    room = str(record.get("dm_room_id") or "")
    event_id = str(record.get("dm_event_id") or "")
    if not room.startswith("!") or not event_id.startswith("$"):
        return False
    answer = _req("POST", "/v1/room", {
        "op": "edit", "room_id": room, "event_id": event_id,
        "body": _resolved_review_body(record),
    }, timeout=20)
    if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
        return False
    record.update({"card_resolution_pending": False,
                   "card_resolved_at": time.time(),
                   "card_resolution_event_id": str(answer.get("event_id") or "")})
    _atomic_private_json(path, record)
    _archive_resolved_review(path, record)
    return True


def _handle_review_decision(task: dict) -> bool:
    task_id = str(task.get("id") or "")
    if task_id and _control_result_path(task_id).is_file():
        return True
    matched = _match_review_decision(task)
    if matched is None:
        return False
    path, record, answer = matched
    if record.get("status") in ("kept_private", "published"):
        _queue_review_control_result(task)
        if record.get("card_resolution_pending"):
            _resolve_review_card(path, record)
        return True  # delivery retry of the same owner decision
    if answer == "yes":
        record.update({"status": "kept_private", "resolved_at": time.time(),
                       "decision": "sensitive", "card_resolution_pending": True})
        _atomic_private_json(path, record)
        _queue_review_control_result(task)
        try:
            _resolve_review_card(path, record)
        except Exception as exc:  # noqa: BLE001 — durable pending state retries
            _log(f"withheld review {record.get('review_id')} card edit deferred: {exc}")
        return True
    # Persist release before the network call; pending retries use a stable
    # dedupe key so they cannot duplicate the disclosure.
    record.update({"status": "publish_pending", "resolved_at": time.time(),
                   "decision": "false_positive", "card_resolution_pending": True})
    _atomic_private_json(path, record)
    _queue_review_control_result(task)
    try:
        _resolve_review_card(path, record)
        if _publish_review(path, record):
            _resolve_review_card(path, record)
    except Exception as exc:  # noqa: BLE001 — durable pending state retries
        _log(f"withheld review {record.get('review_id')} publish deferred: {exc}")
    return True


def _retry_pending_publications() -> None:
    for path, record in _pending_review_records():
        if record.get("status") != "publish_pending":
            continue
        try:
            if not _publish_review(path, record):
                _log(f"withheld review {record.get('review_id')} publish still pending")
        except Exception as exc:  # noqa: BLE001 — next poll retries
            _log(f"withheld review {record.get('review_id')} publish retry failed: {exc}")


def _retry_review_card_resolutions() -> None:
    for path, record in _pending_review_records():
        if not record.get("card_resolution_pending"):
            _archive_resolved_review(path, record)
            continue
        try:
            if not _resolve_review_card(path, record):
                _log(f"withheld review {record.get('review_id')} card edit still pending")
        except Exception as exc:  # noqa: BLE001 — next poll retries
            _log(f"withheld review {record.get('review_id')} card edit retry failed: {exc}")


def _control_result_path(task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)[:160]
    return _WITHHELD_CONTROL_DIR / f"{safe}.json"


def _queue_review_control_result(task: dict) -> None:
    task_id = str(task.get("id") or "")
    if not task_id:
        return
    path = _control_result_path(task_id)
    if not path.is_file():
        _atomic_private_json(path, {"id": task_id, "body": "[no-send]"})


def _retry_review_control_results() -> None:
    try:
        paths = sorted(_WITHHELD_CONTROL_DIR.glob("*.json"))[:512]
    except OSError:
        return
    for path in paths:
        record = _read_private_json(path)
        if not record or not record.get("id"):
            continue
        try:
            _req("POST", "/v1/results", {
                "id": record["id"], "body": record.get("body") or "[no-send]"})
            path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — next poll retries
            _log(f"review control result {record.get('id')} retry deferred: {exc}")


def _is_redelivery_control(body: str) -> bool:
    """Compare on stripped text: `read_ready_result` strips, the constant ends
    in a newline, so a raw `==` never matches a body that came off disk."""
    return (body or "").strip() == GATEWAY_REDELIVERY_RESULT.strip()


def _guarded_result_body(tid: str, body: str):
    """Scan a non-owner result BEFORE any marker is interpreted.

    Returns (safe_body, withheld_reason), or (None, reason) when the guard
    cannot be loaded — the caller leaves the file for retry rather than
    honouring redirect/attach actions on unscanned collaborator output.
    """
    # Body equality is NOT provenance; only this process's record is. Reading must
    # not consume it — a deferred POST retries and needs the same provenance.
    if tid in _REDELIVERED and _is_redelivery_control(body):
        return body, None
    if tid in _WITHHELD_TASK_OUTPUT:
        return _WITHHELD_TASK_OUTPUT[tid]
    try:
        (classify, materialize, resolve, filter_enabled, review_path,
         journal_suppression, suppression_only, guarded_tier) = _team_guard_fns()
        from .chat_secret_filter import filter_chat_secrets
    except Exception as exc:
        return None, f"team_result_guard unavailable: {exc}"
    tfile = find_task_file(TASKS_DIR, tid) or find_archived_task(TASKS_DIR, tid)
    # Absence is not owner provenance: a month-archived Team task is exactly the
    # case that would otherwise fall open.
    tier = resolve(tfile) if tfile is not None else "guest"
    scan_sensitive_data = filter_enabled(tfile, tier) if tfile is not None else True
    context = {}
    if tfile is not None:
        try:
            headers = local_task_protocol.parse_task_headers_trusted(
                tfile.read_text(encoding="utf-8", errors="replace")).headers
            context = {key: headers.get(key, "") for key in (
                "source", "channel_id", "room_name", "reply_to_event",
                "source_message_id", "user_id")}
        except OSError:
            pass
    verdict = classify(
        body, tier, None, secret_filter=filter_chat_secrets,
        scan_sensitive_data=scan_sensitive_data)
    is_leak = verdict.kind == "leak"
    agent_id = _reenroll_identity()
    verdict = materialize(
        verdict, body, _STATE, tid, context=context, agent_id=agent_id,
        now=time.time())
    if guarded_tier(tier) and suppression_only(body):
        # Honouring a guarded close without recording it is the accountability
        # gap the guard used to answer with a refusal.
        verdict = journal_suppression(
            verdict, body, _STATE, tid, context=context, agent_id=agent_id,
            now=time.time())
    if is_leak:
        artifact = review_path(_STATE, tid)
        if not artifact.is_file():
            return None, verdict.reason
        try:
            if not _route_withheld_review(artifact):
                return None, f"{verdict.reason}; private owner review unavailable"
        except Exception as exc:  # noqa: BLE001 — retain result file for retry
            return None, f"{verdict.reason}; private owner review failed: {exc}"
    result = (verdict.body, verdict.reason)
    if verdict.reason is not None:
        _WITHHELD_TASK_OUTPUT[tid] = result
        if len(_WITHHELD_TASK_OUTPUT) > 512:
            _WITHHELD_TASK_OUTPUT.pop(next(iter(_WITHHELD_TASK_OUTPUT)))
    return result


_VAULT_INTERCEPT_FNS: "tuple | None" = None


def _vault_intercept_fns():
    """Lazily locate the monorepo `src/vault_intercept.py` helpers; memoized.
    Returns (None, None) on failure so a caller can fall back to `_local_redact_vault_set`."""
    global _VAULT_INTERCEPT_FNS
    if _VAULT_INTERCEPT_FNS is not None:
        return _VAULT_INTERCEPT_FNS
    try:
        cur = os.path.dirname(os.path.abspath(__file__))
        src = ""
        while True:
            if os.path.isfile(os.path.join(cur, "src", "vault_intercept.py")):
                src = os.path.join(cur, "src")
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        if not src:
            _VAULT_INTERCEPT_FNS = (None, None)
            return _VAULT_INTERCEPT_FNS
        if src not in sys.path:
            sys.path.insert(0, src)
        from vault_intercept import intercept_vault_commands, redact_vault_commands
        _VAULT_INTERCEPT_FNS = (intercept_vault_commands, redact_vault_commands)
    except Exception:
        _VAULT_INTERCEPT_FNS = (None, None)
    return _VAULT_INTERCEPT_FNS


def _local_redact_vault_set(text: str) -> str:
    """Last-resort redaction when no monorepo `vault_intercept.py` is found.
    Delegates to this package's own vendored `vault_set_grammar`, not a hand-copied regex."""
    from .vault_set_grammar import redact_vault_commands as _grammar_redact
    return _grammar_redact(text, placeholder="[VAULT-SET-REDACTED: interceptor unavailable]")


_RAW = _env_compat("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN") or ""
_URL_FALLBACK = ""
_TOKEN_FILE_FALLBACK = ""
if not _RAW:
    _RAW, _URL_FALLBACK, _TOKEN_FILE_FALLBACK = _token_from_ag2space_env()
if not _RAW:
    # Last resort: the Keychain vault — parity with the channel bridges (#2638).
    # Without this, `vault set REMOTE_TASK_TOKEN` was a no-op for ag2space.
    _RAW = _token_from_vault_ag2space()
_URL_FROM_TOKEN, TOKEN = _parse_onboarding_token(_RAW)
URL = (_env_compat("REMOTE_TASK_URL", "AG2_REMOTE_URL")
       or _URL_FROM_TOKEN or _URL_FALLBACK).rstrip("/")
PROVIDER = os.environ.get("REMOTE_TASK_PROVIDER") or "remote"
POLL_WAIT = int(os.environ.get("REMOTE_TASK_POLL_WAIT") or "25")
# A read timeout on the long poll is indistinguishable from the documented
# `200 {"tasks": []}` hold-window expiry, so it is only an outage once no poll
POLL_TIMEOUT_GRACE_S = 3 * (POLL_WAIT + 10)
# Proactive-message drain: when REMOTE_PROACTIVE_ROOM names a room id, every
# `results/proactive-*.txt` the agent writes is delivered to that room as a
_PROACTIVE_ROOM_ENV = os.environ.get("REMOTE_PROACTIVE_ROOM")
PROACTIVE_ROOM = (
    _PROACTIVE_ROOM_ENV
    if _PROACTIVE_ROOM_ENV is not None
    else _config_from_channel_env("REMOTE_PROACTIVE_ROOM")
)
# Host-injected claim gate (Path -> bool), consulted per file before the claim
# rename; None (standalone default) claims every routable file unchanged.
PROACTIVE_CLAIM_GATE: Callable[[Path], bool] | None = None

# Runtime self-report (#3279 verification layer 3): a host loader may inject
# {build_sha, entrypoint} BEFORE exec'ing this source; standalone stays empty.
RUNTIME_IDENTITY: dict = globals().get("RUNTIME_IDENTITY") or {}
_ENGINE_COUNTS = {"core_confirmed": 0, "legacy_sends": 0}


def _engine_desc() -> str:
    c = _DELIVERY_CORE
    if c is None:
        return "DeliveryCore(unbuilt)"
    return (f"DeliveryCore({type(c.backend).__name__}"
            f"->{type(c.provider).__name__})")
# Opt-in compat for brokers whose /v1/room answers {"ok": true} with no
# event_id: trust the bare ok as delivered (at-least-once beats never).
_PROACTIVE_TRUST_OK_ENV = os.environ.get("REMOTE_PROACTIVE_TRUST_OK")
PROACTIVE_TRUST_OK = (
    _PROACTIVE_TRUST_OK_ENV
    if _PROACTIVE_TRUST_OK_ENV is not None
    else _config_from_channel_env("REMOTE_PROACTIVE_TRUST_OK")
) == "1"
# The ONE auth-header dict shared with long-lived consumers (event channel,
# card poster). They must hold this dict BY REFERENCE (no copy) so a token
_AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
OUTBOUND_SCAN_S = float(os.environ.get("REMOTE_OUTBOUND_SCAN_S") or "1.0")
# Outbound worker: decouples outbound progress from inbound long-poll
# progress; delivery machinery untouched — owns ONLY lifecycle/scheduling.
_OUTBOUND_WAKE = threading.Event()
_OUTBOUND_STOP = threading.Event()
_INFLIGHT_MUTEX = threading.RLock()


def wake_outbound() -> None:
    """Kick the outbound worker (e.g. right after a local result write)."""
    _OUTBOUND_WAKE.set()


def _outbound_worker(inflight: "set[str]") -> None:
    first_seen: "dict[str, float]" = {}
    while not _OUTBOUND_STOP.is_set():
        _OUTBOUND_WAKE.wait(OUTBOUND_SCAN_S)
        _OUTBOUND_WAKE.clear()
        if _OUTBOUND_STOP.is_set():
            break                        # graceful: stop taking new items
        # tuple(): kept distinct from the drain's test anchor — redundant once
        # the anchor is line-bounded, load-bearing until then.
        for tid in tuple(inflight):
            rfile = RESULTS_DIR / f"{tid}.txt"
            if tid not in first_seen and rfile.exists():
                first_seen[tid] = time.monotonic()
        try:
            _post_ready_results(inflight)
        except Exception as e:  # noqa: BLE001 — failure isolation per cycle
            _log(f"outbound worker: results drain error (isolated): {e}")
        try:
            _post_proactive()
        except Exception as e:  # noqa: BLE001
            _log(f"outbound worker: proactive drain error (isolated): {e}")
        for tid in [t for t in first_seen if t not in inflight]:
            ms = (time.monotonic() - first_seen.pop(tid)) * 1000.0
            _log(f"outbound worker: {tid} seen->retired {ms:.0f}ms (monotonic)")


def _start_outbound_worker(inflight: "set[str]") -> threading.Thread:
    t = threading.Thread(target=_outbound_worker, args=(inflight,),
                         name="outbound-worker", daemon=True)
    t.start()
    _log(f"outbound worker started (scan {OUTBOUND_SCAN_S}s + wake-on-kick) — "
         "outbound no longer rides the inbound long-poll")
    return t


OUTBOUND_WATCHER = os.environ.get("REMOTE_OUTBOUND_WATCHER", "auto")  # auto|off


def _start_results_watcher() -> "threading.Thread | None":
    """Darwin kqueue doorbell on RESULTS_DIR: advisory wakeups only, never
    delivery state. Correctness stays in the durable drain; the bounded scan
    guarantees progress whenever this thread is degraded or absent."""
    if OUTBOUND_WATCHER == "off" or not hasattr(select, "kqueue"):
        return None

    VNODE_GONE = (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
                  | select.KQ_NOTE_REVOKE)

    def run():
        backoff = 1.0
        while not _OUTBOUND_STOP.is_set():
            fd, kq = -1, None
            try:
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                # O_EVTONLY is absent on some supported pythons (Xcode 3.9);
                # kqueue accepts an O_RDONLY fd — it just pins the mount.
                fd = os.open(str(RESULTS_DIR),
                             getattr(os, "O_EVTONLY", os.O_RDONLY))
                kq = select.kqueue()
                kq.control([select.kevent(
                    fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                    | VNODE_GONE)], 0, 0)
                backoff = 1.0
                # Full sweep on every (re)registration: files that landed
                # before the watch began are the first sweep's job.
                wake_outbound()
                while not _OUTBOUND_STOP.is_set():
                    events = kq.control(None, 1, 1.0)
                    if not events:
                        continue
                    wake_outbound()
                    if events[0].fflags & VNODE_GONE:
                        # Rate-cap rebuilds like the exception path: a flapping
                        # dir must not spin the register loop.
                        _OUTBOUND_STOP.wait(min(backoff, 30.0))
                        backoff = min(backoff * 2, 30.0)
                        break            # dir vnode gone: rebuild registration
            except Exception as e:  # noqa: BLE001 — degraded, never fatal
                _log(f"results watcher degraded (scan remains the floor): {e}")
                _OUTBOUND_STOP.wait(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    if kq is not None:
                        kq.close()
                finally:
                    if fd >= 0:
                        os.close(fd)

    t = threading.Thread(target=run, name="results-watcher", daemon=True)
    t.start()
    _log("results watcher started (kqueue doorbell — advisory; scan is the floor)")
    return t


HEARTBEAT_INTERVAL = 60
# When the gateway lacks /v1/tasks/<id>/ack it returns 404/405; we back off
# instead of hammering it — but only for this cooldown, then retry. A permanent
ACK_UNSUPPORTED_COOLDOWN = int(os.environ.get("REMOTE_ACK_RETRY_COOLDOWN") or "300")
_ack_disabled_until = 0.0   # 0 = enabled; else epoch until which acks are skipped
# Auth-rejection recovery: when the gateway rejects the bearer (401/403 — the
# key was revoked or expired), the historical behavior is an immediate FATAL
TOKEN_FILE = os.environ.get("REMOTE_TASK_TOKEN_FILE") or _TOKEN_FILE_FALLBACK or ""
AUTH_RECHECK_INTERVAL = int(os.environ.get("REMOTE_AUTH_RECHECK_INTERVAL") or "30")
# Registry-loss self-claim (backend #595): the code is device-visible only;
# binding requires the owner's concierge approval. Disable: REMOTE_REENROLL=0.
# Does NOT gate _auth_probe() — a token simply being accepted again isn't a relink.
REENROLL_ENABLED = str(os.environ.get("REMOTE_REENROLL", "1")).strip().lower() \
    not in ("0", "false", "no", "off")
REENROLL_PROBE_EVERY = max(1, int(os.environ.get("REMOTE_REENROLL_PROBE_EVERY") or "2"))
REENROLL_CLAIM_RETRY_S = int(os.environ.get("REMOTE_REENROLL_CLAIM_RETRY_S") or "600")
_reenroll_state: dict = {"last_attempt_at": None, "code": None, "claimed_at": None}


def _provision_base() -> str:
    """Gateway base -> provision-api base (…/relay* -> …/api)."""
    return URL.split("/relay")[0].rstrip("/") + "/api"


def _reenroll_identity() -> str:
    """Agent mxid: process env, then the channel .env file — the same fallback
    the token uses (desktop launchers don't export either) — then the durable
    per-host identity enrolment wrote to state/auth/ag2space.json."""
    for key in ("AGENT_MXID", "AGENT_ID"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    for key in ("AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID"):
        v = _config_from_channel_env(key).strip()
        if v:
            return v
    # Re-read per call, like the channel-env candidates: an identity that
    # appears mid-episode must take effect without a restart.
    try:
        rec = json.loads((_STATE / "auth" / "ag2space.json").read_text())
        # Non-string values must read as unknown, not be coerced into a
        # garbage identity that _reenroll_claim would POST on the cadence.
        v = rec.get("agent_id")
        return v.strip() if isinstance(v, str) else ""
    except Exception:  # absent, unreadable, or malformed — identity unknown
        return ""


def _reenroll_claim() -> None:
    """Best-effort claim: one live code per episode; failed POSTs retry no
    sooner than REENROLL_CLAIM_RETRY_S; never raises into the caller."""
    if not REENROLL_ENABLED or _reenroll_state["code"]:
        return
    last = _reenroll_state["last_attempt_at"]
    # Monotonic: a wall-clock step backward must not suppress claims (review
    # P2); None = never attempted, so a fresh boot claims immediately.
    if last is not None and time.monotonic() - last < REENROLL_CLAIM_RETRY_S:
        return
    agent_id = _reenroll_identity()
    if not agent_id or not TOKEN:
        # No POST issued -> no cadence stamp. The instruction must match what
        # can actually work: file candidates are re-read every cycle, but the
        if not agent_id:
            pointered = os.environ.get("AG2_DEVICE_ENV") \
                or os.environ.get("CLAUDE_CONFIG_DIR")
            _log("reenroll: agent identity unknown — write "
                 "AGENT_MXID=<agent mxid> into the channel .env; retrying "
                 "(takes effect without restart)" if pointered else
                 "reenroll: agent identity unknown and no channel-env "
                 "pointers (AG2_DEVICE_ENV/CLAUDE_CONFIG_DIR) — set "
                 "AGENT_MXID in the gateway environment and RESTART the "
                 "wrapper/app; holding the connection wait meanwhile")
        else:
            _log("reenroll: no token available — not claiming")
        return
    _reenroll_state["last_attempt_at"] = time.monotonic()
    try:
        req = urllib.request.Request(
            _provision_base() + "/connect/reenroll",
            data=json.dumps({"agent_id": agent_id, "bearer": TOKEN}).encode(),
            # The prod edge 403s urllib's default UA — same contract as _req().
            headers={"Content-Type": "application/json",
                     "User-Agent": "sutando-gateway-client/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read() or b"{}")
        code = str(body.get("approval_code") or "")
        if body.get("pending") and code:
            _reenroll_state["code"] = code
            _reenroll_state["claimed_at"] = int(time.time())
            _log("RELINK PENDING — this agent's server-side registration was "
                 f"lost. RELINK CODE: {code} — the owner approves by DMing the "
                 f"concierge: relink approve {code}")
        else:
            _log(f"reenroll: claim not parked ({str(body)[:200]})")
    except urllib.error.HTTPError as e:
        _log(f"reenroll: claim refused HTTP {e.code} ({_http_error_body(e)[:200]})")
    except Exception as e:  # noqa: BLE001 — recovery must never crash the loop
        _log(f"reenroll: claim failed: {e}")


def _reenroll_clear(recovered: bool = False) -> None:
    """End the episode; recovered=True (probe-success path only) leaves the
    explicit terminal — disappearance alone must never read as success."""
    was_pending = bool(_reenroll_state.get("code"))
    prior_attempt = _reenroll_state.get("last_attempt_at")
    _reenroll_state.update({"last_attempt_at": None, "code": None, "claimed_at": None})
    if not was_pending:
        # No claim was granted this episode — preserve its cadence, or a
        # probe-only resume lets every future episode re-claim immediately.
        _reenroll_state["last_attempt_at"] = prior_attempt
    if recovered and was_pending:
        _reenroll_state["recovered_at"] = int(time.time())
    else:
        _reenroll_state.pop("recovered_at", None)


def _auth_probe() -> bool:
    """True ONLY on a successful authed response — an error proves nothing
    about auth, so every failure keeps waiting."""
    try:
        _req("GET", "/v1/agents", timeout=15)
        return True
    except Exception:  # noqa: BLE001
        return False
_heartbeat_disabled = False
_last_heartbeat_at = 0.0

_TASK_FIELDS = ("id", "timestamp", "session_scope", "task", "source", "channel_id",
                # Context enrichment (AG2 broker writer side): human room/sender
                # names + reply reference. Serialized only when the gateway sends
                "room_name", "sender_name", "reply_to_event", "reply_to_me", "reply_to_sender",
                "addressed_to",
                # Ingress only: the backend inherits the route by task id, so a
                # reply echoing these back could name a thread it was not asked in.
                "thread_root", "source_room_id",
                # Room-membership context (gateway writer side, same contract):
                # a capped one-line mxid list + the true joined total.
                "room_members", "room_member_count",
                "source_message_id", "user_id", "priority", "interaction_type",
                # Platform-signed metadata pointer — serialized as a one-line
                # JSON header by a dedicated branch below (dict, not scalar).
                "platform_card")

# platform_card passes through with exactly these subkeys — a signed pointer
# {card_url, card_sha256, sig, key_id, alg} to the platform's canonical agent
_PLATFORM_CARD_KEYS = ("card_url", "card_sha256", "sig", "key_id", "alg")

# Interaction-plane vocabulary (interaction-planes refactor step 1). Remote
# values outside this set degrade to "message" rather than passing through.
_INTERACTION_TYPES = frozenset({
    "message", "realtime_audio", "realtime_video",
    "tool_initiated", "system_event", "self_reflective",
})

# Effective access is the lower of the broker-attested tier and the local cap.
# Unset local policy defaults to owner; invalid values fail closed to guest.
LOCAL_TIER = (_env_compat("REMOTE_TASK_TIER", "AG2_REMOTE_TIER") or "owner").strip().lower()
if LOCAL_TIER == "other":
    LOCAL_TIER = "guest"
if LOCAL_TIER not in ("owner", "team", "guest"):
    LOCAL_TIER = "guest"


# A local per-sender map can only cap the broker tier; unlisted senders use LOCAL_TIER.
# Cache identity includes mtime, size, and inode so revocations take effect promptly.
_ACCESS_PATH_LOGGED = None


def _ag2space_access_path():
    """Resolve the map only from the launcher-provided active config.
    The desktop .env pointer wins over the config-root fallback."""
    device_env = os.environ.get("AG2_DEVICE_ENV")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if device_env:
        channel_dir = os.path.dirname(
            os.path.abspath(os.path.expanduser(device_env))
        )
        path = os.path.join(channel_dir, "access.json")
    elif config_dir:
        path = os.path.join(
            os.path.abspath(os.path.expanduser(config_dir)),
            "channels",
            CHANNEL_DIR,
            "access.json",
        )
    else:
        path = ""

    global _ACCESS_PATH_LOGGED
    if path != _ACCESS_PATH_LOGGED:
        detail = path or "disabled (no AG2_DEVICE_ENV or CLAUDE_CONFIG_DIR)"
        print(
            f"[remote-gateway-bridge] access tier map path: {detail}",
            file=sys.stderr,
            flush=True,
        )
        _ACCESS_PATH_LOGGED = path
    return path


# Known tier vocabulary and privilege ordering. `_tier_for` uses this ordering
# to choose the lower of the broker-attested tier and the local cap.
_TIER_RANK = {"guest": 0, "team": 1, "owner": 2}


def _normalized_tier(value):
    tier = str(value or "").strip().lower()
    if tier == "other":
        tier = "guest"
    return tier if tier in _TIER_RANK else "guest"

_TIER_MAP_CACHE = {"path": None, "ident": None, "map": {}}


def _has_above_local(cached) -> bool:
    """True if the cached map grants anyone a tier ABOVE this node's LOCAL_TIER."""
    local_rank = _TIER_RANK.get(LOCAL_TIER, _TIER_RANK["owner"])
    return any(_TIER_RANK.get(v, 0) > local_rank for v in cached.values())


def _stale_safe(cached):
    """Keep stale caps at or below LOCAL_TIER and drop stale privilege grants.
    Transient demotion is safer than retaining a possibly revoked escalation."""
    local_rank = _TIER_RANK.get(LOCAL_TIER, _TIER_RANK["owner"])
    return {k: v for k, v in cached.items() if _TIER_RANK.get(v, 0) <= local_rank}

# Durable on-disk backup of the last-known-good tierMap, under state/auth/ — the
# cleanup-exempt per-host install-state dir (per CLAUDE.md; never wiped by
_TIER_MAP_BACKUP_FILE = _STATE / "auth" / "ag2space-tiermap-backup.json"


def _validate_tier_map(raw):
    """Coerce a raw {who: tier} dict to a validated {mxid: tier} map (drops any
    entry whose value isn't a recognised tier or whose key isn't a str).

    The accepted set MUST match the live reader's; a tier this drops is a
    down-tier silently lost on restore."""
    tm = {}
    if isinstance(raw, dict):
        for who, tier in raw.items():
            t = str(tier).strip().lower()
            if isinstance(who, str) and t in ("owner", "team", "guest", "other"):
                tm[who.strip()] = _normalized_tier(t)
    return tm


def _backup_tier_map_to_disk(tm):
    """Persist the tierMap to the durable backup after a SUCCESSFUL parse of
    access.json. Validates STRUCTURE, not emptiness: a well-formed EMPTY map is a
    legitimate owner state (they removed every down-tier), so it IS persisted and
    a later restart restores *that* — not a stale @rick. The good copy is never
    overwritten by a wipe because a wipe/corrupt access.json never reaches here —
    _load_tier_map() returns on the read error before calling this. Born 0600
    (created already-restricted, never written-at-umask-then-narrowed — the backup
    holds the same authorization data as access.json, so a world-readable copy,
    even for the write window, would be a new exposure introduced by the fix).
    Atomic via per-PID tmp +
    os.replace; best-effort — a backup failure must never break tier resolution
    (mirrors the slack allowlist backup, cd5c5db1 / #2163).

    CHOSEN TRADEOFF (#2354 review): the good copy is preserved by entering the
    caller's safe branch on a PARSE failure, not on "the map looks empty". So a
    zero-byte or truncated access.json (the likely wipe shapes) preserves the
    backup, but a writer that rewrites access.json as VALID JSON with no
    `tierMap` key (or `tierMap: {}`) DOES clear it — byte-identical in effect to
    a deliberate clear. This is unavoidable: "an owner's empty map must persist"
    and "an empty map must never overwrite the backup" are the SAME input; you
    cannot satisfy both by shape alone. A shape-independent fix would require
    PROVENANCE (a generation counter or writer identity), out of scope here. A
    cleared backup after such a write is therefore BY DESIGN, not a bug."""
    if not isinstance(tm, dict):
        return
    try:
        # Create state/auth/ owner-only (0700). It holds only 0600 secrets, so
        # don't rely on the parent's incidental mode: macOS ~/Library/Application
        _TIER_MAP_BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = _TIER_MAP_BACKUP_FILE.with_name(
            f"{_TIER_MAP_BACKUP_FILE.name}.{os.getpid()}.tmp"
        )
        payload = json.dumps(tm, indent=2, sort_keys=True) + "\n"
        # Born 0600, NOT written-at-umask-then-chmod'd. A write_text()+os.chmod()
        # sequence leaves a window where the tmp holds the same authorization data
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(payload)
        os.replace(tmp, _TIER_MAP_BACKUP_FILE)
    except Exception:
        pass


def _restore_tier_map_from_disk():
    """Return the last durable-backed tierMap, or {} if absent/unreadable/invalid."""
    try:
        raw = json.loads(_TIER_MAP_BACKUP_FILE.read_text())
    except Exception:
        return {}
    return _validate_tier_map(raw)


def _last_known_tier_map(path):
    """The floor returned when access.json is unreadable: the in-memory
    last-known-good if the process has one, else the durable on-disk backup (so a
    wipe+restart restores the down-tier floor instead of failing OPEN to {}).

    Two constraints inherited from the live reader, both load-bearing:
    the backup is NOT restored across a config-path switch (it would reintroduce
    the cross-install trust leak the path check exists to prevent), and whatever
    is returned goes through _stale_safe so a stale grant ABOVE LOCAL_TIER is
    dropped rather than resurrected from disk."""
    # A cold process has NO prior path — that is not a switch, and treating it as
    # one refuses the backup on the first request, the exact window this closes.
    if _TIER_MAP_CACHE["path"] is not None and path != _TIER_MAP_CACHE["path"]:
        _TIER_MAP_CACHE["path"], _TIER_MAP_CACHE["ident"], _TIER_MAP_CACHE["map"] = (
            path, None, {})
        return {}
    _TIER_MAP_CACHE["path"] = path
    if _TIER_MAP_CACHE["map"]:
        return _stale_safe(_TIER_MAP_CACHE["map"])
    backup = _restore_tier_map_from_disk()
    if backup:
        _TIER_MAP_CACHE["map"] = backup
    return _stale_safe(_TIER_MAP_CACHE["map"])


def _load_tier_map():
    """Preserve safe caps on same-path faults, but never across path switches.
    An absent launcher config explicitly clears the cache.

    A fault on the SAME path falls back to the durable on-disk backup when the
    process is cold, so a wipe + restart restores the down-tier floor instead of
    failing OPEN to {}; see _last_known_tier_map for the path/stale constraints."""
    path = _ag2space_access_path()
    if not path:
        _TIER_MAP_CACHE["path"], _TIER_MAP_CACHE["ident"], _TIER_MAP_CACHE["map"] = (
            path,
            None,
            {},
        )
        return {}
    try:
        st = os.stat(path)
    except OSError:
        # Keep last-known-good only for the same configured path. Carrying a
        # map across a path switch would leak trust decisions between installs.
        return _last_known_tier_map(path)
    # Size and inode supplement nanosecond mtime so same-timestamp rewrites are detected.
    ident = (st.st_mtime_ns, st.st_size, st.st_ino)
    # Re-read while an above-default grant is cached so revocation cannot be masked.
    if (
        path == _TIER_MAP_CACHE["path"]
        and ident == _TIER_MAP_CACHE["ident"]
        and not _has_above_local(_TIER_MAP_CACHE["map"])
    ):
        # File present and UNCHANGED — this cache is current, not stale. Return it
        # verbatim: projecting here would drop a legitimate up-tier on every call.
        return _TIER_MAP_CACHE["map"]
    try:
        with open(path) as f:
            raw = (json.load(f) or {}).get("tierMap") or {}
        tm = _validate_tier_map(raw)
    except Exception:
        # As above, fail closed across config switches but retain the same
        # path's safe caps for a malformed or mid-write file.
        return _last_known_tier_map(path)
    _TIER_MAP_CACHE["path"], _TIER_MAP_CACHE["ident"], _TIER_MAP_CACHE["map"] = (
        path,
        ident,
        tm,
    )
    _backup_tier_map_to_disk(tm)  # refresh durable copy for a future wipe+restart
    return tm


def _tier_for(user_id, attested_tier=None):
    """Return the lower of broker-attested access and the local sender cap.
    Missing or invalid broker tiers resolve to guest."""
    wire = _normalized_tier(attested_tier)
    local = _normalized_tier(LOCAL_TIER)
    uid = (user_id or "").strip()
    if uid:
        mapped = _load_tier_map().get(uid)
        if mapped is not None:
            local = _normalized_tier(mapped)
    return wire if _TIER_RANK[wire] <= _TIER_RANK[local] else local


# ── inbound media fetch (owner screenshots, file uploads) ────────────────────
# A gateway can hand the task body a media MARKER instead of raw bytes:
MEDIA_MARKER_TAG = re.sub(r"[^A-Za-z0-9_-]", "",
                          os.environ.get("REMOTE_MEDIA_MARKER") or "remote-media")
MEDIA_MARKER_RE = re.compile(r"\[" + re.escape(MEDIA_MARKER_TAG) + r":([^\]]*)\]")

# Untrusted room-ops metadata block: the gateway appends a free-text
# `[room-ops metadata: …]` pointer to the operating card onto the message body.
_ROOM_OPS_META_RE = re.compile(r"\s*\[room-ops metadata:[^\]]*\]", re.IGNORECASE)


def _strip_room_ops_meta(body: str) -> "tuple[str, bool]":
    """Remove any untrusted `[room-ops metadata: …]` block(s) from a task body.

    Returns (cleaned_body, stripped) so the caller can log the quarantine. Runs
    before _one_line so a block split across newlines is still caught."""
    if not body or "room-ops metadata:" not in body.lower():
        return body, False
    cleaned = _ROOM_OPS_META_RE.sub("", body)
    stripped = cleaned != body
    # Return the cleaned body even when it is now empty: a metadata-ONLY body is
    # pure injection with no legitimate task text, so it must degrade to an empty
    return (cleaned.strip(), stripped)
HS_MEDIA_TOKEN = os.environ.get("REMOTE_MEDIA_HS_TOKEN") or ""
# The homeserver token is attached ONLY to media URLs on this exact origin
# (scheme+host+port). Without it configured, Matrix media URLs are never
HS_MEDIA_ORIGIN = (os.environ.get("REMOTE_MEDIA_HS_ORIGIN") or "").rstrip("/")
MEDIA_DIR = Path(os.environ.get("REMOTE_MEDIA_DIR") or str(_STATE / "remote-media"))
MAX_MEDIA_BYTES = int(os.environ.get("REMOTE_MEDIA_MAX_BYTES") or str(25 * 1024 * 1024))
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
}

# Bridges-as-siblings: discord/telegram/slack bridges write
# `state/last-owner-activity.json` whenever the owner messages them, so the
OWNER_ACTIVITY_FILE = _STATE / "last-owner-activity.json"  # sutando-only; harmless if unused


# Blocker (review 2026-06-13): the gateway is untrusted, so a task `id` flows
# into filesystem paths (task write + result read-back/POST). Reject anything
_TID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _valid_tid(tid: str) -> bool:
    return bool(_TID_RE.fullmatch(tid)) and tid not in (".", "..")


def _one_line(value) -> str:
    """Header-safe single-line value: CR/LF stripped so a gateway-controlled
    field can't inject extra `key: value` lines (e.g. forge a second
    access_tier). Applied to every field — task content is single-line in
    practice and a stray newline only ever indicates an injection attempt."""
    return str(value).replace("\r", " ").replace("\n", " ")


def _redact_url(value: str) -> str:
    """Scheme+host+path only — drop userinfo, query, and fragment before a URL
    is persisted. `gateway-status.json` lives under `state/` (which vault-syncs),
    so a gateway configured with `user:pass@` userinfo or a `?token=` query param
    must not land there in plaintext. Falls back to the bare string on any parse
    failure (never raise from a best-effort status write)."""
    try:
        p = urllib.parse.urlsplit(str(value))
        if not p.scheme and not p.netloc:
            return str(value)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except Exception:  # noqa: BLE001 — redaction must never break status I/O
        return str(value)


class _NeverFatalStream:
    """Logging must never take the poll loop down.

    Mirrors the merged `src/discord-bridge.py` fix (#2856). This package is
    standalone (`dependencies = []`, imports nothing from sutando's `src/`), so
    the guard is local by necessity rather than duplicated policy.

    The loop's own `except Exception  # keep the loop alive` cannot help here:
    every handler calls `_log()` first, so a `BrokenPipeError` from that print
    is raised *inside* the handler and escapes it. Stdout is a pipe whenever the
    launcher pipes to `tee`, which is how this bridge runs today.

    Swallow ONLY OSError (the EPIPE/EBADF class); anything else still propagates
    so real bugs are not masked.
    """

    __slots__ = ("_stream",)

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        try:
            return self._stream.write(data)
        except OSError:
            # Report the write as accepted: callers must not branch on it.
            return len(data)

    def flush(self):
        try:
            self._stream.flush()
        except OSError:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _NeverFatalStream(sys.stdout)
sys.stderr = _NeverFatalStream(sys.stderr)


def _log(msg: str) -> None:
    line = f"[remote-gateway-bridge] {msg}"
    print(line, flush=True)
    if _LAUNCHED_VIA == "supervised":
        return  # stdout already persisted by the supervisor's redirect
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if _LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
                _LOG_FILE.replace(_LOG_FILE.with_suffix(".log.1"))
        except FileNotFoundError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(_LOG_FILE, "a") as f:
            f.write(f"{stamp} {line}\n")
    except Exception:  # noqa: BLE001 — logging must never break the bridge
        pass


def _req(method: str, path: str, payload: dict | None = None, timeout: int = 35):
    """One authenticated HTTP request. Returns parsed JSON (or {} for empty)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{URL}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/json")
    # CloudFlare bot-fight (error 1010) rejects python-urllib's default
    # User-Agent with a 403; send an explicit client UA so the gateway's edge
    req.add_header("User-Agent", "sutando-gateway-client/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode().strip()
        return json.loads(raw) if raw else {}


def _http_error_body(e) -> str:
    """Best-effort read of an HTTPError's response body, for content-sniffing a
    per-task answer vs an endpoint-unsupported one. Never raises."""
    try:
        return (e.read() or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _read_token_file(path: str) -> str:
    """The onboarding string from a token file, or "" (missing / unreadable /
    empty — the caller treats all three as no-rotation). Two accepted shapes:
    a dotenv-style file carrying a REMOTE_TASK_TOKEN= line (legacy
    AG2_REMOTE_TOKEN= honored, optional `export `, surrounding quotes
    stripped), or the raw onboarding string alone on the first non-comment,
    '='-free line."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    # Collect BOTH alias assignments across the WHOLE file, then apply the
    # documented precedence REMOTE_TASK_TOKEN > AG2_REMOTE_TOKEN regardless of
    found: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        for key in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
            if line.startswith(key + "="):
                found[key] = _unquote_env(line[len(key) + 1:])
    for key in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
        if found.get(key):
            return found[key]
    for line in text.splitlines():
        line = line.strip()
        if line and "=" not in line and not line.startswith("#"):
            return line
    return ""


def _read_token_file_url(path: str) -> str:
    """The REMOTE_TASK_URL (legacy AG2_REMOTE_URL) from a SPLIT-layout token
    file, or "" if absent/unreadable. The combined `url|secret` form embeds the
    URL (extracted by _parse_onboarding_token), but a split file (bare
    REMOTE_TASK_TOKEN + a separate REMOTE_TASK_URL line) does not — and
    _read_token_file discards that URL. The reload path needs it so a split file
    rewritten by connect to a DIFFERENT gateway is caught by the same
    cross-gateway guard the combined form already gets; otherwise a re-onboard
    to a new gateway would hot-swap the new bearer onto the OLD running URL
    (the exact credential-boundary split the guard exists to prevent)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    found: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        for key in ("REMOTE_TASK_URL", "AG2_REMOTE_URL"):
            if line.startswith(key + "="):
                found[key] = _unquote_env(line[len(key) + 1:])
    return found.get("REMOTE_TASK_URL") or found.get("AG2_REMOTE_URL") or ""


def _reload_rotated_token() -> bool:
    """Re-read TOKEN_FILE and swap in a rotated SECRET. True only when a
    usable, DIFFERENT secret was found for the SAME gateway: the TOKEN global
    and the shared _AUTH_HEADERS dict are updated in place, so the poll loop,
    event channel, and card poster resume with the new bearer without a
    process restart.

    A rotation NEVER moves URL. The long-lived consumers (EventChannel,
    CardPoster) captured the base URL at construction, so honoring a new URL
    here would split the process across gateways — the poller on the new
    base, SSE + card posts still on the old one, now carrying the freshly
    rotated bearer to the old endpoint (review P1). Changing gateways is a
    reconfiguration, not a key rotation: refuse it loudly and keep waiting —
    a restart picks the new URL up through the normal import-time parse."""
    global TOKEN
    if not TOKEN_FILE:
        return False
    raw = _read_token_file(TOKEN_FILE)
    if not raw:
        return False
    # Route through the SAME parse used at import time (_parse_onboarding_token)
    # so a rotation written in the URL-encoded form (https://gw/relay%7C<secret>,
    url_from_token, secret = _parse_onboarding_token(raw)
    # The URL guard must cover BOTH layouts: the combined url|secret form
    # (url_from_token) AND the split form (bare secret + a separate
    file_url = (url_from_token or _read_token_file_url(TOKEN_FILE)).rstrip("/")
    if file_url and file_url != URL:
        _log(f"token file names a DIFFERENT gateway ({file_url}) "
             f"than the running one ({URL}) — a URL change is not hot-swappable; "
             "restart the bridge to move gateways")
        return False
    if not secret or secret == TOKEN:
        return False
    TOKEN = secret
    _AUTH_HEADERS["Authorization"] = f"Bearer {TOKEN}"
    return True


def _recover_auth(code: int) -> bool:
    """Auth-rejection recovery. An immediate re-read catches a rotation that
    already happened (the bridge lagged behind a re-onboard); otherwise hold
    in a slow re-check loop — keeping the poller singleton heartbeated so a
    supervisor sees ONE live waiting process instead of a crash-loop — until
    the token file rotates. Returns True once a rotated token is live; False
    when no TOKEN_FILE is configured (caller keeps the historical FATAL
    exit)."""
    # A new rejection episode invalidates any prior recovered terminal.
    _reenroll_state.pop("recovered_at", None)
    if _reload_rotated_token():
        _log("auth rejected but token file already rotated — resuming with new token")
        _reenroll_clear()
        return True
    _reenroll_claim()
    if not TOKEN_FILE and not _reenroll_state["code"] \
            and not (REENROLL_ENABLED and TOKEN):
        # Historical FATAL contract survives ONLY where recovery is truly
        # impossible: reenroll off, or no bearer to claim with (#2924).
        return False
    _log(f"gateway auth rejected (HTTP {code}) — waiting for token rotation"
         + (f" in {TOKEN_FILE}" if TOKEN_FILE else "")
         + (" or re-link approval" if _reenroll_state["code"]
            else " or re-link identity/claim")
         + f" (re-check every {AUTH_RECHECK_INTERVAL}s)")
    cycle = 0
    while True:
        pending = _reenroll_state["code"]
        # `backoff_s` means "retryable TRANSPORT backoff"; this loop is waiting on
        # a human, so it stays 0 — the re-check cadence is not a reconnect estimate.
        _emit_gateway_status(False,
                             error=(f"auth rejected HTTP {code} — relink pending "
                                    f"(code {pending})" if pending else
                                    f"auth rejected HTTP {code} — waiting for re-connect"))
        time.sleep(AUTH_RECHECK_INTERVAL)
        if not _heartbeat_singleton():
            sys.exit("FATAL: lost poller singleton while waiting for token rotation")
        if _reload_rotated_token():
            _log("rotated token detected — resuming")
            _reenroll_clear()
            return True
        if not pending:
            # A transient failure isn't a lost episode: retry stays in the
            # loop, cadence-bounded internally (safe while nothing is parked).
            _reenroll_claim()
        cycle += 1
        if cycle % REENROLL_PROBE_EVERY == 0 and _auth_probe():
            # Re-read: `pending` above predates this iteration's own claim,
            # which can park a code the log line must not miss (review).
            fresh_pending = bool(_reenroll_state["code"])
            _log("token accepted again — resuming"
                 + (" (re-link approved)" if fresh_pending else ""))
            # _reenroll_clear re-reads current state for was_pending, so this
            # is correct whether or not a code was parked when we got here.
            _reenroll_clear(recovered=True)
            return True


def _post_task_ack(tid: str) -> bool:
    """Tell the gateway a task made it safely into the local queue."""
    global _ack_disabled_until
    # Validate the WIRE id (post-conversion): a named instance's LOCAL id may
    # legitimately exceed the 64-char wire bound (review P1 #1) — refusing on
    if not _valid_tid(_broker_tid(tid)):
        return False
    if _ack_disabled_until and time.time() < _ack_disabled_until:
        return False  # gateway recently 404'd /ack — retry after the cooldown
    try:
        wire_tid = _broker_tid(tid)
        safe_tid = urllib.parse.quote(wire_tid, safe="")
        _req("POST", f"/v1/tasks/{safe_tid}/ack", {"id": wire_tid}, timeout=10)
        _ack_disabled_until = 0.0  # success (or re-enablement) → clear any backoff
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            # A 404/405 is ambiguous. The pre-/ack broker returns a bare no-route
            # 404/405 → the endpoint is UNSUPPORTED: back off (cooldown) and retry
            if e.code == 404 and "not leased" in _http_error_body(e).lower():
                return False   # per-task lease gone — keep acking the rest
            _ack_disabled_until = time.time() + ACK_UNSUPPORTED_COOLDOWN
            _log(f"gateway does not support task ack — retrying in "
                 f"{ACK_UNSUPPORTED_COOLDOWN}s")
            return False
        if e.code in (401, 403):
            raise
        _log(f"task ack failed for {tid}: HTTP {e.code} — gateway may redeliver")
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        _log(f"task ack network error for {tid}: {e} — gateway may redeliver")
        return False


_CORE_STEP_MAX = 500


def _core_str(v) -> str | None:
    """A core-status field → bounded non-empty str, or None. core-status.json is
    written by another process and may be malformed; a non-string field must not
    be forwarded (the broker calls .lower() on it)."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:_CORE_STEP_MAX] if v else None


def _read_core_status() -> tuple[str | None, str | None]:
    """Read this node's core-status.json → (status, step) for the presence layer.
    core-status is written by the proactive loop / task handlers (status =
    running|idle, step = human 'what it's doing'). The broker derives the agent's
    presence badge from it.

    MUST NOT raise: this runs in the main loop BEFORE the /v1/tasks poll, so an
    exception here would back the loop off and stall task delivery — a malformed
    presence side-channel must never become a delivery blocker. So we guard the
    JSON shape (a valid-JSON non-object would AttributeError on .get) and coerce
    every field to a bounded str-or-None; any surprise → (None, None) and the
    heartbeat still fires as a plain liveness ping."""
    try:
        with open(_STATE / "core-status.json") as f:
            cs = json.load(f)
        if not isinstance(cs, dict):
            return (None, None)
        status = _core_str(cs.get("status"))
        step = _core_str(cs.get("step"))
        # An idle status carries no meaningful step — send status only so the
        # sweep reads 'available' rather than stale 'what it was last doing'.
        return (status, None if status == "idle" else step)
    except Exception:  # noqa: BLE001 — best-effort; never stall the main loop
        return (None, None)


def _post_heartbeat(inflight: set[str], force: bool = False) -> bool:
    """Best-effort liveness + core-status ping. Liveness feeds hosted dashboards;
    the status/step feed the broker's presence sweep (agent working/available/…)."""
    global _heartbeat_disabled, _last_heartbeat_at
    if _heartbeat_disabled:
        return False
    now = time.time()
    if not force and now - _last_heartbeat_at < HEARTBEAT_INTERVAL:
        return False
    _last_heartbeat_at = now
    _status, _step = _read_core_status()
    try:
        payload = {
            "client": "sutando-gateway-client",
            "protocol_version": 1,
            "provider": PROVIDER,
            "tier": LOCAL_TIER,
            "inflight": len(inflight),
            "capabilities": ["task-ack", "heartbeat", "result-skip-markers",
                             "core-status", "team-collaborator"],
        }
        # Only include when present so a status-less node never clobbers the
        # broker's last-known core-status (the broker only records on presence).
        if _status is not None:
            payload["status"] = _status
        if _step is not None:
            payload["step"] = _step
        _req("POST", "/v1/heartbeat", payload, timeout=10)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            _heartbeat_disabled = True
            _log("gateway does not support heartbeat — continuing without")
            return False
        if e.code in (401, 403):
            raise
        _log(f"heartbeat failed: HTTP {e.code} — continuing")
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        _log(f"heartbeat network error: {e} — continuing")
        return False


def _emit_gateway_status(connected: bool, *, error: str | None = None,
                         backoff_s: int = 0) -> None:
    """Write `state/gateway-status.json` — the connection's own liveness, for a
    local supervisor to render connected-vs-reconnecting.

    Best-effort: a status-write failure MUST NOT disturb the poll loop, so all
    errors are swallowed. `last_ok_ts` is preserved across reconnecting writes
    (read back from the prior file) so a consumer can show "last connected N s
    ago" while the link is down.
    """
    try:
        last_ok = None
        try:
            with open(GATEWAY_STATUS_FILE) as f:
                last_ok = (json.load(f) or {}).get("last_ok_ts")
        except (FileNotFoundError, ValueError, OSError):
            last_ok = None
        now = int(time.time())
        if connected:
            last_ok = now
        payload = {
            "connected": bool(connected),
            "ts": now,
            "last_ok_ts": last_ok,
            "backoff_s": int(backoff_s),
            "error": _one_line(error) if error else None,
            "gateway": _redact_url(URL),
            "launched_via": _LAUNCHED_VIA,
            "schema_version": 1,
            "runtime": {**RUNTIME_IDENTITY, "engine": _engine_desc(),
                        **_ENGINE_COUNTS},
        }
        # Recovery surface: recovered ONLY via the probe-success terminal; a
        # missing block means "no episode known", never success.
        if _reenroll_state.get("code"):
            payload["reenroll"] = {
                "pending": True,
                "approval_code": _reenroll_state["code"],
                "claimed_at": _reenroll_state["claimed_at"],
            }
        elif _reenroll_state.get("recovered_at"):
            payload["reenroll"] = {
                "pending": False,
                "recovered": True,
                "recovered_at": _reenroll_state["recovered_at"],
            }
        # AWP P0 per-channel health: the task connection is `connected` above; the
        # additive event channel (if running) reports its own status, so a
        _ch = _EVENT_CHANNEL
        if _ch is not None:
            payload["channels"] = {
                "tasks": "connected" if connected else "reconnecting",
                "events": _ch.health.get("status"),
            }
            payload["events"] = {k: _ch.health.get(k) for k in
                                 ("status", "last_cursor", "last_event_at", "retry_count")}
        GATEWAY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Per-PID staging (sonichi/sutando#2222 follow-up): single-writer today,
        # but a shared temp name collides if a second sparrow instance ever runs;
        tmp = GATEWAY_STATUS_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, GATEWAY_STATUS_FILE)
    except Exception:  # noqa: BLE001 — never let status I/O break the poll loop
        pass


def _marker_attr(attrs: str, key: str) -> str:
    """Pull a `key=value` (value = non-space run) out of the marker attr tail."""
    m = re.search(rf"\b{re.escape(key)}=([^\s\]]+)", attrs)
    return m.group(1) if m else ""


def _to_authed_media_url(url: str) -> str:
    """Upgrade a legacy unauthenticated Matrix media route to the MSC3916
    authenticated client route (Matrix v1.11+). Leaves other URLs untouched.
      /_matrix/media/(r0|v3)/download/<server>/<id>
        → /_matrix/client/v1/media/download/<server>/<id>"""
    return re.sub(r"/_matrix/media/(?:r0|v3)/download/",
                  "/_matrix/client/v1/media/download/", url, count=1)


def _same_origin(url: str, base: str) -> bool:
    """True iff `url` shares scheme+host+port with `base` (exact origin match —
    parsed, never string-prefix: `https://relay.example.evil` must NOT match
    a base of `https://relay.example`). Whole body is guarded: `.port` raises
    ValueError at ACCESS time for a malformed port (`https://h:bad/`), and a
    gateway-controlled URL must never crash task intake — malformed ⇒ False."""
    try:
        u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
        if not u.scheme or not u.hostname or u.scheme != b.scheme:
            return False
        default = {"https": 443, "http": 80}.get(u.scheme)
        return (u.hostname.lower() == (b.hostname or "").lower()
                and (u.port or default) == (b.port or default))
    except ValueError:
        return False


def _under_gateway(url: str) -> bool:
    """True iff `url` is genuinely gateway-hosted: exact gateway origin AND the
    path sits at/under the gateway base path with a real `/` boundary (so a
    base path of `/relay` doesn't match `/relay-evil/...`). Malformed ⇒ False."""
    if not URL or not _same_origin(url, URL):
        return False
    try:
        base_path = urllib.parse.urlsplit(URL).path.rstrip("/")
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return False
    return path == base_path or path.startswith(base_path + "/")


def _download_bytes(url: str, headers: dict, cap: int) -> bytes:
    """GET raw bytes with an explicit size cap (reads cap+1 then rejects if
    over, so a missing/lying Content-Length can't OOM us). When an
    Authorization header is present, redirects are NOT followed — a
    gateway-controlled URL must not bounce our bearer to another host — and a
    3xx is treated as a FAILURE (raise) so the redirect page's body is never
    saved as if it were the media."""
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    if "Authorization" in headers:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):  # noqa: D401
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        resp_ctx = opener.open(req, timeout=30)
    else:
        resp_ctx = urllib.request.urlopen(req, timeout=30)
    with resp_ctx as resp:
        status = getattr(resp, "status", 200)
        if 300 <= status < 400:
            raise ValueError(f"authenticated media fetch got a redirect ({status})")
        data = resp.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"media exceeds {cap}-byte cap")
    return data


def _maybe_fetch_media(body: str, _refs_out: "list | None" = None) -> str:
    """If `body` carries a media marker, download the attachment to a local
    file and rewrite the marker to `[File attached: <path>]`. Returns the body
    unchanged on any failure (drop-in safe).

    When `_refs_out` is provided and a fetch succeeds, the corresponding
    `AttachmentRef` (interaction-model 4D, step 1.5) is appended to it — so
    _write_task can stamp structured `attachments:` headers alongside the legacy
    body line, without disturbing any of the drop-in-safe early returns."""
    m = MEDIA_MARKER_RE.search(body or "")
    if not m:
        return body
    inner = m.group(1).strip()
    if not inner:
        return body
    parts = inner.split(None, 1)
    url = parts[0]
    attrs = parts[1] if len(parts) > 1 else ""
    mime = _marker_attr(attrs, "mime")
    name = _marker_attr(attrs, "name")
    kind = _marker_attr(attrs, "kind")

    if not url.startswith(("https://", "http://")):
        return body
    headers = {"User-Agent": "sutando-gateway-client/1.0"}
    # Credential routing is by PARSED exact origin, never string prefix or
    # substring — `https://relay.example.evil/...` must not receive the
    try:
        _split = urllib.parse.urlsplit(url)
        _ = _split.port                    # raises ValueError on a malformed port
        url_path = _split.path
    except ValueError:
        return body                        # unparseable URL — leave marker untouched
    if _under_gateway(url):
        headers["Authorization"] = f"Bearer {TOKEN}"            # gateway media-proxy
    elif url_path.startswith("/_matrix/"):
        if not HS_MEDIA_TOKEN or not HS_MEDIA_ORIGIN or not _same_origin(url, HS_MEDIA_ORIGIN):
            return body                    # can't auth safely — leave marker
        url = _to_authed_media_url(url)
        headers["Authorization"] = f"Bearer {HS_MEDIA_TOKEN}"
    # else: a plain public URL — fetched with no credentials.

    try:
        data = _download_bytes(url, headers, MAX_MEDIA_BYTES)
    except Exception as e:  # noqa: BLE001 — drop-in safe
        _log(f"media fetch failed ({e}) — leaving marker as-is")
        return body
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        ext = _EXT_BY_MIME.get(mime.lower(), "")
        if not ext and "." in name:
            ext = "." + re.sub(r"[^A-Za-z0-9]", "", name.rsplit(".", 1)[1])[:8]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "attachment"
        if ext and safe.endswith(ext):
            safe = safe[: -len(ext)]
        # Exclusive create (mkstemp) — two same-name saves in the same
        # millisecond must get distinct paths, never overwrite (review
        fd, path_str = tempfile.mkstemp(prefix=f"{safe}-", suffix=ext, dir=MEDIA_DIR)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        path = Path(path_str)
    except Exception as e:  # noqa: BLE001
        _log(f"media save failed ({e}) — leaving marker as-is")
        return body
    _log(f"fetched media → {path} ({len(data)} bytes)")
    if _refs_out is not None:
        _refs_out.append(local_task_protocol.AttachmentRef(
            locator=str(path), mime=mime, filename=(name or path.name), size=len(data)))
    label = "Photo attached" if str(kind) == "m.image" else "File attached"
    # Replacement as a FUNCTION so backslashes/`\g<>` in the path can never be
    # interpreted as re.sub group references.
    return MEDIA_MARKER_RE.sub(lambda _m: f"[{label}: {path}]", body, count=1)


# Fleet-agent directory cache — peer agents (in the broker's /v1/agents) are
# NEVER the human owner, so their messages must not set owner-presence. Only the
_FLEET_AGENTS_TTL_S = 300.0
_fleet_agents_cache: dict = {"ts": 0.0, "ids": set()}


def _fleet_agent_ids() -> set[str]:
    """Broker-attested set of fleet agent mxids (from GET /v1/agents), cached
    ~5 min. FAIL-OPEN: on any fetch/parse error keep (and return) the last good
    set — never an empty set that would mistake a real peer for the owner. Before
    the first successful fetch the set is empty, so behavior is exactly today's
    (record) until the directory is known — presence must never SWALLOW genuine
    owner activity, only decline to record a KNOWN peer."""
    now = time.time()
    if now - _fleet_agents_cache["ts"] < _FLEET_AGENTS_TTL_S and _fleet_agents_cache["ids"]:
        return _fleet_agents_cache["ids"]
    try:
        resp = _req("GET", "/v1/agents")
        ids = {a.get("id") for a in (resp.get("agents") or []) if isinstance(a, dict) and a.get("id")}
        if ids:
            _fleet_agents_cache["ts"] = now
            _fleet_agents_cache["ids"] = ids
    except Exception:
        pass  # keep the prior good set (fail-open)
    return _fleet_agents_cache["ids"]


def _write_owner_activity(task: dict, sender_tier: str | None = None) -> None:
    """Record owner activity only for resolved owner-tier human senders.
    Reuse the task's resolved tier so routing and presence cannot diverge."""
    if sender_tier is None:
        sender_tier = _tier_for(task.get("user_id"), task.get("access_tier"))
    if sender_tier != "owner":
        return
    # Agent peers may have owner task authority but must not count as human-owner presence.
    # The broker-attested directory is authoritative for peer identity.
    _uid = (task.get("user_id") or "").strip()
    if _uid and _uid in _fleet_agent_ids():
        return
    try:
        OWNER_ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Strip a bracket prefix a gateway may add (e.g. "[Provider @user] body").
        body = (task.get("task") or "").lstrip()
        if body.startswith("[") and "]" in body:
            body = body[body.index("]") + 1:].lstrip()
        # Presence summaries are persisted, so redact secrets before writing them.
        body = filter_chat_secrets(body).text
        payload = {
            "ts": int(time.time()),
            "channel": task.get("source") or PROVIDER,
            "summary": body[:80],
        }
        # Propagate the routable room id so the core-supervisor relay can escalate
        # BACK into the AG2Space room the owner was last active in (resolve_active_
        _cid = str(task.get("channel_id") or "").strip()
        if _cid:
            payload["channel_id"] = _cid
        # Per-PID staging: last-owner-activity.json is written by FOUR processes
        # (this sparrow bridge + slack/discord/telegram). A shared ".json.tmp"
        tmp = OWNER_ACTIVITY_FILE.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, OWNER_ACTIVITY_FILE)
    except Exception as e:  # noqa: BLE001
        _log(f"owner-activity write failed: {e}")


def _write_task(task: dict) -> str | None:
    """Serialize a gateway task into tasks/task-<id>.txt (same schema as bridges).
    Returns the task id, or None if it has no id / already present."""
    broker_tid = str(task.get("id") or "").strip()
    if not broker_tid:
        _log("dropping task with no id")
        return None
    if not _valid_tid(broker_tid):
        _log(f"dropping task with unsafe id {broker_tid!r}")
        return None
    # Everything from here down — filenames, ledgers, archives, the serialized
    # id: header the core echoes back as the result filename — uses the LOCAL
    tid = _local_tid(broker_tid)
    task = {**task, "id": tid}
    dest = TASKS_DIR / f"{tid}.txt"
    # Idempotent: don't re-write a task already queued, claimed, or archived.
    if _task_pending(tid):
        return tid
    # Relay redelivery of already-handled work: on reconnect the gateway replays
    # its unacked pool, including tasks this node long since processed (the
    _task_archive = TASKS_DIR / "archive"
    task_archived = (
        # legacy flat layout: tasks/archive/<taskId>.txt
        (_task_archive / f"{tid}.txt").exists()
        # active month-partitioned layout: tasks/archive/YYYY-MM/<taskId>.txt
        # (see src/task-bridge.ts). Glob one level of month subdirs for this
        or next(_task_archive.glob(f"*/{tid}.txt"), None) is not None
    )
    if task_archived or _delivered_copy_exists(tid):
        rfile = RESULTS_DIR / f"{tid}.txt"
        if not rfile.exists():
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            rfile.write_text(GATEWAY_REDELIVERY_RESULT)
            # Provenance the result BODY cannot carry: a Team runtime controls
            # the body and can emit these exact bytes, but not this process's set.
            _REDELIVERED.add(tid)
        _log(f"dedup: {tid} already handled — not re-queued")
        return tid
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    # Promote only the exact broker boolean plus Team request; the legacy Guest
    # wire tier keeps old nodes restricted and body text cannot opt itself in.
    broker_tier = _normalized_tier(task.get("access_tier"))
    requested_tier = _normalized_tier(task.get("requested_access_tier"))
    broker_collaborator = (
        task.get("collaborator") is True
        and (broker_tier == "team" or requested_tier == "team")
    )
    attested_tier = "team" if broker_collaborator else broker_tier
    # Resolved once and reused below so routing and owner-activity cannot diverge.
    sender_tier = _tier_for(task.get("user_id"), attested_tier)
    collaborator_enabled = broker_collaborator and sender_tier == "team"
    lines = []
    # Which instance took delivery (shared-room fan-out: each Sutando writes its own
    # task file). Emitted just after id: below; KNOWN_HEADER_KEYS defangs a forged body copy.
    _recv = _reenroll_identity()
    _secret_types: tuple = ()
    for f in _TASK_FIELDS:
        if f == "session_scope":
            if task.get(f) == "room":
                lines.append("session_scope: room")
        elif f == "source":
            lines.append(f"source: {_one_line(task.get('source') or PROVIDER)}")
        elif f == "interaction_type":
            # Pass through when the gateway sends it; default to "message" —
            # all current gateway traffic is Matrix room messages. Whitelisted:
            it = str(task.get("interaction_type") or "")
            if it not in _INTERACTION_TYPES:
                it = "message"
            lines.append(f"interaction_type: {it}")
        elif f == "task" and task.get("task") not in (None, ""):
            # Keep the established id/timestamp prefix stable, but place this
            # trusted execution-policy header before all untrusted body text.
            if collaborator_enabled:
                lines.append("collaborator: true")
                if task.get("sensitive_data_filter") is False:
                    lines.append("sensitive_data_filter: false")
            # Quarantine the untrusted `[room-ops metadata: …]` block BEFORE it
            # reaches the agent as body content (owner directive 2026-07-16) —
            _raw_task, _stripped_meta = _strip_room_ops_meta(str(task["task"]))
            if _stripped_meta:
                _log(f"stripped untrusted room-ops metadata from {tid} body")
            # Resolve an inbound media marker to a local file the core can read.
            _media_refs: list = []
            _fetched = _maybe_fetch_media(_raw_task, _media_refs)
            # Intercept `vault set KEY VALUE` before ordinary redaction, owner-tier only.
            # Every path below intercepts-and-stores or falls through to a redactor — never neither.
            _intercept_fn, _redact_fn = _vault_intercept_fns()
            _redact_fallback = _redact_fn or _local_redact_vault_set
            if sender_tier == "owner" and _intercept_fn is not None:
                try:
                    _vault_result = _intercept_fn(_fetched)
                    _fetched = _vault_result.text
                    if _vault_result.stored:
                        _log(f"[vault] stored keys: {_vault_result.stored}")
                    if _vault_result.failed:
                        _log(f"[vault] store failed (still redacted): {_vault_result.failed}")
                except Exception as _vault_exc:
                    _fetched = _redact_fallback(_fetched)
                    _log(f"[vault] intercept errored "
                         f"({type(_vault_exc).__name__}: {_vault_exc}) — "
                         f"redacted, NOT stored")
            else:
                _fetched = _redact_fallback(_fetched)
            # Redact pasted secrets BEFORE the body is persisted (#2267 parity
            # with the discord/slack/telegram bridges): a token pasted into a
            _filtered = filter_chat_secrets(_fetched)
            if _filtered.secret_types:
                _secret_types = tuple(_filtered.secret_types)
                _log(f"redacted pasted secret(s) in {tid} body: "
                     f"{', '.join(sorted(_secret_types))}")
            lines.append(f"task: {_one_line(_filtered.text)}")
            # Make the sanitized body authoritative everywhere, not just this task file —
            # _write_owner_activity() re-reads task["task"] independently and isn't vault-aware.
            task["task"] = _filtered.text
            # interaction-model 4D, step 1.5: if a media marker was fetched,
            # stamp structured attachments[]/content_modalities/media_form
            if _media_refs:
                _txt = re.sub(r"\[(?:File|Photo) attached: [^\]]*\]", "", _fetched).strip()
                if _txt.startswith("[") and "]" in _txt:
                    _txt = _txt.split("]", 1)[1]
                _mh = local_task_protocol.media_attachment_headers(_media_refs, bool(_txt.strip()))
                if _mh:
                    lines.extend(_mh.rstrip("\n").split("\n"))
        elif f == "platform_card":
            # Signed platform-metadata pointer: re-serialize only the expected
            # subkeys as one compact JSON line (dict repr or extra keys never
            pc = task.get("platform_card")
            if isinstance(pc, dict) and all(k in pc for k in _PLATFORM_CARD_KEYS):
                card = {k: str(pc[k]) for k in _PLATFORM_CARD_KEYS}
                lines.append(f"platform_card: {json.dumps(card, separators=(',', ':'))}")
        elif f in task and task[f] not in (None, ""):
            lines.append(f"{f}: {_one_line(task[f])}")
            # After id: so the canonical id-first / HMAC-stamp prefix stays line 0.
            if f == "id" and _recv:
                lines.append(f"receiving_instance: {_one_line(_recv)}")
    # sender_tier is resolved once, ahead of the field loop above (needed there
    # for the "task" field's vault interception), and reused here unchanged.
    lines.append(f"access_tier: {sender_tier}")
    # The fixed prose notice follows access_tier without introducing recognized headers.
    if _secret_types:
        lines.append(secret_handling_instruction("AG2Space", _secret_types).strip("\n"))
    # Guest keeps the read-only Codex path. Team carries its guardrail IN-BAND:
    # closing the Team session route removed the only thing that used to deliver it.
    if sender_tier == "team":
        if collaborator_enabled:
            lines.append(engage_rulebook("room", AG2SPACE_PROVENANCE, f"results/{tid}.txt"))
        else:
            lines.extend(team_guardrail_lines(f"results/{tid}.txt"))
    if sender_tier == "guest":
        lines.extend(sandboxed_delegation_lines(
            "AG2 Space", "GUEST tier", f"results/{tid}.txt",
            "Research, inspect, explain, and draft only. Do not modify files or external systems.",
        ))
    # ===SKILL INSTRUCTIONS=== (owner-tier only): prose/numbered lines only, no
    # header-shaped lines, so appending after access_tier keeps it the last one.
    if sender_tier == "owner":
        _chan = _one_line(task.get("channel_id") or "")
        # shlex.quote: an unescaped quote in _chan must not close the shell
        # string early and turn the remainder into executable shell syntax.
        _chan_q = shlex.quote(_chan)
        # The credential and the notify lane are per-instance: a dev-homeserver
        # task needs ITS channel dir, not the default one this file was written for.
        _cdir_q = shlex.quote(CHANNEL_DIR)
        _step = 1
        _skill = ["", "===SKILL INSTRUCTIONS (follow before any other action)==="]
        _addr = _one_line(task.get("addressed_to") or "")
        if _addr:
            # Addressing gate (#649): the broker resolved this reply's target to a
            # peer agent. State it in-band so the check cannot fail to retrieve.
            _skill.append(
                f"{_step}. ADDRESSING: this message replies to {_addr}'s message and "
                f"does not mention you — it is {_addr}'s to claim. Do not process it "
                "unless a later message hands it to you explicitly; close your copy "
                "with [no-send].")
            _step += 1
        if _chan:
            _skill.append(
                f"{_step}. CONTEXT-FIRST (unconditional): before interpreting this "
                f"message, reconstruct the room thread — `python3 "
                f"skills/agent-room-ops/room_ops.py read {_chan_q} --limit 30` (if it "
                f"reports no gateway configured, load the channel env first: `set -a; . "
                f"\"$(bash scripts/channel-env.sh {_cdir_q})\"; set +a`) — and read it "
                "back (everyone's messages including your own prior replies) until this "
                "message stands on its own, then answer from the reconstructed thread, "
                "NOT from memory. Do this every time; do NOT skip it because the message "
                "looks self-contained or you feel you already understand it — felt "
                "confidence is exactly the signal that fails. The only exception is a "
                'pure greeting or acknowledgement with no referent (e.g. "hi", "thanks").')
            _step += 1
            # Which channel file holds REMOTE_TASK_* differs per onboarding, so the
            # prelude resolves it by content; notify.py's own guard can refuse a symlink.
            _skill.append(
                f"{_step}. NOTIFY FIRST (if task takes >60s): `set -a; . "
                f"\"$(bash scripts/channel-env.sh {_cdir_q})\"; set +a` then python3 "
                f"skills/task-progress/scripts/notify.py --source {_cdir_q} "
                f"--channel-id {_chan_q} --message \"On it — back in a moment.\"")
            _step += 1
        _skill.append(f"{_step}. Process and write the result to results/{tid}.txt")
        lines.extend(_skill)
    tmp = dest.with_suffix(".txt.tmp")
    from .local_task_protocol import apply_task_stamper
    tmp.write_text(apply_task_stamper("\n".join(lines) + "\n"))
    tmp.rename(dest)  # atomic publish so the watcher never sees a partial file
    _log(f"queued {tid}")
    # #2274 parity: one task_processed per NEWLY queued task (idempotent early
    # returns never reach here), bucketed to this gateway's own "remote" surface
    try:
        from telemetry import bucket_source, task_processed
        task_processed(bucket_source(_one_line(task.get("source") or PROVIDER), "remote"))
    except Exception:
        pass
    _record_task_room(tid, str(task.get("channel_id") or ""))
    # Bridges-as-siblings: feed the proactive-loop's active-engagement gate — but
    # only for owner-tier senders (same resolved tier as the task above).
    _write_owner_activity(task, sender_tier)
    return tid


def _load_dedup_aliases() -> "dict[str, str] | None":
    """The alias map, or None when it exists but cannot be read.

    None is not the same as empty: guessing "no alias" for an unreadable
    ledger POSTs a recovered answer under the re-ask id, which the broker is
    not waiting on.
    """
    if not DEDUP_ALIAS_FILE.exists():
        return {}
    try:
        loaded = json.loads(DEDUP_ALIAS_FILE.read_text())
    except (OSError, ValueError) as exc:
        _log(f"dedup alias ledger unreadable ({exc}) — deferring")
        return None
    return loaded if isinstance(loaded, dict) else None


def _save_dedup_aliases(aliases: dict[str, str]) -> bool:
    """Atomically persist the alias map. Returns False if it did not commit.

    Unlike the neighbouring sidecars this one is delivery-critical, so the
    caller must not retire the original delivery until it returns True.
    """
    try:
        DEDUP_ALIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEDUP_ALIAS_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(aliases, sort_keys=True))
        os.replace(tmp, DEDUP_ALIAS_FILE)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"dedup alias persist FAILED ({exc}) — keeping original delivery")
        return False


def _delivery_tid(tid: str) -> "str | None":
    """The id to POST under, or None when the ledger cannot be read."""
    aliases = _load_dedup_aliases()
    return None if aliases is None else aliases.get(tid, tid)


def _forget_dedup_alias(tid: str) -> None:
    aliases = _load_dedup_aliases()
    if aliases is None:
        return
    if aliases.pop(tid, None) is not None:
        _save_dedup_aliases(aliases)  # cleanup: a stale entry is harmless


def _load_task_rooms() -> dict[str, str]:
    """Restore the {task id → room id} sidecar map (fail-open to empty)."""
    try:
        data = json.loads(TASK_ROOMS_FILE.read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        _log(f"task-rooms file unreadable ({e}) — starting empty")
        return {}


def _save_task_rooms(rooms: dict[str, str]) -> None:
    """Atomically persist the task→room map. Best-effort (never blocks the loop)."""
    try:
        TASK_ROOMS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Per-PID staging (sonichi/sutando#2222 follow-up): collision-proof if a
        # second sparrow instance ever runs. os.replace is atomic overwrite.
        tmp = TASK_ROOMS_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rooms, sort_keys=True))
        os.replace(tmp, TASK_ROOMS_FILE)
    except Exception as e:  # noqa: BLE001
        _log(f"task-rooms persist failed ({e}) — continuing")


def _record_task_room(tid: str, room: str) -> None:
    if not room:
        return
    rooms = _load_task_rooms()
    if rooms.get(tid) != room:
        rooms[tid] = room
        _save_task_rooms(rooms)


def _forget_task_room(tid: str) -> None:
    rooms = _load_task_rooms()
    if tid in rooms:
        rooms.pop(tid)
        _save_task_rooms(rooms)


def _upload_attachment(room: str, path_str: str) -> tuple[bool, str]:
    """Upload one allowlisted local file to the task's room via the gateway
    media endpoint. Returns (ok, reason)."""
    fpath = os.path.realpath(os.path.expanduser(path_str.strip()))
    if not is_path_sendable(fpath):
        return False, "path not allowlisted"
    try:
        size = os.path.getsize(fpath)
    except OSError as e:
        return False, f"stat failed: {e}"
    if size > MAX_MEDIA_BYTES:
        return False, f"file exceeds {MAX_MEDIA_BYTES} bytes"
    try:
        with open(fpath, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        return False, f"read failed: {e}"
    safe_room = urllib.parse.quote(room, safe="")
    try:
        _req("POST", f"/v1/rooms/{safe_room}/media",
             {"filename": os.path.basename(fpath), "content_b64": content_b64},
             timeout=60)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return False, f"network error: {e}"
    return True, ""


def _archive_result(path: Path, tid: str) -> None:
    ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path.rename(ARCHIVE_RESULTS_DIR / f"{tid}-{int(time.time())}.txt")
    except OSError:
        path.unlink(missing_ok=True)
    # The delivered task's queue file comes along too — otherwise served tasks
    # sit in tasks/ forever and the health-check counts them as a stuck queue.
    tfile = find_task_file(TASKS_DIR, tid)
    if tfile is not None:
        archive_dir = TASKS_DIR / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            tfile.rename(archive_dir / f"{tid}.txt")
        except OSError:
            pass  # best-effort; core may have archived it concurrently


# A legacy bare `.sending` claim carries no owner info, so recovery for those
# falls back to an age guard: younger than this = possibly a live worker's
_ORPHAN_MIN_AGE_S = 600

# An empty body observed right after claiming is a writer mid-flush, so it is
# always re-queued — and NEVER moved to a terminal resting place.

# Filenames THIS process has already logged as claimed-empty, so a genuinely
# orphaned nudge is noted once instead of on every pass. Discarded when the file
_EMPTY_LOGGED: "set[str]" = set()

# Bodies above this never fit a Matrix event, so they are undeliverable no
# matter how often they are retried; they are dead-lettered instead of looping.
_PROACTIVE_MAX_BODY_B = 48 * 1024

# Destination FORMAT validation is this bridge's own job ("the bridge
# validates the id format for its platform when applying" — result_markers).
_MATRIX_ROOM_RE = re.compile(r"^![^\s:]+:\S+$")


def _proactive_route(body: str) -> "tuple[str, str | None, str]":
    """('send', room_or_None, stripped-body) | ('foreign', None, '') |
    ('drop', None, '').

    Marker grammar comes SOLELY from parse_markers() (no private parser —
    CLAUDE.md result-marker contract); this function only applies the actions
    this transport supports:
      * skip markers   → 'drop' (archive silently, deliver nothing)
      * [dm-only]      → parse_markers already suppressed any redirect, so the
                         body falls through to the default (owner) room
      * [channel: !r:s]→ 'send' to that room, marker stripped
      * [channel: C…/digits] → 'foreign' — that bridge owns the file (review
                         blocker: claiming it here would leak the raw body)
      * attach markers → stripped by the parser; uploads are unsupported on
                         the room-message op, so the actions are ignored
    """
    parsed = parse_markers(body)
    if any(a.kind == "skip" for a in parsed.actions):
        return ("drop", None, "")
    redirect = next((a for a in parsed.actions if a.kind == "redirect"), None)
    if redirect is not None:
        dest = redirect.value
        if _MATRIX_ROOM_RE.match(dest):
            return ("send", dest, parsed.body)
        return ("foreign", None, "")
    return ("send", None, parsed.body)


def _record_proactive_receipt(item_id: str, room: str) -> None:
    """Durable "delivered where" for the proactive leg. The log line naming the
    room rotates; this outlives it. Fail-open: a receipt write must never
    unwind a delivery that already happened."""
    try:
        record_delivered(RESULTS_DIR / ".outbox-ag2space-proactive", item_id,
                         provider="ag2space-proactive", destination=room)
    except Exception as e:  # noqa: BLE001 — receipt is best-effort by design
        _log(f"proactive receipt write failed for {item_id}: {e} "
             "(delivery unaffected)")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not signalable — treat as alive
    return True


def _recover_orphan_proactive() -> None:
    """Restart-safety: recover orphan proactive claims back to `.txt` so the
    next drain re-claims them — WITHOUT stealing a live worker's in-flight
    claim (review blocker). Claims are pid-scoped (`.sending.<pid>`): a claim
    whose owner pid is alive is left alone; a dead owner's claim recovers
    immediately. Legacy bare `.sending` claims (no owner info) recover only
    past an age threshold. Runs without PROACTIVE_ROOM: a file naming its own room
    is now drainable, so its orphan claims must recover too."""
    for f in list(RESULTS_DIR.glob("proactive-*.sending.*")) \
            + list(RESULTS_DIR.glob("proactive-*.sending")):
        name = f.name
        owner = name.rsplit(".sending.", 1)
        if len(owner) == 2:  # pid-scoped claim
            try:
                pid = int(owner[1])
            except ValueError:
                continue  # not ours to interpret
            if _pid_alive(pid):
                continue  # live worker's in-flight claim (incl. our own
                # process's other thread) — never steal
        else:  # legacy bare .sending — age guard only
            try:
                if time.time() - f.stat().st_mtime < _ORPHAN_MIN_AGE_S:
                    continue
            except OSError:
                continue
        target = f.with_name(name.split(".sending")[0] + ".txt")
        if target.exists():
            continue  # collision — leave for an operator, never clobber
        try:
            f.rename(target)
            _log(f"recovered orphan proactive claim {f.name}")
        except OSError:
            pass


def _retire_proactive(claim: Path, original: Path, dest_dir: Path) -> None:
    """Retire a claimed nudge that was NOT delivered, without destroying it.

    Used by the terminal non-delivery paths (settled-empty, dead-lettered).
    Deliberately not shared with the delivered path: there the content already
    reached the owner, so unlinking on an archive failure is harmless, whereas
    handing the claim back would re-send a duplicate. Here nothing was
    delivered, so the fallback is the opposite — hand it back as `.txt` rather
    than unlink, since a duplicate on a later pass beats a lost message.
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        claim.rename(dest_dir / f"{original.stem}-{int(time.time())}.txt")
    except OSError as e:
        _log(f"could not retire proactive {original.name} ({e}) — re-queueing")
        try:
            claim.rename(original)
        except OSError:
            pass


def _resolve_send_failure(claim, original, exc) -> str:
    """Bounded retry: decision AND file moves are the shared policy's
    (send_failure_policy.resolve_failed_send). This binder passes sparrow's
    pid-scoped claim's real body path — with_suffix() cannot derive it — and
    the park directory, then renders the bridge's log phrase. Single sends
    can't partially deliver, so `progressed` stays False here.

    Returns the phrase for the caller's log line.
    """
    tried = _PROACTIVE_ATTEMPTS.get(original.name, 0)
    outcome = resolve_failed_send(
        claim, exc, _PROACTIVE_ATTEMPTS,
        body=original, undelivered_dir=UNDELIVERABLE_RESULTS_DIR)
    if outcome == "retried":
        return f"will retry ({tried + 1}/{MAX_TRANSIENT_ATTEMPTS})"
    if outcome == "parked":
        return (f"PARKED to {UNDELIVERABLE_RESULTS_DIR.name}/ after {tried + 1} "
                "send attempt(s) — it will NOT be re-sent")
    return "stuck"


def _post_proactive() -> None:
    """Deliver `results/proactive-*.txt` to PROACTIVE_ROOM as room messages.

    Claim-by-rename (`.txt` → `.sending`) so a concurrent drain (or a racing
    legacy bridge on a multi-channel host) can't double-deliver; a delivered
    file archives beside task results; a failed POST renames the claim back
    to `.txt` for retry on the next loop pass. Auth errors propagate to the
    caller (the poll loop owns auth handling); everything else is per-file
    fail-open — one malformed nudge never blocks the rest. A file naming its own
    Matrix room is delivered whether or not PROACTIVE_ROOM is set; only a file
    with no target needs it. A host-injected PROACTIVE_CLAIM_GATE may defer a file
    that belongs to another bridge (cross-bridge routing stays host policy)."""
    for f in sorted(RESULTS_DIR.glob("proactive-*.txt")):
        # PEEK before claiming: a file explicitly routed to a non-Matrix
        # destination ([channel: <discord/slack id>]) belongs to that bridge —
        try:
            route, peek_room, _ = _proactive_route(f.read_text(encoding="utf-8"))
        except OSError:
            continue  # racing consumer already claimed it
        if route == "foreign":
            continue
        # No target of its own AND no default: skip BEFORE claiming. Claiming it
        # would spin (claim -> no destination -> hand back) on every pass.
        if route == "send" and peek_room is None and not PROACTIVE_ROOM:
            continue
        if PROACTIVE_CLAIM_GATE is not None:
            try:
                if not PROACTIVE_CLAIM_GATE(f):
                    continue  # another bridge's file right now; retry next pass
            except Exception:
                pass  # a broken gate must not strand owner nudges — claim
        # pid-scoped claim: recovery can tell a live worker's in-flight claim
        # from a dead one's (review blocker: bare .sending was stealable).
        claim = f.with_suffix(f".sending.{os.getpid()}")
        try:
            f.rename(claim)  # atomic claim; loser of a race just misses
        except OSError:
            continue
        # Re-read and re-route AFTER the claim, and act only on THIS result.
        # The peek above can observe a writer mid-write (file created, body not
        try:
            route, room_override, routed_body = _proactive_route(
                claim.read_text(encoding="utf-8"))
        except OSError as exc:
            # A TRANSIENT post-claim read failure must not strand the nudge: the
            # file is now `.sending.<our-pid>`, and _recover_orphan_proactive()
            try:
                claim.rename(f)
            except OSError as restore_exc:
                _log(f"CRITICAL: proactive {claim.name} post-claim read failed "
                     f"({exc}) AND restore to {f.name} failed ({restore_exc}) — "
                     f"owner nudge stranded under live pid until restart")
            continue
        if route == "foreign" or (
                route == "send" and room_override is None and not PROACTIVE_ROOM):
            # Hand back rather than eat: a foreign target seen only post-claim,
            # or one that vanished with no default (room_id=None loses the body).
            try:
                claim.rename(f)
            except OSError:
                pass
            continue
        if route == "drop":
            # Skip marker ([no-send]/[REPLIED]/[deduped:]) — the protocol says
            # archive silently, deliver nothing. Nothing was delivered, so on
            _log(f"proactive {f.name} carries a skip marker — archiving, no send")
            _retire_proactive(claim, f, ARCHIVE_RESULTS_DIR)
            continue
        body = routed_body.strip()
        if not body:
            # An empty claim means the producer has not flushed the body yet.
            # NEVER move this inode: no observable signal proves the writer is
            if f.name not in _EMPTY_LOGGED:
                _EMPTY_LOGGED.add(f.name)
                _log(f"proactive {f.name} claimed empty — producer has not "
                     f"flushed yet; handing back (never dead-lettered, a late "
                     f"flush must still deliver)")
            try:
                claim.rename(f)
            except OSError:
                pass
            continue
        # Non-empty now — drop any prior empty observation so a file that merely
        # flushed late can log again if it is ever re-observed empty.
        _EMPTY_LOGGED.discard(f.name)
        if len(body.encode("utf-8")) > _PROACTIVE_MAX_BODY_B:
            # Every failure branch below re-queues unconditionally, so a body
            # that can NEVER be delivered would retry and log on every loop
            _log(f"proactive {f.name} body is "
                 f"{len(body.encode('utf-8'))}B (> {_PROACTIVE_MAX_BODY_B}B) "
                 "— dead-lettering, it can never be delivered")
            _retire_proactive(claim, f, UNDELIVERABLE_RESULTS_DIR)
            continue
        dest_room = room_override or PROACTIVE_ROOM
        try:
            resp = _req("POST", "/v1/room",
                        {"op": "message",
                         "room_id": dest_room,
                         "body": body},
                        timeout=15)
            # A bare 200 is NOT proof of delivery: the gateway can swallow a
            # room-send failure server-side (bad room id, kicked agent,
            receipt = classify_response(200, resp, id_keys=("event_id",))
            delivered = receipt.outcome is DeliveryOutcome.CONFIRMED or (
                PROACTIVE_TRUST_OK and isinstance(resp, dict) and resp.get("ok") is True
            )
            if not delivered:
                # Accepted but unconfirmed. It may ALSO have been delivered, so
                # the retry must be bounded — an unbounded one duplicates.
                outcome = _resolve_send_failure(
                    claim, f, _UnconfirmedDelivery("no event_id in response"))
                _log(f"proactive send for {f.name} got no delivery signal "
                     f"(response {str(resp)[:120]!r}) — {outcome}; check "
                     "REMOTE_PROACTIVE_ROOM and the agent's room membership")
                continue
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                try:
                    claim.rename(f)  # un-claim before auth handling takes over
                except OSError:
                    pass
                raise
            outcome = _resolve_send_failure(claim, f, e)
            _log(f"proactive send failed for {f.name}: HTTP {e.code} — {outcome}")
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            outcome = _resolve_send_failure(claim, f, e)
            _log(f"proactive send network error for {f.name}: {e} — {outcome}")
            continue
        _record_proactive_receipt(f.stem, dest_room)
        ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            claim.rename(ARCHIVE_RESULTS_DIR / f"{f.stem}-{int(time.time())}.txt")
        except OSError:
            claim.unlink(missing_ok=True)
        _PROACTIVE_ATTEMPTS.pop(f.name, None)
        _ENGINE_COUNTS["legacy_sends"] += 1
        _log(f"delivered proactive {f.name} to {dest_room}")


def _load_inflight() -> set[str]:
    """Restore the in-flight set from disk (fail-open to empty)."""
    try:
        data = json.loads(INFLIGHT_FILE.read_text())
        return {str(t) for t in data} if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001
        _log(f"inflight file unreadable ({e}) — starting empty")
        return set()


def _save_inflight(inflight: set[str]) -> None:
    """Atomically persist the in-flight set. Best-effort (never blocks the loop).
    The mutex covers snapshot+write: the poll loop and the outbound worker both
    save, and an unguarded interleave could persist a state missing the other
    thread's mutation (resurrecting a delivered id or dropping a fresh one)."""
    with _INFLIGHT_MUTEX:
        try:
            INFLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Per-PID staging (sonichi/sutando#2222 follow-up): collision-proof if
            # a second sparrow instance ever runs. os.replace is atomic overwrite.
            tmp = INFLIGHT_FILE.with_suffix(f".json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(sorted(inflight)))
            os.replace(tmp, INFLIGHT_FILE)
        except Exception as e:  # noqa: BLE001
            _log(f"inflight persist failed ({e}) — continuing")


# (tid, path) pairs already uploaded this process — result-POST retry guard.
_uploaded_attachments: set[tuple[str, str]] = set()


def _dedup_plan(tid: str, holder_id: str | None):
    """Shared dedup recovery, bound to this adapter's directories.

    A requeue carries the delivery forward: the re-ask keeps the room, stays
    in flight, and aliases back to the id the broker is waiting on. Without
    that the re-ask is written and its answer is never looked for.
    """
    room = _load_task_rooms().get(tid, "")

    def _commit(new_id: str) -> bool:
        """Persist routing for the re-ask before it becomes visible."""
        delivery = _delivery_tid(tid)
        aliases = _load_dedup_aliases()
        if delivery is None or aliases is None:
            return False
        aliases[new_id] = delivery
        if not _save_dedup_aliases(aliases):
            return False
        rooms = _load_task_rooms()
        rooms[new_id] = room
        _save_task_rooms(rooms)
        return True

    action, payload = plan_dedup_recovery(
        RESULTS_DIR, TASKS_DIR, tid, holder_id, room,
        f"task-{uuid.uuid4().hex[:18]}", commit_identity=_commit)
    return action, payload, room


def _lease_close_body(skip) -> str:
    """Canonical bytes for a lease close — the marker, never the sender's prose.

    The guard decides THAT a result is suppressed; what rides this wire is the
    transport's own choice, and nobody reads a suppressed body.
    """
    if skip.value == "deduped":
        extra = (skip.extra or "").strip()
        return (f"[deduped: {extra}]"
                if local_task_protocol.valid_archive_lookup_id(extra) else "[no-send]")
    return "[REPLIED]" if skip.value == "REPLIED" else "[no-send]"


_DELIVERY_CORE: "DeliveryCore | None" = None


def _delivery_core() -> DeliveryCore:
    """The outbound result leg behind the ClaimBackend/DeliveryProvider seam:
    claim, retry, ambiguity and crash-recovery semantics live in DeliveryCore;
    this bridge keeps presentation (guard, markers, attachments) and the
    resolved dirs. The ceiling is the shared outbound cap, NOT the legacy
    retry-every-pass behaviour: an unbounded retry is a duplicate generator.
    The root lives INSIDE the
    results dir it drains (archive/ and undelivered/ precedent), so every
    harness that redirects RESULTS_DIR is hermetic for free; the singleton is
    keyed by that root and recomposes when it moves."""
    global _DELIVERY_CORE
    root = RESULTS_DIR / f".outbox{_INST_SUFFIX}"
    if _DELIVERY_CORE is None or _DELIVERY_CORE.backend.root != root:
        _DELIVERY_CORE = DeliveryCore(
            DesignAClaimBackend(root),
            # Late-bound so token rotation reassigning module globals (and the
            # test harness's _req double) reach the provider mid-process.
            AG2SpaceResultProvider(lambda *a, **k: _req(*a, **k)),
            policy=RetryPolicy(max_attempts=MAX_TRANSIENT_ATTEMPTS),
            worker="gateway-result-drain")
    return _DELIVERY_CORE


def _quarantine_undelivered(rfile, tid: str, why: str) -> None:
    """Move a result the outbox has finally refused into results/undelivered/,
    the same quarantine the proactive path uses. Without this the file is
    rescanned every pass and the refusal is invisible."""
    try:
        UNDELIVERABLE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        rfile.rename(UNDELIVERABLE_RESULTS_DIR /
                     f"{rfile.stem}-{int(time.time())}.txt")
        _log(f"result {tid}: {why} — quarantined to "
             f"{UNDELIVERABLE_RESULTS_DIR.name}/, it will NOT be re-sent")
    except OSError as e:
        _log(f"result {tid}: {why} but quarantine failed ({e}) — "
             "leaving it in place")


def _worker_of(task_id: str) -> str:
    """Which pool worker finished this task, read from the per-core done-flag.
    Ambiguous or absent means unattributed — never a guess, never an error."""
    try:
        hits = sorted((_STATE / "cores").glob(f"*/done/task-{task_id}.flag"))
    except OSError:
        return ""
    return hits[0].parent.parent.name if len(hits) == 1 else ""


def _deliver_result_payload(tid: str, broker_tid: str, body: str,
                            no_send: bool = False, result_file=None) -> bool:
    """One outbound result POST through the delivery core. True = the
    gateway confirmed (server lease closed; caller archives). False = not
    confirmed this pass; leave the result file for the next one."""
    core = _delivery_core()
    # `no_send` is the broker's STRUCTURED suppression field: the lease must
    # close without a user-facing send. It rides the payload, not the body.
    doc = {"id": broker_tid, "body": body}
    if no_send:
        doc["no_send"] = True
    # Structured attribution, not the "— core-N" prose in the body: the
    # signature is for humans and reformatting it must not change routing.
    worker = _worker_of(tid)
    if worker:
        doc["metadata"] = {"worker_id": worker}
    payload = json.dumps(doc).encode("utf-8")
    core.backend.publish(broker_tid, payload)   # False = already live: retry pass
    res = core.deliver_one(broker_tid, payload)
    if res.status is DrainStatus.TERMINAL:
        # The outbox has decided this item; no pass will ever claim it again,
        # so retrying logs forever and hides the failure behind "will retry".
        why = (f"outbox item is terminal after "
               f"{core.backend.attempts(broker_tid)} attempt(s)")
        if result_file is not None:
            _quarantine_undelivered(result_file, tid, why)
        else:
            _log(f"result {tid}: {why} — not retrying")
        return False
    if res.status is DrainStatus.NOT_CLAIMED:
        # A dead prior incarnation's claim; reclaim-TTL recovers it, and
        # with an idempotent provider nothing parks on ambiguity.
        _log(f"result {tid}: outbox item not claimable this pass "
             f"(attempts={core.backend.attempts(broker_tid)}) — will retry")
        return False
    if res.outcome is CoreDeliveryOutcome.CONFIRMED:
        _ENGINE_COUNTS["core_confirmed"] += 1
        # A confirmed send was otherwise silent, so nothing on the happy path
        # told a live round trip apart from the legacy one it replaces.
        _log(f"result {tid} delivered via DeliveryCore "
             f"(provider={type(core.provider).__name__}, "
             f"backend={type(core.backend).__name__}, worker={core.worker})")
        return True
    _log(f"result POST not confirmed for {tid} "
         f"({res.outcome.value if res.outcome else '?'}) — will retry")
    return False


def _result_tier(tid: str) -> "str | None":
    """Resolve the task tier; unknown provenance stays on the guarded path."""
    try:
        tfile = find_task_file(TASKS_DIR, tid) or find_archived_task(TASKS_DIR, tid)
        return (team_result_guard.resolve_access_tier(tfile)
                if tfile is not None else "guest")
    except Exception:
        return None


def _post_ready_results(inflight: set[str]) -> None:
    """For each in-flight task, if its result file exists, POST it + archive."""
    changed = False
    for tid in list(inflight):
        if not _valid_local_tid(tid):  # defense-in-depth: never read an unsafe path (local ids may carry the instance encoding)
            inflight.discard(tid); changed = True
            continue
        rfile = RESULTS_DIR / f"{tid}.txt"
        raw = read_ready_result(rfile)
        if raw is None:
            continue
        # The guard honours suppression on every tier now, so there is no stub
        # to pre-apply; the ordinary guarded path returns the body unchanged.
        body, _withheld = _guarded_result_body(tid, raw)
        if body is None:
            _log(f"result guard unavailable for {tid} — leaving for retry")
            continue
        if _withheld:
            _log(f"withheld non-owner result for {tid}: {_withheld}")
        # Route marker decisions through the unified parser (#873) like the
        # other bridges — no hand-rolled startswith checks.
        parsed = parse_markers(body)
        skip = next((a for a in parsed.actions if a.kind == "skip"), None)
        # Every dedup marker routes through the shared plan, malformed included:
        # it owns the reject-and-report policy (dedup_recovery.plan_dedup_recovery).
        if skip and skip.value == "deduped":
            action, payload, room = _dedup_plan(tid, skip.extra)
            if action == "defer":
                # Nothing was retired; the next pass retries the whole decision.
                _log(f"dedup deferred for {tid} — alias not committed")
                continue
            if action != "honour":
                if action == "report":
                    # The results endpoint is keyed by delivery id; an empty
                    # room map must not turn the report into a silent drop.
                    _delivery = _delivery_tid(tid)
                    if _delivery is None:
                        _log(f"dedup report deferred for {tid} — ledger unreadable")
                        continue
                    # The report IS the delivery: archiving before confirm
                    # would strand the ask exactly as the unreported dedup did.
                    if not _deliver_result_payload(tid, _broker_tid(_delivery),
                                                  payload):
                        continue
                _holder = (skip.extra or "").strip()
                # An out-of-grammar holder is sender-controlled; name its shape,
                # never its bytes.
                _shown = (_holder if local_task_protocol.valid_archive_lookup_id(_holder)
                          else f"<malformed, {len(_holder)} chars>")
                _log(f"dedup {action} for {tid} (holder {_shown} delivered nothing)")
                _archive_result(rfile, tid)
                inflight.discard(tid)
                _forget_task_room(tid)
                _forget_dedup_alias(tid)
                if action == "requeue":
                    inflight.add(payload)
                changed = True
                continue
        if skip:
            # Skip markers still POST: only add_result closes the server lease;
            # the server suppresses their user-facing delivery.
            _delivery = _delivery_tid(tid)
            if _delivery is None:
                _log(f"delivery deferred for {tid} — alias ledger unreadable")
                continue
            if not _deliver_result_payload(tid, _broker_tid(_delivery),
                                           _lease_close_body(skip),
                                           no_send=True):
                continue
            _archive_result(rfile, tid)
            # Retire the provenance WITH the result, never at read: this line is
            # only reached once the lease-closing POST has actually succeeded.
            _REDELIVERED.discard(tid)
            inflight.discard(tid)
            _forget_task_room(tid)
            changed = True
            _log(f"archived {tid} (marker {skip.value}, lease closed, not sent)")
            continue
        out_body = parsed.body
        redirect = next((a for a in parsed.actions if a.kind == "redirect"), None)
        if redirect:
            # Cross-room redirect is handled GATEWAY-side for this transport —
            # re-stitch the marker the parser stripped so the server still
            out_body = f"[channel: {redirect.value}]\n{out_body}"
        attaches = [a.value for a in parsed.actions if a.kind == "attach"]
        if attaches:
            room = _load_task_rooms().get(tid, "")
            sent = 0
            for fp in attaches:
                # Uploads happen before the result POST (so failures can be
                # annotated in-band); if that POST then fails and this loop
                if (tid, fp) in _uploaded_attachments:
                    sent += 1
                    continue
                ok, reason = (_upload_attachment(room, fp) if room
                              else (False, "origin room unknown"))
                if ok:
                    sent += 1
                    _uploaded_attachments.add((tid, fp))
                    _log(f"attached {fp} to {room} for {tid}")
                else:
                    # Keep the information in-band rather than dropping the
                    # file silently — mirrors the other bridges' rejection UX.
                    out_body += f"\n[attachment not sent: {fp} ({reason})]"
                    _log(f"attachment skipped for {tid}: {fp} ({reason})")
            if not out_body.strip() and sent:
                out_body = "(file attached)"
        _delivery = _delivery_tid(tid)
        if _delivery is None:
            _log(f"delivery deferred for {tid} — alias ledger unreadable")
            continue
        if not _deliver_result_payload(tid, _broker_tid(_delivery), out_body,
                                       result_file=rfile):
            continue
        _archive_result(rfile, tid)
        inflight.discard(tid)
        _forget_task_room(tid)
        _forget_dedup_alias(tid)
        changed = True
        _log(f"delivered result for {tid}")
    if changed:
        _save_inflight(inflight)


def _reconcile_abandoned(inflight: set[str], suspects: set[str]) -> set[str]:
    """Drop in-flight ids that can never complete through this loop: the task
    file is no longer pending in tasks/ AND no result file is waiting. That
    combination means the task was completed elsewhere (a concurrent core
    racing the same workspace, a manual sweep to tasks/processed/, or history
    from before a restart) — this client will never see a result to POST, so
    the id would otherwise strand in the ledger forever. Stranded ids inflate
    the heartbeat's `inflight` count monotonically until the broker's presence
    sweep marks the agent unassignable (observed 2026-07-09: 175 stranded ids,
    0 with any pending work).

    Two consecutive sightings are required before dropping (`suspects` carries
    the previous pass's candidates): a result landing between the task-file
    check and the discard is then picked up by the next `_post_ready_results`
    instead of being raced. Returns the new suspects set for the next pass."""
    gone = {tid for tid in inflight
            if _valid_local_tid(tid)
            and not _task_pending(tid)
            and not (RESULTS_DIR / f"{tid}.txt").exists()
            and not _task_archived_recently(tid)}
    confirmed = gone & suspects
    if confirmed:
        for tid in sorted(confirmed):
            inflight.discard(tid)
            _log(f"dropped abandoned in-flight id {tid} (no task/result file — completed elsewhere)")
        _save_inflight(inflight)
    return gone - confirmed


# A task archived here minutes ago was completed HERE, not elsewhere — its
# result may still be seconds away (measured 7-minute gap, sonichi/sutando#3009).
ARCHIVE_COMPLETION_GRACE_S = 1800.0


def _archived_task_file(tid: str):
    """The archived task file for tid, or None — flat and month-partitioned."""
    base = TASKS_DIR / "archive"
    flat = base / f"{tid}.txt"
    if flat.exists():
        return flat
    hits = sorted(base.glob(f"*/{tid}.txt"))
    return hits[-1] if hits else None


def _task_archived_recently(tid: str) -> bool:
    f = _archived_task_file(tid)
    if f is None:
        return False
    try:
        return (time.time() - f.stat().st_mtime) < ARCHIVE_COMPLETION_GRACE_S
    except OSError:
        return False


# ── orphan-result reconciler (sonichi/sutando#3009) ─────────────────────────
# Results whose tid left the in-flight ledger have no consumer.
ORPHAN_SWEEP_EVERY_S = 600.0
ORPHAN_GRACE_S = 600.0
# Beyond this, an automatic sweep must not replay into a live room.
ORPHAN_MAX_AGE_S = 86400.0
_last_orphan_sweep = 0.0
_orphan_quarantine_logged: set = set()


# Exactly what the writers emit after `{tid}`: ONE epoch, optionally tagged,
# optionally uniquified. A second `-\d+` would re-admit a longer id's entry.
_ARCHIVE_SUFFIX = re.compile(r"-\d+(?:-late-duplicate)?(?:\.\d+)?\.txt\Z")


def _delivered_copy_exists(tid: str) -> bool:
    """Both archive conventions: flat `<tid>-<ts>.txt` AND month-partitioned
    `YYYY-MM/<tid>.txt` (bare name) — a flat-only probe mis-routes real
    replies to re-delivery (peer-measured 4/50 on a live corpus)."""
    # The id boundary must be unambiguous: a bare `{tid}-*` glob also matches
    # a LONGER valid id's archive entry, so `task-a` reads as delivered.
    if any(_ARCHIVE_SUFFIX.fullmatch(p.name[len(tid):])
           for p in ARCHIVE_RESULTS_DIR.glob(f"{tid}-*.txt")):
        return True
    if (ARCHIVE_RESULTS_DIR / f"{tid}.txt").exists():   # flat bare: retired writer
        return True
    if any(ARCHIVE_RESULTS_DIR.glob(f"*/{tid}.txt")):
        return True
    return any(_ARCHIVE_SUFFIX.fullmatch(p.name[len(tid):])
               for p in ARCHIVE_RESULTS_DIR.glob(f"*/{tid}-*.txt"))


def _move_no_clobber(src, dst) -> bool:
    """Move src to dst or a uniquified sibling, never over an existing file:
    os.link fails EEXIST atomically, where exists-then-rename clobbers."""
    for candidate in (dst, dst.with_name(f"{dst.stem}.{time.time_ns()}{dst.suffix}")):
        try:
            os.link(str(src), str(candidate))
        except FileExistsError:
            continue
        except OSError:
            return False
        try:
            src.unlink()
        except OSError:
            pass
        return True
    return False


def _quarantine_orphan(rfile, tid: str, reason: str) -> bool:
    """Never replaces prior quarantined evidence, under collision."""
    UNDELIVERABLE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    dst = UNDELIVERABLE_RESULTS_DIR / f"{tid}.{reason}.{int(time.time())}.txt"
    return _move_no_clobber(rfile, dst)


def _reconcile_orphan_results(inflight: "set[str]") -> None:
    global _last_orphan_sweep
    now = time.time()
    if now - _last_orphan_sweep < ORPHAN_SWEEP_EVERY_S:
        return
    _last_orphan_sweep = now
    try:
        candidates = sorted(RESULTS_DIR.glob("task-*.txt"))
    except OSError:
        return
    for rfile in candidates:
        tid = rfile.stem
        if not _valid_local_tid(tid) or tid in inflight:
            continue
        try:
            age = now - rfile.stat().st_mtime
        except OSError:
            continue
        if age < ORPHAN_GRACE_S:
            continue                            # young: normal path may claim it
        if age > ORPHAN_MAX_AGE_S:
            # A minimum age alone lets an automatic pass replay an unbounded
            # historical backlog into live rooms; backfill must be deliberate.
            if _quarantine_orphan(rfile, tid, "too-old"):
                if tid not in _orphan_quarantine_logged:
                    _orphan_quarantine_logged.add(tid)
                    _log(f"orphan sweep: {tid} is {int(age)}s old (>{int(ORPHAN_MAX_AGE_S)}s) "
                         "— quarantined rather than replayed")
            continue
        # Delivered copy = double-write. NEVER re-deliver: the sweep would
        # post agent narration about having answered into the room.
        if _delivered_copy_exists(tid):
            ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            dst = ARCHIVE_RESULTS_DIR / f"{tid}-{int(now)}-late-duplicate.txt"
            if _move_no_clobber(rfile, dst):
                _log(f"orphan sweep: {tid} is a post-delivery duplicate — moved aside")
            continue
        # No task anywhere: nothing resolves a destination — quarantine,
        # never a labeled re-delivery (permanent sweep error otherwise).
        task = find_task_file(TASKS_DIR, tid) or _archived_task_file(tid)
        if task is None:
            if not _quarantine_orphan(rfile, tid, "no-task"):
                continue
            if tid not in _orphan_quarantine_logged:
                _orphan_quarantine_logged.add(tid)
                _log(f"orphan sweep: {tid} has no task file — quarantined")
            continue
        # Genuinely undelivered: ONE labeled attempt — at-least-once by
        # design; the label makes the rare duplicate self-explaining.
        raw = read_ready_result(rfile)
        if raw is None:
            continue
        delivery = _delivery_tid(tid)
        if delivery is None:
            continue                            # alias ledger unreadable: retry later
        # Recovery is still a delivery: the ordinary path's guard runs BEFORE
        # any marker is interpreted, so tier + suppression cannot be skipped.
        body, _withheld = _guarded_result_body(tid, raw)
        if body is None:
            _log(f"orphan sweep: result guard unavailable for {tid} — leaving for retry")
            continue
        if _withheld:
            _log(f"orphan sweep: withheld non-owner result for {tid}: {_withheld}")
        parsed = parse_markers(body)
        if [a for a in parsed.actions if a.kind == "attach"]:
            # Delivering without the files would silently drop them — park
            # for a human instead of composing a partial delivery.
            if _quarantine_orphan(rfile, tid, "has-attachments"):
                _log(f"orphan sweep: {tid} carries attachments — quarantined")
            continue
        skip = next((a for a in parsed.actions if a.kind == "skip"), None)
        if skip and skip.value == "deduped":
            # _dedup_plan reports or requeues when the holder delivered
            # nothing; posting here would retire the ask without that check.
            if _quarantine_orphan(rfile, tid, "deduped-orphan"):
                _log(f"orphan sweep: {tid} defers to its dedup holder — quarantined")
            continue
        if skip:
            # A suppression marker moves no data: the canonical close rides the
            # wire, not the sender's prose, and no_send gates delivery.
            labeled = _lease_close_body(skip)
        else:
            labeled = ("(recovered result — original delivery was lost)\n"
                       + parsed.body)
            _r = next((a for a in parsed.actions if a.kind == "redirect"), None)
            if _r:
                labeled = f"[channel: {_r.value}]\n{labeled}"
        # Same outcome owner as the live drain: a 2xx {"ok": false} is a
        # refusal, and an unconfirmed close must keep its retryable result.
        _btid = _broker_tid(delivery)
        if _deliver_result_payload(tid, _btid, labeled, no_send=bool(skip)):
            _archive_result(rfile, tid)
            _log(f"orphan sweep: recovered + delivered {tid}")
            continue
        _tries = _delivery_core().backend.attempts(_btid)
        if _tries >= MAX_TRANSIENT_ATTEMPTS:
            # Permanent disposition (lease gone or standing refusal): the
            # bounded-attempts ceiling replaces the raw 4xx probe the core hides.
            if _quarantine_orphan(rfile, tid, "undeliverable-after-retries"):
                _log(f"orphan sweep: {tid} unconfirmed after {_tries} attempts "
                     "— quarantined")
            continue
        _log(f"orphan sweep: {tid} close not confirmed (attempt {_tries}) — will retry")


# ── MC1 per-workspace singleton (dual-poller guard) ─────────────────────────
# Exactly one gateway-bridge may poll a given workspace's relay bearer. A second
_LOCK_ROLE = f"gateway-bridge{_INST_SUFFIX}"  # per-instance: dual-poller guard stays per-gateway
_LOCK_WS = _STATE.parent  # _STATE = <workspace>/state (injected) or ~/.ag2-sparrow/state


def _lock_on() -> bool:
    return os.environ.get("SUTANDO_BRIDGE_LOCK", "1") != "0"


def _release_singleton() -> None:
    if not _lock_on():
        return
    try:
        _ws_release(_LOCK_ROLE, _LOCK_WS)
    except Exception:
        pass


def _heartbeat_singleton() -> bool:
    """Refresh the poller lock. Returns False ONLY when we have definitively LOST
    ownership — a replacement reaped our lock after we were deemed stale (the
    stale-takeover race). The caller MUST stop polling on False, or the reaped
    process and the new owner both pull the same relay bearer (the dual-poll this
    slice closes). Fail-open on everything else (lock disabled / heartbeat error
    → True) so a lock bug never wedges task delivery."""
    if not _lock_on():
        return True
    try:
        return bool(_ws_heartbeat(_LOCK_ROLE, _LOCK_WS))
    except Exception:
        return True


def _acquire_singleton() -> bool:
    """True → we hold the poller lock (or it is disabled / errored → fail-open).
    False → a live bridge already owns this workspace and the caller must NOT poll."""
    if not _lock_on():
        return True
    try:
        r = _ws_acquire(_LOCK_ROLE, _LOCK_WS)
    except Exception as e:  # fail-open — never wedge task delivery on a lock bug
        _log(f"singleton: acquire error ({e}) — proceeding without lock")
        return True
    if r.status == "deferred":
        h = r.holder or {}
        _log(f"singleton: another live gateway-bridge owns this workspace "
             f"(host={h.get('host')} pid={h.get('pid')}) — exiting to avoid dual-poll")
        return False
    atexit.register(_release_singleton)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, lambda *_a: sys.exit(0))
        except Exception:
            pass  # non-main-thread or platform without the signal — atexit still covers exit
    _log(f"singleton: acquired workspace poller lock ({r.status})")
    return True


def _react_sender(timeout: int = 10):
    """The 👀 receipt's room-verb call. Lives here because the room-verb
    endpoint surface is frozen to this adapter edge, not to sparrow modules."""
    def _react(room_id, message_id, key) -> None:
        # safe="" — the default safe="/" would split a room id containing "/"
        # across path segments and misroute the react.
        safe_room = urllib.parse.quote(str(room_id), safe="")
        _req("POST", f"/v1/rooms/{safe_room}/react",
             {"event_id": message_id, "key": key}, timeout=timeout)
    return _react


def _maybe_start_event_channel() -> None:
    """AWP P0: start the persistent Workspace-Event channel in its OWN daemon
    thread, ISOLATED from task delivery. Opt-in (SPARROW_EVENTS truthy) and
    fully guarded — any startup failure is logged and swallowed so it can NEVER
    affect task polling. Off by default = zero change to existing deployments;
    the task loop below is untouched whether this runs or not."""
    global _EVENT_CHANNEL
    if str(os.environ.get("SPARROW_EVENTS", "")).strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        from .event_inbox import EventInbox
        from .event_channel import EventChannel
        inbox = EventInbox(str(_STATE / "event-inbox.db"))
        ch = EventChannel(inbox, URL, _AUTH_HEADERS, log=_log,
                          auth_retry=bool(TOKEN_FILE))
        threading.Thread(target=ch.run, name="sparrow-event-channel", daemon=True).start()
        _EVENT_CHANNEL = ch
        # P1: drain the inbox into the Core's attention (taskify → tasks/) on a
        # timer, in ITS OWN daemon thread, fully guarded — task delivery unaffected.
        from .event_consumer import EventConsumer, TaskifyHandler
        handler = TaskifyHandler(str(TASKS_DIR), os.environ.get("AGENT_MXID"), log=_log)
        # Human-action bridge (v1 steps 2+3): when an owner + room are configured,
        # route the owner's answers to pending actions BEFORE taskify sees them,
        poster = None
        ha_owner = os.environ.get("SPARROW_HA_OWNER")
        ha_room = os.environ.get("SPARROW_HA_ROOM")
        if ha_owner:
            from .human_action import ActionStore, CardPoster, DecisionHandler, HandlerChain
            store = ActionStore(str(_STATE / "human-actions"))
            handler = HandlerChain([DecisionHandler(store, ha_owner, log=_log), handler])
            if ha_room:
                poster = CardPoster(store, URL, _AUTH_HEADERS,
                                    ha_room, log=_log,
                                    include_a2ui=os.environ.get("SPARROW_HA_A2UI", "")
                                    .strip().lower() in ("1", "true", "yes", "on"))
        # 👀 receipt: OPT-IN because it scopes by room_id alone, so default-on
        # would react in shared rooms. Wrapped OUTERMOST, chain-transparent.
        if (str(os.environ.get("SPARROW_OBSERVE_REACT", "")).strip().lower()
                in ("1", "true", "yes", "on")):
            mxid = os.environ.get("AGENT_MXID") or os.environ.get("AGENT_ID")
            if mxid:
                from .default_observer import ReactObserverHandler
                handler = ReactObserverHandler(handler, _react_sender(), mxid,
                                               log=_log)
            else:
                _log("react-observer: AGENT_MXID/AGENT_ID unset — observed-receipt off")
        consumer = EventConsumer(inbox, handler)

        def _drain_loop():
            while True:
                try:
                    consumer.drain()
                    if poster is not None:
                        poster.sweep()
                except Exception as e:  # noqa: BLE001 — drain must never break anything
                    _log(f"event drain error (isolated): {e}")
                time.sleep(2.0)
        threading.Thread(target=_drain_loop, name="sparrow-event-drain", daemon=True).start()
        _log("event channel + consumer started (SPARROW_EVENTS enabled) — isolated "
             "daemon threads, task delivery unaffected")
    except Exception as e:  # noqa: BLE001 — event startup must NEVER break tasks
        _log(f"event channel start failed (task delivery unaffected): {e}")


def _poll_timeout_is_empty(last_ok: float, now: float,
                           grace: float = POLL_TIMEOUT_GRACE_S) -> bool:
    """Whether a long-poll read timeout should be read as `{"tasks": []}`.

    False once no poll has succeeded within `grace`, so a wedged relay still
    reaches the outage path instead of looping quietly forever.
    """
    return (now - last_ok) <= grace


def main() -> None:
    if not TOKEN:
        sys.exit("FATAL: set REMOTE_TASK_TOKEN (the onboarding string, or a bare secret with REMOTE_TASK_URL).")
    if not URL:
        # A token that starts with a URL scheme but yielded no URL means the
        # url|secret separator was swallowed (e.g. a %7C survived decoding, or a
        _hint = (" — the token carries a gateway URL but the url|secret separator "
                 "looks missing/corrupted" if TOKEN[:4].lower() == "http" else "")
        sys.exit("FATAL: no gateway URL — set REMOTE_TASK_URL, or use the combined "
                 f"'https://<gateway>|<secret>' onboarding token{_hint}.")
    if not _acquire_singleton():
        return  # a live bridge already polls this workspace — exit cleanly (no dual-poll)
    inflight: set[str] = _load_inflight()
    _recover_orphan_proactive()
    abandoned_suspects: set[str] = set()
    _log(f"starting — gateway={URL} provider={PROVIDER} tasks={TASKS_DIR} "
         f"(restored {len(inflight)} in-flight)")
    # Always name where the diagnostics live: after an incident this line is the
    # trailhead (a bare-launched bridge under default dirs writes status to
    _log(f"launched_via={_LAUNCHED_VIA} status={GATEWAY_STATUS_FILE}")
    if _LAUNCHED_VIA == "bare":
        _log(f"running unsupervised — output also logged to {_LOG_FILE}; "
             f"prefer launching through startup.sh for full diagnostics")
    backoff = 1
    last_poll_ok = time.time()
    _emit_gateway_status(False, error="starting — not yet connected")
    _maybe_start_event_channel()  # additive/opt-in/isolated — never blocks the task loop
    _results_watcher = _start_results_watcher()
    _outbound_thread = _start_outbound_worker(inflight)
    while True:
        try:
            if not _heartbeat_singleton():
                # Lost the poller lock (reaped after being deemed stale). Stop
                # polling immediately so we don't dual-poll the relay bearer with
                _log("singleton: lost workspace poller lock (reaped after stale takeover) "
                     "— exiting to avoid dual-poll")
                _OUTBOUND_STOP.set(); _OUTBOUND_WAKE.set()
                _outbound_thread.join(timeout=OUTBOUND_SCAN_S * 3 + 5)
                if _results_watcher is not None:
                    _results_watcher.join(timeout=5)
                return
            _post_heartbeat(inflight)
            _retry_pending_publications()
            _retry_review_card_resolutions()
            _retry_review_control_results()
            try:
                resp = _req("GET", f"/v1/tasks?wait={POLL_WAIT}", timeout=POLL_WAIT + 10)
                last_poll_ok = time.time()
            except (TimeoutError, socket.timeout):
                # Read timeout only (URLError takes the outage path below).
                # socket.timeout only aliases TimeoutError on 3.10+, not 3.9.
                if not _poll_timeout_is_empty(last_poll_ok, time.time()):
                    raise
                resp = {"tasks": []}
            added = False
            pending_ack = []
            for task in resp.get("tasks", []):
                if _handle_review_decision(task):
                    _queue_review_control_result(task)
                    _retry_review_control_results()
                    _log(f"consumed private review decision {task.get('id')}")
                    continue
                tid = _write_task(task)
                if tid:
                    if tid not in inflight:
                        inflight.add(tid)
                        added = True
                    pending_ack.append(tid)
            if added:
                _save_inflight(inflight)
            # Ack only after both the task file and local in-flight state are
            # durable, so a crash after ack does not strand the eventual result.
            for tid in pending_ack:
                _post_task_ack(tid)
            if added:
                wake_outbound()          # a fresh task often precedes its ack round-trip
            abandoned_suspects = _reconcile_abandoned(inflight, abandoned_suspects)
            _reconcile_orphan_results(inflight)
            _post_heartbeat(inflight)
            backoff = 1  # healthy round-trip → reset backoff
            _emit_gateway_status(True)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                if _recover_auth(e.code):
                    backoff = 1
                    continue
                _emit_gateway_status(False, error=f"auth rejected HTTP {e.code}")
                sys.exit(f"FATAL: gateway auth rejected (HTTP {e.code}) — check REMOTE_TASK_TOKEN.")
            _log(f"poll HTTP {e.code} — backing off {backoff}s")
            _emit_gateway_status(False, error=f"HTTP {e.code}", backoff_s=backoff)
            time.sleep(backoff); backoff = min(backoff * 2, 60)
        except (urllib.error.URLError, TimeoutError) as e:
            _log(f"poll network error: {e} — backing off {backoff}s")
            _emit_gateway_status(False, error=f"network: {e}", backoff_s=backoff)
            time.sleep(backoff); backoff = min(backoff * 2, 60)
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            _log(f"unexpected: {e} — backing off {backoff}s")
            _emit_gateway_status(False, error=f"unexpected: {e}", backoff_s=backoff)
            time.sleep(backoff); backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
