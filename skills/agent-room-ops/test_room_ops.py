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
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
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
        lines = open(paths[0]).read().split("\n")
        self.assertEqual(lines[0], f"id: {base[:-4]}")
        self.assertTrue(lines[1].startswith("timestamp: ") and lines[1].endswith("Z"))
        # Origin must be explicit at a glance: the [taskify] marker leads the
        # task line and the promoted-from suffix names the room.
        self.assertTrue(lines[2].startswith("task: [taskify] "))
        self.assertIn("2 messages, 1 reaction", lines[2])
        self.assertIn(f"(promoted from 3 subscribed events in {ROOM})", lines[2])
        self.assertEqual(lines[3], "source: events-promotion")
        self.assertEqual(lines[4], f"channel_id: {ROOM}")
        self.assertEqual(lines[5], "priority: low")          # never outranks humans
        self.assertEqual(lines[6], "model_hint: efficient")  # cheap-model eligible
        self.assertEqual(lines[7], "access_tier: ambient")  # trust boundary: never owner
        self.assertEqual(lines[8], "")
        prov = json.loads(lines[9].split("provenance: ", 1)[1])
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
        self.assertNotEqual(p1, p2)  # same-ms collision guard: distinct files
        self.assertIn("1 message", open(p1).read())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
