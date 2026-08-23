#!/usr/bin/env python3
"""B1 identity contract: vectors, purity (R1/R3), injectivity, round-trip.

Run: python3 packages/ag2-sparrow/tests/test_identity.py   (stdlib only)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

from ag2_sparrow import identity as I  # noqa: E402
from ag2_sparrow.identity import derive, legacy, serialization  # noqa: E402

VECTORS = json.loads(
    (PKG_ROOT / "ag2_sparrow" / "identity" / "vectors.json").read_text())

_KINDS = {
    "ingress_task_id": lambda a: I.ingress_task_id(*a),
    "delivery_id": lambda a: I.delivery_id(I.TaskId(a[0]), a[1]),
    "legacy_delivery_id": lambda a: I.legacy_delivery_id(*a),
    "resend_delivery_id": lambda a: I.resend_delivery_id(
        serialization.parse_delivery_id(a[0]), a[1]),
    "attempt_id": lambda a: I.attempt_id(
        serialization.parse_delivery_id(a[0]), a[1]),
    "idempotency_key": lambda a: I.idempotency_key(I.TaskId(a[0]), a[1]),
    "incarnation_id_from": lambda a: I.incarnation_id_from(*a),
}


class Vectors(unittest.TestCase):
    def test_every_vector_matches_exactly(self):
        for v in VECTORS["vectors"]:
            got = _KINDS[v["kind"]](v["args"])
            self.assertEqual(got.value, v["expected"],
                             f"{v['kind']}{tuple(v['args'])}")

    def test_derivation_is_deterministic(self):
        for v in VECTORS["vectors"]:
            a = _KINDS[v["kind"]](v["args"])
            b = _KINDS[v["kind"]](v["args"])
            self.assertEqual(a, b)


class PurityRatchet(unittest.TestCase):
    """R1/R3 at the source level: the derivation module cannot reach a clock,
    the process table, or entropy, so no identity can be time- or pid-seeded."""

    def test_derive_module_has_no_impure_imports(self):
        src = (PKG_ROOT / "ag2_sparrow" / "identity" / "derive.py").read_text()
        for forbidden in ("import time", "import os", "import random",
                          "import uuid", "import datetime", "from time",
                          "from os", "from random", "from uuid",
                          "from datetime"):
            self.assertNotIn(forbidden, src,
                             f"derive.py must not use {forbidden!r} (R1/R3)")


class Injectivity(unittest.TestCase):
    def test_separator_in_components_cannot_collide(self):
        a = I.legacy_delivery_id("a@b", "c")
        b = I.legacy_delivery_id("a", "b@c")
        self.assertNotEqual(a, b)
        c = I.ingress_task_id("x~y", "z")
        d = I.ingress_task_id("x", "y~z")
        self.assertNotEqual(c, d)

    def test_escape_round_trip_distinctness(self):
        raws = ["plain", "with@at", "with~tilde", "with#hash", "with%pct",
                "with:colon", "with+plus", "with space"]
        escaped = [derive.escape_component(r) for r in raws]
        self.assertEqual(len(set(escaped)), len(raws))


class LineageAndOrdering(unittest.TestCase):
    def test_resend_is_new_id_with_lineage_and_same_effect_key(self):
        task = I.TaskId("task-abc")
        d1 = I.delivery_id(task, "gw")
        d2 = I.resend_delivery_id(d1, 1)
        self.assertNotEqual(d1, d2)
        self.assertTrue(d2.value.startswith(d1.value))
        self.assertEqual(I.idempotency_key(task, "gw"),
                         I.idempotency_key(task, "gw"))

    def test_ordinals_are_one_based(self):
        d = I.delivery_id(I.TaskId("task-abc"), "gw")
        with self.assertRaises(ValueError):
            I.attempt_id(d, 0)
        with self.assertRaises(ValueError):
            I.resend_delivery_id(d, 0)


class RoundTrip(unittest.TestCase):
    def test_parse_accepts_every_derived_shape(self):
        task = I.TaskId("task-1~x")
        d = I.delivery_id(task, "gw")
        r = I.resend_delivery_id(d, 2)
        a = I.attempt_id(r, 1)
        k = I.idempotency_key(task, "gw")
        n = I.incarnation_id_from("w", 1, 2)
        self.assertEqual(serialization.parse_delivery_id(d.value), d)
        self.assertEqual(serialization.parse_delivery_id(r.value), r)
        self.assertEqual(serialization.parse_attempt_id(a.value), a)
        self.assertEqual(serialization.parse_idempotency_key(k.value), k)
        self.assertEqual(serialization.parse_task_id(task.value), task)
        self.assertEqual(serialization.parse_incarnation_id(n.value), n)

    def test_parse_rejects_foreign_strings(self):
        for bad in ("", "task-x", "d:missing-boundary", "x@y", "d:a@b#a0",
                    "legacy:a@b+r0"):
            with self.assertRaises(ValueError, msg=bad):
                serialization.parse_delivery_id(bad)

    def test_record_embed_extract_round_trip(self):
        task = I.TaskId("task-9")
        d = I.delivery_id(task, "gw")
        fields = serialization.to_record_fields(delivery_id=d, task_id=task)
        record = {"attempts": 2, **fields}
        parsed = serialization.identity_fields_from_record(record)
        self.assertEqual(parsed["delivery_id"], d)
        self.assertEqual(parsed["task_id"], task)
        self.assertNotIn("attempt_id", parsed)

    def test_record_embed_rejects_wrong_type(self):
        with self.assertRaises(TypeError):
            serialization.to_record_fields(delivery_id=I.TaskId("task-9"))


class LegacyAdapter(unittest.TestCase):
    def test_outbox_conflation_maps_purely(self):
        m1 = legacy.from_outbox_item("task-1712000000001", "discord-dm")
        m2 = legacy.from_outbox_item("task-1712000000001", "discord-dm")
        self.assertEqual(m1, m2)
        self.assertEqual(m1.delivery_id.value,
                         "legacy:task-1712000000001@discord-dm")
        self.assertEqual(
            m1.idempotency_key,
            I.idempotency_key(I.TaskId("task-1712000000001"), "discord-dm"))

    def test_sentinel_and_non_task_item_shapes(self):
        s = legacy.from_delivered_sentinel("task-7", "discord-dm")
        self.assertEqual(s.delivery_id.value, "legacy:task-7@discord-dm")
        fence = legacy.from_outbox_item("proactive-9.txt#123", "discord-proactive")
        self.assertEqual(fence.delivery_id.value,
                         "legacy:proactive-9.txt%23123@discord-proactive")


if __name__ == "__main__":
    unittest.main()
