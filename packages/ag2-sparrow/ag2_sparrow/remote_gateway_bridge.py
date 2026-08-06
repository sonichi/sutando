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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Prefer IPv4 for gateway/relay connections. The relay host (e.g. chat.ag2.space)
# publishes AAAA records, but some hosts have IPv6 black-holed at the network
# (the SYN is silently dropped, not refused). Python's getaddrinfo returns v6
# first, so each fresh urllib connection — this bridge opens one per long-poll
# AND one per outbound send, with no keep-alive — hangs on the dead v6 address
# for the full TCP connect timeout (~26s observed) before falling back to v4,
# which connects in <1s. That timeout is added to EVERY inbound message and
# EVERY reply, so the owner sees ~26s each way and messages look dropped. We
# filter getaddrinfo to A (v4) records for the gateway host so the dead v6 path
# is never tried; we keep the original result when there is no v4 address, so a
# genuinely v6-only destination still resolves. Opt out with
# REMOTE_GATEWAY_ALLOW_IPV6=1 (hosts with working v6 lose nothing either way).
# DNS resolution has NO native timeout: getaddrinfo blocks the caller until the
# resolver answers or the OS gives up (which can be minutes, or never on a
# captive portal / dropped link mid-query). urllib's socket timeout covers
# connect+read but NOT name resolution — so without a bound, a hung resolver
# wedges the long-poll loop indefinitely with no "reconnecting" status write and
# no self-recovery (observed on a tester's machine 2026-07-25: gateway process
# stuck, DNS for space.ag2.space failing, UI showing "reconnecting" forever).
# Bounding it lets the loop raise → emit gateway-status reconnecting → back off →
# retry, so the connection self-heals the moment DNS recovers. Override the bound
# with REMOTE_GATEWAY_DNS_TIMEOUT (seconds); 0/negative disables it.
_DNS_TIMEOUT_S = float(os.environ.get("REMOTE_GATEWAY_DNS_TIMEOUT") or "8")
_PREFER_V4 = os.environ.get("REMOTE_GATEWAY_ALLOW_IPV6") != "1"
# Reload-safe original capture: on module re-exec/reload, socket.getaddrinfo is
# already our wrapper — capturing it blindly makes _resolve_bounded call itself
# (RecursionError). The installed wrapper carries the TRUE original on its
# `_ag2_orig_getaddrinfo` attribute, so re-executions pick that up instead.
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
# hung system resolver — every retry for the same key attaches to it instead
# of spawning another thread, so a persistently hung resolver pins exactly
# one thread no matter how many times the poll loop retries. The worker
# removes its slot when the underlying call finally returns, so recovery
# drains cleanly and the next call starts fresh.
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
# src/ and pointed outside the repo).
from ._dirs import task_dir as _task_dir, result_dir as _result_dir, state_dir as _state_dir
from .chat_secret_filter import filter_chat_secrets, secret_handling_instruction
from .task_archive import find_task_file
from . import local_task_protocol
from .result_markers import parse_markers
from .result_ready import read_ready_result
from .result_markers import dedup_holder_delivered
from .local_task_protocol import find_archived_result
from .send_allowlist import is_path_sendable
from .workspace_lock import acquire as _ws_acquire, heartbeat as _ws_heartbeat, release as _ws_release

TASKS_DIR = _task_dir()
RESULTS_DIR = _result_dir()
_STATE = _state_dir()
ARCHIVE_RESULTS_DIR = RESULTS_DIR / "archive"
# Named-instance support (multi-gateway): one core may run SEVERAL bridge
# processes, each pointed at a different gateway (e.g. prod + dev homeservers)
# via its own REMOTE_TASK_TOKEN env. GATEWAY_INSTANCE names this process's
# instance; it suffixes the per-BRIDGE state files below and the singleton lock
# role, so two instances never clobber each other's ledgers or contend one
# lock. Unset (default) keeps every filename byte-identical to before — the
# single-bridge install sees zero change. Deliberately NOT suffixed:
# tasks/ + results/ (the shared task bus — the core is one consumer),
# last-owner-activity.json (owner presence is one signal regardless of which
# door the message came through), and core-status.json (the core's, not ours).
# THE instance-name contract — single source of truth. The import guard below
# and _LOCAL_TID_RE both derive from this pattern, because every drift between
# them has produced the same bug class (queue + ACK + silently-stranded
# results): first a length mismatch (>32 chars, review P1 round 5), then a
# charset mismatch (str.isalnum() accepts Unicode letters the ASCII regex
# rejects — GATEWAY_INSTANCE=é imported fine and stranded results, round 6).
# ASCII [A-Za-z0-9_-], 1-32 chars, and nothing else — if this ever needs to
# change, it changes HERE and both consumers follow.
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

# Persist the in-flight set (tasks pulled from the gateway, awaiting result-POST)
# so a client restart between pull and POST doesn't strand the result. Scoped to
# gateway-pulled tasks only — we must NOT blindly POST every results/ file, or we'd
# cross-send other channels' (Discord/Telegram) results to the gateway. The
# per-instance suffix is what keeps a second bridge from claiming results this
# instance delegated (each instance only POSTs ids in ITS OWN ledger).
INFLIGHT_FILE = _STATE / f"remote-task-inflight{_INST_SUFFIX}.json"
# Sidecar map {task id → origin room id}, recorded at queue time. Outbound
# file-attach needs the room because media uploads go to the room-scoped
# endpoint (POST /v1/rooms/{room}/media) while text results go to /v1/results
# (which resolves the room server-side). Separate file — the inflight ledger's
# list-of-ids format stays untouched for compat.
TASK_ROOMS_FILE = _STATE / f"remote-task-rooms{_INST_SUFFIX}.json"
# Liveness of the gateway *connection* itself (distinct from _post_heartbeat,
# which pings the broker). A local supervisor (e.g. the desktop app's
# sutando-ctl.sh) reads this to show connected-vs-reconnecting instead of
# guessing from tmux-window presence. Written on every poll outcome: connected
# after a healthy round-trip, reconnecting in the backoff branches.
GATEWAY_STATUS_FILE = _STATE / f"gateway-status{_INST_SUFFIX}.json"

# Launch provenance + in-bridge file log. A supervisor that persists stdout
# (sutando's startup.sh redirects it to logs/remote-gateway-bridge.log) exports
# SUTANDO_SUPERVISED=1, and _log stays stdout-only — byte-identical to before.
# Launched any other way ("bare": a hand-run of the script, a debug shell, an
# app spawn that forgot the redirect), stdout persists nowhere — the exact
# diagnostic hole of the 2026-07-25 tester wedge (bridge stuck 21h, zero logs
# or discoverable status to read). So a bare launch ALSO appends every _log
# line to <state-parent>/logs/gateway-bridge.log (<workspace>/logs/ when
# sutando injects dirs, ~/.ag2-sparrow/logs/ under defaults), size-capped with
# a single .1 rotation, best-effort — log I/O must never break the bridge.
_LAUNCHED_VIA = "supervised" if os.environ.get("SUTANDO_SUPERVISED") else "bare"
_LOG_DIR = _STATE.parent / "logs"
_LOG_FILE = _LOG_DIR / "gateway-bridge.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024

# AWP P0: the persistent event channel (if enabled) — a module-level handle so
# gateway-status can report per-channel health. None until _maybe_start_event_channel.
_EVENT_CHANNEL = None

# Back-compat: instances onboarded before the AG2_REMOTE_* → REMOTE_TASK_*
# rename still export the legacy names in their .env. Honor them as DEPRECATED
# aliases for one release (remove next), with a one-line migration nudge, so the
# bridge keeps connecting under any launcher. New onboards use REMOTE_TASK_*.
_warned_legacy = set()
def _env_compat(new, old):
    v = os.environ.get(new)
    if v:
        return v
    v = os.environ.get(old)
    if v and old not in _warned_legacy:
        _warned_legacy.add(old)
        print(f"[remote-gateway-bridge] {old} is deprecated — rename to {new} in your .env",
              file=sys.stderr, flush=True)
    return v

# One-token onboarding: REMOTE_TASK_TOKEN alone is enough. The onboarding
# string may be the combined "https://<gateway>|<secret>" form (the URL travels
# inside the token — nothing service-specific lives in this repo); a bare
# secret needs REMOTE_TASK_URL alongside it.
# The combined onboarding form is "<url>|<secret>" — the URL travels inside the
# token. The separator is a literal "|", OR a "%7C"/"%7c" when the desktop connect
# flow URL-encodes it (ag2space-cinny-desktop#231): "https://<gateway>/relay%7C<secret>".
# A %7C-separated token carries no literal "|", so a naive split leaves it a bare
# secret with an empty URL and the bridge FATALs at startup — the core looks
# "connected" (device-connect completed) but never responds, the Vidhu-onboarding
# failure 2026-07-24. A literal "|" is PREFERRED over %7C/%7c when both appear:
# a raw pipe cannot legally occur inside a URL, so when one exists it IS the
# separator — keeps a URL half carrying an encoded %7C intact (#2679; same
# rule as the shared credential contract until PR3 delegates this parser).
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
# historical single-instance layout.
CHANNEL_DIR = os.environ.get("REMOTE_TASK_CHANNEL_DIR") or "ag2space"


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
    candidates = [os.environ.get("AG2_DEVICE_ENV")]
    _cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if _cfg:
        candidates.append(os.path.join(_cfg, "channels", CHANNEL_DIR, ".env"))
    for path in candidates:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        vals = {}
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            key, _, val = ln.partition("=")
            vals[key.strip()] = val.strip().strip('"').strip("'")
        # REMOTE_TASK_TOKEN is the current name; AG2_REMOTE_TOKEN the legacy alias.
        tok = vals.get("REMOTE_TASK_TOKEN") or vals.get("AG2_REMOTE_TOKEN")
        if tok:
            # Name the exact file — which .env supplied the token is load-bearing
            # for diagnosis (and for spotting a wrong-file bind).
            print(f"[remote-gateway-bridge] token not in env; loaded from {path}",
                  file=sys.stderr, flush=True)
            # Carry the file's REMOTE_TASK_URL too. A combined url|secret token
            # embeds the URL (parsed downstream), but a SPLIT layout (bare token +
            # separate REMOTE_TASK_URL) does not — and in the fallback case the env
            # is empty, so without this the URL chain has nothing and the bridge
            # fatals on "no gateway URL" in the exact scenario this fix targets.
            url = vals.get("REMOTE_TASK_URL") or vals.get("AG2_REMOTE_URL") or ""
            # Carry REMOTE_MEDIA_MARKER from the same file too. The bridge derives
            # its marker tag from os.environ at import (MEDIA_MARKER_TAG below), and
            # a bare/desktop launch reaches config ONLY through this file — it never
            # sees startup.sh's env exports, which is the one place the AG2 default
            # is otherwise set. Without this, such a launch falls back to the
            # provider-neutral default, the marker never matches the gateway's
            # `[ag2space-media: …]`, and inbound image/file URLs stay unresolved in
            # the task body — the core can't see owner-sent screenshots (owner-
            # reported 2026-08-03). This runs at import, before MEDIA_MARKER_TAG is
            # computed, so the tag picks it up. Provider-neutral: the VALUE lives in
            # the channel .env, not in this generic package; and a real env var
            # still wins (we only fill it when unset).
            _mm = vals.get("REMOTE_MEDIA_MARKER")
            if _mm and not os.environ.get("REMOTE_MEDIA_MARKER"):
                os.environ["REMOTE_MEDIA_MARKER"] = _mm
            # Return the source file path too (main #2323): it is the durable token
            # source the auth-recovery path re-reads on rejection. In the desktop-
            # spawned case this is the ONLY thing that arms recovery —
            # REMOTE_TASK_TOKEN_FILE is unset there, so without carrying `path` into
            # TOKEN_FILE the bridge keeps the historical FATAL/crash-loop behavior.
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
# The ONE auth-header dict shared with long-lived consumers (event channel,
# card poster). They must hold this dict BY REFERENCE (no copy) so a token
# rotation (_reload_rotated_token) propagates to their next request without a
# restart. _req() itself reads the TOKEN global per call and needs no dict.
_AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
HEARTBEAT_INTERVAL = 60
# When the gateway lacks /v1/tasks/<id>/ack it returns 404/405; we back off
# instead of hammering it — but only for this cooldown, then retry. A permanent
# latch would mean a broker that GAINS the endpoint (e.g. a deploy) is never
# picked up until the worker restarts; time-gating makes it self-healing.
ACK_UNSUPPORTED_COOLDOWN = int(os.environ.get("REMOTE_ACK_RETRY_COOLDOWN") or "300")
_ack_disabled_until = 0.0   # 0 = enabled; else epoch until which acks are skipped
# Auth-rejection recovery: when the gateway rejects the bearer (401/403 — the
# key was revoked or expired), the historical behavior is an immediate FATAL
# exit. Under a supervisor that blindly relaunches, that becomes a silent
# crash-loop hammering the gateway until a human notices. When
# REMOTE_TASK_TOKEN_FILE names the durable token source (a dotenv-style file
# with a REMOTE_TASK_TOKEN= line, or the raw onboarding string alone on a
# line), the bridge instead re-reads that file on rejection: a DIFFERENT token
# there (the connect/onboarding flow re-ran) is swapped in live — no restart —
# and an unchanged one holds the bridge in a slow re-check loop until rotation
# happens. Unset → exactly the pre-existing FATAL-exit behavior.
TOKEN_FILE = os.environ.get("REMOTE_TASK_TOKEN_FILE") or _TOKEN_FILE_FALLBACK or ""
AUTH_RECHECK_INTERVAL = int(os.environ.get("REMOTE_AUTH_RECHECK_INTERVAL") or "30")
_heartbeat_disabled = False
_last_heartbeat_at = 0.0

_TASK_FIELDS = ("id", "timestamp", "task", "source", "channel_id",
                # Context enrichment (AG2 broker writer side): human room/sender
                # names + reply reference. Serialized only when the gateway sends
                # them (absent for other sources); each newline-stripped by
                # _one_line so a room/display name can't forge an extra line.
                "room_name", "sender_name", "reply_to_event", "reply_to_me",
                # Room-membership context (gateway writer side, same contract):
                # a capped one-line mxid list + the true joined total.
                "room_members", "room_member_count",
                "source_message_id", "user_id", "priority", "interaction_type",
                # Platform-signed metadata pointer — serialized as a one-line
                # JSON header by a dedicated branch below (dict, not scalar).
                "platform_card")

# platform_card passes through with exactly these subkeys — a signed pointer
# {card_url, card_sha256, sig, key_id, alg} to the platform's canonical agent
# operating card. The bridge does NOT verify the signature (consumers do, per
# origin, via skills/agent-room-ops/verify_platform_card.py — fail-closed);
# it only constrains the shape so the field can't smuggle arbitrary payload.
_PLATFORM_CARD_KEYS = ("card_url", "card_sha256", "sig", "key_id", "alg")

# Interaction-plane vocabulary (interaction-planes refactor step 1). Remote
# values outside this set degrade to "message" rather than passing through.
_INTERACTION_TYPES = frozenset({
    "message", "realtime_audio", "realtime_video",
    "tool_initiated", "system_event", "self_reflective",
})

# Trust tier is a LOCAL decision (review 2026-06-13): the gateway is outside
# this machine's trust boundary, so a task's SELF-CLAIMED access_tier is
# ignored. The tier written to every task file comes from REMOTE_TASK_TIER.
#
# Default is "owner" for the personal-agent model (2026-07-08): a user runs
# their OWN gateway authenticated with their OWN owner bearer, and the broker
# OWNER-SCOPES every pull (per-agent bearer; caller-owner == target-owner), so
# this gateway can ONLY ever receive its owner's own tasks — e.g. a voice-call
# delegation from the user's own cloud agent. The trust therefore derives from
# the broker's owner-scoping, NOT from trusting the gateway process or the
# task's claim. The previous "team" default made a user's own voice
# delegations look untrusted, so a hardened core (correctly, given the signal)
# refused them.
# ESCAPE HATCH: a SHARED / multi-user gateway — one that could pull tasks NOT
# scoped to a single owner — MUST set REMOTE_TASK_TIER=team (or other).
LOCAL_TIER = (_env_compat("REMOTE_TASK_TIER", "AG2_REMOTE_TIER") or "owner").strip().lower()
if LOCAL_TIER not in ("owner", "team", "other"):
    # An INVALID value (e.g. a typo "owenr") fails CLOSED to "team" — NEVER
    # silently grant owner on a misconfiguration. Only the UNSET case defaults to
    # "owner" (the `or "owner"` above — the explicit personal-agent model).
    LOCAL_TIER = "team"


# ── per-sender tier map (owner-controlled, LOCAL) ────────────────────────────
# LOCAL_TIER above is the gateway-wide default. A SHARED room can carry messages
# from senders who are NOT the owner (e.g. a teammate invited into a project
# room). To tier those correctly WITHOUT trusting the task's self-claimed tier,
# the OWNER declares a per-sender map in a LOCAL, owner-owned file:
#   $CLAUDE_CONFIG_DIR/channels/ag2space/access.json → {"tierMap": {"@user:hs": "team"}}
# We key the lookup on the BROKER-attested `user_id` (Matrix sender the broker
# writes into the task, not a task-body self-claim), so this stays a LOCAL trust
# decision — same principle as LOCAL_TIER. Only listed senders are re-tiered;
# everyone else keeps LOCAL_TIER, so an UNLISTED sender can never escalate. A LISTED
# sender gets exactly the tier the owner mapped them to — including one ABOVE
# LOCAL_TIER, which is how a least-privilege node names its owner. See the CONTRACT
# note on _tier_for for why that cannot be driven from the wire.
# Hot: the cache keys on (st_mtime_ns, st_size, st_ino) and never serves an
# above-LOCAL_TIER grant without a fresh read, so the owner can add teammates AND
# revoke them without a restart.
def _ag2space_access_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "channels", CHANNEL_DIR, "access.json")


# Known tier vocabulary. Also an ordering (higher == more privileged); kept for
# validating a mapped value. It no longer CLAMPS — see _tier_for.
_TIER_RANK = {"other": 0, "team": 1, "owner": 2}

_TIER_MAP_CACHE = {"ident": None, "map": {}}


def _has_above_local(cached) -> bool:
    """True if the cached map grants anyone a tier ABOVE this node's LOCAL_TIER."""
    local_rank = _TIER_RANK.get(LOCAL_TIER, _TIER_RANK["owner"])
    return any(_TIER_RANK.get(v, 0) > local_rank for v in cached.values())


def _stale_safe(cached):
    """Project a STALE cached map onto the fail-closed side in BOTH directions.

    The cache is deliberately preserved across a read error so a transient
    mid-write cannot fail-OPEN a down-tiered sender back to LOCAL_TIER. That
    reasoning holds only for entries at or below LOCAL_TIER. Once the map can
    also grant a tier ABOVE LOCAL_TIER, replaying the cache verbatim keeps an
    ESCALATION alive on a file the owner may have just deleted to revoke it —
    so deleting access.json would not actually revoke anything.

    Drop the above-LOCAL_TIER entries (those senders fall back to LOCAL_TIER)
    and keep the rest. A legitimately-granted sender is briefly demoted while
    the file is unreadable and is restored on the next successful read; an
    unrevoked escalation is not left standing. Transient demotion is the safe
    direction — the module already fails closed to "team" on a bad LOCAL_TIER."""
    local_rank = _TIER_RANK.get(LOCAL_TIER, _TIER_RANK["owner"])
    return {k: v for k, v in cached.items() if _TIER_RANK.get(v, 0) <= local_rank}


def _load_tier_map():
    """Return the owner's {sender_mxid: tier} map, mtime-cached.

    Preserves the last-known-good map on a READ error (stat/open/parse failure)
    rather than clearing to {}. Clearing would silently up-tier every previously
    down-tiered sender back to LOCAL_TIER the moment access.json is mid-write,
    corrupt, or transiently unreadable — a fail-OPEN that is especially bad on a
    LOCAL_TIER=owner node (a teammate momentarily regains owner). Only a
    SUCCESSFUL read of a changed file replaces the cache; a fixed file (new mtime)
    is picked up on the next call. A genuine deletion keeps the last map until the
    owner writes an empty tierMap or the process restarts (the safe, non-surprising
    tradeoff — the map floor never drops on a transient fault)."""
    path = _ag2space_access_path()
    try:
        st = os.stat(path)
    except OSError:
        # Absent/unstattable → keep last-known-good (initially {} before any load).
        return _stale_safe(_TIER_MAP_CACHE["map"])
    # Cache identity is (mtime_ns, size, inode), NOT float mtime. A float mtime
    # collides under a same-second rewrite and can be restored outright with
    # os.utime, which would serve a REVOKED grant from cache — reproduced on
    # #2584 (rewrite to {"tierMap":{}}, restore st_mtime_ns, still resolved owner).
    ident = (st.st_mtime_ns, st.st_size, st.st_ino)
    # Belt-and-braces: never serve an ABOVE-LOCAL grant from cache without a fresh
    # read. Identity can still be forged deliberately; a revoked escalation must not
    # survive that. Costs one small read per call only while an escalation is
    # cached — discord's loader has no cache at all and re-reads every message.
    if ident == _TIER_MAP_CACHE["ident"] and not _has_above_local(_TIER_MAP_CACHE["map"]):
        # File present and UNCHANGED — this cache is current, not stale. Return it
        # verbatim: projecting here would drop a legitimate up-tier on every call.
        return _TIER_MAP_CACHE["map"]
    try:
        with open(path) as f:
            raw = (json.load(f) or {}).get("tierMap") or {}
        tm = {}
        for who, tier in raw.items():
            t = str(tier).strip().lower()
            if isinstance(who, str) and t in ("owner", "team", "other"):
                tm[who.strip()] = t
    except Exception:
        # Malformed / mid-write → keep last-known-good; don't advance mtime so a
        # later successful read of the fixed file is still picked up.
        return _stale_safe(_TIER_MAP_CACHE["map"])
    _TIER_MAP_CACHE["ident"], _TIER_MAP_CACHE["map"] = ident, tm
    return tm


def _tier_for(user_id):
    """Resolve the access_tier for a task's broker-attested sender.

    An EXPLICITLY LISTED sender gets exactly the tier the owner mapped them to —
    including a tier ABOVE this node's LOCAL_TIER. That is the point: it lets a
    SHARED gateway run a least-privilege default (REMOTE_TASK_TIER=team) and name
    the one sender who is the owner, instead of the only previously-available
    shape — a blanket `owner` default that every unlisted sender inherits.

    This mirrors the discord and slack bridges, which have always resolved
    `access_tier = tierMap[sender_id]` with no clamp. The map is LOCAL,
    owner-owned config with the same trust standing as REMOTE_TASK_TIER itself,
    and the lookup key is the BROKER-ATTESTED user_id, never a task-body
    self-claim — so the WIRE still cannot escalate anyone. Only the owner's own
    local file can, and only for a sender they named explicitly.

    Everyone else (unlisted / no user_id) gets LOCAL_TIER, unchanged: no existing
    install is silently demoted, and an unknown sender never gains privilege.

    ⚠ CONTRACT — `user_id` MUST stay broker-attested (cold-review note, #2584).
    The removed clamp used to be a backstop: even if `user_id` had become
    body-influenced, a mapped tier was still bounded by LOCAL_TIER. With the
    clamp gone, "`user_id` is the broker-written Matrix sender, never a
    task-body self-claim" is SOLELY load-bearing for the no-wire-escalation
    property. It holds today because `user_id` is a broker-writer-side entry in
    _TASK_FIELDS, serialized beside room_name/sender_name. Any future change
    that lets a task body influence `user_id` reintroduces wire-controlled
    escalation — re-add a bound here if that contract is ever weakened.
    (Deliberately a contract note, not an assert: provenance cannot be checked
    at runtime — the value is an ordinary string whichever path produced it.)"""
    uid = (user_id or "").strip()
    if uid:
        mapped = _load_tier_map().get(uid)
        if mapped in _TIER_RANK:
            return mapped
    return LOCAL_TIER


# ── inbound media fetch (owner screenshots, file uploads) ────────────────────
# A gateway can hand the task body a media MARKER instead of raw bytes:
#   [<tag>: <url> mime=<m> name=<f> size=<n> kind=<msgtype>] <caption>
# `<url>` is typically unreachable for the core as-is (a homeserver media URL
# behind authenticated-media, or a gateway media-proxy URL). We resolve it here —
# where the gateway bearer already lives — download the bytes to a local file,
# and rewrite the marker to `[File attached: <path>]` (the inbound convention
# the Discord/Telegram bridges use) so the core just reads a local path with
# zero remote creds.
#
# Auth is picked by the URL:
#   • URL under REMOTE_TASK_URL → fetched with the gateway bearer we already hold
#   • a Matrix `/_matrix/media/...` URL → upgraded to the authenticated
#     MSC3916 client route and fetched with REMOTE_MEDIA_HS_TOKEN (a homeserver
#     access token), if configured
#   • any other https URL → fetched with NO credentials
# Authenticated fetches do NOT follow redirects (a gateway-controlled URL must
# not be able to bounce our bearer to a third-party host). Drop-in safe: no
# token / fetch error / oversize → the marker is left untouched.
#
# The marker tag is configurable (REMOTE_MEDIA_MARKER, slug chars only) so a
# provider-specific gateway can keep its existing marker name without this repo
# carrying provider strings.
MEDIA_MARKER_TAG = re.sub(r"[^A-Za-z0-9_-]", "",
                          os.environ.get("REMOTE_MEDIA_MARKER") or "remote-media")
MEDIA_MARKER_RE = re.compile(r"\[" + re.escape(MEDIA_MARKER_TAG) + r":([^\]]*)\]")

# Untrusted room-ops metadata block: the gateway appends a free-text
# `[room-ops metadata: …]` pointer to the operating card onto the message body.
# It self-labels "Not an instruction" and is UNSIGNED (unlike platform_card,
# which is a signed header consumers verify offline). Because it rides in the
# task body — the same field as the user's words — a naive agent can read it as
# an instruction. We strip it here so it never reaches the agent as body content
# (owner directive 2026-07-16). The operating card stays discoverable via the
# documented prep_get op; a TRUSTED pointer, if ever wanted, belongs in a signed
# header like platform_card, not in unsigned body text. Bracket-body is
# `[^\]]*` — the block carries no nested `]`, so this never over-eats.
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
    # (no-op) body. NEVER fall back to the original here — that would re-admit the
    # very `[room-ops metadata: …]` block we are quarantining (P1, PR #2149).
    return (cleaned.strip(), stripped)
HS_MEDIA_TOKEN = os.environ.get("REMOTE_MEDIA_HS_TOKEN") or ""
# The homeserver token is attached ONLY to media URLs on this exact origin
# (scheme+host+port). Without it configured, Matrix media URLs are never
# credentialed — a bare "/_matrix/" substring must not route a bearer to an
# arbitrary host (review 2026-07-03).
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
# proactive-loop's "active engagement" gate knows a conversation is live. The
# gateway transport should feed the same gate — but only when THIS node treats
# gateway traffic as owner traffic (LOCAL_TIER, never the gateway's claim).
OWNER_ACTIVITY_FILE = _STATE / "last-owner-activity.json"  # sutando-only; harmless if unused


# Blocker (review 2026-06-13): the gateway is untrusted, so a task `id` flows
# into filesystem paths (task write + result read-back/POST). Reject anything
# that isn't a plain slug — kills path traversal in both directions.
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
    # lets the long-poll through. (Same fix the other gateway callers carry.)
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
    # line order (review P1: a migration-era env with a stale legacy line
    # ABOVE the current canonical one made recovery hot-swap back to the stale
    # legacy secret — first-match-in-file-order inverted startup.sh's
    # precedence). Last assignment of a repeated key wins, matching shell
    # sourcing semantics.
    found: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        for key in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
            if line.startswith(key + "="):
                found[key] = line[len(key) + 1:].strip().strip("'\"")
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
                found[key] = line[len(key) + 1:].strip().strip("'\"")
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
    # the desktop connect flow) splits correctly. A literal "|" split treated the
    # encoded form as a bare secret and set the bearer to the whole URL string,
    # so a valid rotation kept failing auth (regression caught on #2323 once
    # #2307's %7C onboarding parser reached main).
    url_from_token, secret = _parse_onboarding_token(raw)
    # The URL guard must cover BOTH layouts: the combined url|secret form
    # (url_from_token) AND the split form (bare secret + a separate
    # REMOTE_TASK_URL line, which _read_token_file drops). Without the split
    # fallback, a split file re-pointed by connect to a new gateway sends the
    # new bearer to the OLD running URL — the credential split this guards.
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
    if _reload_rotated_token():
        _log("auth rejected but token file already rotated — resuming with new token")
        return True
    if not TOKEN_FILE:
        return False
    _log(f"gateway auth rejected (HTTP {code}) — waiting for token rotation in "
         f"{TOKEN_FILE} (re-check every {AUTH_RECHECK_INTERVAL}s)")
    while True:
        _emit_gateway_status(False,
                             error=f"auth rejected HTTP {code} — waiting for re-connect",
                             backoff_s=AUTH_RECHECK_INTERVAL)
        time.sleep(AUTH_RECHECK_INTERVAL)
        if not _heartbeat_singleton():
            sys.exit("FATAL: lost poller singleton while waiting for token rotation")
        if _reload_rotated_token():
            _log("rotated token detected — resuming")
            return True


def _post_task_ack(tid: str) -> bool:
    """Tell the gateway a task made it safely into the local queue."""
    global _ack_disabled_until
    # Validate the WIRE id (post-conversion): a named instance's LOCAL id may
    # legitimately exceed the 64-char wire bound (review P1 #1) — refusing on
    # the local form stranded queued results while the gateway waited forever.
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
            # later, so a broker that deploys /ack afterward is picked up without a
            # restart. But the DEPLOYED broker returns a PER-TASK
            # 404 {"error":"not leased to you"} when THIS task's lease expired /
            # was re-served / isn't ours — routine under churn. That must NOT
            # disable acking for every OTHER task (one stale lease would blind the
            # whole host's `received` state), so treat it as a single-task negative
            # ack: skip this one, leave global acking enabled. (Per qingyun-001,
            # broker-half author — the deployed 404 is per-task, not "no route".)
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
                             "core-status"],
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
        }
        # AWP P0 per-channel health: the task connection is `connected` above; the
        # additive event channel (if running) reports its own status, so a
        # supervisor never shows the agent healthy while the event stream is dead.
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
        # a per-PID temp is collision-proof for the cost of one getpid().
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
    # gateway bearer, and a foreign host serving a `/_matrix/` path must not
    # receive the homeserver bearer (review 2026-07-03).
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
        # 2026-07-03).
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
# PRESENCE gate consults this; task authority (_tier_for) is deliberately left
# untouched, so peer-to-peer delegation keeps its access_tier (a peer agent still
# resolves to owner tier on a tierMap-less node and can still act — the two
# consumers of _tier_for are decoupled here on purpose).
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
    """Record that the owner was active on this transport right now — but only
    when THIS node resolves the SENDER to owner tier. Gated on the sender's
    resolved tier, NOT the gateway-wide LOCAL_TIER: in a shared room a
    down-tiered teammate (tierMap[...] = "team"/"other") must not overwrite
    `state/last-owner-activity.json`, or their message would poison owner-presence
    routing (the proactive-loop's "owner active N min ago" signal + the core-
    supervisor escalation target). `sender_tier` is passed in by `_write_task` so
    the task tier and this gate share a SINGLE resolution (no divergence, no
    double tierMap read); a direct caller can omit it and we resolve here. For an
    Since the map may now grant a tier ABOVE LOCAL_TIER, the mirror case also
    holds: a sender explicitly mapped to owner on a least-privilege node DOES
    register owner presence — that is the point of naming them owner. Tests 23/24
    pin both directions, because a refactor that regated this on LOCAL_TIER would
    silently stop recording the real owner's activity. For an
    unlisted sender `_tier_for` returns LOCAL_TIER, so the single-owner case is
    unchanged. Never trusts the gateway's own claim (it is outside the trust
    boundary) — only the broker-attested user_id keyed against the owner's LOCAL
    tierMap. Atomic write via per-PID tmp + os.replace (this file has four
    concurrent writers; #2222); same schema (`ts`, `channel`, `summary`)
    as discord-bridge.write_owner_activity so the proactive-loop reader is
    transport-agnostic. Best-effort — never blocks task intake."""
    if sender_tier is None:
        # user_id is broker-attested here — see the CONTRACT note in _tier_for.
        sender_tier = _tier_for(task.get("user_id"))
    if sender_tier != "owner":
        return
    # A peer FLEET agent resolves to owner tier on a tierMap-less node (LOCAL_TIER
    # fallthrough), but it is never the HUMAN owner — recording its post as
    # owner-presence poisons the proactive-loop's engagement signal and the
    # core-supervisor escalation target. Gate PRESENCE ONLY here; the task's
    # access_tier (the other _tier_for consumer) stays owner so peer delegation
    # is unaffected. Broker-attested user_id only — the /v1/agents directory is
    # the authoritative peer set (the human owner is not in it).
    _uid = (task.get("user_id") or "").strip()
    if _uid and _uid in _fleet_agent_ids():
        return
    try:
        OWNER_ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Strip a bracket prefix a gateway may add (e.g. "[Provider @user] body").
        body = (task.get("task") or "").lstrip()
        if body.startswith("[") and "]" in body:
            body = body[body.index("]") + 1:].lstrip()
        # #2267 parity: the presence summary is persisted state too — a pasted
        # token must not survive in last-owner-activity.json either.
        body = filter_chat_secrets(body).text
        payload = {
            "ts": int(time.time()),
            "channel": task.get("source") or PROVIDER,
            "summary": body[:80],
        }
        # Propagate the routable room id so the core-supervisor relay can escalate
        # BACK into the AG2Space room the owner was last active in (resolve_active_
        # target requires both `channel` and `channel_id`; without this it degrades
        # to macOS-only for the gateway surface). Only when present — keeps the
        # discord-bridge schema compatible for non-message activity.
        _cid = str(task.get("channel_id") or "").strip()
        if _cid:
            payload["channel_id"] = _cid
        # Per-PID staging: last-owner-activity.json is written by FOUR processes
        # (this sparrow bridge + slack/discord/telegram). A shared ".json.tmp"
        # name lets two concurrent writers truncate and interleave the same temp
        # file, so the rename can publish torn JSON to the proactive loop's
        # presence check. A per-PID temp is never shared; os.replace is an atomic
        # overwrite — last writer wins, cleanly. (sonichi/sutando#2222)
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
    # id; `_broker_tid` restores the wire id at the ack/result POST boundary.
    tid = _local_tid(broker_tid)
    task = {**task, "id": tid}
    dest = TASKS_DIR / f"{tid}.txt"
    # Idempotent: don't re-write a task already queued, claimed, or archived.
    if dest.exists() or any(TASKS_DIR.glob(f"{tid}.claimed-*")):
        return tid
    # Relay redelivery of already-handled work: on reconnect the gateway replays
    # its unacked pool, including tasks this node long since processed (the
    # 2026-06-30 and 2026-07-01 500-task floods). If the core already archived
    # the task file, or the result was already delivered and archived, don't
    # re-queue — drop a [no-send] result instead so the normal result drain
    # re-acks it upstream and clears it from inflight.
    _task_archive = TASKS_DIR / "archive"
    task_archived = (
        # legacy flat layout: tasks/archive/<taskId>.txt
        (_task_archive / f"{tid}.txt").exists()
        # active month-partitioned layout: tasks/archive/YYYY-MM/<taskId>.txt
        # (see src/task-bridge.ts). Glob one level of month subdirs for this
        # exact task id — cheap (one stat per month dir, not a full tree walk).
        or next(_task_archive.glob(f"*/{tid}.txt"), None) is not None
    )
    if (task_archived
            or (ARCHIVE_RESULTS_DIR / f"{tid}.txt").exists()
            or next(ARCHIVE_RESULTS_DIR.glob(f"{tid}-[0-9]*.txt"), None)):
        rfile = RESULTS_DIR / f"{tid}.txt"
        if not rfile.exists():
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            rfile.write_text("[no-send] gateway redelivery of already-handled task\n")
        _log(f"dedup: {tid} already handled — not re-queued")
        return tid
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    _secret_types: tuple = ()
    for f in _TASK_FIELDS:
        if f == "source":
            lines.append(f"source: {_one_line(task.get('source') or PROVIDER)}")
        elif f == "interaction_type":
            # Pass through when the gateway sends it; default to "message" —
            # all current gateway traffic is Matrix room messages. Whitelisted:
            # the gateway is outside the trust boundary, so an unknown value
            # degrades to the default instead of landing verbatim in the file.
            it = str(task.get("interaction_type") or "")
            if it not in _INTERACTION_TYPES:
                it = "message"
            lines.append(f"interaction_type: {it}")
        elif f == "task" and task.get("task") not in (None, ""):
            # Quarantine the untrusted `[room-ops metadata: …]` block BEFORE it
            # reaches the agent as body content (owner directive 2026-07-16) —
            # see _strip_room_ops_meta. Runs first so the stripped body is what
            # media-resolution and the header write both see.
            _raw_task, _stripped_meta = _strip_room_ops_meta(str(task["task"]))
            if _stripped_meta:
                _log(f"stripped untrusted room-ops metadata from {tid} body")
            # Resolve an inbound media marker to a local file the core can read.
            _media_refs: list = []
            _fetched = _maybe_fetch_media(_raw_task, _media_refs)
            # Redact pasted secrets BEFORE the body is persisted (#2267 parity
            # with the discord/slack/telegram bridges): a token pasted into a
            # room message must never land on disk. Runs AFTER media
            # resolution so a signed media-proxy URL is consumed intact and
            # only the resolved text is filtered.
            _filtered = filter_chat_secrets(_fetched)
            if _filtered.secret_types:
                _secret_types = tuple(_filtered.secret_types)
                _log(f"redacted pasted secret(s) in {tid} body: "
                     f"{', '.join(sorted(_secret_types))}")
            lines.append(f"task: {_one_line(_filtered.text)}")
            # interaction-model 4D, step 1.5: if a media marker was fetched,
            # stamp structured attachments[]/content_modalities/media_form
            # alongside the legacy [File attached:] body line (dual-write) via the
            # shared local_task_protocol helper — same shape the 3 message bridges
            # emit. has_text = caption present beyond the provider prefix + marker.
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
            # reach the file). json.dumps escapes newlines, so the value can't
            # forge a header line even without _one_line.
            pc = task.get("platform_card")
            if isinstance(pc, dict) and all(k in pc for k in _PLATFORM_CARD_KEYS):
                card = {k: str(pc[k]) for k in _PLATFORM_CARD_KEYS}
                lines.append(f"platform_card: {json.dumps(card, separators=(',', ':'))}")
        elif f in task and task[f] not in (None, ""):
            lines.append(f"{f}: {_one_line(task[f])}")
    # (This used to note that a gateway-written trust signal was dropped for
    # lack of end-to-end support. platform_card above is that signal, done
    # properly: KNOWN_HEADER_KEYS vocabulary + guard defang + a verifying
    # consumer in skills/agent-room-ops/verify_platform_card.py.)
    # access_tier is a LOCAL decision and written LAST so it wins even under a
    # last-occurrence parser; every other field is newline-stripped so none can
    # forge an earlier one either. Resolved per broker-attested sender via the
    # owner's tierMap (LOCAL file), falling back to LOCAL_TIER for unlisted
    # senders — so a named teammate can be down-tiered without trusting the task.
    # Resolve ONCE and reuse for both the task tier AND the owner-activity gate
    # below, so the two decisions can never diverge (a single source of truth,
    # no double read of the tierMap).
    # user_id is broker-attested here — see the CONTRACT note in _tier_for.
    sender_tier = _tier_for(task.get("user_id"))
    lines.append(f"access_tier: {sender_tier}")
    # #2267 parity, second half: the other bridges append the in-band security
    # notice so the core neither reproduces nor re-requests the redacted value.
    # Appended AFTER access_tier: the notice is bridge-generated fixed text with
    # no header-shaped lines, so the access-tier-wins-last invariant holds.
    if _secret_types:
        lines.append(secret_handling_instruction("AG2Space", _secret_types).strip("\n"))
    # ===SKILL INSTRUCTIONS=== (owner-tier only): prose/numbered lines only, no
    # header-shaped lines, so appending after access_tier keeps it the last one.
    if sender_tier == "owner":
        _chan = _one_line(task.get("channel_id") or "")
        # shlex.quote: an unescaped quote in _chan must not close the shell
        # string early and turn the remainder into executable shell syntax.
        _chan_q = shlex.quote(_chan)
        _step = 1
        _skill = ["", "===SKILL INSTRUCTIONS (follow before any other action)==="]
        if _chan:
            _skill.append(
                f"{_step}. CONTEXT-FIRST (unconditional): before interpreting this "
                f"message, reconstruct the room thread — `python3 "
                f"skills/agent-room-ops/room_ops.py read {_chan_q} --limit 30` (if it "
                f"reports no gateway configured, load the channel env first: `set -a; . "
                f"\"$CLAUDE_CONFIG_DIR/channels/ag2space/.env\"; set +a`) — and read it "
                "back (everyone's messages including your own prior replies) until this "
                "message stands on its own, then answer from the reconstructed thread, "
                "NOT from memory. Do this every time; do NOT skip it because the message "
                "looks self-contained or you feel you already understand it — felt "
                "confidence is exactly the signal that fails. The only exception is a "
                'pure greeting or acknowledgement with no referent (e.g. "hi", "thanks").')
            _step += 1
            _skill.append(
                f"{_step}. NOTIFY FIRST (if task takes >60s): python3 "
                f"skills/task-progress/scripts/notify.py --source ag2space "
                f"--channel-id {_chan_q} --message \"On it — back in a moment.\"")
            _step += 1
        _skill.append(f"{_step}. Process and write the result to results/{tid}.txt")
        lines.extend(_skill)
    tmp = dest.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.rename(dest)  # atomic publish so the watcher never sees a partial file
    _log(f"queued {tid}")
    # #2274 parity: one task_processed per NEWLY queued task (idempotent early
    # returns never reach here), bucketed to this gateway's own "remote" surface
    # when the source label isn't an allowlisted bucket so activity isn't lost.
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
    # find_task_file resolves the ACTUAL filename: bare `<tid>.txt` or the
    # claimed variant `<tid>.claimed-core-N.txt` the core renames to while
    # processing (review catch: probing only the bare name left claimed files
    # behind, and health-check counts every top-level tasks/*.txt). Archived
    # under the bare name — the shape _write_task's redelivery dedup checks.
    tfile = find_task_file(TASKS_DIR, tid)
    if tfile is not None:
        archive_dir = TASKS_DIR / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            tfile.rename(archive_dir / f"{tid}.txt")
        except OSError:
            pass  # best-effort; core may have archived it concurrently


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
    """Atomically persist the in-flight set. Best-effort (never blocks the loop)."""
    try:
        INFLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Per-PID staging (sonichi/sutando#2222 follow-up): collision-proof if a
        # second sparrow instance ever runs. os.replace is atomic overwrite.
        tmp = INFLIGHT_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(sorted(inflight)))
        os.replace(tmp, INFLIGHT_FILE)
    except Exception as e:  # noqa: BLE001
        _log(f"inflight persist failed ({e}) — continuing")


# (tid, path) pairs already uploaded this process — result-POST retry guard.
_uploaded_attachments: set[tuple[str, str]] = set()


def _dedup_holder_answered(holder_id: str | None) -> bool:
    """Did the dedup holder actually deliver? Unknown holder counts as no."""
    if not holder_id:
        return False
    archived = find_archived_result(RESULTS_DIR, holder_id.strip())
    text = None
    if archived is not None:
        try:
            text = archived.read_text()
        except OSError:
            text = None
    return dedup_holder_delivered(text)


def _post_ready_results(inflight: set[str]) -> None:
    """For each in-flight task, if its result file exists, POST it + archive."""
    changed = False
    for tid in list(inflight):
        if not _valid_local_tid(tid):  # defense-in-depth: never read an unsafe path (local ids may carry the instance encoding)
            inflight.discard(tid); changed = True
            continue
        rfile = RESULTS_DIR / f"{tid}.txt"
        body = read_ready_result(rfile)
        if body is None:
            continue
        # Route marker decisions through the unified parser (#873) like the
        # other bridges — no hand-rolled startswith checks.
        parsed = parse_markers(body)
        skip = next((a for a in parsed.actions if a.kind == "skip"), None)
        if skip and skip.value == "deduped" and not _dedup_holder_answered(skip.extra):
            # The holder never delivered, so archiving here would strand the ask
            # and every retry carrying the same marker.
            _log(f"refusing dedup for {tid}: holder {skip.extra} delivered nothing")
            continue
        if skip:
            # [no-send]/[REPLIED]/[deduped:] mean "no user-facing reply":
            # archive without POSTing (match the other bridges' semantics).
            _archive_result(rfile, tid)
            inflight.discard(tid)
            _forget_task_room(tid)
            changed = True
            _log(f"archived {tid} (marker {skip.value}, not sent)")
            continue
        out_body = parsed.body
        redirect = next((a for a in parsed.actions if a.kind == "redirect"), None)
        if redirect:
            # Cross-room redirect is handled GATEWAY-side for this transport —
            # re-stitch the marker the parser stripped so the server still
            # sees it as the first line.
            out_body = f"[channel: {redirect.value}]\n{out_body}"
        attaches = [a.value for a in parsed.actions if a.kind == "attach"]
        if attaches:
            room = _load_task_rooms().get(tid, "")
            sent = 0
            for fp in attaches:
                # Uploads happen before the result POST (so failures can be
                # annotated in-band); if that POST then fails and this loop
                # retries, don't re-upload the same file into the room.
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
        try:
            _req("POST", "/v1/results", {"id": _broker_tid(tid), "body": out_body})
        except urllib.error.HTTPError as e:
            _log(f"result POST failed for {tid}: HTTP {e.code} — will retry")
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            _log(f"result POST network error for {tid}: {e} — will retry")
            continue
        _archive_result(rfile, tid)
        inflight.discard(tid)
        _forget_task_room(tid)
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
            and not (TASKS_DIR / f"{tid}.txt").exists()
            and not any(TASKS_DIR.glob(f"{tid}.claimed-*"))
            and not (RESULTS_DIR / f"{tid}.txt").exists()}
    confirmed = gone & suspects
    if confirmed:
        for tid in sorted(confirmed):
            inflight.discard(tid)
            _log(f"dropped abandoned in-flight id {tid} (no task/result file — completed elsewhere)")
        _save_inflight(inflight)
    return gone - confirmed


# ── MC1 per-workspace singleton (dual-poller guard) ─────────────────────────
# Exactly one gateway-bridge may poll a given workspace's relay bearer. A second
# one — an orphaned bridge from a prior install (ppid 1, outlived its parent), or
# a simultaneous respawn — would double-deliver every task. Acquire a per-
# (workspace, role) lock before polling; if a LIVE bridge already holds it, exit
# without polling. The lock is held + heartbeated so a crashed/stale holder is
# reaped (freshness like state/cores/<host>.alive). FAIL-OPEN by design: any
# lock-layer error → poll anyway (a lock bug must never silence task delivery;
# the only risk of a dropped guard is the pre-existing dual-poller). Kill-switch:
# SUTANDO_BRIDGE_LOCK=0 lets the owner disable it in prod without a redeploy.
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
        # and sweep-post question cards for actions the hook created. Both are
        # additive — unset env leaves the plain taskify path exactly as before.
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


def main() -> None:
    if not TOKEN:
        sys.exit("FATAL: set REMOTE_TASK_TOKEN (the onboarding string, or a bare secret with REMOTE_TASK_URL).")
    if not URL:
        # A token that starts with a URL scheme but yielded no URL means the
        # url|secret separator was swallowed (e.g. a %7C survived decoding, or a
        # new encoding we don't handle) — say so, instead of the misleading
        # "set REMOTE_TASK_TOKEN" when the token is present but malformed.
        _hint = (" — the token carries a gateway URL but the url|secret separator "
                 "looks missing/corrupted" if TOKEN[:4].lower() == "http" else "")
        sys.exit("FATAL: no gateway URL — set REMOTE_TASK_URL, or use the combined "
                 f"'https://<gateway>|<secret>' onboarding token{_hint}.")
    if not _acquire_singleton():
        return  # a live bridge already polls this workspace — exit cleanly (no dual-poll)
    inflight: set[str] = _load_inflight()
    abandoned_suspects: set[str] = set()
    _log(f"starting — gateway={URL} provider={PROVIDER} tasks={TASKS_DIR} "
         f"(restored {len(inflight)} in-flight)")
    # Always name where the diagnostics live: after an incident this line is the
    # trailhead (a bare-launched bridge under default dirs writes status to
    # ~/.ag2-sparrow/state/, where nobody thinks to look).
    _log(f"launched_via={_LAUNCHED_VIA} status={GATEWAY_STATUS_FILE}")
    if _LAUNCHED_VIA == "bare":
        _log(f"running unsupervised — output also logged to {_LOG_FILE}; "
             f"prefer launching through startup.sh for full diagnostics")
    backoff = 1
    _emit_gateway_status(False, error="starting — not yet connected")
    _maybe_start_event_channel()  # additive/opt-in/isolated — never blocks the task loop
    while True:
        try:
            if not _heartbeat_singleton():
                # Lost the poller lock (reaped after being deemed stale). Stop
                # polling immediately so we don't dual-poll the relay bearer with
                # the process that took over. atexit release is a no-op (not ours).
                _log("singleton: lost workspace poller lock (reaped after stale takeover) "
                     "— exiting to avoid dual-poll")
                return
            _post_heartbeat(inflight)
            resp = _req("GET", f"/v1/tasks?wait={POLL_WAIT}", timeout=POLL_WAIT + 10)
            added = False
            pending_ack = []
            for task in resp.get("tasks", []):
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
            _post_ready_results(inflight)
            abandoned_suspects = _reconcile_abandoned(inflight, abandoned_suspects)
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
