"""The desktop app's switch-request files in an agent's own login dir (CLAUDE_CONFIG_DIR or CODEX_HOME), v2.

  request  `.ag2-switch-request.json` {"v": 2, "key", "nonce", "at"}, 0600, created exclusively (a temp file linked in).
  claimed  the app renames the request to `.ag2-switch-claimed-<nonce>.json` while it switches.
  consumer `.ag2-switch-consumer.json` {"v": 2, "runtimes": [...]}: the app writes it once it watches this dir;
           a pick is offered only for a runtime it lists.
  result   then writes `.ag2-switch-result-<nonce>.json` {"v": 2, "nonce", "ok", "label" | "error", "verified",
           "unknown"} and removes the claimed file.

Shared by every adapter whose agent the app switches from the owner's phone (claude_code's account_remote, codex).
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .bounded_file_read import read_regular

REQUEST = ".ag2-switch-request.json"
CLAIMED = ".ag2-switch-claimed-%s.json"
RESULT = ".ag2-switch-result-%s.json"
FILE_MAX = 16 << 10
VERSION = 2
CONSUMER = ".ag2-switch-consumer.json"
NONCE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def request_nonce(config_dir):
    """The nonce the request file holds; '' for one that cannot be read; None for no request file."""
    text, why = read_regular(str(Path(config_dir) / REQUEST), FILE_MAX, follow=False)
    if text is None:
        return "" if why else None
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    return str(data.get("nonce") or "") if isinstance(data, dict) else ""


def claimed(config_dir, nonce: str) -> bool:
    return os.path.lexists(str(Path(config_dir) / (CLAIMED % nonce)))


def claim_age(config_dir, nonce: str, now=None):
    """Seconds since the claim of `nonce` last changed; None when there is none."""
    try:
        at = os.lstat(str(Path(config_dir) / (CLAIMED % nonce))).st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - at


def claims(config_dir) -> list:
    """The nonces of the claims the app holds (each stays until the app itself answers it)."""
    try:
        names = os.listdir(str(config_dir))
    except OSError:
        return []
    return [n[len(".ag2-switch-claimed-"):-len(".json")] for n in names
            if n.startswith(".ag2-switch-claimed-") and n.endswith(".json")]


def claimed_any(config_dir) -> bool:
    return bool(claims(config_dir))


def _parsed(text, nonce: str) -> dict:
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("nonce") != nonce or not usable(data):
        return {"nonce": nonce, "unreadable": True}      # not an answer: how the switch ended is not known
    return {k: data.get(k) for k in ("nonce", "ok", "label", "error", "unknown", "verified") if k in data}


def usable(data: dict) -> bool:
    """Whether a receipt's answer has the contract's shape: `unknown`, when there, a bool, and true is an answer
    by itself; else `ok` a bool, a success naming its account (`label`, a string), `verified` when there a bool.
    A success without `verified` (an older app) is usable, and shown as not yet tried."""
    if "unknown" in data and not isinstance(data["unknown"], bool):
        return False
    if data.get("unknown") is True:
        return True
    if not isinstance(data.get("ok"), bool):
        return False
    if "verified" in data and not isinstance(data["verified"], bool):
        return False
    return not data["ok"] or (isinstance(data.get("label"), str) and bool(data["label"].strip()))


def write_request(config_dir, key: str, nonce: str, now: float) -> None:
    """The request, whole and owner-only, linked in only where there is none (two writers cannot both win)."""
    path = Path(config_dir) / REQUEST
    tmp = path.with_name("%s.%s.tmp" % (REQUEST, nonce))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"v": VERSION, "key": key, "nonce": nonce, "at": int(now)}))
            f.flush()
            os.fsync(f.fileno())
        os.link(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def withdraw(config_dir, nonce: str) -> bool:
    """Take back the request while it is still this nonce's and unclaimed: True when it was taken back."""
    path = Path(config_dir) / REQUEST
    aside = path.with_name("%s.withdrawn-%s" % (REQUEST, nonce))
    try:
        os.rename(str(path), str(aside))
    except OSError:
        return False        # the app claimed it meanwhile
    try:
        text, _ = read_regular(str(aside), FILE_MAX, follow=False)
        try:
            mine = isinstance(json.loads(text or ""), dict) and json.loads(text).get("nonce") == nonce
        except ValueError:
            mine = False
        if not mine:
            try:
                os.link(str(aside), str(path))      # not ours: put back, unless a newer one is there
            except OSError:
                pass
        return mine
    finally:
        aside.unlink(missing_ok=True)


def one_line(text, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def consumer_takes(config_dir, runtime: str) -> bool:
    """Whether the app says it watches `config_dir` for requests of `runtime` (its consumer marker, v2)."""
    text, _ = read_regular(str(Path(config_dir) / CONSUMER), FILE_MAX, follow=False)
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return False
    runtimes = data.get("runtimes") if isinstance(data, dict) and data.get("v") == VERSION else None
    return isinstance(runtimes, list) and runtime in runtimes


def retire_claims(config_dir, older_than: float, now=None) -> int:
    """Remove the claims the app left unanswered for `older_than` seconds (a switch it never finished), so they no
    longer hold every later request back; how many."""
    gone = 0
    for nonce in claims(config_dir):
        age = claim_age(config_dir, nonce, now)
        if age is not None and age >= older_than:
            try:
                os.unlink(str(Path(config_dir) / (CLAIMED % nonce)))
                gone += 1
            except OSError:
                pass
    return gone
