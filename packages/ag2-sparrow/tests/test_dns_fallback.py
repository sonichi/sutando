"""Resolver fallback — when the OS resolver returns a negative answer for the
relay host but the configured nameserver still resolves it, the bridge asks
that nameserver directly and connects by IP (hostname kept for SNI/Host).

Regression guard for the 2026-09-02 owner outage: macOS mDNSResponder held a
stuck negative entry for chat.ag2.space for ~32 min while `dig @<resolver>`
answered the whole time; the bridge log shows the same shape on 8 of the
previous 14 days. No root flush should be needed for the bridge to recover.

Plain-script convention (run as `python3 test_dns_fallback.py`), matching the
other ag2-sparrow tests — no pytest.
"""
import importlib
import os
import pathlib
import socket
import struct
import sys
import tempfile
import threading
import time


def _load():
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    mod = importlib.reload(mod)
    mod._fallback_cache.clear()
    return mod


def _swap(mod, **attrs):
    saved = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    return saved


def _restore(mod, saved):
    for k, v in saved.items():
        setattr(mod, k, v)


def _response(qid, host, ips, *, rcode=0, cname=None, qr=True):
    """Hand-built DNS response: question, optional CNAME, then A records that
    use a compression pointer back to the question name."""
    def name(h):
        out = b""
        for label in h.split("."):
            out += bytes([len(label)]) + label.encode()
        return out + b"\x00"
    ancount = len(ips) + (1 if cname else 0)
    r = struct.pack("!HHHHHH", qid, (0x8180 if qr else 0x0100) | rcode, 1, ancount, 0, 0)
    r += name(host) + struct.pack("!HH", 1, 1)
    ptr = b"\xc0\x0c"  # pointer to offset 12 = the question name
    if cname:
        rdata = name(cname)
        r += ptr + struct.pack("!HHIH", 5, 1, 60, len(rdata)) + rdata
    for ip in ips:
        r += ptr + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(ip)
    return r


def _gaierror(*a, **k):
    raise socket.gaierror(8, "nodename nor servname provided, or not known")


def _resolver_that_fails_names_only(host, *args, **kwargs):
    """Stand-in for the OS resolver during the outage: names fail, literals work."""
    try:
        socket.inet_pton(socket.AF_INET, host)
    except OSError:
        _gaierror()
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, args[0] if args else 0))]


def test_parse_a_records_follows_cname_and_compression():
    mod = _load()
    data = _response(0x1234, "chat.ag2.space", ["104.26.15.112", "172.67.72.17"],
                     cname="chat.ag2.space.cdn.example")
    assert mod._parse_a_records(data, 0x1234) == ["104.26.15.112", "172.67.72.17"]
    print("PASS test_parse_a_records_follows_cname_and_compression")


def test_parse_rejects_id_mismatch_error_rcode_and_junk():
    mod = _load()
    good = _response(7, "chat.ag2.space", ["1.2.3.4"])
    assert mod._parse_a_records(good, 8) == [], "id mismatch must not yield IPs"
    nx = _response(7, "chat.ag2.space", ["1.2.3.4"], rcode=3)
    assert mod._parse_a_records(nx, 7) == [], "NXDOMAIN must not yield IPs"
    assert mod._parse_a_records(b"\x00\x07", 7) == []
    assert mod._parse_a_records(good[:20], 7) == [], "truncated answer must not raise"
    query_echo = _response(7, "chat.ag2.space", ["1.2.3.4"], qr=False)
    assert mod._parse_a_records(query_echo, 7) == [], "a query (QR=0) with a matching id is not a response"
    other = _response(7, "evil.example", ["6.6.6.6"])
    assert mod._parse_a_records(other, 7, mod._encode_qname("chat.ag2.space")) == [], \
        "an answer for a different name must not be accepted for the asked one"
    assert mod._parse_a_records(good, 7, mod._encode_qname("CHAT.ag2.space")) == ["1.2.3.4"], "QNAME match is case-insensitive"
    print("PASS test_parse_rejects_id_mismatch_error_rcode_and_junk")


def test_fallback_used_when_system_resolver_fails():
    mod = _load()
    saved = _swap(mod, _orig_getaddrinfo=_resolver_that_fails_names_only,
                  _fallback_resolve=lambda host: ["104.26.15.112"])
    try:
        infos = mod._getaddrinfo_prefer_v4("chat.ag2.space", 443, 0, socket.SOCK_STREAM)
    finally:
        _restore(mod, saved)
    assert infos and infos[0][4] == ("104.26.15.112", 443), infos
    print("PASS test_fallback_used_when_system_resolver_fails")


def test_original_error_reraised_when_fallback_has_nothing():
    mod = _load()
    saved = _swap(mod, _orig_getaddrinfo=_resolver_that_fails_names_only,
                  _fallback_resolve=lambda host: [])
    try:
        try:
            mod._getaddrinfo_prefer_v4("chat.ag2.space", 443)
        except socket.gaierror as e:
            assert e.args[0] == 8, e
        else:
            raise AssertionError("empty fallback must re-raise the resolver error")
    finally:
        _restore(mod, saved)
    print("PASS test_original_error_reraised_when_fallback_has_nothing")


def test_fallback_never_queried_for_ip_literals_or_on_success():
    mod = _load()
    calls = []
    saved = _swap(mod, _orig_getaddrinfo=_resolver_that_fails_names_only,
                  _fallback_resolve=lambda host: calls.append(host) or [])
    try:
        try:
            mod._getaddrinfo_prefer_v4("::1", 443)
        except socket.gaierror:
            pass
        assert calls == [], f"literal host must not hit the fallback: {calls}"
        ok = mod._getaddrinfo_prefer_v4("104.26.15.112", 443)
        assert ok[0][4][0] == "104.26.15.112" and calls == []
    finally:
        _restore(mod, saved)
    print("PASS test_fallback_never_queried_for_ip_literals_or_on_success")


def test_fallback_exception_does_not_mask_the_resolver_error():
    mod = _load()

    def boom(host):
        raise RuntimeError("parser bug")

    saved = _swap(mod, _orig_getaddrinfo=_resolver_that_fails_names_only, _fallback_resolve=boom)
    try:
        try:
            mod._getaddrinfo_prefer_v4("chat.ag2.space", 443)
        except socket.gaierror:
            pass
        else:
            raise AssertionError("expected the original gaierror")
    finally:
        _restore(mod, saved)
    print("PASS test_fallback_exception_does_not_mask_the_resolver_error")


def test_nameservers_parsed_from_resolv_conf_v4_only_in_order():
    mod = _load()
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
        fh.write("# comment\nsearch example.net\nnameserver 100.100.100.100\n"
                 "nameserver fd7a:115c:a1e0::53\nnameserver 10.0.200.1\n")
        path = fh.name
    try:
        assert mod._system_nameservers(path) == ["100.100.100.100", "10.0.200.1"]
        assert mod._system_nameservers(path + ".missing") == []
    finally:
        os.unlink(path)
    print("PASS test_nameservers_parsed_from_resolv_conf_v4_only_in_order")


def test_live_udp_query_against_a_local_nameserver():
    """The real socket path: a fake nameserver on 127.0.0.1 answers the query id
    it receives with one A record."""
    mod = _load()
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(5)
    port = srv.getsockname()[1]

    def serve():
        data, addr = srv.recvfrom(512)
        qid = struct.unpack("!H", data[:2])[0]
        # Off-path first: a spoofed answer from a different source port must be dropped
        # by the connected socket, so the real answer below is the one that lands.
        rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rogue.sendto(_response(qid, "chat.ag2.space", ["6.6.6.6"]), addr)
        rogue.close()
        time.sleep(0.2)
        srv.sendto(_response(qid, "chat.ag2.space", ["104.26.15.112"]), addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        got = mod._dns_a_query("chat.ag2.space", "127.0.0.1", timeout=3, port=port)
    finally:
        t.join(5)
        srv.close()
    assert got == ["104.26.15.112"], f"off-path datagram was accepted: {got}"
    assert mod._dns_a_query("chat.ag2.space", "127.0.0.1", timeout=0.2, port=port) == [], \
        "a silent nameserver must yield [] within the timeout, never raise"
    print("PASS test_live_udp_query_against_a_local_nameserver")


def test_fallback_resolve_caches_for_ttl_and_tries_nameservers_in_order():
    mod = _load()
    queries = []

    def fake_query(host, ns, timeout=None, port=53):
        queries.append(ns)
        return ["9.9.9.9"] if ns == "10.0.200.1" else []

    saved = _swap(mod, _system_nameservers=lambda path=None: ["100.100.100.100", "10.0.200.1"],
                  _dns_a_query=fake_query, _log=lambda m: None, _FALLBACK_TTL_S=60.0)
    try:
        assert mod._fallback_resolve("chat.ag2.space") == ["9.9.9.9"]
        assert queries == ["100.100.100.100", "10.0.200.1"], queries
        assert mod._fallback_resolve("chat.ag2.space") == ["9.9.9.9"]
        assert len(queries) == 2, "second call within TTL must be served from cache"
        mod._fallback_cache["chat.ag2.space"] = (0.0, ["9.9.9.9"])  # expired
        mod._fallback_resolve("chat.ag2.space")
        assert len(queries) == 4, "an expired entry must re-query"
    finally:
        _restore(mod, saved)
        mod._fallback_cache.clear()
    print("PASS test_fallback_resolve_caches_for_ttl_and_tries_nameservers_in_order")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — report every failure
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("ALL PASS" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
