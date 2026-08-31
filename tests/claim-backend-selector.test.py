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

# Review findings (sutando-rui, bounded pass on dc38adbc): never-raise is not
# never-warn, and garbage in the env is not an override of a valid config.
check(_resolve(env="zzz", cfg="c") == "c",
      "unparseable env DEFERS to the configured 'c' instead of discarding it")
import io as _io
_err = _io.StringIO()
with contextlib.redirect_stderr(_err):
    _resolve(cfg="design-c")
    _resolve(env="zzz", cfg="c")
check("claim_backend 'design-c'" in _err.getvalue(),
      "an unrecognised config value warns on stderr (typo is visible in logs)")
check("SUTANDO_CLAIM_BACKEND 'zzz'" in _err.getvalue()
      and "keeping configured 'c'" in _err.getvalue(),
      "an unparseable env override warns and names what it kept")
_err2 = _io.StringIO()
with contextlib.redirect_stderr(_err2):
    check(_resolve(cfg="c") == "c", "valid config resolves silently")
check(_err2.getvalue() == "", "and the happy path writes nothing to stderr")


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


def _fence_with(env, *, activate=False, malform=False, corrupt_fence=False,
                legacy_items=0, legacy_shape=None, c_state=None,
                inspect=None):
    """Build the fence the way the bridge does, and report what it chose.
    c_state="operated" drives the REAL C backend to a 5-attempt retry state
    (keweichen's repro); "files" writes raw C-namespace entries so the root
    shows C state without this process activating it. inspect(root) runs
    after the fence for state-preservation assertions."""
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
        if legacy_shape == "file":
            r = Path(td) / ".outbox-discord-proactive"
            r.mkdir(parents=True, exist_ok=True)
            (r / ".items").write_text("not a dir")
        elif legacy_shape == "danglink":
            r = Path(td) / ".outbox-discord-proactive"
            r.mkdir(parents=True, exist_ok=True)
            (r / ".items").symlink_to(Path(td) / "gone-target")
        elif legacy_shape == "unreadable":
            d = Path(td) / ".outbox-discord-proactive" / ".items"
            d.mkdir(parents=True)
            os.chmod(d, 0o000)
        elif legacy_shape == "subdir":
            d = Path(td) / ".outbox-discord-proactive" / ".items" / "nested"
            d.mkdir(parents=True)
        if legacy_items:
            d = Path(td) / ".outbox-discord-proactive" / ".items"
            d.mkdir(parents=True)
            for i in range(legacy_items):
                (d / f"legacy-{i}.json").write_text('{"attempts": 4}')
        if malform:
            # A file where C's namespace mkdir expects a directory.
            (Path(td) / ".outbox-discord-proactive").mkdir()
            (Path(td) / ".outbox-discord-proactive" / "tmp").write_text("not a dir")
        if activate:
            from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend
            DesignCClaimBackend(Path(td) / ".outbox-discord-proactive",
                                activate=True)
        if c_state == "operated":
            from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend
            from ag2_sparrow.delivery_core.contract import DeliveryOutcome
            r = Path(td) / ".outbox-discord-proactive"
            cb = DesignCClaimBackend(r, activate=True)
            cb.publish("hot-item", b"payload")
            for _ in range(5):
                tok = cb.claim("hot-item", "w0")
                cb.complete(tok, DeliveryOutcome.NOT_DELIVERED)
        elif c_state == "files":
            r = Path(td) / ".outbox-discord-proactive"
            (r / "ready").mkdir(parents=True, exist_ok=True)
            (r / "ready" / "hot-item=deadbeef00000000").write_bytes(b"payload")
            (r / "attempts").mkdir(exist_ok=True)
            (r / "attempts" / "hot-item=deadbeef00000000").write_text("5")
        elif c_state == "attempts-file":
            r = Path(td) / ".outbox-discord-proactive"
            r.mkdir(parents=True, exist_ok=True)
            (r / "attempts").write_text("5")
        elif c_state == "ready-danglink":
            r = Path(td) / ".outbox-discord-proactive"
            r.mkdir(parents=True, exist_ok=True)
            (r / "ready").symlink_to(r / "gone-target")
        elif c_state == "epoch-dir":
            r = Path(td) / ".outbox-discord-proactive"
            (r / "protocol-epoch").mkdir(parents=True)
        elif c_state == "staged-fence":
            # write_fence() faulted at its os.replace: staged temp, NO final
            # fence, and no A entries — so the caller's root_is_clean is True.
            r = Path(td) / ".outbox-discord-proactive"
            r.mkdir(parents=True, exist_ok=True)
            (r / "protocol-epoch.tmp").write_text("C", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fence = db._proactive_fence()
        extra = inspect(Path(td) / ".outbox-discord-proactive") if inspect else None
        if inspect:
            return type(fence._backend).__name__, buf.getvalue(), extra
        return type(fence._backend).__name__, buf.getvalue()
    finally:
        db.RESULTS_DIR, db._PROACTIVE_FENCE = saved_results, saved_fence
        if saved_env is None:
            os.environ.pop("SUTANDO_CLAIM_BACKEND", None)
        else:
            os.environ["SUTANDO_CLAIM_BACKEND"] = saved_env
        unreadable = Path(td) / ".outbox-discord-proactive" / ".items"
        if unreadable.is_dir():
            os.chmod(unreadable, 0o755)   # so rmtree can clean an 000 fixture
        shutil.rmtree(td, ignore_errors=True)


kind, out = _fence_with(None)
check(kind == "DesignAClaimBackend", f"no selection -> Design A (got {kind})")
check(out == "", "and the default arm says nothing")

kind, out = _fence_with("c", activate=True)
check(kind == "DesignCClaimBackend",
      f"claim_backend=c on an ACTIVATED root -> Design C (got {kind})")

# 5. A switch must not hide Design A state (Codex transition blocker):
# unmigrated .items refuses C and stays on A, so attempt history remains live.
kind, out = _fence_with("c", activate=True, legacy_items=4)
check(kind == "DesignAClaimBackend",
      f"unmigrated A items -> selector stays on Design A (got {kind})")
check("unmigrated Design A" in out and "4" in out,
      "and the refusal names the count and the migration prerequisite")
check("migration" in out, "and points at the migration as the fix")

# Unexpected .items SHAPES also refuse (fail closed, not fail open):
# a FILE at .items, and a directory containing only a subdirectory.
kind, out = _fence_with("c", activate=True, legacy_shape="file")
check(kind == "DesignAClaimBackend",
      f"a FILE at .items refuses the switch (got {kind})")
kind, out = _fence_with("c", activate=True, legacy_shape="subdir")
check(kind == "DesignAClaimBackend",
      f"a subdir inside .items refuses the switch (got {kind})")
kind, out = _fence_with("c", activate=True, legacy_shape="unreadable")
check(kind == "DesignAClaimBackend",
      f"UNREADABLE .items (OSError on listing) -> fail closed on A (got {kind})")
check("unmigrated Design A" in out,
      "and the refusal message still fires for the unreadable shape")

kind, out = _fence_with("c", activate=True, legacy_shape="danglink")
check(kind == "DesignAClaimBackend",
      f"a DANGLING .items symlink refuses the switch (got {kind})")

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

# 6. REVERSE fence (keweichen P1, exact repro): C operated this root to a
# 5-attempt retry state; selecting A must NOT reset the durable retry budget.
def _attempts(root):
    ad = root / "attempts"
    return sorted(f.read_text() for f in ad.iterdir()) if ad.is_dir() else []

kind, out, att = _fence_with("a", c_state="operated", inspect=_attempts)
check(kind == "TransitionRefusalBackend",
      f"claim_backend=a over a C-OPERATED root is REFUSED (got {kind})")
check("DEFERRED" in out and "C-operated" in out,
      "and the refusal says delivery is deferred, naming the C state")
check(att == ["5"],
      f"C's 5-attempt budget is preserved untouched (got {att})")

kind, out, att = _fence_with(None, c_state="operated", inspect=_attempts)
check(kind == "TransitionRefusalBackend",
      f"the DEFAULT (no selection = a) is refused the same way (got {kind})")
check(att == ["5"], "and preserves the budget the same way")

# The C-unusable fallback must also honor the reverse fence: raw C-namespace
# state + a corrupt stripe fence = C raises, but A is still not safe.
kind, out = _fence_with("c", corrupt_fence=True, c_state="files")
check(kind == "TransitionRefusalBackend",
      f"C-unusable + C live state -> refusal, NOT the A fallback (got {kind})")

# A C-ACTIVATED but never-operated root carries no state to lose: A may run.
kind, out = _fence_with("a", activate=True)
check(kind == "DesignAClaimBackend",
      f"activated-but-unoperated C root still lets A run (got {kind})")

# MIXED state: A entries AND live C state — neither protocol is safe, and
# the forward fence's fall-back-to-A path must not bypass the reverse fence.
kind, out = _fence_with("c", legacy_items=2, c_state="files")
check(kind == "TransitionRefusalBackend",
      f"claim_backend=c over MIXED A+C state -> refusal, not the A fallback (got {kind})")
check("BOTH" in out and "reconcile" in out,
      "and the message names the mixed state and the reconcile requirement")
kind, out = _fence_with("a", legacy_items=2, c_state="files")
check(kind == "TransitionRefusalBackend",
      f"claim_backend=a over MIXED A+C state is refused too (got {kind})")

# Malformed C-namespace SHAPES fail closed too, and startup stays alive: a
# FILE at attempts/, a dangling ready symlink, a DIRECTORY at protocol-epoch.
for shape in ("attempts-file", "ready-danglink", "epoch-dir"):
    kind, out = _fence_with("a", c_state=shape)
    check(kind == "TransitionRefusalBackend",
          f"malformed C shape {shape!r} -> refusal, not silent A (got {kind})")
check("unreadable" in out or "unrecognized" in out or "epoch" in out,
      "and the last refusal names the ambiguous state")

# The refusal backend is claim-inert: bodies stay queued, nothing processed.
from ag2_sparrow.delivery_core.migration import TransitionRefusalBackend
_rb = TransitionRefusalBackend("test")
check(_rb.publish("x", b"p") is False and _rb.claim("x", "w") is None
      and _rb.recover().recovered == [],
      "refusal backend publishes nothing, claims nothing, recovers nothing")

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

print("6. a refusing backend must DEFER — no rename, no send path returned")
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("pcf", REPO / "src" / "proactive_claim_fence.py")
_pcf = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_pcf)
from ag2_sparrow.delivery_core.migration import TransitionRefusalBackend


def _fence_over(backend):
    fence = _pcf.ProactiveClaimFence.__new__(_pcf.ProactiveClaimFence)
    fence._backend = backend
    fence._worker = "t"
    fence._tokens = {}
    return fence


with tempfile.TemporaryDirectory() as td:
    txt = Path(td) / "proactive-refuse-check.txt"
    txt.write_text("body")
    claim = _fence_over(TransitionRefusalBackend("A over live C state")).claim(txt)
    check(claim is None, "refusing backend: claim() returns None (distinct skip)")
    check(txt.exists(), "refusing backend: the .txt stays queued (no rename)")
    check(not txt.with_suffix(".sending").exists(),
          "refusing backend: no .sending claim is created")

# Negative control: an ERRORING (not refusing) backend renames and returns the
# claim, so the checks above discriminate refusal rather than pass vacuously.


class _ErroringBackend:
    def publish(self, *a): raise RuntimeError("backend down")
    def claim(self, *a): raise RuntimeError("backend down")


with tempfile.TemporaryDirectory() as td:
    txt = Path(td) / "proactive-degrade-check.txt"
    txt.write_text("body")
    claim = _fence_over(_ErroringBackend()).claim(txt)
    check(claim == txt.with_suffix(".sending"),
          "erroring backend: file-only degraded cycle still returns the claim")
    check(not txt.exists() and claim.exists(),
          "erroring backend: the rename DID happen (control discriminates)")

# kewei r-latest P1: the fence must GATE selection and never raise into startup.
from ag2_sparrow.delivery_core.migration import (  # noqa: E402
    EPOCH_FILE, classify_epoch, c_selection_allowed, write_fence,
    staged_fence_path)


def _root(content=None, kind="file"):
    d = Path(tempfile.mkdtemp(prefix="epoch-"))
    p_ = d / EPOCH_FILE
    if kind == "file" and content is not None:
        p_.write_bytes(content)
    elif kind == "dir":
        p_.mkdir()
    elif kind == "dangling":
        p_.symlink_to(d / "does-not-exist")
    elif kind == "staged":
        # write_fence() interrupted between its temp write and os.replace
        staged_fence_path(d).write_text("C", encoding="utf-8")
    return d


# (label, root, expected state, C allowed on a CLEAN root)
_EPOCH_CASES = [
    ("A",            _root(b"A"),              "ok",         False),
    ("C",            _root(b"C"),              "ok",         True),
    ("unknown",      _root(b"garbage"),        "unknown",    False),
    ("missing",      _root(None),              "missing",    True),
    ("dangling",     _root(kind="dangling"),   "unreadable", False),
    ("staged",       _root(kind="staged"),     "staged",     False),
    ("invalid-utf8", _root(b"\xff"),           "unreadable", False),
    ("dir",          _root(kind="dir"),        "unreadable", False),
]
for _label, _r, _want_state, _want_c in _EPOCH_CASES:
    _st, _ = classify_epoch(_r)
    check(_st == _want_state,
          f"epoch {_label}: classify_epoch -> {_st!r}, want {_want_state!r}")
    check(c_selection_allowed(_r, True)[0] is _want_c,
          f"epoch {_label}: C-on-clean-root should be {_want_c}")
    # A dirty root may NEVER bootstrap C, whatever the fence says about absence
    if _want_state == "missing":
        check(c_selection_allowed(_r, False)[0] is False,
              f"epoch {_label}: C must be refused when the root holds A entries")

# The row kewei measured: write_fence(root, "A") then C was selected anyway.
_fenced_a = _root(None)
write_fence(_fenced_a, "A")
check(c_selection_allowed(_fenced_a, True)[0] is False,
      "written_epoch=A must REFUSE C (the migration owner still says A)")

# Crash before the final fence: C state present, fence still A -> refuse.
_crashed = _root(b"A")
(_crashed / "ready").mkdir()
check(c_selection_allowed(_crashed, True)[0] is False,
      "crash-before-final-fence: A fence with C namespaces must refuse C")

# The row above writes an explicit final A fence, so it cannot reach the
# default-A-by-ABSENCE window. Fault write_fence() at its os.replace instead.
_staged = _root(kind="staged")
check(not (_staged / EPOCH_FILE).exists(),
      "premise: the faulted write leaves NO final fence (else this is the A-fence row)")
check(staged_fence_path(_staged).exists(), "premise: the staged temp is present")
_allow, _why = c_selection_allowed(_staged, True)
check(_allow is False, f"interrupted-fence window must refuse C on a clean root (got {_why!r})")
# CONTROL: same helper, no fault -> a genuinely clean root must STILL bootstrap,
# or the fix has simply disabled the feature rather than closed the window.
check(c_selection_allowed(_root(None), True)[0] is True,
      "control: an untouched clean root must still bootstrap C")
# CONTROL: a COMPLETED write_fence leaves no temp, so it must not read as staged.
_done = _root(None)
write_fence(_done, "C")
check(classify_epoch(_done)[0] == "ok" and c_selection_allowed(_done, True)[0] is True,
      "control: a completed fence write must not be mistaken for a staged one")

# At the ADAPTER: the unit check above proves the policy, this proves the
# shipped selector consumes it.
kind, out = _fence_with("c", activate=True, c_state="staged-fence")
check(kind != "DesignCClaimBackend",
      f"interrupted-fence window must not start C through the adapter (got {kind})")
check(kind == "DesignAClaimBackend",
      f"and must fall back to Design A, the epoch owner's authoritative protocol (got {kind})")
check("refused" in out and "staged" in out,
      f"the refusal must be ANNOUNCED and name the staged fence, not silent: {out!r}")
# CONTROL: without the fault C must still start, or this passes on a build
# where C never starts at all.
kind_ok, _ = _fence_with("c", activate=True)
check(kind_ok == "DesignCClaimBackend",
      f"control: the SAME activated root without the staged temp must still "
      f"select C — the pair differs only by the fault (got {kind_ok})")

# kewei r-latest P2: a Protocol class attribute is a REQUIRED structural member.

from ag2_sparrow.delivery_core.contract import ClaimBackend  # noqa: E402
from ag2_sparrow.delivery_core.migration import TransitionRefusalBackend  # noqa: E402

_required = set(getattr(ClaimBackend, "__protocol_attrs__", set()))
for _name, _cls in (("DesignAClaimBackend", DesignAClaimBackend),
                    ("DesignCClaimBackend", DesignCClaimBackend),
                    ("TransitionRefusalBackend", TransitionRefusalBackend)):
    _missing = sorted(a for a in _required if not hasattr(_cls, a))
    check(not _missing, f"{_name} must satisfy ClaimBackend; missing={_missing}")


if fails:
    print(f"\n{len(fails)} FAILURE(S)")
    raise SystemExit(1)
print("\nALL PASS — selection is policy, mapping is the adapter's, default is 'a'")
