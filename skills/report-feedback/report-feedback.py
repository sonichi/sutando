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
      [--severity low|medium|high|critical] [--no-logs] [--auto]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Hosts /api/feedback may redirect between. Credentials are re-sent ONLY to
# these; any other target aborts rather than forwarding the owner's token.
TRUSTED_API_HOSTS = frozenset({"sutando.ag2.ai", "sutando.ag2.space"})

# Test seam. Empty in production: a redirect that downgrades to plaintext must
# never replay the bearer token, so http is allowed only where a test opts in.
INSECURE_REDIRECT_HOSTS: frozenset[str] = frozenset()

DEFAULT_CLOUD_ORIGIN = "https://sutando.ag2.space"
# sutando.ag2.ai 307s to .space and clients drop Authorization across the
# cross-origin redirect, so a bearer sent there reads back as a bogus 401.
RETIRED_CLOUD_ORIGINS = ("https://sutando.ag2.ai",)
# What the desktop host overwrites the Keychain token with on sign-out (its
# vault CLI has no delete verb); must read as "not signed in".
SIGNED_OUT_SENTINEL = "__signed_out__"

# Owner prefs written by the desktop Settings UI (host is the single writer).
# autoReport defaults ON; sendLogs defaults OFF. The log excerpt is the part
# that carries incidental owner data, so it is opt-in rather than opt-out.
PREFS_DEFAULTS = {"autoReport": True, "sendLogs": False, "askFirst": False}
# Ask-first: an automatic report is parked as a draft and the owner gets a card
# (File / File without logs / Skip) instead of a filing; the reply files or drops it.
DRAFTS_DIR = "feedback-drafts"
DRAFT_ID_RE = re.compile(r"^fb_[0-9a-f]{10}$")  # the writer's grammar; anything else never becomes a path
HITL_RUNTIME = "report-feedback"
ASK_ACTIONS = [
    {"id": "file", "kind": "confirmation", "label": "File this bug report"},
    {"id": "file_no_logs", "kind": "confirmation", "label": "File without logs"},
    {"id": "skip", "kind": "confirmation", "label": "Skip"},
]
# Auto-report throttle state (this script is the single writer).
AUTO_STATE_FILE = "feedback-auto-reports.json"
AUTO_DEDUPE_WINDOW_S = 24 * 3600
AUTO_DAILY_CAP = 5


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


def _normalize_base(base: str) -> str:
    """A retired production origin IS the current one — never send a bearer to
    it (the 307 to the new host drops Authorization → a misleading 401)."""
    base = (base or "").strip().rstrip("/")
    if base in RETIRED_CLOUD_ORIGINS or not base:
        return DEFAULT_CLOUD_ORIGIN
    return base


def _fnv1a64(s: str) -> int:
    """FNV-1a 64-bit, byte-for-byte the desktop host's (cloud_session.rs)."""
    h = 0xCBF29CE484222325
    for b in s.encode():
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def origin_vault_key(origin: str) -> str:
    """Origin-scoped Keychain key, matching cloud_session.rs origin_key_suffix."""
    slug = "".join(c.upper() if (c.isascii() and c.isalnum()) else "_" for c in origin)
    return f"AG2_CLOUD_TOKEN_{slug}_{_fnv1a64(origin):016X}"


def resolve_cloud_origin() -> str:
    env = os.environ.get("AG2_CLOUD_ORIGIN", "").strip().rstrip("/")
    return env or DEFAULT_CLOUD_ORIGIN


def _keychain_get(key: str):
    """Read one Keychain secret the way the engine vault does; None if absent."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "sutando", "-s", key, "-w"],
            capture_output=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        return r.stdout.decode().strip() or None
    except Exception:
        return None


def read_keychain_auth():
    """(apiBase, token) from the Tauri host's origin-scoped Keychain session.

    The desktop host stores the sutk_ ONLY in the Keychain, under a key bound
    to the cloud origin it was minted against (no cross-origin fallback except
    the host's own retired-production carry-over, mirrored here).
    """
    origin = resolve_cloud_origin()
    candidates = [origin]
    if origin == DEFAULT_CLOUD_ORIGIN:
        candidates.extend(RETIRED_CLOUD_ORIGINS)
    for o in candidates:
        tok = _keychain_get(origin_vault_key(o))
        if tok and tok != SIGNED_OUT_SENTINEL:
            return origin, tok
    # Pre-origin-scoping installs stored a bare, unscoped key.
    tok = _keychain_get("AG2_CLOUD_TOKEN")
    if tok and tok != SIGNED_OUT_SENTINEL:
        return origin, tok
    return None, None


def read_cloud_auth(ws: Path):
    """Return (apiBase, token) if signed in to Sutando Cloud, else (None, None).

    Matches the desktop's readCloudAuth (electron/ipc.cjs). Post-M1 the record
    lives at ``<workspace>/state/auth/cloud-auth.json``; the pre-M1 root
    ``<workspace>/cloud-auth.json`` is probed as a 30-day reader fallback. Both
    packaged-app workspace equivalents are also probed so the skill finds the
    token even when running from a different checkout. The Tauri desktop writes
    no auth file at all — its session lives in the Keychain, probed next.
    Falls back to the metering env the supervisor injects for signed-in runs.
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
                    return _normalize_base(d.get("apiBase") or ""), d["token"]
        except Exception:
            continue

    base, tok = read_keychain_auth()
    if tok:
        return base, tok

    hdrs = os.environ.get("SUTANDO_METERING_HEADERS")
    if hdrs:
        try:
            auth = json.loads(hdrs).get("Authorization", "")
            tok = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
            base = os.environ.get("SUTANDO_METERING_ENDPOINT", "").replace("/api/usage/v2", "")
            if tok:
                return _normalize_base(base), tok
        except Exception:
            pass
    return None, None


def why_no_logs(ws: Path) -> str:
    """Why logs_excerpt() came back empty, in words a ticket reader can act on.

    Runs ONLY on the failure path, so it must never raise: logs_excerpt() already
    degraded to (None, []) here, and an exception would turn a report filed
    without logs into a report not filed at all.
    """
    logs = ws / "logs"
    try:
        if not logs.is_dir():
            return f"no logs directory at {logs} (reporter ran outside a live workspace)"
        if not any(f.suffix == ".log" for f in logs.iterdir()):
            return f"{logs} has no .log files"
    except OSError as exc:
        return f"{logs} could not be listed ({type(exc).__name__})"
    except Exception:  # noqa: BLE001 - the explanation must not outrank the report
        return f"{logs} could not be inspected"
    return f"{logs} exists but its log files could not be read"


def read_prefs(ws: Path) -> dict:
    """Owner bug-report prefs from <workspace>/state/feedback-prefs.json.

    Written by the desktop Settings toggles ("File automatic bug reports",
    "Send logs with bug reports"). Missing file, missing key, or a non-bool
    value all read as PREFS_DEFAULTS: autoReport ON, sendLogs OFF.

    The two defaults differ on purpose. Absence of the file must never disable
    REPORTING on installs that predate the toggles. But absence is not consent
    either, and the log excerpt is the part that carries incidental owner data
    (paths with usernames, hostnames, workspace content), so it is opt-in: an
    owner who has never opened Settings ships no logs.
    """
    prefs = dict(PREFS_DEFAULTS)
    try:
        d = json.loads((ws / "state" / "feedback-prefs.json").read_text())
        for k in prefs:
            if isinstance(d.get(k), bool):
                prefs[k] = d[k]
        # The room the ask-first card goes to (the owner's DM); a string, unlike the switches.
    except Exception:
        pass
    return prefs


def _drafts_dir(ws: Path) -> Path:
    return ws / "state" / DRAFTS_DIR


def write_draft(ws: Path, payload: dict, now: float | None = None) -> str:
    """Park a report the owner has not approved yet; returns the draft id."""
    d = _drafts_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    draft_id = f"fb_{uuid.uuid4().hex[:10]}"
    rec = {"id": draft_id, "created": now if now is not None else time.time(), "payload": payload}
    tmp = d / f".{draft_id}.tmp"
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    os.replace(tmp, d / f"{draft_id}.json")
    return draft_id


def list_drafts(ws: Path) -> list:
    """Pending drafts, oldest first."""
    out = []
    for f in sorted(_drafts_dir(ws).glob("fb_*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def _draft_path(ws: Path, draft_id: str) -> Path:
    if not DRAFT_ID_RE.match(draft_id or ""):
        raise ValueError(f"not a draft id: {draft_id!r}")
    return _drafts_dir(ws) / f"{draft_id}.json"


def load_draft(ws: Path, draft_id: str) -> dict | None:
    try:
        return json.loads(_draft_path(ws, draft_id).read_text())
    except Exception:
        return None


def drop_draft(ws: Path, draft_id: str) -> None:
    try:
        _draft_path(ws, draft_id).unlink()
    except FileNotFoundError:
        pass


def decision_for_reply(text: str) -> str | None:
    """Map an action reply's body (the card label, optionally `label — note`) to a decision."""
    head = (text or "").split(" — ", 1)[0].strip().lower()
    for a in ASK_ACTIONS:
        if head == a["label"].lower():
            return a["id"]
    return None


def hitl_manager(ws: Path):
    """The engine's HITL store: the bridge projects what is created here and applies the clicks."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from hitl.manager import HitlManager, HitlStore, default_store  # type: ignore

    return HitlManager(HitlStore(default_store(ws)))


def register_ask(ws: Path, draft_id: str, title: str, device: str) -> str:
    """One HumanRequirement per draft: the card the owner sees, keyed to the draft by guard."""
    from hitl.schema import Action, HumanRequirement  # type: ignore

    req = HumanRequirement(
        kind="choice", runtime=HITL_RUNTIME, title="File a bug report?",
        message=f"I hit what looks like a Sutando/AG2 Space defect: {title}. File it to the team?",
        guard=draft_id, device={"id": f"{HITL_RUNTIME}:{draft_id}", "name": device},
        actions=[Action(id=a["id"], kind=a["kind"], label=a["label"]) for a in ASK_ACTIONS],
        subject={"draft_id": draft_id, "title": title},
    )
    return hitl_manager(ws).create(req).id


def requirement_for_draft(manager, draft_id: str):
    for req in manager.active():
        if req.runtime == HITL_RUNTIME and req.guard == draft_id:
            return req
    return None


def apply_clicks(ws: Path, prefs: dict, device: str) -> dict:
    """Register parked drafts that have no card yet, then run every choice the owner clicked.
    A decision that cannot complete (signed out, API down) stays in progress for the next run."""
    out = {"registered": [], "applied": [], "kept": [], "cancelled": []}
    manager = hitl_manager(ws)
    for rec in list_drafts(ws):
        if requirement_for_draft(manager, rec["id"]) is None:
            out["registered"].append(register_ask(ws, rec["id"], rec["payload"]["title"], device))
    for req in manager.active():
        if req.runtime != HITL_RUNTIME or req.status != "in_progress" or not req.chosen_action:
            continue
        draft_id = str((req.subject or {}).get("draft_id") or req.guard)
        if load_draft(ws, draft_id) is None:
            manager.cancel(req.id)  # the draft is gone (decided by hand, or never valid): nothing to run
            out["cancelled"].append(req.id)
            continue
        if decide(ws, prefs, draft_id, req.chosen_action) == 0:
            manager.resolve(req.id)
            out["applied"].append(req.id)
        else:
            out["kept"].append(req.id)
    return out


def _auto_state_path(ws: Path) -> Path:
    return ws / "state" / AUTO_STATE_FILE


def _read_auto_state(ws: Path) -> list:
    try:
        d = json.loads(_auto_state_path(ws).read_text())
        return [r for r in d.get("reports", []) if isinstance(r, dict)]
    except Exception:
        return []


def _auto_key(title: str) -> str:
    return hashlib.sha1(" ".join(title.lower().split()).encode()).hexdigest()


def check_auto_gate(ws: Path, title: str, now: float | None = None):
    """(ok, reason) — dedupe identical titles and cap volume in a 24h window."""
    now = now or time.time()
    recent = [r for r in _read_auto_state(ws) if now - r.get("ts", 0) < AUTO_DEDUPE_WINDOW_S]
    if any(r.get("key") == _auto_key(title) for r in recent):
        return False, "an identical report was already filed in the last 24h"
    if len(recent) >= AUTO_DAILY_CAP:
        return False, f"auto-report cap reached ({AUTO_DAILY_CAP} per 24h)"
    return True, ""


def record_auto_report(ws: Path, title: str, now: float | None = None) -> None:
    now = now or time.time()
    recent = [r for r in _read_auto_state(ws) if now - r.get("ts", 0) < AUTO_DEDUPE_WINDOW_S]
    recent.append({"key": _auto_key(title), "ts": now})
    path = _auto_state_path(ws)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"reports": recent}))
        os.replace(tmp, path)
    except Exception:
        pass  # throttle state is best-effort; never fail the report over it


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


def post_feedback(url: str, payload: dict, token: str, _hops: int = 0) -> int:
    """POST the report, following one 307/308 hop itself.

    urllib only auto-follows 307/308 for GET/HEAD — for POST it raises instead,
    so a cloud host that redirects (sutando.ag2.ai -> .space) makes every report
    fail with `feedback API 307` and file nothing.
    """
    req = urllib.request.Request(
        url,
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
            return r.status
    except urllib.error.HTTPError as e:
        if e.code not in (307, 308) or _hops >= 2:
            raise
        loc = e.headers.get("Location")
        if not loc:
            raise
        nxt = urllib.parse.urljoin(url, loc)
        split = urllib.parse.urlsplit(nxt)
        host = split.hostname or ""
        # Userinfo lets a target read as a trusted host to this check while
        # resolving elsewhere in other parsers; refuse rather than reconcile.
        if split.username or split.password:
            raise RuntimeError(
                "refusing to forward credentials to a redirect target carrying userinfo"
            ) from e
        if host not in TRUSTED_API_HOSTS:
            raise RuntimeError(
                f"refusing to forward credentials to untrusted redirect host {host!r}"
            ) from e
        if split.scheme != "https" and host not in INSECURE_REDIRECT_HOSTS:
            raise RuntimeError(
                f"refusing to forward credentials over {split.scheme or 'no'} scheme to {host!r}"
            ) from e
        return post_feedback(nxt, payload, token, _hops + 1)


def decide(ws: Path, prefs: dict, draft_id: str, choice: str) -> int:
    """The owner answered the card: file (with logs per prefs), file without logs, or skip.
    Returns the exit code; every failure keeps the draft parked."""
    if choice not in ("file", "file_no_logs", "skip"):
        print(f"ERROR: choice must be file | file_no_logs | skip, got {choice!r}.")
        return 1
    rec = load_draft(ws, draft_id)
    if not rec:
        print(f"ERROR: no parked draft {draft_id}.")
        return 1
    if choice == "skip":
        drop_draft(ws, draft_id)
        print(f"SKIPPED: draft {draft_id} dropped at the owner's request.")
        return 0
    base, token = read_cloud_auth(ws)
    if not token:
        print("NOT_SIGNED_IN: not signed in to Sutando Cloud — the draft stays parked; sign in, then retry.")
        return 2
    d = rec["payload"]
    ctx: dict = {"source": "core-agent", "platform": platform.platform(), "python": platform.python_version(),
                 "auto": bool(d.get("auto")), "owner_approved": True}
    with_logs = choice == "file" and prefs["sendLogs"] and not d.get("no_logs")
    if with_logs:
        excerpt, names = logs_excerpt(ws)
        if excerpt:
            ctx["last_logs_excerpt"] = excerpt
            ctx["log_files"] = names
        else:
            ctx["logs_omitted"] = why_no_logs(ws)
    else:
        ctx["logs_opted_out"] = True
    payload = {"kind": d["kind"], "severity": d["severity"], "title": d["title"], "body": d["body"], "context": ctx}
    try:
        status = post_feedback(f"{base.rstrip('/')}/api/feedback", payload, token)
    except urllib.error.HTTPError as e:
        print(f"ERROR: feedback API {e.code}: {e.read().decode(errors='replace')[:300]}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        return 1
    drop_draft(ws, draft_id)  # the ledger was written when the card was asked
    print(f"OK: filed {d['kind']} report ({status}) from draft {draft_id}.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    # Optional at parse time: --drafts and --decide carry no title; filing and asking enforce it below.
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--kind", choices=["bug", "feature", "other"], default="bug")
    ap.add_argument("--severity", choices=["low", "medium", "high", "critical"], default="medium")
    ap.add_argument("--no-logs", action="store_true", help="Omit the diagnostic log excerpt.")
    ap.add_argument(
        "--auto",
        action="store_true",
        help="Agent-initiated automatic report: honors the owner's auto-report "
        "setting and is deduped/rate-limited. Exits 3 (SKIPPED) when gated.",
    )
    ap.add_argument("--ask", action="store_true",
                    help="Park the report as a draft and ask with a File / File without logs / Skip card instead of filing.")
    ap.add_argument("--apply", action="store_true",
                    help="Register parked drafts that have no card yet and run the choices the owner clicked.")
    ap.add_argument("--decide", nargs=2, metavar=("DRAFT_ID", "CHOICE"),
                    help="Apply a choice to a parked draft by hand: file | file_no_logs | skip.")
    ap.add_argument("--drafts", action="store_true", help="List parked drafts as JSON and exit.")
    a = ap.parse_args()

    ws = resolve_workspace()
    prefs = read_prefs(ws)

    device = platform.node().split(".")[0]
    if a.drafts:
        print(json.dumps(list_drafts(ws), indent=2))
        return
    if a.apply:
        out = apply_clicks(ws, prefs, device)
        print("APPLIED: " + json.dumps(out))
        return
    if a.decide:
        rc = decide(ws, prefs, a.decide[0], a.decide[1])
        if rc == 0:
            manager = hitl_manager(ws)
            req = requirement_for_draft(manager, a.decide[0])
            if req is not None:
                manager.resolve(req.id)  # the card follows the hand-made decision
        if rc:
            sys.exit(rc)
        return

    if not a.title.strip():
        print("ERROR: --title is required (a short one-line summary).")
        sys.exit(1)
    if a.auto or a.ask:
        if not prefs["autoReport"]:
            print("SKIPPED: automatic bug reports are disabled (Settings → Bug reports).")
            sys.exit(3)
        ok, reason = check_auto_gate(ws, a.title)
        if not ok:
            print(f"SKIPPED: {reason}.")
            sys.exit(3)

    if a.ask or (a.auto and prefs["askFirst"]):
        draft = {"kind": a.kind, "severity": a.severity, "title": a.title.strip(),
                 "body": a.body.strip() or a.title.strip(), "auto": bool(a.auto), "no_logs": bool(a.no_logs)}
        draft_id = write_draft(ws, draft)
        # The ask is the throttled event: a card is the more intrusive channel, so it
        # counts toward the dedupe window and the daily cap whether or not it is later filed.
        record_auto_report(ws, draft["title"])
        try:
            req_id = register_ask(ws, draft_id, draft["title"], device)
        except Exception as e:  # noqa: BLE001
            print(f"PARKED: draft {draft_id} kept, card not registered yet ({e}); --apply retries it.")
            sys.exit(3)
        print(f"ASKED: draft {draft_id} parked as {req_id}; the bridge delivers the card. After the owner answers, run --apply.")
        return

    base, token = read_cloud_auth(ws)
    if not token:
        print("NOT_SIGNED_IN: not signed in to Sutando Cloud — ask the user to sign in (Settings → Sutando Cloud), then retry.")
        sys.exit(2)

    ctx: dict = {"source": "core-agent", "platform": platform.platform(), "python": platform.python_version()}
    if a.auto:
        ctx["auto"] = True
    if not a.no_logs and prefs["sendLogs"]:
        excerpt, names = logs_excerpt(ws)
        if excerpt:
            ctx["last_logs_excerpt"] = excerpt
            ctx["log_files"] = names
        else:
            # sendLogs is opt-in, so reaching here means logs were asked for:
            # say why they are absent rather than shipping silence.
            ctx["logs_omitted"] = why_no_logs(ws)
    elif not prefs["sendLogs"]:
        # Without this the payload carries NEITHER key and triage cannot tell a
        # standing opt-out from logs that were wanted and missing. Carries no data.
        ctx["logs_opted_out"] = True

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
    try:
        status = post_feedback(f"{base.rstrip('/')}/api/feedback", payload, token)
        if a.auto:
            record_auto_report(ws, a.title)
        print(f"OK: filed {a.kind} report ({status}).")
    except urllib.error.HTTPError as e:
        print(f"ERROR: feedback API {e.code}: {e.read().decode(errors='replace')[:300]}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
