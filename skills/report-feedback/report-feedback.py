#!/usr/bin/env python3
"""Report a bug / feature request / feedback about Sutando to the team.

Files to the cloud /api/feedback route (the same API the desktop "Report an
issue" form uses, which mirrors into GitHub issues), attaching diagnostic
context (platform + a tail of recent workspace logs). This is the single
reporting path for every surface — chat, Discord, Telegram, and voice (via
task delegation) — so there's no per-surface duplication.

Usage:
  python3 skills/report-feedback/report-feedback.py \
      --title "..." [--body "..."] [--kind bug|feature|other] \
      [--severity low|medium|high|critical] [--no-logs]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _redact(text: str) -> str:
    """Best-effort scrub of secrets/PII before a log excerpt leaves the machine.

    A backstop, not a guarantee: the /api/feedback mirror is a private Slack
    channel today, but /api/feedback can also open GitHub issues, so we mask the
    obvious leak vectors (auth headers, key=value secrets, common token formats,
    and the home-dir username) rather than ship raw logs. Kept deliberately
    simple — it should never throw or mangle the excerpt beyond recognition.
    """
    if not text:
        return text
    # Authorization: Bearer <tok>  /  "Bearer abc123"
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", text)
    # token=... / api_key: "..." / key=... / secret=... / password=... / authorization=...
    text = re.sub(
        r"(?i)\b(token|api[_-]?key|key|secret|password|passwd|authorization)\b"
        r"(\s*[:=]\s*)(\"?)([^\s\"',;&]+)",
        r"\1\2\3<redacted>",
        text,
    )
    # Common provider token formats (sk-..., xox*-..., xapp-..., ghp_..., github_pat_..., AIza...)
    text = re.sub(
        r"\b(sk|xox[a-z]|xapp|ghp|gho|ghs|github_pat)[_-][A-Za-z0-9_\-]{6,}",
        "<redacted-token>",
        text,
    )
    # Google API keys used in Gemini transport URLs: AIza + 35 URL-safe chars.
    text = re.sub(r"\bAIza[0-9A-Za-z_\-]{35}\b", "<redacted-token>", text)
    # AWS access keys: AKIA + 16 uppercase alphanumeric (no separator — AKIAIOSFODNN7EXAMPLE)
    text = re.sub(r"\bAKIA[A-Z0-9]{16}\b", "<redacted-token>", text)
    # Home dir → /Users/<user> so the OS username doesn't leak
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "/Users/<user>")
    return text


def resolve_workspace() -> Path:
    """Canonical workspace, mirroring the TS/py resolver; fall back to <repo>/workspace."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from workspace_default import resolve_workspace as rw  # type: ignore

        return Path(rw())
    except Exception:
        return Path(__file__).resolve().parents[2] / "workspace"


def read_cloud_auth(ws: Path):
    """Return (apiBase, token) if signed in to Sutando Cloud, else (None, None).

    Matches the desktop's readCloudAuth (electron/ipc.cjs). Post-M1 the record
    lives at ``<workspace>/state/auth/cloud-auth.json``; the pre-M1 root
    ``<workspace>/cloud-auth.json`` is probed as a 30-day reader fallback. Both
    packaged-app workspace equivalents are also probed so the skill finds the
    token even when running from a different checkout. Falls back to the metering
    env the supervisor injects for signed-in runs.
    """
    seen: set[str] = set()
    _app_ws = Path.home() / ".sutando" / "repo" / "workspace"
    for p in (
        ws / "state" / "auth" / "cloud-auth.json",  # M1 canonical (state/auth/cloud-auth.json)
        ws / "cloud-auth.json",  # pre-M1 root fallback (30-day reader window per workspace contract)
        _app_ws / "state" / "auth" / "cloud-auth.json",  # packaged-app M1 canonical
        _app_ws / "cloud-auth.json",  # packaged-app pre-M1 fallback
        Path.home() / "Library" / "Application Support" / "@stando" / "ui" / "cloud-auth.json",  # legacy
    ):
        rp = str(p)
        if rp in seen:
            continue
        seen.add(rp)
        try:
            if p.exists():
                d = json.loads(p.read_text())
                if d.get("token"):  # signed in == has token (matches desktop)
                    return (d.get("apiBase") or "https://sutando.ag2.ai"), d["token"]
        except Exception:
            continue

    hdrs = os.environ.get("SUTANDO_METERING_HEADERS")
    if hdrs:
        try:
            auth = json.loads(hdrs).get("Authorization", "")
            tok = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
            base = os.environ.get("SUTANDO_METERING_ENDPOINT", "").replace("/api/usage/v2", "").rstrip("/")
            if tok:
                return (base or "https://sutando.ag2.ai"), tok
        except Exception:
            pass
    return None, None


def logs_excerpt(ws: Path):
    """Last 40 lines of the 4 most-recent <workspace>/logs/*.log (capped ~8KB)."""
    try:
        logs = ws / "logs"
        if not logs.is_dir():
            return None, []
        files = sorted(
            (f for f in logs.iterdir() if f.suffix == ".log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:4]
        parts = []
        for f in files:
            tail = "\n".join(f.read_text(errors="replace").splitlines()[-40:])
            parts.append(f"===== {f.name} (last 40 lines) =====\n{tail}")
        return _redact("\n\n".join(parts))[-8000:], [f.name for f in files]
    except Exception:
        return None, []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--kind", choices=["bug", "feature", "other"], default="bug")
    ap.add_argument("--severity", choices=["low", "medium", "high", "critical"], default="medium")
    ap.add_argument("--no-logs", action="store_true", help="Omit the diagnostic log excerpt.")
    a = ap.parse_args()

    if not a.title.strip():
        print("ERROR: --title is required (a short one-line summary).")
        sys.exit(1)

    ws = resolve_workspace()
    base, token = read_cloud_auth(ws)
    if not token:
        print("NOT_SIGNED_IN: not signed in to Sutando Cloud — ask the user to sign in (Settings → Sutando Cloud), then retry.")
        sys.exit(2)

    ctx: dict = {"source": "core-agent", "platform": platform.platform(), "python": platform.python_version()}
    if not a.no_logs:
        excerpt, names = logs_excerpt(ws)
        if excerpt:
            ctx["last_logs_excerpt"] = excerpt
            ctx["log_files"] = names

    payload = {
        "kind": a.kind,
        "severity": a.severity,
        "title": a.title.strip(),
        # Always a string — the /api/feedback schema types `body` as string and
        # rejects null (400 invalid_payload). Mirror the desktop form, which
        # sends the trimmed body (possibly ""). Fall back to the title so an
        # empty-body report still carries context.
        "body": a.body.strip() or a.title.strip(),
        "context": ctx,
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/feedback",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Sutando-Feedback/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"OK: filed {a.kind} report ({r.status}).")
    except urllib.error.HTTPError as e:
        print(f"ERROR: feedback API {e.code}: {e.read().decode(errors='replace')[:300]}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
