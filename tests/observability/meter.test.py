"""Twin of meter.test.ts."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability import meter, obs  # noqa: E402

ENV = ("SUTANDO_WORKSPACE", "SUTANDO_TENANT_ID", "SUTANDO_TENANT_MODE", "SUTANDO_METERING_FSYNC")


class Capture:
    type = "capture"

    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, ev: dict) -> None:
        self.events.append(ev)


def base_usage() -> dict:
    return {
        "source": "core-cli",
        "actor": {"user_id": "u1", "channel": "claude-code", "access_tier": "owner"},
        "meter": "claude.tokens",
        "quantity": 1840,
        "unit": "tokens",
        "provider": "anthropic",
        "provider_ref": "req_01H",
        "attrs": {"model": "claude-opus-4-8", "input_tokens": 1500, "output_tokens": 340},
    }


class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.pop(k, None) for k in ENV}
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.ws)
        obs.reset_sinks()
        self.cap = Capture()
        obs.register_sink(self.cap)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        obs.reset_sinks()
        self._tmp.cleanup()

    def _read_ledger(self, rec: dict) -> list[str]:
        path = meter.ledger_path(rec["ts"] * 1000, self.ws)
        if not path.exists():
            return []
        return [l for l in path.read_text().split("\n") if l]

    def test_byte_exact_line(self) -> None:
        rec = meter.record({**base_usage(), "usage_id": "ux_FIXED0000000000000000000", "ts": 1_717_900_000.12})
        lines = self._read_ledger(rec)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(json.loads(lines[0]), rec)
        self.assertRegex(str(meter.ledger_path(rec["ts"] * 1000, self.ws)), r"/data/usage/usage-\d{4}-\d{2}-\d{2}\.jsonl$")

    def test_stamps_and_defaults(self) -> None:
        u = base_usage()
        u.pop("provider_ref")
        rec = meter.record(u)
        self.assertEqual(rec["schema"], 1)
        self.assertRegex(rec["usage_id"], r"^ux_")
        self.assertRegex(rec["trace_id"], r"^tr_")
        self.assertIsInstance(rec["ts"], float)
        self.assertIsNone(rec["provider_ref"])

    def test_supplied_usage_id_verbatim(self) -> None:
        a = meter.record({**base_usage(), "usage_id": "ux_SAME0000000000000000000", "ts": 1_717_900_000})
        meter.record({**base_usage(), "usage_id": "ux_SAME0000000000000000000", "ts": 1_717_900_000})
        lines = self._read_ledger(a)
        self.assertEqual(len(lines), 2)
        for l in lines:
            self.assertEqual(json.loads(l)["usage_id"], "ux_SAME0000000000000000000")

    def test_auto_mint_distinct(self) -> None:
        a = meter.record(base_usage())
        b = meter.record(base_usage())
        self.assertNotEqual(a["usage_id"], b["usage_id"])

    def test_advisory_event_emitted(self) -> None:
        rec = meter.record(base_usage())
        adv = next((e for e in self.cap.events if e.get("kind") == "usage.recorded"), None)
        self.assertIsNotNone(adv)
        self.assertEqual(adv["source"], "core-cli")
        self.assertEqual(adv["data"]["usage_id"], rec["usage_id"])
        self.assertEqual(adv["usage"]["input_tokens"], 1500)

    def test_tenant_default_and_override(self) -> None:
        self.assertIsNone(meter.record(base_usage())["tenant_id"])
        os.environ["SUTANDO_TENANT_ID"] = "acct_9"
        self.assertEqual(meter.record(base_usage())["tenant_id"], "acct_9")
        self.assertEqual(meter.record({**base_usage(), "tenant_id": "explicit"})["tenant_id"], "explicit")


if __name__ == "__main__":
    unittest.main()
