#!/usr/bin/env python3
"""B1 identity contract: vectors, purity (R1/R3), injectivity, round-trip.

Run: python3 packages/ag2-sparrow/tests/test_identity.py   (stdlib only)
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

from ag2_sparrow import identity as I  # noqa: E402
from ag2_sparrow.identity import derive, legacy, serialization  # noqa: E402

from ag2_sparrow.delivery_core import backend_a, contract, core  # noqa: E402

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
    "idempotency_key": lambda a: I.idempotency_key(I.TaskId(a[0]), *a[1:]),
    "legacy_idempotency_key": lambda a: I.legacy_idempotency_key(*a),
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

    def test_escape_is_fixed_width_over_the_full_alphabet(self):
        # The review's collision pair: variable-width ord-encoding collapsed
        # U+200B with " 0B". Fixed-width UTF-8 byte encoding keeps them apart.
        self.assertEqual(derive.escape_component("\u200b"), "%E2%80%8B")
        self.assertEqual(derive.escape_component(" 0B"), "%200B")
        self.assertNotEqual(I.ingress_task_id("a", "\u200b"),
                            I.ingress_task_id("a", " 0B"))
        hostile = ["\u200b", " 0B", "Ā", "�", "💥", "task",
                   "%20", " ", "\t", "\x00", "a/b", "a\\b", "é", "%E2%80%8B"]
        escaped = [derive.escape_component(r) for r in hostile]
        self.assertEqual(len(set(escaped)), len(hostile))

    def test_escape_output_and_typed_values_are_printable_ascii(self):
        for raw in ["💥", "\u200b", "héllo", "\x01", "ключ"]:
            esc = derive.escape_component(raw)
            self.assertTrue(all(0x21 <= ord(c) <= 0x7E for c in esc), esc)
            t = I.ingress_task_id(raw, raw)
            self.assertTrue(all(0x21 <= ord(c) <= 0x7E for c in t.value))

    def test_types_reject_non_ascii_and_whitespace(self):
        for bad in ("task-💥", "task-\u200b", "task-a b", "task-a\tb",
                    "task-a/b", "task-a\\b"):
            with self.assertRaises(ValueError, msg=bad):
                I.TaskId(bad)


class LineageAndOrdering(unittest.TestCase):
    def test_resend_is_new_id_with_lineage_and_a_new_effect_key(self):
        # A re-send is a NEW side-effect: its epoch advances and its key is
        # new; epoch 0 stays stable across retries (R2's minting row).
        task = I.TaskId("task-abc")
        d1 = I.delivery_id(task, "gw")
        d2 = I.resend_delivery_id(d1, 1)
        self.assertNotEqual(d1, d2)
        self.assertTrue(d2.value.startswith(d1.value))
        k0 = I.idempotency_key(task, "gw")
        self.assertEqual(k0, I.idempotency_key(task, "gw", 0))
        k1 = I.idempotency_key(task, "gw", 1)
        self.assertNotEqual(k0, k1)
        self.assertEqual(k1.value, "e:task-abc@gw+r1")
        self.assertNotEqual(k1, I.idempotency_key(task, "gw", 2))
        with self.assertRaises(ValueError):
            I.idempotency_key(task, "gw", -1)
        with self.assertRaises(TypeError):
            I.idempotency_key(task, "gw", True)
        m0 = legacy.from_delivered_sentinel("task-abc", "gw")
        m1 = legacy.from_delivered_sentinel("task-abc", "gw", 1)
        self.assertEqual(m0.idempotency_key, k0)
        self.assertEqual(m1.idempotency_key, k1)

    def test_ordinals_are_one_based(self):
        d = I.delivery_id(I.TaskId("task-abc"), "gw")
        with self.assertRaises(ValueError):
            I.attempt_id(d, 0)
        with self.assertRaises(ValueError):
            I.resend_delivery_id(d, 0)


class LegacyKeyDomain(unittest.TestCase):
    def test_every_string_the_shipped_formatter_accepted_is_a_key(self):
        # main formats f"{item_id}#{epoch}" for every str, the empty one
        # included; narrowing it strands a published item on every drain.
        for item in ("", "a/b", "a\nb", "x" * 500):
            k = I.legacy_idempotency_key(item, 0)
            self.assertEqual(k.value, f"{item}#0")
            self.assertEqual(serialization.parse_idempotency_key(k.value), k)
        with self.assertRaises(TypeError):
            I.legacy_idempotency_key(None, 0)

    def test_the_epoch_spelling_is_canonical(self):
        # legacy_idempotency_key emits task-X#0, never task-X#00: a spelling the
        # constructor cannot produce must not parse as a shipped key.
        self.assertEqual(I.IdempotencyKey("task-X#0").value, "task-X#0")
        self.assertEqual(I.IdempotencyKey("#0").value, "#0")
        for bad in ("task-X#00", "task-X#01", "task-X#", "task-X#-1"):
            with self.assertRaises(ValueError, msg=bad):
                I.IdempotencyKey(bad)


class RoundTrip(unittest.TestCase):
    def test_parse_accepts_every_derived_shape(self):
        task = I.TaskId("task-1~x")
        d = I.delivery_id(task, "gw")
        r = I.resend_delivery_id(d, 2)
        a = I.attempt_id(r, 1)
        k = I.idempotency_key(task, "gw")
        k3 = I.idempotency_key(task, "gw", 3)
        n = I.incarnation_id_from("w", 1, 2)
        self.assertEqual(serialization.parse_delivery_id(d.value), d)
        self.assertEqual(serialization.parse_delivery_id(r.value), r)
        self.assertEqual(serialization.parse_attempt_id(a.value), a)
        self.assertEqual(serialization.parse_idempotency_key(k.value), k)
        self.assertEqual(serialization.parse_idempotency_key(k3.value), k3)
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

    def test_record_embed_rejects_a_misspelled_field_even_when_none(self):
        """Name validation must precede the None short-circuit, or an
        optional-field typo silently discards identity metadata."""
        for bad in ("delviery_id", "taks_id", "nonsense"):
            with self.assertRaises(ValueError, msg=bad):
                serialization.to_record_fields(**{bad: None})
        # A KNOWN field set to None is still legitimately omitted.
        self.assertEqual(serialization.to_record_fields(delivery_id=None), {})


class OneCanonicalGrammar(unittest.TestCase):
    """Types, constructors, parsers, and the record serializer share ONE
    grammar: what a constructor cannot produce, nothing accepts."""

    def test_parsers_reject_constructor_impossible_components(self):
        cases = {
            serialization.parse_delivery_id: [
                "d:@", "d:@b", "d:a@", "d:@#a1", "d:a@b+r0", "d:a%2@b",
                "d:a%zz@b", "d:a%e2@b", "d:a@b💥", "task-x+r1"],
            serialization.parse_idempotency_key: [
                "e:@", "e:@b", "e:a@", "e:a%2@b", "e:a@b\u200bx"],
            serialization.parse_incarnation_id: [
                ":0:0", "w:0", "w:-1:0", "w:0:0x1", "💥:0:0"],
            serialization.parse_attempt_id: [
                "d:@#a1", "d:a@b#a0", "d:a@b#a", "d:a@b"],
            serialization.parse_task_id: [
                "not-a-task", "task-", "task", "task-a b", "task-💥"],
        }
        for parse, values in cases.items():
            for bad in values:
                with self.assertRaises(ValueError,
                                       msg=f"{parse.__name__}({bad!r})"):
                    parse(bad)

    def test_parsers_reject_non_canonical_escapes(self):
        """The grammar admits any %XX; only a constructor's own output is
        canonical. `%41` is a safe 'A' spelled the long way, `%FF` is not
        UTF-8, and `01` is an ordinal no derivation emits."""
        rejects = {
            serialization.parse_delivery_id: ["d:%41@gw", "d:%FF@gw", "d:%C3%28@gw"],
            serialization.parse_attempt_id: ["d:%41@gw#a1"],
            serialization.parse_idempotency_key: ["e:%41@gw"],
            serialization.parse_incarnation_id: ["%41:1:2", "w:01:2", "w:1:02"],
        }
        for parse, values in rejects.items():
            for bad in values:
                with self.assertRaises(ValueError, msg=f"{parse.__name__}({bad!r})"):
                    parse(bad)
        # Positive controls: reserved and non-ASCII escapes a constructor DOES
        # emit, and ordinal zero, stay accepted.
        for parse, good in [(serialization.parse_delivery_id, "d:%40@gw"),
                            (serialization.parse_delivery_id, "d:%E2%80%8B@gw"),
                            (serialization.parse_idempotency_key, "e:%40@gw"),
                            (serialization.parse_delivery_id, "d:a@%25"),
                            (serialization.parse_incarnation_id, "w:0:0"),
                            (serialization.parse_attempt_id, "d:%40@gw#a1")]:
            parse(good)

    def test_every_escaped_component_is_canonical_and_nothing_else_is(self):
        from ag2_sparrow.identity.escape import is_canonical_component
        for raw in ("plain", "a b", "a/b", "@#%+~:", "t\u00e9st", "\u200b", "x" * 50):
            self.assertTrue(is_canonical_component(I.escape_component(raw)), raw)
        for bad in ("", "%41", "%ff", "%FF", "%", "a%", "%2", "a%zz"):
            self.assertFalse(is_canonical_component(bad), bad)

    def test_type_construction_matches_parse_acceptance(self):
        with self.assertRaises(ValueError):
            I.TaskId("not-a-task")
        with self.assertRaises(ValueError):
            I.DeliveryId("task-x+r1")
        with self.assertRaises(ValueError):
            I.AttemptId("d:a@b#aTrue")

    def test_every_constructed_value_survives_record_round_trip(self):
        task = I.ingress_task_id("inst~7", "ev@nt:1")
        d = I.resend_delivery_id(I.delivery_id(task, "gw"), 3)
        fields = serialization.to_record_fields(
            task_id=task, delivery_id=d,
            attempt_id=I.attempt_id(d, 2),
            idempotency_key=I.idempotency_key(task, "gw"),
            incarnation_id=I.incarnation_id_from("w", 9, 8))
        parsed = serialization.identity_fields_from_record(dict(fields))
        self.assertEqual({k: v.value for k, v in parsed.items()}, fields)

    def test_constructors_enforce_exact_input_types(self):
        task = I.TaskId("task-x")
        d = I.delivery_id(task, "gw")
        with self.assertRaises(TypeError):
            I.resend_delivery_id(task, 1)  # TaskId is not a DeliveryId
        with self.assertRaises(TypeError):
            I.attempt_id(d, True)
        with self.assertRaises(TypeError):
            I.attempt_id(d, 1.5)
        with self.assertRaises(TypeError):
            I.resend_delivery_id(d, "1")
        with self.assertRaises(TypeError):
            I.delivery_id("task-x", "gw")
        with self.assertRaises(TypeError):
            I.incarnation_id_from("w", True, 2)


class LegacyAdapter(unittest.TestCase):
    def test_outbox_conflation_maps_purely(self):
        m1 = legacy.from_outbox_item("task-1712000000001", "discord-dm")
        m2 = legacy.from_outbox_item("task-1712000000001", "discord-dm")
        self.assertEqual(m1, m2)
        self.assertEqual(m1.delivery_id.value,
                         "legacy:task-1712000000001@discord-dm")

    def test_outbox_mapping_preserves_the_shipped_provider_key(self):
        """This assertion is INVERTED from the one this suite shipped with.
        It used to require the canonical e:<task>@<boundary> here, which is
        precisely the key change that would re-offer a parked item under a
        name the provider cannot dedupe."""
        m = legacy.from_outbox_item("task-1712000000001", "discord-dm")
        self.assertEqual(m.idempotency_key.value,
                         core.idempotency_key("task-1712000000001"))
        self.assertNotEqual(
            m.idempotency_key,
            I.idempotency_key(I.TaskId("task-1712000000001"), "discord-dm"))

    def test_boundary_does_not_enter_the_preserved_key(self):
        # The shipped key never carried a boundary; letting one in would
        # split one side effect into two keys.
        a = legacy.from_outbox_item("task-9", "discord-dm").idempotency_key
        b = legacy.from_outbox_item("task-9", "slack-dm").idempotency_key
        self.assertEqual(a, b)

    def test_resend_epoch_versions_the_preserved_key(self):
        m = legacy.from_outbox_item("task-9", "gw", resend_epoch=1)
        self.assertEqual(m.idempotency_key.value, core.idempotency_key("task-9", 1))
        self.assertNotEqual(m.idempotency_key,
                            legacy.from_outbox_item("task-9", "gw").idempotency_key)

    def test_sentinel_and_non_task_item_shapes(self):
        s = legacy.from_delivered_sentinel("task-7", "discord-dm")
        self.assertEqual(s.delivery_id.value, "legacy:task-7@discord-dm")
        # No provider ever saw a key for a local sentinel file, so the
        # canonical key applies there — nothing to preserve.
        self.assertEqual(s.idempotency_key,
                         I.idempotency_key(I.TaskId("task-7"), "discord-dm"))
        fence = legacy.from_outbox_item("proactive-9.txt#123", "discord-proactive")
        self.assertEqual(fence.delivery_id.value,
                         "legacy:proactive-9.txt%23123@discord-proactive")
        # An item_id that already contains '#' still round-trips as a key.
        self.assertEqual(fence.idempotency_key.value, "proactive-9.txt#123#0")


class LegacyDeliveryIdStaysBounded(unittest.TestCase):
    """The delivery id is ours to shape (unlike the key), so a long or
    escape-dense item takes a digest form rather than raising — and the
    ids derived from it (attempt, resend) must still fit."""

    LONG = ("x" * 191, "/" * 65, "\u00e9" * 100, "a b" * 70)

    def test_long_items_map_and_stay_derivable(self):
        for item in self.LONG:
            with self.subTest(item=item[:12]):
                m = legacy.from_outbox_item(item, "gw")
                self.assertLessEqual(len(m.delivery_id.value), 200)
                self.assertEqual(serialization.parse_delivery_id(m.delivery_id.value), m.delivery_id)
                I.attempt_id(m.delivery_id, 1)
                I.resend_delivery_id(m.delivery_id, 1)
                self.assertEqual(m.idempotency_key.value, f"{item}#0",
                                 "the provider key must stay byte-for-byte")

    def test_digest_form_is_pure_and_injective(self):
        a = legacy.from_outbox_item("x" * 191, "gw").delivery_id
        self.assertEqual(a, legacy.from_outbox_item("x" * 191, "gw").delivery_id)
        b = legacy.from_outbox_item("x" * 190 + "y", "gw").delivery_id
        self.assertNotEqual(a, b, "the readable prefix is lossy; the digest must decide")

    def test_short_items_keep_the_readable_form(self):
        self.assertEqual(legacy.from_outbox_item("task-1712000000001", "gw").delivery_id.value,
                         "legacy:task-1712000000001@gw")


class DeliveryCoreKeyOwnership(unittest.TestCase):
    """One owner derives provider keys. The delivery core keeps its public
    name; the bytes come from ag2_sparrow.identity."""

    def test_core_key_is_byte_identical_to_the_canonical_owner(self):
        for item, epoch in [("task-X", 0), ("task-X", 1),
                            ("task-a~b", 7), ("proactive-9.txt#123", 0)]:
            self.assertEqual(core.idempotency_key(item, epoch),
                             I.legacy_idempotency_key(item, epoch).value)

    def test_shipped_key_bytes_are_frozen(self):
        # The exact strings in flight today. A change here is a duplicate
        # side effect for every item a provider has already seen.
        self.assertEqual(core.idempotency_key("task-X", 0), "task-X#0")
        self.assertEqual(core.idempotency_key("task-X", 1), "task-X#1")

    def test_preserved_key_parses_and_stays_disjoint_from_canonical(self):
        k = I.legacy_idempotency_key("task-X", 0)
        self.assertEqual(serialization.parse_idempotency_key(k.value), k)
        self.assertNotEqual(k, I.idempotency_key(I.TaskId("task-X"), "gw"))

    def test_preserved_key_keeps_the_whole_shipped_domain(self):
        # The parent formatted EVERY string byte-for-byte, and the outbox
        # admits opaque ids, so narrowing this domain strands live claims.
        for item in ("task a", "task/a", "task\\a", "task-💥", "a/b/c"):
            with self.subTest(item=item):
                self.assertEqual(I.legacy_idempotency_key(item).value,
                                 f"{item}#0")

    def test_preserved_key_keeps_every_length_and_newline(self):
        # The parent formatted every string byte-for-byte; a cap or a charset
        # applied before the opaque bypass strands the items past it.
        for item in ("x" * 199, "x" * 300, "a\nb", "line\n#1"):
            with self.subTest(item=item):
                k = I.legacy_idempotency_key(item)
                self.assertEqual(k.value, f"{item}#0")
                self.assertEqual(serialization.parse_idempotency_key(k.value), k)

    def test_preserved_key_rejects_only_genuinely_invalid_input(self):
        # "" is in the shipped domain (main formats every str); only a
        # non-str item or a malformed epoch is genuinely invalid.
        self.assertEqual(I.legacy_idempotency_key("").value, "#0")
        with self.assertRaises(TypeError):
            I.legacy_idempotency_key(None)
        with self.assertRaises(TypeError):
            I.legacy_idempotency_key("task-X", True)
        with self.assertRaises(ValueError):
            I.legacy_idempotency_key("task-X", -1)

    def test_canonical_grammar_stays_strict(self):
        # Widening is scoped to the opaque legacy shape; the canonical
        # constructor must still reject the same characters it always did.
        for bad in ("e:task X@gw", "e:a/b@gw"):
            with self.assertRaises(ValueError, msg=bad):
                I.IdempotencyKey(bad)


class AttemptedThenRequeuedLegacyRoundTrip(unittest.TestCase):
    """The finding, end-to-end: an item attempted through the REAL
    DeliveryCore, parked ambiguous, then re-offered after adopting the R3
    adapter must reach the provider under the SAME key — otherwise the
    provider's dedup misses and the side effect happens twice."""

    def _core_and_provider(self, root):
        seen = []

        class P:
            capabilities = contract.ProviderCapabilities(
                reconcile_capable=False, idempotent_send=False)

            def deliver(self, item_id, payload, idempotency_key):
                seen.append(idempotency_key)
                raise contract.ProviderIndeterminate("boundary crossed")

            def reconcile(self, attempt):
                return None

        return core.DeliveryCore(backend_a.DesignAClaimBackend(root), P(),
                                 worker="w"), seen

    def test_requeued_legacy_item_reuses_the_provider_key(self):
        item_id, boundary = "task-1712000000001", "gw"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "outbox"
            dc, seen = self._core_and_provider(root)
            dc.backend.publish(item_id, b"{}")
            res = dc.deliver_one(item_id, b"{}")
        # Attempt 1 crossed the boundary and parked ambiguous.
        self.assertIs(res.outcome, contract.DeliveryOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(len(seen), 1)
        attempted_key = seen[0]

        # The item is now adopted into the B identity model.
        mapping = legacy.from_outbox_item(item_id, boundary)
        self.assertEqual(
            mapping.idempotency_key.value, attempted_key,
            "the re-offer would carry a NEW provider key — the ambiguous "
            "first attempt may already have taken effect, so this is a "
            "duplicate side effect")

    def test_a_deliberate_resend_is_the_only_key_change(self):
        item_id = "task-1712000000001"
        first = legacy.from_outbox_item(item_id, "gw").idempotency_key.value
        self.assertEqual(first, core.idempotency_key(item_id, 0))
        resend = legacy.from_outbox_item(item_id, "gw",
                                         resend_epoch=1).idempotency_key.value
        self.assertEqual(resend, core.idempotency_key(item_id, 1))
        self.assertNotEqual(first, resend)


if __name__ == "__main__":
    unittest.main()
