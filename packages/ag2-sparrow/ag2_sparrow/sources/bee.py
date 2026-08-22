"""sources/bee.py — the Bee AI wearable as a watcher SOURCE.

A source contributes only its SUBSCRIBE specifics (stream location, auth)
and a NORMALIZE fn (provider payload -> the relay task shape); the shared
runner owns SSE parsing, cursor, sinks, reconnect, and the CLI. Adding
integration N = one module like this + a registry entry."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SOURCE = "bee"

# Config vocabulary the runner resolves (CLI > env > DEFAULTS) for this source.
# The same keys are declared in skills/bee-actions/manifest.json for discovery.
CONFIG_KEYS = (
    "BEE_PROXY_URL", "BEE_EVENTS_PATH", "BEE_EVENT_TYPES",
    "BEE_BROKER_URL", "BEE_BROKER_TOKEN", "BEE_AGENT_ID",
    "BEE_SINK", "BEE_API_BASE", "BEE_API_TOKEN",
    "BEE_CURSOR_FILE", "BEE_INBOX_FILE", "BEE_CA_FILE",
)
VAULT_KEYS = ("BEE_BROKER_TOKEN", "BEE_API_TOKEN")

DEFAULTS = {
    "BEE_EVENTS_PATH": "/v1/stream",
    "BEE_EVENT_TYPES": "todo-created,todo-updated",  # conservative; utterances flood
    "BEE_AGENT_ID": "bee-lane",
    # local, not broker: the broker path drops the wire tier and the RECEIVING
    # core applies REMOTE_TASK_TIER, which defaults to owner.
    "BEE_SINK": "local",
}

_REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO / "src"))

# Third-party device content: confine the body against header/fence
# injection; if the shared guard is missing, fail CLOSED with an inline defang.
try:
    from task_body_guard import confine_user_content
except Exception:  # pragma: no cover - only when src/ is absent
    import re as _re
    _ZWSP = "\u200b"
    _INLINE_FORGE = _re.compile(r"^(={3,}|[\w-]+\s*:)")
    _INLINE_SEP = _re.compile("\r\n|[\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")

    def confine_user_content(text: str) -> str:
        if not text:
            return text
        out = []
        for line in _INLINE_SEP.sub("\n", text).split("\n"):
            out.append(_ZWSP + line if _INLINE_FORGE.match(line.lstrip()) else line)
        return "\n".join(out)


# One id alphabet for every Bee-controlled value that becomes file/header
# structure: task ids validate against it, other ids are confined to it.
_SAFE_ID_CHARS = "A-Za-z0-9._-"
_SAFE_ID_RE = re.compile(rf"[{_SAFE_ID_CHARS}]{{1,48}}")
_UNSAFE_ID_CHAR_RE = re.compile(rf"[^{_SAFE_ID_CHARS}]")


def _confine_id(raw: object, limit: int = 120) -> str:
    """Confine a provider-controlled identifier to the safe id alphabet —
    it lands on single-line `channel_id:`/`room_id` structure downstream."""
    return _UNSAFE_ID_CHAR_RE.sub("-", str(raw)[:limit]) or "unknown"


def _safe_task_id(raw: str) -> str:
    """Mirror of the broker-side id rule: sparrow's task-id contract is
    [A-Za-z0-9._-]{1,64}; in-alphabet ids pass through, anything else becomes
    a stable sha256 slug so no task can be lane-rejected."""
    if _SAFE_ID_RE.fullmatch(raw):
        return f"task-bee-{raw}"
    return f"task-bee-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def stream_request(cfg: dict) -> "tuple[str, dict]":
    """SUBSCRIBE half: (url, headers) for this source's live event stream.
    Two modes: the local authenticated proxy (`bee login`), or the headless
    cloud API (BEE_API_BASE + BEE_API_TOKEN bearer) for an always-on runner."""
    direct = bool(cfg.get("BEE_API_BASE") and cfg.get("BEE_API_TOKEN"))
    base = cfg["BEE_API_BASE"] if direct else cfg["BEE_PROXY_URL"]
    headers = {"Accept": "text/event-stream",
               "User-Agent": "sutando-bee-watcher/1.0"}
    if direct:
        headers["Authorization"] = f"Bearer {cfg['BEE_API_TOKEN']}"
    return base.rstrip("/") + cfg["BEE_EVENTS_PATH"], headers


def ssl_context(cfg: dict):
    """Direct-cloud TLS: Bee's API sits behind a private CA the system trust
    store lacks (per Bee's docs) — BEE_CA_FILE supplies it; unset = default."""
    ca = (cfg.get("BEE_CA_FILE") or "").strip()
    if not ca:
        return None
    import ssl
    return ssl.create_default_context(cafile=ca)


def source_configured(cfg: dict) -> bool:
    """True when either subscribe mode (proxy or cloud bearer) is configured."""
    return bool(cfg.get("BEE_PROXY_URL")
                or (cfg.get("BEE_API_BASE") and cfg.get("BEE_API_TOKEN")))


def event_to_task(etype: str, event_id: str, data: dict) -> dict:
    """NORMALIZE half: one SSE event into the relay task shape.

    Live-stream shape: text nests under `utterance.text`/`todo.text` with
    the stable id at `.id`; the conversation key is `conversation_uuid`;
    no SSE `id:` field is sent, so the payload's stable entity id is the
    dedupe key. Unknown shapes fall back to compact JSON, never dropped."""
    utt = data.get("utterance") if isinstance(data.get("utterance"), dict) else {}
    todo = data.get("todo") if isinstance(data.get("todo"), dict) else {}
    text = ""
    for v in (utt.get("text"), todo.get("text"), data.get("text"),
              data.get("summary"), data.get("name"), data.get("title")):
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text:
        text = json.dumps(data, separators=(",", ":"))[:500]
    text = confine_user_content(text)   # untrusted device content — defang

    # Device-controlled id becomes a header line: strict allowlist, so an
    # embedded newline can never forge one.
    conv = _confine_id(data.get("conversation_uuid") or data.get("conversation_id")
                       or data.get("id") or event_id)
    stable = str(utt.get("id") or todo.get("id") or data.get("id") or event_id)
    # Tier belongs to the SOURCE, not the sink: an omitted access_tier is
    # read as owner downstream, so every sink must carry it explicitly.
    return {
        "id": _safe_task_id(f"{etype}-{stable}"),
        "task": f"[Bee {etype}] {text}",
        "source": "bee",
        "user_id": "bee",
        "sender_name": "Bee",
        "channel_id": conv,
        "room_name": "Bee",
        "interaction_type": "message",
        "access_tier": "ambient",
        "priority": "low",
    }
