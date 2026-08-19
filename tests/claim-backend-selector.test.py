#!/usr/bin/env python3
"""Which ClaimBackend the proactive leg constructs is selectable, and the
selection fails toward the shipped default rather than toward an outage.

Two halves, because they are separate claims: sutando_config decides the NAME
(policy), and discord-bridge maps it to a CLASS (adapter). A test that only
drove the resolver would pass while the bridge ignored it.
"""
# ruff: noqa: E402 — imports follow the sys.path inserts below
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

# Isolate BEFORE anything resolves config: this test asserts on config
# resolution, so reading the operator's real file would make it host-dependent.
_ccd = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = _ccd
_p = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_p.mkdir(parents=True, exist_ok=True)
(_p / "access.json").write_text("{}")

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


def _resolve_raw_delivery(value):
    """Set `delivery` ITSELF to `value` — _resolve only reaches claim_backend."""
    saved_load, saved_env = sc.load_config, os.environ.get("SUTANDO_CLAIM_BACKEND")
    try:
        os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        sc.load_config = lambda *a, **k: {"delivery": value}
        return sc.resolve_claim_backend()
    finally:
        sc.load_config = saved_load
        if saved_env is not None:
            os.environ["SUTANDO_CLAIM_BACKEND"] = saved_env


# A schema-lenient loader must degrade a wrong-SHAPE `delivery` like a typo:
# `.get()` on a scalar raises AttributeError, outside every caller's boundary.
for bad in ("c", ["c"], True, 3):
    check(_resolve_raw_delivery(bad) == "a",
          f"delivery={bad!r} (not an object) falls back to 'a'")
check(_resolve_raw_delivery({"claim_backend": "c"}) == "c",
      "control: a well-formed delivery object still selects 'c'")

print("2. 'delivery' is a KNOWN top-level config key")
# Otherwise a user who configures it gets an unknown-key warning telling them
# the setting they just read about is not real.
check("delivery" in sc._KNOWN_TOP_LEVEL if hasattr(sc, "_KNOWN_TOP_LEVEL")
      else "delivery" in open(REPO / "src" / "sutando_config.py").read(),
      "'delivery' registered so it is not warned as an unknown key")

print("3. the ADAPTER is RUN, not read — each arm constructs its backend")
# Reading the source proves the call is spelled there; only running it proves
# the selection reaches the fence.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                      "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

_spec = importlib.util.spec_from_file_location(
    "dbridge_sel", REPO / "src" / "discord-bridge.py")
db = importlib.util.module_from_spec(_spec)
sys.modules["dbridge_sel"] = db
_spec.loader.exec_module(db)


def _fence_with(env, *, activate=False, malform=False, corrupt_fence=False):
    """Build the fence the way the bridge does, and report what it chose."""
    saved_env = os.environ.get("SUTANDO_CLAIM_BACKEND")
    saved_results, saved_fence = db.RESULTS_DIR, db._PROACTIVE_FENCE
    td = tempfile.mkdtemp(prefix="sel-adapter-")
    try:
        if env is None:
            os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        else:
            os.environ["SUTANDO_CLAIM_BACKEND"] = env
        db.RESULTS_DIR = Path(td)
        db._PROACTIVE_FENCE = None
        if corrupt_fence:
            from ag2_sparrow import outbox as _o
            r = Path(td) / ".outbox-discord-proactive"
            (r / _o.LOCKS_DIR).mkdir(parents=True)
            _o._fence_path(r).write_text("[]")
            _o._STRIPE_MODE.pop(_o._root_key(r), None)
        if malform:
            # A file where C's namespace mkdir expects a directory.
            (Path(td) / ".outbox-discord-proactive").mkdir()
            (Path(td) / ".outbox-discord-proactive" / "tmp").write_text("not a dir")
        if activate:
            from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend
            DesignCClaimBackend(Path(td) / ".outbox-discord-proactive",
                                activate=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fence = db._proactive_fence()
        return type(fence._backend).__name__, buf.getvalue()
    finally:
        db.RESULTS_DIR, db._PROACTIVE_FENCE = saved_results, saved_fence
        if saved_env is None:
            os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        else:
            os.environ["SUTANDO_CLAIM_BACKEND"] = saved_env
        shutil.rmtree(td, ignore_errors=True)


kind, out = _fence_with(None)
check(kind == "DesignAClaimBackend", f"no selection -> Design A (got {kind})")
check(out == "", "and the default arm says nothing")

kind, out = _fence_with("c", activate=True)
check(kind == "DesignCClaimBackend",
      f"claim_backend=c on an ACTIVATED root -> Design C (got {kind})")

# The arm that matters: C is asked for, the root cannot serve it, and the
# proactive leg must keep delivering rather than raise out of the fence.
kind, out = _fence_with("c")
check(kind == "DesignAClaimBackend",
      f"c on an UN-activated root falls back to Design A (got {kind})")
check("Design A" in out and "claim_backend=c" in out,
      "and the fallback is announced — the operator asked for C and got A")

# C's namespace mkdirs raise OSError, not RuntimeError. on_ready() builds the
# fence before the poll loops start, so an escape kills every result poller.
kind, out = _fence_with("c", malform=True)
check(kind == "DesignAClaimBackend",
      f"c on a MALFORMED root falls back to Design A (got {kind})")
check("claim_backend=c" in out and "Design A" in out,
      "the filesystem failure is announced like the activation refusal")
check(any(n in out for n in ("Error", "error")),
      f"and the announcement names the failure class, not just 'unusable': {out.strip()[:90]!r}")

# The whole point of the fallback: delivery keeps working on the same root.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / ".outbox-discord-proactive").mkdir()
    (root / ".outbox-discord-proactive" / "tmp").write_text("a file where a dir belongs")
    saved_env = os.environ.get("SUTANDO_CLAIM_BACKEND")
    saved_results, saved_fence = db.RESULTS_DIR, db._PROACTIVE_FENCE
    try:
        os.environ["SUTANDO_CLAIM_BACKEND"] = "c"
        db.RESULTS_DIR, db._PROACTIVE_FENCE = root, None
        body = root / "proactive-malformed.txt"
        body.write_text("delivery survives a malformed C root")
        with contextlib.redirect_stdout(io.StringIO()):
            fence = db._proactive_fence()
            claim = fence.claim(body)
            moved = claim is not None and claim.exists() and not body.exists()
            fence.confirm(claim)
        check(moved and not claim.exists(),
              "claim+confirm still round-trips on the malformed root")
    finally:
        db.RESULTS_DIR, db._PROACTIVE_FENCE = saved_results, saved_fence
        if saved_env is None:
            os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        else:
            os.environ["SUTANDO_CLAIM_BACKEND"] = saved_env

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

print("4b. a structurally corrupt stripe fence refuses like any other bad fence")
from ag2_sparrow import outbox as _ob
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / ".outbox"
    (root / _ob.LOCKS_DIR).mkdir(parents=True)
    _ob._fence_path(root).write_text("[]")          # valid JSON, wrong shape
    _ob._STRIPE_MODE.pop(_ob._root_key(root), None)
    raised = None
    try:
        DesignCClaimBackend(root)
    except Exception as e:                           # noqa: BLE001 — classifying it IS the test
        raised = e
    check(isinstance(raised, RuntimeError),
          f"non-object fence raises RuntimeError (got {type(raised).__name__})")
    check(isinstance(raised, (RuntimeError, OSError)),
          "so it lands inside the adapter's existing fail-open boundary")

kind, out = _fence_with("c", corrupt_fence=True)
check(kind == "DesignAClaimBackend",
      f"c against a corrupt fence falls back to Design A (got {kind})")
check("claim_backend=c" in out, "and the fallback is announced")

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
# That the adapter SURVIVES this refusal is asserted in section 3 by running it,
# not by reading `except RuntimeError` out of the source.

if fails:
    print(f"\n{len(fails)} FAILURE(S)")
    raise SystemExit(1)
print("\nALL PASS — selection is policy, mapping is the adapter's, default is 'a'")
