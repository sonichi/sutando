#!/usr/bin/env python3
"""Wire format for skills/room-collab: the parts that fail silently.

A varint off by one, or a room id quoted wrong, raises nothing — it produces a
frame the server discards and a document that never syncs. The module under
test imports no third-party package, so this runs wherever CI runs.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "room-collab"))

from room_collab_protocol import (  # noqa: E402
    CLOSE_BAD_KIND, CLOSE_BAD_ROOM, CLOSE_FORBIDDEN, RECONNECT_CODES, RECONNECT_STATUSES,
    RoomDocError, close_reason, doc_socket_url, explain, http_status, is_transient,
    read_var_bytes, read_var_uint, write_var_bytes, write_var_uint,
)


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _HttpError(Exception):
    def __init__(self, status, body=b""):
        self.response = _Resp(status)
        self.response.body = body


class _Closed(Exception):
    def __init__(self, code):
        self.code = code

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def test_var_uint_round_trips():
    for n in (0, 1, 127, 128, 255, 256, 16383, 16384, 1 << 20, 1 << 31):
        encoded = write_var_uint(n)
        assert read_var_uint(encoded, 0) == (n, len(encoded)), f"round trip failed at {n}"


def test_var_uint_matches_the_wire_format():
    # Pinned against the known encoding, not our own decoder: a consistently
    # wrong scheme would round-trip its way to green.
    assert write_var_uint(0) == b"\x00"
    assert write_var_uint(1) == b"\x01"
    assert write_var_uint(127) == b"\x7f"
    assert write_var_uint(128) == b"\x80\x01"
    assert write_var_uint(300) == b"\xac\x02"


def test_var_uint_refuses_a_negative():
    raises(ValueError, lambda: write_var_uint(-1))


def test_var_bytes_round_trip_and_offset():
    payload = b"\x00\xffhello"
    buf = b"\x09" + write_var_bytes(payload) + b"trailing"
    got, offset = read_var_bytes(buf, 1)
    assert got == payload
    assert buf[offset:] == b"trailing", "the offset must land past the payload"


def test_truncated_frames_raise_rather_than_short_read():
    raises(RoomDocError, lambda: read_var_uint(b"\x80", 0))       # continuation, no next byte
    raises(RoomDocError, lambda: read_var_bytes(b"\x05abc", 0))   # claims 5, carries 3


def test_url_quotes_the_whole_room_id():
    url = doc_socket_url("https://chat.ag2.space", "!abc:ag2.space")
    assert url == "wss://chat.ag2.space/api/v1/room-collab/%21abc%3Aag2.space/ws", url
    assert "!" not in url and ":ag2" not in url, "a bare ! or : would split the path"


def test_url_upgrades_the_scheme_and_does_not_double_the_prefix():
    assert doc_socket_url("http://localhost:9996", "!r:s").startswith("ws://")
    assert doc_socket_url("https://h", "!r:s").startswith("wss://")
    assert doc_socket_url("wss://h/api/v1/room-collab", "!r:s").count("/api/v1/room-collab") == 1
    # An explicit old path is kept as given for the alias window, never doubled.
    old = doc_socket_url("wss://h/api/v1/room-doc", "!r:s")
    assert old.startswith("wss://h/api/v1/room-doc/") and "room-collab" not in old, old


def test_url_refuses_what_it_cannot_derive_a_socket_from():
    for bad in ("", "chat.ag2.space", "ftp://h"):
        raises(RoomDocError, lambda b=bad: doc_socket_url(b, "!r:s"))


def test_each_rung_names_a_different_owner():
    """401, 403 and an edge 403 look alike from outside and are fixed by three
    different people. Three agents chased the wrong one tonight; the text must
    make the ladder visible, not just echo the number."""
    unknown = explain(_HttpError(401, b"bearer is not a valid Matrix user session"), "wss://h/ws")
    assert "401" in unknown and "provisioning" in unknown, unknown
    assert "bearer is not a valid Matrix user session" in unknown, "the service's own words survive"
    assert "register" in unknown, "it says who fixes it"

    room = explain(_HttpError(403, b'{"code":"forbidden"}'), "wss://h/ws")
    assert "403" in room and "room admin" in room, room
    assert "doc.write" not in room and "Matrix access token" not in room, \
        "the retired credential model must not be re-taught"

    edge = explain(_HttpError(403, b"error code: 1010"), "wss://h/ws")
    assert "Cloudflare" in edge and "never saw" in edge, edge
    assert "room admin" not in edge, "an edge refusal is not a room answer"


def test_a_404_explains_that_non_members_are_hidden():
    text = explain(_HttpError(404), "wss://h/ws")
    assert "404" in text and "member" in text, text


def test_a_426_says_to_use_the_client():
    assert "websocket only" in explain(_HttpError(426), "https://h/authz")


def test_an_unknown_error_still_says_something_usable():
    other = explain(ValueError("boom"), "wss://h/ws")
    assert "boom" in other and "wss://h/ws" in other


def test_close_codes_are_translated_not_echoed():
    bad_room = close_reason(_Closed(CLOSE_BAD_ROOM))
    assert "malformed" in bad_room, bad_room
    assert "empty" in bad_room, "it must deny the empty-document reading explicitly"
    assert "malformed" in close_reason(_Closed(CLOSE_BAD_KIND))
    forbidden = close_reason(_Closed(CLOSE_FORBIDDEN))
    assert "unreachable" in forbidden, "one 4403 is not proof of revocation"


def test_an_unknown_close_and_a_bare_close_are_distinguished():
    assert "4999" in close_reason(_Closed(4999))
    assert close_reason(None), "a close with no exception still reads as something"


def test_a_rollout_handshake_is_transient_and_a_refusal_is_not():
    # A watcher died on a 502 at handshake during a real deploy: the close codes
    # were retried, a refused open was not. Both are "the service went away".
    for status in sorted(RECONNECT_STATUSES):
        err = RoomDocError(explain(_HttpError(status), "wss://h/ws"))
        err.status = http_status(_HttpError(status))
        assert err.status == status and is_transient(err), status
        assert "not being served right now" in str(err) and "retries" in str(err), str(err)
    for status in (401, 403, 404, 426):
        err = RoomDocError("refused")
        err.status = status
        assert not is_transient(err), status
    for code in sorted(RECONNECT_CODES):
        err = RoomDocError("closed")
        err.code = code
        assert is_transient(err), code
    refused = RoomDocError("refused")
    refused.code = CLOSE_FORBIDDEN
    assert not is_transient(refused)
    assert not is_transient(RoomDocError("no code, no status")) and not is_transient(None)
    assert http_status(ValueError("no response")) is None


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab protocol: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab protocol: ok")
