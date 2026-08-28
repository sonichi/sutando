#!/usr/bin/env python3
"""#3279 layer 3: the running bridge self-reports its identity and the
health probe compares it to the checkout. Pins: loader pre-exec injection
survives the exec; the status payload carries the runtime block; the two
engine counters increment at their real send sites; probe verdicts for
fresh / drifted / pre-report sidecars."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


os.environ.setdefault("SUTANDO_TEST_MODE", "1")
os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:1"
os.environ["REMOTE_TASK_TOKEN"] = "t"

# ── loader injection survives the exec; sha matches the real checkout ──────
spec = importlib.util.spec_from_file_location(
    "rgb_loader", REPO / "src" / "remote-gateway-bridge.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
head = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                               text=True).strip()
check(m.RUNTIME_IDENTITY.get("build_sha") == head,
      "loader injects the checkout HEAD as build_sha (pre-exec, exec-proof)")
import hashlib as _hl
check(m.RUNTIME_IDENTITY.get("loader_sha256") == _hl.sha256(
      (REPO / "src" / "remote-gateway-bridge.py").read_bytes()).hexdigest(),
      "loader self-hash equals its on-disk bytes at startup")
# fail-closed arms of the loader helpers (restored — a section rewrite ate them)
check(m._build_sha("/nonexistent-dir-xyz") is None,
      "loader _build_sha fails closed (None) outside git with no manifest")
check(m._build_sha("/etc/hosts") is None,
      "loader _build_sha fails closed on a non-directory repo argument")

# bundle install: no git, but ENGINE_MANIFEST.json carries the revision
import tempfile as _tf
import json as _json
with _tf.TemporaryDirectory() as _bd:
    _mf = Path(_bd) / "ENGINE_MANIFEST.json"
    _mf.write_text(_json.dumps({"sha": "23c3c94d4b2068b647ef55c507bfa0c13ee100ce",
                                "built_at": "2026-08-21T17:28:59Z"}))
    check(m._build_sha(_bd) == "23c3c94d4b2068b647ef55c507bfa0c13ee100ce",
          "loader falls back to the manifest sha on a non-git install")
    _mf.write_text(_json.dumps({"sha": 42, "built_at": "x"}))
    check(m._build_sha(_bd) is None,
          "non-string manifest sha is refused, not injected")
    _mf.write_text("not json")
    check(m._build_sha(_bd) is None, "corrupt manifest fails closed")
check(m._sha256_of("/nonexistent-file-abc") is None,
      "loader _sha256_of fails closed (None) on an unreadable path")

check(m.RUNTIME_IDENTITY.get("module_sha256") == _hl.sha256(
      (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" /
       "remote_gateway_bridge.py").read_bytes()).hexdigest(),
      "canonical-module digest recorded pre-exec equals disk")
check(str(m.RUNTIME_IDENTITY.get("entrypoint", "")).endswith(
    "src/remote-gateway-bridge.py"), "entrypoint names the canonical loader")

# ── the status payload carries the runtime block ───────────────────────────
with tempfile.TemporaryDirectory() as td:
    m.GATEWAY_STATUS_FILE = Path(td) / "gateway-status.json"
    m._emit_gateway_status(True)
    rt = json.loads(m.GATEWAY_STATUS_FILE.read_text())["runtime"]
    check(rt["build_sha"] == head and "engine" in rt
          and rt["core_confirmed"] == 0 and rt["legacy_sends"] == 0,
          "status sidecar carries {build_sha, engine, both counters}")

    # ── counters increment at the real sites and re-emit ───────────────────
    m._ENGINE_COUNTS["core_confirmed"] += 0  # anchor: the dict is the API
    class _Res:
        pass
    # Drive _deliver_result_payload's confirmed branch through a stub core.
    class _StubBackend:
        # Must equal the singleton's keyed root or _delivery_core() rebuilds
        # a REAL core over the stub (the exact keying the prod code uses).
        root = m.RESULTS_DIR / f".outbox{m._INST_SUFFIX}"
        def publish(self, *a): return True
        def attempts(self, *a): return 0
    class _StubCore:
        backend = _StubBackend()
        provider = object()
        worker = "w"
        def deliver_one(self, *a, **k):
            r = _Res()
            r.status = m.DrainStatus.ATTEMPTED
            r.outcome = m.CoreDeliveryOutcome.CONFIRMED
            return r
    m._DELIVERY_CORE = _StubCore()
    ok = m._deliver_result_payload("tid-1", "tid-1", "body")
    check(ok and m._ENGINE_COUNTS["core_confirmed"] == 1,
          "a CONFIRMED DeliveryCore result increments core_confirmed")
    m._emit_gateway_status(True)
    rt = json.loads(m.GATEWAY_STATUS_FILE.read_text())["runtime"]
    check(rt["core_confirmed"] == 1, "the incremented counter reaches the sidecar")
    check("StubBackend" in rt["engine"],
          "engine string names the LIVE backend/provider pair")

# ── probe verdicts: absent=idle, damaged=warn, valid=verified ──────────────
hspec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(hspec)
try:
    hspec.loader.exec_module(hc)
except SystemExit:
    pass
CANON = str((REPO / "src" / "remote-gateway-bridge.py").resolve())
GOOD = {"build_sha": "a" * 40, "entrypoint": CANON, "engine": "E",
        "core_confirmed": 3, "legacy_sends": 1}
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "gateway-status.json"
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "ok" and "nothing to verify" in r["detail"],
          "absent sidecar: probe idles")
    for label, content in (("corrupt JSON", "{not json"), ("empty file", ""),
                           ("non-object", "[1,2]")):
        p.write_text(content)
        r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
        check(r["status"] == "warn",
              f"{label}: WARN, never rendered as the absent-idle pass")
    p.write_text(json.dumps({"connected": True}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "predates" in r["detail"],
          "no runtime block: warn names the restart remedy")
    p.write_text(json.dumps({"runtime": {}}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "malformed" in r["detail"],
          "empty runtime block: warn malformed (required keys enforced)")
    for label, block in (("non-dict runtime", "a string"),
                         ("no entrypoint", {"build_sha": "a" * 40}),
                         ("no engine", {"build_sha": "a" * 40,
                                        "entrypoint": CANON})):
        p.write_text(json.dumps({"runtime": block}))
        r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
        check(r["status"] == "warn", f"{label}: warn malformed")
    bad = dict(GOOD, legacy_sends=-2)
    p.write_text(json.dumps({"runtime": bad}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "non-negative" in r["detail"],
          "negative counter: warn malformed")
    p.write_text(json.dumps({"runtime": dict(GOOD, build_sha="b" * 40)}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "build drift" in r["detail"],
          "sha mismatch: warn drift with both shas")
    p.write_text(json.dumps({"runtime": dict(
        GOOD, entrypoint="/tmp/other/src/remote-gateway-bridge.py")}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "non-canonical" in r["detail"],
          "suffix-matching foreign path: warn non-canonical (resolved compare)")
    # build_sha present but garbage (not a sha string): still malformed
    p.write_text(json.dumps({"runtime": dict(GOOD, build_sha=12345)}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "not a sha string" in r["detail"],
          "garbage build_sha: warn malformed (None is the only allowed absence)")

    # BUNDLE INSTALL (yixuan P1): build_sha=None + no git + no manifest is an
    # install shape, not damage — digests become the verification evidence
    import hashlib as _hl
    bundle = Path(td) / "bundle"
    (bundle / "src").mkdir(parents=True)
    (bundle / "packages" / "ag2-sparrow" / "ag2_sparrow").mkdir(parents=True)
    ldr = bundle / "src" / "remote-gateway-bridge.py"
    mod = bundle / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"
    ldr.write_text("# loader bytes")
    mod.write_text("# module bytes")
    bundle_rt = {"build_sha": None, "entrypoint": str(ldr), "engine": "E",
                 "core_confirmed": 0, "legacy_sends": 0,
                 "loader_sha256": _hl.sha256(ldr.read_bytes()).hexdigest(),
                 "module_sha256": _hl.sha256(mod.read_bytes()).hexdigest()}
    p.write_text(json.dumps({"runtime": bundle_rt}))
    saved_repo = hc.REPO_DIR
    try:
        hc.REPO_DIR = bundle
        r = hc.check_runtime_identity(path=p)
        check(r["status"] == "ok" and "revision comparison unavailable" in r["detail"],
              "bundle (no git/manifest): OK via digests, never 'malformed'")
        # digest drift is still caught with no revision authority at all
        mod.write_text("# CHANGED bytes")
        r = hc.check_runtime_identity(path=p)
        check(r["status"] == "warn" and "bytes drift" in r["detail"],
              "bundle: digest drift warns even without any sha to compare")
        mod.write_text("# module bytes")
        # manifest as the revision authority: sha compared, built_at ignored
        (bundle / "ENGINE_MANIFEST.json").write_text(json.dumps(
            {"sha": "c" * 40, "built_at": "2026-08-21T17:28:59Z"}))
        p.write_text(json.dumps({"runtime": dict(bundle_rt, build_sha="c" * 40)}))
        r = hc.check_runtime_identity(path=p)
        check(r["status"] == "ok" and "sha=cccccccc" in r["detail"],
              "bundle with manifest: sha verified against manifest sha")
        p.write_text(json.dumps({"runtime": dict(bundle_rt, build_sha="d" * 40)}))
        r = hc.check_runtime_identity(path=p)
        check(r["status"] == "warn" and "build drift" in r["detail"],
              "bundle with manifest: mismatched sha still warns drift")
    finally:
        hc.REPO_DIR = saved_repo
    p.write_text(json.dumps({"runtime": GOOD}))
    # symlink-loop entrypoint (reviewer P1): probe warns, never raises
    loop = Path(td) / "loop-link"
    try:
        loop.symlink_to(loop)
    except OSError:
        pass
    p.write_text(json.dumps({"runtime": dict(GOOD, entrypoint=str(loop))}))
    try:
        r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
        raised = None
    except Exception as e:                          # noqa: BLE001
        r, raised = None, repr(e)
    check(raised is None and r["status"] == "warn",
          f"symlink-loop entrypoint: warn, probe never raises ({raised})")
    # same-HEAD byte-change (reviewer P1): reported loader hash != disk -> warn
    import hashlib
    p.write_text(json.dumps({"runtime": dict(
        GOOD, loader_sha256="0" * 64)}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "bytes drift" in r["detail"],
          "same-HEAD loader byte change: warn bytes drift")
    disk_fp = hashlib.sha256(
        (REPO / "src" / "remote-gateway-bridge.py").read_bytes()).hexdigest()
    p.write_text(json.dumps({"runtime": dict(GOOD, loader_sha256=disk_fp)}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "ok", "matching loader hash: still ok")
    # same-HEAD change to the CANONICAL module (reviewer P1): warn
    p.write_text(json.dumps({"runtime": dict(GOOD, loader_sha256=disk_fp,
                                             module_sha256="0" * 64)}))
    r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
    check(r["status"] == "warn" and "module bytes drift" in r["detail"],
          "same-HEAD canonical-module byte change: warn module bytes drift")
    # hash-compare disk-read failure (REPO_DIR unreadable): skip, never raise
    p.write_text(json.dumps({"runtime": dict(GOOD, loader_sha256="1" * 64)}))
    saved_repo2 = hc.REPO_DIR
    try:
        hc.REPO_DIR = Path(td) / "empty-nonrepo"
        r = hc.check_runtime_identity(path=p, head_sha="a" * 40)
        check(r["status"] in ("ok", "warn") and "drift" not in r["detail"],
              "loader file unreadable on disk: hash compare skips, never raises")
    finally:
        hc.REPO_DIR = saved_repo2
    real_head = subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    p.write_text(json.dumps({"runtime": dict(GOOD, build_sha=real_head)}))
    r = hc.check_runtime_identity(path=p)
    check(r["status"] == "ok" and "legacy_sends=1" in r["detail"],
          "valid block + matching derived HEAD + canonical resolved entrypoint: ok")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
