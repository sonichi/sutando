"""Drive claim_machine_b's machine over designC.

- designC is injected as the 'designB' module BEFORE claim_machine_b imports it.
- Constructor-time activation is patched onto the module-level BClaimDriver and
  the machine is invoked via M.main() — NEVER runpy.run_path(run_name='__main__'),
  which re-executes the file as a fresh module whose classes silently ignore
  every patch (observed: an unpatched run reproducing the lazy-activation crash
  while the wrapper claimed activation was in place).
- _replay_tokens is wrapped to append recorded exceptions' full tracebacks to
  the assertion, so a failure names its true site.
"""
import importlib.util
import os
import sys
import traceback
HERE = os.path.dirname(os.path.abspath(__file__))
B_EVAL = os.path.join(os.path.dirname(HERE), "designB-eval")
sys.path.insert(0, B_EVAL)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "wt-c-lane", "tests", "_helpers"))
spec = importlib.util.spec_from_file_location("designB", os.path.join(HERE, "designC.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["designB"] = mod
spec.loader.exec_module(mod)
import claim_machine_b as M
import fcntl as _real_fcntl
import time as _time
from pathlib import Path
from claim_machine_harness import OsProxy, GatedPath, FcntlProxy

class _NoExclusionFcntl:
    """SINGLE-VARIABLE control: LOCK_EX acquisition becomes a no-op, so the
    lock exists, the fence exists, activation runs, the sweep runs, lock
    FILES are still created — but exclusion is gone. C_NEUTER_FLOCK=1 arms
    it. Removing exactly one mechanism is what lets a control failure NAME
    what it falsified; the old B_NEUTER path removed lock+activation+fence
    together and could only claim "the composite is load-bearing"."""
    def __init__(self, real):
        self._real = real
    def __getattr__(self, name):
        val = getattr(self._real, name)
        if name != "flock" or not callable(val):
            return val
        def flock(fd, op):
            if op & self._real.LOCK_EX and not op & self._real.LOCK_UN:
                return None                      # acquisition no-ops: no exclusion
            return val(fd, op)
        return flock

_NEUTER_FLOCK = os.environ.get("C_NEUTER_FLOCK") == "1"
_NEUTER_LINK = os.environ.get("C_NEUTER_LINK") == "1"
_NEUTER_RENAME = os.environ.get("C_NEUTER_RENAME") == "1"


class _NonAtomicRename:
    """SINGLE-VARIABLE control #3: os.rename loses atomicity — becomes
    read + write-dst + unlink-src, with gated boundaries between the steps.
    Motivated by controls #1/#2 both passing: C's claim is a single atomic
    rename (ready/key -> inflight/token), so single-owner rests on THIS
    mechanism; flock guards multi-step transitions and link serves other
    legs. This is the control that matches the single-owner invariant."""
    def __init__(self, real_os):
        self._os = real_os
    def __getattr__(self, name):
        val = getattr(self._os, name)
        if name != "rename" or not callable(val):
            return val
        def rename(src, dst, **kw):
            with open(src, "rb") as fh:          # raises ENOENT like rename
                data = fh.read()
            with open(dst, "wb") as fh:
                fh.write(data)
            self._os.unlink(src)
        return rename


class _NoExclusionLink:
    """SINGLE-VARIABLE control #2: os.link loses create-if-absent — on
    FileExistsError the destination is overwritten, so a losing claimer
    "wins" too. Everything else (flock, fence, activation, sweep) intact.
    Motivated by the flock-only control PASSING at 800x60: C's single-owner
    invariant rests on the link primitive, not the stripe lock — so the
    control for THAT invariant must neuter THIS mechanism."""
    def __init__(self, real_os):
        self._os = real_os
    def __getattr__(self, name):
        val = getattr(self._os, name)
        if name != "link" or not callable(val):
            return val
        def link(src, dst, **kw):
            try:
                return val(src, dst, **kw)
            except FileExistsError:
                self._os.unlink(dst)
                return val(src, dst, **kw)
        return link

_orig_init = M.BClaimDriver.__init__
def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    # SEAM RE-SITING: designC delegates locking to ob, whose os/fcntl/Path are
    # its own module globals — invisible to the gate unless proxied here too.
    # Without this, ob's flocks stall the scheduler (observed wedge) and its
    # interleavings are silently unexplored.
    self.in_flock = {a2: False for a2 in M.ACTORS}
    mod.ob.os = OsProxy(os, self.gate, self._root_holder)
    mod.ob.Path = GatedPath           # class attrs already bound by _orig_init
    if _NEUTER_FLOCK:
        mod.ob.fcntl = _NoExclusionFcntl(_real_fcntl)
    else:
        mod.ob.fcntl = FcntlProxy(_real_fcntl, self)
    if _NEUTER_LINK:
        # layer over the OsProxy the driver just installed on designC
        mod.os = _NoExclusionLink(mod.os)
    if _NEUTER_RENAME:
        mod.os = _NonAtomicRename(mod.os)
    # Constructor-time activation (the landing rule): before any thread's
    # first fence read for this root. Unified module: init() IS that rule;
    # eval module fallback keeps the wrapper runnable against both.
    if hasattr(mod, "init"):
        mod.init(self.root)
    else:
        mod.ob.activate_lock_striping(Path(self.root))
        mod._STRIPED.add(os.path.realpath(str(self.root)))
M.BClaimDriver.__init__ = _init

# FIXTURE FIX: the machine's plant_dead_claim writes a 4-part B-format ghost
# (key~ghost~pid~birth); designC.TOKEN_PARTS is 5 (adds generation), so C's
# recover() skipped the ghost in EVERY prior run — the whole recover path
# (live-holder check, _move re-arm, quarantine) was dead code in the matrix.
# Plant a 5-part C-format ghost so recover's multi-step legs are exercised.
import json as _json
def _plant_dead_claim_c(self):
    if any(self.busy.values()) or self._believers():
        return
    key = mod.safe_key(M.ITEM)
    d = Path(self.root) / mod.INFLIGHT
    if d.exists() and any(f.name.split(mod.SEP)[0] == key for f in d.iterdir()):
        return
    d.mkdir(parents=True, exist_ok=True)
    ghost = d / mod.SEP.join((key, "ghost", str(M.DEAD_PID), "1", "0"))
    ghost.write_text(_json.dumps({"body": "payload"}), encoding="utf-8")
M.BClaimDriver.plant_dead_claim = _plant_dead_claim_c

# in_flock-aware step: skip an actor parked in a flock wait instead of
# burning the arrival window (mirrors the A-driver's flock_skip).
def _step(self, actor, n):
    for _ in range(n):
        if not self.busy[actor]:
            return
        if not self.gate.at_gate[actor].wait(timeout=0.05):
            if self.in_flock.get(actor):
                return
            deadline = _time.time() + 0.95
            arrived = False
            while _time.time() < deadline:
                if self.gate.at_gate[actor].wait(timeout=0.1):
                    arrived = True
                    break
                if not self.busy[actor] or self.in_flock.get(actor):
                    return
            if not arrived:
                return
        self.gate.at_gate[actor].clear()
        self.gate.go[actor].release()
        deadline = _time.time() + 1.0
        while (_time.time() < deadline and self.busy[actor]
               and not self.gate.at_gate[actor].is_set()
               and not self.in_flock.get(actor)):
            _time.sleep(0.0005)
M.BClaimDriver.step = _step

_orig_replay = M.BClaimDriver._replay_tokens
def _replay(self):
    try:
        return _orig_replay(self)
    except AssertionError as e:
        details = []
        for rec in list(self.done_log):
            try:
                _, actor, op, res, _ = rec
            except Exception:
                continue
            if isinstance(res, Exception):
                details.append(f"--- {actor} {op}:\n" + "".join(
                    traceback.format_exception(type(res), res, res.__traceback__)))
        raise AssertionError(str(e) + "\n" + "\n".join(details)) from None
M.BClaimDriver._replay_tokens = _replay

M.main()
