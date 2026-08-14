#!/usr/bin/env python3
"""Anonymous, opt-out product telemetry for Sutando (PostHog).

Sutando is open source; the desktop app runs the same core. Instrumenting the
core (not just the app) lets maintainers see how many people run Sutando and
which features they use — WITHOUT ever collecting who they are or what they do.

What is sent
------------
Only bucketed / categorical PRODUCT events (e.g. ``core_started``,
``feature_used {feature: ...}``). Never task content, message text, prompts,
logs, file paths, or any PII. See ``TELEMETRY.md`` for the exact list.

How it is sent
--------------
A best-effort JSON POST to PostHog's ``/capture`` endpoint over the standard
library (no third-party dependency), fired in a daemon thread so it can never
block or crash the app. Any error is swallowed.

Opting out (checked live on every call — never cached)
------------------------------------------------------
Set ANY of the following and all telemetry becomes a silent no-op:

* ``DO_NOT_TRACK=1``     — the cross-project standard (Astro, Bun, Prisma, …)
* ``SUTANDO_TELEMETRY=0``
* a file named ``telemetry-disabled`` in EITHER the durable OS data dir (see
  Identity below — survives workspace churn / app updates) OR the legacy
  ``<workspace>/state/`` dir. A marker in either location opts you out.

Identity
--------
A random per-install UUID persisted at the durable, update-surviving OS data
dir (macOS: ``~/Library/Application Support/Sutando/telemetry-id``; other:
``${XDG_DATA_HOME:-~/.local/share}/sutando/telemetry-id``), with a back-compat
copy at the legacy ``<workspace>/state/telemetry-id``. It
is not a device fingerprint and is not tied to any account or email. Events set
``$ip=""`` and ``$geoip_disable`` so PostHog does not store or geolocate the
request IP; the network-level source IP is inherent to any HTTPS request (as
with any website the machine contacts) and is not used for attribution.

Config
------
* ``POSTHOG_API_KEY`` — the PostHog *project* key. ``phc_...`` keys are public
  and write-only, so one may be embedded in ``_EMBEDDED_KEY`` below for
  distribution. Absent a key, telemetry is a no-op.
* ``POSTHOG_HOST``    — defaults to the US cloud (``https://us.i.posthog.com``).
* ``SUTANDO_DEBUG_TELEMETRY=1`` — print every event to stderr before sending.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
import uuid
from pathlib import Path

# ``phc_...`` PostHog project keys are PUBLIC and write-only — safe to embed in
# open-source source/binaries. Paste the project key here to enable telemetry
# for distributed builds; forks/self-hosters override via POSTHOG_API_KEY.
_EMBEDDED_KEY = "phc_kt7Syd7YpYJxL2i3467C3D2Q4TAQLxJre9aUuxht7wBj"  # pragma: allowlist secret — public write-only PostHog project key

_KEY = (os.environ.get("POSTHOG_API_KEY") or _EMBEDDED_KEY).strip()
_HOST = (os.environ.get("POSTHOG_HOST") or "https://us.i.posthog.com").rstrip("/")

_TRUTHY = {"1", "true", "yes", "on"}


def _state_dir() -> Path:
    """`<workspace>/state`. An explicit ``SUTANDO_STATE_DIR`` wins; otherwise
    resolved via the M0 helper, with a last-resort default."""
    override = os.environ.get("SUTANDO_STATE_DIR")
    if override:
        return Path(override)
    try:  # pragma: no cover — resolver glue, exercised in integration not unit
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from workspace_default import resolve_workspace  # noqa: E402

        return Path(resolve_workspace()) / "state"
    except Exception:  # pragma: no cover
        return Path.home() / ".sutando" / "repo" / "workspace" / "state"


def _durable_id_path() -> Path:
    """Launcher-independent, update-surviving path for the per-install id.

    The id MUST outlive: app auto-updates (bundle replaced), workspace churn,
    and repo re-clones — otherwise every boot mints a fresh id and PostHog
    counts each boot as a brand-new user (DAU == new-installs; the ~20-40x
    inflation observed 2026-07-16). ``<workspace>/state`` did not survive on
    desktop installs, so the id lives in the OS user-data dir instead:

      - macOS:  ~/Library/Application Support/Sutando/telemetry-id
      - other:  ${XDG_DATA_HOME:-~/.local/share}/sutando/telemetry-id

    ``SUTANDO_TELEMETRY_ID_FILE`` overrides the full path (tests / explicit
    control).
    """
    override = os.environ.get("SUTANDO_TELEMETRY_ID_FILE")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Sutando"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = (Path(xdg) if xdg else home / ".local" / "share") / "sutando"
    return base / "telemetry-id"


def opted_out() -> bool:
    """True if the user has opted out via env var or the disable file.

    Checked on every ``capture`` call (never cached) so toggling it takes
    effect immediately — the bug class that has bitten other OSS projects
    whose opt-out flag was read once at import.
    """
    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in _TRUTHY:
        return True
    if os.environ.get("SUTANDO_TELEMETRY", "").strip().lower() in {"0", "false", "no", "off"}:
        return True
    try:
        # Honor the opt-out marker in EITHER the durable OS data dir (survives
        # workspace churn / app updates — same reason the install id moved
        # there, #2147) OR the legacy <workspace>/state dir. A marker in either
        # location means opted out (fail toward privacy). Without the durable
        # check, a desktop user's opt-out was lost on workspace churn and
        # telemetry silently re-enabled — the same bandaid the id fix generalizes.
        if (_durable_id_path().parent / "telemetry-disabled").exists() \
                or (_state_dir() / "telemetry-disabled").exists():
            return True
    except Exception:  # pragma: no cover — defensive; never let a FS error force opt-in
        pass
    return False


def _read_id(path: Path) -> str:
    """Return a non-empty id stored at ``path``, else ``""``."""
    try:
        if path.exists():
            got = path.read_text().strip()
            if got:
                return got
    except Exception:  # pragma: no cover — defensive FS read
        pass
    return ""


def _distinct_id() -> str:
    """Stable random per-install id (not a fingerprint, not PII).

    Persisted at the durable, update-surviving location (:func:`_durable_id_path`)
    so a returning install keeps ONE id across boots/updates — the fix for the
    2026-07-16 inflation where the id lived under ``<workspace>/state`` and did
    not survive desktop relaunch/update, so every boot looked like a new user.

    Resolution order:
      1. durable path — the source of truth once written;
      2. migrate: if durable is empty but a legacy ``<workspace>/state/telemetry-id``
         exists, adopt that id (preserve installs whose id already persisted —
         no one-time reset for them) and copy it to the durable path;
      3. otherwise mint a new id and write it to the durable path.
    A best-effort copy is also left at the legacy path for back-compat readers.
    Any FS failure falls back to the legacy behavior, then to ``"anonymous"``.
    """
    durable = _durable_id_path()
    got = _read_id(durable)
    if got:
        return got
    # Migrate an existing id from the legacy workspace location if present.
    legacy = None
    try:
        legacy = _state_dir() / "telemetry-id"
    except Exception:  # pragma: no cover — resolver glue
        legacy = None
    ident = _read_id(legacy) if legacy is not None else ""
    if not ident:
        ident = uuid.uuid4().hex
    # Write to the durable path (source of truth going forward).
    try:
        durable.parent.mkdir(parents=True, exist_ok=True)
        durable.write_text(ident)
    except Exception:
        # Durable location unwritable → fall back to legacy behavior so the id
        # still persists as well as it can on this platform.
        try:
            if legacy is not None:
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_text(ident)
        except Exception:  # pragma: no cover — best-effort; fall back to constant
            return "anonymous"
        return ident
    # Best-effort back-compat copy at the legacy path (readers that still look
    # there keep working); never fatal.
    try:
        if legacy is not None and not legacy.exists():
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(ident)
    except Exception:  # pragma: no cover — non-fatal
        pass
    return ident


def _install_surface() -> str:
    """Which Sutando surface this install runs: ``"desktop"`` or ``"oss"``.

    Sutando is open source; the desktop app (Sutando.app) runs the same core.
    This distinguishes the two so metrics can be broken down by surface.

    Resolution:
      1. ``$SUTANDO_SURFACE`` (``desktop``/``oss``) — explicit override, wins.
      2. Otherwise probe for a running ``Sutando`` menu-bar process (the same
         signal health-check uses to detect the app). Present → desktop; a
         plain OSS checkout has no app → oss.

    Categorical only; carries no PII. Fail-safe: any error → ``"oss"`` (the
    conservative default — never over-reports desktop).
    """
    env = os.environ.get("SUTANDO_SURFACE", "").strip().lower()
    if env in ("desktop", "oss"):
        return env
    try:
        r = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Sutando"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return "desktop"
    except Exception:
        pass
    return "oss"


def enabled() -> bool:
    """Telemetry fires only when a key is configured AND not opted out."""
    return bool(_KEY) and not opted_out()


def _post(payload: dict, timeout: float = 5) -> None:  # pragma: no cover — real network I/O; mocked in tests
    try:
        req = urllib.request.Request(
            f"{_HOST}/capture/",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass  # best-effort: telemetry must never affect the app


def capture(event: str, properties: dict | None = None, *, flush: bool = False) -> None:
    """Record one anonymous product event. No-op if opted out or no key.

    ``properties`` must be bucketed/categorical only — never task content,
    message text, paths, or PII. The caller owns that discipline; this module
    does not inspect payloads beyond attaching the anonymous distinct id.

    ``flush=True`` uses a bounded synchronous send for short-lived commands
    that are about to exit. Long-running services should keep the default
    daemon-thread path so telemetry never delays their work.
    """
    if os.environ.get("SUTANDO_DEBUG_TELEMETRY", "").strip().lower() in _TRUTHY:
        sys.stderr.write(
            f"[telemetry] {event} {properties or {}} "
            f"(enabled={enabled()})\n"
        )
    if not enabled():
        return
    _dispatch(event, properties, flush=flush)


# Coarse inbound surfaces `task_processed` is allowed to tag. The value can
# originate from a task file's ``source:`` header, which on the local web/API
# path is caller-supplied — so it is validated against this fixed allowlist
# before it reaches PostHog. Anything unrecognized collapses to ``unknown``:
# telemetry stays coarse and bounded-cardinality, and a caller cannot leak an
# accidentally-supplied identifier/secret or inflate the property space through
# the ``source:`` field (CR #2274, qingyun-wu). Keep in sync with the surfaces
# that writers actually emit (`src/task_priority.py:default_priority_for_source`).
_KNOWN_SOURCES = frozenset({
    "voice", "phone", "chat", "api", "context-drop",
    "discord", "slack", "telegram", "whatsapp",
    "twilio_voice", "twilio_sms", "twilio_voicemail",
    "cron", "health-check", "sync-memory", "sync-workspace",
    "github", "web", "push", "remote",
    # Gateway (AG2 Space) surfaces — emitted by ag2_sparrow's _write_task:
    # "ag2space" for direct room messages, "events-promotion" for taskify
    # promotions, "remote" (above) as the REMOTE_TASK_PROVIDER default.
    "ag2space", "events-promotion",
})


def _coarse_source(source: str) -> str:
    """Collapse an inbound ``source:`` value to a known coarse bucket, or
    ``unknown``. Guards the caller-supplied web/API path against unbounded
    cardinality and accidental identifier/secret leakage (CR #2274)."""
    s = (source or "").strip().lower()
    return s if s in _KNOWN_SOURCES else "unknown"


def bucket_source(source: str, default: str = "unknown") -> str:
    """Resolve a ``source:`` to a known bucket, falling back to ``default``
    instead of the generic ``unknown``. A surface with a known home passes its
    own bucket — e.g. the gateway is the ``remote`` surface, so a gateway task
    whose per-task/per-deployment label isn't allowlisted counts as ``remote``,
    not lost to ``unknown``. ``default`` is itself validated, so it can't smuggle
    cardinality either."""
    coarse = _coarse_source(source)
    return coarse if coarse != "unknown" else _coarse_source(default)


def task_processed(source: str, *, flush: bool = False) -> None:
    """One anonymous event per task the core accepts, tagged only with the
    inbound surface (``discord`` / ``slack`` / ``telegram`` / ``voice`` /
    ``chat`` / ``phone`` / …).

    This is the activation signal that ``core_started`` alone can't give:
    whether an install does anything after launching. It carries ONLY the
    coarse source bucket — never the task text, ids, user, or channel. The
    bucket is validated against a fixed allowlist (`_coarse_source`) so a
    caller-supplied ``source:`` header cannot smuggle high-cardinality data or
    a secret into the property.

    ``flush=True`` is for short-lived callers (the CLI entrypoint below, spawned
    by the TypeScript task-delegation/phone paths) that exit immediately: the
    default daemon-thread sender would be killed before the request completes.
    Long-running services (the Python bridges) keep the default async path.
    """
    capture("task_processed", {"source": _coarse_source(source)}, flush=flush)


def feature_used(feature: str, *, flush: bool = False) -> None:
    """One anonymous event when a named product feature runs, tagged only with
    the feature's short categorical name (e.g. ``morning_briefing``). Never any
    task content, arguments, or PII.
    """
    capture("feature_used", {"feature": str(feature)}, flush=flush)


def _dispatch(event: str, properties: dict | None, *, flush: bool = False) -> None:
    props = {
        "$ip": "",
        "$geoip_disable": True,
        **(properties or {}),
    }
    # Surface (desktop vs OSS) on EVERY event: as an event property (filter /
    # break down any metric by surface) AND a person property ($set) so the
    # anonymous install is bucketed into an OSS-vs-desktop cohort. Set after the
    # caller spread + merged into any existing $set so it's always present.
    surface = _install_surface()
    props["surface"] = surface
    props["$set"] = {**props.get("$set", {}), "surface": surface}
    payload = {
        "api_key": _KEY,
        "event": event,
        "distinct_id": _distinct_id(),
        # A PostHog person is created/updated for the random per-install UUID so
        # installs show up as active users — the "person" carries no PII, it is
        # just the anonymous install id. $ip="" + $geoip_disable still stop
        # PostHog from storing or geolocating the request IP (that address is
        # inherent to any HTTPS request; the vendor is told not to keep it).
        "properties": props,
    }
    if flush:
        # One-shot feature scripts exit immediately after this call. A daemon
        # sender is terminated with the interpreter, so give the request a
        # short bounded window to complete before returning.
        _post(payload, timeout=1)
    else:
        threading.Thread(target=_post, args=(payload,), daemon=True).start()


# ── CLI entrypoint ─────────────────────────────────────────────────────────
# Lets non-Python task creators (the TypeScript task-delegation and phone
# paths) emit an event without a duplicate emitter in TS. Fire-and-forget from
# the caller: `python3 src/telemetry.py task_processed <source>`. Always uses
# the flush path — this process exits the instant it returns, so a daemon-thread
# send would be killed mid-flight. No-op (exit 0) when telemetry is opted out /
# unconfigured, exactly like the in-process calls. Never prints task content.
def _cli_main(argv: list[str]) -> int:
    """Dispatch a one-shot CLI emit. Returns the process exit code. Kept a plain
    function (not inlined under ``__main__``) so every branch is unit-testable
    in-process — subprocess-only code escapes the coverage gate."""
    if len(argv) == 2 and argv[0] == "task_processed":
        task_processed(argv[1], flush=True)
        return 0
    if len(argv) == 2 and argv[0] == "feature_used":
        feature_used(argv[1], flush=True)
        return 0
    sys.stderr.write(
        "usage: python3 src/telemetry.py {task_processed|feature_used} <value>\n"
    )
    return 2


if __name__ == "__main__":  # pragma: no cover — thin subprocess shim; _cli_main is tested
    sys.exit(_cli_main(sys.argv[1:]))
