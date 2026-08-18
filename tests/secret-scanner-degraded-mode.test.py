#!/usr/bin/env python3
"""secret_scanner degraded mode: detect-secrets' absence must not disable the
repo-local whole-line rules (issue #3100 — a compensating control was killed
by the absence of the thing it compensates for), and each mode announces
itself loudly at import so a degraded host is visible.

Both arms run in subprocesses: the degraded arm hides detect_secrets via an
import hook (the package IS installed on some hosts), the full arm is the
positive control proving the probe can tell the modes apart.

Run: python3 tests/secret-scanner-degraded-mode.test.py
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


PROBE = r"""
import sys
sys.path.insert(0, {src!r})
{blocker}
import secret_scanner as ss
hits = ss.scan_secrets("prose line\n" + "a1" * 20 + "\n")
print("ACTIVE=" + str(ss.DETECT_SECRETS_ACTIVE))
print("HEX_HIT=" + str(any(h.secret_type == "Bare Hex Token" for h in hits)))
"""

BLOCKER = r"""
import importlib.abc


class _Hide(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "detect_secrets" or name.startswith("detect_secrets."):
            raise ImportError("hidden for degraded-mode test")


sys.meta_path.insert(0, _Hide())
"""


def run(blocker):
    code = PROBE.format(src=str(REPO / "src"), blocker=blocker)
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=60)



def in_process_degraded_arm():
    """Coverage-visible twin of the subprocess arm: reload the module under
    an import hook IN THIS interpreter so instrumentation sees the fallback
    branch execute (the subprocess arms stay as the behavioral proof)."""
    import contextlib
    import importlib.abc
    import io

    class _Hide(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "detect_secrets" or name.startswith("detect_secrets."):
                raise ImportError("hidden for in-process degraded arm")

    hook = _Hide()
    saved = {k: sys.modules.pop(k) for k in list(sys.modules)
             if k == "secret_scanner" or k.startswith("detect_secrets")}
    sys.meta_path.insert(0, hook)
    try:
        sys.path.insert(0, str(REPO / "src"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            import secret_scanner as ss
        check("in-process: ACTIVE=False", ss.DETECT_SECRETS_ACTIVE is False)
        hits = ss.scan_secrets("prose\n" + "a1" * 20 + "\n")
        check("in-process: hex rule fires",
              any(h.secret_type == "Bare Hex Token" for h in hits))
        check("in-process: DEGRADED announced", "DEGRADED" in err.getvalue())
        # The vault refusal must branch on capability, not import failure,
        # and keep naming THIS interpreter (the repair instruction).
        sys.modules.pop("vault_intercept", None)
        import vault_intercept as vi
        res = vi.intercept_vault_commands(
            "vault set STRIPE_KEY sk-live-abc123XYZ")
        out = res.text
        check("vault refusal survives the guarded import (degraded)",
              "REFUSED" in out and "detect-secrets not installed" in out)
        check("refusal still names this interpreter",
              sys.executable in out)
        sys.modules.pop("vault_intercept", None)
        # The no-module-at-all branch: hide secret_scanner itself and the
        # refusal must still hold (DETECT_SECRETS_ACTIVE = False fallback).
        class _HideScanner(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "secret_scanner":
                    raise ImportError("hidden for no-module arm")
        hook2 = _HideScanner()
        saved2 = sys.modules.pop("secret_scanner", None)
        sys.meta_path.insert(0, hook2)
        try:
            import vault_intercept as vi2
            out2 = vi2.intercept_vault_commands(
                "vault set STRIPE_KEY sk-live-abc123XYZ").text
            check("no-module arm: refusal holds without secret_scanner",
                  "REFUSED" in out2 and sys.executable in out2)
        finally:
            sys.meta_path.remove(hook2)
            sys.modules.pop("vault_intercept", None)
            if saved2 is not None:
                sys.modules["secret_scanner"] = saved2
    finally:
        sys.meta_path.remove(hook)
        sys.modules.pop("secret_scanner", None)
        sys.modules.update(saved)


def main() -> int:
    in_process_degraded_arm()
    deg = run(BLOCKER)
    check("degraded: module imports (no ModuleNotFoundError)", deg.returncode == 0,
          deg.stderr[-300:])
    check("degraded: ACTIVE=False", "ACTIVE=False" in deg.stdout)
    check("degraded: Bare Hex Token rule still fires", "HEX_HIT=True" in deg.stdout)
    check("degraded: mode announced loudly on stderr",
          "DEGRADED" in deg.stderr and "detect-secrets missing" in deg.stderr)

    full = run("")
    if "ACTIVE=True" not in full.stdout:
        # Host without detect_secrets: the degraded arm above already proved
        # the guard; the full arm's assertions would be vacuous here.
        print("  note: detect_secrets not installed on this host — full-mode "
              "arm skipped (degraded arm is the load-bearing one)")
    else:
        check("full: positive control — modes are distinguishable",
              "ACTIVE=True" in full.stdout)
        check("full: hex rule fires in full mode too", "HEX_HIT=True" in full.stdout)
        check("full: healthy path is SILENT (no mode line)",
              "[secret-scanner]" not in full.stderr)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: secret-scanner survives detect-secrets absence; repo-local "
          "rules live in both modes; mode is loud")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
