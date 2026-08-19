#!/usr/bin/env python3
"""Task-envelope authentication: an HMAC stamp that makes access_tier a
verified claim instead of an honor-system header.

Threat model (attack class 2 of the 2026-08-17 mailbox-security design):
today the consumer grants owner-tier processing on the header's word (the
2026-07-11 self-written-task incident was the benign instance). The stamp
is INTEGRITY TELEMETRY, not an authorization boundary: a writer holding
the per-host key MACs the WHOLE file (headers + body), so a forger who
can reach the drop directory but NOT read the key (cross-host/synced
writers, processes with a narrow write grant, network-delivered files)
cannot mint a `verified` owner-tier task, and tampering with a stamped
one (tier flip, body swap) is detected.

Deliberately NOT covered in this phase, per the recorded design's phasing:
- a same-user attacker with key-read access — same uid can read the 0600
  key (or pre-create it) and mint valid envelopes; making the tier
  authoritative against that attacker requires moving key possession
  behind a separately enforced principal (Keychain-ACL/XPC phase); the
  S2 policy seam stays the second line regardless;
- freshness/replay of a verbatim old stamped file (nonce/expiry are the
  envelope v2 fields).

Rollout is SOAK-FIRST: `verify_text` reports `unsigned` for legacy files;
consumers must treat that as a warning (log/telemetry), not a rejection,
until every live writer stamps. The verdict for a PRESENT-but-wrong stamp
is `invalid` and is always actionable.

Key: `<workspace>/state/auth/task-hmac.key` (0600, auto-generated) —
per-host durable install state, exempt from transient-state cleanup.

CLI: `task_envelope.py stamp <file>` (in place) | `verify <file>`
(exit 0 verified / 3 unsigned / 4 invalid / 5 unverifiable-no-key).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

STAMP_PREFIX = "envelope_hmac: v1:"
_KEY_RELPATH = Path("state") / "auth" / "task-hmac.key"


def key_path(workspace: Path | None = None) -> Path:
    return (workspace or resolve_workspace()) / _KEY_RELPATH


def _parse_key(text: str, p: Path) -> bytes:
    """Exactly 64 hex chars / 32 bytes, or ValueError. bytes.fromhex('')
    returns b'' without raising, so an EMPTY key file otherwise stamps and
    verifies under a zero-length key — mirrored guard in task_envelope.ts."""
    key = bytes.fromhex(text)
    if len(text) != 64 or len(key) != 32:
        raise ValueError(
            f"invalid task-hmac key at {p} (want 64 hex chars, got {len(text)})")
    return key


def load_key(workspace: Path | None = None) -> "bytes | None":
    """Read-only: None when no key exists. Verification MUST use this —
    minting a key from a verify path turns a fresh/restored host's first
    verification of a good file into a false `invalid` (review finding).
    A PRESENT-but-corrupt key raises ValueError (verify_text maps it to
    'unverifiable' — the key is at fault, not the file being judged)."""
    p = key_path(workspace)
    try:
        return _parse_key(p.read_text(encoding="utf-8").strip(), p)
    except FileNotFoundError:
        return None


def load_or_create_key(workspace: Path | None = None) -> bytes:
    p = key_path(workspace)
    try:
        return _parse_key(p.read_text(encoding="utf-8").strip(), p)
    except FileNotFoundError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    # 0600 at open: write_text-then-chmod leaves a umask-default (0644)
    # window in which any local process can read the durable key.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_hex(32))
    # First writer wins across concurrent bridges: link() refuses to clobber.
    try:
        os.link(tmp, p)
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return _parse_key(p.read_text(encoding="utf-8").strip(), p)


def _strip_stamp(text: str) -> tuple[str, str | None]:
    """Recognize a stamp ONLY in its canonical header slot (line 0, or line 1
    after `id:`). A stamp-shaped line anywhere else is user CONTENT and must
    survive byte-identically — deleting it would authenticate altered bytes."""
    lines = text.split("\n")
    for i in (0, 1):
        if i < len(lines) and lines[i].startswith(STAMP_PREFIX) and \
                (i == 0 or lines[0].startswith("id:")):
            mac = lines[i][len(STAMP_PREFIX):].strip()
            return "\n".join(lines[:i] + lines[i + 1:]), mac
    return text, None


def _mac(text_without_stamp: str, key: bytes) -> str:
    return hmac.new(key, text_without_stamp.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def stamp_text(text: str, workspace: Path | None = None) -> str:
    """Return the task text with a fresh stamp line inserted after the
    first line (the `id:` header in every live writer shape — before the
    `task:` line, so task-last consumers see it as a real header). Any
    pre-existing stamp is replaced, never doubled."""
    body, _old = _strip_stamp(text)
    key = load_or_create_key(workspace)
    lines = body.split("\n")
    at = 1 if lines and lines[0].startswith("id:") else 0
    lines.insert(at, STAMP_PREFIX + _mac(body, key))
    return "\n".join(lines)


def verify_text(text: str, workspace: Path | None = None) -> dict:
    """Verdicts: 'verified' | 'unsigned' | 'invalid' | 'unverifiable'
    (keyless host — cannot judge). Soak-first: only 'verified' is proof.
    'invalid' means content changed under an in-place stamp; an edit that
    DISPLACES the stamp out of its canonical slot reads 'unsigned', so
    tamper is not always loud — enforcement must fail closed on
    'unsigned'/'unverifiable', not treat 'invalid' as the only bad case."""
    stripped, mac = _strip_stamp(text)
    if mac is None:
        return {"verdict": "unsigned", "reason": "no stamp line"}
    try:
        key = load_key(workspace)
    except ValueError:
        return {"verdict": "unverifiable",
                "reason": "corrupt local key — cannot judge; treat as warn"}
    if key is None:
        return {"verdict": "unverifiable",
                "reason": "no local key — cannot judge; treat as warn"}
    want = _mac(stripped, key)
    if hmac.compare_digest(mac, want):
        return {"verdict": "verified", "reason": ""}
    return {"verdict": "invalid",
            "reason": "stamp does not match file content"}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("stamp", "verify"):
        print("usage: task_envelope.py stamp|verify <file>", file=sys.stderr)
        return 2
    p = Path(argv[2])
    text = p.read_text(encoding="utf-8")
    if argv[1] == "stamp":
        p.write_text(stamp_text(text), encoding="utf-8")
        print("stamped")
        return 0
    v = verify_text(text)
    print(f"{v['verdict']}" + (f": {v['reason']}" if v["reason"] else ""))
    return {"verified": 0, "unsigned": 3, "invalid": 4,
            "unverifiable": 5}[v["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
