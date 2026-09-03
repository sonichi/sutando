#!/usr/bin/env python3
"""Tests for the room-ops collection — shared gate, the read/media/react modules,
and the unified room_ops CLI dispatcher. No network."""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

# Shadow the vault boundary before any gateway() call can reach it. gateway() now
# resolves the token from the Keychain when the env has none — and this file's
# EnvCase fixture clears every token var in setUp, so "no token in env" is the
# DEFAULT state for every test here. Without this shadow, any gateway() call under
# that fixture would read the operator's REAL macOS Keychain — the fixture-default
# analogue of #2646's import-time Keychain read (@john-the-dev / @qingyun-air). The
# real channel_token.token_from_vault policy stays under test; only the host
# boundary is replaced. Installed before `import _gateway` so no path can slip past.
_VAULT_STORE: dict = {}
_VAULT_CALLS: list = []


def _fake_get_vault_key(var):
    _VAULT_CALLS.append(var)
    if var in _VAULT_STORE:
        return _VAULT_STORE[var]
    raise KeyError(var)


_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = _fake_get_vault_key
sys.modules["vault_intercept"] = _FAKE_VI

import _gateway  # noqa: E402
import read as rd  # noqa: E402
import media as md  # noqa: E402
import react as rc  # noqa: E402
import join as jn  # noqa: E402
import doc as dc  # noqa: E402
import room_ops  # noqa: E402

HS = "@agent.a:hs"
ROOM = "!roomA:hs"
EV = "$evt1"
ENVK = ["GATEWAY_URL", "GATEWAY_TOKEN", "RELAY_URL", "REMOTE_TASK_URL", "RELAY_TOKEN", "REMOTE_TASK_TOKEN",
        "ROOM_MEDIA_ALLOW", "ROOM_MEDIA_INBOX", "ROOM_MEDIA_OUTBOX", "ROOM_OPS_GATE"]


def _clear():
    for k in ENVK:
        os.environ.pop(k, None)


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENVK}
        _clear()
        # setUp clears every token var, so an unshadowed gateway() would fall
        # through and read the operator's REAL channels/ag2space/.env.
        self._env_file_patch = mock.patch.object(
            _gateway, "_channel_env_file", lambda: None)
        self._env_file_patch.start()
        self.addCleanup(self._env_file_patch.stop)

    def tearDown(self):
        _clear()
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v


# ----- shared gate (_gateway) ----- #
class GateTests(unittest.TestCase):
    def test_none_defers(self):
        self.assertTrue(_gateway.gate_allows(HS, ROOM, None))

    def test_empty_denies(self):
        self.assertFalse(_gateway.gate_allows(HS, ROOM, {}))

    def test_explicit_room(self):
        self.assertTrue(_gateway.gate_allows(HS, ROOM, {HS: {"rooms": [ROOM]}}))
        self.assertFalse(_gateway.gate_allows(HS, "!x:hs", {HS: {"rooms": [ROOM]}}))

    def test_all_member(self):
        self.assertTrue(_gateway.gate_allows(HS, ROOM, {HS: {"all_member_rooms": True}}))

    def test_malformed(self):
        self.assertFalse(_gateway.gate_allows(HS, ROOM, {HS: 1}))

    def test_load_missing_none(self):
        self.assertIsNone(_gateway.load_gate("/nonexistent/g.json"))

    def test_degrade_reasons(self):
        self.assertIn("unimplemented", _gateway.degrade_reason(404))
        self.assertIn("not a joined member", _gateway.degrade_reason(403))
        self.assertIn("HTTP 500", _gateway.degrade_reason(500))


# The gateway() vault tier (sonichi#2638) is tested in
# tests/room_ops_gateway_vault.test.py — the coverage gate discovers only
# tests/*.test.py, so the _gateway.py changed lines are measured there. The
# shadow installed at the top of this file keeps every OTHER gateway()-calling
# test below off the real host Keychain.


# ----- read ----- #
class ReadTests(EnvCase):
    def test_no_room(self):
        self.assertFalse(rd.read_room("", HS)["ok"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied", rd.read_room(ROOM, HS, gate={})["reason"])

    def test_no_relay(self):
        self.assertEqual(rd.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})["reason"],
                         "no gateway configured")

    def test_404_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(rd, "http_request", side_effect=err):
            self.assertIn("unimplemented", rd.read_room(ROOM, HS, gate=None)["reason"])

    def test_limit_clamped(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rd, "http_request",
                               side_effect=lambda m, u, h: (cap.update(url=u), (200, b'{"messages":[]}', {}))[1]):
            rd.read_room(ROOM, HS, limit=9999, gate=None)
        self.assertIn(f"limit={rd.MAX_LIMIT}", cap["url"])

    def _raw_window_gateway(self, total_messages=14, noise_per_message=2):
        """A gateway whose `limit` bounds RAW EVENTS, of which only some are messages.

        This is what the live ag2.space gateway does. Reproduced here because the failure
        it causes is silent: fewer messages than asked for, ok:true, and no error.
        """
        import urllib.parse as _up

        def _http(_m, url, _h):
            raw = int(dict(_up.parse_qsl(_up.urlparse(url).query))["limit"])
            # Newest-first timeline: every message preceded by `noise` non-message events,
            # so the first `noise` slots of any window contain no messages at all.
            msgs, consumed = [], 0
            for i in range(total_messages):
                consumed += noise_per_message
                if consumed >= raw:
                    break
                consumed += 1
                msgs.append({"sender": "@a:hs", "ts": 1000 - i, "body": f"m{i}"})
                if consumed >= raw:
                    break
            return (200, json.dumps({"messages": msgs}).encode(), {})
        return _http

    def test_small_limit_does_not_report_an_empty_room(self):
        """limit=3 must not return zero messages from a room that has fourteen.

        REGRESSION (measured live 2026-08-05): the gateway's limit counts raw timeline
        events, so `--limit 3` returned `ok:true` with an EMPTY message list on a busy
        room. A cheap "did anyone reply?" probe is exactly what passes a small limit, and
        the false empty is indistinguishable from silence. Before the fix this asserts 0.
        """
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(rd, "http_request", side_effect=self._raw_window_gateway()):
            res = rd.read_room(ROOM, HS, limit=3, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["messages"]), 3, "a small limit must still yield messages")

    def test_limit_counts_messages_not_raw_events(self):
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(rd, "http_request", side_effect=self._raw_window_gateway()):
            res = rd.read_room(ROOM, HS, limit=10, gate=None)
        self.assertEqual(len(res["messages"]), 10)
        self.assertTrue(res["complete"])

    def test_short_room_under_claims_completeness(self):
        """Short of `limit` never claims complete — the client cannot tell why it is short.

        Updated per the #2678 review: a repeated message count across a wider window is
        NOT proof of exhausted history (the extra raw events may all be non-messages), and
        this endpoint returns only message-type items, so a short page is indistinguishable
        from a noisy one. Under-claiming is the safe direction here.
        """
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(rd, "http_request",
                               side_effect=self._raw_window_gateway(total_messages=2)):
            res = rd.read_room(ROOM, HS, limit=20, gate=None)
        self.assertEqual(len(res["messages"]), 2)
        self.assertFalse(res["complete"], "short of limit -> never claim complete")

    def test_success_parses(self):
        os.environ["RELAY_URL"] = "https://r"
        body = (200, json.dumps({"messages": [{"sender": "@a:hs", "ts": 1, "body": "hi"}]}).encode(), {})
        with mock.patch.object(rd, "http_request", return_value=body):
            res = rd.read_room(ROOM, HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"][0]["body"], "hi")


# ----- media ----- #
class MediaTests(EnvCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        os.environ["ROOM_MEDIA_ALLOW"] = self.tmp
        os.environ["ROOM_MEDIA_INBOX"] = self.tmp
        self.f = os.path.join(self.tmp, "ok.png")
        open(self.f, "wb").write(b"IMG")

    def test_fetch_404(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(md, "http_request", side_effect=err):
            self.assertIn("unimplemented", md.fetch_media("mxc://x/y", HS, ROOM, gate=None)["reason"])

    def test_fetch_success_writes(self):
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(md, "http_request", return_value=(200, b"PNG", {"X-Media-Filename": "p.png"})):
            res = md.fetch_media("mxc://x/y", HS, ROOM, gate=None)
        self.assertTrue(res["ok"] and os.path.isfile(res["path"]))
        self.assertEqual(open(res["path"], "rb").read(), b"PNG")

    def test_fetch_reads_bounded_and_rejects_oversize(self):
        # Regression for the OOM finding: fetch must (a) bound the read to
        # MAX_BYTES+1 and (b) reject an oversize body — never buffer the full
        # (possibly multi-GB) payload.
        os.environ["RELAY_URL"] = "https://r"
        cap = {}

        def fake(method, url, headers=None, data=None, max_bytes=None):
            cap["max_bytes"] = max_bytes
            # simulate a hostile gateway: return exactly the overflow sentinel size
            return 200, b"x" * (md.MAX_BYTES + 1), {}

        with mock.patch.object(md, "http_request", side_effect=fake):
            res = md.fetch_media("mxc://x/y", HS, ROOM, gate=None)
        self.assertEqual(cap["max_bytes"], md.MAX_BYTES)   # read was bounded
        self.assertFalse(res["ok"])
        self.assertIn("exceeds", res["reason"])            # oversize rejected

    def test_send_gate_denies_before_file_stat(self):
        # Gate must run before any filesystem stat — an unauthorized agent
        # shouldn't cause the skill to touch the path at all.
        with mock.patch.object(md.os.path, "isfile", side_effect=AssertionError("stat before gate")):
            res = md.send_media(ROOM, "/whatever.png", HS, gate={})  # empty gate -> deny
        self.assertFalse(res["ok"])
        self.assertIn("gate denied", res["reason"])

    def test_send_path_not_allowed(self):
        os.environ["ROOM_MEDIA_ALLOW"] = "/other"
        self.assertIn("not in ROOM_MEDIA_ALLOW", md.send_media(ROOM, self.f, HS, gate=None)["reason"])

    def test_send_oversize(self):
        big = os.path.join(self.tmp, "big.bin")
        open(big, "wb").write(b"x" * (md.MAX_BYTES + 1))
        self.assertIn("exceeds", md.send_media(ROOM, big, HS, gate=None)["reason"])

    def test_send_success(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}

        def fake(method, url, headers=None, data=None):
            cap["data"] = data
            return 200, b"{}", {}

        with mock.patch.object(md, "http_request", side_effect=fake):
            res = md.send_media(ROOM, self.f, HS, gate=None, caption="c")
        self.assertTrue(res["ok"])
        sent = json.loads(cap["data"])
        self.assertEqual(base64.b64decode(sent["content_b64"]), b"IMG")


# ----- react ----- #
class ReactTests(EnvCase):
    def test_missing_args(self):
        self.assertFalse(rc.react(ROOM, "", "👀", HS)["ok"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied", rc.react(ROOM, EV, "👀", HS, gate={})["reason"])

    def test_react_endpoint(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, {}))[1]):
            res = rc.react(ROOM, EV, "✅", HS, gate=None)
        self.assertTrue(res["ok"] and cap["url"].endswith("/react"))
        self.assertEqual(cap["payload"], {"event_id": EV, "key": "✅"})

    def test_unreact_endpoint(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u), (200, {}))[1]):
            rc.unreact(ROOM, EV, "👀", HS, gate=None)
        self.assertTrue(cap["url"].endswith("/unreact"))

    def test_403_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(rc, "http_json", side_effect=err):
            self.assertIn("not a joined member", rc.react(ROOM, EV, "👀", HS, gate=None)["reason"])


# ----- join ----- #
class JoinTests(EnvCase):
    def test_missing_room(self):
        self.assertFalse(jn.join_room("", HS)["ok"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied", jn.join_room(ROOM, HS, gate={})["reason"])

    def test_no_relay(self):
        self.assertIn("no gateway", jn.join_room(ROOM, HS, gate=None)["reason"])

    def test_join_endpoint(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(jn, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, {}))[1]):
            res = jn.join_room(ROOM, HS, gate=None)
        self.assertTrue(res["ok"] and cap["url"].endswith("/join"))
        self.assertEqual(cap["payload"], {})

    def test_403_degrades(self):
        # No standing invite for an invite-only room → the gateway 403 degrades
        # to the structured membership reason, not an exception.
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(jn, "http_json", side_effect=err):
            self.assertIn("not a joined member", jn.join_room(ROOM, HS, gate=None)["reason"])


class DocTests(EnvCase):
    def test_missing_room(self):
        self.assertFalse(dc.doc_get("", HS)["ok"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied", dc.doc_put(ROOM, "x", agent_mxid=HS, gate={})["reason"])

    def test_no_relay(self):
        self.assertIn("no gateway", dc.doc_get(ROOM, agent_mxid=HS, gate=None)["reason"])

    def test_get_envelope(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        fake = {"ok": True, "file": "TODO.md", "folder": "room-todo", "content": "# T"}
        with mock.patch.object(dc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, fake))[1]):
            res = dc.doc_get(ROOM, folder="room-todo", name="TODO.md", agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertTrue(cap["url"].endswith("/v1/room"))
        self.assertEqual(cap["payload"]["op"], "prep_get")
        self.assertEqual(cap["payload"]["folder"], "room-todo")
        self.assertEqual(res["content"], "# T")

    def test_put_envelope_b64(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        fake = {"ok": True, "file": "notes.md", "sha": "abc123"}
        with mock.patch.object(dc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, fake))[1]):
            res = dc.doc_put(ROOM, "hello", folder="scratch", name="notes.md",
                             message="m", agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"] and res["sha"] == "abc123")
        self.assertEqual(cap["payload"]["op"], "prep_put")
        self.assertEqual(base64.b64decode(cap["payload"]["content_b64"]).decode(), "hello")
        self.assertEqual(cap["payload"]["message"], "m")

    def test_rm_envelope(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(dc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, {"ok": True}))[1]):
            res = dc.doc_rm(ROOM, "old.md", folder="room-memo", agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(cap["payload"]["op"], "prep_delete")
        self.assertEqual(cap["payload"]["filename"], "old.md")

    def test_gateway_error_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(dc, "http_json",
                               return_value=(200, {"error": "bad folder (1-3 safe path segments, no leading dots)"})):
            res = dc.doc_put(ROOM, "x", folder="../etc", agent_mxid=HS, gate=None)
        self.assertFalse(res["ok"])
        self.assertIn("bad folder", res["reason"])

    def test_403_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(dc, "http_json", side_effect=err):
            self.assertIn("not a joined member", dc.doc_get(ROOM, agent_mxid=HS, gate=None)["reason"])


# ----- unified CLI ----- #
class CliTests(EnvCase):
    def test_read_exits_zero(self):
        self.assertEqual(room_ops._main(["read", ROOM, "--agent", HS]), 0)

    def test_send_exits_zero_on_no_context(self):
        self.assertEqual(room_ops._main(["send", ROOM, "/nope/x.png", "--agent", HS]), 0)

    def test_react_ack_maps_and_exits_zero(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(p), (200, {}))[1]):
            rc_ = room_ops._main(["react", ROOM, EV, "--ack", "done", "--agent", HS])
        self.assertEqual(rc_, 0)
        self.assertEqual(cap["key"], rc.ACK["done"])

    def test_fetch_exits_zero(self):
        self.assertEqual(room_ops._main(["fetch", "mxc://x/y", "--room", ROOM, "--agent", HS]), 0)

    def test_join_exits_zero_on_no_gateway(self):
        self.assertEqual(room_ops._main(["join", ROOM, "--agent", HS]), 0)

    def test_doc_put_missing_file_structured_error(self):
        # P2 (PR #2050 review): --file read failure must yield the structured
        # ok:false envelope, not a traceback.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_ = room_ops._main(["doc", "put", ROOM, "--file", "/nope/missing.md", "--agent", HS])
        self.assertEqual(rc_, 0)
        res = json.loads(buf.getvalue())
        self.assertFalse(res["ok"])
        self.assertIn("cannot read --file", res["reason"])


class GatewayTokenOnboardingTests(EnvCase):
    """The combined one-token onboarding contract (REMOTE_TASK_TOKEN='url|secret')."""

    def test_gateway_env_is_primary(self):
        # GATEWAY_* is the primary name; RELAY_* remains a transition alias.
        os.environ["GATEWAY_URL"] = "https://gw"
        os.environ["GATEWAY_TOKEN"] = "gwsecret"
        os.environ["RELAY_URL"] = "https://old"  # alias must lose to GATEWAY_URL
        base, headers = _gateway.gateway()
        self.assertEqual(base, "https://gw")
        self.assertEqual(headers["Authorization"], "Bearer gwsecret")

    def test_relay_alias_still_honored(self):
        os.environ["RELAY_URL"] = "https://old"
        os.environ["RELAY_TOKEN"] = "oldsecret"
        base, headers = _gateway.gateway()
        self.assertEqual(base, "https://old")
        self.assertEqual(headers["Authorization"], "Bearer oldsecret")

    def test_combined_token_only(self):
        os.environ["REMOTE_TASK_TOKEN"] = "https://gateway.example|s3cret"
        base, headers = _gateway.gateway()
        self.assertEqual(base, "https://gateway.example")
        self.assertEqual(headers["Authorization"], "Bearer s3cret")

    def test_explicit_url_beats_token_url(self):
        os.environ["REMOTE_TASK_TOKEN"] = "https://from-token|s3cret"
        os.environ["RELAY_URL"] = "https://explicit"
        base, headers = _gateway.gateway()
        self.assertEqual(base, "https://explicit")
        self.assertEqual(headers["Authorization"], "Bearer s3cret")

    def test_explicit_relay_token_not_split(self):
        # An explicit token whose part before '|' is NOT a URL scheme is a real
        # bearer and kept whole (only the "https://<url>|<secret>" onboarding
        # form is split).
        os.environ["RELAY_TOKEN"] = "weird|bearer|value"
        os.environ["RELAY_URL"] = "https://r"
        base, headers = _gateway.gateway()
        self.assertEqual(headers["Authorization"], "Bearer weird|bearer|value")

    def test_explicit_combined_gateway_token_is_split(self):
        # The combined onboarding form is split even when passed EXPLICITLY as
        # GATEWAY_TOKEN, so `GATEWAY_TOKEN=https://g|secret` authenticates with
        # just the secret (regression: it used to send the whole string → 401).
        os.environ["GATEWAY_TOKEN"] = "https://chat.example/relay|s3cr3t"
        base, headers = _gateway.gateway()
        self.assertEqual(headers["Authorization"], "Bearer s3cr3t")
        self.assertEqual(base, "https://chat.example/relay")

    def test_bare_secret_needs_url(self):
        os.environ["REMOTE_TASK_TOKEN"] = "baresecret"
        base, headers = _gateway.gateway()
        self.assertEqual(base, "")  # no url anywhere -> empty (op will degrade)
        self.assertEqual(headers["Authorization"], "Bearer baresecret")

    def test_degrade_reason_distinguishes_401_from_403(self):
        # 401 = auth failure (bad bearer), 403 = real non-member. Keeping them
        # distinct is what stops a token bug being misread as "not a member".
        self.assertIn("auth", _gateway.degrade_reason(401).lower())
        self.assertNotIn("member", _gateway.degrade_reason(401).lower())
        self.assertIn("member", _gateway.degrade_reason(403).lower())


class OutboxAllowlistTests(EnvCase):
    """Outbound allowlist must fail (mostly) closed by default."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()

    def test_random_temp_file_denied_by_default(self):
        # ROOM_MEDIA_ALLOW unset -> a file just sitting under /tmp is NOT sendable.
        stray = os.path.join(self.tmp, "stray.png")
        open(stray, "wb").write(b"x")
        self.assertFalse(md._path_allowed(stray))

    def test_outbox_file_allowed_by_default(self):
        os.environ["ROOM_MEDIA_OUTBOX"] = self.tmp  # the dedicated outbox
        inside = os.path.join(self.tmp, "ok.png")
        open(inside, "wb").write(b"x")
        self.assertTrue(md._path_allowed(inside))

    def test_explicit_allow_dir(self):
        os.environ["ROOM_MEDIA_ALLOW"] = self.tmp
        f = os.path.join(self.tmp, "f.png")
        open(f, "wb").write(b"x")
        self.assertTrue(md._path_allowed(f))
        self.assertFalse(md._path_allowed("/etc/passwd"))


class ContentLengthTests(EnvCase):
    def test_fetch_rejects_declared_oversize_without_reading(self):
        os.environ["RELAY_URL"] = "https://r"
        # gateway declares an oversize Content-Length; http_request returns b"" (no read).
        with mock.patch.object(md, "http_request",
                               return_value=(200, b"", {"Content-Length": str(md.MAX_BYTES + 1)})):
            res = md.fetch_media("mxc://x/y", HS, ROOM, gate=None)
        self.assertFalse(res["ok"])
        self.assertIn("exceeds", res["reason"])


class NormalizeReactionsTests(unittest.TestCase):
    """_normalize must carry the gateway's per-message `reactions` annotation —
    it's the ONLY surface for the 👀 delivery-ack (reactions never arrive as tasks).
    Regression: the field was silently dropped, so a worker saw zero reactions."""

    def test_reactions_preserved(self):
        out = rd._normalize([{"event_id": "$e", "sender": "@a:hs", "body": "hi",
                              "reactions": [{"key": "\U0001F440", "sender": "@b:hs"}]}])
        self.assertEqual(out[0]["reactions"], [{"key": "\U0001F440", "sender": "@b:hs"}])

    def test_reactions_default_empty_list(self):
        out = rd._normalize([{"event_id": "$e", "sender": "@a:hs", "body": "hi"}])
        self.assertEqual(out[0]["reactions"], [])


import resolve as rs  # noqa: E402
import mention as mn  # noqa: E402

_AGENTS = [
    {"id": "@sutando-qingyun-001:ag2.space", "label": "Air MBP"},
    {"id": "@sutando-qingyun-mini:ag2.space", "label": "Mini"},
    {"id": "@qingyun-air.agent:ag2.space", "label": "core"},
]


class ResolveTests(unittest.TestCase):
    def test_exact_localpart(self):
        r = rs.match_agent("sutando-qingyun-001", _AGENTS)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mxid"], "@sutando-qingyun-001:ag2.space")

    def test_substring_unique(self):
        r = rs.match_agent("001", _AGENTS)  # only qingyun-001's localpart contains it
        self.assertTrue(r["ok"])
        self.assertEqual(r["mxid"], "@sutando-qingyun-001:ag2.space")

    def test_full_mxid_passthrough(self):
        r = rs.match_agent("@whoever:ag2.space", _AGENTS)  # already an mxid → trust it
        self.assertTrue(r["ok"])
        self.assertEqual(r["mxid"], "@whoever:ag2.space")

    def test_ambiguous_reports_candidates_and_does_not_resolve(self):
        r = rs.match_agent("sutando-qingyun", _AGENTS)  # substring of BOTH 001 and mini
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["candidates"]), 2)
        self.assertIsNone(r["mxid"])

    def test_no_match(self):
        r = rs.match_agent("nobody", _AGENTS)
        self.assertFalse(r["ok"])
        self.assertEqual(r["candidates"], [])

    def test_exact_beats_substring(self):
        agents = [{"id": "@mini:hs", "label": ""}, {"id": "@mini-helper:hs", "label": ""}]
        r = rs.match_agent("mini", agents)  # exact localpart wins over the substring one
        self.assertTrue(r["ok"])
        self.assertEqual(r["mxid"], "@mini:hs")

    def test_label_exact_match(self):
        r = rs.match_agent("Mini", _AGENTS)  # case-insensitive label hit
        self.assertTrue(r["ok"])
        self.assertEqual(r["mxid"], "@sutando-qingyun-mini:ag2.space")

    def test_empty_query(self):
        self.assertFalse(rs.match_agent("", _AGENTS)["ok"])


class MentionBodyTests(unittest.TestCase):
    def test_leads_with_mxid(self):
        b = mn.build_body("@sutando-qingyun-001:ag2.space", "review #149")
        self.assertTrue(b.startswith("@sutando-qingyun-001:ag2.space"))
        self.assertIn("review #149", b)

    def test_empty_message_is_bare_mxid(self):
        self.assertEqual(mn.build_body("@x:hs", ""), "@x:hs")

    def test_ambiguous_handle_does_not_post(self):
        # mention() must refuse to post on an ambiguous handle (never mention the
        # wrong agent) — returns ok:false + candidates, no network touched.
        res = mn.mention("sutando-qingyun", "hi", ROOM, HS, gate=None, agents=_AGENTS)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["candidates"]), 2)

    def test_post_payload_leads_with_mxid_and_carries_mentions(self):
        # A resolved mention posts op:message to /v1/room with the mxid LEADING
        # the body (text trigger) AND a forward-compat `mentions:[mxid]` (activates
        # structured push once the broker honors it). Both pinned so neither regresses.
        cap = {}

        def _fake_http_json(method, url, headers, payload):
            cap["url"], cap["payload"] = url, payload
            return 200, {"event_id": "$posted"}

        with mock.patch.object(mn, "gateway", return_value=("https://gw/relay", {})), \
             mock.patch.object(mn, "http_json", side_effect=_fake_http_json):
            res = mn.mention("qingyun-001", "review #149", ROOM, HS, gate=None, agents=_AGENTS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["event_id"], "$posted")
        self.assertTrue(cap["url"].endswith("/v1/room"))
        p = cap["payload"]
        self.assertEqual(p["op"], "message")
        self.assertEqual(p["mentions"], ["@sutando-qingyun-001:ag2.space"])
        self.assertTrue(p["body"].startswith("@sutando-qingyun-001:ag2.space"))


import events as ev  # noqa: E402
import rooms as rm  # noqa: E402
import events_acceptance as ea  # noqa: E402


# ----- rooms (#184 client half) ----- #
class RoomsTests(EnvCase):
    def test_no_gateway(self):
        self.assertIn("no gateway", rm.joined_rooms(HS)["reason"])

    def test_result_shape_and_envelope(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        fake = {"ok": True, "rooms": [ROOM], "rooms_detailed": [{"room_id": ROOM, "name": "A"}]}
        with mock.patch.object(rm, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, fake))[1]):
            res = rm.joined_rooms(HS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["rooms"], [ROOM])
        self.assertEqual(res["rooms_detailed"][0]["room_id"], ROOM)
        self.assertIsNone(res["reason"])
        self.assertTrue(cap["url"].endswith("/v1/room"))
        self.assertEqual(cap["payload"], {"op": "joined_rooms"})

    def test_403_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(rm, "http_json", side_effect=err):
            self.assertIn("not a joined member", rm.joined_rooms(HS)["reason"])


# ----- events: subscription ops (#184) ----- #
class EventsSubscribeTests(EnvCase):
    def test_no_gateway(self):
        self.assertIn("no gateway", ev.subscribe(ROOM, ["message.created"],
                                                 agent_mxid=HS, gate=None)["reason"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied",
                      ev.subscribe(ROOM, ["message.created"], agent_mxid=HS, gate={})["reason"])

    def test_subscribe_envelope(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        # #184 contract shape: the id is NESTED under "subscription", not
        # top-level. Guards the P2-1 fix — client must read the nested id.
        fake = {"ok": True, "subscription": {"subscription_id": "sub-1", "room_id": ROOM}}
        with mock.patch.object(ev, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, fake))[1]):
            res = ev.subscribe(ROOM, ["message.created", "reaction.added"],
                               filters={"actor": "@x:hs"}, agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["subscription_id"], "sub-1")
        self.assertTrue(cap["url"].endswith("/v1/room"))
        self.assertEqual(cap["payload"], {"op": "events_subscribe", "room_id": ROOM,
                                          "event_types": ["message.created", "reaction.added"],
                                          "filters": {"actor": "@x:hs"}})

    def test_subscribe_omits_absent_filters(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(ev, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, {"ok": True}))[1]):
            ev.subscribe(ROOM, ["message.created"], agent_mxid=HS, gate=None)
        self.assertNotIn("filters", cap["payload"])

    def test_unsubscribe_envelope(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(ev, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, {"ok": True}))[1]):
            res = ev.unsubscribe(ROOM, agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(cap["payload"], {"op": "events_unsubscribe", "room_id": ROOM})

    def test_subscriptions_no_room_no_gate(self):
        # `events_subscriptions` has no room target → the per-room gate must
        # NOT be consulted (an empty gate would otherwise deny everything).
        os.environ["RELAY_URL"] = "https://r"
        os.environ["ROOM_OPS_GATE"] = "/nonexistent/but-empty-would-deny.json"
        cap = {}
        fake = {"ok": True, "subscriptions": [{"room_id": ROOM, "subscription_id": "sub-1"}]}
        with mock.patch.object(ev, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, fake))[1]):
            res = ev.subscriptions(HS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["subscriptions"][0]["subscription_id"], "sub-1")
        self.assertEqual(cap["payload"], {"op": "events_subscriptions"})

    def test_403_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(ev, "http_json", side_effect=err):
            self.assertIn("not a joined member",
                          ev.subscribe(ROOM, ["message.created"], agent_mxid=HS, gate=None)["reason"])


# ----- events: typed emit (op:event) ----- #
class EventsEmitTests(EnvCase):
    def test_no_gateway(self):
        res = ev.emit(ROOM, "space.ag2.app.card", {"k": 1}, agent_mxid=HS, gate=None)
        self.assertIn("no gateway", res["reason"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        res = ev.emit(ROOM, "space.ag2.app.card", {"k": 1}, agent_mxid=HS, gate={})
        self.assertIn("gate denied", res["reason"])

    def test_emit_envelope_and_event_id(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        fake = {"event_id": "$abc"}
        with mock.patch.object(ev, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, fake))[1]):
            res = ev.emit(ROOM, "space.ag2.app.card", {"k": 1}, agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["event_id"], "$abc")
        self.assertEqual(res["state"], "confirmed")
        self.assertTrue(cap["url"].endswith("/v1/room"))
        self.assertEqual(cap["payload"], {"op": "event", "room_id": ROOM,
                                          "type": "space.ag2.app.card",
                                          "content": {"k": 1}})

    def test_missing_event_id_is_unconfirmed_but_ok(self):
        # Same fail-open receipt as say/mention — a caller must not re-send an
        # event the gateway may already have landed.
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(ev, "http_json",
                               side_effect=lambda m, u, h, p: (200, {"ok": True})):
            res = ev.emit(ROOM, "space.ag2.app.card", {"k": 1}, agent_mxid=HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"], "unconfirmed")
        self.assertIsNone(res["event_id"])

    def test_server_type_refusal_surfaces_verbatim(self):
        # The namespace rule lives server-side and is deliberately NOT copied
        # into the client; its refusal must reach the caller as `reason`.
        os.environ["RELAY_URL"] = "https://r"
        err = {"error": "event type must be under space.ag2.*"}
        with mock.patch.object(ev, "http_json", side_effect=lambda m, u, h, p: (200, err)):
            res = ev.emit(ROOM, "not.ag2.thing", {"k": 1}, agent_mxid=HS, gate=None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "event type must be under space.ag2.*")


# ----- events: long-poll pull ----- #
class EventsPullTests(EnvCase):
    def test_no_gateway(self):
        res = ev.pull(cursor=3)
        self.assertFalse(res["ok"])
        self.assertEqual(res["cursor"], 3)  # cursor passes through unchanged

    def test_pull_url_and_timeout(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        fake = {"ok": True, "events": [], "cursor": 7}
        with mock.patch.object(ev, "_http_get_json",
                               side_effect=lambda u, h, t: (cap.update(url=u, headers=h, timeout=t), fake)[1]):
            res = ev.pull(cursor=7, wait=5)
        self.assertTrue(res["ok"])
        self.assertEqual(res["cursor"], 7)
        self.assertIn("/v1/events?", cap["url"])
        self.assertIn("cursor=7", cap["url"])
        self.assertIn("wait=5", cap["url"])
        # Socket timeout must OUTLIVE the server's hold window (wait) or every
        # quiet poll dies mid-hold instead of returning empty events.
        self.assertGreater(cap["timeout"], 5)
        # Contract: User-Agent required on every request, like everything else.
        self.assertIn("User-Agent", cap["headers"])

    def test_404_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(ev, "_http_get_json", side_effect=err):
            self.assertIn("unimplemented", ev.pull()["reason"])


# ----- events: SSE frame parser ----- #
class SSEFrameTests(unittest.TestCase):
    def _frames(self, text):
        return list(ev.sse_frames(text.split("\n")))

    def test_id_data_blank_dispatch(self):
        fr = self._frames('id: 5\ndata: {"a":1}\n\n')
        self.assertEqual(fr, [("event", "5", '{"a":1}')])

    def test_multiline_data_accumulates(self):
        fr = self._frames("id: 6\ndata: line1\ndata: line2\n\n")
        self.assertEqual(fr, [("event", "6", "line1\nline2")])

    def test_keepalive_comment(self):
        fr = self._frames(": ping\n\n")
        self.assertEqual(fr, [("comment", None, "ping")])

    def test_no_dispatch_without_blank_line(self):
        # A frame is only dispatched by its terminating blank line — a torn
        # tail at disconnect must NOT surface as a (partial) event.
        self.assertEqual(self._frames('data: {"a":1}'), [])

    def test_id_is_sticky_across_frames(self):
        fr = self._frames("id: 9\ndata: a\n\ndata: b\n\n")
        self.assertEqual(fr[1], ("event", "9", "b"))

    def test_unknown_fields_ignored(self):
        fr = self._frames("event: message\nretry: 100\nid: 3\ndata: x\n\n")
        self.assertEqual(fr, [("event", "3", "x")])


# ----- events: stream ----- #
def _sse_resp(text):
    return io.BytesIO(text.encode())


class EventsStreamTests(EnvCase):
    def test_no_gateway_raises_runtimeerror(self):
        with self.assertRaises(RuntimeError):
            ev.stream(on_event=lambda c, e: None)

    def test_last_event_id_header_and_delivery(self):
        os.environ["RELAY_URL"] = "https://r"
        cap, got = {}, []
        body = 'id: 7\ndata: {"event_id":"$e","cursor":7,"type":"message.created"}\n\n'

        def fake_open(url, headers):
            cap.update(url=url, headers=headers)
            return _sse_resp(body)

        with mock.patch.object(ev, "_open_stream", side_effect=fake_open):
            cur = ev.stream(cursor=42, on_event=lambda c, e: got.append((c, e)), max_events=1)
        self.assertEqual(cap["headers"]["Last-Event-ID"], "42")
        self.assertEqual(cap["headers"]["Accept"], "text/event-stream")
        self.assertTrue(cap["url"].endswith("/v1/events/stream"))
        self.assertEqual(got[0][0], 7)
        self.assertEqual(got[0][1]["event_id"], "$e")
        self.assertEqual(cur, 7)

    def test_no_cursor_no_header(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}

        def fake_open(url, headers):
            cap.update(headers=headers)
            return _sse_resp('id: 1\ndata: {"cursor":1}\n\n')

        with mock.patch.object(ev, "_open_stream", side_effect=fake_open):
            ev.stream(on_event=lambda c, e: None, max_events=1)
        self.assertNotIn("Last-Event-ID", cap["headers"])

    def test_keepalive_cb_and_eof_raises(self):
        os.environ["RELAY_URL"] = "https://r"
        pings = []
        with mock.patch.object(ev, "_open_stream", return_value=_sse_resp(": ping\n\n")):
            with self.assertRaises(ev.StreamDisconnected):
                ev.stream(on_event=lambda c, e: None, keepalive_cb=pings.append)
        self.assertEqual(pings, ["ping"])

    def test_403_open_raises_runtimeerror(self):
        # Permission problems must NOT look like a transient disconnect, or the
        # resume wrapper would retry a hopeless connection forever.
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(ev, "_open_stream", side_effect=err):
            with self.assertRaises(RuntimeError):
                ev.stream(on_event=lambda c, e: None)


# ----- events: durable cursor ----- #
class CursorFileTests(unittest.TestCase):
    def test_save_load_roundtrip_atomic(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "cursor")
        ev.save_cursor(p, 41)
        ev.save_cursor(p, 42)  # overwrite goes through the same tmp+rename path
        self.assertEqual(ev.load_cursor(p), 42)
        self.assertFalse(os.path.exists(p + ".tmp"))  # rename consumed the tmp

    def test_load_missing_is_none(self):
        self.assertIsNone(ev.load_cursor("/nonexistent/cursor"))

    def test_load_garbled_is_none(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "cursor")
        open(p, "w").write("junk")
        self.assertIsNone(ev.load_cursor(p))


class StreamWithResumeTests(EnvCase):
    def test_resumes_from_file_and_persists_each_event(self):
        os.environ["RELAY_URL"] = "https://r"
        d = tempfile.mkdtemp()
        cf = os.path.join(d, "cursor")
        ev.save_cursor(cf, 10)
        cap, got = {"resume_ids": []}, []
        body = ('id: 11\ndata: {"event_id":"$a","cursor":11}\n\n'
                'id: 12\ndata: {"event_id":"$b","cursor":12}\n\n')

        def fake_open(url, headers):
            cap["resume_ids"].append(headers.get("Last-Event-ID"))
            return _sse_resp(body)

        with mock.patch.object(ev, "_open_stream", side_effect=fake_open):
            cur = ev.stream_with_resume(cf, lambda c, e: got.append(c), max_events=2)
        self.assertEqual(cap["resume_ids"][0], "10")  # resumed from the file
        self.assertEqual(got, [11, 12])
        self.assertEqual(ev.load_cursor(cf), 12)      # last delivered persisted
        self.assertEqual(cur, 12)

    def test_reconnects_with_backoff_after_disconnect(self):
        os.environ["RELAY_URL"] = "https://r"
        d = tempfile.mkdtemp()
        cf = os.path.join(d, "cursor")
        calls, naps = {"n": 0}, []

        def fake_open(url, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                return _sse_resp(": ping\n\n")  # EOF → StreamDisconnected
            return _sse_resp('id: 1\ndata: {"event_id":"$a","cursor":1}\n\n')

        with mock.patch.object(ev, "_open_stream", side_effect=fake_open), \
             mock.patch.object(ev.time, "sleep", side_effect=naps.append):
            cur = ev.stream_with_resume(cf, lambda c, e: None, max_events=1)
        self.assertEqual(cur, 1)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(naps, [1.0])  # the ladder starts at 1s

    def test_should_persist_holds_cursor_for_pending_batch(self):
        # P1-2: a batching consumer HOLDS cursor persistence while events sit
        # un-flushed. Events 11,12 are "pending" (gate False); 13 "flushes"
        # (gate True) — only 13's cursor may reach the durable file, so a
        # restart replays 11,12 instead of skipping past them.
        os.environ["RELAY_URL"] = "https://r"
        d = tempfile.mkdtemp()
        cf = os.path.join(d, "cursor")
        ev.save_cursor(cf, 10)
        body = ('id: 11\ndata: {"event_id":"$a","cursor":11}\n\n'
                'id: 12\ndata: {"event_id":"$b","cursor":12}\n\n'
                'id: 13\ndata: {"event_id":"$c","cursor":13}\n\n')
        persisted = []
        real_save = ev.save_cursor

        def spy_save(path, cur):
            persisted.append(cur)
            real_save(path, cur)

        with mock.patch.object(ev, "_open_stream", side_effect=lambda url, headers: _sse_resp(body)), \
             mock.patch.object(ev, "save_cursor", side_effect=spy_save):
            cur = ev.stream_with_resume(cf, lambda c, e: None, max_events=3,
                                        should_persist=lambda c, e: c == 13)
        self.assertEqual(persisted, [13])       # 11,12 held; only the flush committed
        self.assertEqual(ev.load_cursor(cf), 13)
        self.assertEqual(cur, 13)


    def test_save_cursor_fsyncs_file_then_directory(self):
        # Review P1 (both reviewers): the COLD-START anchor's directory entry
        # must be durable — save_cursor asserts data-fsync → rename →
        # dir-fsync, same contract as task promotion. Losing a later cursor
        # merely replays; losing the first-ever anchor loses the held batch.
        d = tempfile.mkdtemp()
        cf = os.path.join(d, "cursor")
        events_seen = []
        real_fsync, real_replace, real_open_fd = os.fsync, os.replace, os.open
        fd_paths = {}

        def spy_open(path, flags, *a, **k):
            fd = real_open_fd(path, flags, *a, **k)
            fd_paths[fd] = path
            return fd

        def spy_fsync(fd):
            events_seen.append(("fsync", "dir" if fd_paths.get(fd) == d else "file"))
            return real_fsync(fd)

        def spy_replace(src, dst):
            events_seen.append(("rename", dst))
            return real_replace(src, dst)

        try:
            os.open, os.fsync, os.replace = spy_open, spy_fsync, spy_replace
            ev.save_cursor(cf, 130)
        finally:
            os.open, os.fsync, os.replace = real_open_fd, real_fsync, real_replace
        self.assertEqual([k for k, _ in events_seen], ["fsync", "rename", "fsync"])
        self.assertEqual(events_seen[0][1], "file")  # temp-file data first
        self.assertEqual(events_seen[2][1], "dir")   # then the directory entry
        self.assertEqual(ev.load_cursor(cf), 130)

    def test_cold_start_seeds_replay_anchor_before_withheld_batch(self):
        # Review P1 (cold-start half): NO cursor file + a batching consumer
        # that withholds persistence. The first delivered event must seed
        # `cur - 1` as a durable pre-batch anchor BEFORE it can sit in a
        # withheld batch — otherwise a hard kill leaves no cursor file, the
        # restart sends no Last-Event-ID, the server defaults to new-events-
        # only, and the held event is lost forever (not at-least-once).
        os.environ["RELAY_URL"] = "https://r"
        d = tempfile.mkdtemp()
        cf = os.path.join(d, "cursor")           # absent — true cold start
        body = ('id: 131\ndata: {"event_id":"$a","cursor":131}\n\n'
                'id: 132\ndata: {"event_id":"$b","cursor":132}\n\n')
        with mock.patch.object(ev, "_open_stream",
                               side_effect=lambda url, headers: _sse_resp(body)):
            ev.stream_with_resume(cf, lambda c, e: None, max_events=2,
                                  should_persist=lambda c, e: False)  # batch never flushes
        # "hard kill with the batch pending": the file must already anchor
        # replay at 130 so a restart re-delivers 131 (and 132).
        self.assertEqual(ev.load_cursor(cf), 130)
        cap = {"resume": None}

        def fake_open2(url, headers):
            cap["resume"] = headers.get("Last-Event-ID")
            return _sse_resp(body)

        with mock.patch.object(ev, "_open_stream", side_effect=fake_open2):
            ev.stream_with_resume(cf, lambda c, e: None, max_events=2,
                                  should_persist=lambda c, e: False)
        self.assertEqual(cap["resume"], "130")   # restart REPLAYS the held events
        self.assertEqual(ev.load_cursor(cf), 130)  # anchor still held (no flush)
        # a warm start (existing cursor) never rewrites the anchor backwards:
        ev.save_cursor(cf, 200)
        with mock.patch.object(ev, "_open_stream",
                               side_effect=lambda url, headers: _sse_resp(
                                   'id: 300\ndata: {"event_id":"$z","cursor":300}\n\n')):
            ev.stream_with_resume(cf, lambda c, e: None, max_events=1,
                                  should_persist=lambda c, e: False)
        self.assertEqual(ev.load_cursor(cf), 200)  # loaded cursor wins; no 299 seed


# ----- events CLI ----- #
class EventsCliTests(EnvCase):
    def test_rooms_exits_zero(self):
        self.assertEqual(room_ops._main(["rooms", "--agent", HS]), 0)

    def test_events_pull_exits_zero_no_gateway(self):
        self.assertEqual(room_ops._main(["events", "pull"]), 0)

    def test_events_subscribe_bad_filters_structured(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_ = room_ops._main(["events", "subscribe", ROOM, "--types", "message.created",
                                  "--filters", "{not json", "--agent", HS])
        self.assertEqual(rc_, 0)
        res = json.loads(buf.getvalue())
        self.assertFalse(res["ok"])
        self.assertIn("not valid JSON", res["reason"])

    def test_events_stream_no_gateway_structured(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_ = room_ops._main(["events", "stream", "--once"])
        self.assertEqual(rc_, 0)
        out = json.loads(buf.getvalue().strip().split("\n")[-1])
        self.assertFalse(out["ok"])
        self.assertIn("no gateway", out["reason"])


# ----- acceptance runner (events_acceptance) ----- #
class ObserveReactionKeyTests(unittest.TestCase):
    def test_observe_key_is_eyes_and_distinct_from_task_ack(self):
        # Owner-finalized convention: 👀 = event OBSERVED, 🫡 = task acknowledged.
        # The earlier 🔭 hold is retired now that the task-ack glyph moved to 🫡
        # (broker server-side #188 + this skill's ACK["received"]), so a room
        # glance still tells "observed" (👀) apart from "acknowledged" (🫡).
        self.assertEqual(ea.OBSERVE_REACTION, "\U0001F440")   # 👀
        self.assertEqual(rc.ACK["received"], "\U0001FAE1")    # 🫡
        self.assertNotEqual(ea.OBSERVE_REACTION, rc.ACK["received"])


# ----- taskify accumulator (events_acceptance) ----- #
class EventAccumulatorTests(unittest.TestCase):
    @staticmethod
    def _env(eid, actor, etype="message.created", room=ROOM):
        return {"event_id": eid, "type": etype, "room_id": room, "actor_id": actor,
                "ts": 0, "content": {}}

    def test_threshold_promotion_skips_self_and_dups(self):
        d = tempfile.mkdtemp()
        acc = ea.EventAccumulator(ROOM, HS, 3, d)
        feed = [
            (1, self._env("$self1", HS)),                        # self → skip
            (2, self._env("$a", "@u:hs")),                       # counts (1)
            (3, self._env("$self2", HS)),                        # self → skip
            (4, self._env("$b", "@u:hs", "reaction.added")),     # counts (2)
            (5, self._env("$a", "@u:hs")),                       # duplicate → skip
            (6, self._env("$c", "@u:hs")),                       # counts (3) → promote
        ]
        paths = [p for cur, e in feed for p in [acc.offer(cur, e)] if p]
        self.assertEqual(len(paths), 1)

        base = os.path.basename(paths[0])
        self.assertTrue(base.startswith("task-") and base.endswith(".txt"))
        # Format is the SPARROW promotion format now — the accumulator is a
        # thin adapter over ag2_sparrow TaskifyHandler (one implementation).
        lines = open(paths[0]).read().split("\n")
        self.assertEqual(lines[0], f"id: {base[:-4]}")
        self.assertTrue(lines[1].startswith("timestamp: ") and lines[1].endswith("Z"))
        # Origin must be explicit at a glance: the [taskify] marker leads the
        # task line and the promoted-from suffix names the room.
        self.assertTrue(lines[2].startswith("task: [taskify] "))
        self.assertIn(f"(promoted from 3 subscribed events in {ROOM})", lines[2])
        self.assertEqual(lines[3], "source: events-promotion")
        self.assertEqual(lines[4], f"channel_id: {ROOM}")
        self.assertEqual(lines[5], "priority: low")          # never outranks humans
        self.assertEqual(lines[6], "model_hint: efficient")  # cheap-model eligible
        self.assertEqual(lines[7], "access_tier: ambient")  # trust boundary: never owner
        prov_line = next(ln for ln in lines if ln.startswith("provenance: "))
        prov = json.loads(prov_line.split("provenance: ", 1)[1])
        self.assertEqual(prov["source_event_ids"], ["$a", "$b", "$c"])
        self.assertEqual(prov["promotion_reason"], "threshold 3 meaningful events")
        self.assertEqual(prov["cursor_range"], [2, 6])
        # In-band DiD block trails the headers/provenance (mirrors the bridge
        # fence), telling the core: observation-not-instruction, no privileged
        # action. It's guidance, not the boundary (the tier is).
        full = open(paths[0]).read()
        self.assertIn("===SUTANDO SYSTEM INSTRUCTIONS", full)
        self.assertIn("===END SUTANDO SYSTEM INSTRUCTIONS===", full)
        self.assertIn("ambient OBSERVATION", full)
        self.assertIn("NO privileged action", full)
        # ...and it comes AFTER the provenance line, not interleaved with headers.
        self.assertGreater(full.index("SUTANDO SYSTEM INSTRUCTIONS"), full.index("provenance:"))

    def test_wrong_room_and_state_changed_do_not_count(self):
        d = tempfile.mkdtemp()
        acc = ea.EventAccumulator(ROOM, HS, 1, d)
        self.assertIsNone(acc.offer(1, self._env("$x", "@u:hs", room="!other:hs")))
        self.assertIsNone(acc.offer(2, self._env("$y", "@u:hs", "room.state_changed")))

    def test_each_batch_promotes_once_and_resets(self):
        d = tempfile.mkdtemp()
        acc = ea.EventAccumulator(ROOM, HS, 1, d)
        p1 = acc.offer(1, self._env("$x", "@u:hs"))
        p2 = acc.offer(2, self._env("$y", "@u:hs"))
        self.assertTrue(p1 and p2)
        # Distinct batches ($x vs $y) → distinct deterministic ids → distinct
        # files (the id is keyed on source event_ids, not a timestamp).
        self.assertNotEqual(p1, p2)
        self.assertIn("1 room events", open(p1).read())  # sparrow format

    def test_replayed_batch_is_idempotent_no_duplicate(self):
        # P1-2 duplicate half (#2292 review): a crash after _promote() renames
        # the task file but before the cursor advances replays the SAME events
        # into a fresh accumulator (empty _seen_ids). The deterministic id keyed
        # on source event_ids means the replay resolves to the SAME path and is
        # skipped — no second time-based task. Simulates restart via a new acc
        # over the same task_dir.
        d = tempfile.mkdtemp()
        acc1 = ea.EventAccumulator(ROOM, HS, 2, d)
        acc1.offer(1, self._env("$a", "@u:hs"))
        p1 = acc1.offer(2, self._env("$b", "@u:hs"))          # promote
        self.assertTrue(p1)
        # "restart": fresh accumulator, fresh dedup, same task_dir; same events
        # replay (cursor did not advance past them).
        acc2 = ea.EventAccumulator(ROOM, HS, 2, d)
        acc2.offer(1, self._env("$a", "@u:hs"))
        p2 = acc2.offer(2, self._env("$b", "@u:hs"))          # replay → same id
        self.assertEqual(p1, p2)                               # same path, idempotent
        self.assertEqual(len([f for f in os.listdir(d) if f.endswith(".txt")]), 1)

    def test_promotion_fsyncs_file_then_directory_before_return(self):
        # Review merge-gate 1: should_persist advances the durable cursor on
        # the strength of the task path existing, so BOTH the file data and
        # the directory entry must be fsynced BEFORE _promote returns — an
        # unflushed rename + host crash loses the batch permanently (cursor
        # moved, file gone). Asserts the ORDER: data fsync → rename → dir fsync.
        d = tempfile.mkdtemp()
        events = []
        real_fsync, real_replace = os.fsync, os.replace
        fd_paths = {}
        real_open_fd = os.open

        def spy_open(path, flags, *a, **k):
            fd = real_open_fd(path, flags, *a, **k)
            fd_paths[fd] = path
            return fd

        def spy_fsync(fd):
            # dir-fsync arrives on an os.open() fd we recorded (the task_dir);
            # the data fsync arrives on the write-handle's fd (not via os.open).
            events.append(("fsync", "dir" if fd_paths.get(fd) == d else "file"))
            return real_fsync(fd)

        def spy_replace(src, dst):
            events.append(("rename", dst))
            return real_replace(src, dst)

        acc = ea.EventAccumulator(ROOM, HS, 1, d)
        try:
            os.open = spy_open
            os.fsync = spy_fsync
            os.replace = spy_replace
            path = acc.offer(1, self._env("$dur", "@u:hs"))
        finally:
            os.open, os.fsync, os.replace = real_open_fd, real_fsync, real_replace
        self.assertTrue(path and os.path.exists(path))
        kinds = [k for k, _ in events]
        self.assertEqual(kinds, ["fsync", "rename", "fsync"],
                         f"durability order must be data-fsync → rename → dir-fsync, got {events}")
        self.assertEqual(events[0][1], "file")   # first fsync is the temp file's data
        self.assertEqual(events[2][1], "dir")    # last fsync is the tasks directory entry

    def test_has_pending_tracks_unflushed_batch(self):
        # P1-2 cursor-hold hinges on this: pending while events accumulate,
        # clear the instant a promotion flushes the batch.
        d = tempfile.mkdtemp()
        acc = ea.EventAccumulator(ROOM, HS, 3, d)
        self.assertFalse(acc.has_pending())                 # empty
        acc.offer(1, self._env("$a", "@u:hs"))
        self.assertTrue(acc.has_pending())                  # 1 accumulated
        acc.offer(2, self._env("$self", HS))               # self → skipped, no change
        self.assertTrue(acc.has_pending())                  # still pending $a
        acc.offer(3, self._env("$b", "@u:hs"))
        p = acc.offer(4, self._env("$c", "@u:hs"))         # 3rd meaningful → promote
        self.assertTrue(p)
        self.assertFalse(acc.has_pending())                 # flushed → cursor safe


# ----- acceptance runner arg guards (events_acceptance) ----- #
class AcceptanceRunnerArgTests(EnvCase):
    def test_acting_modes_require_agent(self):
        # P2-2: react/taskify ACT (emit reactions / write tasks) and rely on the
        # agent's own mxid to suppress self-echo — argparse must reject them when
        # neither --agent nor AGENT_MXID is present, or the runner acts on its
        # own events in a feedback loop.
        os.environ.pop("AGENT_MXID", None)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ea._main(["--room", ROOM, "--cursor-file", "/tmp/c", "--mode", "react"])
            with self.assertRaises(SystemExit):
                ea._main(["--room", ROOM, "--cursor-file", "/tmp/c",
                          "--mode", "taskify", "--task-dir", "/tmp/t"])


# ----- reply citation (relations, say, mention) ----- #
import say as sy, relations as rl  # noqa: E401,E402

OTHER = "$evt2"


class RelationFieldsTests(unittest.TestCase):
    def test_none_is_uncited(self):
        self.assertEqual(rl.relation_fields(), {})

    def test_reply_to_becomes_the_field(self):
        self.assertEqual(rl.relation_fields(reply_to=EV), {"reply_to": EV})

    def test_malformed_ids_raise(self):
        for bad in ("evt1", "$", "root", "  ", "  $ok"[:3]):
            with self.assertRaises(rl.RelationError):
                rl.relation_fields(reply_to=bad)

    def test_empty_string_means_no_citation(self):
        # Distinct from malformed: "" is the flag not being given at all.
        self.assertEqual(rl.relation_fields(reply_to=""), {})

    def test_whitespace_is_stripped(self):
        self.assertEqual(rl.relation_fields(reply_to="  $evt1  "), {"reply_to": EV})

    def test_no_thread_surface_is_offered(self):
        # The gateway cannot honour a thread relation, so asking for one must be
        # impossible rather than silently downgraded to this citation.
        with self.assertRaises(TypeError):
            rl.relation_fields(thread_root=EV)


class SayCitationTests(EnvCase):
    def _post(self, **kwargs):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(sy, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, {}))[1]):
            res = sy.say("hi", ROOM, HS, gate=None, **kwargs)
        return res, cap

    def test_plain_say_cites_nothing(self):
        res, cap = self._post()
        self.assertTrue(res["ok"])
        self.assertNotIn("reply_to", cap["payload"])

    def test_reply_to_rides_the_payload(self):
        res, cap = self._post(reply_to=EV)
        self.assertTrue(res["ok"])
        self.assertEqual(cap["payload"]["reply_to"], EV)
        # The body and op are untouched by the citation field.
        self.assertEqual(cap["payload"]["body"], "hi")
        self.assertEqual(cap["payload"]["op"], "message")

    def test_no_thread_relation_is_ever_sent(self):
        # Pins the review's requirement: nothing on this path may claim thread
        # membership the gateway cannot deliver.
        _res, cap = self._post(reply_to=EV)
        self.assertNotIn("thread_root", cap["payload"])
        self.assertNotIn("m.relates_to", cap["payload"])

    def test_bad_id_refuses_before_the_network(self):
        os.environ["RELAY_URL"] = "https://r"
        called = []
        with mock.patch.object(sy, "http_json",
                               side_effect=lambda *a, **k: called.append(a) or (200, {})):
            res = sy.say("hi", ROOM, HS, gate=None, reply_to="evt1")
        self.assertFalse(res["ok"])
        self.assertIn("event id", res["reason"])
        self.assertEqual(called, [])   # no post — control below proves the mock fires

    def test_control_good_id_does_post(self):
        # Pairs with the test above: proves the empty call list there is the
        # refusal, not a mock that never fires.
        os.environ["RELAY_URL"] = "https://r"
        called = []
        with mock.patch.object(sy, "http_json",
                               side_effect=lambda *a, **k: called.append(a) or (200, {})):
            sy.say("hi", ROOM, HS, gate=None, reply_to=EV)
        self.assertEqual(len(called), 1)


class MentionCitationTests(EnvCase):
    AGENTS = [{"id": "@peer:hs", "label": "peer"}]

    def test_citation_rides_the_payload(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(mn, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, {}))[1]):
            res = mn.mention("peer", "ping", ROOM, HS, gate=None, agents=self.AGENTS,
                             reply_to=OTHER)
        self.assertTrue(res["ok"])
        self.assertEqual(cap["payload"]["reply_to"], OTHER)
        self.assertEqual(cap["payload"]["mentions"], ["@peer:hs"])

    def test_bad_id_refuses_before_resolve_and_network(self):
        os.environ["RELAY_URL"] = "https://r"
        called = []
        with mock.patch.object(mn, "http_json",
                               side_effect=lambda *a, **k: called.append(a) or (200, {})):
            res = mn.mention("peer", "ping", ROOM, HS, gate=None, agents=self.AGENTS,
                             reply_to="root")
        self.assertFalse(res["ok"])
        self.assertEqual(called, [])


class CitationCLITests(EnvCase):
    def test_say_flag_reaches_the_function(self):
        cap = {}
        with mock.patch.object(room_ops._say, "say",
                               side_effect=lambda *a, **k: (cap.update(kw=k), {"ok": True})[1]):
            with contextlib.redirect_stdout(io.StringIO()):
                room_ops._main(["say", ROOM, "hi", "--reply-to", EV])
        self.assertEqual(cap["kw"], {"reply_to": EV})

    def test_mention_flag_reaches_the_function(self):
        cap = {}
        with mock.patch.object(room_ops._mention, "mention",
                               side_effect=lambda *a, **k: (cap.update(kw=k), {"ok": True})[1]):
            with contextlib.redirect_stdout(io.StringIO()):
                room_ops._main(["mention", "peer", "ping", ROOM, "--reply-to", EV])
        self.assertEqual(cap["kw"], {"reply_to": EV})

    def test_help_says_a_citation_is_not_a_thread(self):
        # The surface a caller hits first must carry the limitation, not only
        # the module docstring and the skill doc.
        for verb in ("say", "mention"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                room_ops._main([verb, "--help"])
            text = out.getvalue()
            self.assertIn("CITATION", text)
            self.assertIn("does NOT put", text)


class ChannelEnvLocatorTests(unittest.TestCase):
    """Exercise the REAL _channel_env_file() — every other test shadows it.

    Without this, drifting the path segments or moving claude_home_path leaves the
    tier permanently unresolved and the suite green, reproducing the exact symptom
    this feature removes: "no gateway configured" with a credential on disk.
    """

    def test_locates_the_channel_env_under_claude_config_dir(self):
        d = tempfile.mkdtemp()
        ch = os.path.join(d, "channels", "ag2space")
        os.makedirs(ch)
        env = os.path.join(ch, ".env")
        with open(env, "w") as fh:
            fh.write("REMOTE_TASK_TOKEN=x\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            got = _gateway._channel_env_file()
        self.assertIsNotNone(got, "real locator returned None for a file that exists")
        self.assertEqual(os.path.realpath(str(got)), os.path.realpath(env))
        os.remove(env)
        for p in (ch, os.path.dirname(ch), d):
            os.rmdir(p)

    def test_returns_none_when_the_file_is_absent(self):
        d = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, d)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            self.assertIsNone(_gateway._channel_env_file())


class ChannelEnvTierTests(EnvCase):
    """The channel `.env` tier: env -> channels/ag2space/.env -> vault.

    Regression: the desktop-spawned core's supervisor uses a fixed env whitelist,
    so REMOTE_TASK_* never reach the process even though the channel .env holds
    them. gateway() read env then vault and skipped the file, reporting
    "no gateway configured" while a working credential sat on disk.
    """

    def _write_env(self, body):
        d = tempfile.mkdtemp()
        f = os.path.join(d, ".env")
        with open(f, "w") as fh:
            fh.write(body)
        self.addCleanup(lambda: (os.remove(f), os.rmdir(d)))
        return f

    def test_token_and_url_come_from_channel_env(self):
        f = self._write_env("REMOTE_TASK_URL=https://gw.example\n"
                            "REMOTE_TASK_TOKEN=sekret\n")
        with mock.patch.object(_gateway, "_channel_env_file",
                               lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(base, "https://gw.example")
        self.assertEqual(headers.get("Authorization"), "Bearer sekret")

    def test_process_env_still_wins_over_the_file(self):
        f = self._write_env("REMOTE_TASK_URL=https://file.example\n"
                            "REMOTE_TASK_TOKEN=from-file\n")
        os.environ["GATEWAY_URL"] = "https://env.example"
        os.environ["GATEWAY_TOKEN"] = "from-env"
        with mock.patch.object(_gateway, "_channel_env_file",
                               lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(base, "https://env.example")
        self.assertEqual(headers.get("Authorization"), "Bearer from-env")

    def test_file_wins_over_vault(self):
        f = self._write_env("REMOTE_TASK_URL=https://file.example\n"
                            "REMOTE_TASK_TOKEN=from-file\n")
        _VAULT_STORE["REMOTE_TASK_TOKEN"] = "https://vault.example|from-vault"
        self.addCleanup(_VAULT_STORE.pop, "REMOTE_TASK_TOKEN", None)
        with mock.patch.object(_gateway, "_channel_env_file",
                               lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(headers.get("Authorization"), "Bearer from-file")
        self.assertEqual(base, "https://file.example")

    def test_absent_file_leaves_prior_behaviour_untouched(self):
        # _channel_env_file() -> None is EnvCase's default shadow.
        base, headers = _gateway.gateway()
        self.assertEqual(base, "")
        self.assertNotIn("Authorization", headers)

    def test_legacy_ag2_remote_token_alias_resolves(self):
        # Parity with the vault tier, which has always tried this legacy name.
        # Old installs carry the token under it; without this they stay broken.
        f = self._write_env("REMOTE_TASK_URL=https://gw.example\n"
                            "AG2_REMOTE_TOKEN=legacy-secret\n")
        with mock.patch.object(_gateway, "_channel_env_file",
                               lambda: f):
            base, headers = _gateway.gateway()
        self.assertEqual(headers.get("Authorization"), "Bearer legacy-secret")
        self.assertEqual(base, "https://gw.example")

    def test_missing_path_is_not_an_error(self):
        with mock.patch.object(_gateway, "_channel_env_file",
                               lambda: "/nope/does/not/exist/.env"):
            base, _ = _gateway.gateway()
        self.assertEqual(base, "")

class DegradeReasonFromTests(unittest.TestCase):
    """The server's own error text reaches the caller; auth statuses keep the local diagnosis in front."""

    @staticmethod
    def _err(code, body):
        import io
        from urllib.error import HTTPError
        return HTTPError("http://gw/v1/room", code, "x", {}, io.BytesIO(body.encode()))

    def test_403_appends_the_servers_reason(self):
        r = _gateway.degrade_reason_from(self._err(403, '{"error": "permission denied: platform grant events.subscribe missing"}'))
        self.assertIn("not a joined member", r)
        self.assertIn("events.subscribe missing", r)

    def test_401_appends_too_and_keeps_the_token_diagnosis(self):
        r = _gateway.degrade_reason_from(self._err(401, '{"error": "denied - agent not a joined member"}'))
        self.assertTrue(r.startswith("auth failed"))
        self.assertIn("server said", r)

    def test_non_auth_status_uses_the_servers_text(self):
        self.assertEqual(_gateway.degrade_reason_from(self._err(404, '{"error": "roadmap/plan.md not found"}')),
                         "roadmap/plan.md not found")

    def test_no_body_falls_back_to_the_status_text(self):
        self.assertEqual(_gateway.degrade_reason_from(self._err(403, "")), _gateway.degrade_reason(403))


if __name__ == "__main__":
    unittest.main(verbosity=2)
