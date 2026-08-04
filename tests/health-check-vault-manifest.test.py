#!/usr/bin/env python3
"""`check_vault_manifest_integrity` — vault manifest vs Keychain divergence.

HERMETIC BY CONSTRUCTION: every case passes an explicit `manifest_path` (tmp) and
an injected `keychain_probe`, so no case can read the operator's real manifest or
spawn `security`. The final case asserts that property instead of trusting it —
it hashes the host's live manifest before and after the whole run.

The case that carries the design is `test_zero_backed_is_inconclusive_not_alarm`.
`security find-generic-password` exits 44 both for an absent key AND for a wrong
`-a <account>` (measured on macOS 15), so a bad account name would otherwise make
every key read phantom and produce a maximally alarming, entirely wrong report.
The probe refuses to cry divergence when NOTHING resolves. Delete that guard and
this case fails; that is the point.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

_spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

import vault_intercept as vi  # noqa: E402  (resolution path under test)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def _manifest(tmp: Path, keys) -> Path:
    p = tmp / "keys.json"
    p.write_text(json.dumps({k: {"stored_at": "2026-01-01T00:00:00Z"} for k in keys}))
    return p


def _probe(present):
    """Keychain stub: resolves only names in `present`, ignores the account."""
    present = set(present)
    return lambda _account, key: key in present


def _live_manifest_hash() -> "str | None":
    try:
        import vault_intercept
        p = Path(vault_intercept._manifest_path())
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "ABSENT"
    except Exception:
        return None


def main() -> int:
    print("vault-manifest integrity probe:")
    live_before = _live_manifest_hash()

    with tempfile.TemporaryDirectory(prefix="vault-manifest-test-") as td:
        tmp = Path(td)

        # --- divergence IS reported ---------------------------------------
        m = _manifest(tmp, ["REAL_ONE", "GHOST_A", "GHOST_B"])
        r = hc.check_vault_manifest_integrity(m, _probe(["REAL_ONE"]))
        check("phantom keys -> warn", r["status"] == "warn", repr(r))
        check("names the phantoms", "GHOST_A" in r["detail"] and "GHOST_B" in r["detail"], r["detail"])
        check("does not name the backed key as phantom",
              "GHOST_A" in r["detail"] and r["detail"].count("REAL_ONE") == 0, r["detail"])
        check("states the ratio", "2/3" in r["detail"], r["detail"])

        # --- the positive control: 0 backed means BROKEN CHECKER ----------
        r = hc.check_vault_manifest_integrity(_manifest(tmp, ["A", "B"]), _probe([]))
        check("test_zero_backed_is_inconclusive_not_alarm", r["status"] == "ok", repr(r))
        check("  ...and says WHY it isn't asserting divergence",
              "unverifiable" in r["detail"], r["detail"])

        # --- clean vault ---------------------------------------------------
        r = hc.check_vault_manifest_integrity(_manifest(tmp, ["K1", "K2"]), _probe(["K1", "K2"]))
        check("all backed -> ok", r["status"] == "ok" and "resolve" in r["detail"], repr(r))

        # --- degenerate inputs ---------------------------------------------
        r = hc.check_vault_manifest_integrity(tmp / "nope.json", _probe([]))
        check("absent manifest -> ok", r["status"] == "ok", repr(r))

        r = hc.check_vault_manifest_integrity(_manifest(tmp, []), _probe([]))
        check("empty manifest -> ok", r["status"] == "ok", repr(r))

        bad = tmp / "bad.json"
        bad.write_text("{not json")
        r = hc.check_vault_manifest_integrity(bad, _probe([]))
        check("malformed manifest -> warn", r["status"] == "warn", repr(r))

        # --- truncation is DISCLOSED, never silent -------------------------
        many = _manifest(tmp, [f"K{i}" for i in range(10)])
        r = hc.check_vault_manifest_integrity(many, _probe(["K0"]), max_keys=4)
        check("capped scan discloses the cap", "checked first 4 of 10" in r["detail"], r["detail"])

        # --- the REAL probe path, which every case above bypasses ----------
        # Cases above inject `keychain_probe`, so none of them exercises how the
        # probe behaves with no stub. Each branch below is a distinct way the
        # check can be unable to answer, and every one of them must resolve to
        # "not asserting" — an unanswerable check that warns is a false alarm.

        # (i) no `security` binary -> cannot verify, must NOT claim divergence.
        _which = hc.shutil.which
        hc.shutil.which = lambda name: None if name == "security" else _which(name)
        try:
            r = hc.check_vault_manifest_integrity(_manifest(tmp, ["K1", "K2"]))
        finally:
            hc.shutil.which = _which
        check("no `security` binary -> ok, not warn", r["status"] == "ok", repr(r))
        check("  ...and says it cannot verify", "cannot verify" in r["detail"], r["detail"])

        # (ii) vault_intercept not importable (trimmed install) -> ok, not warn.
        _saved = sys.modules.get("vault_intercept")
        sys.modules["vault_intercept"] = None  # makes `import vault_intercept` raise
        try:
            r = hc.check_vault_manifest_integrity(_manifest(tmp, ["K1"]))
        finally:
            if _saved is None:
                sys.modules.pop("vault_intercept", None)
            else:
                sys.modules["vault_intercept"] = _saved
        check("vault_intercept unimportable -> ok", r["status"] == "ok", repr(r))

        # (iii) the real Keychain probe answers False for an absent key and does
        # not raise. Read-only: `find-generic-password` never creates anything.
        real = hc.check_vault_manifest_integrity(_manifest(tmp, ["ZZ_NO_SUCH_KEY_88131"]))
        check("real keychain probe runs without raising",
              real["status"] == "ok", repr(real))
        check("  ...and 0-resolved is reported as unverifiable, not divergence",
              "unverifiable" in real["detail"], real["detail"])

        # (iv) valid JSON, WRONG SHAPE. This case previously asserted "ok, no
        # crash" — pinning the wrong behaviour, which is worse than not testing
        # it. `_read_manifest()` returns the value verbatim and
        # `list_vault_keys()` calls `.keys()` on it, so discovery raises
        # AttributeError. "Empty" would be a clean bill of health for a vault
        # nobody can enumerate. (qingyun-wu, #2623)
        for blob, label in (('["K1","K2"]', "list"), ('"A"', "str"), ("123", "int")):
            wrong = tmp / f"wrong-{label}.json"
            wrong.write_text(blob)
            r = hc.check_vault_manifest_integrity(wrong, _probe([]))
            check(f"wrong-shape manifest ({label}) -> warn, not ok",
                  r["status"] == "warn", repr(r))
        check("  ...and says discovery is broken, not empty",
              "AttributeError" in r["detail"], r["detail"])

        # (v) THE FALSE-CLEAN BOTH REVIEWERS FOUND: canonical absent, legacy
        # present. Production `list_vault_keys()` reads through `_read_manifest()`,
        # which falls back to the legacy home-dir manifest — so a probe that only
        # consults `_manifest_path()` reports "no vault manifest on this host"
        # for a pre-migration install that is still advertising keys. That is a
        # false clean on exactly the population this check exists to diagnose.
        legacy = tmp / "legacy-keys.json"
        legacy.write_text(json.dumps({"REAL": {}, "PHANTOM": {}}))
        absent_canonical = tmp / "no-such-canonical.json"
        assert not absent_canonical.exists()
        _mp = vi._manifest_path
        vi._manifest_path = lambda: str(absent_canonical)
        try:
            r = hc.check_vault_manifest_integrity(
                keychain_probe=_probe(["REAL"]), legacy_path=legacy)
        finally:
            vi._manifest_path = _mp
        check("canonical absent + legacy present -> reaches the keychain probe",
              r["status"] == "warn", repr(r))
        check("  ...names the phantom from the LEGACY manifest",
              "PHANTOM" in r["detail"], r["detail"])
        check("  ...and discloses it read the legacy fallback",
              "LEGACY" in r["detail"], r["detail"])

        # --- the probe is registered, not just defined ---------------------
        src = (REPO / "src" / "health-check.py").read_text()
        check("registered in the check list",
              "checks.append(check_vault_manifest_integrity())" in src,
              "defined but never appended = a probe that can never fire")

    live_after = _live_manifest_hash()
    check("HERMETIC: host's live vault manifest untouched by this suite",
          live_before == live_after, f"{live_before} -> {live_after}")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All vault-manifest probe checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
