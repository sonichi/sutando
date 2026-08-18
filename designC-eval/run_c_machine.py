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
    mod.ob.fcntl = FcntlProxy(_real_fcntl, self)
    # Constructor-time activation (the landing rule): before any thread's
    # first fence read for this root.
    mod.ob.activate_lock_striping(Path(self.root))
    mod._STRIPED.add(os.path.realpath(str(self.root)))
M.BClaimDriver.__init__ = _init

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
