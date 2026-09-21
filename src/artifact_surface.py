"""Typed artifacts an agent writes and the client renders — producer side.

The agent writes CONTENT and names its TYPE; the client owns every renderer.
That split is the owner's (2026-09-21), and it is why there is no registry
here: the type set is closed and ours, so this module validates against it
rather than discovering it.

What survives a closed type set is version skew, because the two ends update
by different mechanisms: an agent's skill is pulled per host, the client is
deployed. They are never in step and nobody updates both at once. So every
artifact states the schema version it was written against, and a renderer
that reads a version it does not know degrades to the plain form instead of
failing — the caller gets `degrade_to_plain()` for exactly that.

Rides the existing `space.ag2.*` extra_content channel (same mechanism as the
a2ui review card and the hitl card), so a room carries one card vocabulary.
"""
from __future__ import annotations

from typing import Any, Dict

FIELD = "space.ag2.artifact"
SCHEMA_VERSION = 1

# The closed set. A type lands here only when a renderer for it ships.
X_POST = "x_post"
LINKEDIN_POST = "linkedin_post"
EMAIL = "email"
TYPES = (X_POST, LINKEDIN_POST, EMAIL)

# Per-type required content keys. An absent key is the producer's bug: a
# renderer must never have to guess whether a key is missing or merely empty.
REQUIRED: Dict[str, tuple] = {
    X_POST: ("text",),
    LINKEDIN_POST: ("text",),
    EMAIL: ("subject", "body"),
}


class ArtifactError(ValueError):
    """The producer built something a renderer would have to guess about."""


def build(artifact_type: str, content: Dict[str, Any],
          version: int = SCHEMA_VERSION) -> Dict[str, Any]:
    """One artifact, ready to ride as `extra_content[FIELD]`.

    Refuses here rather than at the renderer: a malformed artifact that
    reaches a client is a crash someone else has to diagnose.
    """
    if artifact_type not in TYPES:
        raise ArtifactError(
            f"unknown artifact type {artifact_type!r}; the set is closed: {', '.join(TYPES)}")
    if not isinstance(content, dict):
        raise ArtifactError("content must be a dict")
    missing = [k for k in REQUIRED[artifact_type] if not str(content.get(k) or "").strip()]
    if missing:
        raise ArtifactError(
            f"{artifact_type} needs {', '.join(REQUIRED[artifact_type])}; missing or empty: "
            f"{', '.join(missing)}")
    return {"type": artifact_type, "version": int(version), "content": dict(content)}


def degrade_to_plain(artifact: Dict[str, Any], known_version: int = SCHEMA_VERSION
                     ) -> Dict[str, Any] | None:
    """What a renderer shows when it cannot render this artifact properly.

    Returns None when the artifact renders normally (known type, version at or
    below what the renderer knows). Otherwise returns the plain form to draw
    instead. It never raises: a renderer calling this is already on the path
    where something is unfamiliar, and that path must not be the crashing one.
    """
    if not isinstance(artifact, dict):
        return {"reason": "malformed", "text": ""}
    a_type = artifact.get("type")
    raw_content = artifact.get("content")
    # A known type with the wrong content SHAPE still degrades: letting it
    # through hands the renderer something to guess about.
    malformed = not isinstance(raw_content, dict)
    content = raw_content if isinstance(raw_content, dict) else {}
    try:
        version = int(artifact.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    if a_type in TYPES and 0 < version <= known_version and not malformed:
        return None
    if malformed:
        reason = "malformed"
    else:
        reason = "unknown_type" if a_type not in TYPES else "newer_version"
    # Best-effort text so a degraded card still carries the writing, which is
    # the part the reader came for.
    text = str(content.get("text") or content.get("body") or content.get("subject") or "")
    return {"reason": reason, "type": a_type, "version": version, "text": text}


def as_extra_content(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """The `extra_content` mapping for one artifact."""
    return {FIELD: artifact}
