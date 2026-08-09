#!/usr/bin/env python3
"""media_frame envelope — encode/decode roundtrip + robustness.

The transport routes binary media frames by stream_id without interpreting the
payload; this pins the 4-byte envelope so the framing stays stable and a
malformed frame is a clean error (dropped), never a crash.

Run: python3 tests/runtime-api-media-frame.test.py   (stdlib only)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "runtime-api"))
import media_frame as mf  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    # roundtrip across representative payloads
    for stype, sid, payload in [
            (mf.STREAM_AUDIO, 1, b"\x00\x01\x02pcm"),
            (mf.STREAM_AUDIO, 0xFFFF, b""),
            (mf.STREAM_TERMINAL, 258, bytes(range(256)))]:
        st, si, pl = mf.decode(mf.encode(stype, sid, payload))
        check(st == stype and si == sid and pl == payload,
              f"roundtrip stream_type={stype} stream_id={sid} len={len(payload)}")

    # header length + big-endian stream_id
    frame = mf.encode(mf.STREAM_AUDIO, 0x0102, b"x")
    check(len(frame) == mf.HEADER_LEN + 1 and frame[2] == 0x01 and frame[3] == 0x02,
          "header is 4 bytes with big-endian stream_id")

    # short frame → clean error, not a crash
    try:
        mf.decode(b"\x01\x01")
        check(False, "short frame should raise")
    except mf.MediaFrameError:
        check(True, "short frame raises MediaFrameError")

    # wrong version → clean error
    try:
        mf.decode(b"\x99\x01\x00\x01payload")
        check(False, "wrong version should raise")
    except mf.MediaFrameError:
        check(True, "wrong version raises MediaFrameError")

    # out-of-range guards
    for bad in [(256, 1), (1, 0x10000)]:
        try:
            mf.encode(bad[0], bad[1], b"")
            check(False, f"encode {bad} should raise")
        except mf.MediaFrameError:
            check(True, f"encode rejects out-of-range {bad}")

    print(f"\n{'PASS — media_frame green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
