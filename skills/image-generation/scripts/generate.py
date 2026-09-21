#!/usr/bin/env python3
"""Generate or edit an image with a Gemini image model (REST, standard library only), or a video with
Veo (google-genai SDK, optional). The key is whatever the credential resolver finds for the
`gemini-image` capability: the managed key of a desktop install, else GEMINI_API_KEY / GEMINI_VOICE_API_KEY.

stdout is exactly one JSON line, always:
  {"ok": true, "path": "<file>", "model": "<model>", "note": "<model text, if any>"}
  {"ok": false, "error": "no_key|refused|no_image|api_error|sdk_missing|bad_input", "message": "...", "remedy": "..."}
Exit 0 on ok; 1 when Gemini declined or answered without an image (refused, no_image, api_error);
2 when this install cannot do it at all (no_key, sdk_missing, bad_input). Progress goes to stderr.

Usage:
  python3 generate.py --prompt "A sunset over mountains"
  python3 generate.py --input photo.jpg --prompt "Replace the background"
  python3 generate.py --video --prompt "A timelapse of a city" --output city.mp4
Output defaults to <workspace>/results/media/generated-<ts>.png (.mp4), inside the attachment allowlist.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root

# parents[3] is the repo when the skill runs from its checkout (or the symlink skills/install.sh
# makes); a copied install has no core tree, and every import below degrades to env-only.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
DEFAULT_VIDEO_MODEL = "veo-3.1-generate-preview"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT_S = 180
REFUSAL_REASONS = ("SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
                   "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "IMAGE_OTHER")
EXIT = {"ok": 0, "refused": 1, "no_image": 1, "api_error": 1, "no_key": 2, "sdk_missing": 2, "bad_input": 2}
REMEDY = {
    "no_key": "Add a Gemini key in Agent settings → Agent → Gemini API, or ask again once your plan includes the managed key.",
    "refused": "Reword the prompt: no real people's faces, no copyrighted characters, nothing explicit.",
    "no_image": "Try a more concrete prompt that describes a picture, or name the style and the subject.",
    "api_error": "Try again in a moment; if it keeps failing, check the key and the model name (IMAGE_MODEL).",
    "sdk_missing": "Run `pip3 install google-genai` on this machine, or ask for an image instead of a video.",
    "bad_input": "Check the input image path and format (png, jpg, webp, gif).",
}


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return EXIT["ok" if payload.get("ok") else payload["error"]]


def fail(error: str, message: str) -> int:
    return emit({"ok": False, "error": error, "message": message, "remedy": REMEDY[error]})


def log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def load_env() -> None:
    """A checkout's .env keys (GEMINI_*) win over a stale shell env, as before; nothing else is read."""
    for env_path in (SKILL_DIR.parents[1] / ".env", Path.home() / ".env"):
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if key.startswith("GEMINI_"):
                os.environ[key] = val


def resolve_key() -> tuple[str, str]:
    """(key, source) for the gemini-image capability: the resolver when the core tree is reachable,
    else the same env chain (text key, then voice key)."""
    try:
        from credential_resolver import resolve_credential  # noqa: PLC0415
        got = resolve_credential("gemini-image")
        return got.key, got.source
    except Exception:  # noqa: BLE001 - a copied install has no core tree
        for var in ("GEMINI_API_KEY", "GEMINI_VOICE_API_KEY"):
            if os.environ.get(var):
                return os.environ[var], "env"
        return "", "none"


def manifest_config(name: str) -> str | None:
    try:
        cfg = json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8")).get("config") or {}
        return cfg.get(name) or None
    except (OSError, ValueError, AttributeError):
        return None


def media_dir() -> Path:
    """<workspace>/results/media (the attachment allowlist); a temp sutando- dir without a core tree."""
    try:
        from workspace_default import resolve_workspace  # noqa: PLC0415
        return resolve_workspace() / "results" / "media"
    except Exception:  # noqa: BLE001
        return Path(tempfile.gettempdir()) / "sutando-media"


def output_path(requested: str | None, ext: str) -> Path:
    if requested:
        return Path(os.path.expanduser(requested))
    return media_dir() / f"generated-{int(time.time() * 1000)}{ext}"


def read_input_image(path: str) -> tuple[bytes, str] | None:
    """The image bytes and mime type, downscaled to 4096px when Pillow is around; None when the
    file is missing, not an image by extension, or one Pillow cannot decode."""
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return None
    mime = mimetypes.guess_type(p.name)[0] or ""
    if not mime.startswith("image/"):
        return None
    data = p.read_bytes()
    try:
        from PIL import Image  # noqa: PLC0415
        import io  # noqa: PLC0415
    except ImportError:
        return data, mime
    # Pillow raises OSError (UnidentifiedImageError) for bytes that are not an image: bad input, not a crash.
    try:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > 4096:
            ratio = 4096 / max(img.size)
            img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            data, mime = buf.getvalue(), "image/png"
            log(f"Resized {p} to {img.size[0]}x{img.size[1]}")
    except OSError as err:
        log(f"Not a readable image ({err}): {p}")
        return None
    return data, mime


def call_gemini(key: str, model: str, parts: list[dict], opener=None) -> dict:
    """One generateContent call; raises urllib errors and ValueError (non-JSON body) to the caller."""
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}).encode("utf-8")
    req = urllib.request.Request(ENDPOINT.format(model=model), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def api_error_message(err: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(err.read().decode("utf-8", "replace"))
        return str((detail.get("error") or {}).get("message") or f"HTTP {err.code}")
    except (ValueError, AttributeError, OSError):
        return f"HTTP {err.code}"


def first_image(response: dict) -> tuple[bytes | None, str, str, str]:
    """(image bytes, mime, model text, refusal reason) from a generateContent response."""
    text, reason = "", ""
    feedback = response.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        reason = str(feedback["blockReason"])
    for cand in response.get("candidates") or []:
        finish = str(cand.get("finishReason") or "")
        if finish in REFUSAL_REASONS:
            reason = reason or finish
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and str(inline.get("mimeType") or inline.get("mime_type") or "").startswith("image/"):
                mime = str(inline.get("mimeType") or inline.get("mime_type"))
                return base64.b64decode(inline.get("data") or ""), mime, text, reason
            if part.get("text"):
                text += part["text"]
    return None, "", text.strip(), reason


def save_image(data: bytes, mime: str, out: Path, quality: int) -> Path:
    """Write the bytes; convert to the requested extension only when Pillow can, else keep the
    returned format under its own extension so the file is never mislabelled."""
    wanted = out.suffix.lower()
    returned = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    if wanted in (".jpg", ".jpeg", ".webp") and wanted != returned:
        try:
            from PIL import Image  # noqa: PLC0415
            import io  # noqa: PLC0415
            img = Image.open(io.BytesIO(data))
            fmt = "JPEG" if wanted in (".jpg", ".jpeg") else "WEBP"
            if fmt == "JPEG":
                img = img.convert("RGB")
            img.save(str(out), fmt, quality=quality)
            return out
        except ImportError:
            out = out.with_suffix(returned)
            log(f"Pillow not installed: saved the returned {mime} as {out.name}")
        except OSError as err:
            out = out.with_suffix(returned)
            log(f"Pillow could not convert the returned {mime} ({err}): saved it as {out.name}")
    elif wanted not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        out = out.with_suffix(returned)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


def generate_image(args, key: str, opener=None) -> int:
    parts: list[dict] = []
    for path in args.input:
        got = read_input_image(path)
        if got is None:
            return fail("bad_input", f"Input image not found or not an image: {path}")
        data, mime = got
        parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}})
        log(f"Input: {path} ({mime}, {len(data)} bytes)")
    parts.append({"text": args.prompt})
    model = args.model or os.environ.get("IMAGE_MODEL") or manifest_config("IMAGE_MODEL") or DEFAULT_IMAGE_MODEL
    log(f"Model: {model}")
    log(f"Prompt: {args.prompt[:100]}{'...' if len(args.prompt) > 100 else ''}")
    try:
        response = call_gemini(key, model, parts, opener)
    except urllib.error.HTTPError as err:
        return fail("api_error", api_error_message(err))
    except (urllib.error.URLError, OSError, ValueError) as err:
        return fail("api_error", str(getattr(err, "reason", None) or err))
    data, mime, text, reason = first_image(response if isinstance(response, dict) else {})
    if data:
        out = save_image(data, mime, output_path(args.output, ".png"), args.quality)
        log(f"Saved: {out} ({len(data)} bytes, {mime})")
        result = {"ok": True, "path": str(out.resolve()), "model": model}
        if text:
            result["note"] = text
        return emit(result)
    if reason:
        return fail("refused", f"Gemini declined ({reason})" + (f": {text}" if text else ""))
    return fail("no_image", text or "Gemini returned no image and no text")


def generate_video(args, key: str) -> int:
    """Veo needs the google-genai SDK (long-running operation + file download); say so when it is missing."""
    try:
        from google import genai  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415
    except ImportError:
        return fail("sdk_missing", "Video generation needs the google-genai package, which is not installed")
    client = genai.Client(api_key=key)
    model = args.model or os.environ.get("VIDEO_MODEL") or manifest_config("VIDEO_MODEL") or DEFAULT_VIDEO_MODEL
    aspect = args.aspect or "16:9"
    out = output_path(args.output, ".mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"Model: {model}  Aspect: {aspect}")
    image = None
    if args.input:
        got = read_input_image(args.input[0])
        if got is None:
            return fail("bad_input", f"Reference image not found or not an image: {args.input[0]}")
        image = types.Image(image_bytes=got[0], mime_type=got[1])
    log("Generating video (this may take 1-3 minutes)...")
    try:
        kwargs = {"model": model, "prompt": args.prompt, "config": types.GenerateVideosConfig(aspect_ratio=aspect)}
        if image is not None:
            kwargs["image"] = image
        operation = client.models.generate_videos(**kwargs)
        elapsed = 0
        while not operation.done:
            time.sleep(10); elapsed += 10
            log(f"Waiting... ({elapsed}s)")
            operation = client.operations.get(operation)
        video = operation.response.generated_videos[0]
        client.files.download(file=video.video)
        video.video.save(str(out))
    except Exception as err:  # noqa: BLE001 - every SDK failure is one api_error to the owner
        return fail("api_error", str(err))
    return emit({"ok": True, "path": str(out.resolve()), "model": model})


def main(argv: list[str] | None = None, opener=None) -> int:
    ap = argparse.ArgumentParser(description="Generate images (Gemini, REST) or videos (Veo) from a prompt")
    ap.add_argument("--prompt", "-p", required=True, help="Text prompt")
    ap.add_argument("--input", "-i", action="append", default=[], help="Input image path(s) to edit or reference")
    ap.add_argument("--output", "-o", default=None, help="Output file path (default: <workspace>/results/media/generated-<ts>.png|.mp4)")
    ap.add_argument("--model", "-m", default=None, help="Model id (default: IMAGE_MODEL / VIDEO_MODEL from env or the manifest)")
    ap.add_argument("--quality", "-q", type=int, default=90, help="JPEG/WEBP quality 1-100 when converting with Pillow (default: 90)")
    ap.add_argument("--video", "-v", action="store_true", help="Generate a video instead of an image (needs google-genai)")
    ap.add_argument("--aspect", default=None, help="Video aspect ratio: 16:9 (default) or 9:16")
    args = ap.parse_args(argv)
    load_env()
    key, source = resolve_key()
    if not key:
        return fail("no_key", "There is no Gemini key for image generation on this install (managed, GEMINI_API_KEY or GEMINI_VOICE_API_KEY)")
    log(f"Key: {source}")
    return generate_video(args, key) if args.video else generate_image(args, key, opener)


if __name__ == "__main__":
    sys.exit(main())
