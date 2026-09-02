#!/usr/bin/env python3
"""src/policy/signal_tokens.py — the per-room token registry, read side.

Covers: verify() maps a plaintext to its {room_id, scope} through sha256(salt +
token); a revoked row never matches; rotation (new row, then old revoked) hands
over without a gap; the file is re-read on change without restart; the three
registry states — `unprovisioned` (no file, or no row ever), `provisioned` (any
row, live or revoked: all-revoked stays provisioned) and `invalid` (unreadable,
wrong version, ANY malformed row — every contract field is validated) — and that
an invalid file verifies nothing; the writer helper produces a 0600 file.

Run: python3 tests/signal-tokens.test.py
"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from policy import signal_tokens as st  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


ws = Path(tempfile.mkdtemp(prefix="signal-tokens-"))
path = st.registry_path(ws)
check("registry path is <workspace>/state/signal-room-tokens.json",
      path == ws / "state" / "signal-room-tokens.json")

reg = st.TokenRegistry(path)
check("missing file: unprovisioned", reg.state() == st.STATE_UNPROVISIONED)
check("missing file: nothing verifies", reg.verify("anything") is None)
st.write_registry(path, [])
check("valid document with no row ever: still unprovisioned", reg.state() == st.STATE_UNPROVISIONED)

a_enq = st.make_row("!a:hs", "enqueue", "tok-a-enqueue", created_at_ms=1, salt="00" * 8)
a_read = st.make_row("!a:hs", "read", "tok-a-read", created_at_ms=2)
b_enq = st.make_row("!b:hs", "enqueue", "tok-b-enqueue", created_at_ms=3)
st.write_registry(path, [a_enq, a_read, b_enq])

mode = stat.S_IMODE(os.stat(path).st_mode)
check("registry file is 0600", mode == 0o600, oct(mode))
raw = json.loads(path.read_text())
check("file shape {v:1, tokens:[...]}", raw.get("v") == 1 and len(raw["tokens"]) == 3)
check("rows carry the contract fields",
      set(raw["tokens"][0]) == {"room_id", "scope", "salt", "sha256", "created_at", "revoked_at"})
check("sha256 is of salt + token (hex)",
      a_enq["sha256"] == st.token_digest("00" * 8, "tok-a-enqueue")
      and len(a_enq["salt"]) == 16)
check("plaintext never stored", "tok-a-enqueue" not in path.read_text())

check("enqueue token -> its room + scope",
      reg.verify("tok-a-enqueue") == {"room_id": "!a:hs", "scope": "enqueue"})
check("read token -> its room + scope",
      reg.verify("tok-a-read") == {"room_id": "!a:hs", "scope": "read"})
check("other room's token -> its own room", reg.verify("tok-b-enqueue")["room_id"] == "!b:hs")
check("unknown token -> None", reg.verify("tok-nope") is None)
check("empty token -> None", reg.verify("") is None)
check("non-string token -> None", reg.verify(None) is None)
check("provisioned once a row exists", reg.state() == st.STATE_PROVISIONED)

# Revocation: set revoked_at; the row stops matching immediately (mtime reload).
a_enq_revoked = dict(a_enq, revoked_at=1000)
st.write_registry(path, [a_enq_revoked, a_read, b_enq])
check("revoked row no longer verifies", reg.verify("tok-a-enqueue") is None)
check("sibling rows unaffected by a revocation", reg.verify("tok-a-read") is not None)

# Rotation: new row FIRST, old row revoked after — no gap.
a_enq2 = st.make_row("!a:hs", "enqueue", "tok-a-enqueue-2", created_at_ms=4)
st.write_registry(path, [a_enq, a_enq2, a_read, b_enq])
check("rotation step 1: both old and new verify",
      reg.verify("tok-a-enqueue") is not None and reg.verify("tok-a-enqueue-2") is not None)
st.write_registry(path, [dict(a_enq, revoked_at=5), a_enq2, a_read, b_enq])
check("rotation step 2: only the new token verifies",
      reg.verify("tok-a-enqueue") is None
      and reg.verify("tok-a-enqueue-2") == {"room_id": "!a:hs", "scope": "enqueue"})

# All revoked -> STILL provisioned: the flip stays on, nothing verifies.
st.write_registry(path, [dict(r, revoked_at=9) for r in (a_enq2, a_read, b_enq)])
check("all rows revoked: no active rows", reg.active_rows() == [])
check("all rows revoked: registry stays provisioned", reg.state() == st.STATE_PROVISIONED)

print("== invalid: unreadable, wrong version, any malformed row ==")
path.write_text("{not json")
check("corrupt file: nothing verifies", reg.verify("tok-a-read") is None)
check("corrupt file: state invalid with a reason", reg.state() == st.STATE_INVALID
      and "unparseable" in reg.invalid_reason(), reg.invalid_reason())
path.write_text(json.dumps({"v": 2, "tokens": [a_read]}))
check("unknown version: invalid, rows ignored",
      reg.verify("tok-a-read") is None and reg.state() == st.STATE_INVALID)
path.write_text(json.dumps({"v": 1, "tokens": {"a": 1}}))
check("tokens not a list: invalid", reg.state() == st.STATE_INVALID)
path.write_text(json.dumps([a_read]))
check("document not an object: invalid", reg.state() == st.STATE_INVALID)
bad_rows = {
    "empty room_id": dict(a_read, room_id=""),
    "non-string room_id": dict(a_read, room_id=7),
    "unknown scope": dict(a_read, scope="admin"),
    "short salt": dict(a_read, salt="ab"),
    "uppercase salt": dict(a_read, salt="AB" * 8),
    "short sha256": dict(a_read, sha256="cd"),
    "uppercase sha256": dict(a_read, sha256=a_read["sha256"].upper()),
    "string created_at": dict(a_read, created_at="1"),
    "bool created_at": dict(a_read, created_at=True),
    "string revoked_at": dict(a_read, revoked_at="9"),
    "bool revoked_at": dict(a_read, revoked_at=False),
    "non-dict row": "junk",
}
for label, bad in bad_rows.items():
    path.write_text(json.dumps({"v": 1, "tokens": [b_enq, bad]}))
    check(f"{label}: whole file invalid, the sibling row verifies nothing",
          reg.state() == st.STATE_INVALID and reg.verify("tok-b-enqueue") is None
          and "row 1" in reg.invalid_reason(), reg.invalid_reason())
check("invalid reason never carries token material",
      "tok-" not in reg.invalid_reason() and b_enq["sha256"] not in reg.invalid_reason())
if os.geteuid() != 0:
    st.write_registry(path, [a_read])
    os.chmod(path, 0)
    check("unreadable file: invalid", reg.state() == st.STATE_INVALID, reg.invalid_reason())
    os.chmod(path, 0o600)
st.write_registry(path, [a_read])
check("a rewritten valid file recovers without restart",
      reg.state() == st.STATE_PROVISIONED and reg.verify("tok-a-read") is not None)
path.unlink()
check("file removed again: back to unprovisioned", reg.state() == st.STATE_UNPROVISIONED)

try:
    st.make_row("!a:hs", "admin", "t", created_at_ms=1)
    check("make_row rejects an unknown scope", False)
except ValueError:
    check("make_row rejects an unknown scope", True)

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — signal token registry")
