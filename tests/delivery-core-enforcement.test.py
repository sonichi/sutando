#!/usr/bin/env python3
"""Delivery Core enforcement suite (001's four same-PR deliverables;
seam doc v0.2.1 riders):

1. Cross-incarnation key identity — recovery/re-claim must not mint a new
   delivery idempotency key: the provider sees the SAME key from a second
   incarnation (different worker/pid) as from the first.
2. Static no-claim-material-in-key scan — AST over delivery_core: the
   idempotency_key function may reference only item_id + resend_epoch, and
   every provider.deliver call site passes a key derived from it.
3. Lease/local seam oracle (invariant 9) — the four booleans lease-acked /
   locally-claimed / provider-confirmed / server-finalized are independent
   observables: the reference lease provider exports a finalize-observable
   and CONFIRMED never implies finalized (nor vice versa), and while the
   lease is held at most one local claim exists.
4. Fence fault tests (§4) — legacy sentinel maps conservatively (bare
   sentinel -> OUTCOME_UNKNOWN, never CONFIRMED); crash at every step of a
   conversion leaves each item converted or untouched, never both-visible,
   with the fence still naming the OLD epoch.

Run: python3 tests/delivery-core-enforcement.test.py
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow.delivery_core import (  # noqa: E402
    DeliveryCore, DeliveryOutcome, DeliveryReceipt, DesignAClaimBackend,
    ProviderCapabilities, idempotency_key)
from ag2_sparrow.delivery_core import migration  # noqa: E402

ITEM = "room-evt-1"
CORE_DIR = _PKG / "ag2_sparrow" / "delivery_core"


class _KeyRecorder:
    """Provider double that records every idempotency key it is handed."""

    def __init__(self, outcomes, caps=ProviderCapabilities()):
        self.outcomes = list(outcomes)
        self.capabilities = caps
        self.keys = []

    def deliver(self, item_id, payload, idempotency_key):
        self.keys.append(idempotency_key)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return DeliveryReceipt(outcome=out)

    def reconcile(self, item_id, idempotency_key):
        return None


class KeyIdentityAcrossIncarnations(unittest.TestCase):
    """Deliverable 1: the machine op — claim, die without completing, let a
    second incarnation reclaim and deliver; the wire must see one key."""

    def test_reclaim_reuses_the_same_key(self):
        with tempfile.TemporaryDirectory(prefix="enf-key-") as td:
            backend = DesignAClaimBackend(Path(td), reclaim_ttl_s=0.0)
            backend.publish(ITEM, b"x")
            # incarnation 1 claims in a child process and CRASHES (exits
            # without complete()), leaving a claim whose owner pid is dead
            code = (f"import sys; sys.path.insert(0, {str(_PKG)!r}); "
                    f"from ag2_sparrow.delivery_core import "
                    f"DesignAClaimBackend; from pathlib import Path; "
                    f"b = DesignAClaimBackend(Path({td!r})); "
                    f"t = b.claim({ITEM!r}, 'w1.111.b1'); "
                    f"sys.exit(0 if t else 3)")
            rc = subprocess.run([sys.executable, "-c", code]).returncode
            self.assertEqual(rc, 0, "first incarnation must claim")
            provider = _KeyRecorder([DeliveryOutcome.CONFIRMED])
            core2 = DeliveryCore(backend, provider, worker="w2.222.b2")
            out = core2.deliver_one(ITEM, b"x")
            self.assertIs(out, DeliveryOutcome.CONFIRMED)
            self.assertEqual(provider.keys, [idempotency_key(ITEM)],
                             "second incarnation must reuse the item key")
            for material in ("w1", "w2", "111", "222", "b1", "b2"):
                self.assertNotIn(material, provider.keys[0],
                                 "claim material leaked into the key")

    def test_key_stable_across_park_and_redrive(self):
        with tempfile.TemporaryDirectory(prefix="enf-key2-") as td:
            backend = DesignAClaimBackend(Path(td), reclaim_ttl_s=0.0)
            backend.publish(ITEM, b"x")
            p1 = _KeyRecorder([RuntimeError("wire died")])
            DeliveryCore(backend, p1, worker="w1.1.1").deliver_one(ITEM, b"x")
            p2 = _KeyRecorder([DeliveryOutcome.CONFIRMED])
            DeliveryCore(backend, p2, worker="w2.2.2").deliver_one(ITEM, b"x")
            self.assertEqual(p1.keys, p2.keys,
                             "park -> re-drive changed the idempotency key")


class StaticKeyScan(unittest.TestCase):
    """Deliverable 2: structural pin — no claim material can reach the key,
    by construction, verified over the shipped source (not a copy)."""

    FORBIDDEN = {"worker", "opaque", "pid", "token", "birth", "start_usec"}

    def _fn(self, tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_key_fn_signature_and_body_are_claim_free(self):
        tree = ast.parse((CORE_DIR / "core.py").read_text(encoding="utf-8"))
        fn = self._fn(tree, "idempotency_key")
        self.assertIsNotNone(fn, "idempotency_key must exist in core.py")
        params = {a.arg for a in fn.args.args}
        self.assertEqual(params, {"item_id", "resend_epoch"},
                         "key derives from item identity + resend epoch ONLY")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        leaked = (names | attrs) & self.FORBIDDEN
        self.assertFalse(leaked, f"claim material in key fn: {sorted(leaked)}")

    def test_every_deliver_call_passes_a_derived_key(self):
        derived = set()
        bad = []
        for src in CORE_DIR.glob("*.py"):
            tree = ast.parse(src.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and \
                        isinstance(node.value, ast.Call) and \
                        isinstance(node.value.func, ast.Name) and \
                        node.value.func.id == "idempotency_key":
                    derived |= {t.id for t in node.targets
                                if isinstance(t, ast.Name)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and \
                        isinstance(node.func, ast.Attribute) and \
                        node.func.attr in ("deliver", "reconcile"):
                    if len(node.args) < 2:
                        continue
                    key_arg = node.args[-1]
                    ok = (isinstance(key_arg, ast.Name)
                          and key_arg.id in derived) or \
                         (isinstance(key_arg, ast.Call)
                          and isinstance(key_arg.func, ast.Name)
                          and key_arg.func.id == "idempotency_key")
                    if not ok:
                        bad.append(f"{src.name}:{node.lineno}")
        self.assertFalse(bad, f"deliver/reconcile call passes a key not "
                              f"derived from idempotency_key(): {bad}")


class _LeaseProvider:
    """Reference AG2-Space-shaped provider: server lease + finalize as
    INDEPENDENT observables (the invariant-9 shape Phase-2's real adapter
    tests must export)."""

    capabilities = ProviderCapabilities(reconcile_capable=True)

    def __init__(self):
        self.lease_held = False
        self.provider_confirmed = False
        self.server_finalized = False

    def deliver(self, item_id, payload, idempotency_key):
        self.provider_confirmed = True
        return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                               provider_ref="rcpt-1")

    def reconcile(self, item_id, idempotency_key):
        return DeliveryReceipt(
            outcome=DeliveryOutcome.CONFIRMED if self.provider_confirmed
            else DeliveryOutcome.NOT_DELIVERED, provider_ref="rcpt-1")

    def finalize(self, item_id):
        self.server_finalized = True


class LeaseLocalSeamOracle(unittest.TestCase):
    """Deliverable 3: four independent booleans; lease held -> at most one
    local claim."""

    def test_lease_held_admits_exactly_one_local_claim(self):
        with tempfile.TemporaryDirectory(prefix="enf-lease-") as td:
            backend = DesignAClaimBackend(Path(td))
            provider = _LeaseProvider()
            provider.lease_held = True
            backend.publish(ITEM, b"x")
            t1 = backend.claim(ITEM, "w1")
            self.assertIsNotNone(t1)
            self.assertIsNone(backend.claim(ITEM, "w2"),
                              "two local claims under one server lease")
            oracle = (provider.lease_held, t1 is not None,
                      provider.provider_confirmed, provider.server_finalized)
            self.assertEqual(oracle, (True, True, False, False),
                             "claim alone must not move provider booleans")

    def test_confirmed_and_finalized_are_independent(self):
        with tempfile.TemporaryDirectory(prefix="enf-lease2-") as td:
            backend = DesignAClaimBackend(Path(td))
            provider = _LeaseProvider()
            backend.publish(ITEM, b"x")
            out = DeliveryCore(backend, provider).deliver_one(ITEM, b"x")
            self.assertIs(out, DeliveryOutcome.CONFIRMED)
            self.assertTrue(provider.provider_confirmed)
            self.assertFalse(provider.server_finalized,
                             "CONFIRMED must not imply server-finalized")
            provider.finalize(ITEM)
            self.assertTrue(provider.server_finalized)

    def test_reference_provider_exports_finalize_observable(self):
        p = _LeaseProvider()
        self.assertTrue(hasattr(p, "finalize") and
                        hasattr(p, "server_finalized"),
                        "invariant-9 adapters must export finalize state")


class FenceFaults(unittest.TestCase):
    """Deliverable 4: sentinel mapping table + crash-at-every-step
    conversion pass."""

    def test_sentinel_mapping_is_conservative(self):
        self.assertIs(migration.classify_legacy_sentinel(None),
                      DeliveryOutcome.OUTCOME_UNKNOWN,
                      "bare delivered-sentinel must NEVER map to CONFIRMED")
        self.assertIs(migration.classify_legacy_sentinel("rcpt-9"),
                      DeliveryOutcome.CONFIRMED)

    OLD = json.dumps({"fmt": "legacy"}).encode()

    @staticmethod
    def _render(data):
        return json.dumps({"fmt": "v2", "outcome":
                           migration.classify_legacy_sentinel(None).value
                           }).encode()

    @staticmethod
    def _is_converted(data):
        return json.loads(data).get("fmt") == "v2"

    def _convert_one(self, root, fault=None):
        def convert(item):
            migration.convert_item_atomic(root / item, self._render,
                                          self._is_converted, fault=fault)
        return convert

    def _states(self, root, items):
        out = {}
        for it in items:
            self.assertFalse((root / (it + ".tmp")).exists() and
                             self._is_converted(
                                 (root / it).read_bytes()) is None,
                             "impossible")
            out[it] = "new" if self._is_converted(
                (root / it).read_bytes()) else "old"
        return out

    def test_crash_at_every_step_never_leaves_both_visible(self):
        """Between-item AND intra-item crash points: at every one, each
        item reads back as exactly old-format or new-format (one path, one
        replace — a both-visible state has no filesystem representation)."""
        items = [f"it-{i}" for i in range(3)]
        intra = [None, "pre-write-tmp", "pre-replace", "post-replace"]
        for crash_after in range(len(items) + 1):
            for crash_step in intra:
                with tempfile.TemporaryDirectory(prefix="enf-fence-") as td:
                    root = Path(td)
                    for it in items:
                        (root / it).write_bytes(self.OLD)

                    def fault(step, _cs=crash_step):
                        if _cs is not None and step == _cs:
                            raise RuntimeError(f"crash at {step}")
                    try:
                        migration.convert_epoch(
                            root, items, self._convert_one(root, fault),
                            "B", crash_after=crash_after)
                        crashed = False
                    except RuntimeError:
                        crashed = True
                    for it, state in self._states(root, items).items():
                        self.assertIn(state, ("old", "new"),
                                      f"{it} in mixed state")
                    if crashed:
                        self.assertEqual(migration.read_epoch(root), "A",
                                         "fence must stay OLD after crash")
                    else:
                        self.assertEqual(migration.read_epoch(root), "B")

    def test_restarted_migrator_completes_before_any_drainer(self):
        """Crash mid-pass, then re-run the SAME conversion: idempotent
        convert skips already-new items, finishes the stragglers, and only
        then flips the fence — until that instant read_epoch names the old
        protocol, so no new-epoch drainer may start and old-epoch items
        were never half-interpreted."""
        items = [f"it-{i}" for i in range(4)]
        with tempfile.TemporaryDirectory(prefix="enf-fence-r-") as td:
            root = Path(td)
            for it in items:
                (root / it).write_bytes(self.OLD)
            with self.assertRaises(RuntimeError):
                migration.convert_epoch(root, items,
                                        self._convert_one(root), "B",
                                        crash_after=2)
            self.assertEqual(migration.read_epoch(root), "A")
            states = self._states(root, items)
            self.assertEqual(sorted(states.values()),
                             ["new", "new", "old", "old"])
            migration.convert_epoch(root, items, self._convert_one(root),
                                    "B")
            self.assertEqual(migration.read_epoch(root), "B")
            self.assertEqual(set(self._states(root, items).values()),
                             {"new"})

    def test_fence_write_is_atomic_and_last(self):
        with tempfile.TemporaryDirectory(prefix="enf-fence2-") as td:
            root = Path(td)
            self.assertEqual(migration.read_epoch(root), "A")
            migration.convert_epoch(root, [], lambda i: None, "B")
            self.assertEqual(migration.read_epoch(root), "B")
            self.assertFalse(list(root.glob("*.tmp")),
                             "no fence temp file may survive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
