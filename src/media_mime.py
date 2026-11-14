"""The media type an upload declares to the gateway, from the file's name.

`room.media.upload` defaults `mime` to `application/octet-stream`, and the server picks the
message type from it: an upload that declares nothing arrives as a file attachment, so a video
an agent sends is drawn as a file card instead of a player (V148). Every uploader therefore
declares the type rather than leaving the default to decide.

`mimetypes` alone is not enough on a stock interpreter: `.mov` and `.webm` map differently or not
at all depending on the platform's mime database, and those are the two formats a screen recording
arrives as. The table below pins the ones that matter; everything else falls back to `mimetypes`
and then to the same octet-stream the server would have assumed.
Stdlib only; vendored byte-identical into the ag2-sparrow package by
packages/ag2-sparrow/tools/sync_from_src.py.
"""
from __future__ import annotations

import mimetypes
import os

DEFAULT_MIME = "application/octet-stream"

# Pinned where the platform database disagrees or is silent. Keys are lowercase, with the dot.
_PINNED = {
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
    ".m4a": "audio/mp4",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".webp": "image/webp",
}


def guess_mime(filename: str) -> str:
    """The media type for `filename`, never empty; `DEFAULT_MIME` when nothing is known.

    Takes a name, not a path's contents: the callers have already read the bytes and the server
    re-checks the type itself, so this only has to stop the default from being the answer.
    """
    if not filename:
        return DEFAULT_MIME
    ext = os.path.splitext(str(filename))[1].lower()
    pinned = _PINNED.get(ext)
    if pinned:
        return pinned
    guessed, _ = mimetypes.guess_type(str(filename))
    return guessed or DEFAULT_MIME


def is_video(mime: str) -> bool:
    """Whether `mime` names a video, the case the file-card bug was about."""
    return isinstance(mime, str) and mime.lower().startswith("video/")
