#!/usr/bin/env python3
"""`read-quota.py` must not present another session's numbers as this one's budget.

Run: python3 tests/quota-read-not-routed.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"

_BURN = {
    "burn_rate_pct_per_pass": 1.25,
    "burn_samples": 9,
    "binding_window": "5h",
    "estimated_passes_left": 40.0,
    "estimated_minutes_left": 200,
    "unforecast_windows": [],
}


def _load_module(workspace: Path):
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    sys.modules.pop("read_quota_under_test", None)
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "quota-state.json").write_text(json.dumps({"headers": {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.12",
        "anthropic-ratelimit-unified-7d-utilization": "0.55",
    }}))
    spec = importlib.util.spec_from_file_location("read_quota_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class NotRoutedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-routed-"))
        self._env = dict(os.environ)
        self.mod = _load_module(self.tmp)
        self._real_update_burn_rate = self.mod._update_burn_rate
        self.mod._update_burn_rate = lambda *a, **k: dict(_BURN)

    def _use_real_burn(self) -> None:
        """History cases need the real writer; the stub never touches disk."""
        self.mod._update_burn_rate = self._real_update_burn_rate

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, routed, argv=("read-quota.py",)) -> str:
        if routed is None:
            pass                      # caller set ANTHROPIC_BASE_URL itself
        elif routed:
            os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:7846"
        else:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        buf = io.StringIO()
        old = sys.argv
        sys.argv = list(argv)
        try:
            with contextlib.redirect_stdout(buf):
                self.mod.main()
        finally:
            sys.argv = old
        return buf.getvalue()

    # --- the banner ----------------------------------------------------------

    def test_unrouted_says_the_numbers_are_not_this_session(self) -> None:
        out = self._run(routed=False)
        self.assertIn("NOT ROUTED", out)
        self.assertIn("ANTHROPIC_BASE_URL", out)

    def test_routed_prints_no_banner(self) -> None:
        """Control: without this, the banner test passes on a always-print bug."""
        out = self._run(routed=True)
        self.assertNotIn("NOT ROUTED", out)

    # --- the forecast --------------------------------------------------------

    def test_unrouted_suppresses_the_forecast(self) -> None:
        out = self._run(routed=False)
        self.assertIn("SUPPRESSED", out)
        self.assertNotIn("Burn rate:", out)

    def test_routed_still_prints_the_forecast(self) -> None:
        """Control: suppression must come from routing, not from losing the burn."""
        out = self._run(routed=True)
        self.assertIn("Burn rate:", out)
        self.assertNotIn("SUPPRESSED", out)

    # --- the machine-readable half -------------------------------------------

    def test_json_carries_routed_false(self) -> None:
        out = self._run(routed=False, argv=("read-quota.py", "--json"))
        self.assertIs(json.loads(out)["routed"], False)

    def test_json_carries_routed_true(self) -> None:
        out = self._run(routed=True, argv=("read-quota.py", "--json"))
        self.assertIs(json.loads(out)["routed"], True)

    # --- the DECISION surface, which is what machine callers read ------------

    def _gate_exit(self, routed: bool) -> int:
        if routed:
            os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:7846"
        else:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        old = sys.argv
        sys.argv = ["read-quota.py", "--gate"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.mod.main()
        except SystemExit as e:
            return int(e.code or 0)
        finally:
            sys.argv = old
        return 0

    def test_unrouted_json_is_not_available(self) -> None:
        """The banner is prose; this is the field a script branches on."""
        out = json.loads(self._run(routed=False, argv=("read-quota.py", "--json")))
        self.assertIs(out["available"], False)
        self.assertEqual(out["unavailable_reason"], "not-routed")

    def test_routed_json_is_available(self) -> None:
        """Control: unavailability must come from routing, not from the fixture."""
        out = json.loads(self._run(routed=True, argv=("read-quota.py", "--json")))
        self.assertIs(out["available"], True)
        self.assertIsNone(out["unavailable_reason"])

    def test_unrouted_gate_exits_nonzero(self) -> None:
        self.assertNotEqual(self._gate_exit(routed=False), 0)

    def test_routed_gate_exits_zero(self) -> None:
        """Control: the gate still passes for a session that IS routed."""
        self.assertEqual(self._gate_exit(routed=True), 0)

    # --- the DURABLE side effect: history must not record a foreign reading ---

    def _hist(self):
        f = self.tmp / "state" / "quota-burn-history.json"
        return f.read_bytes() if f.is_file() else None

    def test_unrouted_read_leaves_burn_history_untouched(self) -> None:
        """A folded EWMA sample outlives the banner that flagged it, and the
        next routed read prints a forecast built from it with no banner."""
        self._use_real_burn()
        before = self._hist()
        self._run(routed=False)
        self.assertEqual(self._hist(), before)

    def test_routed_read_does_advance_burn_history(self) -> None:
        """Control: the skip must come from routing, not from history being dead."""
        self._use_real_burn()
        before = self._hist()
        self._run(routed=True)
        self.assertNotEqual(self._hist(), before)

    def test_unrouted_json_also_leaves_history_untouched(self) -> None:
        """--json is the machine path and takes the same non-gate branch."""
        self._use_real_burn()
        before = self._hist()
        self._run(routed=False, argv=("read-quota.py", "--json"))
        self.assertEqual(self._hist(), before)

    def test_unrouted_still_says_the_forecast_is_suppressed(self) -> None:
        """Skipping the computation must not silently drop the explanation."""
        self.assertIn("SUPPRESSED", self._run(routed=False))

    # --- destination, not presence -------------------------------------------

    def test_arbitrary_nonempty_base_url_is_not_routed(self) -> None:
        """The launcher preserves a caller-set URL verbatim, so presence proves nothing."""
        os.environ["ANTHROPIC_BASE_URL"] = "http://example.test:1"
        out = json.loads(self._run(routed=None, argv=("read-quota.py", "--json")))
        self.assertIs(out["available"], False)
        self.assertEqual(out["unavailable_reason"], "not-routed")

    def test_wrong_port_on_localhost_is_not_routed(self) -> None:
        self.assertFalse(self.mod._points_at_credential_proxy("http://localhost:9999"))

    def test_the_real_proxy_url_is_routed(self) -> None:
        """Control: the destination check must still accept the launcher's own URL."""
        for u in ("http://localhost:7846", "http://127.0.0.1:7846"):
            self.assertTrue(self.mod._points_at_credential_proxy(u), u)

    def test_unparseable_base_url_fails_closed(self) -> None:
        self.assertFalse(self.mod._points_at_credential_proxy("garbage"))

    def test_urlparse_raising_fails_closed(self) -> None:
        """An unterminated IPv6 bracket makes urlparse itself raise."""
        for u in ("http://[", "http://[::1"):
            self.assertFalse(self.mod._points_at_credential_proxy(u), u)

    def test_wrong_scheme_is_not_routed(self) -> None:
        """The proxy speaks plain HTTP; https/ftp on the same host:port do not reach it."""
        for u in ("https://localhost:7846", "ftp://localhost:7846"):
            self.assertFalse(self.mod._points_at_credential_proxy(u), u)

    def test_schemeless_authority_still_routed(self) -> None:
        """Control: the scheme check must not reject the launcher's own forms."""
        self.assertTrue(self.mod._points_at_credential_proxy("localhost:7846"))

    # --- the human diagnosis must match the actual cause ----------------------

    def test_set_but_elsewhere_names_the_url_not_unset(self) -> None:
        """Saying "is unset" about a set variable is a false diagnosis."""
        os.environ["ANTHROPIC_BASE_URL"] = "http://example.test:1"
        out = self._run(routed=None)
        self.assertIn("http://example.test:1", out)
        self.assertNotIn("is unset", out)
        self.assertNotIn("relaunch the proxy, then restart", out)

    def test_mismatch_diagnosis_redacts_credentials(self) -> None:
        """This line reaches shared self-diagnose bundles, not just a terminal."""
        os.environ["ANTHROPIC_BASE_URL"] = (
            "https://user:super-secret@example.test/v1?token=also-secret")
        out = self._run(routed=None)
        for secret in ("super-secret", "also-secret", "token=", "user:"):
            self.assertNotIn(secret, out, secret)
        self.assertIn("example.test", out)      # still diagnostic, just not raw

    def test_redaction_keeps_scheme_host_port(self) -> None:
        """Control: redaction must not flatten every URL to the same string."""
        self.assertEqual(self.mod._redacted_endpoint("http://h.test:9/a?b=c"), "http://h.test:9")
        self.assertEqual(self.mod._redacted_endpoint("https://x.test"), "https://x.test")

    def test_redaction_fails_closed_on_unparseable(self) -> None:
        """Both raising paths: urlparse itself, and a bad `.port` inside the try."""
        for u in ("http://[", "http://[::1", "http://localhost:abc"):
            self.assertEqual(self.mod._redacted_endpoint(u), "an unparseable endpoint", u)

    def test_redaction_fails_closed_when_no_host(self) -> None:
        """Parses fine but carries no authority — say nothing rather than echo a path."""
        for u in ("http:///only/path", "http://:7846", "/just/a/path"):
            self.assertEqual(self.mod._redacted_endpoint(u), "another endpoint", u)

    def test_genuinely_unset_still_says_unset(self) -> None:
        """Control: the original diagnosis must survive for the case it describes."""
        out = self._run(routed=False)
        self.assertIn("is unset", out)
        self.assertIn("has a listener at launch", out)

    def test_bad_port_fails_closed(self) -> None:
        """`.port` raises on a non-numeric or out-of-range authority port."""
        for u in ("http://localhost:abc", "http://localhost:99999"):
            self.assertFalse(self.mod._points_at_credential_proxy(u), u)

    # --- the window the numbers still describe -------------------------------

    def test_unrouted_still_reports_the_raw_windows(self) -> None:
        """Not-routed is a provenance warning, not a reason to hide the data."""
        out = self._run(routed=False)
        self.assertIn("5h window", out)
        self.assertIn("7d window", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
