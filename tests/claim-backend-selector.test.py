#!/usr/bin/env python3
"""Which ClaimBackend the proactive leg constructs is selectable, and the
selection fails toward the shipped default rather than toward an outage.

Two halves, because they are separate claims: sutando_config decides the NAME
(policy), and discord-bridge maps it to a CLASS (adapter). A test that only
drove the resolver would pass while the bridge ignored it.
"""
# ruff: noqa: E402 — imports follow the sys.path inserts below
import ast
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

spec = importlib.util.spec_from_file_location("sc", REPO / "src" / "sutando_config.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

fails = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        fails.append(msg)


def _resolve(env=None, cfg=None):
    """Resolve with the env and the config file both controlled."""
    saved = os.environ.get("SUTANDO_CLAIM_BACKEND")
    saved_load = sc.load_config
    try:
        if env is None:
            os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        else:
            os.environ["SUTANDO_CLAIM_BACKEND"] = env
        sc.load_config = lambda *a, **k: ({"delivery": {"claim_backend": cfg}}
                                          if cfg is not None else {})
        return sc.resolve_claim_backend()
    finally:
        sc.load_config = saved_load
        if saved is None:
            os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        else:
            os.environ["SUTANDO_CLAIM_BACKEND"] = saved


print("1. the name resolves by policy, and an unknown value is not an outage")
check(_resolve() == "a", "no config, no env -> the shipped default 'a'")
check(_resolve(cfg="c") == "c", "config selects 'c'")
check(_resolve(env="c") == "c", "env selects 'c'")
check(_resolve(env="a", cfg="c") == "a", "env OVERRIDES config, per the sibling idiom")
check(_resolve(env=" C ") == "c", "whitespace and case are normalised")
# A typo must not take the proactive leg down; the default is the safe answer.
check(_resolve(cfg="design-c") == "a", "unrecognised config value falls back to 'a'")
check(_resolve(env="zzz") == "a", "unrecognised env value falls back to 'a'")
check(_resolve(cfg="") == "a", "empty config value falls back to 'a'")

print("2. 'delivery' is a KNOWN top-level config key")
# Otherwise a user who configures it gets an unknown-key warning telling them
# the setting they just read about is not real.
check("delivery" in sc._KNOWN_TOP_LEVEL if hasattr(sc, "_KNOWN_TOP_LEVEL")
      else "delivery" in open(REPO / "src" / "sutando_config.py").read(),
      "'delivery' registered so it is not warned as an unknown key")

print("3. the ADAPTER actually consults the resolver and maps both names")
src = (REPO / "src" / "discord-bridge.py").read_text()
tree = ast.parse(src)
fn = next((n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_proactive_fence"), None)
check(fn is not None, "_proactive_fence still exists")
body = ast.get_source_segment(src, fn) or ""
check("resolve_claim_backend()" in body,
      "the construction site CALLS the resolver (not a hardcoded backend)")
check("DesignCClaimBackend" in body and "DesignAClaimBackend" in body,
      "both backends are reachable from the construction site")
check(body.index("DesignAClaimBackend") < len(body),
      "Design A remains present as the default arm")

print("4. both backends satisfy what the fence needs")
from ag2_sparrow.delivery_core import DesignAClaimBackend
from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend
from proactive_claim_fence import ProactiveClaimFence

for name, mk in (("A", lambda r: DesignAClaimBackend(r)),
                 # C refuses an un-activated root: striping is a
                 # quiescence-requiring migration the deploy path performs.
                 ("C", lambda r: DesignCClaimBackend(r, activate=True))):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body_file = root / "proactive-sel.txt"
        body_file.write_text("selector round trip")
        fence = ProactiveClaimFence(mk(root / ".outbox"), root, worker="t")
        claim = fence.claim(body_file)
        ok = claim is not None and claim.exists() and not body_file.exists()
        fence.confirm(claim)
        check(ok and not claim.exists(),
              f"backend {name}: claim moves the body and confirm consumes it")

print("5. selecting C against an UN-ACTIVATED root must not take the leg down")
with tempfile.TemporaryDirectory() as td:
    raised = None
    try:
        DesignCClaimBackend(Path(td) / ".outbox")     # no activate=True
    except RuntimeError as e:
        raised = e
    check(raised is not None,
          "C refuses an un-activated root (this is why the adapter needs a fallback)")
    check("activation" in str(raised),
          "and its message names activation, so the log line is actionable")
adapter = (REPO / "src" / "discord-bridge.py").read_text()
fn2 = next(n for n in ast.walk(ast.parse(adapter))
           if isinstance(n, ast.FunctionDef) and n.name == "_proactive_fence")
seg = ast.get_source_segment(adapter, fn2) or ""
check("except RuntimeError" in seg,
      "the adapter CATCHES that refusal rather than crashing the proactive leg")
check("Design A" in seg,
      "and the fallback is announced, not silent — the operator asked for C")

if fails:
    print(f"\n{len(fails)} FAILURE(S)")
    raise SystemExit(1)
print("\nALL PASS — selection is policy, mapping is the adapter's, default is 'a'")
