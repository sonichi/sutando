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
  GET  {REMOTE_TASK_URL}/v1/tasks?wait=<sec>&capabilities=voice-outcome-v1
       → 200 {"tasks": [ {<task fields...>}, ... ]}   (long-poll; [] on timeout)
  POST {REMOTE_TASK_URL}/v1/tasks/<task-id>/ack
       → body {"id": "<task-id>"}  → 200 on accepted
  POST {REMOTE_TASK_URL}/v1/results
       → body {"id": "<task-id>", "body": "<result text>",
               "capabilities": ["voice-outcome-v1"], "dm_only": false}  → 200 on accepted
  POST {REMOTE_TASK_URL}/v1/heartbeat
       → body {"client": "...", "inflight": N, ...}  → 200 on accepted
  GET  {REMOTE_TASK_URL}/v1/setup
       → 200 the agent's own agent.setup.steps result (when a heartbeat announces a new revision)

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
  REMOTE_TASK_ON_TASK   optional wake command, run once per newly queued task
                        file (argv split with shlex, no shell; file path appended;
                        the task's bytes on stdin, routing headers as SPARROW_* env)
  REMOTE_HEALTH_COMMAND optional command printing the core's health row (JSON) for
                        the heartbeat; see health_command.py
  REMOTE_OUTBOUND_SCAN_S outbound worker scan period seconds (default 1.0)

Stdlib only (urllib) — no new dependencies.
"""

from __future__ import annotations

import atexit
import glob
import base64
import contextlib
import hashlib
import hmac
import io
import http.client
import json
import os
import stat
import subprocess
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

from .dotspace import NO_FETCH as _NO_FETCH, dotspace_for as _dotspace_for

# Prefer IPv4 for connections to any DotSpace gateway/relay (e.g. chat.ag2.space): they
# publish AAAA records, but some hosts have IPv6 black-holed at the network
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


# Resolver fallback: the OS cache can hold a stuck negative answer for the relay
# host while the configured nameserver still resolves it — ask that one directly.
_FALLBACK_TTL_S = float(os.environ.get("REMOTE_GATEWAY_DNS_FALLBACK_TTL") or "300")
_FALLBACK_QUERY_TIMEOUT_S = 2.0
_RESOLV_CONF = "/etc/resolv.conf"
_fallback_cache: dict = {}  # host -> (expires_at_monotonic, [ip, ...])
_fallback_lock = threading.Lock()


def _system_nameservers(path=None):
    """IPv4 nameservers from resolv.conf in file order; [] when unreadable.
    IPv6 entries are skipped: the query socket is AF_INET only."""
    out = []
    try:
        with open(path or _RESOLV_CONF) as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) >= 2 and parts[0] == "nameserver" and ":" not in parts[1]:
                    out.append(parts[1])
    except OSError:
        pass
    return out


def _skip_name(data, i):
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def _encode_qname(host):
    out = b""
    for label in host.rstrip(".").split("."):
        b = label.encode("idna")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _parse_a_records(data, qid, qname=None):
    """A-record IPs from one DNS response; [] unless id, QR bit, RCODE and
    the echoed question all match what was asked."""
    import struct
    try:
        rid, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
        if rid != qid or not flags & 0x8000 or flags & 0x000F:
            return []
        i = 12
        for _ in range(qd):
            end = _skip_name(data, i)
            if qname is not None and data[i:end].lower() != qname.lower():
                return []
            i = end + 4
        ips = []
        for _ in range(an):
            i = _skip_name(data, i)
            rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[i:i + 10])
            i += 10
            if rtype == 1 and rdlen == 4:
                ips.append(socket.inet_ntoa(data[i:i + 4]))
            i += rdlen
        return ips
    except (IndexError, struct.error, OSError):
        return []


def _dns_a_query(host, nameserver, timeout=None, port=53):
    """One UDP A query (RFC 1035), stdlib only; [] on any failure."""
    import secrets
    import struct
    qid = secrets.randbelow(0xFFFF) + 1
    qname = _encode_qname(host)
    q = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(_FALLBACK_QUERY_TIMEOUT_S if timeout is None else timeout)
        # connect(): the kernel then drops datagrams from any other peer.
        s.connect((nameserver, port))
        s.send(q)
        data = s.recv(4096)
    except OSError:
        return []
    finally:
        s.close()
    return _parse_a_records(data, qid, qname)


def _fallback_resolve(host):
    """IPs for host via the system nameservers, cached; [] when none answers."""
    now = time.monotonic()
    with _fallback_lock:
        hit = _fallback_cache.get(host)
        if hit and hit[0] > now:
            return list(hit[1])
    for ns in _system_nameservers():
        ips = _dns_a_query(host, ns)
        if ips:
            with _fallback_lock:
                _fallback_cache[host] = (now + _FALLBACK_TTL_S, ips)
            _log(f"system resolver failed for {host}; nameserver {ns} answered "
                 f"{ips[0]} — using it for {_FALLBACK_TTL_S:.0f}s")
            return ips
    return []


def _is_ip_literal(host):
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, host)
            return True
        except (OSError, TypeError, ValueError):
            pass
    return False


def _prefer_v4_for(host) -> bool:
    """Whether `host` (str or bytes) is a DotSpace host whose instance asks for IPv4 first."""
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    d = _dotspace_for(name, fetch=_NO_FETCH)
    return bool(d.operator and d.prefer_ipv4)


def _getaddrinfo_prefer_v4(host, *args, **kwargs):
    try:
        infos = _resolve_bounded(host, *args, **kwargs)
    except socket.gaierror as err:
        ips = []
        if isinstance(host, str) and host and not _is_ip_literal(host):
            try:
                ips = _fallback_resolve(host)
            except Exception:  # noqa: BLE001 — fallback must never mask the gaierror
                ips = []
        if not ips:
            raise err
        infos = []
        for ip in ips:
            # A literal IP never touches DNS: this only shapes the tuples.
            infos.extend(_orig_getaddrinfo(ip, *args, **kwargs))
    if _PREFER_V4 and host and _prefer_v4_for(host):
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
from . import (agent_identity, diagnostics, gateway_credential_io, gateway_credential_sources,
               gateway_credentials, gateway_instance, local_task_protocol, routing_migrations,
               setup_fetch)
from .bounded_file_read import read_private, read_regular, read_regular_text
from .result_markers import parse_markers, render_skill_prelude
from .result_markers import neutralize_markers
from . import undelivered_quarantine
from .proactive_routing import (PROACTIVE_OUTBOX_DIRNAME, about_task, about_task_ts, proactive_filename,
                                proactive_item_id)
from .team_guardrail import ag2space_tier_lines, owner_mention_lines
from . import team_result_guard
from . import health_command
from . import __version__ as _PACKAGE_VERSION
from . import delivery_destination as _delivery_destination
from .outbox import dispatch_unsettled, record_provider_ref, released_by_operator
from .outbox import RetrySchedule, delivered_body_differs, read_item, source_digest
from .outbox import list_items, requeue_item_with_epoch, RequeueOutcome
from .proactive_recovery import claim_owner_may_be_alive as _pid_alive
from .send_failure_policy import MAX_TRANSIENT_ATTEMPTS
from .delivery_core import (WITHDRAWN, DeliveryCore, DesignAClaimBackend, DrainStatus,
                            ProviderRefused, RetryPolicy)
from .delivery_core import DeliveryOutcome as CoreDeliveryOutcome
from .delivery_core import is_refused_for_good
from .delivery_core.provider_ag2space import AG2SpaceRoomMessageProvider, room_response_refused
from .delivery_core.provider_ag2space import (AG2SpaceResultProvider, RESULT_THREAD_ASK,
                                              RESULT_THREAD_FIELD)
from .result_ready import (identity_of, read_ready_result, read_ready_result_with_identity,
                           ready_body_of, ResultIdentity)
from . import result_disposal as disposal
from .result_publish import (is_staged_for as _is_staged_for, publish_staged as _publish_staged_whole,
                             stage_text as _stage_text)
from .dedup_recovery import plan_dedup_recovery, classify_holder_delivery
from . import pool_record
from . import task_hold
from .send_allowlist import is_path_sendable
from .workspace_lock import acquire as _ws_acquire, heartbeat as _ws_heartbeat, release as _ws_release
from .workspace_lock import DEFAULT_STALE_SECONDS as _WS_STALE_SECONDS
from .workspace_lock import retain as _ws_retain
from .workspace_lock import _host_label as _stable_host_label
from .workspace_lock import proc_start as _ws_proc_start

TASKS_DIR = _task_dir()
# Written by THIS bridge on replay, not by an agent — the guard must not
# mistake its own dedup control for collaborator output.
GATEWAY_REDELIVERY_RESULT = "[no-send] gateway redelivery of already-handled task\n"


RESULTS_DIR = _result_dir()
_STATE = _state_dir()
_WITHHELD_TASK_OUTPUT: "dict[tuple, tuple]" = {}
_WITHHELD_DM_CACHE = _STATE / "withheld-review-dm.json"
_WITHHELD_CONTROL_DIR = _STATE / "withheld-review-control-results"
_GATEWAY_OWNER_DM_HINT = ""
ARCHIVE_RESULTS_DIR = RESULTS_DIR / "archive"
# tids THIS process redelivered. Not a file: the collaborator path has full
# workspace write, so any sidecar it can create is provenance it can forge.
_REDELIVERED: "set[str]" = set()

# Terminal resting place for proactive nudges that can never be delivered.
# results/undelivered/ is the repo-wide quarantine convention — health-check's
UNDELIVERABLE_RESULTS_DIR = RESULTS_DIR / "undelivered"
# Named-instance support (multi-gateway): one core may run SEVERAL bridge
# processes, each pointed at a different gateway (e.g. prod + dev homeservers)
_INSTANCE_RE = gateway_instance.INSTANCE_RE

GATEWAY_INSTANCE = (os.environ.get("GATEWAY_INSTANCE") or "").strip()
if GATEWAY_INSTANCE and not _INSTANCE_RE.fullmatch(GATEWAY_INSTANCE):
    sys.exit("FATAL: GATEWAY_INSTANCE must match "
             f"{_INSTANCE_RE.pattern} (ASCII only; got {GATEWAY_INSTANCE!r})")
_INST_SUFFIX = gateway_instance.suffix(GATEWAY_INSTANCE)


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
    return gateway_instance.local_tid(broker_tid, GATEWAY_INSTANCE)


def _broker_tid(local_tid: str) -> str:
    """Reverse of `_local_tid` — the id the GATEWAY knows this task by."""
    return gateway_instance.broker_tid(local_tid, GATEWAY_INSTANCE)


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
# Sidecar map {WIRE task id → {"mode": "task-media", "thread_root": …}}, committed
# BEFORE the task is published: nothing else survives a restart to route the upload.
TASK_MEDIA_FILE = _STATE / f"remote-task-media{_INST_SUFFIX}.json"
# Acks the gateway has not confirmed yet, with the durability each one may claim.
# The outbound worker retries them; a closed lease retires the record.
PENDING_ACK_FILE = _STATE / f"remote-task-acks{_INST_SUFFIX}.json"
# A served `lease_id` longer than this is not echoed: the ack goes without one.
LEASE_ID_MAX = 256
# Re-asked task id -> the id the broker is waiting on. A dedup re-ask gets a
# fresh local id, but the delivery it answers is still the original one.
DEDUP_ALIAS_FILE = _STATE / f"remote-dedup-alias{_INST_SUFFIX}.json"
# Broker id -> the local id it was published under, for each task served with `ack_first`.
# A file of its own: an older bridge prunes the media sidecar when it settles a task.
ACK_FIRST_FILE = _STATE / f"remote-task-ack-first{_INST_SUFFIX}.json"
# Local id -> an `ack_first` task staged before its ack and not yet published, so a
# crash in between publishes an accepted task once and discards a refused one.
ACK_FIRST_MANIFEST_FILE = _STATE / f"remote-task-ack-first-manifest{_INST_SUFFIX}.json"
# Declared in the heartbeat and carried on every poll and result; it stands for `dm_only`,
# `consolidated_into` and acknowledging an `ack_first` task before publishing it.
VOICE_OUTCOME_CONTRACT = "voice-outcome-v1"
_POLL_PROOF = f"&capabilities={VOICE_OUTCOME_CONTRACT}"
# The only source the broker serves with `ack_first`.
_VOICE_ASK_SOURCE = "matrixrtc-voice"
# The lane's `ask_sutando` correlation ids, under which a relay ask is served.
_RELAY_ASK_PREFIX = "ask-"
# Liveness of the gateway *connection* itself (distinct from _post_heartbeat,
# which pings the broker). A local supervisor (e.g. the desktop app's
GATEWAY_STATUS_FILE = _STATE / f"gateway-status{_INST_SUFFIX}.json"
# The heartbeat reply's pointers to the served setup; read by adapters, never by Sparrow.
SETUP_HINT_FILE = _STATE / f"setup-hint{_INST_SUFFIX}.json"
SETUP_HINT_FIELDS = ("setup_revision", "min_version", "target_version")
SETUP_REVISION_WITHHELD = "setup_revision_withheld"  # read by adapters/hatch/runtime_text.py

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


# The credential sources' precedence and parsing have one owner; this module keeps only its I/O.
_unquote_env = gateway_credential_sources.unquote


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
_parse_onboarding_token = gateway_credentials.parse_onboarding_token


# Which channels/<dir>/ this instance reads (.env fallback + access.json).
# Env-only — the .env file can't name its own directory. Default preserves the
CHANNEL_DIR = gateway_credential_sources.channel_dir(os.environ)


def _read_env_file(path: str):
    """A credential file's (text, refusal reason): bounded_file_read.read_regular."""
    return read_regular(path)


def _read_token_file_text(path: str):
    """A token file's (text, refusal reason). A seat's own token file holds its credential alone, so a seat reads
    it read_private (this user's, 0600, no link); a host, whose named file may be its channel .env, read_regular."""
    owner = gateway_credential_sources
    seat_file = owner.is_seat(os.environ) and path == owner.named_token_file(os.environ)
    return read_private(path) if path and seat_file else read_regular(path)


def _channel_env_candidates():
    """The readable channel .env files in precedence order, as [(path, vals)]
    (gateway_credential_sources.env_files decides which and in what order)."""
    return gateway_credential_sources.env_files(os.environ, _read_env_file)


def _config_from_channel_env(key: str) -> str:
    """First candidate CARRYING `key`, else "" (gateway_credential_sources.config_from_files)."""
    return gateway_credential_sources.config_from_files(_channel_env_candidates(), key)


def _announce_file_token(path: str, vals) -> None:
    # Name the exact file: which .env supplied the token is load-bearing for diagnosis.
    print(f"[remote-gateway-bridge] token not in env; loaded from {path}", file=sys.stderr, flush=True)
    # The bridge derives its media marker tag from os.environ at import, so carry the file's.
    _mm = vals.get("REMOTE_MEDIA_MARKER")
    if _mm and not os.environ.get("REMOTE_MEDIA_MARKER"):
        os.environ["REMOTE_MEDIA_MARKER"] = _mm


def _token_from_ag2space_env():
    """The file tier, for a launcher that did not export the token: (token, url, path) from the
    first channel .env carrying one (gateway_credential_sources.token_from_files), or ("", "", "").
    The desktop-spawned core is the case that matters: it reaches the bridge only through
    AG2_DEVICE_ENV. The path is the durable source the auth-recovery path re-reads."""
    tok, url, path, vals = gateway_credential_sources.token_from_files(_channel_env_candidates())
    if tok:
        _announce_file_token(path, vals)
    return tok, url, path


def _vault_lookup(vault_get=None):
    """The vault read for the ag2space token, parity with the channel bridges: the shared
    core `channel_token.token_from_vault`, total-failure-safe and never surfacing the value. The
    package ships standalone, so the monorepo `src/` may be absent: then there is no vault tier
    (None) rather than a crash at startup. `vault_get` is injectable for hermetic tests."""
    return gateway_credential_io.vault_lookup(_monorepo_src("channel_token.py"), vault_get)


def _announce_vault_token() -> None:
    # Name the source, never the value.
    print("[remote-gateway-bridge] token not in env or .env; loaded from vault", file=sys.stderr, flush=True)


def _token_from_vault_ag2space(vault_get=None):
    """The vault tier (gateway_credential_sources.token_from_vault): the current name, then the
    legacy AG2_REMOTE_TOKEN alias; '' on any failure."""
    lookup = _vault_lookup(vault_get)
    tok = gateway_credential_sources.token_from_vault(lookup)[0] if lookup else ""
    if tok:
        _announce_vault_token()
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
        withheld_review_artifact,
        withheld_review_path,
    )
    return (classify_result_for_tier, materialize_withheld_verdict,
            resolve_access_tier, sensitive_data_filter_enabled,
            withheld_review_path, journal_suppressed_result,
            is_suppression_only, is_guarded_tier, withheld_review_artifact)


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


def _stage_durable(path: Path, text: str) -> "Path | None":
    """Stage `text` beside `path` through the shared publisher; None when nothing
    reached the disk. Staging is separate from publishing because a sidecar the
    published file refers to has to commit in between."""
    try:
        return _stage_text(path, text)
    except Exception as exc:  # noqa: BLE001
        _log(f"durable stage failed for {path.name} ({diagnostics.describe(exc)})")
        return None


def _publish_staged(tmp: Path, path: Path) -> bool:
    """Rename a staged file into place through the shared publisher."""
    try:
        _publish_staged_whole(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"durable publish failed for {path.name} ({diagnostics.describe(exc)})")
        return False


def _durable_write(path: Path, text: str) -> bool:
    """write temp → fsync file → rename → fsync parent directory. False when the
    bytes did not commit, so the caller can withhold whatever they gate."""
    tmp = _stage_durable(path, text)
    if tmp is None:
        return False
    if _publish_staged(tmp, path):
        return True
    tmp.unlink(missing_ok=True)
    return False


def _read_review_json(path: Path) -> "dict | None":
    """A review record read only through the guard's strict walk; None when it cannot be."""
    try:
        record = team_result_guard.read_json_strict(path)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _read_private_json(path: Path) -> "dict | None":
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _gateway_owner() -> str:
    global _GATEWAY_OWNER_DM_HINT
    _GATEWAY_OWNER_DM_HINT = ""  # a lookup that never reaches a row leaves no reading behind for the caller
    identity = _reenroll_identity()
    answer = _req("GET", "/v1/agents")
    agents = answer.get("agents") if isinstance(answer, dict) else None
    if not isinstance(agents, list):
        return ""
    row = next((row for row in agents
                if isinstance(row, dict) and row.get("id") == identity), None)
    owner = str((row or {}).get("owner") or "")
    hint = str((row or {}).get("owner_dm_room") or "")
    _GATEWAY_OWNER_DM_HINT = gateway_instance.owner_room(hint, identity)
    return gateway_instance.owner_mxid(owner, identity)


def _owner_dm_is_private(room: str, owner: str) -> "bool | None":
    """Whether only the agent and its owner are in `room`; None when the gateway cannot say."""
    agent = _reenroll_identity()
    if not (room and owner and agent):
        return None
    try:
        answer = _req("POST", "/v1/room", {"op": "members", "room_id": room}, timeout=10)
    except Exception:  # noqa: BLE001 - unreadable is undecided, never private
        return None
    if not isinstance(answer, dict) or answer.get("error") or not isinstance(answer.get("members"), list):
        return None
    members = {r.get("user_id") for r in answer["members"] if isinstance(r, dict)}
    return bool(members) and members <= {agent, owner}


def _owner_review_dm(owner: str) -> str:
    if _GATEWAY_OWNER_DM_HINT and _owner_dm_is_private(_GATEWAY_OWNER_DM_HINT, owner) is True:
        _atomic_private_json(_WITHHELD_DM_CACHE,
                             {"owner": owner, "room_id": _GATEWAY_OWNER_DM_HINT, "private": True})
        return _GATEWAY_OWNER_DM_HINT
    cached = _read_private_json(_WITHHELD_DM_CACHE) or {}
    room = gateway_instance.owner_room(cached.get("room_id"), _reenroll_identity())
    if cached.get("owner") == owner and room and cached.get("private") is True:
        return room  # an unmarked cache predates the membership check and may name a shared room
    answer = _req("POST", "/v1/room", {
        "op": "create", "invite": [owner], "is_direct": True,
        "name": "Private result reviews",
        "topic": "Owner-only review of results withheld from shared rooms.",
    }, timeout=_ROOM_POST_TIMEOUT_S)
    room = gateway_instance.owner_room((answer or {}).get("room_id"), _reenroll_identity())
    if not room:
        raise RuntimeError("gateway did not return a private review room")
    _atomic_private_json(_WITHHELD_DM_CACHE, {"owner": owner, "room_id": room, "private": True})
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
    record = _read_review_json(path)
    if not record:
        return False
    if record.get("status") != "pending_dm":
        return True
    version = team_result_guard.record_version(record)
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
        answer = _req("POST", "/v1/room", payload, timeout=_ROOM_POST_TIMEOUT_S)
        if not isinstance(answer, dict) or not answer.get("ok"):
            raise RuntimeError(f"private review DM part {index}/{len(messages)} was not accepted")
    record.update({"status": "awaiting_owner", "owner": owner, "dm_room_id": room,
                   "dm_event_id": str(answer.get("event_id") or ""),
                   "dm_sent_at": time.time()})
    if team_result_guard.update_record(path, record, expect=version):
        return True
    # Another writer moved the record on while the DM was in flight; its state stands.
    current = _read_review_json(path) or {}
    _log(f"withheld review {path.stem}: routed copy is stale; keeping {current.get('status')}")
    return current.get("status") not in (None, "pending_dm")


_REVIEW_DECISION_RE = re.compile(
    r"^(yes|no)(?:\s+(wr_[0-9a-f]{16}))?[.!]?$", re.IGNORECASE)


class _ReviewsUnreadable(Exception):
    """The reviews could not be inspected, which is not the same as no review matching."""

    def __init__(self, detail, record: str = "", reason: str = ""):
        super().__init__(detail)
        self.record = record  # the review file to inspect, relative to the workspace
        self.reason = reason  # plain words for the owner; never a host path


def _pending_review_records() -> list[tuple[Path, dict]]:
    """Live reviews that own their id; the guard freezes any that conflict with
    their id's reservation or archived decision, and the owner is told."""
    directory = _STATE / team_result_guard.WITHHELD_RESULT_DIR
    for path in _review_paths(directory, False):
        try:
            if team_result_guard.proven_superseded_record(path):
                _defers_to_archive(path)
        except OSError:
            continue
    try:
        actionable, frozen = team_result_guard.actionable_records(directory)
    except OSError:
        return []
    for path in frozen:
        _log(f"withheld review {path.name}: conflicts with its id's decision or reservation; "
             f"frozen in {path.parent.name}/ for the owner, never acted on")
    listed = []
    for path, record in actionable:
        if not (path.name.startswith("wr_") and record.get("status") in (
                "awaiting_owner", "publish_pending", "kept_private", "published", "publish_failed")):
            continue
        if not team_result_guard.decision_is_durable(record):
            # The durability barrier: a decision acts only once it is durably on disk.
            if team_result_guard.establish_decision(path):
                record = _read_review_json(path) or record
        listed.append((path, record))
    return listed


def _archive_resolved_review(path: Path, record: dict) -> bool:
    if record.get("status") not in ("kept_private", "published", "publish_failed"):
        return False
    if record.get("card_resolution_pending"):
        return False
    return team_result_guard.archive_record(path)


def _decision_text(task: dict) -> str:
    text = str(task.get("task") or "").strip()
    # Button replies can follow the broker's quoted context envelope; only the
    # text after it is the decision.
    text = local_task_protocol.reply_action_text(text)
    text = re.sub(r"^\[AG2Space\s+[^\]]+\]\s*", "", text, count=1)
    return text.strip()


def _review_paths(directory: Path, strict: bool) -> list[Path]:
    try:
        return team_result_guard.entries(directory, "wr_*.json")
    except OSError as exc:
        if strict:
            raise _ReviewsUnreadable(exc) from exc
        return []


def _check_reviews_readable(directory: Path) -> None:
    # The guard's one read-only preflight, under the ledger lock so no move races it: any entry
    # a reader would refuse makes the reply unknown, never "no matching review".
    if not team_result_guard.present(directory, stat.S_IFDIR):
        return
    with team_result_guard.withheld_decision_lock(directory):  # the lock also refuses an odd issued/
        problems = team_result_guard.namespace_problems(directory)
    if problems:
        path, reason = problems[0]
        workspace = _STATE.parent
        name = str(path.relative_to(workspace)) if path.is_relative_to(workspace) else path.name
        raise _ReviewsUnreadable(f"{name}: {reason}", name, reason)


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
    _check_reviews_readable(_STATE / team_result_guard.WITHHELD_RESULT_DIR)
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
    if not candidates:
        candidates = _archived_review_candidates(room, task, explicit_id, reply_to)
    return (*candidates[0], answer) if len(candidates) == 1 else None


def _archived_review_candidates(room: str, task: dict, explicit_id, reply_to: str) -> list:
    """Decided reviews a later owner reply names, by id or by replying to their card;
    the reply is answered with the archived outcome and acts on nothing."""
    archive = _STATE / team_result_guard.WITHHELD_RESULT_DIR / "archive"
    found = []
    for archived in _review_paths(archive, strict=True):
        record = _read_review_json(archived) or {}  # the preflight already refused any it cannot read
        if (room != str(record.get("dm_room_id") or "")
                or str(task.get("user_id") or "") != str(record.get("owner") or "")):
            continue
        if (explicit_id == record.get("review_id")
                or (not explicit_id and reply_to and reply_to == str(record.get("dm_event_id") or ""))):
            found.append((archived.parent.parent / archived.name, record))
    return found


def _released_review_body(raw: str) -> str:
    """Room text for an owner-released result. Markers are stripped, never executed:
    the release posts to the original room only and uploads nothing."""
    parsed = parse_markers(raw)
    dropped = sum(1 for action in parsed.actions if action.kind == "attach")
    if not dropped:
        return parsed.body
    note = f"({dropped} attachment{'' if dropped == 1 else 's'} not published)"
    return f"{parsed.body}\n\n{note}" if parsed.body else note


_REFUSED_REVIEW_PUBLICATIONS: set = set()


def _stale_review_write(path: Path, step: str) -> None:
    current = _read_review_json(path) or {}
    _log(f"withheld review {path.stem}: {step} not recorded, the record changed meanwhile "
         f"(now {current.get('status')}); the next beat retries from it")


# Outbox park reasons that mean the release was never delivered; any other park may have landed.
_DEFINITE_PUBLISH_FAILURES = ("permanent-refusal", "max-attempts", "retry-window-exhausted")


def _post_room_message(room: str, body: str, dedupe_key: str, thread_root: "str | None" = None,
                       deadline: "float | None" = None) -> dict:
    """The room-message send for an outbox provider: one /v1/room message POST, done by
    `deadline` (time.monotonic()) when one is given."""
    return _req("POST", "/v1/room", {"op": "message", "room_id": room, "body": body,
                                     "dedupe_key": dedupe_key,
                                     **({"thread_root": thread_root} if thread_root else {})},
                timeout=_ROOM_POST_TIMEOUT_S, **({"deadline": deadline} if deadline is not None else {}))


def _record_release_receipt(item_id: str, event_id: str) -> None:
    """The release's durable receipt, written to its review record before the outbox is
    told it was delivered; raises when it cannot be written."""
    review_id, _, claim = item_id.partition(".")
    path = _STATE / team_result_guard.WITHHELD_RESULT_DIR / f"{review_id}.json"
    if not team_result_guard.record_release_receipt(path, claim, event_id):
        raise OSError(f"release receipt for {review_id} not recorded")


def _withheld_outbox() -> DeliveryCore:
    """The outbox that publishes an owner-released body to its room, at most once."""
    return _delivery_core("withheld")


def _publish_review(path: Path, record: dict) -> bool:
    version = team_result_guard.record_version(record)
    claim = record.get("decision_claim_id")
    if not team_result_guard.withheld_claim_publishable(path, claim):
        if str(path) not in _REFUSED_REVIEW_PUBLICATIONS:
            _REFUSED_REVIEW_PUBLICATIONS.add(str(path))
            _log(f"withheld review {path.stem}: publication refused, no matching release "
                 "claim; the body stays private until the owner acts")
        return False
    context = record.get("context") or {}
    room = str(context.get("channel_id") or "")
    body = _released_review_body(str(record.get("withheld_body") or ""))
    if not room.startswith("!") or not body:
        return _record_publish_failure(path, record, version, "invalid origin/body")
    core, item_id = _withheld_outbox(), f"{record['review_id']}.{claim}"
    landed = _reread_record(path)
    if (landed.get("decision_claim_id") == claim
            and isinstance(landed.get("release_event_id"), str) and landed["release_event_id"]):
        # A recorded event id proves the release landed: settle, never send it again.
        _settle_landed_release(core, item_id, room)
        return _commit_landed(path, record, landed)
    payload = json.dumps({"room_id": room, "body": body}, sort_keys=True).encode("utf-8")
    core.backend.publish(item_id, payload)  # once per claim; the outbox owns every retry
    if (read_item(core.backend.root, item_id) or {}).get("status") not in core.backend.TERMINAL:
        core.deliver_one(item_id, payload)
    item = read_item(core.backend.root, item_id) or {}
    if item.get("status") == "DELIVERED":
        current = _reread_record(path)
        if current.get("status") == "published":
            return True
        event_id = current.get("release_event_id")
        if isinstance(event_id, str) and event_id:
            return _commit_landed(path, record, current)
        if f"no-receipt:{item_id}" not in _REFUSED_REVIEW_PUBLICATIONS:
            _REFUSED_REVIEW_PUBLICATIONS.add(f"no-receipt:{item_id}")
            _log(f"withheld review {path.stem}: the outbox marks the release delivered but no "
                 "event id was recorded; it is left pending for the owner, not marked published")
        return False
    if item.get("status") == "PARKED":
        if item.get("reason") in _DEFINITE_PUBLISH_FAILURES:
            return _record_publish_failure(path, record, version, item.get("reason"))
        if not record.get("publish_parked"):
            record.update({"publish_parked": str(item.get("reason")), "card_resolution_pending": True})
            if not team_result_guard.update_record(path, record, expect=version):
                _stale_review_write(path, "parked publication")
        if f"unknown:{item_id}" not in _REFUSED_REVIEW_PUBLICATIONS:
            _REFUSED_REVIEW_PUBLICATIONS.add(f"unknown:{item_id}")
            _log(f"withheld review {path.stem}: the publication may have landed but was not "
                 f"confirmed ({item.get('reason')}); it is not sent again and stays pending. "
                 f"Check the room first; to send it again (a new dedupe key, so it may post "
                 f"twice): {_requeue_command(core.backend.root, item_id)}")
    return False


def _commit_landed(path: Path, record: dict, current: dict) -> bool:
    committed = _commit_publication(path, current, team_result_guard.record_version(current),
                                    current["release_event_id"])
    if committed:
        record.clear()
        record.update(current)  # the caller continues from the committed version
    return committed


def _settle_landed_release(core: DeliveryCore, item_id: str, room: str) -> bool:
    """Drive an outbox item whose release a recorded event id proves to DELIVERED, through
    the outbox's own requeue, claim and complete; never sends. False while a claim holds it."""
    root = core.backend.root
    if (read_item(root, item_id) or {}).get("status") == "PARKED":
        requeue_item_with_epoch(root, item_id, operator=core.worker,
                                reason="release proven by a recorded event id")
    token = core.backend.claim(item_id, core.worker)
    if token is None:
        return (read_item(root, item_id) or {}).get("status") == "DELIVERED"
    return core.backend.complete(token, CoreDeliveryOutcome.CONFIRMED,
                                 provider=type(core.provider).__name__, destination=room)


def _settle_landed_releases() -> None:
    """Each beat: an outbox item not yet DELIVERED whose review holds the receipt for its claim
    is driven to DELIVERED until it is, never sent again. Reads every item of this outbox."""
    core = _withheld_outbox()
    directory = _STATE / team_result_guard.WITHHELD_RESULT_DIR
    for item in list_items(core.backend.root):
        item_id = str(item.get("item_id") or "")
        review_id, _, claim = item_id.partition(".")
        if item.get("status") == "DELIVERED" or not claim:
            continue
        record = _settlement_record(directory, review_id)
        if record is None:
            if f"unreadable:{review_id}" not in _REFUSED_REVIEW_PUBLICATIONS:
                _REFUSED_REVIEW_PUBLICATIONS.add(f"unreadable:{review_id}")
                _log(f"withheld review {review_id}: its record could not be read; its outbox "
                     "item is not settled until it can be")
            continue
        if (record.get("decision_claim_id") != claim
                or not isinstance(record.get("release_event_id"), str) or not record["release_event_id"]):
            continue
        room = str((record.get("context") or {}).get("channel_id") or "")
        try:
            if not _settle_landed_release(core, item_id, room) and f"settle:{item_id}" not in \
                    _REFUSED_REVIEW_PUBLICATIONS:
                _REFUSED_REVIEW_PUBLICATIONS.add(f"settle:{item_id}")
                _log(f"withheld review {review_id}: published; its outbox item is still claimed "
                     "and is settled once that claim can be taken")
        except Exception as exc:  # noqa: BLE001 — next beat retries
            _log(f"withheld review {review_id}: outbox settlement deferred ({diagnostics.describe(exc)})")


def _settlement_record(directory: Path, review_id: str) -> "dict | None":
    """The archived decision owns its review, so it is read first and a live copy never hides
    it; None (fail closed) when the record that would decide cannot be read."""
    for path in (directory / "archive" / f"{review_id}.json", directory / f"{review_id}.json"):
        try:
            record = team_result_guard.read_json_strict(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None
    return {}


def _requeue_command(root, item_id: str) -> str:
    return shlex.join(["python3", "src/outbox_cli.py", "--root", str(root), "requeue", item_id])


def _reread_record(path: Path) -> dict:
    """The live record, read again while it exists but does not read; {} once it is gone."""
    for _attempt in range(5):
        current = _read_review_json(path)
        if current is not None or not team_result_guard.present(path):
            return current or {}
    return {}


def _record_publish_failure(path: Path, record: dict, version: dict, reason) -> bool:
    record.update({"status": "publish_failed", "publish_error": str(reason),
                   "card_resolution_pending": True})
    if not team_result_guard.update_record(path, record, expect=version):
        _stale_review_write(path, "publication failure")
    return False


def _commit_publication(path: Path, record: dict, version: dict, event_id: str) -> bool:
    """Record a confirmed publication on the review; a record that moved meanwhile is left
    to the next beat, which commits it from the durable receipt and never sends again."""
    record.update({"status": "published", "published_at": time.time(),
                   "published_event_id": event_id, "card_resolution_pending": True})
    if team_result_guard.update_record(path, record, expect=version):
        return True
    _stale_review_write(path, "publication")
    return False


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
    elif record.get("publish_parked"):
        outcome = "False positive recorded, but publication could not be confirmed and requires attention."
    else:
        outcome = "False positive recorded; publication to the original room is pending."
    return f"**Private result review `{rid}` resolved**\n\n✓ {outcome}"


def _edit_review_card(record: dict) -> "dict | None":
    room = str(record.get("dm_room_id") or "")
    event_id = str(record.get("dm_event_id") or "")
    if not room.startswith("!") or not event_id.startswith("$"):
        return None
    answer = _req("POST", "/v1/room", {
        "op": "edit", "room_id": room, "event_id": event_id,
        "body": _resolved_review_body(record),
    }, timeout=_ROOM_POST_TIMEOUT_S)
    if (not isinstance(answer, dict) or room_response_refused(answer)
            or not (answer.get("ok") or answer.get("event_id"))):
        return None
    return answer


_RECONCILE_FAILURES: dict = {}
# A reconcile that keeps failing stops re-editing the card after this many tries per process.
_RECONCILE_GIVE_UP_AFTER = 3


def _defers_to_archive(path: Path) -> bool:
    """True when an archived decision owns this review: the live copy never acts. Only a
    proven archived outcome restores the card, then retires the copy; never raises."""
    try:
        archived = team_result_guard.archived_withheld_decision(path)
        if archived is None:
            return False
        if _RECONCILE_FAILURES.get(str(path), 0) >= _RECONCILE_GIVE_UP_AFTER:
            return True
        outcome = team_result_guard.archived_outcome(archived)
        if (outcome is not None
                and _edit_review_card(archived) is not None
                and team_result_guard.retire_superseded_record(path)):
            _log(f"withheld review {path.stem}: a live copy contradicted its archived "
                 f"{outcome} decision; card restored and copy retired")
            return True
        note = ("its archived decision stands" if outcome is not None else
                "its archived decision is not a recognised outcome; left for the owner")
    except Exception as exc:  # noqa: BLE001 — one record's fault must never stop the beat
        note = f"archive reconcile failed ({diagnostics.describe(exc)}); left for the owner"
        failures = _RECONCILE_FAILURES[str(path)] = _RECONCILE_FAILURES.get(str(path), 0) + 1
        if failures == _RECONCILE_GIVE_UP_AFTER:
            _log(f"withheld review {path.stem}: archive reconcile failed {failures} times; "
                 "no further card edits for it until the bridge restarts")
    if f"archived:{path}" not in _REFUSED_REVIEW_PUBLICATIONS:
        _REFUSED_REVIEW_PUBLICATIONS.add(f"archived:{path}")
        _log(f"withheld review {path.stem}: live copy left untouched; {note}")
    return True


def _resolve_review_card(path: Path, record: dict) -> bool:
    if not team_result_guard.decision_is_durable(record) or _defers_to_archive(path):
        return False
    version = team_result_guard.record_version(record)
    answer = _edit_review_card(record)
    if answer is None:
        return False
    record.update({"card_resolution_pending": False,
                   "card_resolved_at": time.time(),
                   "card_resolution_event_id": str(answer.get("event_id") or "")})
    if not team_result_guard.update_record(path, record, expect=version):
        _stale_review_write(path, "card resolution")
        return False
    _archive_resolved_review(path, record)
    return True


def _handle_hitl_action(task: dict):
    """An owner card click delivered on the task relay (its `hitl_action`, or a
    reply to the card whose text is an action label). Applied to the HITL
    store here so a click never becomes a chat task; the caller closes the
    task with a [no-send] control result. False = an ordinary message."""
    task_id = str(task.get("id") or "")
    # The control record exists only for a task already consumed here (either
    # click form, or a review decision); its form must not be re-derived.
    if task_id and _control_result_path(task_id).is_file():
        return "redelivered"
    local = _handle_claude_local(task)
    if local:
        return local
    if not isinstance(task.get("hitl_action"), dict) and not task.get("reply_to_event"):
        return False
    owner = os.environ.get("SPARROW_HA_OWNER") or ""
    handler = _hitl_reply_handler(owner, log=_log) if owner else None
    if handler is None:
        return False  # no owner or no store on this host: the click stays an ordinary task
    try:
        out = handler.offer_task(task)
    except Exception as e:  # noqa: BLE001 — a broken click must not stall the poll loop
        _log(f"hitl: task-relay click {task_id} not applied: {diagnostics.describe(e)}")
        return False
    if out == "rejected":
        return "rejected:" + (getattr(handler, "last_reason", "") or "stale or malformed")
    if out == "applied" and getattr(handler, "last_turn", False):
        # Recorded, and the requirement asked for a turn: the same relay task goes on to
        # the core (idempotent by id) with a header saying it is a click, not an order.
        task["hitl_click"] = "true"
        return False
    if out == "ignored" and getattr(handler, "last_branch", None) == "fallback":
        # A non-owner typed a label as a reply: that is a message, not a click
        # to decline; it must reach _write_task like any other text.
        return False
    # A non-owner's real click stays consumed on purpose: a card press is not
    # prose, so it closes [no-send] and is only logged, never a chat task.
    return out or False


def _claim_review_decision(task: dict, path: Path, updates: dict) -> "dict | None":
    # The first durable decision is final; a later or racing reply is closed and logged.
    try:
        claimed = team_result_guard.claim_withheld_decision(path, updates)
        if claimed is None:
            _explain_unclaimed(task, path)
    except Exception as exc:  # noqa: BLE001 — a failed claim is answered, never escapes the beat
        _log(f"withheld review {path.stem}: reply {task.get('id')} could not be processed ({diagnostics.describe(exc)})")
        _queue_review_control_result(task, body=(
            f"Your decision on review `{path.stem}` could not be processed right now. Nothing "
            "changed; please reply again."))
        claimed = None
    _queue_review_control_result(task)
    return claimed


def _explain_unclaimed(task: dict, path: Path) -> None:
    current = _read_review_json(path) or {}
    if team_result_guard.decision_lost(path):
        _log(f"withheld review {path.stem}: an earlier decision was lost; reply "
             f"{task.get('id')} not applied, left for the owner")
        _queue_review_control_result(task, body=_lost_decision_notice(path, current))
    elif current.get("status") not in (None, "awaiting_owner") \
            and not team_result_guard.decision_is_durable(current):
        _log(f"withheld review {path.stem}: decision recorded, it acts once it is durable; "
             f"reply {task.get('id')} not applied again")
    elif current.get("status") == "awaiting_owner":
        _log(f"withheld review {path.stem}: decision could not be saved; reply "
             f"{task.get('id')} not applied, the owner can answer again")
        _queue_review_control_result(task, body=(
            f"Your decision on review `{path.stem}` could not be saved. Nothing changed; "
            "please reply again."))
    else:
        _log(f"withheld review {path.stem}: already decided; reply {task.get('id')} ignored")


def _lost_decision_notice(path: Path, record: dict) -> str:
    """What the owner is told about a decision lost after its claim was issued: whether a
    publication was ever attempted, which only the outbox can prove, and how to unlock it."""
    claims = team_result_guard.issued_claims(path)
    root = _withheld_outbox().backend.root
    attempted = any(read_item(root, f"{record.get('review_id') or path.stem}.{c}") is not None
                    for c in claims)
    sent = ("Its publication may already have been sent; check the room before answering."
            if attempted else "Nothing was sent for it.")
    unlock = shlex.join(["rm", "--", *(str(path.parent / "claims" / f"{path.stem}.{c}")
                                       for c in claims)])
    return (f"An earlier decision on review `{path.stem}` was not saved, so this reply was not "
            f"applied. {sent} To answer again, remove its claim: {unlock}")


def _archived_reply(path: Path) -> bool:
    present = team_result_guard.present
    return not present(path) and present(path.parent / "archive" / path.name)


def _handle_review_decision(task: dict) -> bool:
    task_id = str(task.get("id") or "")
    if task_id and _control_result_path(task_id).is_file():
        return True
    try:
        matched = _match_review_decision(task)
        archived_reply = matched is not None and _archived_reply(matched[0])
    except (_ReviewsUnreadable, OSError) as exc:
        _log(f"withheld reviews: reply {task.get('id')} could not be checked ({diagnostics.describe(exc)})")
        record = getattr(exc, "record", "")
        _queue_review_control_result(task, body=(
            f"Your reply could not be checked: the review record `{record}` is not a readable "
            f"review ({exc.reason}). Nothing changed. Inspect or remove that file; then please "
            "reply again."
            if record else
            "Your reply could not be checked against your pending reviews right now. Nothing "
            "changed; please reply again."))
        return True
    if matched is None:
        return False
    path, record, answer = matched
    if archived_reply:
        _log(f"withheld review {path.stem}: already decided; reply {task.get('id')} "
             "answered with the archived outcome")
        outcome = team_result_guard.archived_outcome(record)
        _queue_review_control_result(task, body=_resolved_review_body(record) if outcome else (
            f"Review `{record.get('review_id') or path.stem}` was already decided, but its record "
            "is not a recognised outcome. Nothing was changed; it is left for you to check."))
        return True  # a later reply to a decided review: answered, never acted on
    if record.get("status") in ("kept_private", "published"):
        _queue_review_control_result(task)
        if record.get("card_resolution_pending"):
            _resolve_review_card(path, record)
        return True  # delivery retry of the same owner decision
    if answer == "yes":
        record = _claim_review_decision(task, path, {
            "status": "kept_private", "resolved_at": time.time(),
            "decision": "sensitive", "card_resolution_pending": True})
        if record is None:
            return True
        try:
            _resolve_review_card(path, record)
        except Exception as exc:  # noqa: BLE001 — durable pending state retries
            _log(f"withheld review {record.get('review_id')} card edit deferred: {diagnostics.describe(exc)}")
        return True
    # Persist release before the network call; pending retries use a stable
    # dedupe key so they cannot duplicate the disclosure.
    record = _claim_review_decision(task, path, {
        "status": "publish_pending", "resolved_at": time.time(),
        "decision": "false_positive", "card_resolution_pending": True})
    if record is None:
        return True
    try:
        _resolve_review_card(path, record)
        if _publish_review(path, record):
            _resolve_review_card(path, record)
    except Exception as exc:  # noqa: BLE001 — durable pending state retries
        _log(f"withheld review {record.get('review_id')} publish deferred: {diagnostics.describe(exc)}")
    return True


def _retry_withheld_reviews() -> None:
    """One poll beat's durable review retries, in order."""
    _retry_pending_publications()
    _retry_review_card_resolutions()
    _retry_review_control_results()


def _retry_pending_publications() -> None:
    for path, record in _pending_review_records():
        if record.get("status") != "publish_pending":
            continue
        try:
            if not _publish_review(path, record):
                _log(f"withheld review {record.get('review_id')} publish still pending")
        except Exception as exc:  # noqa: BLE001 — next poll retries
            _log(f"withheld review {record.get('review_id')} publish retry failed: {diagnostics.describe(exc)}")
    try:
        _settle_landed_releases()
    except Exception as exc:  # noqa: BLE001 — next poll retries
        _log(f"withheld release settlement deferred: {diagnostics.describe(exc)}")


def _retry_review_card_resolutions() -> None:
    for path, record in _pending_review_records():
        if _defers_to_archive(path):
            continue
        if not record.get("card_resolution_pending"):
            _archive_resolved_review(path, record)
            continue
        try:
            if not _resolve_review_card(path, record):
                _log(f"withheld review {record.get('review_id')} card edit still pending")
        except Exception as exc:  # noqa: BLE001 — next poll retries
            _log(f"withheld review {record.get('review_id')} card edit retry failed: {diagnostics.describe(exc)}")


def _control_result_path(task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)[:160]
    return _WITHHELD_CONTROL_DIR / f"{safe}.json"


def _queue_review_control_result(task: dict, body: str = "[no-send]") -> None:
    task_id = str(task.get("id") or "")
    if not task_id:
        return
    path = _control_result_path(task_id)
    try:
        if not path.is_file():
            _atomic_private_json(path, {"id": task_id, "body": body})
    except Exception as exc:  # noqa: BLE001 — the reply is redelivered and answered then
        _log(f"review control result {task_id} not saved: {diagnostics.describe(exc)}")


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
                "id": record["id"], "body": record.get("body") or "[no-send]",
                **_result_contract(False)})
            path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — next poll retries
            _log(f"review control result {record.get('id')} retry deferred: {diagnostics.describe(exc)}")


def _is_redelivery_control(body: str) -> bool:
    """Compare on stripped text: `read_ready_result` strips, the constant ends
    in a newline, so a raw `==` never matches a body that came off disk."""
    return (body or "").strip() == GATEWAY_REDELIVERY_RESULT.strip()


# The one marker verdict every delivery decision here reads on the guarded body; the
# suppression journal reads it too, so whatever the drain closes silently is recorded.
DELIVERY_SKIP_AFTER_CHANNEL = True


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
    # A verdict is about one body: a later, different result of the task is judged anew.
    cached = (tid, source_digest(body))
    if cached in _WITHHELD_TASK_OUTPUT:
        return _WITHHELD_TASK_OUTPUT[cached]
    try:
        (classify, materialize, resolve, filter_enabled, _review_path,
         journal_suppression, suppression_only, guarded_tier, review_artifact) = _team_guard_fns()
        from .chat_secret_filter import filter_chat_secrets
    except Exception as exc:
        return None, f"team_result_guard unavailable: {diagnostics.describe(exc)}"
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
    # 5G ⑤a-cap: a TEAM task whose sidecar says task-media may attach from the DELIVERY
    # task's `<results>/<id>/` (a dedup requeue carries the original's instruction).
    attach_roots: tuple = ()
    if tier == "team":
        _delivery_for_media = _delivery_tid(tid)
        _media_modes = _load_task_media() if _delivery_for_media is not None else None
        if (_media_modes is not None
                and (_media_modes.get(_broker_tid(_delivery_for_media)) or {}).get("mode")
                == "task-media"):
            attach_roots = (str(RESULTS_DIR / _delivery_for_media),)
    verdict = classify(
        body, tier, None, secret_filter=filter_chat_secrets,
        scan_sensitive_data=scan_sensitive_data, attach_roots=attach_roots)
    is_leak = verdict.kind == "leak"
    agent_id = _reenroll_identity()
    verdict = materialize(
        verdict, body, _STATE, tid, context=context, agent_id=agent_id,
        now=time.time())
    if guarded_tier(tier) and suppression_only(
            body, skip_after_channel=DELIVERY_SKIP_AFTER_CHANNEL):
        # Honouring a guarded close without recording it is the accountability
        # gap the guard used to answer with a refusal.
        verdict = journal_suppression(
            verdict, body, _STATE, tid, context=context, agent_id=agent_id,
            now=time.time())
    if is_leak:
        artifact = review_artifact(_STATE, tid, body)
        if not artifact.is_file():
            return None, verdict.reason
        try:
            if not _route_withheld_review(artifact):
                return None, f"{verdict.reason}; private owner review unavailable"
        except Exception as exc:  # noqa: BLE001 — retain result file for retry
            return None, f"{verdict.reason}; private owner review failed: {diagnostics.describe(exc)}"
    result = (verdict.body, verdict.reason)
    if verdict.reason is not None:
        _WITHHELD_TASK_OUTPUT[cached] = result
        if len(_WITHHELD_TASK_OUTPUT) > 512:
            _WITHHELD_TASK_OUTPUT.pop(next(iter(_WITHHELD_TASK_OUTPUT)))
    return result


_VAULT_INTERCEPT_FNS: "tuple | None" = None


def _monorepo_src(marker: str) -> str:
    """The monorepo `src/` that holds `marker`, walking up from this file;
    '' when sparrow runs standalone (pyproject install, no monorepo around it)."""
    return gateway_credential_io.monorepo_src(marker, __file__)


# `project()` assigns retry cadence to its caller and the worker drives this
# every ~1s. Skip-until-deadline, not sleep: other drains share this thread.
_HITL_BACKOFF = {"until": 0.0, "delay": 0.0}
_HITL_BACKOFF_MAX_S = 60.0


def _hitl_backoff_bump(log=print) -> None:
    d = min((_HITL_BACKOFF["delay"] or 0.5) * 2, _HITL_BACKOFF_MAX_S)
    _HITL_BACKOFF["delay"] = d
    _HITL_BACKOFF["until"] = time.monotonic() + d
    log(f"hitl: projection refused — backing off {d:g}s")


def _hitl_backoff_reset() -> None:
    _HITL_BACKOFF["delay"] = 0.0
    _HITL_BACKOFF["until"] = 0.0


def _hitl_modules():
    """(manager, projector, replies) modules: the wheel's own `ag2_sparrow.hitl` first, the monorepo
    `src/hitl` only when the vendored copy is absent, so a standalone install has them."""
    try:
        from .hitl import manager, projector, replies
        return manager, projector, replies
    except ImportError:
        src = _monorepo_src(os.path.join("hitl", "projector.py"))
        if not src:
            return None
        if src not in sys.path:
            sys.path.insert(0, src)
        from hitl import manager, projector, replies
        return manager, projector, replies


def _claude_local_store_root() -> Path:
    """The claude-local cards' own store root beside the default store: what an unmodified gateway
    never reads, so old main neither projects nor accepts them."""
    from .claude_local_card import store_root
    return store_root(_STATE)


def _project_claude_local(mods, room: str, log=print) -> list:
    """Project the claude-local root's cards into the owner DM (the room filled on the
    requirement under the store lock when the launcher could not know it)."""
    manager_mod, projector, _ = mods
    root = _claude_local_store_root()
    if not root.is_dir():
        return []
    store = manager_mod.HitlStore(root)
    manager = manager_mod.HitlManager(store, policy=None)
    with store.locked():
        for req in store.all():
            if not req.room and not req.terminal:
                req.room = room
                sub = req.subject.get("claude-local") if isinstance(req.subject, dict) else None
                if isinstance(sub, dict) and not sub.get("room"):
                    sub["room"] = room
                store.save(req, store.projection(req.id))
    if not projector.pending_ids(manager):
        return []
    return projector.project(manager, lambda payload: _req("POST", "/v1/room", payload, timeout=_ROOM_POST_TIMEOUT_S), room)


def _project_hitl(log=print) -> int:
    """Push every un-projected HITL requirement out as a card, or 0 when
    `src/hitl` is absent (standalone sparrow) — same optional tier as the
    reply handler, and the outbound half of the same card.

    The projector owns idempotency (a projection ledger per requirement), so
    calling this every pulse re-sends nothing; it is the driver that was
    missing, not the machinery.
    """
    if GATEWAY_INSTANCE:
        return 0  # only the primary gateway (launched without GATEWAY_INSTANCE) projects the owner's cards
    room = resolve_destination(OWNER_PRIVATE)  # a runtime prompt is the owner's, whatever room raised it
    if not room:
        _held("the runtime prompt card")
        return 0
    if time.monotonic() < _HITL_BACKOFF["until"]:
        return 0
    try:
        mods = _hitl_modules()
        if mods is None:
            return 0
        manager_mod, projector, _ = mods
        HitlManager, HitlStore, default_store = manager_mod.HitlManager, manager_mod.HitlStore, manager_mod.default_store
        pending_ids, project = projector.pending_ids, projector.project
    except Exception as e:  # noqa: BLE001 — an optional tier never breaks the pulse
        log(f"hitl: projector unavailable ({diagnostics.describe(e)}); cards will not be delivered")
        return 0
    workspace = _STATE.parent
    manager = HitlManager(HitlStore(default_store(workspace)))
    done, failed, raised = [], False, None
    # Each store gets its pass: a stuck card in one never holds back the other's.
    try:
        if pending_ids(manager):
            got = project(manager, lambda payload: _req("POST", "/v1/room", payload, timeout=_ROOM_POST_TIMEOUT_S), room)
            done, failed = list(got or []), not got
    except Exception as e:  # noqa: BLE001 — re-raised below, after the claude-local pass
        failed, raised = True, e
    try:
        done += _project_claude_local(mods, room, log)
    except Exception as e:  # noqa: BLE001 — re-raised below
        failed, raised = True, raised or e
    for req_id, event_id in done:
        log(f"hitl: projected {req_id} -> {event_id or 'no event id'}")
    if failed:
        _hitl_backoff_bump(log)  # a refusing or raising sender
        if raised is not None:
            raise raised
        return len(done)
    _hitl_backoff_reset()  # idle is not failure; a new card must not wait out a backoff
    return len(done)


def _hitl_reply_handler(owner_mxid: str, log=print):
    """Owner card-click handler for HITL cards, or None when neither the vendored `ag2_sparrow.hitl`
    nor `src/hitl` is around — the chain then simply lacks it, like the vault tier."""
    try:
        mods = _hitl_modules()
        if mods is None:
            return None
        manager_mod, _, replies = mods
        workspace = _STATE.parent
        return replies.HitlReplyHandler(manager_mod.HitlManager(manager_mod.HitlStore(manager_mod.default_store(workspace))),
                                        owner_mxid, workspace=workspace, log=log)
    except Exception as e:  # noqa: BLE001 — an optional handler must never break the chain
        log(f"hitl: reply handler unavailable ({diagnostics.describe(e)}); card clicks will not be applied")
        return None


CLAUDE_LOCAL_ID_PREFIX = "hitl_cl_"
REARM_PENDING = "applied:rearm-pending:"
_EXIT_TEXT = "/exit"


def _claude_local_accepter():
    """The bridge-owned claude-local accepter; (None, why) when it cannot be imported."""
    try:
        from . import claude_local_accepter as accepter
    except ImportError as e:  # part of this package; a missing piece is a packaging fault
        return None, f"claude-local accepter unavailable ({e})"
    return accepter, ""


def _owner_dm_text(task: dict) -> bool:
    """The owner's own top-level, unquoted, unforwarded, unrelayed message in the projected owner DM."""
    owner = os.environ.get("SPARROW_HA_OWNER") or ""
    uid = str(task.get("user_id") or "")
    if not owner or uid != owner:
        return False
    if _tier_for(uid, _normalized_tier(task.get("access_tier"))) != "owner":
        return False
    room = str(task.get("channel_id") or "")
    if not room or room != resolve_destination(OWNER_PRIVATE):
        return False
    if task.get("reply_to_event") or task.get("thread_root") or task.get("interaction_type") in ("forward", "forwarded"):
        return False
    text = str(task.get("task") or "")
    stripped = text.strip()
    return bool(stripped) and local_task_protocol.reply_action_text(text) == stripped and not stripped.startswith("[")


def _handle_claude_local(task: dict):
    """A click on a claude-local card (id namespaced hitl_cl_) or the owner's bare `/exit` in the owner
    DM: both go to the adapter's one accepter; the outcome closes the task [no-send]. False = not ours."""
    payload = task.get("hitl_action")
    reply = None
    if isinstance(payload, dict) and str(payload.get("hitl_id") or "").startswith(CLAUDE_LOCAL_ID_PREFIX):
        accepter, problem = _claude_local_accepter()
        if accepter is None:
            return "rejected:" + problem
        from .hitl.schema import ActionReply, MalformedActionError
        try:
            reply = ActionReply.from_wire(dict(payload, room=task.get("channel_id") or "", sender=task.get("user_id") or ""))
        except MalformedActionError as e:
            return f"rejected:{e}"
    elif str(task.get("task") or "").strip() == _EXIT_TEXT and _owner_dm_text(task):
        accepter, _ = _claude_local_accepter()
        if accepter is None or not _claude_local_store_root().is_dir():
            return False  # no claude-local card store here: the text stays a message
        store = accepter.card.HitlStore(_claude_local_store_root())
        reply = accepter.exit_text_reply(store, str(task.get("channel_id") or ""), str(task.get("user_id") or ""))
        if reply == 0:
            return False  # no open card: a bare /exit is an ordinary message
        if isinstance(reply, int):
            return f"rejected:{reply} stop cards are open; use the card's own button"
    else:
        return False
    owner = os.environ.get("SPARROW_HA_OWNER") or ""
    uid = str(task.get("user_id") or "")
    owner_ok = bool(owner) and uid == owner and _tier_for(uid, _normalized_tier(task.get("access_tier"))) == "owner"
    try:
        out = accepter.accept(reply, state_dir=_STATE, owner_ok=owner_ok, log=_log)
    except Exception as e:  # noqa: BLE001 — a broken click must not stall the poll loop
        return f"rejected:{e}"
    return out


def _hitl_control_body(hitl_out) -> str:
    """The control result closing a consumed click: a refusal or cancellation is the owner's to hear;
    applied and commit-unknown (consumed, reconciled by the launcher) send nothing."""
    out = str(hitl_out)
    if out.startswith(REARM_PENDING):
        return (f"Recorded: keep waiting. The new card could not be raised yet ({out[len(REARM_PENDING):]}); "
                "the launcher completes it at its next reconcile.")
    if out.startswith("rejected:"):
        return f"That click did not apply: the card had already moved ({out[9:]}). Please use the current card."
    if out.startswith("cancelled:"):
        return f"Cancelled: {out[10:]}"
    return "[no-send]"


def _click_owner_problem(owner: str, actor: str, attested_tier) -> str:
    """'' when a card click is the owner's, else why not. Fails closed: the actor must equal the configured
    owner exactly; an attested tier, when present, must also be owner; the local tier cap always applies."""
    if not owner:
        return "no owner is configured, so no card click is the owner's"
    if actor != owner:
        return "only the owner may act on this card"
    claimed = _normalized_tier(attested_tier) if attested_tier else "owner"
    if _tier_for(actor, claimed) != "owner":
        return "the click is not at owner tier"
    return ""


class _ClaudeLocalClicks:
    """An event-channel click on a claude-local card, claimed ahead of the default store's handler so
    it reaches the adapter's accepter and never the default store or taskify."""

    def __init__(self, owner: str, log=print):
        self.owner, self.log, self.last_path, self.last_outcome = owner, log, None, None

    @staticmethod
    def _payload(event: dict):
        content = event.get("content") or {}
        payload = content.get("space.ag2.hitl.action")
        return payload if isinstance(payload, dict) else None

    def claims(self, event: dict) -> bool:
        payload = self._payload(event)
        return (event.get("type") == "message.created" and payload is not None
                and str(payload.get("hitl_id") or "").startswith(CLAUDE_LOCAL_ID_PREFIX))

    def offer(self, event: dict) -> list:
        eid = str(event.get("event_id") or "")
        actor = str(event.get("actor_id") or "")
        accepter, problem = _claude_local_accepter()
        if accepter is None:
            self.last_outcome = "rejected:" + problem
        else:
            from .hitl.schema import ActionReply, MalformedActionError
            try:
                reply = ActionReply.from_wire(dict(self._payload(event), room=str(event.get("room_id") or ""),
                                                   sender=actor))
                problem = _click_owner_problem(self.owner, actor, event.get("access_tier"))
                self.last_outcome = (f"rejected:{problem}" if problem else
                                     accepter.accept(reply, state_dir=_STATE, log=self.log, owner_ok=True))
            except MalformedActionError as e:
                self.last_outcome = f"rejected:{e}"
            except Exception as e:  # noqa: BLE001 — a broken click must not stall the drain
                self.last_outcome = f"rejected:{e}"
        self.log(f"hitl: claude-local click {eid or '?'} by {actor or '?'}: {self.last_outcome}")
        return [eid] if eid else []


def _vault_intercept_fns():
    """Lazily locate the monorepo `src/vault_intercept.py` helpers; memoized.
    Returns (None, None) on failure so a caller can fall back to `_local_redact_vault_set`."""
    global _VAULT_INTERCEPT_FNS
    if _VAULT_INTERCEPT_FNS is not None:
        return _VAULT_INTERCEPT_FNS
    try:
        src = _monorepo_src("vault_intercept.py")
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


def _read_text(path: str) -> str:
    return read_regular_text(path) or ""


# The process's credentials: every candidate registered, the winners chosen by the one owner's table.
_SECRETS = diagnostics.REGISTRY  # the one registry every sink withholds from
# The vault is located only if no earlier source holds a token.
_FOUND = gateway_credential_sources.discover(
    os.environ, _channel_env_candidates(), read_token_file=_read_token_file_text,
    vault_lookup=lambda key: (_vault_lookup() or (lambda _k: ""))(key), registry=_SECRETS)
for _note in _FOUND.notes + ((_FOUND.fatal,) if _FOUND.fatal else ()):
    print(f"[remote-gateway-bridge] {_note}", file=sys.stderr, flush=True)
for _old, _new in _FOUND.deprecated:
    if _old not in _warned_legacy:
        _warned_legacy.add(_old)
        print(f"[remote-gateway-bridge] {_old} is deprecated — rename to {_new} in your .env",
              file=sys.stderr, flush=True)
if _FOUND.token_source == "file":
    _announce_file_token(_FOUND.source_file, _FOUND.source_file_values)
elif _FOUND.token_source == "token_file":
    print(f"[remote-gateway-bridge] token loaded from REMOTE_TASK_TOKEN_FILE {_FOUND.token_file}",
          file=sys.stderr, flush=True)
elif _FOUND.token_source == "vault":
    _announce_vault_token()
_RAW, TOKEN, URL = _FOUND.raw, _FOUND.token, _FOUND.url
# Every credential this process holds is withheld from logs and status in any rendering.
diagnostics.register_gateway_url(URL)
diagnostics.register_secret(TOKEN)
PROVIDER = os.environ.get("REMOTE_TASK_PROVIDER") or "remote"
POLL_WAIT = int(os.environ.get("REMOTE_TASK_POLL_WAIT") or "25")
ON_TASK_CMD = os.environ.get("REMOTE_TASK_ON_TASK") or ""
# A read timeout on the long poll is indistinguishable from the documented
# `200 {"tasks": []}` hold-window expiry, so it is only an outage once no poll
POLL_TIMEOUT_GRACE_S = 3 * (POLL_WAIT + 10)
# Backoff alone caps at 60s and never gives up, so a relay that stays down
# leaves a live process polling into the void. 0 disables the exit.
POLL_STALL_EXIT_S = float(os.environ.get("REMOTE_TASK_POLL_STALL_EXIT_S") or "600")
# Exiting only helps where something restarts us. startup-runtime.sh launches the
# primary and every named lane as a bare `&`, so the owner must be declared here.
POLL_STALL_RESTART_OWNER = (os.environ.get("SUTANDO_BRIDGE_RESTART_OWNER") or "").strip()
# EX_UNAVAILABLE: the wrapper relaunches on it without an owner alert, so an outage
# does not queue a "bridge restarted" message every stall period.
POLL_STALL_EXIT_CODE = 69
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
# Host-injected post-claim room gate ((original path, room) -> bool), consulted
# on the CLAIMED body of a `proactive-result-*` naming a room; None allows all.
PROACTIVE_ROOM_GATE: Callable[[Path, str], bool] | None = None
# Host-injected (old_room, replacement_room) -> None, owed once per upgrade of the owner
# DM to a room this agent is not in; raising is a failure and is retried, bounded.
OWNER_DM_UPGRADED_NOTIFY: Callable[[str, str], None] | None = None
# Host-injected task_id -> veto reason or None, asked right before a message about that task
# is sent; None (standalone default) checks only the task's own result.
PROACTIVE_DISPATCH_GATE: Callable[[str], "str | None"] | None = None
# Host-injected (task_id, item_id) -> veto reason or None, asked for the same message with its item id.
PROACTIVE_ACK_GATE: Callable[[str, str], "str | None"] | None = None
# Host-injected () -> bool, asked once right after the poller lease is taken: False drops
# PROACTIVE_ROOM, so a bridge spawned before another became primary never carries it too.
PROACTIVE_ROOM_KEEP: Callable[[], bool] | None = None
PROACTIVE_ROUTE_GATE: Callable[[str, str, "str | None"], "str | None"] | None = None
# Host-injected task -> reason or None, asked before a fresh task is queued: a reason settles it
# as a redelivery instead. None (standalone default) queues every fresh task.
TASK_INTAKE_GATE: Callable[[dict], "str | None"] | None = None
# Routing state belongs to the gateway (owner 2026-09-07): the agent row's owner_dm_room is read at
# connect and on a slow cadence and kept while offline; the pinned room is bootstrap, never authority.
_ROUTING: dict = {"owner_dm": "", "persisted": "", "persisted_private": False, "identity": "",
                  "gateway": "", "next": 0.0, "held_logged": 0.0}
ROUTING_REFRESH_S = 300.0
ROUTING_RETRY_S = 30.0
OWNER_PRIVATE = "owner_private"
CURRENT_ROOM = "current_room"
SELECTED_MEMBERS = "selected_members"
SYSTEM = "system"


def _routing_file() -> Path:
    return _STATE / f"owner-routing{_INST_SUFFIX}.json"


def _routing_record(identity: str, gateway_url: str, owner_dm: str, *,
                    private: bool = False) -> "dict | None":
    """The owner-routing record as persisted: the redacted URL and the keyed binding of the full
    one, never the full URL. None when no binding can be made, so nothing unbound is written.
    `private` only for a room whose membership was checked; a migrated legacy record is not."""
    binding = gateway_instance.binding_digest(identity, gateway_url, create=True)
    if not binding:
        return None
    record = {"identity": identity, "gateway": _redact_url(gateway_url), "binding": binding,
              "owner_dm": gateway_instance.owner_room(owner_dm, identity)}
    return {**record, "private": True} if private else record


def _migrate_routing() -> "routing_migrations.Result":
    """The routing state's migrations, on every load: no latch, so what disk still holds (a legacy
    record, a legacy temp still inside its age bound) is handled when it is there."""
    return routing_migrations.migrate_owner_routing(
        _routing_file(), record_for=_routing_record, write=_durable_write,
        log=lambda line: _log_backoff(("owner-routing migration", line), line))


def _load_routing() -> None:
    """Migrate, then rebuild the reading from the disk record, on every load. A gateway reading
    the writer has not committed yet (owner_dm != persisted) is kept until its write commits."""
    _migrate_routing()
    current = (_reenroll_identity(), URL)
    if not _ROUTING["identity"] and not _ROUTING["gateway"]:
        _ROUTING["identity"], _ROUTING["gateway"] = current  # a reading set before any binding adopts this one
    elif (_ROUTING["identity"], _ROUTING["gateway"]) != current:  # re-enrolled: the old reading is not ours
        _ROUTING.update(owner_dm="", persisted="", persisted_private=False,
                        identity=current[0], gateway=current[1])
    try:
        d = json.loads(_routing_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    # another agent's reading, or this agent's reading from another gateway, authorizes nothing
    bound = isinstance(d, dict) and d.get("identity") == current[0] and bool(d.get("binding")) \
        and hmac.compare_digest(str(d["binding"]).encode("utf-8"),
                                gateway_instance.binding_digest(current[0], URL).encode("utf-8"))
    on_disk = gateway_instance.owner_room(d.get("owner_dm"), current[0]) if bound else ""
    # unchecked or since found shared: never adopted, and left unequal so the writer overwrites it
    disk = on_disk if d.get("private") is True and on_disk != _ROUTING.get("shared") else ""
    # A reading that came from disk follows the disk; one the writer has not committed yet stays.
    if not _ROUTING["owner_dm"] or not _uncommitted():
        _ROUTING["owner_dm"] = disk
    _ROUTING["persisted"] = on_disk
    _ROUTING["persisted_private"] = d.get("private") is True


def _uncommitted() -> bool:
    """The reading differs from the record, or names a room the record has not marked private."""
    return _ROUTING["owner_dm"] != _ROUTING["persisted"] or (
        bool(_ROUTING["owner_dm"]) and not _ROUTING["persisted_private"])


def refresh_routing(force: bool = False) -> None:
    """Pull the agent's routing state from the gateway. A failed refresh books a short retry, not the
    window; an answer without a DM (no row, no field) keeps the last reading, which lives on disk.
    A room is the owner DM only while its members are the agent and its owner; a shared room clears it."""
    reenrolled = bool(_ROUTING["identity"] or _ROUTING["gateway"]) and (
        (_reenroll_identity(), URL) != (_ROUTING["identity"], _ROUTING["gateway"]))
    _load_routing()
    force = force or reenrolled
    now = time.time()
    if not force and now < _ROUTING["next"]:
        return
    try:
        owner = _gateway_owner()
    except Exception:  # noqa: BLE001 - offline: the last known owner DM stands
        _ROUTING["next"] = now + ROUTING_RETRY_S
        return
    candidate = _GATEWAY_OWNER_DM_HINT or _ROUTING["owner_dm"]
    private = _owner_dm_is_private(candidate, owner) if candidate else None
    if private is False:
        if now - _ROUTING.get("shared_logged", 0.0) >= ROUTING_REFRESH_S:
            _ROUTING["shared_logged"] = now
            _log(f"owner_dm_room {candidate} has members besides the agent and its owner: not an owner DM")
        _ROUTING["owner_dm"], _ROUTING["shared"] = "", candidate
    elif private is True:
        _ROUTING["owner_dm"] = candidate
        if _ROUTING.get("shared") == candidate:
            _ROUTING["shared"] = ""
    elif candidate != _ROUTING["owner_dm"]:  # undecided: a new room is not adopted, the checked one stands
        _ROUTING["next"] = now + ROUTING_RETRY_S
        return
    if _uncommitted():  # until durably published, a cleared reading too
        record = _routing_record(_reenroll_identity(), URL, _ROUTING["owner_dm"], private=True)
        if record is None or not _durable_write(_routing_file(), json.dumps(record)):
            _ROUTING["next"] = now + ROUTING_RETRY_S  # in memory it holds; the write is retried soon
            return
        _ROUTING["persisted"], _ROUTING["persisted_private"] = _ROUTING["owner_dm"], True
    _ROUTING["next"] = now + (ROUTING_REFRESH_S if _ROUTING["owner_dm"] else ROUTING_RETRY_S)


def _held(what: str) -> None:
    """An owner-private message with no owner DM reading is held, and the log says so once per window."""
    now = time.time()
    if now - _ROUTING["held_logged"] >= ROUTING_REFRESH_S:
        _ROUTING["held_logged"] = now
        _log(f"holding {what}: no owner DM reading from the gateway yet (never the pinned room)")


def resolve_destination(audience: str, *, room_id: str | None = None,
                        recipients: list | None = None) -> str | list[str]:
    """The one place an outbound room is chosen. OWNER_PRIVATE is the gateway's owner DM, the last
    reading kept identity-bound on disk across restarts and outages; with no reading it is "" and the
    caller holds the message, never the pinned room. CURRENT_ROOM is the triggering room;
    SELECTED_MEMBERS are explicit recipients (the one list-valued audience); SYSTEM is the
    configured destination. Eligibility is the instance, never the pin: only the primary (launched
    without GATEWAY_INSTANCE) owns unaddressed nudges and projects cards."""
    if audience == OWNER_PRIVATE:
        refresh_routing()
        return _ROUTING["owner_dm"]
    if audience == CURRENT_ROOM:
        return room_id or ""
    if audience == SELECTED_MEMBERS:
        return list(recipients or [])
    if audience == SYSTEM:
        return PROACTIVE_ROOM or ""
    raise ValueError(f"unknown audience {audience!r}")


def proactive_room() -> str:
    """Owner-directed by default: a proactive message or an owner-private card goes to the owner."""
    return resolve_destination(OWNER_PRIVATE)

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
# The ONE auth-header dict shared with long-lived consumers (event channel,
# card poster). They must hold this dict BY REFERENCE (no copy) so a token
_AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
OUTBOUND_SCAN_S = float(os.environ.get("REMOTE_OUTBOUND_SCAN_S") or "1.0")
# Outbound worker: decouples outbound progress from inbound long-poll
# progress; delivery machinery untouched — owns ONLY lifecycle/scheduling.
_OUTBOUND_WAKE = threading.Event()
_OUTBOUND_STOP = threading.Event()
_INFLIGHT_MUTEX = threading.RLock()
# True when the in-flight restore FAILED rather than finding no file. Both cases
# yield an empty set, but only the second one means "nothing is in flight".
_INFLIGHT_DEGRADED = False
# Same hazard as the in-flight set: these ledgers are read-modify-written by the
# poll loop and the outbound worker, so each load->mutate->write runs under a lock.
_TASK_MEDIA_MUTEX = threading.RLock()
_PENDING_ACK_MUTEX = threading.RLock()
_ACK_FIRST_MUTEX = threading.RLock()


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
            _log(f"outbound worker: results drain error (isolated): {diagnostics.describe(e)}")
        try:
            _post_proactive()
        except Exception as e:  # noqa: BLE001
            _log(f"outbound worker: proactive drain error (isolated): {diagnostics.describe(e)}")
        try:
            _project_hitl(log=_log)
        except Exception as e:  # noqa: BLE001
            _log(f"outbound worker: hitl projection error (isolated): {diagnostics.describe(e)}")
        try:
            _retry_pending_acks(inflight)
        except Exception as e:  # noqa: BLE001
            _log(f"outbound worker: pending-ack retry error (isolated): {diagnostics.describe(e)}")
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
                _log(f"results watcher degraded (scan remains the floor): {diagnostics.describe(e)}")
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
# A relay without /v1/heartbeat (404/405) is retried on a doubling backoff up to this cap,
# so a broker that deploys or restores the route is picked up without a bridge restart.
HEARTBEAT_UNSUPPORTED_MAX_BACKOFF = 1800.0
# When the gateway lacks /v1/tasks/<id>/ack it returns 404/405; we back off
# instead of hammering it — but only for this cooldown, then retry. A permanent
ACK_UNSUPPORTED_COOLDOWN = int(os.environ.get("REMOTE_ACK_RETRY_COOLDOWN") or "300")
_ack_disabled_until = 0.0   # 0 = enabled; else epoch until which acks are skipped
# Auth-rejection recovery: when the gateway rejects the bearer (401/403 — the
# key was revoked or expired), the historical behavior is an immediate FATAL
TOKEN_FILE = _FOUND.token_file
# A seat that cannot start says why in its status, then waits this long before exiting, so a manager that
# respawns on any exit (launchd KeepAlive, systemd Restart=always) retries it about once a minute, not hot.
SEAT_FATAL_BACKOFF_S = 60
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
    per-host identity enrolment wrote to state/auth/ag2space.json; a launcher's
    pin (agent_identity.PINNED_KEY) leaves only its AGENT_MXID. Re-read per
    call, so an identity that appears mid-episode takes effect without a restart."""
    return agent_identity.resolve(_STATE, _config_from_channel_env)


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
        if not agent_id and os.environ.get(agent_identity.PINNED_KEY):
            _log("reenroll: this bridge's launcher pinned no agent identity, so "
                 "no file or binding supplies one; save the agent's MXID with the "
                 "launcher and RESTART it")
        elif not agent_id:
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
        with _gateway_call():
            req = urllib.request.Request(
                _provision_base() + "/connect/reenroll",
                data=json.dumps({"agent_id": agent_id, "bearer": TOKEN}).encode(),
                # The prod edge 403s urllib's default UA — same contract as _req().
                headers={"Content-Type": "application/json",
                         "User-Agent": "sutando-gateway-client/1.0"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.loads(r.read() or b"{}")
        code = diagnostics.code_field(body.get("approval_code"), URL) or ""
        if body.get("pending") and code:
            _reenroll_state["code"] = code
            _reenroll_state["claimed_at"] = int(time.time())
            _reenroll_state.pop("refused", None)
            _log("RELINK PENDING — this agent's server-side registration was "
                 f"lost. RELINK CODE: {code} — the owner approves by DMing the "
                 f"concierge: relink approve {code}")
        else:
            _log("reenroll: claim not parked (the answer carries no usable approval code)")
    except urllib.error.HTTPError as e:
        raw = _http_error_body(e)
        try:
            err = json.loads(raw).get("error")
        except Exception:  # noqa: BLE001 — a non-JSON body still records the status
            err = None
        # The body is the gateway's free text: only an error name that is a safe token leaves here.
        err = diagnostics.code_field(err, URL)
        _reenroll_state["refused"] = {"status": e.code, "error": err, "at": int(time.time())}
        _log(f"reenroll: claim refused HTTP {e.code}" + (f" ({err})" if err else ""))
    except Exception as e:  # noqa: BLE001 — recovery must never crash the loop
        _log(f"reenroll: claim failed: {diagnostics.describe(e)}")


def _reenroll_clear(recovered: bool = False) -> None:
    """End the episode; recovered=True (probe-success path only) leaves the
    explicit terminal — disappearance alone must never read as success."""
    was_pending = bool(_reenroll_state.get("code"))
    prior_attempt = _reenroll_state.get("last_attempt_at")
    _reenroll_state.update({"last_attempt_at": None, "code": None, "claimed_at": None})
    _reenroll_state.pop("refused", None)
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
_heartbeat_retry_at = 0.0  # 0 = enabled; else epoch before which heartbeats are skipped
_heartbeat_backoff = 0.0
_last_heartbeat_at = 0.0
_last_core_health: "dict | None" = None

_TASK_FIELDS = ("id", "timestamp", "session_scope",
                # Both ahead of "task": the safe parser stops at the body, so a
                # field written below it is invisible to every task-last reader.
                "requested_worker", "priority",
                # A card click already recorded in the HITL store, passed on for the turn it
                # causes: the core answers [no-send] and lets its Stop hook do the work.
                "hitl_click",
                # Which worker-picker button was pressed. Above "task" for the same
                # reason: worker_picker_commands reads it with the safe parser.
                "picker_command", "picker_args",
                # Also above "task": these carry the picker's authorization, and a
                # task-last reader cannot see a field written below the body.
                "source", "channel_id",
                # Declares the task-mid shape (one-line body, writer-owned trailer) so a
                # verified reader may trust the lines below task:. Above it: a body cannot claim it.
                "task_layout",
                # The broker's word that the message mentioned the owner, not this agent.
                # Above "task" so the strict parser reads it and a body cannot claim it.
                "owner_mentioned",
                "task",
                # Context enrichment (AG2 broker writer side): human room/sender
                # names + reply reference. Serialized only when the gateway sends
                "room_name", "sender_name", "reply_to_event", "reply_to_me", "reply_to_sender",
                "addressed_to",
                # Ingress only: the backend inherits the route by task id, so a
                # reply echoing these back could name a thread it was not asked in.
                "thread_root", "source_room_id",
                # Room-membership context (gateway writer side, same contract):
                # a capped one-line mxid list + the true joined total, and the
                # broker's own dm|room verdict so a skill need not count members.
                "room_members", "room_member_count", "channel_kind",
                "source_message_id", "user_id", "interaction_type",
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
LOCAL_TIER = local_task_protocol.canonical_access_tier(
    _env_compat("REMOTE_TASK_TIER", "AG2_REMOTE_TIER") or "owner")
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
    tier = local_task_protocol.canonical_access_tier(value)
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
            t = local_task_protocol.canonical_access_tier(tier)
            if isinstance(who, str) and t in _TIER_RANK:
                tm[who.strip()] = t
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

HS_MEDIA_TOKEN = _FOUND.media_token
diagnostics.register_secret(HS_MEDIA_TOKEN)
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
    practice and a stray newline only ever indicates an injection attempt.

    Load-bearing: this producer does no body defanging, so the flatten is the
    only thing stopping a field from forging a registered header line.

    Folds on str.splitlines()' own set, not on CR/LF: readers split with
    splitlines(), so any narrower set leaves a separator that is one line to
    the writer and two to the reader."""
    return " ".join(str(value).splitlines())


def _redact_url(value: str) -> str:
    """Scheme+host+path only — drop userinfo, query, and fragment before a URL
    is persisted. `gateway-status.json` lives under `state/` (which vault-syncs),
    so a gateway configured with `user:pass@` userinfo or a `?token=` query param
    must not land there in plaintext. Fails closed to a placeholder on any parse
    failure (never the raw value, never raise from a best-effort status write)."""
    return gateway_credentials.redact_url(value)


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
    safe = diagnostics.scrub(gateway_credentials.redact_text(msg, globals().get("URL") or ""))
    line = f"[remote-gateway-bridge] {safe}"
    print(line, flush=True)
    _append_log_file(line)


def _append_log_file(line: str) -> None:
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


_REQ_TIMEOUT_S = 35
_ROOM_POST_TIMEOUT_S = 20
_PROACTIVE_POST_TIMEOUT_S = 15
_UPLOAD_TIMEOUT_S = 60
_ACK_TIMEOUT_S = 10
# The longest single request the outbound worker can be waiting on; a stop that
# returns sooner can kill an accepted send whose response is still on the way.
_OUTBOUND_SEND_TIMEOUT_S = max(_REQ_TIMEOUT_S, _ROOM_POST_TIMEOUT_S,
                               _PROACTIVE_POST_TIMEOUT_S, _UPLOAD_TIMEOUT_S, _ACK_TIMEOUT_S)

# urllib wraps only the send in URLError; a socket closed after it raises http.client's
# errors bare (RemoteDisconnected is a ConnectionResetError), and those are outages too.
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionResetError,
                   http.client.IncompleteRead)


@contextlib.contextmanager
def _gateway_call():
    """Every call that receives the gateway URL runs inside this: what it raises carries only
    its type, the redacted gateway and an HTTP status, so no caller can log or persist the URL."""
    with gateway_instance.gateway_call(URL):
        yield


# The last response's status and edge headers, per thread, for the poll's anomaly line.
_RESP_META = threading.local()


def _read_by(resp, deadline: float) -> bytes:
    """The body, read in chunks against a wall-clock deadline: urllib's timeout bounds each
    socket operation, so a body that drips in could otherwise run on past it."""
    chunks = []
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("response body still arriving at its deadline")
        # Looked up each pass: the response drops its socket once the last byte is read.
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.settimeout(left)
        # read1 returns what one socket read gave; read(n) would wait for all n bytes.
        chunk = (resp.read1 if hasattr(resp, "read1") else resp.read)(4096)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _req(method: str, path: str, payload: dict | None = None, timeout: int = _REQ_TIMEOUT_S,
         deadline: "float | None" = None):
    """One authenticated HTTP request. Returns parsed JSON (or {} for empty). With
    `deadline` (time.monotonic()), the whole request must finish by then."""
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("request deadline already passed")
    with _gateway_call():
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{URL}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("Accept", "application/json")
        # CloudFlare bot-fight (error 1010) rejects python-urllib's default
        # User-Agent with a 403; send an explicit client UA so the gateway's edge
        req.add_header("User-Agent", "sutando-gateway-client/1.0")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if deadline is None:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _parse_response(resp, resp.read())
        # Every phase shares the deadline: each step gets the budget left, and a timer shuts
        # every socket made (the wrapped TLS one included) to end a blocked read.
        socks: "list[socket.socket]" = []

        def recorded(base):
            class Recorded(base):
                def __init__(self, *a, **k):
                    super().__init__(*a, **k)
                    self._create_connection = lambda addr, tmo=None, src=None: _connect_by(
                        addr, deadline, tmo, src, socks)

                def connect(self):
                    http.client.HTTPConnection.connect(self)
                    ctx = getattr(self, "_context", None)
                    if ctx is None:
                        return
                    # The wrapped socket replaces the raw one, so it is recorded for the timer and
                    # its handshake, which the timer must be able to cut, runs after that.
                    wrapped = ctx.wrap_socket(self.sock, do_handshake_on_connect=False,
                                              server_hostname=self._tunnel_host or self.host)
                    self.sock = wrapped
                    socks.append(wrapped)
                    wrapped.settimeout(_left(deadline, timeout))
                    wrapped.do_handshake()
            return Recorded

        class Http(urllib.request.HTTPHandler):
            def http_open(self, r):
                return self.do_open(recorded(http.client.HTTPConnection), r)

        class Https(urllib.request.HTTPSHandler):
            def https_open(self, r):
                return self.do_open(recorded(http.client.HTTPSConnection), r, context=self._context)
        cut = threading.Timer(max(0.0, deadline - time.monotonic()), _shut_sockets, (socks,))
        cut.daemon = True
        cut.start()
        try:
            try:
                opened = urllib.request.build_opener(Http, Https).open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                # A buffer, not bytes: gateway_call's scrub keeps objects and empties bytes.
                try:
                    e.sparrow_body = io.BytesIO(e.read() or b"")
                except Exception:  # noqa: BLE001 - a cut or broken error body reads as empty
                    e.sparrow_body = io.BytesIO()
                with contextlib.suppress(Exception):
                    e.close()  # its body is buffered: the connection is released now, not at GC
                raise
            with opened as resp:
                return _parse_response(resp, _read_by(resp, deadline))
        except (OSError, http.client.HTTPException) as e:
            if time.monotonic() >= deadline and not isinstance(e, urllib.error.HTTPError):
                raise TimeoutError("request still unfinished at its deadline") from None
            raise
        finally:
            cut.cancel()


def _shut_sockets(socks) -> None:
    for sock in list(socks):
        with contextlib.suppress(OSError):
            # The base method: an SSLSocket's own shutdown drops its TLS object under a blocked read.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)


def _left(deadline: float, cap: float) -> float:
    """The seconds to the deadline, at most `cap`; TimeoutError once it has passed."""
    left = min(cap, deadline - time.monotonic())
    if left <= 0:
        raise TimeoutError("request deadline reached")
    return left


def _connect_by(addr, deadline: float, cap, source, socks) -> "socket.socket":
    """A connection to `addr` inside the request's deadline: resolution runs in a helper thread
    we stop waiting for at it, and each address candidate gets only the budget left."""
    host, port = addr
    cap = cap if isinstance(cap, (int, float)) and cap > 0 else _REQ_TIMEOUT_S
    found: "list" = []

    def resolve():
        try:
            found.append(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
        except OSError as e:
            found.append(e)
    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    t.join(_left(deadline, cap))
    if not found:
        raise TimeoutError("name resolution still unfinished at its deadline")
    if isinstance(found[0], OSError):
        raise found[0]
    err: "OSError | None" = None
    for family, kind, proto, _name, sa in found[0]:
        sock = None
        try:
            # Inside the try: a family this host cannot open moves on to the next candidate.
            sock = socket.socket(family, kind, proto)
            socks.append(sock)
            sock.settimeout(_left(deadline, cap))
            if source:
                sock.bind(source)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                sock.close()
            if isinstance(e, TimeoutError) and time.monotonic() >= deadline:
                break
    raise err or OSError("no address to connect to")


def _parse_response(resp, body: bytes):
    headers = getattr(resp, "headers", None) or {}
    _RESP_META.last = {"status": getattr(resp, "status", None),
                       "content_length": headers.get("Content-Length"),
                       "cf_ray": headers.get("CF-Ray")}
    raw = body.decode().strip()
    return json.loads(raw) if raw else {}


def _http_error_body(e) -> str:
    """Best-effort read of an HTTPError's response body, for content-sniffing a
    per-task answer vs an endpoint-unsupported one. Never raises. A deadline-bound request has
    already read it within its deadline."""
    try:
        body = getattr(e, "sparrow_body", None)
        return (body.getvalue() if body is not None else (e.read() or b"")).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _read_token_file(path: str) -> str:
    """The onboarding string from a token file, or "" (missing, refused or empty: no rotation); a
    refused file is named on stderr, path and reason only. token_file_token decides the shape."""
    text, reason = _read_token_file_text(path)
    if reason:
        print(f"[remote-gateway-bridge] {gateway_credential_sources.refusal_note(path, reason)}; "
              "no rotation", file=sys.stderr, flush=True)
    return gateway_credential_sources.token_file_token(text or "")


def _read_token_file_url(path: str) -> str:
    """The URL of a split-layout token file, or "": the reload path needs it so a split file
    rewritten to a DIFFERENT gateway is caught by the cross-gateway guard."""
    return gateway_credential_sources.token_file_url(_read_token_file_text(path)[0] or "")


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
    gateway_credential_sources.register(_SECRETS, [raw, secret])  # read from a file: a credential either way
    # The URL guard must cover BOTH layouts: the combined url|secret form
    # (url_from_token) AND the split form (bare secret + a separate
    file_url = (url_from_token or _read_token_file_url(TOKEN_FILE)).rstrip("/")
    if file_url and file_url != URL:
        _log(f"token file names a DIFFERENT gateway ({_redact_url(file_url)}) "
             f"than the running one ({_redact_url(URL)}) — a URL change is not hot-swappable; "
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
    # A new rejection episode invalidates any prior recovered or refused terminal.
    _reenroll_state.pop("recovered_at", None)
    _reenroll_state.pop("refused", None)
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
        refused = _reenroll_state.get("refused")
        # `backoff_s` means "retryable TRANSPORT backoff"; this loop is waiting on
        # a human, so it stays 0 — the re-check cadence is not a reconnect estimate.
        if pending:
            wait = f"relink pending (code {pending})"
        elif refused:
            wait = (f"re-link claim refused (HTTP {refused['status']}"
                    + (f" {refused['error']})" if refused["error"] else ")"))
        else:
            wait = "waiting for re-connect"
        _emit_gateway_status(False, error=f"auth rejected HTTP {code} — {wait}")
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


def _ack_lease_file() -> Path:
    # Beside the ledger, not in it: an older Sparrow reads each ledger value as bool(v).
    return PENDING_ACK_FILE.with_name(f"{PENDING_ACK_FILE.stem}-leases.json")


def _read_ack_json(path: Path) -> dict:
    """One pending-ack file as a dict (fail-open to empty)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        _log(f"pending-ack file {path.name} unreadable ({diagnostics.describe(e)}) — starting empty")
        return {}
    return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}


def _lease_id_of(d: dict) -> "str | None":
    """The broker's lease generation when the dict names a usable one."""
    lease = d.get("lease_id")
    return lease if isinstance(lease, str) and 0 < len(lease) <= LEASE_ID_MAX else None


def _load_pending_acks() -> "dict[str, bool]":
    """Acks the gateway never confirmed, mapped to the durability each may
    claim (fail-open to empty: a lost ledger only costs a redelivery)."""
    return {k: bool(v) for k, v in _read_ack_json(PENDING_ACK_FILE).items()}


def _load_pending_leases() -> "dict[str, str]":
    """The lease generation each pending ack was served under, where known."""
    acks = _read_ack_json(PENDING_ACK_FILE)
    leases = {k: _lease_id_of({"lease_id": v})
              for k, v in _read_ack_json(_ack_lease_file()).items() if k in acks}
    return {k: v for k, v in leases.items() if v}


def _record_pending_acks(pending: "list[tuple]") -> bool:
    """Commit this round's acks before any of them is posted, so a crash between
    ack and confirmation still leaves a ledger to retry from. Each entry is
    (tid, durable) or (tid, durable, lease_id). The lease file commits first, so
    the ledger never names a leased task whose generation is not on disk."""
    with _PENDING_ACK_MUTEX:
        acks = _load_pending_acks()
        stored = _read_ack_json(_ack_lease_file())
        leases = dict(stored)
        for entry in pending:
            tid, durable, lease = (tuple(entry) + (None,))[:3]
            acks[tid] = bool(durable)
            if lease:
                leases[tid] = lease
            else:
                leases.pop(tid, None)
        leases = {k: v for k, v in leases.items() if k in acks}
        if leases != stored and not _durable_write(
                _ack_lease_file(), json.dumps(leases, sort_keys=True)):
            return False
        return _durable_write(PENDING_ACK_FILE, json.dumps(acks, sort_keys=True))


def _forget_pending_ack(tid: str, lease_id: "str | None" = None) -> None:
    """Retire tid's record only while it still names `lease_id`: a newer serve
    re-recorded under another generation keeps its own record."""
    with _PENDING_ACK_MUTEX:
        acks = _load_pending_acks()
        leases = _read_ack_json(_ack_lease_file())
        if tid not in acks or _lease_id_of({"lease_id": leases.get(tid)}) != lease_id:
            return
        acks.pop(tid)
        if _durable_write(PENDING_ACK_FILE, json.dumps(acks, sort_keys=True)) \
                and leases.pop(tid, None) is not None:
            _durable_write(_ack_lease_file(), json.dumps(leases, sort_keys=True))


def _retry_pending_acks(inflight: "set[str]") -> None:
    """Re-post acks the gateway never confirmed. An id no longer in flight had
    its lease closed by the result POST, so its record is retired unposted.
    That inference needs a TRUSTWORTHY in-flight set. When the restore degraded
    the set is empty-because-unknown, so post instead and let the gateway's
    'not leased' 404 retire the lease it actually closed."""
    leases = _load_pending_leases()
    for tid, durable in sorted(_load_pending_acks().items()):
        if tid not in inflight and not _INFLIGHT_DEGRADED:
            _forget_pending_ack(tid, leases.get(tid))
            continue
        _post_task_ack(tid, durable, leases.get(tid))


ACK_DROP_RETRY_S = 0.5
# Dropped acks are retried only while this long since the poll answered remains: well
# inside the broker's 15 s delivery lease. Past it they are left to the pending-ack retry.
ACK_RETRY_BUDGET_S = 10.0


def _connection_dropped(e: BaseException) -> bool:
    """The peer closed or reset the connection: the request may never have been read."""
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return isinstance(reason, (ConnectionResetError, http.client.IncompleteRead))


def _post_task_ack(tid: str, durable: bool = False, lease_id: "str | None" = None,
                   dropped: "list | None" = None, deadline: "float | None" = None,
                   retry_inline: bool = True) -> bool:
    """Tell the gateway a task made it safely into the local queue. `durable`
    says the task file, its media sidecar and the in-flight set are all fsync'd,
    which is the only thing that lets the relay call the task accepted.
    `lease_id` echoes the generation the task was served under; absent for a
    broker that served none. With `dropped`, an ack lost to a dropped connection is
    appended there for the caller to retry; without it, it is retried once inline unless
    `retry_inline` is False. `deadline` (time.monotonic()) bounds the whole request."""
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
        body = {"id": wire_tid}
        if lease_id:
            body["lease_id"] = lease_id
        if durable:
            body["durable"] = True
        bound = {} if deadline is None else {"deadline": deadline}   # a healthy ack's call is unchanged
        try:
            _req("POST", f"/v1/tasks/{safe_tid}/ack", body, timeout=_ACK_TIMEOUT_S, **bound)
        except _NETWORK_ERRORS as e:
            if not _connection_dropped(e) or (dropped is None and not retry_inline):
                raise
            if dropped is not None:
                dropped.append((tid, durable, lease_id))
                _log(f"task ack for {tid} lost to a dropped connection ({diagnostics.describe(e)}) — set aside for retry")
                return False
            # A dropped link loses the ack; one prompt retry lands it inside the delivery lease.
            time.sleep(ACK_DROP_RETRY_S)
            _req("POST", f"/v1/tasks/{safe_tid}/ack", body, timeout=_ACK_TIMEOUT_S, **bound)
        _ack_disabled_until = 0.0  # success (or re-enablement) → clear any backoff
        _forget_pending_ack(tid, lease_id)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            # A 404/405 is ambiguous. The pre-/ack broker returns a bare no-route
            # 404/405 → the endpoint is UNSUPPORTED: back off (cooldown) and retry
            reason = _http_error_body(e).lower() if e.code == 404 else ""
            if "not leased" in reason:
                if "stale lease generation" in reason:
                    _log(f"task ack for {tid} named a superseded lease generation "
                         "— the broker re-serves it; not retrying")
                _forget_pending_ack(tid, lease_id)  # terminal: the lease is gone for good
                return False   # per-task lease gone — keep acking the rest
            _ack_disabled_until = time.time() + ACK_UNSUPPORTED_COOLDOWN
            _log(f"gateway does not support task ack — retrying in "
                 f"{ACK_UNSUPPORTED_COOLDOWN}s")
            return False
        if e.code in (401, 403):
            raise
        _log(f"task ack failed for {tid}: HTTP {e.code} — gateway may redeliver")
        return False
    except _NETWORK_ERRORS as e:
        _log(f"task ack network error for {tid}: {diagnostics.describe(e)} — gateway may redeliver")
        return False


# Times each waiting line recurred in this process, for `_log_backoff`.
_LOG_COUNTS: "dict[tuple, int]" = {}


def _log_backoff(key: tuple, line: str) -> None:
    """Log `line` the 1st, 2nd, 4th, 8th ... time `key` recurs in this process, with the count:
    a wait with no ceiling would otherwise log every second for as long as it lasts."""
    n = _LOG_COUNTS[key] = _LOG_COUNTS.get(key, 0) + 1
    if not n & (n - 1):
        _log(line + (f" (seen {n} times)" if n > 1 else ""))


def _log_backoff_forget(first: str) -> None:
    """Drop every count keyed by `first` (a task or item id) once it is done, so the counts
    stay bounded; the keys are copied first because another thread may be adding one."""
    for key in [k for k in list(_LOG_COUNTS) if k[:1] == (first,)]:
        _LOG_COUNTS.pop(key, None)


def _post_ack_first(tid: str, lease_id: "str | None") -> str:
    """The durable ack of a staged `ack_first` task: "accepted" (a repeat answers
    `duplicate`); "stale", a newer serve holds the lease, so this one was never accepted;
    "gone", no lease at all; or "retry" for any other answer. Never raises: the poll
    meets an auth failure."""
    wire_tid = _broker_tid(tid)
    body = {"id": wire_tid, "durable": True}
    if lease_id:
        body["lease_id"] = lease_id
    try:
        _req("POST", f"/v1/tasks/{urllib.parse.quote(wire_tid, safe='')}/ack", body, timeout=_ACK_TIMEOUT_S)
        return "accepted"
    except urllib.error.HTTPError as e:
        reason = _http_error_body(e).lower() if e.code == 404 else ""
        if "not leased" in reason:
            return "stale" if "stale lease generation" in reason else "gone"
        _log_backoff((tid, "ack-retry"), f"ack_first task {tid}: ack answered HTTP {e.code} "
                     "— kept staged, retrying")
        return "retry"
    except (OSError, ValueError, http.client.HTTPException) as e:
        # A re-ack of an accepted task answers `duplicate`, so retrying is always safe.
        _log_backoff((tid, "ack-retry"), f"ack_first task {tid}: ack failed "
                     f"({diagnostics.describe(e)}) — kept staged, retrying")
        return "retry"


def _ack_first_state(tid: str) -> "str | None":
    """The broker's state for the ask behind a staged task (`GET /v1/tasks/<id>/status`),
    or None when it cannot be read."""
    wire_tid = _broker_tid(tid)
    try:
        doc = _req("GET", f"/v1/tasks/{urllib.parse.quote(wire_tid, safe='')}/status", timeout=_ACK_TIMEOUT_S)
    except (OSError, ValueError, http.client.HTTPException) as e:
        _log_backoff((tid, "ack-status"), f"ack_first task {tid}: status unreadable "
                     f"({diagnostics.describe(e)}) — kept staged")
        return None
    state = doc.get("state") if isinstance(doc, dict) else None
    return state if isinstance(state, str) else None


_CORE_STEP_MAX = 500


def _core_str(v) -> str | None:
    """A core-status field → bounded non-empty str, or None. core-status.json is
    written by another process and may be malformed; a non-string field must not
    be forwarded (the broker calls .lower() on it)."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:_CORE_STEP_MAX] if v else None


def _core_status_written_at() -> float | None:
    """When core-status.json was last written: its `ts` stamp, else the file mtime; None
    when neither can be read. Never raises."""
    path = _STATE / "core-status.json"
    try:
        with open(path) as f:
            ts = json.load(f).get("ts")
        if type(ts) in (int, float) and ts == ts:
            return float(ts)
    except Exception:  # noqa: BLE001 — fall back to the mtime
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return None


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


_POOL_ADVERTISEMENT_FILE = _STATE / "pool-advertisement.json"
# A roster row is ~100 bytes, so this is >10k workers; past it the loop would
# re-read and re-parse a runaway file every pass before it can poll for tasks.
_POOL_ADVERTISEMENT_MAX_BYTES = 1 << 20
_workers_pushed_identity = ""
_workers_push_retry_at = 0.0
# The broker wants a report at least every 60 s (rows go stale at 120 s); the push only runs
# between polls, which take up to POLL_WAIT + 10 s, so 20 s keeps the gap under 60 s.
WORKERS_REFRESH_S = 20.0
_workers_pushed_at: "float | None" = None
_advertisement_unavailable_logged = False

# 404/405/501 are the broker saying "this endpoint does not exist here"; every
# other status is a live endpoint failing, which a short retry can clear.
_UNSUPPORTED_ENDPOINT_STATUS = frozenset({404, 405, 501})


def _read_pool_advertisement() -> "tuple[str, dict | None]":
    """The pool's roster-derived advertisement -> (identity, record), or
    ("", None) when the record is UNAVAILABLE — absent, unreadable, mid-write,
    malformed, or carrying only one of its two halves.

    The identity is a digest of the record's canonical content and is the ONE
    change signal both publications key on. mtime is not: a restore that lands
    below the prior file's mtime is new content the picker must see, and the
    two halves keyed on different signals disagreed on exactly that record.

    Availability is a tri-state, and None is the only "cannot read it" value: a
    record whose maps are empty is AVAILABLE and means "the pool is empty", an
    intentional clear the callers must push. Collapsing the two (the pre-fix
    (0.0, {})) is what let a deleted or half-written file PUT a profile card
    with no `workers`, and the broker REPLACES that document.

    Both halves are validated from this one read so a one-sided record cannot
    POST a status map whose labels never ship. This runs in the task loop
    BEFORE the /v1/tasks poll and may never raise: the outer handler backs off
    and retries the same file, so any exception here — a RecursionError from a
    deeply nested value, not only OSError/ValueError — would stall intake for
    as long as that file stays. The path is opened ONCE, non-blocking, and
    everything is decided on that descriptor: a FIFO with no writer would park
    a blocking open forever, and a size read from a second lookup can be
    stale by the time the content is read, so the bound is enforced on the
    bytes actually read (MAX+1: one more than allowed proves the overflow)."""
    fd = -1
    try:
        fd = os.open(str(_POOL_ADVERTISEMENT_FILE), os.O_RDONLY | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ("", None)
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(_POOL_ADVERTISEMENT_MAX_BYTES + 1)
        if len(data) > _POOL_ADVERTISEMENT_MAX_BYTES:
            return ("", None)
        rec = json.loads(data.decode("utf-8"))
        canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001 — a parser/resource failure is UNAVAILABLE, never a stalled poll
        return ("", None)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(rec, dict):
        return ("", None)
    if not isinstance(rec.get("workers"), dict):
        return ("", None)
    if not isinstance(rec.get("profile_workers"), dict):
        return ("", None)
    return (hashlib.sha256(canonical.encode()).hexdigest(), rec)


def _advertisement_or_none() -> "tuple[str, dict | None]":
    """_read_pool_advertisement, logging the availability edge once per edge so
    an absent producer cannot fill the log at the loop's cadence."""
    global _advertisement_unavailable_logged
    identity, rec = _read_pool_advertisement()
    if rec is None:
        if not _advertisement_unavailable_logged:
            _advertisement_unavailable_logged = True
            _log(f"pool advertisement unavailable ({_POOL_ADVERTISEMENT_FILE.name}"
                 " missing or unreadable) — keeping the last pushed pool state")
    elif _advertisement_unavailable_logged:
        _advertisement_unavailable_logged = False
        _log("pool advertisement readable again")
    return (identity, rec)


def _defer_push(what: str, e: "urllib.error.HTTPError", now: float) -> float:
    """The retry taxonomy both pushes share -> the absolute retry-at."""
    if e.code in _UNSUPPORTED_ENDPOINT_STATUS:
        _log(f"{what} deferred 1h: broker has no such endpoint (HTTP {e.code})")
        return now + 3600
    _log(f"{what} failed, retrying in 5m: endpoint exists, HTTP {e.code}")
    return now + 300


# The broker's copy is a replica: a broker restart empties it, and nothing on
# our side changes, so resend both halves on a cadence even when unchanged.
_REPUSH_EVERY_S = 600.0
_last_full_push_at = 0.0
_now = time.monotonic


_PUSH_THREAD: "threading.Thread | None" = None
_PUSH_LOCK = threading.Lock()
# The profile PUT is unversioned and REPLACES the broker document, so a request
# landing after a successor took the lock overwrites the successor's card.
_OWNERSHIP_RELINQUISHED = threading.Event()


def _publication_permitted() -> bool:
    """False once ownership is being handed over: no request may START, because
    its completion could land after a successor owns the document."""
    return not _OWNERSHIP_RELINQUISHED.is_set()


def _push_pool_advertisement() -> None:
    """Hand the beat's publications to a daemon thread and return at once: both
    requests carry 15 s timeouts and the intake poll must never wait on them.
    A push still in flight from the last beat is left to finish; this beat's is
    skipped, not queued, since the next beat re-reads the advertisement."""
    global _PUSH_THREAD
    with _PUSH_LOCK:
        if _PUSH_THREAD is not None and _PUSH_THREAD.is_alive():
            return
        _PUSH_THREAD = threading.Thread(target=_push_pool_advertisement_now,
                                        name="pool-advertisement", daemon=True)
        _PUSH_THREAD.start()


def _join_push_thread(timeout: float = 5.0) -> bool:
    """A normal exit finishes the snapshot+card pair (bounded): the broker must
    not be left holding a snapshot and a card from different revisions.

    True when nothing is in flight — ownership may only be handed over then.
    """
    t = _PUSH_THREAD
    if t is None or not t.is_alive():
        return True
    t.join(timeout)
    return not t.is_alive()


atexit.register(_join_push_thread)


def _push_pool_advertisement_now() -> None:
    """One read of the advertisement per beat, handed to BOTH publications:
    two reads let a sibling writer's atomic rename land between them, and the
    broker would then hold a snapshot and a card from different revisions."""
    global _workers_pushed_identity, _profile_pushed_identity, _last_full_push_at
    if _now() - _last_full_push_at >= _REPUSH_EVERY_S:
        _workers_pushed_identity = ""
        _profile_pushed_identity = ""
        _last_full_push_at = _now()
    try:
        record = _advertisement_or_none()
        _maybe_push_workers_snapshot(record)
        _maybe_push_agent_profile(record)
        _maybe_pull_worker_labels(record)
    except Exception as e:  # noqa: BLE001 — a background push fails loudly, never silently
        _log(f"pool advertisement push failed: {diagnostics.describe(e)}")


def _workers_body(ad: dict) -> dict:
    """The per-worker report when the record carries one: only it tells the broker
    a worker is retired; the legacy snapshot has no per-worker state."""
    rep = ad.get("report")
    if isinstance(rep, dict) and isinstance(rep.get("workers"), list):
        return rep
    return ad["workers"]


HEALTH_FIELDS = health_command.FIELDS
HEALTH_TTL_S = 15.0
_health_cache: dict = {"at": None, "value": None, "error": None}
# An adapter's own core row, when it injects one; it outranks the monorepo snapshot's.
_HEALTH_COMMAND = health_command.HealthCommand(
    os.environ.get("REMOTE_HEALTH_COMMAND") or "", HEARTBEAT_INTERVAL, lambda m: _log(m),
    env=lambda: _on_task_env())


def _health_snapshot() -> "dict | None":
    """The workspace health summary, reused for HEALTH_TTL_S; None where the monorepo
    src/ is absent or the snapshot fails, so a standalone sparrow reports no health."""
    at = _health_cache["at"]
    if at is not None and time.monotonic() - at < HEALTH_TTL_S:
        return _health_cache["value"]
    value, error = None, None
    try:
        src = _monorepo_src("health_snapshot.py")
        if src:
            if src not in sys.path:
                sys.path.insert(0, src)
            import health_snapshot
            value = health_snapshot.snapshot(_STATE.parent)
    except Exception as e:  # noqa: BLE001 — health is optional; it never breaks a push
        error = diagnostics.describe(e)
    if error and error != _health_cache["error"]:
        _log(f"health snapshot unavailable: {error}")
    _health_cache.update(at=time.monotonic(), value=value, error=error)
    return value


def _health_row(agent: dict) -> dict:
    """The wire row, its `reason` made a slug the broker accepts."""
    row = {k: agent.get(k) for k in HEALTH_FIELDS}
    row["reason"] = health_command.slug(row["reason"])
    return row


def _core_health(snap: "dict | None") -> "dict | None":
    core = next((a for a in (snap or {}).get("agents") or [] if a.get("role") == "core"), None)
    return _health_row(core) if core else None


def _with_health(body: dict, snap: "dict | None") -> dict:
    """The report plus each worker's health and the pool suspension. The legacy body has
    no worker rows, so it and a missing snapshot pass through unchanged."""
    if snap is None or not isinstance(body.get("workers"), list):
        return body
    health = {a.get("id"): _health_row(a) for a in snap.get("agents") or [] if a.get("role") == "worker"}
    rows = [{**r, "health": health[r["id"]]} if isinstance(r, dict) and r.get("id") in health else r
            for r in body["workers"]]
    return {**body, "workers": rows, "suspended": _suspended_row(snap.get("suspended"))}


def _suspended_row(value) -> "dict | None":
    """The pool suspension as summary fields: its reason a slug like any health reason."""
    if not isinstance(value, dict):
        return None
    reason = _health_row({"reason": value.get("reason")})["reason"]
    at = value.get("at")
    numeric = isinstance(at, (int, float)) and not isinstance(at, bool)
    return {"reason": reason or "suspended", "at": at if numeric else None}


def _resolve_host_label() -> None:
    """Fix the process's host label at startup, from the monorepo src/ when present, so the
    lock holder and the profile host_id agree for the whole run; gethostname() drifts with DHCP."""
    src = _monorepo_src("util_paths.py")
    if src and src not in sys.path:
        sys.path.insert(0, src)
    _profile_host_id()


def _profile_host_id() -> str:
    """The host's stable label, as resolved once at startup; never edits sys.path."""
    try:
        return _stable_host_label()
    except OSError:
        return "unknown-host"


def _maybe_push_workers_snapshot(record) -> bool:
    """Push-on-change relay of the pool's workers snapshot (the worker
    picker's read path). An unavailable advertisement pushes NOTHING and
    leaves the broker holding the last snapshot we sent; an unsupported
    endpoint backs the push off an hour and any other HTTP status 5m;
    nothing here may ever break the task loop."""
    global _workers_pushed_identity, _workers_push_retry_at, _workers_pushed_at
    if not _publication_permitted():
        return False
    now = time.time()
    if now < _workers_push_retry_at:
        return False
    identity, ad = record  # one read per beat, shared with the other publication
    if ad is None:
        return False
    base = _workers_body(ad)
    body = _with_health(base, _health_snapshot())
    if body is not base:
        # Health changes between advertisements, so it joins the change signal.
        identity += ":" + hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    # Only a body carrying health has the broker's 120 s staleness; others keep the 600 s re-push.
    fresh = body is base or (_workers_pushed_at is not None and _now() - _workers_pushed_at < WORKERS_REFRESH_S)
    if identity == _workers_pushed_identity and fresh:
        return False
    try:
        _req("POST", "/v1/workers", body, timeout=15)
    except urllib.error.HTTPError as e:
        _workers_push_retry_at = _defer_push("workers-snapshot push", e, now)
        return False
    except Exception as e:  # noqa: BLE001 — relay must never kill the loop
        _workers_push_retry_at = now + 300
        _log(f"workers-snapshot push failed, retrying in 5m: {diagnostics.describe(e)}")
        return False
    _workers_pushed_identity = identity
    _workers_pushed_at = _now()
    _log("workers-snapshot pushed")
    return True


_profile_pushed_identity = ""
_profile_push_retry_at = 0.0

# The host injects this optional adapter before executing the standalone bridge.
# AG2 Space owns overrides; the skill owns the roster.
_WORKER_LABEL_APPLIER = globals().get("_SUTANDO_WORKER_LABEL_APPLIER")
_PROFILE_LABEL_REFRESH_S = 60.0
_profile_labels_checked_at = 0.0
_profile_labels_retry_at = 0.0


def _build_agent_profile(workers: "dict") -> "dict":
    """The instance's identity card for PUT /v1/agents/{mxid}/profile.
    Only instance-authoritative fields — appearance is user/platform-owned
    and the broker drops it from instance PUTs anyway.

    `workers` comes from an advertisement the caller already established is
    AVAILABLE: the broker REPLACES the profile document, so this function must
    never be reached with a map it could not read."""
    name = (os.environ.get("SUTANDO_DISPLAY_NAME") or "Sutando").strip()
    return {"display": {"name": name},
            "host": {"host_id": _profile_host_id(), "kind": "local"},
            "workers": workers}


def _maybe_push_agent_profile(record) -> bool:
    """Push-on-change relay of the agent's profile card. An unavailable
    advertisement pushes NOTHING: the card carries the `workers` map the
    picker draws and the broker REPLACES the document, so a card built from
    a record we could not read would erase the pool. Same retry taxonomy as
    the workers-snapshot push; nothing here may break the task loop."""
    global _profile_pushed_identity, _profile_push_retry_at
    if not _publication_permitted():
        return False
    now = time.time()
    if now < _profile_push_retry_at:
        return False
    mxid = _reenroll_identity()
    if not mxid:
        return False  # identity may appear mid-episode; recheck next loop
    identity, ad = record  # one read per beat, shared with the other publication
    if ad is None:
        return False
    # The same record identity the workers push keys on, so the two halves
    # can never disagree on whether a record is new; mxid may change mid-run.
    key = f"{mxid}\n{identity}"
    if key == _profile_pushed_identity:
        return False
    card = _build_agent_profile(ad["profile_workers"])
    try:
        _req("PUT", f"/v1/agents/{urllib.parse.quote(mxid, safe='')}/profile",
             card, timeout=15)
    except urllib.error.HTTPError as e:
        _profile_push_retry_at = _defer_push("agent-profile push", e, now)
        return False
    except Exception as e:  # noqa: BLE001 — relay must never kill the loop
        _profile_push_retry_at = now + 300
        _log(f"agent-profile push failed, retrying in 5m: {diagnostics.describe(e)}")
        return False
    _profile_pushed_identity = key
    _log(f"agent-profile pushed for {mxid}")
    return True


def _maybe_pull_worker_labels(record) -> bool:
    """Apply explicit owner label overrides after our profile is published."""
    global _profile_labels_checked_at, _profile_labels_retry_at
    if not callable(_WORKER_LABEL_APPLIER) or not _publication_permitted():
        return False
    now = time.time()
    if now < max(_profile_labels_retry_at,
                 _profile_labels_checked_at + _PROFILE_LABEL_REFRESH_S):
        return False
    mxid = _reenroll_identity()
    identity, ad = record
    if not mxid or ad is None or _profile_pushed_identity != f"{mxid}\n{identity}":
        return False
    path = f"/v1/agents/{urllib.parse.quote(mxid, safe='')}/profile"
    try:
        profile = _req("GET", path, timeout=15)
    except urllib.error.HTTPError as e:
        _profile_labels_retry_at = _defer_push("worker-label read", e, now)
        return False
    except Exception as e:  # noqa: BLE001 — optional read cannot stop task intake
        _profile_labels_retry_at = now + 300
        _log(f"worker-label read failed, retrying in 5m: {diagnostics.describe(e)}")
        return False

    _profile_labels_checked_at = now
    config = profile.get("config") if isinstance(profile, dict) else None
    display = profile.get("display") if isinstance(profile, dict) else None
    version = config.get("version") if isinstance(config, dict) else None
    labels = display.get("worker_labels") if isinstance(display, dict) else None
    if (not isinstance(profile, dict)
            or type(profile.get("schema_version")) is not int
            or profile.get("schema_version") != 1
            or profile.get("mxid") != mxid or type(version) is not int
            or version < 0 or not isinstance(labels, dict)
            or len(labels) > 10000
            or any(not isinstance(wid, str) or not _is_worker_id(wid)
                   or not isinstance(label, str) or not label
                   or label != label.strip() or len(label) > 120
                   or any(ord(ch) < 32 or ord(ch) == 127 for ch in label)
                   for wid, label in labels.items())):
        _profile_labels_retry_at = now + 300
        _log("worker-label read returned an invalid profile; keeping local labels")
        return False
    if _reenroll_identity() != mxid:
        return False
    try:
        result = _WORKER_LABEL_APPLIER(labels, version, mxid)
    except Exception as e:  # noqa: BLE001 — local pool may be absent or upgrading
        _profile_labels_retry_at = now + 300
        _log(f"worker-label apply failed, retrying in 5m: {diagnostics.describe(e)}")
        return False
    if isinstance(result, dict) and result.get("changed"):
        _log(f"worker labels applied from owner profile config v{int(version)}")
    return True


# The primary app checks every 30 minutes; the fallback checks every five.
_HEALTH_REPORT_MAX_AGE = 35 * 60
# A self-report older than this needs a live beat to stand: a dead core leaves its last
# "idle" behind, while an idle core rewrites nothing, so a beating core keeps it.
_CORE_STATUS_MAX_AGE = 60 * 60


def _liveness_status(alive) -> str | None:
    """The status the core's beat alone supports: idle when it beats, offline when it
    stopped, nothing when there is no beat to judge (a standalone sparrow, or a core
    that has not started its heartbeat yet)."""
    return {True: "idle", False: "offline"}.get(alive)


def _core_stopped_at() -> float | None:
    """mtime of this host's graceful-stop tombstone (core_heartbeat.mark_stopped), or None.
    The next beat run deletes it, so it cannot outlive a core that came back and beat."""
    try:
        return (_STATE / "cores" / f"{_profile_host_id()}.stopped").stat().st_mtime
    except OSError:
        return None


def _reported_core_status(core_health: "dict | None" = None) -> tuple[str | None, str | None]:
    """Overlay independent diagnostics without exporting their private details."""
    status, step = _read_core_status()
    alive = core_health.get("alive") if isinstance(core_health, dict) else None
    written_at = _core_status_written_at() if status is not None else None
    stopped_at = _core_stopped_at() if core_health is not None and alive is not True else None
    if stopped_at is not None and (written_at is None or written_at < stopped_at):
        # A stop newer than the self-report outranks it; a later start supersedes the stop.
        status, step = "offline", None
    elif core_health is not None and (written_at is None or (
            alive is not True and time.time() - written_at > _CORE_STATUS_MAX_AGE)):
        # A stopped core deletes its beat, so a missing beat vouches for nothing; omitting the
        # field would leave the broker's last presence status standing.
        status, step = _liveness_status(alive), None
        if status is None:
            status, step = "unknown", "Core liveness unavailable"
    if status in ("error", "offline"):
        return status, step
    try:
        report = json.loads((_STATE / "agent-health.json").read_text())
    except FileNotFoundError:
        return status, step
    except Exception:
        return "unknown", "Health check unavailable"
    try:
        ts, total, failures = (report[k] for k in ("checked_at", "total", "failures"))
        if (report.get("version") != 1 or type(ts) not in (int, float)
                or not 0 <= time.time() - ts
                or type(total) is not int or total <= 0
                or type(failures) is not int or not 0 <= failures <= total):
            return "unknown", "Health check unavailable"
        if time.time() - ts > _HEALTH_REPORT_MAX_AGE:
            # An old report is no opinion; a host that stopped checking must not read unknown forever.
            return status, step
        if failures:
            return "error", f"Health check: {failures} failing check(s)"
    except Exception:
        return "unknown", "Health check unavailable"
    return status, step


def _post_heartbeat(inflight: set[str], force: bool = False) -> bool:
    """Best-effort liveness + core-status ping. Liveness feeds hosted dashboards;
    the status/step feed the broker's presence sweep (agent working/available/…)."""
    global _heartbeat_retry_at, _heartbeat_backoff, _last_heartbeat_at, _last_core_health
    now = time.time()
    if _heartbeat_retry_at and now < _heartbeat_retry_at:
        return False
    health = (health_command.with_motion(_HEALTH_COMMAND.row(), bool(inflight))
              if _HEALTH_COMMAND else _core_health(_health_snapshot()))
    changed = health is not None and health != _last_core_health
    if not force and not changed and now - _last_heartbeat_at < HEARTBEAT_INTERVAL:
        return False
    _last_heartbeat_at = now
    _last_core_health = health
    # An injected row is not the core's beat: presence status stays the core-status file's.
    _status, _step = _reported_core_status(None if _HEALTH_COMMAND else health)
    try:
        payload = {
            "client": "sutando-gateway-client",
            "protocol_version": 1,
            "provider": PROVIDER,
            "tier": LOCAL_TIER,
            "inflight": len(inflight),
            "package_version": _PACKAGE_VERSION,
            "capabilities": ["task-ack", "heartbeat", "result-skip-markers",
                             "core-status", "team-collaborator", VOICE_OUTCOME_CONTRACT],
        }
        # Only include when present so a status-less node never clobbers the
        # broker's last-known core-status (the broker only records on presence).
        if _status is not None:
            payload["status"] = _status
        if _step is not None:
            payload["step"] = _step
        if health is not None:
            payload["health"] = health
            payload["capabilities"].append("worker_health.v1")
        reply = _req("POST", "/v1/heartbeat", payload, timeout=10)
        _heartbeat_retry_at, _heartbeat_backoff = 0.0, 0.0
        _record_setup_hint(reply)
        _refresh_served_setup(reply)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            _heartbeat_backoff = min(max(_heartbeat_backoff * 2, HEARTBEAT_INTERVAL),
                                     HEARTBEAT_UNSUPPORTED_MAX_BACKOFF)
            _heartbeat_retry_at = time.time() + _heartbeat_backoff
            _log(f"gateway does not support heartbeat — retrying in {_heartbeat_backoff:.0f}s")
            return False
        if e.code in (401, 403):
            raise
        _log(f"heartbeat failed: HTTP {e.code} — continuing")
        return False
    except _NETWORK_ERRORS as e:
        _log(f"heartbeat network error: {diagnostics.describe(e)} — continuing")
        return False


def _record_setup_hint(reply) -> None:
    """Keep the heartbeat reply's setup_revision and versions for an adapter to read;
    best-effort, like the heartbeat itself."""
    if not isinstance(reply, dict):
        return
    # Gateway values: the revision only as a revision_field token, the versions only as PEP 440.
    hint = {k: (diagnostics.revision_field(reply.get(k), URL) if k == "setup_revision"
                else diagnostics.version_field(reply.get(k))) for k in SETUP_HINT_FIELDS if k in reply}
    if "setup_revision" in hint and hint["setup_revision"] is None:
        # A refused announcement still retires the cache it replaces: no revision is never "any".
        hint[SETUP_REVISION_WITHHELD] = True
    hint = {k: v for k, v in hint.items() if v is not None}
    if not hint:
        return
    payload = diagnostics.safe_status(dict(hint, ts=int(time.time())),
                                      lambda text: gateway_credentials.redact_text(text, URL))
    try:
        text = json.dumps(payload, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as e:
        _log(f"setup hint not recorded: {diagnostics.describe(e)}")
        return
    if not _durable_write(SETUP_HINT_FILE, text):
        _log("setup hint not recorded")


_SETUP_REFRESHER: "setup_fetch.Refresher | None" = None


def _refresh_served_setup(reply) -> None:
    """Fetch the served setup when the reply announces a revision Sparrow does not hold; never
    raises, and a failed fetch keeps the last good copy (setup_fetch.Refresher)."""
    global _SETUP_REFRESHER
    if not isinstance(reply, dict):
        return
    if _SETUP_REFRESHER is None:
        _SETUP_REFRESHER = setup_fetch.Refresher(
            lambda path, t: _req("GET", path, timeout=t, deadline=time.monotonic() + t),
            _durable_write, _STATE, GATEWAY_INSTANCE, log=_log, gateway_url=URL)
    try:
        agent = _reenroll_identity()
    except Exception as e:  # noqa: BLE001 - an unread identity only skips the agent_mxid check
        _log(f"served setup: identity unread ({diagnostics.describe(e)})")
        agent = ""
    _SETUP_REFRESHER.maybe_refresh(diagnostics.revision_field(reply.get("setup_revision"), URL), agent)


_OWN_PROC_START: dict = {}


def _own_proc_start() -> "str | None":
    """This process's precise start (workspace_lock.proc_start), the generation its lock names."""
    pid = os.getpid()
    if pid not in _OWN_PROC_START:
        _OWN_PROC_START.clear()
        _OWN_PROC_START[pid] = _ws_proc_start(pid)
    return _OWN_PROC_START[pid]


def _emit_gateway_status(connected: bool, *, error: str | None = None,
                         backoff_s: int = 0, poll_ok_ts: float | None = None) -> None:
    """Write `state/gateway-status.json` — the connection's own liveness, for a
    local supervisor to render connected-vs-reconnecting.

    Best-effort: a status-write failure MUST NOT disturb the poll loop, so all
    errors are swallowed. Only explicit poll-success evidence advances
    `last_ok_ts`; other writes preserve the prior value from disk.
    """
    try:
        last_ok = None
        try:
            with open(GATEWAY_STATUS_FILE) as f:
                last_ok = (json.load(f) or {}).get("last_ok_ts")
        except (FileNotFoundError, ValueError, OSError):
            last_ok = None
        written_at = time.time()
        now = int(written_at)
        if poll_ok_ts is not None:
            last_ok = int(poll_ok_ts)
        payload = {
            "connected": bool(connected),
            "ts": now,
            # Which process wrote it, and exactly when: a supervisor binds a restart's success to them.
            "pid": os.getpid(),
            "start": agent_identity.own_start(),
            "proc_start": _own_proc_start(),
            "written_at": written_at,
            "last_ok_ts": last_ok,
            "backoff_s": int(backoff_s),
            "error": _one_line(gateway_credentials.redact_text(error, URL)) if error else None,
            "gateway": _redact_url(URL),
            # Re-read on every write, so a lane switch or re-login shows at the next poll.
            "agent_id": (_reenroll_identity() or None) if connected else None,
            # Keyed digest of identity + full URL: a reader matches a saved reading's binding.
            "gateway_binding": (gateway_instance.binding_digest(_reenroll_identity(), URL)
                                or None) if connected else None,
            "launched_via": _LAUNCHED_VIA,
            # Read by an adapter's verify: which inbox this bridge actually feeds.
            "tasks_dir": os.path.realpath(TASKS_DIR),
            "results_dir": os.path.realpath(RESULTS_DIR),
            "schema_version": 1,
            "runtime": {**RUNTIME_IDENTITY, "engine": _engine_desc(),
                        **_ENGINE_COUNTS},
        }
        if _HELD_UNRESOLVED:
            # Staged relay asks the bridge can neither publish nor clear without a person.
            payload["ack_first_unresolved"] = sorted(_one_line(t)[:128] for t in _HELD_UNRESOLVED)[:20]
        retrying = _relay_results_retrying(time.time())
        if retrying:
            payload["relay_results_retrying"] = retrying
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
        elif _reenroll_state.get("refused"):
            # The server answered the claim; without this the owner sees only a 401.
            r = _reenroll_state["refused"]
            payload["reenroll"] = {
                "pending": False,
                "refused": True,
                "refused_status": r["status"],
                "refused_error": r["error"],
                "refused_at": r["at"],
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
        payload = diagnostics.safe_status(payload, lambda text: gateway_credentials.redact_text(text, URL))
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
    with gateway_instance.gateway_call(url):  # a gateway-supplied URL, maybe under the gateway's
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
        _log(f"media fetch failed ({diagnostics.describe(e)}) — leaving marker as-is")
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
        _log(f"media save failed ({diagnostics.describe(e)}) — leaving marker as-is")
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
        _log(f"owner-activity write failed: {diagnostics.describe(e)}")


def _fsync_in_place(tid: str, *targets: Path) -> bool:
    """Fsync existing files and directories where supported."""
    try:
        for target in targets:
            if os.name == "nt" and target.is_dir():
                continue
            flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
            fd = os.open(target, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        _log(f"durability repair failed for {tid} ({diagnostics.describe(exc)})")
        return False
    return True


def _repair_pending_task(tid: str, task: dict) -> bool:
    """A task the gateway redelivered while it is still queued here: fsync the
    queued file and its directory and (re)commit the media sidecar, so a task
    written by a pre-durability client can still earn a `durable` ack."""
    tfile = find_task_file(TASKS_DIR, tid)
    if tfile is None:
        return False
    if not _fsync_in_place(tid, tfile, TASKS_DIR):
        return False
    return _record_task_media(tid, task)



# Signal Room tasks (5G ⑤a-cap), AG2 Space adapter edge: names the ONE directory a
# Team result may attach from; `attach_markers_confined` withholds anything else.
def _signal_task_media_lines(media_dir: str) -> list[str]:
    """Prose lines (no fence, no header-shaped line) appended after the Team
    guardrail of a Signal Room task, naming the task's own media directory."""
    output_q = shlex.quote(f"{media_dir}/<name>.png")  # a workspace path may hold spaces
    return [
        "",
        "Signal Room media: this request came from a live Signal Room call. If it asks for "
        "an image, illustration, chart or diagram, you MAY create ONE with the "
        "image-generation skill (python3 skills/image-generation/scripts/generate.py "
        f"--prompt \"...\" --output {output_q}), and you may save images you "
        f"actually found. Save such files ONLY under {media_dir}/ (create the directory), "
        f"then reference each on its own line as [file: {media_dir}/<name>.png] -- an "
        "absolute path, up to 10 files. A file anywhere else is withheld from the room. "
        "Write the prose answer first; a picture is garnish, never the answer.",
    ]

def _on_task_env() -> "dict[str, str]":
    """The bridge's environment minus every relay credential."""
    secrets = [v for v in (TOKEN, _RAW, HS_MEDIA_TOKEN) if v]
    return {k: v for k, v in os.environ.items()
            if not (k.startswith(("REMOTE_", "AG2_")) and "TOKEN" in k)
            and not any(sec in v for sec in secrets)}


# Header fields handed to the wake command's environment, as SPARROW_<NAME>.
_ON_TASK_ENV_FIELDS = (("SPARROW_SOURCE", "source"), ("SPARROW_ROOM_ID", "channel_id"),
                       ("SPARROW_THREAD_ROOT", "thread_root"),
                       ("SPARROW_SOURCE_MESSAGE_ID", "source_message_id"),
                       ("SPARROW_SENDER", "user_id"), ("SPARROW_SENDER_NAME", "sender_name"),
                       ("SPARROW_ACCESS_TIER", "access_tier"))
_ON_TASK_ENV_MAX = 512
# How long a wake command that does not read stdin may hold the pipe open.
_ON_TASK_STDIN_TIMEOUT_S = 30.0


_CONTROL_TO_SPACE = {c: " " for c in (*range(0x20), *range(0x7F, 0xA0))}


def _env_safe(value) -> str:
    """_one_line, then every C0/C1 control character (ESC, BEL, TAB, DEL...) as a space."""
    return _one_line(value).translate(_CONTROL_TO_SPACE)


def _on_task_header_env(text: str) -> "dict[str, str]":
    """SPARROW_* variables for the task file's routing headers; single-line, bounded."""
    headers = local_task_protocol.parse_task_headers_trusted(text).headers
    secrets = [v for v in (TOKEN, _RAW, HS_MEDIA_TOKEN) if v]
    env = {}
    for name, key in _ON_TASK_ENV_FIELDS:
        value = _env_safe(headers.get(key) or "").strip()
        if value and not any(sec in value for sec in secrets):
            env[name] = value[:_ON_TASK_ENV_MAX]
    return env


def _feed_on_task_stdin(proc: "subprocess.Popen", data: bytes, tid: str) -> None:
    """Write `data` to the child's stdin and close it, giving up after a bounded wait."""
    pipe = proc.stdin
    if pipe is None:
        return
    try:
        if os.name == "nt":  # pragma: no cover  # no select() on Windows pipes: blocking write
            pipe.write(data)
            return
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        view = memoryview(data)
        deadline = time.monotonic() + _ON_TASK_STDIN_TIMEOUT_S
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log(f"on-task command did not read its stdin for {tid}; closed it")
                break
            _, ready, _ = select.select([], [fd], [], min(remaining, 1.0))
            if ready:
                try:
                    view = view[os.write(fd, view):]
                except BlockingIOError:
                    continue
    except (BrokenPipeError, ConnectionResetError):
        pass  # the command exited or closed stdin without reading: it ignores the payload
    except (OSError, ValueError) as exc:
        _log(f"on-task stdin write failed for {tid}: {diagnostics.describe(exc)}")
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _feed_and_reap(proc: "subprocess.Popen", data: bytes, tid: str) -> None:
    """Hand the wake command its task on stdin, then reap it."""
    _feed_on_task_stdin(proc, data, tid)
    code = proc.wait()
    if code != 0:
        _log(f"on-task command exited {code} for {tid}")


def _fire_on_task(tid: str, path: Path, text: "str | None" = None) -> None:
    """Start REMOTE_TASK_ON_TASK for one newly published task file; never waits on it.
    The command gets the task's bytes on stdin and its routing headers in env."""
    if not ON_TASK_CMD:
        return
    try:
        if text is None:
            text = path.read_text(encoding="utf-8")
        data = text.encode("utf-8")
        argv = shlex.split(ON_TASK_CMD) + [str(path)]
        # A declared bridge's wake command acts as the agent: the owner sets its token, never the host's.
        env = {**gateway_credential_sources.wake_environ(_on_task_env(), _RAW, URL), **_on_task_header_env(text),
               "SPARROW_TASK_ID": tid, "SPARROW_TASK_FILE": str(path)}
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception as exc:  # noqa: BLE001 — a bad wake command never stops the pull loop
        _log(f"on-task command failed to start for {tid}: {diagnostics.describe(exc)}")
        return

    threading.Thread(target=_feed_and_reap, args=(proc, data, tid),
                     name=f"on-task-{tid}", daemon=True).start()


def _settle_as_handled(tid: str) -> bool:
    """Answer a task this node will not queue with the redelivery control result; True when durable."""
    rfile = RESULTS_DIR / f"{tid}.txt"
    if rfile.exists():
        # A reply the local core wrote with a plain write_text: this process
        # has committed nothing yet, so fsync before claiming durable.
        return _fsync_in_place(tid, rfile, RESULTS_DIR)
    durable = _durable_write(rfile, GATEWAY_REDELIVERY_RESULT)
    # Provenance the result BODY cannot carry: a Team runtime controls
    # the body and can emit these exact bytes, but not this process's set.
    if durable:
        _REDELIVERED.add(tid)
    return durable


def _task_intake_veto(task: dict) -> "str | None":
    """TASK_INTAKE_GATE's reason to settle a fresh task unqueued; a gate that raises delivers it."""
    if TASK_INTAKE_GATE is None:
        return None
    try:
        return TASK_INTAKE_GATE(task)
    except Exception as exc:  # noqa: BLE001 — a broken gate must never drop a message
        _log(f"task intake gate failed for {task.get('id')}: {diagnostics.describe(exc)}; delivering")
        return None


def _write_task(task: dict, hold: bool = False) -> "tuple[str, bool] | None":
    """Serialize a gateway task into tasks/task-<id>.txt (same schema as bridges).
    Returns (task id, durable) — `durable` says the queue write and its sidecar
    committed, which is what an ack may claim — or None when nothing was queued.
    With `hold` (an `ack_first` task) a fresh task is staged, not published:
    `_advance_held` publishes it once its ack is accepted."""
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
    # Idempotent: don't re-write a task already queued, claimed, or archived —
    # but a pre-S1 queue file still has to earn its durability before the ack.
    if _task_pending(tid):
        return tid, _repair_pending_task(tid, task)
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
        durable = _settle_as_handled(tid)
        _log(f"dedup: {tid} already handled — not re-queued")
        return tid, durable
    duplicate = _task_intake_veto(task)
    if duplicate:
        durable = _settle_as_handled(tid)
        _log(f"{duplicate} ({tid})")
        return tid, durable
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    # Promote only the exact broker boolean plus Team request; the legacy Guest
    # wire tier keeps old nodes restricted and body text cannot opt itself in.
    attested_tier, broker_collaborator = local_task_protocol.broker_attested_tier(
        task.get("access_tier"), task.get("requested_access_tier"), task.get("collaborator"))
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
        elif f == "timestamp":
            if task.get(f) not in (None, ""):
                lines.append(f"timestamp: {_one_line(task[f])}")
            lines.append(f"local_time: {local_task_protocol.local_time_value()}")
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
            _raw_task = str(task["task"])
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
                         f"({diagnostics.describe(_vault_exc)}) — "
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
        elif f == "task_layout":
            lines.append("task_layout: mid")
        elif f == "owner_mentioned":
            if task.get(f) == "true":  # the broker's exact string only; nothing else is a claim
                lines.append("owner_mentioned: true")
        elif f == "picker_args":
            # Present-but-unusable is preserved as a refusing stamp, like
            # picker_command: dropping it makes `add` + bad args a valid add.
            if f in task:
                pa = task[f]
                if isinstance(pa, (dict, list)):
                    # json.loads reads this, so re-serialize — _one_line would
                    # emit a Python repr the reader cannot parse.
                    lines.append(f"picker_args: {json.dumps(pa, separators=(',', ':'))}")
                else:
                    lines.append(f"picker_args: {'' if pa is None else _one_line(pa)}")
        elif f == "platform_card":
            # Signed platform-metadata pointer: re-serialize only the expected
            # subkeys as one compact JSON line (dict repr or extra keys never
            pc = task.get("platform_card")
            if isinstance(pc, dict) and all(k in pc for k in _PLATFORM_CARD_KEYS):
                card = {k: str(pc[k]) for k in _PLATFORM_CARD_KEYS}
                lines.append(f"platform_card: {json.dumps(card, separators=(',', ':'))}")
        elif f == "picker_command":
            # Present but unusable (empty OR null) reaches the parser as a
            # refused stamp; dropping it lets prose decide an unstamped action.
            if f in task:
                _pc = task[f]
                lines.append(f"picker_command: {'' if _pc is None else _one_line(_pc)}")
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
    lines.extend(ag2space_tier_lines(sender_tier, collaborator_enabled, f"results/{tid}.txt"))
    # A relay-stamped Signal task may attach from ITS OWN output directory only;
    # name it here (absolute) so the marker written is the one the guard confines.
    if sender_tier == "team" and isinstance(task.get("signal"), dict):
        lines.extend(_signal_task_media_lines(str(RESULTS_DIR / tid)))
    # ===SKILL INSTRUCTIONS=== (owner-tier only): prose/numbered lines only, no
    # header-shaped lines, so appending after access_tier keeps it the last one.
    if sender_tier == "owner":
        # Template lives in result_markers.render_skill_prelude (single owner):
        # a dedup requeue re-renders it there from the stored header (#3613).
        lines.extend(render_skill_prelude(
            _one_line(task.get("channel_id") or ""), CHANNEL_DIR, tid,
            _one_line(task.get("addressed_to") or "")))
    # Last, after every tier block: it keeps their limits and replaces their reply step.
    if task.get("owner_mentioned") == "true":
        lines.extend(owner_mention_lines(f"results/{tid}.txt"))
    from .local_task_protocol import apply_task_stamper
    task_text = apply_task_stamper("\n".join(lines) + "\n")
    tmp = _stage_durable(_held_dir() / dest.name if hold else dest, task_text)
    if tmp is None:
        _log(f"task write FAILED for {tid} — not queued, not acked")
        return None
    # The sidecar has to be visible BEFORE the task is: the results watcher is
    # already running, so a sidecar written after the publish can lose the race.
    if not _record_task_media(tid, task):
        tmp.unlink(missing_ok=True)
        _log(f"media sidecar FAILED for {tid} — not queued, not acked")
        return None
    if hold:
        return _hold_staged(tid, tmp, task_text, task, sender_tier)
    if not _publish_staged(tmp, dest):  # atomic publish: never a partial file
        tmp.unlink(missing_ok=True)
        return None
    _on_published(tid, dest, task_text, task, sender_tier)
    return tid, True


def _on_published(tid: str, dest: Path, task_text: str, task: dict, sender_tier: str) -> None:
    """What follows a fresh publish of a served task, and only that."""
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
    # Only a fresh publish reaches here, so redelivery and restarts never re-fire.
    _fire_on_task(tid, dest, task_text)


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
        _log(f"dedup alias ledger unreadable ({diagnostics.describe(exc)}) — deferring")
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
        _log(f"dedup alias persist FAILED ({diagnostics.describe(exc)}) — keeping original delivery")
        return False


def _delivery_tid(tid: str) -> "str | None":
    """The id to POST under, or None when the ledger cannot be read."""
    aliases = _load_dedup_aliases()
    return None if aliases is None else aliases.get(tid, tid)


def _forget_dedup_alias(tid: str) -> None:
    aliases = _load_dedup_aliases()
    if aliases is None:
        return
    delivery = aliases.get(tid)
    if delivery is not None:
        record = read_item(_delivery_core().backend.root, _broker_tid(delivery))
        if record and record.get("status") == "DELIVERED":
            return  # Waiting dependents still resolve the holder's accepted broker id.
    if aliases.pop(tid, None) is not None:
        _save_dedup_aliases(aliases)


def _load_task_rooms() -> dict[str, str]:
    """Restore the {task id → room id} sidecar map (fail-open to empty)."""
    try:
        data = json.loads(TASK_ROOMS_FILE.read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        _log(f"task-rooms file unreadable ({diagnostics.describe(e)}) — starting empty")
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
        _log(f"task-rooms persist failed ({diagnostics.describe(e)}) — continuing")


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


def _load_task_media() -> "dict | None":
    """The per-task media-mode map, or None when it exists but cannot be read.

    None is not the same as empty: guessing "no entry" sends a Signal task's
    attachment down the room-scoped route, outside the task's own lease.
    """
    if not TASK_MEDIA_FILE.exists():
        return {}
    try:
        loaded = json.loads(TASK_MEDIA_FILE.read_text())
    except (OSError, ValueError) as exc:
        _log(f"task-media sidecar unreadable ({diagnostics.describe(exc)}) — deferring")
        return None
    return loaded if isinstance(loaded, dict) else None


def _record_task_media(tid: str, task: dict) -> bool:
    """Commit this task's media mode, keyed by the WIRE id the media route posts
    under. True when there is nothing to record (an ordinary relay task)."""
    if not isinstance(task.get("signal"), dict):
        return True
    # A matching entry is re-committed rather than skipped: on the repair path an
    # entry can be visible from a rename whose directory fsync never landed.
    with _TASK_MEDIA_MUTEX:
        media = _load_task_media()
        if media is None:
            return False
        media[_broker_tid(tid)] = {"mode": "task-media",
                                   "thread_root": str(task.get("thread_root") or "")}
        return _durable_write(TASK_MEDIA_FILE, json.dumps(media, sort_keys=True))


def _forget_task_media(tid: str) -> None:
    """Retire a finished delivery's media mode (best-effort, like the room map)."""
    delivery = _delivery_tid(tid)
    if delivery is None:
        return
    with _TASK_MEDIA_MUTEX:
        media = _load_task_media()
        if media is None:
            return
        if media.pop(_broker_tid(delivery), None) is not None:
            _durable_write(TASK_MEDIA_FILE, json.dumps(media, sort_keys=True))


def _load_ack_first() -> "dict | None":
    """The `ack_first` marks, or None when the file exists but cannot be read.

    None is not "no mark": guessing none closes a relay ask's honoured fold as a
    bare suppression, and the broker can no longer say where its answer went.
    """
    if not ACK_FIRST_FILE.exists():
        return {}
    try:
        loaded = json.loads(ACK_FIRST_FILE.read_text())
    except (OSError, ValueError) as exc:
        _log_backoff(("ack-first-marks",), f"ack_first marks unreadable ({diagnostics.describe(exc)}) — deferring")
        return None
    return loaded if isinstance(loaded, dict) else None


def _record_ack_first(tid: str) -> bool:
    """Commit the mark of a task served with `ack_first`, before its ack. False withholds
    the ack; marks that cannot be read are never overwritten."""
    with _ACK_FIRST_MUTEX:
        marks = _load_ack_first()
        if marks is None:
            return False
        marks[_broker_tid(tid)] = tid
        return _durable_write(ACK_FIRST_FILE, json.dumps(marks, sort_keys=True))


def _forget_ack_first(broker_id: str) -> bool:
    """Retire a mark. True once it is gone; False while the marks cannot be read or written."""
    with _ACK_FIRST_MUTEX:
        marks = _load_ack_first()
        if marks is None:
            return False
        if marks.pop(broker_id, None) is None:
            return True
        return _durable_write(ACK_FIRST_FILE, json.dumps(marks, sort_keys=True))


def _retire_delivered_marks() -> None:
    """At start, retire each mark whose result the outbox shows delivered once no result
    of its task is still live: a crash came between that result's archive and its retirement."""
    marks = _load_ack_first()
    if not marks:
        return
    aliases = _load_dedup_aliases()
    if aliases is None:
        return              # a re-ask's live result cannot be ruled out: the marks wait
    held = _load_held()
    if held is None:
        return              # a held task's mark cannot be ruled out: the marks wait
    # A held task's end (a withdrawn ask's `failed`) retires its own mark, last.
    staged = {e.get("broker_id") for e in held.values() if isinstance(e, dict)}
    root = _delivery_core().backend.root
    for broker_id, local in sorted(marks.items()):
        if not (_valid_tid(broker_id) and isinstance(local, str) and _valid_local_tid(local)):
            continue
        if broker_id in staged or (read_item(root, broker_id) or {}).get("status") != "DELIVERED":
            continue
        # A re-ask answers under its original's id, so its live result keeps the mark too.
        answers = {local} | {k for k, v in aliases.items() if v == local and _valid_local_tid(k)}
        if not any(os.path.lexists(RESULTS_DIR / f"{t}.txt") for t in answers):
            _forget_ack_first(broker_id)


def _parked_copy(local: str) -> "Path | None":
    """The newest copy in `results/undelivered/` of the result for the task `local` names,
    filed under `local` or under its re-ask's id, as `<stem>-<epoch>.txt`. Not sutando's
    `find_quarantined`, which looks for `task-ask-…` when given an `ask-…` id."""
    newest = None
    for stem in (local, _reask_id(local)):
        try:
            names = [p.name for p in UNDELIVERABLE_RESULTS_DIR.glob(f"{glob.escape(stem)}-*.txt")]
        except OSError:
            continue
        for name in names:
            m = re.fullmatch(rf"{re.escape(stem)}-(\d+)\.txt", name)
            if m and (newest is None or int(m.group(1)) > newest[0]):
                newest = (int(m.group(1)), UNDELIVERABLE_RESULTS_DIR / name)
    return newest[1] if newest else None


def _finish_relay_end(core, broker_id: str, local: str, record: dict, inflight: "set[str]") -> None:
    """At start, finish the end of a relay result the broker refused for good that a crash cut
    short: its mark is retired last, so it is still here. In the same order as the drain: each
    live result quarantined, each task settled and out of flight on disk, then the mark."""
    aliases = _load_dedup_aliases()
    if aliases is None:
        return                                  # a re-ask cannot be told: the next start finishes it
    answers = {local} | {k for k, v in aliases.items() if v == local and _valid_local_tid(k)}
    for tid in sorted(answers):
        rfile = RESULTS_DIR / f"{tid}.txt"
        if os.path.lexists(rfile):
            _dispose_terminal(core, broker_id, tid, record, rfile)
            if os.path.lexists(rfile):
                return                          # not quarantined yet: the next start tries again
        if (tid in inflight or _task_pending(tid)) and not _settle_task(tid, inflight):
            return
    if not _save_inflight(inflight):
        return
    _log_relay_end(local, _forget_ack_first(broker_id), broker_id, *answers)


def _reclaim_parked_relay_results(inflight: "set[str]") -> None:
    """At start, take back each relay ask's result an older bridge parked after a downgrade.

    An older bridge parks a result its 10-minute window could not deliver and moves the
    body to `results/undelivered/`. On the primary lane it never settles the task (its
    lookup adds `task-` to an `ask-…` id); on a named instance, and for a re-ask on either
    lane, a restart settles it, and the id leaves the in-flight set. For each parked item a
    mark names, with no operator step: the item is requeued with its retry state reset,
    under the relay rule; the local id the mark names goes back in flight; only then is
    the newest copy moved, never over a file, to `results/<local id>.txt`, the name the
    drain reads, never a re-ask's name, whose alias may be gone. The drain then sends the
    item's stored payload, which carries no contract, so the broker treats it as private.
    A copy set aside on purpose has no parked item and stays for the operator."""
    marks = _load_ack_first()
    if not marks:
        return
    core = _delivery_core("relay")
    root = core.backend.root
    for broker_id, local in sorted(marks.items()):
        if not (_valid_tid(broker_id) and isinstance(local, str) and _valid_local_tid(local)):
            continue
        record = read_item(root, broker_id) or {}
        parked = record.get("status") == "PARKED"
        # A reclaim cut short before its move is finished by the next start.
        resumed = (record.get("status") in ("READY", "QUEUED")
                   and record.get("requeue_reason") == _RELAY_RECLAIMED)
        target = RESULTS_DIR / f"{local}.txt"
        if parked and is_refused_for_good(record.get("reason")):
            _finish_relay_end(core, broker_id, local, record, inflight)
            continue
        if not (parked or resumed) or os.path.lexists(target):
            continue
        copy = _parked_copy(local)
        if copy is None:
            if parked:
                _log(f"result {local}: its relay result is parked and no quarantined copy of it "
                     "was found; left for the operator")
            continue
        if parked and requeue_item_with_epoch(
                root, broker_id, reset_attempts=True, operator=core.worker,
                reason=_RELAY_RECLAIMED)[0] is not RequeueOutcome.REQUEUED:
            continue
        if local not in inflight and not _track_durably(local, inflight):
            _log(f"result {local}: its parked relay result could not be put back in flight; "
                 "the next start reclaims it")
            continue
        try:
            with disposal.locked(RESULTS_DIR):
                undelivered_quarantine.rename_noreplace(copy, target, _log)
        except FileExistsError as e:
            _log(f"result {local}: its parked relay result stays as {copy.name}: "
                 + ("this platform has no no-replace rename" if undelivered_quarantine.no_primitive(e)
                    else f"a live result already holds {target.name}"))
            continue
        except OSError as e:
            _log(f"result {local}: its parked relay result could not be moved back ({diagnostics.describe(e)}); "
                 "the next start reclaims it")
            continue
        _log(f"result {local}: reclaimed the relay result an older bridge parked ({copy.name}); "
             f"it is sent under {broker_id}, with no ceiling")


# The served task behind each staged `ack_first` task, for the publish that follows its ack.
_HELD_TASKS: "dict[str, tuple[dict, str, str]]" = {}
# Entries this process logged as malformed or with their staged file gone: each is reported once
# per process, by intent. Never cleared: it grows with those error cases only, not with traffic.
_HELD_REPORTED: "set[str]" = set()
# Entries the last pass could neither publish nor clear, reported in the gateway status.
_HELD_UNRESOLVED: "set[str]" = set()
# A task still staged this long after it was staged is reported too; it keeps being retried.
ACK_FIRST_UNRESOLVED_AFTER_S = 600


def _held_dir() -> Path:
    """Where `ack_first` tasks wait for their ack: on the queue's filesystem, outside its globs."""
    return TASKS_DIR / f".ack-first{_INST_SUFFIX}"


def _load_held() -> "dict | None":
    """The manifest of staged `ack_first` tasks, or None when it cannot be read."""
    if not ACK_FIRST_MANIFEST_FILE.exists():
        return {}
    try:
        loaded = json.loads(ACK_FIRST_MANIFEST_FILE.read_text())
    except (OSError, ValueError) as exc:
        _log(f"ack_first manifest unreadable ({diagnostics.describe(exc)}) — nothing staged is acked or published")
        return None
    return loaded if isinstance(loaded, dict) else None


def _put_held(tid: str, entry: "dict | None") -> bool:
    """Commit one manifest entry, or remove it with None. Only the poll loop writes it."""
    held = _load_held()
    if held is None:
        return False
    if entry is not None:
        held[tid] = entry
    elif held.pop(tid, None) is None:
        return True
    return _durable_write(ACK_FIRST_MANIFEST_FILE, json.dumps(held, sort_keys=True))


def _held_entry_ok(tid: str, entry) -> bool:
    """An entry names only its own task's files: its staged file and its queue file."""
    if not (_valid_local_tid(tid) and isinstance(entry, dict)):
        return False
    staged = entry.get("staged")
    dest = f"{tid}.txt"
    # The unprefixed form is what a bridge before the shared publisher staged; its entries stay valid.
    legacy = isinstance(staged, str) and staged.startswith(f"{dest}.") and staged.endswith(".tmp")
    return (entry.get("broker_id") == _broker_tid(tid) and entry.get("dest") == dest
            and isinstance(staged, str) and Path(staged).name == staged
            and (_is_staged_for(staged, dest) or legacy))


def _held_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _drop_staged(staged: Path) -> None:
    """Remove a staged `ack_first` task file, which the core never saw."""
    staged.unlink(missing_ok=True)


def _sweep_held_dir() -> None:
    """At start, remove each staged file no manifest entry names: a crash came before its
    entry, or after a discard or a newer serve had moved the entry on."""
    held = _load_held()
    if held is None:
        return
    named = {e.get("staged") for e in held.values() if isinstance(e, dict)}
    try:
        files = [f for f in _held_dir().iterdir() if f.is_file()]
    except OSError:
        return
    for staged in files:
        if staged.name not in named:
            _drop_staged(staged)
            _log(f"removed staged file {staged.name}: no ack_first manifest entry names it")


def _hold_staged(tid: str, tmp: Path, task_text: str, task: dict,
                 sender_tier: str) -> "tuple[str, bool] | None":
    """Commit the manifest entry of a staged `ack_first` task, which stays unpublished until
    its ack is accepted; every later step resumes from the entry. None, with nothing staged,
    when the staged file's directory entry or the manifest entry does not commit."""
    held = _load_held()
    entry = {"broker_id": _broker_tid(tid), "lease_id": _lease_id_of(task),
             "staged": tmp.name, "dest": f"{tid}.txt", "sha256": _held_digest(task_text),
             "accepted": False}
    if (held is None or not _fsync_in_place(tid, _held_dir(), TASKS_DIR)
            or not _put_held(tid, entry)):
        _drop_staged(tmp)
        _log(f"ack_first stage FAILED for {tid} — not staged, not acked")
        return None
    previous = held.get(tid)
    if _held_entry_ok(tid, previous) and previous["staged"] != tmp.name:
        # Served again before its ack: only the newest serve is acked and published.
        _drop_staged(_held_dir() / previous["staged"])
    _HELD_TASKS[tid] = (task, sender_tier, task_text)
    _log(f"staged {tid} until its ack is accepted (ack_first)")
    return tid, True


def _held_published(tid: str, entry: dict, dest: Path) -> bool:
    """A matching task file, a claimed or archived copy, or a result for the task."""
    try:
        if dest.exists():
            return _held_digest(dest.read_text(encoding="utf-8")) == entry.get("sha256")
    except (OSError, ValueError):
        return False
    return (_task_pending(tid) or _archived_task_file(tid) is not None
            or os.path.lexists(RESULTS_DIR / f"{tid}.txt") or _delivered_copy_exists(tid)
            or bool(undelivered_quarantine.find_quarantined(RESULTS_DIR, tid)))


def _ack_verdict(tid: str, entry: dict) -> str:
    """The verdict on a staged task's ack (accepted, refused or retry), asking the broker's
    state when the ack cannot settle it. Never refused for an ask the broker holds acknowledged."""
    outcome = _post_ack_first(tid, entry.get("lease_id"))
    if outcome == "accepted":
        return "accepted"
    if outcome == "stale":
        return "refused"        # an acknowledged ask is never served again, so this one was not
    state = _ack_first_state(tid)
    if state in ("withdrawn", "dead", "result_ready"):
        return "refused"        # ended, or answered elsewhere: this copy never ran
    # With no lease at all, `acknowledged` is this serve's; any other serve's would answer stale.
    if state == "acknowledged" and outcome == "gone":
        return "accepted"
    return "retry"


def _discard_held(tid: str, entry: dict, staged: Path, inflight: "set[str]") -> None:
    """A refused ack: the task never reached the core, so nothing of it is kept. The entry
    records the decision first and goes last, so a crash midway resumes the discard."""
    if entry.get("refused") is not True and not _put_held(tid, {**entry, "refused": True}):
        return                                  # the next pass re-acks and decides again
    _drop_staged(staged)
    _HELD_TASKS.pop(tid, None)
    _forget_task_media(tid)
    # Marks outlive tasks whose result may still come; a task refused before it ran has none.
    if not _forget_ack_first(entry["broker_id"]):
        return
    inflight.discard(tid)
    if not (_save_inflight(inflight) and _put_held(tid, None)):
        return
    _log_backoff_forget(tid)
    _log(f"ack_first task {tid}: ack refused (withdrawn, expired or served again) "
         "— discarded, never published")


def _clear_held(tid: str, why: str = "") -> bool:
    """Remove a published task's entry, only once both directories hold its rename: a power
    loss could otherwise bring the staged name back with no entry to say it was published."""
    if not (_fsync_in_place(tid, _held_dir(), TASKS_DIR) and _put_held(tid, None)):
        return False
    _HELD_TASKS.pop(tid, None)
    _log_backoff_forget(tid)
    if why:
        _log(f"ack_first task {tid}: {why}")
    return True


def _advance_held(tid: str, entry: dict, inflight: "set[str]") -> bool:
    """Move a staged `ack_first` task on: published once its ack is accepted and recorded,
    discarded once it is refused, otherwise left staged for the next pass. True once published."""
    staged, dest = _held_dir() / entry["staged"], TASKS_DIR / entry["dest"]
    if entry.get("refused") is True:
        _discard_held(tid, entry, staged, inflight)
        return False
    if entry.get("ending") is True:
        _end_withdrawn_relay_ask(tid, entry, staged, inflight)
        return False
    if not staged.exists():
        # `_publish_staged` consumes the staged file, so only a publish removes it.
        if _held_published(tid, entry, dest):
            _clear_held(tid, "already published — manifest entry cleared")
            return False
        _HELD_UNRESOLVED.add(tid)
        if tid not in _HELD_REPORTED:
            _HELD_REPORTED.add(tid)
            _log(f"ERROR ack_first task {tid}: staged file gone and no copy of the task or its "
                 "result found — manifest entry kept, nothing guessed; reported in the gateway status")
        return False
    if entry.get("accepted") is not True:
        # The mark and the in-flight set are on disk before the ack, as `durable` claims.
        if not _record_ack_first(tid):
            return False
        if tid not in inflight and not _track_durably(tid, inflight):
            return False
        verdict = _ack_verdict(tid, entry)
        if verdict == "refused":
            _discard_held(tid, entry, staged, inflight)
            return False
        # An acceptance that is not on disk keeps the task staged: the next pass acks again.
        if verdict != "accepted" or not _put_held(tid, {**entry, "accepted": True}):
            return False
    else:
        if tid not in inflight and not _track_durably(tid, inflight):
            return False
        if _held_published(tid, entry, dest):
            # A power loss brought the staged name back after its publish: never publish twice.
            _drop_staged(staged)
            _clear_held(tid, "already published — the staged copy left beside it was dropped")
            return False
    # After the ack, before the publish: the entry stays held and accepted, so
    # exactly-once is untouched and only the publication waits.
    if _staged_tier_withdrawn(tid, staged):
        _end_withdrawn_relay_ask(tid, entry, staged, inflight)
        return False
    if not _publish_staged(staged, dest):
        return False
    served = _HELD_TASKS.pop(tid, None)
    if served is not None:
        _on_published(tid, dest, served[2], served[0], served[1])
    else:
        _log(f"queued {tid} (staged and acknowledged before a restart)")
        _fire_on_task(tid, dest)
    _clear_held(tid)
    return True


def _advance_held_tasks(inflight: "set[str]") -> bool:
    """Every pass: retry a staged `ack_first` task's ack, and after a restart publish an
    accepted one once or discard a refused one. True when any was published."""
    published = False
    _HELD_UNRESOLVED.clear()
    for tid, entry in sorted((_load_held() or {}).items()):
        if _held_entry_ok(tid, entry):
            published |= _advance_held(tid, entry, inflight)
            continue
        _HELD_UNRESOLVED.add(tid)
        if tid not in _HELD_REPORTED:
            _HELD_REPORTED.add(tid)
            _log(f"ERROR ack_first manifest entry for {tid!r} is malformed — left as it is; "
                 "reported in the gateway status")
    for tid, entry in (_load_held() or {}).items():
        if _held_entry_ok(tid, entry) and _held_overdue(_held_dir() / entry["staged"]):
            _HELD_UNRESOLVED.add(tid)
            _log_backoff((tid, "overdue"), f"ERROR ack_first task {tid}: still staged "
                         f"{ACK_FIRST_UNRESOLVED_AFTER_S} s after it was staged — reported in the "
                         "gateway status, still retrying")
    return published


def _staged_policy_tier(staged: Path) -> "tuple[str, str] | None":
    """The access tier frozen into a staged task and the sender it names, or None when
    that cannot be proven: the file cannot be read or parsed, or names no known tier.

    Read from the ARTIFACT, not from the manifest entry: the staged body is what a
    resume publishes, so it is what has to be re-examined, and a task staged by an
    older build is then covered by the same check without a schema migration. The
    trusted parser is the correct one by writer — this module wrote the file and
    newline-strips every value, and it writes the locally-decided `access_tier`
    last precisely so it beats anything the remote side claimed earlier.
    """
    try:
        text = staged.read_text(encoding="utf-8", errors="replace")
        headers = local_task_protocol.parse_task_headers_trusted(text).headers
    except Exception:  # noqa: BLE001 - unprovable, so withheld by the caller, never published
        return None
    frozen = local_task_protocol.canonical_access_tier(headers.get("access_tier"))
    if frozen not in _TIER_RANK:
        return None     # every render writes a known tier, so a missing one is damage
    return frozen, (headers.get("user_id") or "")


def _staged_tier_withdrawn(tid: str, staged: Path) -> bool:
    """True when a staged task must wait: the capability it froze is no longer granted
    locally, or what it froze cannot be proven.

    A staged task freezes its whole policy envelope at stage time — the
    `access_tier` line, that tier's in-band guardrail, the owner-only prelude —
    and a resume publishes the text verbatim, so a cap the operator lowered in
    between would be bypassed by replay alone. Re-resolving the frozen tier
    through `_tier_for` cannot escalate it (the resolver takes the lower of
    attested and local), so a drop means local access was genuinely withdrawn.
    An envelope that cannot be proven (unreadable, unparsable, no known tier) is
    withheld too and checked again on every pass: a transient read error delays
    the task and never publishes an envelope nobody could check.
    """
    frozen = _staged_policy_tier(staged)
    if frozen is None:
        _HELD_UNRESOLVED.add(tid)
        _log_backoff((tid, "tier-unproven"),
                     f"ERROR ack_first task {tid}: the tier its staged file froze cannot be read "
                     "— kept staged, not published, checked again every pass. Reported in the "
                     "gateway status")
        return True
    now = _tier_for(frozen[1], frozen[0])
    if _TIER_RANK[now] >= _TIER_RANK[frozen[0]]:
        return False
    _HELD_UNRESOLVED.add(tid)
    _log_backoff((tid, "tier-withdrawn"),
                 f"ERROR ack_first task {tid}: staged as {frozen[0]} but local access is now "
                 f"{now} — kept staged, not published; publishing would hand it the capability "
                 "that was withdrawn. Reported in the gateway status")
    return True


def _held_overdue(staged: Path) -> bool:
    """Staged longer than `ACK_FIRST_UNRESOLVED_AFTER_S`: its file is written once, when staged."""
    try:
        return time.time() - staged.stat().st_mtime >= ACK_FIRST_UNRESOLVED_AFTER_S
    except OSError:
        return False


# Why a held relay ask was ended unpublished: the tier it froze dropped and stayed dropped.
_HELD_ENDED_REASON = "local access withdrawn"


def _withdrawn_for_good(staged: Path) -> "tuple[str, str] | None":
    """(the tier frozen, the tier now) for a held ask whose local access definitely dropped
    and stayed dropped past the threshold. None while it may yet publish or be proven."""
    frozen = _staged_policy_tier(staged)
    if frozen is None or not _held_overdue(staged):
        return None
    now = _tier_for(frozen[1], frozen[0])
    return (frozen[0], now) if _TIER_RANK[now] < _TIER_RANK[frozen[0]] else None


def _end_withdrawn_relay_ask(tid: str, entry: dict, staged: Path, inflight: "set[str]") -> None:
    """A held relay ask is already acknowledged, so withheld for good it would wait forever
    and no outcome would ever be posted. Once its local access has stayed withdrawn past the
    threshold, it is ended: the decision recorded in its entry, the relay `failed` result
    sent with no ceiling, then the staged file, the in-flight id and the entry retired, the
    mark last, each step resumed by the next pass after a crash. It is never published.
    A mark that cannot be retired here is retired by the next start: for a delivered `failed`
    by `_retire_delivered_marks`, which keeps any mark an entry names and so relies on
    `_put_held(tid, None)` above, and for one refused for good by
    `_reclaim_parked_relay_results` through `_finish_relay_end`."""
    if entry.get("ending") is not True:
        dropped = _withdrawn_for_good(staged)
        if dropped is None:
            return
        entry = {**entry, "accepted": True, "ending": True}   # reached only once the ack is accepted
        if not _put_held(tid, entry):
            return
        _log(f"WARNING ack_first task {tid}: {_HELD_ENDED_REASON} (staged as {dropped[0]}, now "
             f"{dropped[1]}) for {ACK_FIRST_UNRESOLVED_AFTER_S} s — ended with a `failed` result, "
             "never published")
    broker_id = entry["broker_id"]
    if not _relay_failed_sent(broker_id):
        return
    _drop_staged(staged)
    _HELD_TASKS.pop(tid, None)
    _forget_task_media(tid)
    inflight.discard(tid)
    if not (_save_inflight(inflight) and _put_held(tid, None)):
        return
    if _forget_ack_first(broker_id):
        _forget_relay_counts(tid, broker_id)
        _log(f"ack_first task {tid}: ended unpublished ({_HELD_ENDED_REASON}); its relay mark is retired")
    else:
        _log(f"ack_first task {tid}: ended unpublished, but its relay mark could not be retired; "
             "the next start retires it")


def _relay_failed_sent(broker_id: str) -> bool:
    """Send the relay `failed` result for an ask that never ran, retried as any relay result
    is. True once the broker has it, or has refused it for good: nothing more can be sent."""
    core = _delivery_core("relay")
    payload = json.dumps({"id": broker_id, "body": "", "status": "failed",
                          **_result_contract(False)}).encode("utf-8")
    core.backend.publish(broker_id, payload)       # once: an existing record is never replaced
    record = read_item(core.backend.root, broker_id) or {}
    if record.get("status") not in ("DELIVERED", "PARKED"):
        core.deliver_one(broker_id, payload)
        record = read_item(core.backend.root, broker_id) or {}
    if record.get("status") == "DELIVERED" or is_refused_for_good(record.get("reason")):
        return True
    if record.get("status") == "PARKED":           # a park the relay rule could not prevent
        requeue_item_with_epoch(core.backend.root, broker_id, reset_attempts=True,
                                operator=core.worker, reason=_RELAY_REOPENED)
    return False


_POLL_ANOMALIES = 0
_POLL_ANOMALY_LINE = ""  # the last anomaly's line: built from our own fields, logged by the loop


class _PollAnomaly(Exception):
    """A 200 poll answer that carries no task list; the loop backs off as for any bad answer."""


def _served_key(task: dict) -> str:
    return _one_line(task.get("id"))[:128]


def _poll_task_list(resp) -> list:
    """The poll's served tasks. A 200 with no `tasks` list (an empty body, or other JSON) is
    counted and raised: it can stand for a batch the broker served and the client never saw."""
    global _POLL_ANOMALIES, _POLL_ANOMALY_LINE
    tasks = resp.get("tasks") if isinstance(resp, dict) else None
    if isinstance(tasks, list):
        return tasks
    _POLL_ANOMALIES += 1
    meta = getattr(_RESP_META, "last", None) or {}
    shape = "an empty body" if resp == {} else f"{type(resp).__name__} without a tasks list"
    _POLL_ANOMALY_LINE = (
        f"poll anomaly #{_POLL_ANOMALIES}: answered {shape} (status={meta.get('status')}, "
        f"content-length={meta.get('content_length')}, cf-ray={meta.get('cf_ray')}) "
        "— any task served in it is lost to this client until its lease expires")
    raise _PollAnomaly()


def _receive_round(tasks: list) -> "tuple[list, dict]":
    """Log each served task at receipt, before any branching; returns the tasks to take and
    their fates (None until the round settles one)."""
    taken, fates = [], {}
    for task in tasks:
        if not isinstance(task, dict):
            _log(f"served entry is a {type(task).__name__}, not a task — dropped")
            continue
        key = _served_key(task)
        _log(f"served {key} lease={_one_line(_lease_id_of(task) or 'none')}"
             + (" ack_first" if task.get("ack_first") is True else ""))
        taken.append(task)
        fates[key] = None
    return taken, fates


def _retry_dropped_acks(dropped: list, received_at: float, fates: dict) -> None:
    """One more try for each ack lost to a dropped connection, inside ACK_RETRY_BUDGET_S of
    the poll's answer; whatever the budget cannot cover stays with the pending-ack retry."""
    for n, (tid, durable, lease) in enumerate(dropped):
        left = received_at + ACK_RETRY_BUDGET_S - time.monotonic() - ACK_DROP_RETRY_S
        if left < 1:
            _log(f"{len(dropped) - n} dropped ack(s) left to the pending-ack retry: the "
                 f"{ACK_RETRY_BUDGET_S:g}s retry budget is spent")
            return
        time.sleep(ACK_DROP_RETRY_S)
        if _post_task_ack(tid, durable, lease, retry_inline=False,
                          deadline=received_at + ACK_RETRY_BUDGET_S):
            fates[_broker_tid(tid)] = "acked"


def _settle_round(fates: dict) -> None:
    """Every served task ends the round acked, staged, consumed or dropped with a reason."""
    for key, fate in fates.items():
        if fate is None:
            _log(f"served {key} was not acked, staged, consumed or dropped this round "
                 "— the broker re-serves it when its lease expires")


def _isolated(task: dict, take, *args, fates: "dict | None" = None) -> bool:
    """`take(*args)`, where a raise costs only this task: it is logged and left unacked for
    the broker to re-serve, and the rest of its batch is still taken and acked."""
    try:
        return bool(take(*args))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise                     # the loop's auth recovery owns a rejected bearer
        why = diagnostics.describe(e)
    except Exception as e:  # noqa: BLE001 — one task must not withhold its batch's acks
        why = diagnostics.describe(e)
    if fates is not None:
        fates[_served_key(task)] = "dropped"
    _log(f"task {_served_key(task)} not taken ({why}) — not acked; the broker re-serves it")
    return False


def _take_served_task(task: dict, inflight: "set[str]", pending_ack: list,
                      fates: "dict | None" = None) -> bool:
    """One served task that is not `ack_first`: consumed as a control, or queued and its
    ack added to `pending_ack`. True when it newly entered the in-flight set."""
    hitl_out = _handle_hitl_action(task)
    if hitl_out:
        body = _hitl_control_body(hitl_out)
        _queue_review_control_result(task, body=body)
        _retry_review_control_results()
        _log(f"hitl: consumed card click {task.get('id')} from the task relay ({hitl_out})")
        _set_fate(fates, task, "consumed")
        return False
    if _handle_review_decision(task):
        _queue_review_control_result(task)
        _retry_review_control_results()
        _log(f"consumed private review decision {task.get('id')}")
        _set_fate(fates, task, "consumed")
        return False
    written = _write_task(task)
    if not written:
        _set_fate(fates, task, "dropped")       # _write_task logged why
        return False
    tid, durable = written
    pending_ack.append((tid, durable, _lease_id_of(task)))
    fresh = tid not in inflight
    inflight.add(tid)
    return fresh


def _set_fate(fates: "dict | None", task: dict, fate: str) -> None:
    if fates is not None:
        fates[_served_key(task)] = fate


def _take_ack_first(task: dict, inflight: "set[str]", fates: "dict | None" = None,
                    dropped: "list | None" = None) -> bool:
    """A task served with `ack_first`: staged, acked on its own and published only once
    the ack is accepted, so a withdrawn ask never reaches the core. True once published."""
    written = _write_task(task, hold=True)
    if written is None:
        _set_fate(fates, task, "dropped")       # _write_task logged why
        return False
    tid, durable = written
    held = _load_held()
    if held is None:
        _set_fate(fates, task, "dropped")       # _load_held logged why
        return False                            # nothing is acked while it cannot be read
    entry = held.get(tid)
    if not _held_entry_ok(tid, entry):
        # Already queued or handled here: acked as any redelivery is.
        lease = _lease_id_of(task)
        if (_track_durably(tid, inflight) and _record_pending_acks([(tid, durable, lease)])
                and _post_task_ack(tid, durable, lease, dropped=dropped)):
            _set_fate(fates, task, "acked")
        return False
    published = _advance_held(tid, entry, inflight)
    if published:
        _set_fate(fates, task, "acked")
    elif tid in (_load_held() or {}):
        _set_fate(fates, task, "staged")
    else:
        _set_fate(fates, task, "dropped")       # _advance_held logged the refusal
    return published


def _fold_target(tid: str, wire: str, holder: "str | None") -> "str | None":
    """What an honoured fold posted under `wire` sends as `consolidated_into`.

    The holder's BROKER id, resolved as result egress resolves its own, for a task
    whose `ack_first` mark is kept; "" for every other task, which closes with the
    bare marker as before (the broker already acts on the field for any task, so
    typed and Signal tasks must never get it); None to defer while the marks (for a
    voice ask) or the alias ledger cannot be read.
    """
    relay = _relay_result(tid, wire)
    if not relay:
        return None if relay is None else ""
    delivery = _delivery_tid((holder or "").strip())
    if delivery is None:
        _log(f"delivery deferred for {tid} — alias ledger unreadable")
        return None
    target = _broker_tid(delivery)
    if not (gateway_instance.owns(delivery, GATEWAY_INSTANCE) and _valid_tid(target)):
        # Another instance's task, or none: this broker never issued that id.
        _log(f"dedup honour for {tid}: holder {target!r} is not a task of this gateway — "
             "closed with the marker, without consolidated_into")
        return ""
    return target


def _task_source(tid: str) -> "str | None":
    """The `source:` header of tid's task file, queued or archived; None when unknown.
    A re-ask's file carries its original's headers."""
    tfile = find_task_file(TASKS_DIR, tid) or find_archived_task(TASKS_DIR, tid)
    if tfile is None:
        return None
    try:
        text = tfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return local_task_protocol.parse_task_headers_trusted(text).headers.get("source")


def _read_attachment(path_str: str) -> "tuple[str, str, str]":
    """(basename, base64 bytes, "") for an allowlisted readable file, or
    ("", "", reason) — the refusals both upload routes report in-band."""
    fpath = os.path.realpath(os.path.expanduser(path_str.strip()))
    if not is_path_sendable(fpath):
        return "", "", "path not allowlisted"
    try:
        size = os.path.getsize(fpath)
    except OSError as e:
        return "", "", f"stat failed: {e}"
    if size > MAX_MEDIA_BYTES:
        return "", "", f"file exceeds {MAX_MEDIA_BYTES} bytes"
    try:
        with open(fpath, "rb") as f:
            return os.path.basename(fpath), base64.b64encode(f.read()).decode("ascii"), ""
    except OSError as e:
        return "", "", f"read failed: {e}"


def _upload_attachment(room: str, path_str: str) -> tuple[bool, str]:
    """Upload one allowlisted local file to the task's room via the gateway
    media endpoint. Returns (ok, reason)."""
    name, content_b64, reason = _read_attachment(path_str)
    if reason:
        return False, reason
    safe_room = urllib.parse.quote(room, safe="")
    try:
        _req("POST", f"/v1/rooms/{safe_room}/media",
             {"filename": name, "content_b64": content_b64}, timeout=_UPLOAD_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return False, f"network error: {e}"
    return True, ""


def _upload_task_attachment(wire_id: str, ordinal: int, path_str: str) -> "tuple[str, str]":
    """Upload one attachment against a Signal task's own lease. Returns
    ("ok"|"note"|"defer", reason).

    "defer" holds the WHOLE result for the next scan rather than collapsing to
    an in-band note: the retry has to re-offer the same wire id, ordinal and
    bytes for the relay to resume the upload it may already have recorded.
    """
    name, content_b64, reason = _read_attachment(path_str)
    if reason:
        return "note", reason
    safe_tid = urllib.parse.quote(wire_id, safe="")
    try:
        _req("POST", f"/v1/tasks/{safe_tid}/media",
             {"ordinal": ordinal, "filename": name, "content_b64": content_b64},
             timeout=_UPLOAD_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return "note", "content conflicts with the recorded upload"
        if e.code == 423:
            return "note", "room is encrypted"
        if e.code >= 500:
            return "defer", f"HTTP {e.code}"
        return "note", f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return "defer", f"network error: {e}"
    return "ok", ""


def _archive_result(path: Path, tid: str, generation) -> bool:
    """Retire a sent result: the lifecycle owner moves only the publication
    this pass read and sent, so a reply that replaced it meanwhile stays live.
    False = the id must stay looked for; the caller keeps its bookkeeping."""
    if not _intend_settle(tid):
        disposal.report_once(f"intent:{tid}", _log,
                             f"result {tid}: sent, but its settle intent could not be saved; nothing "
                             "was moved, and the next pass tries again")
        return False
    done = disposal.retire_generation(RESULTS_DIR, path, generation, _log, ARCHIVE_RESULTS_DIR,
                                      _names(f"{tid}-{int(time.time())}"))
    if done.outcome is disposal.Retirement.REPLACEMENT_LIVE:
        _log(f"result {tid}: the reply this pass sent was replaced at its name before it was "
             "archived; the newer one stays live")
        return False
    if done.outcome is disposal.Retirement.FALLBACK:
        disposal.report_once(f"archive:{tid}", _log,
                             f"result {tid}: sent, but kept outside the archive: {done.cause}")
        return False
    if not done.retired:
        disposal.report_once(f"archive:{tid}", _log,
                             f"result {tid}: sent, but could not be archived ({done.cause})")
        return False
    if not _archive_task_file(tid):
        _task_left_executable(tid)
        return False
    _settled(tid)
    return True


def _archive_task_file(tid: str) -> bool:
    """Move a served task out of the queue. True once no file of it is left there;
    a rename the core raced to first counts, one that failed does not."""
    tfile = find_task_file(TASKS_DIR, tid)
    if tfile is not None:
        archive_dir = TASKS_DIR / "archive"
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            tfile.rename(archive_dir / f"{tid}.txt")
        except OSError:
            pass
    return find_task_file(TASKS_DIR, tid) is None


# Ids whose result is disposed of but whose task could not be archived yet; each
# drain pass retries them. A restart rebuilds the set by scanning each tracked id once.
_SETTLE_PENDING: "set[str]" = set()
_SETTLE_SCANNED: "set[str]" = set()
_SETTLE_LOADED = False
_SETTLE_MUTEX = threading.RLock()


def _settle_pending_file() -> Path:
    return _STATE / f"remote-task-settle-pending{_INST_SUFFIX}.json"


def _read_settle_ledger(path: Path) -> "str | None":
    """The ledger's text, None when there is none; any other failure raises."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _load_settle_pending() -> bool:
    """Merge, once per process, the ids a previous process could not settle.
    False while an existing ledger cannot be read: it must not be overwritten."""
    global _SETTLE_LOADED
    with _SETTLE_MUTEX:
        if _SETTLE_LOADED:
            return True
        try:
            text = _read_settle_ledger(_settle_pending_file())
            ids = [] if text is None else json.loads(text)["ids"]
            if not isinstance(ids, list):
                raise ValueError("ids is not a list")
        except (OSError, ValueError, KeyError, TypeError) as e:
            disposal.report_once("settle-ledger", _log,
                                 f"the settle-intent ledger could not be read ({diagnostics.describe(e)}); it is left as it is "
                                 "and nothing is written over it until it can be")
            return False
        _SETTLE_PENDING.update(t for t in ids if isinstance(t, str) and _valid_local_tid(t))
        _SETTLE_LOADED = True                   # published only once the read is resolved
        return True


def _save_settle_pending() -> bool:
    """Load, merge and write as one transition; never over an unread ledger."""
    with _SETTLE_MUTEX:
        if not _load_settle_pending():
            return False
        return _durable_write(_settle_pending_file(), json.dumps({"ids": sorted(_SETTLE_PENDING)}))


def _intend_settle(tid: str) -> bool:
    """Record, before the sent result is moved, that this id was served: an
    alias whose result then vanishes has no copy of its own to prove it."""
    with _SETTLE_MUTEX:
        _SETTLE_PENDING.add(tid)
        return _save_settle_pending()


def _settled(tid: str) -> None:
    with _SETTLE_MUTEX:
        if tid in _SETTLE_PENDING:
            _SETTLE_PENDING.discard(tid)
            _save_settle_pending()


def _task_left_executable(tid: str) -> None:
    """The settle intent is itself durable: after a restart an alias whose result
    vanished has no copy of its own that would prove it disposed of."""
    with _SETTLE_MUTEX:
        _SETTLE_PENDING.add(tid)
        saved = _save_settle_pending()
    disposal.report_once(f"settle:{tid}", _log,
                         f"result {tid}: its result is disposed of but its task could not be "
                         "archived; it stays tracked and is retried each pass"
                         + ("" if saved else " (the retry intent could not be saved)"))


def _track_durably(tid: str, inflight: "set[str]") -> bool:
    """Make the id restart-durable in flight before anything of it is removed.
    Other threads see it only once the write holding it has committed."""
    with _INFLIGHT_MUTEX:
        if not _save_inflight(set(inflight) | {tid}):
            return False
        inflight.add(tid)
        return True


def _result_disposed(tid: str) -> bool:
    """This id's own result was archived or kept, or its own delivery succeeded;
    an alias is judged only by its own copies, never by its primary's record."""
    if _delivered_copy_exists(tid) or undelivered_quarantine.find_quarantined(RESULTS_DIR, tid):
        return True
    root = getattr(_delivery_core().backend, "root", None)
    delivery = _delivery_tid(tid)
    return (root is not None and delivery == tid
            and (read_item(root, _broker_tid(delivery)) or {}).get("status") == "DELIVERED")


def _settle_task(tid: str, inflight: "set[str] | None" = None) -> bool:
    """Retire the task of an id whose result is already disposed of. Nothing is
    forgotten until its file has left the executable queue."""
    if not _archive_task_file(tid):
        _task_left_executable(tid)              # every caller made the id durable before this
        return False
    _settled(tid)
    if inflight is not None:
        inflight.discard(tid)
    _forget_task_room(tid)
    aliases = _load_dedup_aliases() or {}
    if _delivery_tid(tid) == tid and not any(v == tid for k, v in aliases.items() if k != tid):
        _forget_task_media(tid)
    _forget_dedup_alias(tid)
    _log(f"result {tid}: its result is disposed of and its task is archived; the task is retired")
    return True


# A legacy bare `.sending` claim carries no owner info, so recovery for those
# falls back to an age guard: younger than this = possibly a live worker's
_ORPHAN_MIN_AGE_S = 600

# An empty body observed right after claiming is a writer mid-flush, so it is
# always re-queued — and NEVER moved to a terminal resting place.

# Filenames THIS process has already logged as claimed-empty, so a genuinely
# orphaned nudge is noted once instead of on every pass. Discarded when the file
_EMPTY_LOGGED: "set[str]" = set()
_GATE_FAILED_LOGGED: "set[str]" = set()


def _room_bound_result(name: str, room: "str | None") -> bool:
    """A task-bridge voice result addressed to a room: a gate that fails on
    one holds it, never delivers it unchecked."""
    return name.startswith("proactive-result-") and room is not None


def _gate_failed(name: str, exc: Exception) -> None:
    if name not in _GATE_FAILED_LOGGED:
        _GATE_FAILED_LOGGED.add(name)
        _log(f"proactive {name} held: room gate failed ({diagnostics.describe(exc)}) — a room-bound "
             "result is never delivered unchecked")

# Bodies above this never fit a Matrix event, so they are undeliverable no
# matter how often they are retried; they are dead-lettered instead of looping.
_PROACTIVE_MAX_BODY_B = 48 * 1024

_MATRIX_ROOM_RE = _delivery_destination.MATRIX_ROOM_RE


def _proactive_route(body: str) -> "tuple[str, str | None, str]":
    """The drain's routing of a proactive body; the policy is delivery_destination's."""
    return _delivery_destination.route(body, skip_after_channel=DELIVERY_SKIP_AFTER_CHANNEL)


def _proactive_thread_root(body: str, name: str = "") -> "str | None":
    """The `[thread:]` root a proactive body names, or None (top level). A
    malformed value is logged and posted top level, never into a guessed thread."""
    actions = parse_markers(body).actions
    for a in actions:
        if a.kind == "thread":
            return a.value
        if a.kind == "thread-invalid":
            _log(f"proactive {name} has a malformed [thread: {a.value[:80]!r}] "
                 "— posting top level")
            return None
        if a.kind == "thread-foreign":
            _log(f"proactive {name}: [thread: {a.value[:80]!r}] may not be in the "
                 f"destination room ({a.extra}) — posting top level")
            return None
    if any(a.kind == "thread-ask" for a in actions):
        _log(f"proactive {name}: a bare [thread] applies only to a task result "
             "— posting top level")
    return None


def _lane_identity() -> str:
    """The identity the deliverability predicate uses, republished whenever this instance's lane
    record does not say it or is due a heartbeat, so Hatch judges rooms by it without reading the
    credential env. A drain that cannot write lets the record go stale, and Hatch then refuses."""
    identity = _reenroll_identity()
    if not agent_identity.LANE_SUPPORTED:
        return identity
    rec = agent_identity.published_lane_record(_STATE, GATEWAY_INSTANCE) or {}
    if rec.get("pid") != os.getpid() or rec.get("agent_id") != identity.strip() \
            or rec.get("send_lock") != _delivery_destination.SEND_LOCK_PROTOCOL \
            or time.time() - rec.get("ts", 0) >= agent_identity.LANE_REFRESH_S:
        try:
            _LANE_OWNED.update(agent_identity.publish_lane(
                _STATE, GATEWAY_INSTANCE, identity, send_lock=_delivery_destination.SEND_LOCK_PROTOCOL))
            _LANE_OWNED["failing"] = False
        except OSError as e:  # publish_lane has already tried to invalidate the old record
            if not _LANE_OWNED.get("failing"):
                _log(f"lane identity record not written ({diagnostics.describe(e)}); Hatch holds fail closed once the "
                     f"record is {agent_identity.LANE_FRESH_S:g}s old")
            _LANE_OWNED["failing"] = True
    return identity


_LANE_OWNED: dict = {}


def _retract_own_lane() -> None:
    """At clean exit: invalidate the lane record only if it is still the one this process wrote."""
    if "pid" in _LANE_OWNED:
        agent_identity.retract_lane(_STATE, GATEWAY_INSTANCE, _LANE_OWNED["pid"], _LANE_OWNED["ts"])


def _own_homeserver() -> str:
    """The server this lane's agent lives on, from its live identity;
    "" when the identity is unknown (then nothing below can discriminate)."""
    return _delivery_destination.homeserver_of(_lane_identity())


def _room_is_deliverable_here(room: str) -> bool:
    return _delivery_destination.deliverable_here(room, _own_homeserver())


_EVENT_ID_RE = re.compile(r"\$\S+")
_DELIVERED_INFIX = ".delivered-"


def _log_dead_claim_released(rec) -> None:
    if getattr(rec, "malformed", False):
        # A malformed record has no trustworthy stamp; its file's mtime is the age it has.
        mtime = getattr(rec, "file_mtime", None)
        age = f"{int(time.time() - mtime)}s since written" if mtime else "age unknown"
        _log(f"proactive outbox item {rec.item_id}: released a malformed claim naming dead pid "
             f"{getattr(rec, 'candidate_pid', None) or rec.pid} ({age}) to park it")
        return
    if rec.pid is None or rec.pid < 0:
        _log(f"proactive outbox item {rec.item_id}: swept a torn claim past its grace period to park it")
        return
    _log(f"proactive outbox item {rec.item_id}: released the expired claim of dead drainer pid {rec.pid} "
         f"({int(time.time() - rec.claimed_at)}s old) to park it")


def _reap_hidden_delivered() -> None:
    """Each pass: a delivered proactive file kept hidden moves into archive/ once it can."""
    for h in RESULTS_DIR.glob(f".proactive-*{_DELIVERED_INFIX}*"):
        stem, _, stamp = h.name[1:].rpartition(_DELIVERED_INFIX)
        if not stamp.isdigit():
            continue
        try:
            undelivered_quarantine.place_as(h, ARCHIVE_RESULTS_DIR,
                                            (undelivered_quarantine.quarantine_name(stem, int(stamp) + i)
                                             for i in range(64)))
        except OSError as e:
            _log_backoff(("proactive-hidden-delivered", h.name),
                         f"delivered proactive {h.name} still not archived ({diagnostics.describe(e)})")
_ROOM_UPGRADED = "room-upgraded"
_DESTINATION_CHANGED = "destination-changed"


def _payload_room(payload) -> "str | None":
    try:
        return json.loads(payload or "{}").get("room_id")
    except (ValueError, AttributeError):
        return None


def _proactive_item_id(stem: str, body: str) -> str:
    return proactive_item_id(stem, body)


def _proactive_requeue_command(root, item_id: str, body_sha256: str) -> str:
    """The operator's resend: requeue the item and restore exactly its own quarantined body."""
    return shlex.join(["python3", "src/outbox_cli.py", "--root", str(root), "requeue", item_id,
                       "--body-id", item_id.rsplit("~", 1)[0], "--body-sha256", body_sha256])


def _proactive_outbox_root() -> Path:
    return RESULTS_DIR / PROACTIVE_OUTBOX_DIRNAME


def _record_proactive_receipt(item_id: str, event_id: str) -> None:
    """The event id a proactive send returned, kept on its outbox item before the outbox
    is told it was delivered; raises when it cannot be kept."""
    root = _proactive_outbox_root()
    room = json.loads((read_item(root, item_id) or {}).get("payload") or "{}").get("room_id")
    if not record_provider_ref(root, item_id, event_id, destination=room):
        raise OSError(f"proactive receipt for {item_id} not recorded")


def _about_task_headers(tid: str) -> dict:
    """The routing headers of the task a proactive message is about; {} when it cannot be read.
    The trusted scan applies only to a file this gateway wrote and declared task-mid."""
    path = find_archived_task(TASKS_DIR, tid)
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    strict = local_task_protocol.parse_task_headers(text).headers
    if strict.get("task_layout") == "mid":
        return local_task_protocol.parse_task_headers_trusted(text).headers
    return strict


def _task_thread_root(headers: dict, room: str) -> "str | None":
    """The thread a message about a task goes in: an ask inside a thread keeps that thread's
    root; a top-level ask is rooted at its own message. None outside the task's own room."""
    task_room = headers.get("source_room_id") or headers.get("channel_id")
    if not room or task_room != room:
        return None
    if headers.get("thread_root"):
        root = headers["thread_root"]
        return root if _EVENT_ID_RE.fullmatch(root) else None
    source = headers.get("source_message_id") or ""
    return source if _EVENT_ID_RE.fullmatch(source) else None


def _proactive_dispatch_veto(item_id: str, payload: bytes) -> "str | None":
    """Asked by the outbox right before the send: a message about a task that has been
    answered, has ended, waits on the owner or is being cancelled is withdrawn."""
    try:
        env = json.loads(payload.decode("utf-8"))
        tid = env.get("task_id")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None
    if not tid:
        return None
    if (RESULTS_DIR / f"{tid}.txt").exists() or _delivered_copy_exists(tid):
        return "the task has been answered"
    held = task_hold.line_veto(RESULTS_DIR, tid, about_task_ts(item_id.rsplit("~", 1)[0] + ".txt"))
    if held is not None:
        return held
    try:
        veto = PROACTIVE_DISPATCH_GATE(tid) if PROACTIVE_DISPATCH_GATE is not None else None
        if veto is None and PROACTIVE_ACK_GATE is not None:
            veto = PROACTIVE_ACK_GATE(tid, item_id)
        if veto is None and PROACTIVE_ROUTE_GATE is not None:
            veto = PROACTIVE_ROUTE_GATE(tid, env.get("room_id"), env.get("thread_root"))
        return veto
    except Exception as exc:  # noqa: BLE001 - an unchecked task is never messaged
        return f"the task could not be checked ({diagnostics.describe(exc)})"


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
        if len(owner) == 2:  # pid-scoped claim: .sending.<pid>, or .sending.<pid>.<ns>
            try:
                pid = int(owner[1].split(".", 1)[0])
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


def _hand_back(claim: Path, original: Path) -> None:
    """Return a claim to its live name, never over a newer file a producer wrote there; on a
    collision (or no no-replace rename) the claim keeps its name and the operator is told."""
    try:
        undelivered_quarantine.rename_noreplace(claim, original)
    except FileExistsError:
        _log_backoff(("proactive-handback", claim.name),
                     f"proactive {claim.name} not handed back: {original.name} is held by a newer file "
                     f"(or no no-replace rename here); it is kept as {claim.name} for the operator")
    except OSError as e:
        _log_backoff(("proactive-handback", claim.name),
                     f"proactive {claim.name} could not be handed back ({diagnostics.describe(e)})")


def _retire_proactive(claim: Path, original: Path, dest_dir: Path) -> bool:
    """Retire a claimed nudge that was NOT delivered, without destroying it.

    Used by the terminal non-delivery paths (withdrawn, parked, dead-lettered). Not
    shared with the delivered path: a delivered file that cannot be archived goes to a
    hidden `.<stem>.delivered-<ts>` no bridge globs (a live name would let another bridge
    post it again), reaped into archive/ by _reap_hidden_delivered. Here nothing was
    delivered, so the fallback is the opposite: hand it back as `.txt` rather than unlink;
    its outbox item stays terminal, so it is never resent from here.
    """
    try:
        # Never over an earlier copy: two bodies under one name are two retired generations.
        names = (undelivered_quarantine.quarantine_name(original.stem, time.time_ns() + i) for i in range(64))
        undelivered_quarantine.place_as(claim, dest_dir, names)
        return True
    except OSError as e:
        _log(f"could not retire proactive {original.name} ({diagnostics.describe(e)}) — re-queueing")
        _hand_back(claim, original)
        return False


def _upgrade_hold_file() -> Path:
    return _STATE / f"owner-dm-upgraded{_INST_SUFFIX}.json"


def _room_upgraded_to(err_body: str) -> "str | None":
    """The replacement room a 400 body names when the send room was upgraded, else None."""
    try:
        d = json.loads(err_body)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("reason") != "room_upgraded":
        return None
    room = d.get("replacement_room")
    # A gateway value: kept only as a room id holding no registered secret.
    ok = isinstance(room, str) and gateway_instance.MATRIX_ROOM_RE.fullmatch(room) and not diagnostics.contains_secret(room)
    return room if ok else ""


def _owner_dm_upgrade_held(room: str) -> bool:
    """True while `room` (the current owner DM) is held as upgraded; a hold on another room is stale."""
    f = _upgrade_hold_file()
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict) or not d:
        return False
    if not (room or "").strip():
        return False  # no owner DM to compare: refuse rather than clear the hold
    if d.get("room") == room:
        return True
    try:
        _atomic_private_json(f, {})  # the owner DM was repointed: the hold no longer applies
    except OSError:
        pass
    return False


# (room, replacement) pairs whose owner notice was delivered: no duplicate even if the hold file is unwritable.
_UPGRADE_NOTICES_SENT: "set[tuple[str, str]]" = set()


def _read_upgrade_hold() -> dict:
    try:
        d = json.loads(_upgrade_hold_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    if d.get("room") and "notice" not in d:
        d.update(notice="pending", notice_attempts=0)  # a pre-notice-state hold still owes its notice
    return d


def _hold_owner_dm_upgrade(room: str, replacement: str, noticed: "set | None" = None) -> None:
    """Record the hold (later passes skip the room), then try the owner notice if still owed."""
    d = _read_upgrade_hold()
    if (d.get("room"), d.get("replacement")) != (room, replacement):
        d = {"room": room, "replacement": replacement, "at": time.time(),
             "notice": "pending", "notice_attempts": 0}
        try:
            _atomic_private_json(_upgrade_hold_file(), d)
        except OSError as e:
            _log(f"could not record owner-DM upgrade hold ({diagnostics.describe(e)})")
        _log(f"owner DM {room} was upgraded to {replacement or '(unknown)'}, which this agent "
             "has not joined — holding owner-DM nudges until the owner DM is repointed")
    _notify_owner_dm_upgrade(d, noticed)


def _notify_owner_dm_upgrade(d: dict, noticed: "set | None" = None) -> None:
    """Deliver a pending owner notice; failures retry on later passes up to MAX_TRANSIENT_ATTEMPTS."""
    room, replacement = str(d.get("room") or ""), str(d.get("replacement") or "")
    if d.get("notice") != "pending" or (room, replacement) in _UPGRADE_NOTICES_SENT:
        return
    if noticed is not None:
        if (room, replacement) in noticed:
            return  # one attempt per (room, replacement) per drain pass, however many files it holds
        noticed.add((room, replacement))
    tried = int(d.get("notice_attempts") or 0) + 1
    d["notice_attempts"] = tried
    try:
        if OWNER_DM_UPGRADED_NOTIFY is not None:
            OWNER_DM_UPGRADED_NOTIFY(room, replacement)
        # "delivered" means the ask-owner hook was spawned, not that the owner received the question.
        d["notice"] = "delivered"
        _UPGRADE_NOTICES_SENT.add((room, replacement))
    except Exception as exc:  # noqa: BLE001
        if tried >= MAX_TRANSIENT_ATTEMPTS:
            d["notice"] = "gave_up"
            _log(f"owner-DM upgrade notify GAVE UP after {tried} attempt(s) ({diagnostics.describe(exc)}) — the owner was "
                 f"NOT told that {room} was upgraded to {replacement or '(unknown)'}")
        else:
            _log(f"owner-DM upgrade notify failed ({diagnostics.describe(exc)}) — will retry ({tried}/{MAX_TRANSIENT_ATTEMPTS})")
    try:
        _atomic_private_json(_upgrade_hold_file(), d)
    except OSError as e:
        _log(f"could not record owner-DM upgrade notice state ({diagnostics.describe(e)})")


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
    that belongs to another bridge (cross-bridge routing stays host policy); a
    PROACTIVE_ROOM_GATE re-judges the room the CLAIMED body names, since the
    peek may have read a body still being written."""
    noticed: "set[tuple[str, str]]" = set()
    _lane_identity()  # published each pass, before any file needs it
    _reap_hidden_delivered()
    for f in sorted(RESULTS_DIR.glob("proactive-*.txt")):
        # PEEK before claiming: a file explicitly routed to a non-Matrix
        # destination ([channel: <discord/slack id>]) belongs to that bridge —
        try:
            route, peek_room, _ = _proactive_route(f.read_text(encoding="utf-8"))
        except OSError:
            continue  # racing consumer already claimed it
        if route == "foreign":
            continue
        if route == "send" and peek_room is not None and not _room_is_deliverable_here(peek_room):
            continue  # another homeserver's lane owns it; a claim here can only park it
        # No target of its own AND no default: skip BEFORE claiming. Claiming it
        # would spin (claim -> no destination -> hand back) on every pass.
        if route == "send" and peek_room is None and GATEWAY_INSTANCE:
            continue  # a named secondary never owns unaddressed nudges; the primary runs without GATEWAY_INSTANCE
        if route == "send" and peek_room is None and not proactive_room():
            _held(f"owner-directed {f.name}")
            continue
        if route == "send" and peek_room is None and _owner_dm_upgrade_held(proactive_room()):
            _notify_owner_dm_upgrade(_read_upgrade_hold(), noticed)
            continue  # each claim would only cost another room_upgraded 400
        if PROACTIVE_CLAIM_GATE is not None:
            try:
                if not PROACTIVE_CLAIM_GATE(f):
                    continue  # another bridge's file right now; retry next pass
            except Exception as exc:  # noqa: BLE001
                # A plain owner nudge still claims; a room-bound result waits for the gate.
                if _room_bound_result(f.name, peek_room):
                    _gate_failed(f.name, exc)
                    continue
        # pid-scoped claim: recovery can tell a live worker's in-flight claim
        # from a dead one's (review blocker: bare .sending was stealable).
        claim = f.with_suffix(f".sending.{os.getpid()}.{time.time_ns()}")  # unique: a retained claim is never replaced
        try:
            f.rename(claim)  # atomic claim; loser of a race just misses
        except OSError:
            continue
        # Re-read and re-route AFTER the claim, and act only on THIS result: the
        # peek may have seen a writer mid-write, so the read goes through the readiness gate.
        claimed_text = read_ready_result(claim)
        if claimed_text is None:
            # Unreadable, empty, or still growing: hand back, never post a prefix or strand the claim.
            if f.name not in _EMPTY_LOGGED:
                _EMPTY_LOGGED.add(f.name)
                _log(f"proactive {f.name} not ready (empty, unreadable or still "
                     f"being written) — handing back for a later pass")
            _hand_back(claim, f)
            continue
        route, room_override, routed_body = _proactive_route(claimed_text)
        if route == "foreign" or (
                route == "send" and room_override is not None
                and not _room_is_deliverable_here(room_override)) or (
                route == "send" and room_override is None and (GATEWAY_INSTANCE or not proactive_room())):
            # Hand back rather than eat: a foreign target seen only post-claim, or one that vanished
            # and left an unaddressed body this instance may not own or cannot place.
            _hand_back(claim, f)
            continue
        if PROACTIVE_ROOM_GATE is not None and route == "send" and _room_bound_result(f.name, room_override):
            try:
                allowed = PROACTIVE_ROOM_GATE(f, room_override)
            except Exception as exc:  # noqa: BLE001
                _gate_failed(f.name, exc)
                allowed = False
            if not allowed:
                # Hand back under its own name: the claim gate holds it from here.
                _hand_back(claim, f)
                continue
            _GATE_FAILED_LOGGED.discard(f.name)
        if route == "send" and room_override and f.name.startswith(_OWNER_MENTION_PREFIX):
            # Its room was the owner DM when queued; send to the owner DM read now, or hold.
            room_override = resolve_destination(OWNER_PRIVATE)
            if not room_override:
                _held(f"owner-mention result {f.name}")
                _hand_back(claim, f)
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
            _hand_back(claim, f)
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
        dest_room = resolve_destination(CURRENT_ROOM, room_id=room_override) if room_override else resolve_destination(OWNER_PRIVATE)
        thread_root = _proactive_thread_root(claimed_text, f.name)
        about = about_task(f.name)
        if about:
            thread_root = _task_thread_root(_about_task_headers(about), dest_room) or thread_root
        envelope = {"room_id": dest_room, "body": body,
                    **({"thread_root": thread_root} if thread_root else {}),
                    **({"task_id": about} if about else {})}
        _send_proactive(claim, f, envelope, room_override is None, noticed)


def _send_proactive(claim: Path, f: Path, envelope: dict, owner_dm: bool, noticed: set) -> None:
    """Hand one claimed proactive file to the outbox, which owns the claim, the send and its
    outcome, then move the file as that outcome says. An uncertain send is parked, not resent."""
    core = _delivery_core("proactive")
    item_id = _proactive_item_id(f.stem, envelope["body"])
    payload = json.dumps(envelope, sort_keys=True).encode("utf-8")
    seen = _PROACTIVE_SEND.__dict__.setdefault("seen", {})
    seen.clear()
    core.backend.publish(item_id, payload)
    if (read_item(core.backend.root, item_id) or {}).get("reason") == _ROOM_UPGRADED:
        # Refused for an upgraded room, so it never posted: resent to the room resolved now.
        requeue_item_with_epoch(core.backend.root, item_id, reset_attempts=True, operator=core.worker,
                                reason="the room is no longer held", payload=payload.decode("utf-8"))
    stored = read_item(core.backend.root, item_id) or {}
    stored_room = _payload_room(stored.get("payload"))
    if (stored.get("status") in ("READY", "QUEUED") and stored_room != envelope["room_id"]
            and not dispatch_unsettled(stored) and not released_by_operator(stored)):
        # Held, never re-routed; only a person's release of this stored room sends it there.
        if not core.backend.park_unclaimed(item_id, _DESTINATION_CHANGED, on_release=_log_dead_claim_released):
            again = read_item(core.backend.root, item_id) or {}
            if again.get("status") not in core.backend.TERMINAL and not dispatch_unsettled(again):
                _hand_back(claim, f)  # another drainer holds it: nothing is sent from here
                _log_backoff(("proactive-held-claimed", item_id),
                             f"proactive {f.name} not sent: its destination moved from {stored_room} to "
                             f"{envelope['room_id']} and another drainer holds it; handed back")
                return
        else:
            _log(f"proactive {f.name} HELD: it was queued for {stored_room} and its destination now "
                 f"resolves to {envelope['room_id']}; it is not re-routed")
    # The send and its dispatch-time recheck run under the send lock a producer takes to commit
    # an answer, so a message about a task is sent before its answer is queued or never.
    try:
        with _delivery_destination.send_lock(RESULTS_DIR, _delivery_destination.DRAIN_LOCK_WAIT_S):
            result = core.deliver_one(item_id, payload)
    except _delivery_destination.SendLockUnavailable as e:
        _hand_back(claim, f)
        _log(f"proactive {f.name} not sent: the send lock could not be taken ({diagnostics.describe(e)})")
        return
    item = read_item(core.backend.root, item_id) or {}
    status, room = item.get("status"), stored_room or envelope["room_id"]
    if status == "DELIVERED":
        ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            claim.rename(ARCHIVE_RESULTS_DIR / f"{f.stem}-{int(time.time())}.txt")
        except OSError as e:
            # Hidden, never live and never unlinked: no bridge globs it; a later pass archives it.
            _log(f"delivered proactive {f.name} could not be archived ({diagnostics.describe(e)}); kept hidden")
            try:
                claim.rename(f.with_name(f".{f.stem}{_DELIVERED_INFIX}{time.time_ns()}"))
            except OSError as e2:
                _log(f"delivered proactive {f.name} could not be hidden either ({diagnostics.describe(e2)}); "
                     f"it stays as {claim}; if a restart's orphan recovery returns it to its live "
                     f"name, its DELIVERED outbox item stops any resend")
        if result.status is DrainStatus.ATTEMPTED and result.detail != "settled by a recorded receipt":
            _ENGINE_COUNTS["core_confirmed"] += 1
            _log(f"delivered proactive {f.name} to {room} ({item.get('provider_ref')})")
        else:
            _log(f"proactive {f.name} was already delivered to {room} "
                 f"({item.get('provider_ref') or 'receipt recorded'}) — archived, not sent again")
        return
    replacement = seen.get("room_upgraded")
    if replacement is not None and status != "PARKED":
        if not core.backend.park_unclaimed(item_id, _ROOM_UPGRADED, on_release=_log_dead_claim_released):
            _hand_back(claim, f)
            _log(f"proactive {f.name}: {room} was upgraded, but its item changed meanwhile "
                 f"({(read_item(core.backend.root, item_id) or {}).get('status')}); handed back, not held")
            return
        if owner_dm:
            _hand_back(claim, f)  # stays pending: the room moved, the DM's repointing sends it
            _hold_owner_dm_upgrade(room, replacement, noticed)
            return
        status, item = "PARKED", read_item(core.backend.root, item_id) or {}
    if status == "PARKED":
        reason = str(item.get("reason") or "")
        if reason.startswith(f"{WITHDRAWN}:"):
            # Nothing is owed: archived like a skip marker; the outbox item says it was not sent.
            _retire_proactive(claim, f, ARCHIVE_RESULTS_DIR)
            _log(f"proactive {f.name} withdrawn before its send: {reason[len(WITHDRAWN) + 1:]} — not sent")
            return
        try:
            body_sha256 = hashlib.sha256(claim.read_bytes()).hexdigest()
        except OSError:
            body_sha256 = ""
        retired = _retire_proactive(claim, f, UNDELIVERABLE_RESULTS_DIR)
        # No digest, no command: an unfiltered restore can install another generation's body.
        how = (f"{_proactive_requeue_command(core.backend.root, item_id, body_sha256)}" if body_sha256
               else f"NO recovery command: its body could not be read to pin which parked copy "
                    f"is its own; requeue {item_id} by hand with --body-sha256 of that copy")
        where = (f"PARKED to {UNDELIVERABLE_RESULTS_DIR.name}/" if retired
                 else "PARKED, but its file could not be moved and stays pending")
        if reason == "outcome-unknown":
            _log(f"proactive {f.name} may have reached {room} but the send was not confirmed "
                 f"({result.detail or 'no outcome'}); {where}, not sent again. Check the room first; "
                 f"to send it again (it may post twice): {how}")
        elif reason == _DESTINATION_CHANGED:
            _log(f"proactive {f.name} held for {room}, its stored room ({where}); not re-routed. "
                 f"To send it to {room}: {how}")
        else:
            _log(f"proactive {f.name} PARKED to {UNDELIVERABLE_RESULTS_DIR.name}/ ({reason}"
                 f"{': ' + result.detail if result.detail else ''}) — it will NOT be re-sent")
        return
    _hand_back(claim, f)  # still owed: the outbox's retry schedule decides when it is tried again
    if result.status is DrainStatus.ATTEMPTED:
        _log(f"proactive send for {f.name} was not delivered ({result.detail}) — will retry")
    elif result.status is DrainStatus.DEFERRED:
        _log_backoff(("proactive-deferred", f.name), f"proactive {f.name} deferred ({result.detail}) — not sent yet")
    if "auth" in seen:
        raise seen["auth"]


# Per thread, what the proactive sender saw that the drain acts on once the outbox has
# settled the send: an auth refusal to re-raise, and the room a 400 says replaced the target.
_PROACTIVE_SEND = threading.local()


def _post_proactive_message(room: str, body: str, dedupe_key: str, thread_root: "str | None" = None) -> dict:
    """The proactive lane's sender: one room message POST, read for the two refusals the drain
    handles itself. Both are definite non-deliveries, so neither is ever parked as uncertain."""
    try:
        # Sent under the send lock producers wait on, so the whole request is bounded.
        return _post_room_message(room, body, dedupe_key, thread_root,
                                  time.monotonic() + _delivery_destination.SEND_DEADLINE_S)
    except urllib.error.HTTPError as e:
        seen = _PROACTIVE_SEND.__dict__.setdefault("seen", {})
        if e.code in (401, 403):
            seen["auth"] = e
        elif e.code == 400:
            replacement = _room_upgraded_to(_http_error_body(e))
            if replacement is not None:
                seen["room_upgraded"] = replacement
                raise ProviderRefused(f"room {room} was upgraded") from e
        raise


def _load_inflight() -> set[str]:
    """Restore the in-flight set from disk (fail-open to empty).
    A failed restore also sets _INFLIGHT_DEGRADED: the empty set it returns then
    means "unknown", which is not what FileNotFoundError's empty set means."""
    global _INFLIGHT_DEGRADED
    try:
        data = json.loads(INFLIGHT_FILE.read_text())
        if isinstance(data, list):
            return {str(t) for t in data}
        _INFLIGHT_DEGRADED = True
        _log(f"inflight file is {type(data).__name__}, not a list — starting empty")
        return set()
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001
        _INFLIGHT_DEGRADED = True
        _log(f"inflight file unreadable ({diagnostics.describe(e)}) — starting empty")
        return set()


def _save_inflight(inflight: set[str]) -> bool:
    """Durably persist the in-flight set; False when it did not commit, which
    withholds the ack that would have claimed it.
    The mutex covers snapshot+write: the poll loop and the outbound worker both
    save, and an unguarded interleave could persist a state missing the other
    thread's mutation (resurrecting a delivered id or dropping a fresh one)."""
    with _INFLIGHT_MUTEX:
        return _durable_write(INFLIGHT_FILE, json.dumps(sorted(inflight)))


# (tid, path) pairs already uploaded this process — result-POST retry guard.
_uploaded_attachments: set[tuple[str, str]] = set()


def _holder_delivery_state(holder_id: str) -> str:
    """Gateway acceptance evidence comes from the outbox, never archive location."""
    if not _valid_local_tid(holder_id):
        return "failed"
    delivery = _delivery_tid(holder_id)
    if delivery is None:
        return "failed"
    record = read_item(_delivery_core().backend.root, _broker_tid(delivery))
    candidates = [local_task_protocol.find_result(RESULTS_DIR, holder_id)]
    candidates.extend(reversed(undelivered_quarantine.find_quarantined(RESULTS_DIR, holder_id)))
    body = None
    if record and record.get("payload"):
        try:
            stored = json.loads(record["payload"])
            folded = stored.get("consolidated_into")
            # A fold is stored with an empty body: read it as the marker it replaces.
            body = f"[deduped: {folded}]" if folded else stored.get("body")
        except (ValueError, TypeError, AttributeError):
            return "failed"
    if body is None:
        for path in candidates:
            if path is not None:
                body = read_ready_result(path)
                if body:
                    break
    return classify_holder_delivery(record, body,
                                    (RESULTS_DIR / f"{holder_id}.txt").exists())


def _reask_id(tid: str) -> str:
    """One re-ask per task: a pass that retries the decision reuses it."""
    return "task-" + hashlib.sha256(f"dedup-reask\0{tid}".encode()).hexdigest()[:18]


def _dedup_plan(tid: str, holder_id: str | None, inflight: "set[str] | None" = None):
    """Shared dedup recovery, bound to this adapter's directories.

    A requeue carries the delivery forward: the re-ask keeps the room, stays
    in flight, and aliases back to the id the broker is waiting on. Without
    that the re-ask is written and its answer is never looked for. With
    `inflight`, the re-ask is durably in flight before it is published.
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
        if inflight is not None and new_id not in inflight:
            inflight.add(new_id)
            if not _save_inflight(inflight):
                inflight.discard(new_id)
                return False
        return True

    # One planner at a time: the published check and the link are not atomic
    # across a watcher that claims or archives the re-ask in between.
    try:
        with disposal.locked(RESULTS_DIR):
            action, payload = plan_dedup_recovery(
                RESULTS_DIR, TASKS_DIR, tid, holder_id, room,
                _reask_id(tid), commit_identity=_commit,
                channel_dir=CHANNEL_DIR, holder_delivery_state=_holder_delivery_state)
    except OSError as e:
        _log(f"dedup decision for {tid} deferred: {diagnostics.describe(e)}")
        return "defer", None, room
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


_WITHHELD_OUTBOX: "DeliveryCore | None" = None


# Proactive messages: at most one send each, an uncertain one parked for the operator.
_PROACTIVE_CORE: "DeliveryCore | None" = None


# The results outbox again, under the relay rule: a relay ask's result.
_RELAY_CORE: "DeliveryCore | None" = None

# A relay ask's result is retried until the broker accepts it. `RetrySchedule` requires a
# finite window, so this one is a century, and a park the rule still meets is reopened.
_RELAY_RETRY = RetrySchedule(window_s=100 * 365 * 86400.0)
# Why a relay ask's parked result was requeued: by the drain, or at start after a downgrade.
_RELAY_REOPENED = "relay result: retried until the broker accepts it"
_RELAY_RECLAIMED = "relay result an older bridge parked: reclaimed at start"
# A relay result this long into its retry run without an accept is reported in the gateway
# status, and still retried: the window after which every other result parks.
RELAY_RESULTS_RETRYING_AFTER_S = 600


def _relay_results_retrying(now: float) -> "dict | None":
    """How many relay results the broker has not accepted `RELAY_RESULTS_RETRYING_AFTER_S` or
    more into their retry run, and the oldest one's age in seconds, both from the outbox
    record's `retry.started_at`, which a requeue or a reclaim at start resets. None when none
    has, and when it cannot be worked out (logged on a backoff), so the rest of the status is
    still written. Read from the kept marks and their records; an unreadable one is skipped.
    While the marks file cannot be read (`_load_ack_first` returns None and logs that on its
    own backoff) the field is left out too, so its absence does not prove nothing is retrying."""
    try:
        root = _delivery_core("relay").backend.root
        ages = []
        for broker_id in filter(_valid_tid, _load_ack_first() or {}):
            try:
                record = read_item(root, broker_id) or {}
            except (OSError, ValueError):
                continue
            retry = record.get("retry") if isinstance(record, dict) else None
            started = retry.get("started_at") if isinstance(retry, dict) else None
            if (isinstance(started, (int, float))
                    and record.get("status") not in ("DELIVERED", "PARKED")
                    and now - started >= RELAY_RESULTS_RETRYING_AFTER_S):
                ages.append(now - started)
        return {"count": len(ages), "oldest_age_s": int(max(ages))} if ages else None
    except Exception as e:  # noqa: BLE001 - this field never costs the rest of the status write
        _log_backoff(("relay-results-retrying",), f"relay_results_retrying not reported "
                     f"({diagnostics.describe(e)}); the rest of the gateway status is written")
        return None


def _failed_in_place_of(stored: str) -> "str | None":
    """The minimal `failed` result for the task whose stored payload the broker refused for
    good: the outbox stores it in place of that payload and retries it the same way, so the
    broker ends the ask and posts "couldn't finish". None, which ends the result, for a
    payload that names no task or is itself a `failed`."""
    try:
        doc = json.loads(stored)
    except (TypeError, ValueError):
        doc = None
    rid = doc.get("id") if isinstance(doc, dict) else None
    if not (isinstance(rid, str) and rid) or doc.get("status") == "failed":
        return None
    _log(f"result {rid}: the broker refused it for good — a `failed` result for the same task "
         "is sent in its place, retried until the broker accepts it")
    return json.dumps({"id": rid, "body": "", "status": "failed", **_result_contract(False)})


def _delivery_core(lane: str = "result") -> DeliveryCore:
    """The one construction site of a delivery outbox: gateway results over idempotent
    result POSTs (`relay`: the same outbox under the relay rule), or an owner-released
    withheld result or a proactive message over an at-most-once room send."""
    global _DELIVERY_CORE, _WITHHELD_OUTBOX, _RELAY_CORE, _PROACTIVE_CORE
    withheld, relay, proactive = lane == "withheld", lane == "relay", lane == "proactive"
    root = (RESULTS_DIR / f".outbox-withheld-review{_INST_SUFFIX}" if withheld
            else _proactive_outbox_root() if proactive
            else gateway_instance.outbox_root(RESULTS_DIR, GATEWAY_INSTANCE))
    current = (_WITHHELD_OUTBOX if withheld else _RELAY_CORE if relay
               else _PROACTIVE_CORE if proactive else _DELIVERY_CORE)
    if current is None or current.backend.root != root:
        # Late-bound senders, so token rotation and a test's _req double reach them mid-process.
        # A relay result's room send is the broker's, with its own dead letter: its accept is
        # final here, or a send the broker dead-letters would be retried with no ceiling.
        provider = (AG2SpaceRoomMessageProvider(lambda *a: _post_room_message(*a),
                                                lambda *a: _record_release_receipt(*a)) if withheld
                    else AG2SpaceRoomMessageProvider(lambda *a: _post_proactive_message(*a),
                                                     lambda *a: _record_proactive_receipt(*a))
                    if proactive
                    else AG2SpaceResultProvider(lambda *a, **k: _req(*a, **k), read_back=not relay))
        current = DeliveryCore(
            DesignAClaimBackend(root, retry_schedule=_RELAY_RETRY if relay else RetrySchedule(),
                                republish_delivered=False,
                                replace_refused=_failed_in_place_of if relay else None),
            provider,
            policy=RetryPolicy(max_attempts=MAX_TRANSIENT_ATTEMPTS,
                               defer_idempotent_resend=not (withheld or proactive),
                               park_interrupted_send=proactive),
            worker=("withheld-review-publication" if withheld else "gateway-proactive-drain"
                    if proactive else "gateway-result-drain"),
            revalidate=(lambda *a: _proactive_dispatch_veto(*a)) if proactive else None)
        if withheld:
            _WITHHELD_OUTBOX = current
        elif relay:
            _RELAY_CORE = current
        elif proactive:
            _PROACTIVE_CORE = current
        else:
            _DELIVERY_CORE = current
    return current


def _read_ready_generation(rfile) -> "tuple[str | None, ResultIdentity | None]":
    """The body the drain will send and the identity of the file it came from."""
    ready = read_ready_result_with_identity(rfile)
    return (ready.body, ready.identity) if ready else (None, None)


def _recover_disposing_claims() -> None:
    """Every drain pass and sweep: the lifecycle owner recovers stranded
    claims. Recovery serves the operator; it never gates ordinary delivery."""
    try:
        disposal.recover_abandoned_claims(RESULTS_DIR, _log)
    except Exception as e:  # noqa: BLE001 - the adapter boundary fails open
        disposal.report_once(f"recovery:{RESULTS_DIR}", _log,
                             f"result disposal: recovery failed ({diagnostics.describe(e)}); delivery continues")


def _disposed_copy_exists(tid: str, generation) -> bool:
    """The lifecycle owner's verdict; a lock it cannot take is no verdict, and
    the loser then reports rather than staying quiet."""
    try:
        return disposal.disposed_copy_exists(RESULTS_DIR, tid, generation, _log)
    except OSError as e:
        _log(f"result {tid}: could not check for a disposed copy ({diagnostics.describe(e)})")
        return False


def _quarantine_undelivered(rfile, tid: str, why: str, outbox_item_id=None,
                            generation=None, requeueable: bool = True,
                            not_requeueable_because: str = "") -> None:
    """Move a result the outbox has finally refused into results/undelivered/,
    the same quarantine the proactive path uses. Without this the file is
    rescanned every pass and the refusal is invisible.

    The printed command must carry BOTH ids on a named instance: the record is
    keyed by the broker id and the body file by the local one, so neither alone
    recovers both halves.
    """
    try:
        if generation is None:
            disposal.quarantine_current(RESULTS_DIR, rfile, _log)
        else:
            disposal.quarantine_generation(RESULTS_DIR, rfile, generation, _log)
        if not requeueable:
            _log(f"result {tid}: {why} — quarantined to {UNDELIVERABLE_RESULTS_DIR.name}/; "
                 f"{not_requeueable_because}")
            return
        _log(f"result {tid}: {why} — quarantined to "
             f"{UNDELIVERABLE_RESULTS_DIR.name}/ — `ag2-sparrow-outbox "
             f"--root {gateway_instance.outbox_root(RESULTS_DIR, GATEWAY_INSTANCE)} "
             f"requeue {outbox_item_id or _broker_tid(_delivery_tid(tid) or tid)} --reset-attempts "
             f"--results-dir {RESULTS_DIR} --body-id {tid}` restores it")
    except disposal.KeptQuarantined:
        return                                          # the lifecycle owner named where it waits
    except disposal.GenerationReplaced:
        # The file this pass read is already disposed of, or a newer reply took
        # its name and stays live; either way nothing of this pass is lost.
        if _disposed_copy_exists(tid, generation):
            return
        _log(f"result {tid}: {why} but the reply this pass read was replaced by a "
             "newer one at its name, which stays live")
    except FileNotFoundError:
        # Two observers can reach a terminal item at once; the loser is quiet
        # only when the very file it read is already disposed of.
        if generation is not None and _disposed_copy_exists(tid, generation):
            return
        _log(f"result {tid}: {why} but the result file vanished before "
             "quarantine and no quarantined copy of the body this pass read was found")
    except OSError as e:
        _log(f"result {tid}: {why} but quarantine failed ({diagnostics.describe(e)}) — "
             "leaving it in place")


def _is_worker_id(value: str) -> bool:
    """The pool's instance-id grammar, restated here for the same reason the
    path conventions below are: this package cannot import the optional skill
    that owns it. `core` is not a worker and never satisfies this."""
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _assigned_worker(task_id: str) -> str:
    """Which pool worker this task was ASSIGNED to, read from the router's
    assignment record. Provenance is fixed before the task runs, so it does
    not depend on the worker finishing, on residue surviving, or on the
    producer remembering to stamp itself.

    Path convention (state/attribution/<task_id>) is owned by the pool's own
    recorder in an optional local skill this standalone PyPI package cannot
    import or name (docs/architecture-boundaries.md, "Optional adapter
    capabilities") — the same arrangement _worker_of() has with the done-flag
    writer. tests/gateway-result-worker-attribution.test.py builds its
    fixtures through that recorder's own path function, so a drift fails a
    test instead of silently losing attribution.

    FAILS CLOSED, for the reason _worker_of does: a wrong worker id labels a
    reply with another worker's identity. Anything unreadable, not a regular
    file, or outside the instance-id grammar yields "" rather than a guess.
    """
    if not task_id or "/" in task_id or task_id in (".", ".."):
        return ""
    path = _STATE / "attribution" / task_id
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ""
    except OSError:
        return ""  # unreadable: no reading, not "never assigned"
    if not stat.S_ISREG(st.st_mode):
        return ""  # malformed record the recorder would itself refuse
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if _is_worker_id(value) else ""


# The core is a delivery recipient like any worker, but the router records
# attribution only for workers — so a core sentinel with no record is normal.
_CORE_RECIPIENT = "core"
_DELIVERY_SUFFIXES = (".txt", ".accepted", ".claimed")


def _delivery_recipient(task_id: str) -> tuple[str, bool]:
    """`(recipient, blind)` — which worker this task was DELIVERED to, read
    from the pool's delivery sentinel. Used ONLY to tell "this was a worker's
    task" from "this was the core's" when attribution is missing — never as an
    attribution source.

    Path convention (deliveries/<recipient>/<task_id>.{txt,accepted,claimed})
    is owned by the pool's own delivery writer, an optional local skill this
    standalone package cannot import or name; the test builds its fixtures
    through that writer so a drift fails there instead of silently reading
    nothing, and asserts this module's recipient/suffix constants still cover
    what that writer produces.

    The core NEVER counts as a claimant: the router writes a sentinel for every
    recipient but records attribution only for workers, so "core sentinel, no
    record" is the ordinary shape of a task the core answered itself, not an
    invariant violation.

    `blind` is the second outcome because a read failure and "never delivered"
    are different facts that both have no recipient, and the caller WITHHOLDS on
    one and sends on the other. Logging the difference was not enough: the send
    decision could not see a log. An ABSENT tree is not blind — a host with no
    pool has no deliveries dir, and that is knowledge, not failure.
    """
    if not task_id or "/" in task_id or task_id in (".", ".."):
        return "", False
    root = _STATE.parent / "deliveries"
    try:
        recipients = sorted(p.name for p in root.iterdir())
    except FileNotFoundError:
        return "", False
    except OSError as exc:
        _log(f"attribution: cannot read {root} ({type(exc).__name__}) - the "
             f"delivery discriminator is BLIND for {task_id}, which is not the "
             f"same as this task never having been delivered.")
        return "", True
    claimants = set()
    for name in recipients:
        if name == _CORE_RECIPIENT:
            continue
        for suffix in _DELIVERY_SUFFIXES:
            try:
                st = os.lstat(root / name / f"{task_id}{suffix}")
            except (FileNotFoundError, NotADirectoryError):
                # Reading through a stray file at the root raises
                # NotADirectoryError: a non-recipient, not a read failure.
                continue
            except OSError as exc:
                _log(f"attribution: cannot read delivery sentinel for "
                     f"{task_id} under {name} ({type(exc).__name__}) - the "
                     f"delivery discriminator is BLIND for this task, which is "
                     f"not the same as it never having been delivered.")
                return "", True
            if not stat.S_ISREG(st.st_mode):
                # Malformed, not unreadable: the writer would never make one,
                # so this is refusal to believe it, not absence of a reading.
                return "", False
            claimants.add(name)
            break
    if len(claimants) == 1:
        return claimants.pop(), False
    if claimants:
        # Sentinels under several recipients prove a worker owned this task
        # without saying which, which is the refusal condition, not its absence.
        _log(f"attribution: {task_id} has delivery sentinels under "
             f"{len(claimants)} recipients ({', '.join(sorted(claimants))}) - "
             f"ownership is AMBIGUOUS, so refusing rather than relaying a "
             f"worker's reply as the core's own.")
        return "", True
    return "", False


def _attribution(task_id: str) -> tuple[str, bool]:
    """`(worker, refused)` for one result: assignment truth first, completion
    residue only as the migration fallback.

    A task routed before assignment records existed has none, so residue still
    answers for it; once no such task is in flight the residue arm can go. A
    result with residue but no assignment record is precisely the anomaly the
    assignment store exists to surface — an author nobody recorded at routing
    time — so it is logged rather than passed over.

    `refused` is the second outcome, and it is why this returns a pair: a
    worker's result with no attribution and an ordinary core result BOTH have
    no worker id, so a bare "" cannot tell a caller to withhold one and send
    the other. Refused means the sentinel proves a worker owned the task while
    nothing recorded who — publishing it would relay a worker's reply as the
    core's own.
    """
    assigned = _assigned_worker(task_id)
    if assigned:
        return assigned, False
    residue = _worker_of(task_id)
    if residue:
        _log(f"attribution: {task_id} has no assignment record; using "
             f"completion residue ({residue}). Assignment-time recording "
             f"did not run for this task.")
        return residue, False
    # FAILS CLOSED, LOUDLY. A delivery sentinel proves this was a worker's task,
    # so silence here would relay it as if the core had produced it.
    delivered, blind = _delivery_recipient(task_id)
    if blind:
        # The evidence store is exactly what decides worker-vs-core here, so an
        # unreadable one is not a licence to assume core and send.
        _log(f"attribution: {task_id} cannot be classified - the delivery "
             f"evidence is unreadable, so whether a worker owned this task is "
             f"UNKNOWN. Withholding rather than assuming the core produced it.")
        return "", True
    if delivered:
        _log(f"attribution: {task_id} was DELIVERED to {delivered} but has no "
             f"assignment record and no completion residue - refusing to stamp "
             f"rather than attribute it to the core. The record is written under "
             f"the same lock as the sentinel, so this is an invariant violation.")
        return "", True
    return "", False


def _result_worker(task_id: str) -> str:
    """The worker id alone, for callers that only label and never withhold.
    A refusal reads as "" here, so anything deciding whether to SEND must use
    _attribution() instead."""
    return _attribution(task_id)[0]


def _worker_of(task_id: str) -> str:
    """Which pool worker finished this task, read from the per-worker
    completion record. `task_id` is the result stem, prefix included.

    SUPERSEDED as the primary signal by _assigned_worker(): this infers the
    author from completion residue after the fact, which is exactly the
    fragility assignment-time provenance removes. Kept as the transition
    fallback in _result_worker() for tasks routed before that record existed.

    The recipient grammar, the record layout, the stage order and the "is this
    a record?" predicate all come from pool_record, which the pool's own
    writer binds too — an optional local skill this standalone package can
    neither import nor name (docs/architecture-boundaries.md, "Optional
    adapter capabilities"). No second predicate lives here, so the reader
    cannot accept state the writer would refuse.

    FAILS CLOSED. A wrong worker id is worse than none — it labels a reply
    with another worker's identity — so anything unreadable, or any record the
    writer's own predicate would reject, yields "". Entries PROVEN not to be
    recipients are skipped instead: a stray file beside the recipient folders
    is not an unreadable claimant, and must not suppress valid attribution.
    `Path.glob` is deliberately not used: it reports an unreadable subtree as
    absent, which would let one unreadable claimant hand the answer to another.
    """
    root = pool_record.workers_root(_STATE)
    try:
        recipients = pool_record.iter_recipients(root)
    except FileNotFoundError:
        return ""
    except OSError:
        return ""  # unreadable root: no reading, not "nobody claimed it"
    claimants = set()
    for name in recipients:
        for stage in pool_record.STAGES:
            try:
                state = pool_record.read_record_state(
                    pool_record.record_path(root, name, task_id, stage))
            except OSError:
                return ""  # unreadable claim tree: abstain, never fall through
            if state is pool_record.RecordState.ABSENT:
                continue
            if state is not pool_record.RecordState.PRESENT:
                return ""  # malformed record the writer would itself refuse
            claimants.add(name)
            break
    return claimants.pop() if len(claimants) == 1 else ""


_RECOVERED_LABEL = "(recovered result — original delivery was lost)\n"


def _live_source(result_file, generation) -> "str | None":
    """The ready body of the generation this pass read, or None when the file
    no longer holds it: the digest an outbox record keeps is of this text."""
    if result_file is None:
        return None
    ready = read_ready_result_with_identity(result_file)
    if ready is None or (generation is not None and ready.identity != generation):
        return None
    return ready.body


def _delivered_wire_body(item_id: str) -> "str | None":
    """The reply body the delivered record says went out; None when unknown."""
    root = getattr(_delivery_core().backend, "root", None)
    record = read_item(root, item_id) if root is not None else None
    try:
        stored = json.loads((record or {}).get("payload") or "")
    except (TypeError, ValueError):
        return None
    body = stored.get("body") if isinstance(stored, dict) else None
    return body if isinstance(body, str) else None


REVIEW_UNSENT = "review it; it may already have been sent"


def _body_ref(text: "str | None") -> str:
    """A reference an operator can compare without the body in the log."""
    if text is None:
        return "unknown"
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]} ({len(text)} chars)"


def _quarantine_unsent(result_file, tid: str, item_id: str, generation=None) -> None:
    """The outbox owner ruled this live reply never provably sent. It goes through
    the same owner-mention and result-guard policy as a delivery, never resent; the
    guarded body's markers decide what the operator is told. A failed move leaves
    it live and the next pass rules again."""
    why = "a different reply was published after this id was delivered"
    if result_file is None:
        _log(f"result {tid}: {why} — not retrying")
        return
    ready = read_ready_result_with_identity(result_file)
    if ready is None:
        _log(f"result {tid}: {why}; it is not readable now, left for the next pass")
        return
    if generation is not None and ready.identity != generation:
        # The ruling was about the reply the caller read; this is another one.
        _log(f"result {tid}: the reply changed after it was ruled on; left for the next pass")
        return
    # Every decision below is about this generation, and only it is moved.
    raw, generation = ready.body, ready.identity
    mention = _owner_mention_disposition(tid, raw)
    if mention is None:
        _log(f"result {tid}: {why}; its owner-mention routing is unavailable, left for the next pass")
        return
    if mention:
        # The owner-mention leg queues one DM per task; a later reply stays visible.
        _quarantine_undelivered(result_file, tid, why, outbox_item_id=item_id, generation=generation,
                                requeueable=False, not_requeueable_because=(
                                    f"{REVIEW_UNSENT}; it answers an owner-mention task, so it may "
                                    "only ever reach the owner's DM, never a room"))
        return
    body, withheld = _guarded_result_body(tid, raw)
    if body is None:
        _log(f"result {tid}: {why}; the result guard is unavailable ({withheld}), left for the next pass")
        return
    parsed = parse_markers(body, skip_after_channel=DELIVERY_SKIP_AFTER_CHANNEL)
    actions = parsed.actions
    skip = next((a for a in actions if a.kind == "skip"), None)
    if skip is not None:
        done = disposal.retire_generation(RESULTS_DIR, result_file, generation, _log,
                                          ARCHIVE_RESULTS_DIR, _names(f"{tid}-{int(time.time())}-suppressed"))
        what = (f"withheld by the result guard ({withheld})" if withheld
                else f"suppressed ([{skip.value}])")
        if done.retired:
            _log(f"result {tid}: {why}; it is {what}, owes no room delivery, and is archived")
        elif done.outcome is not disposal.Retirement.REPLACEMENT_LIVE:
            _log(f"result {tid}: {why}; it is {what} but could not be archived "
                 f"({done.cause}); the next pass retries")
        return
    # Whether this reply already went out cannot be told from what is stored (delivery
    # transforms the wire body), so the operator is always told to review, never to send.
    markers = [f"[{a.kind}]" if a.kind == "dm-only" else
               f"[channel: {a.value}]" if a.kind == "redirect" else f"[file: {a.value}]"
               for a in actions if a.kind in ("dm-only", "redirect", "attach")]
    because = (REVIEW_UNSENT
               + (f"; marked {' '.join(markers)}" if markers else "")
               + (f"; the result guard withheld it ({withheld})" if withheld else "")
               + f"; delivered wire body {_body_ref(_delivered_wire_body(item_id))}"
               + f", this reply {_body_ref(ready.body)}")
    _quarantine_undelivered(result_file, tid, why, outbox_item_id=item_id, generation=generation,
                            requeueable=False, not_requeueable_because=because)


def _result_contract(dm_only: bool) -> dict:
    """The voice-outcome contract every result carries, with an explicit `dm_only`.

    The broker reads them only for a relay ask, and treats a relay ask's result
    without them as private: that is how an older bridge's answer, its `[dm-only]`
    line stripped, still reaches only the owner. A broker without the contract
    ignores both keys.
    """
    return {"capabilities": [VOICE_OUTCOME_CONTRACT], "dm_only": bool(dm_only)}


def _dm_only(parsed) -> bool:
    """Whether the body carries `[dm-only]` anywhere. A standalone marker line is
    stripped from the body sent, so this is how the broker learns the answer is private."""
    return any(a.kind == "dm-only" for a in parsed.actions)


def _deliver_result_payload(tid: str, broker_tid: str, body: str,
                            no_send: bool = False, result_file=None,
                            generation=None, thread_ask: bool = False,
                            dm_only: bool = False, consolidated_into: str = "") -> bool:
    """One outbound result POST through the delivery core. True = the
    gateway confirmed (server lease closed; caller archives). False = not
    confirmed this pass; leave the result file for the next one.
    `generation` is the file the caller read; disposal touches only that."""
    core = _delivery_core()
    # Decided before anything moves: a voice ask's result waits while its rule cannot be read.
    relay = _relay_result(tid, broker_tid)
    if relay is None:
        return False
    # `no_send` is the broker's STRUCTURED suppression field: the lease must
    # close without a user-facing send. It rides the payload, not the body.
    doc = {"id": broker_tid, "body": body}
    if no_send:
        doc["no_send"] = True
    if consolidated_into:
        # An honoured fold of a relay ask: the broker records it as merged into
        # that task and posts nothing for it, so no marker and no `no_send` ride along.
        doc["consolidated_into"] = consolidated_into
    doc.update(_result_contract(dm_only))
    if thread_ask:
        # The broker roots it on this task's own ask; a broker without it ignores the key.
        doc[RESULT_THREAD_FIELD] = RESULT_THREAD_ASK
    # Structured attribution, not the "— core-N" prose in the body: the
    # signature is for humans and reformatting it must not change routing.
    worker, refused = _attribution(tid)
    if refused:
        # ENFORCED, not just logged: this is a worker's reply that nothing
        # recorded, so sending it would publish it as the core's own.
        why = ("attribution refused: delivered to a worker but no assignment "
               "record and no completion residue - withholding rather than "
               "publishing an unattributed result as the core's own")
        if result_file is not None:
            _quarantine_undelivered(result_file, tid, why, generation=generation)
        else:
            _log(f"result {tid}: {why} - not published, not delivered")
        return False
    if worker:
        doc["metadata"] = {"worker_id": worker}
        _log(f"result {tid}: attributed to worker {worker}")
    payload = json.dumps(doc).encode("utf-8")
    # A fold posts nothing either, so it closes an already-accepted id the same way.
    item_id = _effective_item_id(core, broker_tid, no_send or bool(consolidated_into))
    # The task's own result item only: a close of an id already accepted keeps today's policy.
    relay = relay and item_id == broker_tid
    if relay:
        core = _delivery_core("relay")
    live = _live_source(result_file, generation)
    # With no result file there is nothing live to protect; the body sent is this call's.
    ruled = live if result_file is not None else body
    source = ({"source_ready_sha256": source_digest(live)}
              if live is not None and getattr(core.backend, "records_source_digest", False) else {})
    if item_id != broker_tid:
        core.backend.publish(item_id, payload, republish_delivered=True, **source)
    else:
        core.backend.publish(item_id, payload, **source)
    res = core.deliver_one(item_id, payload)
    if res.status is DrainStatus.TERMINAL:
        record = read_item(core.backend.root, item_id) or {}
        if record.get("status") == "DELIVERED":
            if delivered_body_differs(core.backend.root, item_id, ruled):
                _quarantine_unsent(result_file, tid, item_id, generation)
                return False
            return True
        if relay:
            _reopen_relay_result(core, item_id, tid, record, result_file, generation)
            return False
        # The outbox has decided this item; no pass will ever claim it again,
        # so retrying logs forever and hides the failure behind "will retry".
        _dispose_terminal(core, item_id, tid, record, result_file, generation)
        return False
    if res.status is DrainStatus.NOT_CLAIMED:
        # A dead prior incarnation's claim; reclaim-TTL recovers it, and
        # with an idempotent provider nothing parks on ambiguity.
        record = read_item(core.backend.root, item_id) or {}
        retry = record.get("retry", {})
        line = (f"result {tid}: pending retry or delivery claim "
                f"(attempts={core.backend.attempts(item_id)}, "
                f"next={retry.get('next_attempt_at')}, deadline={retry.get('deadline')})")
        if relay:
            _log_backoff((item_id, "pending"), line)    # no ceiling: not once a second
        else:
            _log(line)
        return False
    if res.outcome is CoreDeliveryOutcome.CONFIRMED:
        root = getattr(core.backend, "root", None)
        if root is not None and delivered_body_differs(root, item_id, ruled):
            # The outbox sent its stored body, not this one.
            _quarantine_unsent(result_file, tid, item_id, generation)
            return False
        _ENGINE_COUNTS["core_confirmed"] += 1
        # Only the broker's own `delivered` read-back may be logged as delivered;
        # an accept alone is recorded, not posted.
        ref = getattr(res, "provider_ref", None)
        what = ("delivered" if ref == "delivered" else
                f"accepted by the broker, room delivery unconfirmed "
                f"({ref or 'accepted'}),")
        _log(f"result {tid} {what} via DeliveryCore "
             f"(provider={type(core.provider).__name__}, "
             f"backend={type(core.backend).__name__}, worker={core.worker})")
        return True
    record = read_item(core.backend.root, item_id) or {}
    if record.get("status") == "PARKED":
        if relay:
            _reopen_relay_result(core, item_id, tid, record, result_file, generation)
            return False
        # Parked by this very attempt: dispose now, or the next pass logs the
        # refusal a second time before the file leaves the drain's view.
        _dispose_terminal(core, item_id, tid, record, result_file, generation)
        return False
    retry = record.get("retry", {})
    line = (f"result POST not confirmed for {tid} "
            f"({res.outcome.value if res.outcome else '?'}: {res.detail}) — "
            f"status={record.get('status')}, reason={record.get('reason')}, "
            f"next={retry.get('next_attempt_at')}, deadline={retry.get('deadline')}")
    if relay:
        _log_backoff((item_id, "unconfirmed"), line)
    else:
        _log(line)
    return False


def _relay_result(tid: str, broker_id: str) -> "bool | None":
    """Whether a result posted under `broker_id` answers a task served with `ack_first`
    (its mark is kept), so its delivery has no ceiling. A re-ask answers under its
    original's id. None while the marks cannot be read and the task may be marked, which
    then waits. Only a voice ask (`ask-…`, the lane's `ask_sutando` id) can be: the
    session-end task (`voice-end-…`) and every other task go on under today's policy."""
    marks = _load_ack_first()
    if marks is None:
        if not (broker_id.startswith(_RELAY_ASK_PREFIX) and _task_source(tid) == _VOICE_ASK_SOURCE):
            return False
        _log_backoff((tid, "deferred"), f"delivery deferred for {tid} — ack_first marks unreadable")
        return None
    return broker_id in marks


def _reopen_relay_result(core, item_id: str, tid: str, record: dict, result_file=None,
                         generation=None) -> None:
    """A park the relay rule could not prevent (the 10-minute window an older bridge started
    before the Mac updated, or a refusal it recorded) is requeued under the rule; a refused
    payload, sent again, is refused again and swapped for `failed` then. The rule's own end,
    the broker refusing the `failed` too, is never reopened: see `_end_relay_result`."""
    if record.get("status") != "PARKED":
        return
    if is_refused_for_good(record.get("reason")):
        _end_relay_result(core, item_id, tid, record, result_file, generation)
        return
    outcome, _ = requeue_item_with_epoch(core.backend.root, item_id, reset_attempts=True,
                                         operator=core.worker, reason=_RELAY_REOPENED)
    _log(f"result {tid}: a relay ask's result was parked ({record.get('reason')}); "
         f"it is retried until the broker accepts it ({outcome.value})")


def _end_relay_result(core, item_id: str, tid: str, record: dict, result_file, generation) -> None:
    """The broker refused the answer and the `failed` in its place for good: no retry can
    change that. The result is quarantined as any park is; the drain then settles the task and
    its in-flight id, and retires the mark last (`_settle_delivered`), so a crash between the
    steps leaves the mark for start-up to finish the end by (`_finish_relay_end`)."""
    _dispose_terminal(core, item_id, tid, record, result_file, generation)
    _log(f"ERROR result {tid}: the broker refused the answer and the `failed` sent in its place "
         f"for good ({record.get('reason')}); it is quarantined and not retried")


def _settle_delivered(tid: str, item_id: str, rfile, inflight: "set[str] | None" = None) -> bool:
    """A delivered id whose remaining reply the owner kept for a person or
    archived has nothing left to deliver: its task is settled. So is a relay
    result the broker refused for good, whose result is quarantined."""
    root = getattr(_delivery_core().backend, "root", None)
    if root is None or os.path.lexists(rfile):
        return False                            # something is still live at the name: keep looking
    record = read_item(root, item_id) or {}
    if record.get("status") == "DELIVERED":
        return _settle_task(tid, inflight)
    if record.get("status") != "PARKED" or not is_refused_for_good(record.get("reason")):
        return False
    # A relay result's end: its task and in-flight id retired on disk, the mark last.
    settled = _settle_task(tid, inflight)
    if settled and (inflight is None or _save_inflight(inflight)):
        _log_relay_end(tid, _forget_ack_first(item_id), item_id)
    return settled


def _forget_relay_counts(*ids: str) -> None:
    """A relay result retired: its waiting lines' counts go, by broker and local id."""
    for first in ids:
        _log_backoff_forget(first)


def _log_relay_end(tid: str, mark_retired: bool, *ids: str) -> None:
    """Say how the end of a relay result refused for good closed, once its steps ran; a
    complete end drops the backoff counts of `tid` and `ids`, as a delivery does."""
    if mark_retired:
        _forget_relay_counts(tid, *ids)
        _log(f"result {tid}: its relay result's end is complete; its task is settled and its relay mark retired")
    else:
        _log(f"result {tid}: its task is settled, but its relay mark could not be retired; "
             "the next start finishes the end")


def _effective_item_id(core, broker_tid: str, no_send: bool) -> str:
    """A suppressed close for an already-delivered item rides its own record."""
    accepted = (read_item(core.backend.root, broker_tid) or {}) if no_send else {}
    return (f"{broker_tid}.lease-close"
            if accepted.get("status") == "DELIVERED" else broker_tid)


def _dispose_terminal(core, item_id: str, tid: str, record: dict, result_file,
                      generation=None) -> None:
    why = (f"outbox item is terminal: {record.get('reason')} after "
           f"{core.backend.attempts(item_id)} attempt(s)")
    if result_file is not None:
        _quarantine_undelivered(result_file, tid, why, outbox_item_id=item_id,
                                generation=generation)
    else:
        _log(f"result {tid}: {why} — not retrying")


def _result_tier(tid: str) -> "str | None":
    """Resolve the task tier; unknown provenance stays on the guarded path."""
    try:
        tfile = find_task_file(TASKS_DIR, tid) or find_archived_task(TASKS_DIR, tid)
        return (team_result_guard.resolve_access_tier(tfile)
                if tfile is not None else "guest")
    except Exception:
        return None


def _owner_mention_refused(tid: str, body: str) -> "bool | None":
    """Whether the room must not get this result (team_result_guard owns the rule).
    None = the task file exists but could not be read; the caller retries."""
    tfile = find_task_file(TASKS_DIR, tid) or find_archived_task(TASKS_DIR, tid)
    if tfile is None:
        return False
    try:
        text = tfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return team_result_guard.owner_mention_result_refused_by_room(text, body)


def _owner_mention_dm_name(tid: str) -> str:
    return proactive_filename(f"owner-mention-{tid}", "ag2space")[:-len(".txt")]


_OWNER_MENTION_PREFIX = "proactive-owner-mention-"


def _owner_mention_dm_queued(tid: str) -> bool:
    """A copy already handed to the proactive leg (pending, claimed, sent or parked)."""
    name = _owner_mention_dm_name(tid)
    stem = glob.escape(name)
    return ((RESULTS_DIR / f"{name}.txt").exists() or (RESULTS_DIR / f"{name}.sending").exists()
            or any(_delivery_destination.claim_base(p.name) == name
                   for p in RESULTS_DIR.glob(f"{stem}.sending.*"))
            or any(ARCHIVE_RESULTS_DIR.glob(f"{stem}-*.txt"))
            or any(UNDELIVERABLE_RESULTS_DIR.glob(f"{stem}-*.txt")))


def _queue_owner_mention_dm(tid: str, text: str) -> bool:
    """Hand an owner-mention result to the proactive leg, addressed to the owner's DM.
    False = not queued (no owner DM reading yet, or the write failed); retried next pass."""
    if not text.strip() or _owner_mention_dm_queued(tid):
        return True
    room = resolve_destination(OWNER_PRIVATE)
    if not room:
        _held(f"owner-mention result {tid}")
        return False
    # Addressed to a Matrix room, so every other bridge's drain leaves this file alone.
    body = f"[channel: {room}]\n{neutralize_markers(text).strip()}\n"
    return _durable_write(RESULTS_DIR / f"{_owner_mention_dm_name(tid)}.txt", body)


def _owner_mention_disposition(tid: str, raw: str) -> "bool | None":
    """Every result consumer asks this first, before the tier guard.

    True = an owner-mention result the room must not get: its DM is queued and the
    caller closes the room lease silently. False = deliver as usual. None = retry.
    """
    refused = _owner_mention_refused(tid, raw)
    if not refused:
        return refused
    if not _queue_owner_mention_dm(tid, parse_markers(raw).body):
        return None
    _log(f"owner mention {tid}: result sent to the owner's DM, not the room")
    return True


def _post_ready_results(inflight: set[str]) -> None:
    """For each in-flight task, if its result file exists, POST it + archive."""
    _recover_disposing_claims()
    _load_settle_pending()
    changed = False
    for tid in list(inflight):
        if not _valid_local_tid(tid):  # defense-in-depth: never read an unsafe path (local ids may carry the instance encoding)
            inflight.discard(tid); changed = True
            continue
        rfile = RESULTS_DIR / f"{tid}.txt"
        raw, generation = _read_ready_generation(rfile)
        if raw is None:
            # A disposed-of id whose task could not be archived earlier is retried here.
            if not os.path.lexists(rfile):
                if tid in _SETTLE_PENDING or (tid not in _SETTLE_SCANNED and _result_disposed(tid)):
                    changed |= _settle_task(tid, inflight)
                _SETTLE_SCANNED.add(tid)
            continue
        # Before the tier guard: a withheld review would name the shared room as its release target.
        mention = _owner_mention_disposition(tid, raw)
        if mention is None:
            continue
        # The guard honours suppression on every tier now, so there is no stub
        # to pre-apply; the ordinary guarded path returns the body unchanged.
        body, _withheld = ("[no-send]", None) if mention else _guarded_result_body(tid, raw)
        if body is None:
            _log(f"result guard unavailable for {tid} — leaving for retry")
            continue
        if _withheld:
            _log(f"withheld non-owner result for {tid}: {_withheld}")
        # Route marker decisions through the unified parser (#873); the guard has run,
        # so a skip after [channel:] is a skip here, not text posted into that room.
        parsed = parse_markers(body, skip_after_channel=DELIVERY_SKIP_AFTER_CHANNEL)
        skip = next((a for a in parsed.actions if a.kind == "skip"), None)
        # Every dedup marker routes through the shared plan, malformed included:
        # it owns the reject-and-report policy (dedup_recovery.plan_dedup_recovery).
        if skip and skip.value == "deduped":
            action, payload, room = _dedup_plan(tid, skip.extra, inflight)
            if action == "wait":
                inflight.add(payload)
                changed = True
                _log(f"dedup {tid} waiting for holder {payload} gateway acceptance")
                continue
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
                    mention = _owner_mention_disposition(tid, payload)
                    if mention is None:
                        continue
                    if not _deliver_result_payload(tid, _broker_tid(_delivery),
                                                  "[no-send]" if mention else payload,
                                                  no_send=bool(mention), result_file=rfile,
                                                  generation=generation):
                        changed |= _settle_delivered(tid, _broker_tid(_delivery), rfile, inflight)
                        continue
                _holder = (skip.extra or "").strip()
                # An out-of-grammar holder is sender-controlled; name its shape,
                # never its bytes.
                _shown = (_holder if local_task_protocol.valid_archive_lookup_id(_holder)
                          else f"<malformed, {len(_holder)} chars>")
                _log(f"dedup {action} for {tid} (holder {_shown} is not a valid delivery for this task)")
                if not _archive_result(rfile, tid, generation):
                    continue
                inflight.discard(tid)
                _forget_task_room(tid)
                # A requeue carries the SAME delivery forward under a fresh local
                # id, so only a retired delivery gives up its media mode.
                if action != "requeue":
                    _forget_task_media(tid)
                if action == "report":
                    _forget_ack_first(_broker_tid(_delivery))
                    _forget_relay_counts(_broker_tid(_delivery), tid)
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
            _wire = _broker_tid(_delivery)
            # A `deduped` skip reaches here only once the planner honoured the fold.
            _folded = _fold_target(tid, _wire, skip.extra) if skip.value == "deduped" else ""
            if _folded is None:
                continue
            if not _deliver_result_payload(tid, _wire,
                                           "" if _folded else _lease_close_body(skip),
                                           no_send=not _folded, consolidated_into=_folded,
                                           result_file=rfile, generation=generation):
                changed |= _settle_delivered(tid, _wire, rfile, inflight)
                continue
            if not _archive_result(rfile, tid, generation):
                continue
            # Retire the provenance WITH the result, never at read: this line is
            # only reached once the lease-closing POST has actually succeeded.
            _REDELIVERED.discard(tid)
            inflight.discard(tid)
            _forget_task_room(tid)
            _forget_task_media(tid)
            # After the archive, so a re-run of an accepted fold still names its holder.
            _forget_ack_first(_wire)
            _forget_relay_counts(_wire, tid)
            changed = True
            _log(f"archived {tid} (marker {skip.value}, lease closed, not sent"
                 + (f", folded into {_folded})" if _folded else ")"))
            continue
        out_body = parsed.body
        redirect = next((a for a in parsed.actions if a.kind == "redirect"), None)
        if redirect:
            # Cross-room redirect is handled GATEWAY-side for this transport —
            # re-stitch the marker the parser stripped so the server still
            out_body = f"[channel: {redirect.value}]\n{out_body}"
        attaches = [a.value for a in parsed.actions if a.kind == "attach"]
        # Resolved before the uploads: a Signal task posts its media under the
        # DELIVERY id, and an unreadable ledger must defer rather than guess.
        _delivery = _delivery_tid(tid)
        if _delivery is None:
            _log(f"delivery deferred for {tid} — alias ledger unreadable")
            continue
        _wire = _broker_tid(_delivery)
        if attaches:
            _media = _load_task_media()
            if _media is None:
                _log(f"delivery deferred for {tid} — media sidecar unreadable")
                continue
            task_media = (_media.get(_wire) or {}).get("mode") == "task-media"
            room = _load_task_rooms().get(tid, "")
            sent = 0
            deferred = False
            for ordinal, fp in enumerate(attaches):
                # Uploads happen before the result POST (so failures can be
                # annotated in-band); if that POST then fails and this loop
                if (tid, fp) in _uploaded_attachments:
                    sent += 1
                    continue
                if task_media:
                    status, reason = _upload_task_attachment(_wire, ordinal, fp)
                else:
                    ok, reason = (_upload_attachment(room, fp) if room
                                  else (False, "origin room unknown"))
                    status = "ok" if ok else "note"
                if status == "defer":
                    # A retryable upload failure holds the whole result: the next
                    # scan re-offers the same wire id, ordinal and bytes.
                    _log(f"attachment deferred for {tid}: {fp} ({reason})")
                    deferred = True
                    break
                if status == "ok":
                    sent += 1
                    _uploaded_attachments.add((tid, fp))
                    _log(f"attached {fp} for {tid}")
                else:
                    # Keep the information in-band rather than dropping the
                    # file silently — mirrors the other bridges' rejection UX.
                    out_body += f"\n[attachment not sent: {fp} ({reason})]"
                    _log(f"attachment skipped for {tid}: {fp} ({reason})")
            if deferred:
                continue
            if not out_body.strip() and sent:
                out_body = "(file attached)"
        # A redirected answer leaves the asking room, so it never asks for a thread there.
        thread_ask = redirect is None and any(a.kind == "thread-ask" for a in parsed.actions)
        if not _deliver_result_payload(tid, _wire, out_body, result_file=rfile,
                                       generation=generation, thread_ask=thread_ask,
                                       dm_only=_dm_only(parsed)):
            changed |= _settle_delivered(tid, _wire, rfile, inflight)
            continue
        if not _archive_result(rfile, tid, generation):
            continue
        inflight.discard(tid)
        _forget_task_room(tid)
        _forget_task_media(tid)
        _forget_ack_first(_wire)
        _forget_relay_counts(_wire, tid)
        _forget_dedup_alias(tid)
        changed = True
        _log(f"archived result for {tid}")
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
    staged = _load_held() or {}                 # no task file until their ack is accepted
    gone = {tid for tid in inflight
            if _valid_local_tid(tid) and tid not in staged
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


def _names(base: str, tries: int = 64):
    """`<base>.txt`, then `<base>.<ns>.txt`: the shapes the archive and
    quarantine readers already parse."""
    yield f"{base}.txt"
    ns = time.time_ns()
    for i in range(tries - 1):
        yield f"{base}.{ns + i}.txt"


def _retire_orphan(rfile, directory, base: str, generation) -> bool:
    """The lifecycle owner moves the generation this pass classified; a reply
    that replaced it since stays live. True only for the requested disposition."""
    stem = Path(rfile).stem
    try:
        done = disposal.retire_generation(RESULTS_DIR, rfile, generation, _log, directory, _names(base))
    except OSError as e:
        _log(f"orphan sweep: {stem} could not be moved ({diagnostics.describe(e)}); left in place")
        return False
    if done.outcome is disposal.Retirement.FALLBACK:
        _log(f"orphan sweep: {stem} was not moved to {Path(directory).name}/: {done.cause}")
    elif done.outcome is disposal.Retirement.FAILED:
        _log(f"orphan sweep: {stem} could not be moved ({done.cause}); left in place")
    return done.retired


def _quarantine_orphan(rfile, tid: str, reason: str, generation) -> bool:
    """Never replaces prior quarantined evidence, under collision."""
    return _retire_orphan(rfile, UNDELIVERABLE_RESULTS_DIR, f"{tid}.{reason}.{int(time.time())}",
                          generation)


def _relay_orphan(tid: str, inflight: "set[str]") -> "bool | None":
    """A result outside the in-flight set that answers a relay ask: True once its id is back
    in flight, None while that cannot be told for a voice ask, False for any other result."""
    delivery = _delivery_tid(tid)
    if delivery is None:
        return None if _task_source(tid) == _VOICE_ASK_SOURCE else False
    relay = _relay_result(tid, _broker_tid(delivery))
    if relay and _track_durably(tid, inflight):
        _log(f"orphan sweep: {tid} answers a relay ask — back in flight for the drain, not handled here")
    return relay


def _keep_relay_results_in_flight(inflight: "set[str]") -> bool:
    """Put back in flight each live result a kept mark names, a re-ask's included, so the
    drain posts it under the relay rule. Its id leaves flight when the task is dropped as
    abandoned before the result lands, or when the ledger is lost; on the primary lane no
    other pass ever reads an `ask-…` result. True when an id was added."""
    marks = _load_ack_first()
    if not marks:
        return False
    aliases = _load_dedup_aliases() or {}
    added = False
    for broker_id, local in sorted(marks.items()):
        if not (_valid_tid(broker_id) and isinstance(local, str) and _valid_local_tid(local)):
            continue
        answers = {local} | {k for k, v in aliases.items() if v == local and _valid_local_tid(k)}
        for tid in sorted(answers - set(inflight)):
            if os.path.lexists(RESULTS_DIR / f"{tid}.txt") and _track_durably(tid, inflight):
                _log(f"result {tid}: a relay ask's result outside the in-flight set — back in flight")
                added = True
    return added


def _reconcile_orphan_results(inflight: "set[str]") -> None:
    global _last_orphan_sweep
    now = time.time()
    if now - _last_orphan_sweep < ORPHAN_SWEEP_EVERY_S:
        return
    _last_orphan_sweep = now
    _recover_disposing_claims()
    try:
        candidates = sorted(RESULTS_DIR.glob("task-*.txt"))
    except OSError:
        return
    for rfile in candidates:
        tid = rfile.stem
        if not _valid_local_tid(tid) or tid in inflight:
            continue
        # Before any orphan policy: a relay ask's result goes back in flight for the drain,
        # and waits while that cannot be told; it is never relabelled or quarantined here.
        if _relay_orphan(tid, inflight) is not False:
            continue
        task = find_task_file(TASKS_DIR, tid) or _archived_task_file(tid)
        if task is not None:
            try:
                headers = local_task_protocol.parse_task_headers(
                    task.read_text(encoding="utf-8", errors="replace")).headers
            except OSError:
                continue
            # Cron completions belong to the local scheduler, not a gateway lease.
            # Leave their delivery and retirement to the local consumers.
            if headers.get("source") == "cron":
                continue
        # One open decides every arm below, and only that generation is retired.
        try:
            data, generation = identity_of(rfile)
        except OSError:
            continue
        age = now - generation.mtime_ns / 1e9
        if age < ORPHAN_GRACE_S:
            continue                            # young: normal path may claim it
        if age > ORPHAN_MAX_AGE_S:
            # A minimum age alone lets an automatic pass replay an unbounded
            # historical backlog into live rooms; backfill must be deliberate.
            if _quarantine_orphan(rfile, tid, "too-old", generation):
                if tid not in _orphan_quarantine_logged:
                    _orphan_quarantine_logged.add(tid)
                    _log(f"orphan sweep: {tid} is {int(age)}s old (>{int(ORPHAN_MAX_AGE_S)}s) "
                         "— quarantined rather than replayed")
            continue
        # Delivered copy = double-write. NEVER re-deliver: the sweep would
        # post agent narration about having answered into the room.
        if _delivered_copy_exists(tid):
            _delivery = _delivery_tid(tid)
            raw = ready_body_of(data)
            if _delivery is None or raw is None:
                continue                        # ledger unreadable or not ready: a later sweep decides
            _item = _broker_tid(_delivery)
            if find_task_file(TASKS_DIR, tid) is not None and not _track_durably(tid, inflight):
                _log(f"orphan sweep: {tid} could not be saved as in flight; its reply stays "
                     "live for a later sweep")
                continue                        # the live reply is the only restart-durable trace
            _root = getattr(_delivery_core().backend, "root", None)
            if _root is not None and delivered_body_differs(_root, _item, raw):
                _quarantine_unsent(rfile, tid, _item, generation)
            elif _retire_orphan(rfile, ARCHIVE_RESULTS_DIR, f"{tid}-{int(now)}-late-duplicate",
                                generation):
                _log(f"orphan sweep: {tid} is a post-delivery duplicate — moved aside")
            if _settle_delivered(tid, _item, rfile, inflight):
                _save_inflight(inflight)
            continue
        # No task anywhere: nothing resolves a destination — quarantine,
        # never a labeled re-delivery (permanent sweep error otherwise).
        if task is None:
            if not _quarantine_orphan(rfile, tid, "no-task", generation):
                continue
            if tid not in _orphan_quarantine_logged:
                _orphan_quarantine_logged.add(tid)
                _log(f"orphan sweep: {tid} has no task file — quarantined")
            continue
        # Genuinely undelivered: ONE labeled attempt — at-least-once by
        # design; the label makes the rare duplicate self-explaining.
        raw = ready_body_of(data)
        if raw is None:
            continue                            # blank or a partial write: readable on a later pass
        delivery = _delivery_tid(tid)
        if delivery is None:
            continue                            # alias ledger unreadable: retry later
        mention = _owner_mention_disposition(tid, raw)
        if mention is None:
            continue
        # Recovery is still a delivery: the ordinary path's guard runs BEFORE
        # any marker is interpreted, so tier + suppression cannot be skipped.
        body, _withheld = ("[no-send]", None) if mention else _guarded_result_body(tid, raw)
        if body is None:
            _log(f"orphan sweep: result guard unavailable for {tid} — leaving for retry")
            continue
        if _withheld:
            _log(f"orphan sweep: withheld non-owner result for {tid}: {_withheld}")
        parsed = parse_markers(body, skip_after_channel=DELIVERY_SKIP_AFTER_CHANNEL)
        if [a for a in parsed.actions if a.kind == "attach"]:
            # Delivering without the files would silently drop them — park
            # for a human instead of composing a partial delivery.
            if _quarantine_orphan(rfile, tid, "has-attachments", generation):
                _log(f"orphan sweep: {tid} carries attachments — quarantined")
            continue
        skip = next((a for a in parsed.actions if a.kind == "skip"), None)
        if skip and skip.value == "deduped":
            # _dedup_plan reports or requeues when the holder delivered
            # nothing; posting here would retire the ask without that check.
            if _quarantine_orphan(rfile, tid, "deduped-orphan", generation):
                _log(f"orphan sweep: {tid} defers to its dedup holder — quarantined")
            continue
        if skip:
            # A suppression marker moves no data: the canonical close rides the
            # wire, not the sender's prose, and no_send gates delivery.
            labeled = _lease_close_body(skip)
        else:
            labeled = _RECOVERED_LABEL + parsed.body
            _r = next((a for a in parsed.actions if a.kind == "redirect"), None)
            if _r:
                labeled = f"[channel: {_r.value}]\n{labeled}"
        # Same outcome owner as the live drain: a 2xx {"ok": false} is a
        # refusal, and an unconfirmed close must keep its retryable result.
        _btid = _broker_tid(delivery)
        # Delivery may move the result; the id is restart-durable before anything can.
        if not _track_durably(tid, inflight):
            _log(f"orphan sweep: {tid} could not be saved as in flight; its result stays "
                 "live for a later sweep")
            continue
        if _deliver_result_payload(tid, _btid, labeled, no_send=bool(skip), result_file=rfile,
                                   generation=generation, dm_only=_dm_only(parsed)):
            _log(f"orphan sweep: recovered + gateway accepted {tid}; Matrix delivery unconfirmed")
            if _archive_result(rfile, tid, generation):
                inflight.discard(tid)           # settled: the in-flight entry was only a marker
                _save_inflight(inflight)
                _forget_ack_first(_btid)
            continue
        _item = _effective_item_id(_delivery_core(), _btid, bool(skip))
        if _delivery_core().backend.is_terminal(_item):
            if _settle_delivered(tid, _btid, rfile, inflight):
                _save_inflight(inflight)        # disposed and logged by the delivery call
            continue
        inflight.discard(tid)                   # a retryable failure: the sweep tries again
        _save_inflight(inflight)
        _tries = _delivery_core().backend.attempts(_item)
        _log(f"orphan sweep: {tid} close not confirmed (attempt {_tries}) — will retry")


# ── MC1 per-workspace singleton (dual-poller guard) ─────────────────────────
# Exactly one gateway-bridge may poll a given workspace's relay bearer. A second
_LOCK_ROLE = f"gateway-bridge{_INST_SUFFIX}"  # per-instance: dual-poller guard stays per-gateway
_LOCK_WS = _STATE.parent  # _STATE = <workspace>/state (injected) or ~/.ag2-sparrow/state


_LOCK_STALE_S = _WS_STALE_SECONDS
# A loop pass (long poll + backoff + retries) can outlast the stale window, so
# the lock is refreshed off-loop; the loop only reads the verdict.
_LOCK_LOST = threading.Event()
_LOCK_HB_MUTEX = threading.Lock()
_POLL_BACKOFF_MAX_S = 60
# Typical worst case for one pass (a queued retry backlog can exceed it): the
# long poll at its timeout, the largest backoff and two requests, doubled.
_LOCK_PASS_MAX_S = 2 * (POLL_WAIT + 10 + _POLL_BACKOFF_MAX_S + 2 * _REQ_TIMEOUT_S)
_LOOP_TICK = {"at": time.monotonic()}


def _stamp_loop_progress() -> None:
    _LOOP_TICK["at"] = time.monotonic()


def _lock_on() -> bool:
    return os.environ.get("SUTANDO_BRIDGE_LOCK", "1") != "0"


def _release_singleton() -> None:
    if not _lock_on():
        return
    # The ORDER is the invariant, not the registration order of two atexit hooks
    # (atexit runs them in reverse, so that ordering released the lock first).
    _OWNERSHIP_RELINQUISHED.set()
    if not _join_push_thread():
        # Still in flight, and the request cannot be recalled. Holding the lock
        # costs a successor the stale window; releasing costs it its card.
        _log("singleton: a publication is still in flight — holding the lock so "
             "a successor waits for it to go stale instead of having its "
             "advertisement overwritten by this generation")
        try:
            _ws_retain(_LOCK_ROLE, _LOCK_WS)  # else our dead pid gets it reaped at once
        except Exception as e:
            _log(f"singleton: retain failed ({diagnostics.describe(e)}) — the held lock can be reaped once this pid exits")
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
    with _LOCK_HB_MUTEX:
        try:
            held = bool(_ws_heartbeat(_LOCK_ROLE, _LOCK_WS))
        except Exception:
            return True
        if not held:
            _LOCK_LOST.set()
        return held


def _lock_heartbeat_loop() -> None:
    """Refresh the lock only while the main loop keeps making passes. A loop
    stuck past the pass bound is a hung holder, and the lock must go stale for
    it exactly as it did when the loop refreshed the lock itself."""
    interval = max(0.5, _LOCK_STALE_S / 3)
    stalled = False
    while not _OWNERSHIP_RELINQUISHED.wait(interval):
        idle = time.monotonic() - _LOOP_TICK["at"]
        if idle > _LOCK_PASS_MAX_S:
            if not stalled:
                stalled = True
                _log(f"singleton: main loop has made no progress for {idle:.0f}s "
                     f"(bound {_LOCK_PASS_MAX_S:.0f}s) — letting the lock go stale "
                     "so a successor can take over")
            continue
        stalled = False
        if not _heartbeat_singleton():
            return


def _start_lock_heartbeat() -> None:
    _stamp_loop_progress()
    if _lock_on():
        threading.Thread(target=_lock_heartbeat_loop, name="sparrow-lock-heartbeat",
                         daemon=True).start()


def _exit_on_signal(signum, _frame) -> None:
    """Exit 0 as before, but say which signal ended the process."""
    line = f"[remote-gateway-bridge] received {signal.Signals(signum).name} — exiting"
    # Each sink on its own: a signal that lands inside print makes the next
    # print raise, and that must cost neither the file line nor the exit.
    try:
        _append_log_file(line)
    except Exception:  # noqa: BLE001
        pass
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 — the raw fd is reentrancy-safe
        try:
            os.write(1, (line + "\n").encode())
        except Exception:  # noqa: BLE001
            pass
    sys.exit(0)


def _lock_declares() -> dict:
    """What this drain states in its lock as it takes it: the proactive send-lock protocol it
    honours and its process start, so a reader judges the holder by its lock alone."""
    return {"send_lock": _delivery_destination.SEND_LOCK_PROTOCOL,
            "start": agent_identity.own_start()}


def _acquire_singleton() -> bool:
    """True → we hold the poller lock (or it is disabled / errored → fail-open).
    False → a live bridge already owns this workspace and the caller must NOT poll."""
    if not _lock_on():
        return True
    try:
        r = _ws_acquire(_LOCK_ROLE, _LOCK_WS, stale_seconds=_LOCK_STALE_S, declares=_lock_declares())
    except Exception as e:  # fail-open — never wedge task delivery on a lock bug
        _log(f"singleton: acquire error ({diagnostics.describe(e)}) — proceeding without lock")
        return True
    if r.status == "deferred":
        h = r.holder or {}
        _log(f"singleton: another live gateway-bridge owns this workspace "
             f"(host={h.get('host')} pid={h.get('pid')}) — exiting to avoid dual-poll")
        return False
    _LOCK_LOST.clear()
    atexit.register(_release_singleton)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _exit_on_signal)
        except Exception:
            pass  # non-main-thread or platform without the signal — atexit still covers exit
    _log(f"singleton: acquired workspace poller lock ({r.status})")
    return True


def _settle_proactive_room() -> None:
    """Ask the host whether this bridge keeps PROACTIVE_ROOM now that it holds the lease."""
    global PROACTIVE_ROOM
    if not PROACTIVE_ROOM or PROACTIVE_ROOM_KEEP is None:
        return
    try:
        keep = PROACTIVE_ROOM_KEEP()
    except Exception as e:  # noqa: BLE001 — the room stays as launched
        _log(f"proactive room: keep check failed ({diagnostics.describe(e)}) — keeping it")
        return
    if not keep:
        PROACTIVE_ROOM = ""
        _log("proactive room: another bridge is primary — this one carries no proactive room")


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
            # HITL card clicks ride the same owner-only chain, ahead of taskify.
            claimants = [DecisionHandler(store, ha_owner, log=_log), _ClaudeLocalClicks(ha_owner, log=_log)]
            hitl = _hitl_reply_handler(ha_owner, log=_log)
            if hitl is not None:
                claimants.append(hitl)
            handler = HandlerChain(claimants + [handler])
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
                    _log(f"event drain error (isolated): {diagnostics.describe(e)}")
                time.sleep(2.0)
        threading.Thread(target=_drain_loop, name="sparrow-event-drain", daemon=True).start()
        _log("event channel + consumer started (SPARROW_EVENTS enabled) — isolated "
             "daemon threads, task delivery unaffected")
    except Exception as e:  # noqa: BLE001 — event startup must NEVER break tasks
        _log(f"event channel start failed (task delivery unaffected): {diagnostics.describe(e)}")


def _poll_timeout_is_empty(last_ok: float, now: float,
                           grace: float = POLL_TIMEOUT_GRACE_S) -> bool:
    """Whether a long-poll read timeout should be read as `{"tasks": []}`.

    False once no poll has succeeded within `grace`, so a wedged relay still
    reaches the outage path instead of looping quietly forever.
    """
    return (now - last_ok) <= grace


def _poll_stalled(last_ok: float, now: float,
                  limit: float = POLL_STALL_EXIT_S) -> bool:
    """Whether the poll loop has gone `limit` seconds without a success."""
    return limit > 0 and (now - last_ok) > limit


_STALL_REPORTED_FOR: float | None = None
# Set once the stall exit has stopped the outbound worker: from then on the loop
# only waits for that stop, because a resumed poll would run with no outbound worker.
_STALL_EXIT_PENDING = False


def _worker_stop_budget_s() -> float:
    """One join's wait: a full in-flight request plus the worker's scan slack."""
    return _OUTBOUND_SEND_TIMEOUT_S + OUTBOUND_SCAN_S * 3 + 5


def _stop_background_workers(outbound_thread, results_watcher) -> bool:
    """Stop and join the outbound worker; True only once it has actually stopped.

    Exiting while it is mid-send strands its claim, and the next process resends it.
    The results watcher is only a wake-up doorbell, so its join is best-effort.
    """
    _OUTBOUND_STOP.set(); _OUTBOUND_WAKE.set()
    outbound_thread.join(timeout=_worker_stop_budget_s())
    if results_watcher is not None:
        results_watcher.join(timeout=5)
    return not outbound_thread.is_alive()


def _abort_if_poll_stalled(anchor: float,
                           before_exit: Callable[[], bool] | None = None) -> bool:
    """Record a wedged-but-alive bridge, and exit only if something restarts us.

    `anchor` is a `time.monotonic()` reading: wall time would count a sleeping Mac
    as a stall. The exit is armed only when a restart owner is declared, since an
    unsupervised lane that exits stays down; the status sidecar records it either way.
    `before_exit` stops the workers and reports whether they stopped; until they
    have, this returns True and the caller must skip the beat rather than poll.
    """
    global _STALL_REPORTED_FOR, _STALL_EXIT_PENDING
    now = time.monotonic()
    # Pass the limit explicitly: the parameter default binds at def time,
    # so a reconfigured module global would otherwise never be read.
    if not _STALL_EXIT_PENDING and not _poll_stalled(anchor, now, POLL_STALL_EXIT_S):
        return False
    stalled_s = int(now - anchor)
    if not POLL_STALL_RESTART_OWNER:
        # Once per stall episode, not once per poll: the loop calls this on every
        # iteration and the sidecar is a file write.
        if _STALL_REPORTED_FOR != anchor:
            _STALL_REPORTED_FOR = anchor
            _emit_gateway_status(False, error=(
                f"stalled: no successful poll in {stalled_s}s (limit "
                f"{POLL_STALL_EXIT_S:g}s); retrying -- no restart owner declared, "
                "so exiting would end this lane"))
            _log(f"WARN: no successful poll in {stalled_s}s and no restart owner "
                 "(SUTANDO_BRIDGE_RESTART_OWNER unset) -- continuing to retry")
        return False
    if not _STALL_EXIT_PENDING:
        _STALL_EXIT_PENDING = True
        _emit_gateway_status(False, error=f"stalled: no successful poll in {stalled_s}s")
        # sys.exit's message reaches only stderr; the bridge log is where the backoff
        # lines that preceded this live, so the stall must be stated there too.
        _log(f"stalled: no successful poll in {stalled_s}s (limit {POLL_STALL_EXIT_S:g}s) "
             f"while the process stayed alive -- exiting so {POLL_STALL_RESTART_OWNER} restarts it")
    if before_exit is not None and not before_exit():
        _log("stall exit held: the outbound worker is still finishing a send -- "
             "waiting for it to stop before exiting, without polling")
        return True
    print(f"FATAL: no successful poll in {stalled_s}s (limit {POLL_STALL_EXIT_S:g}s) "
          f"-- exiting so {POLL_STALL_RESTART_OWNER} restarts the bridge.", file=sys.stderr)
    sys.exit(POLL_STALL_EXIT_CODE)


def main() -> None:
    # This process holds the gateway URL: nothing uncaught, in any thread, prints it raw.
    gateway_instance.install_scrubbing_excepthooks(lambda: URL)
    if _FOUND.fatal:
        _emit_gateway_status(False, error=f"FATAL: {_FOUND.fatal}")
        if gateway_credential_sources.is_seat(os.environ):
            time.sleep(SEAT_FATAL_BACKOFF_S)
        sys.exit(f"FATAL: {_FOUND.fatal}")
    if not TOKEN:
        sys.exit("FATAL: set REMOTE_TASK_TOKEN (the onboarding string, or a bare secret with REMOTE_TASK_URL).")
    if not URL and gateway_credential_sources.credentials_injected(os.environ) \
            and gateway_credential_sources.token_url_shaped(_RAW):
        sys.exit("FATAL: no gateway URL for this declared bridge — its vault token starts with a URL scheme but "
                 "has no url|secret separator. If it is a combined token that lost its '|', store it again under "
                 "its AG2_RELAY_TOKEN_<worker> vault key as 'https://<gateway>|<secret>'; if it is an opaque "
                 "bearer, store the agent's gateway URL under its AG2_RELAY_URL_<worker> key; "
                 "`python3 src/sparrow_agent_bridge.py --preflight` names both keys.")
    if not URL and gateway_credential_sources.credentials_injected(os.environ):
        # A declared bridge's environment URLs are stripped: only the owner's vault entries can supply it.
        sys.exit("FATAL: no gateway URL for this declared bridge — its vault token is a bare secret. Store the "
                 "agent's gateway URL under its AG2_RELAY_URL_<worker> vault key, or the combined "
                 "'https://<gateway>|<secret>' token under its AG2_RELAY_TOKEN_<worker> key; "
                 "`python3 src/sparrow_agent_bridge.py --preflight` names both keys.")
    if not URL:
        # A token that starts with a URL scheme but yielded no URL means the
        # url|secret separator was swallowed (e.g. a %7C survived decoding, or a
        _hint = (" — the token carries a gateway URL but the url|secret separator "
                 "looks missing/corrupted" if gateway_credential_sources.token_url_shaped(_RAW) else "")
        sys.exit("FATAL: no gateway URL — set REMOTE_TASK_URL, or use the combined "
                 f"'https://<gateway>|<secret>' onboarding token{_hint}.")
    _resolve_host_label()
    if not _acquire_singleton():
        # A live bridge already polls this workspace: stand down. 75 tells a
        # supervising wrapper not to relaunch; a plain exit would loop it.
        sys.exit(75)
    # Heartbeat first: the settle can wait on the connections record lock past the stale window.
    _start_lock_heartbeat()
    _settle_proactive_room()
    _lane_identity()  # published at start, so Hatch's fail-closed window is only startup
    atexit.register(_retract_own_lane)
    inflight: set[str] = _load_inflight()
    _recover_orphan_proactive()
    for sweep in (_retire_delivered_marks, _sweep_held_dir):
        try:
            sweep()
        except Exception as e:  # noqa: BLE001 — what a sweep leaves waits for the next start
            _log(f"{sweep.__name__} failed ({diagnostics.describe(e)}) — continuing")
    try:
        # Before the drain and the orphan sweep, so the body is back in flight under its id.
        _reclaim_parked_relay_results(inflight)
        _keep_relay_results_in_flight(inflight)
    except Exception as e:  # noqa: BLE001 — a parked result left behind waits for the next start
        _log(f"relay result reclaim failed ({diagnostics.describe(e)}) — continuing")
    abandoned_suspects: set[str] = set()
    _log(f"starting — gateway={_redact_url(URL)} provider={PROVIDER} tasks={TASKS_DIR} "
         f"(restored {len(inflight)} in-flight)")
    # Always name where the diagnostics live: after an incident this line is the
    # trailhead (a bare-launched bridge under default dirs writes status to
    _log(f"launched_via={_LAUNCHED_VIA} status={GATEWAY_STATUS_FILE}")
    if _LAUNCHED_VIA == "bare":
        _log(f"running unsupervised — output also logged to {_LOG_FILE}; "
             f"prefer launching through startup.sh for full diagnostics")
    backoff = 1
    poll_grace_since = time.time()
    last_poll_ok: float | None = None
    poll_failures = 0  # consecutive network failures; the next good poll resets it
    stall_anchor = time.monotonic()  # excludes system sleep on macOS, unlike time.time()
    _emit_gateway_status(False, error="starting — not yet connected")
    refresh_routing(force=True)  # the owner's DM comes from the gateway at connect, not from a pin
    _maybe_start_event_channel()  # additive/opt-in/isolated — never blocks the task loop
    _results_watcher = _start_results_watcher()
    _outbound_thread = _start_outbound_worker(inflight)
    while True:
        _stamp_loop_progress()
        try:
            if _abort_if_poll_stalled(stall_anchor, lambda: _stop_background_workers(
                    _outbound_thread, _results_watcher)):
                continue
            if _LOCK_LOST.is_set() or not _heartbeat_singleton():
                # Lost the poller lock (reaped after being deemed stale). Stop
                # polling immediately so we don't dual-poll the relay bearer with
                _log("singleton: lost workspace poller lock (reaped after stale takeover) "
                     "— exiting to avoid dual-poll")
                if not _stop_background_workers(_outbound_thread, _results_watcher):
                    # The successor recovers a dead pid's claim and resends it, so a
                    # send in flight must finish here first; the next pass re-joins.
                    _log("singleton: outbound worker still finishing a send — "
                         "waiting for it to stop before exiting")
                    _LOCK_LOST.set()  # the next pass must come back here, never poll
                    continue
                return
            _post_heartbeat(inflight)
            _retry_withheld_reviews()
            # LAST of the beat's work, on a daemon thread: two optional 15 s
            # requests never delay the next task poll or a durable retry.
            _push_pool_advertisement()
            if _advance_held_tasks(inflight):   # a retried ack, or a publish after a restart
                wake_outbound()
            prev_ok = poll_grace_since
            _RESP_META.last = None
            try:
                resp = _req("GET", f"/v1/tasks?wait={POLL_WAIT}{_POLL_PROOF}", timeout=POLL_WAIT + 10)
                last_poll_ok = poll_grace_since = time.time()
                stall_anchor = time.monotonic()
                # A completed poll remains evidence even if accepting its tasks fails.
                _emit_gateway_status(True, poll_ok_ts=last_poll_ok)
            except (TimeoutError, socket.timeout):
                # Read timeout only (URLError takes the outage path below).
                # socket.timeout only aliases TimeoutError on 3.10+, not 3.9.
                if not _poll_timeout_is_empty(poll_grace_since, time.time()):
                    raise
                _log(f"poll read timed out after {POLL_WAIT + 10}s inside the "
                     f"{POLL_TIMEOUT_GRACE_S:g}s grace — treated as an empty poll")
                resp = {"tasks": []}
            received_at = time.monotonic()           # the dropped-ack retry budget runs from here
            if poll_failures and poll_grace_since != prev_ok:  # a real answer, not the grace path
                _log(f"poll recovered after {poll_failures} failures, {int(poll_grace_since - prev_ok)}s")
                poll_failures = 0
            added = False
            pending_ack = []
            tasks, fates = _receive_round(_poll_task_list(resp))
            # Every task's first ack goes before any retry, so one bad link delays no sibling.
            dropped: list = []
            # First and on its own, so a slow task ahead cannot hold it past its delivery lease.
            for task in tasks:
                if task.get("ack_first") is True:
                    added |= _isolated(task, _take_ack_first, task, inflight, fates, dropped,
                                       fates=fates)
            for task in tasks:
                if task.get("ack_first") is not True:
                    added |= _isolated(task, _take_served_task, task, inflight, pending_ack,
                                       fates, fates=fates)
            # Ack only after task file, in-flight set and ack ledger are durable;
            # any failure withholds every ack this round, redeliveries included.
            committed = _save_inflight(inflight) if pending_ack else True
            if pending_ack and committed:
                committed = _record_pending_acks(pending_ack)
            if pending_ack and not committed:
                _log(f"acks withheld for {len(pending_ack)} task(s): the in-flight set or "
                     "ack ledger did not commit")
            for tid, durable, lease in pending_ack if committed else ():
                if _post_task_ack(tid, durable, lease, dropped=dropped):
                    fates[_broker_tid(tid)] = "acked"
            _retry_dropped_acks(dropped, received_at, fates)
            _settle_round(fates)
            if added:
                wake_outbound()          # a fresh task often precedes its ack round-trip
            abandoned_suspects = _reconcile_abandoned(inflight, abandoned_suspects)
            if _keep_relay_results_in_flight(inflight):
                wake_outbound()
            _reconcile_orphan_results(inflight)
            _post_heartbeat(inflight)
            # The pool's advertisement rides the same beat: push-on-change, never
            # raising, so a broker without the endpoints costs one deferred log.
            _push_pool_advertisement()
            backoff = 1  # healthy round-trip → reset backoff
            _emit_gateway_status(last_poll_ok is not None, poll_ok_ts=last_poll_ok)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                if _recover_auth(e.code):
                    backoff = 1
                    # The wait was on a human, not a stall: resume with the same
                    # fresh anchor a newly started bridge gets.
                    stall_anchor = time.monotonic()
                    continue
                _emit_gateway_status(False, error=f"auth rejected HTTP {e.code}", poll_ok_ts=last_poll_ok)
                sys.exit(f"FATAL: gateway auth rejected (HTTP {e.code}) — check REMOTE_TASK_TOKEN.")
            _log(f"poll HTTP {e.code} — backing off {backoff}s")
            _emit_gateway_status(False, error=f"HTTP {e.code}", backoff_s=backoff, poll_ok_ts=last_poll_ok)
            time.sleep(backoff); backoff = min(backoff * 2, _POLL_BACKOFF_MAX_S)
        except _PollAnomaly:
            # Every anomaly is counted in its line; the log itself thins out a run of them.
            _log_backoff(("poll-anomaly",), f"{_POLL_ANOMALY_LINE} — backing off {backoff}s")
            _emit_gateway_status(False, error="poll answered without a task list", backoff_s=backoff,
                                 poll_ok_ts=last_poll_ok)
            time.sleep(backoff); backoff = min(backoff * 2, _POLL_BACKOFF_MAX_S)
        except _NETWORK_ERRORS as e:
            poll_failures += 1
            _log(f"poll network error: {diagnostics.describe(e)} — {poll_failures} consecutive, "
                 f"{int(time.time() - poll_grace_since)}s since last successful poll — "
                 f"backing off {backoff}s")
            _emit_gateway_status(False, error=f"network: {diagnostics.describe(e)}", backoff_s=backoff,
                                 poll_ok_ts=last_poll_ok)
            time.sleep(backoff); backoff = min(backoff * 2, _POLL_BACKOFF_MAX_S)
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            _log(f"unexpected: {diagnostics.describe(e)} — backing off {backoff}s")
            _emit_gateway_status(False, error=f"unexpected: {diagnostics.describe(e)}", backoff_s=backoff,
                                 poll_ok_ts=last_poll_ok)
            time.sleep(backoff); backoff = min(backoff * 2, _POLL_BACKOFF_MAX_S)


if __name__ == "__main__":
    main()
