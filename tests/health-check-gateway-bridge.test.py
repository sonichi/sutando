#!/usr/bin/env python3
"""
Tests for health-check.py's check_gateway_bridge() — the ag2.space gateway
bridge (remote-gateway-bridge.py) health probe.

Context (PR #2067): the health check probed telegram/discord bridges but not the
gateway bridge that carries MOBILE-app messages. So when it died (2026-07-10:
dead since Jul 7), nothing reported it and mobile messages stranded in the cloud
for 3 days. This check closes that gap; these tests drive every branch:

  * NOT configured (no token in env or channels/ag2space/.env) → None (silent).
  * configured + one running process                          → ok.
  * configured + zero processes                               → warn (down).
  * configured + duplicate processes                          → warn (pileup).
  * configured via the channel .env file (not env var)        → detected.

pgrep + the config source are mocked so the test is hermetic (no live bridge).

Run: python3 tests/health-check-gateway-bridge.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok  ' if cond else '  FAIL '}{name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def _pgrep(returncode, stdout):
    """A subprocess.run mock returning a fixed pgrep result."""
    def _side_effect(cmd, **kwargs):
        r = unittest.mock.MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        return r
    return _side_effect


def _run(*, env=None, gw_env_path=None, pgrep_rc=1, pgrep_out="", pgrep_raises=False, serving=None):
    """Call check_gateway_bridge() with env, the channel-.env path, and the
    pgrep result all controlled. env=None means the token vars are cleared.
    pgrep_raises=True makes subprocess.run raise (the except-branch path).
    `serving` pins the gateway-status sidecar verdict (None = no opinion) so no
    case depends on whether the host running the tests happens to have a live
    sidecar — without it, "one process -> ok" flips to warn on a real host."""
    env = env or {}
    # Clear both token vars, then apply the requested env.
    base = {k: v for k, v in hc.os.environ.items()
            if k not in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")}
    base.update(env)
    run_mock = (unittest.mock.Mock(side_effect=OSError("pgrep exploded"))
                if pgrep_raises else unittest.mock.Mock(side_effect=_pgrep(pgrep_rc, pgrep_out)))
    with unittest.mock.patch.dict(hc.os.environ, base, clear=True), \
         unittest.mock.patch.object(hc, "claude_home_path", return_value=gw_env_path), \
         unittest.mock.patch.object(hc.subprocess, "run", run_mock):
        with unittest.mock.patch.object(hc, "_gateway_serving", lambda *a, **k: serving):
            return hc.check_gateway_bridge()


def main() -> int:
    # 1) NOT configured (no env token, channel .env absent) → None
    missing = Path(tempfile.gettempdir()) / "sutando-gw-nonexistent-xyz" / ".env"
    r = _run(env={}, gw_env_path=missing)
    check("not configured → None", r is None, f"got {r!r}")

    # 2) configured via env, one running process → ok
    r = _run(env={"REMOTE_TASK_TOKEN": "tok"}, gw_env_path=missing, pgrep_rc=0, pgrep_out="12345\n")
    check("configured + running → ok", r is not None and r["status"] == "ok", f"got {r!r}")
    check("ok detail says running", r and "running" in r["detail"], f"got {r!r}")

    # 3) configured, zero processes → warn (down)
    r = _run(env={"REMOTE_TASK_TOKEN": "tok"}, gw_env_path=missing, pgrep_rc=1, pgrep_out="")
    check("configured + down → warn", r is not None and r["status"] == "warn", f"got {r!r}")
    check("down detail names the impact", r and "will not be delivered" in r["detail"], f"got {r!r}")

    # 4) configured, duplicate processes → warn (pileup)
    r = _run(env={"REMOTE_TASK_TOKEN": "tok"}, gw_env_path=missing, pgrep_rc=0, pgrep_out="111\n222\n")
    check("configured + duplicates → warn", r is not None and r["status"] == "warn", f"got {r!r}")
    check("duplicate detail says multiple", r and "multiple processes" in r["detail"], f"got {r!r}")

    # 5) configured via the channel .env file (not env var) → detected as ok
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("# comment\nREMOTE_TASK_TOKEN=abc123\n")
        env_file = Path(f.name)
    try:
        r = _run(env={}, gw_env_path=env_file, pgrep_rc=0, pgrep_out="999\n")
        check("configured via .env file → ok", r is not None and r["status"] == "ok", f"got {r!r}")
    finally:
        env_file.unlink(missing_ok=True)

    # 6) configured, but pgrep itself raises → treated as not-running (warn),
    #    covering the except branch.
    r = _run(env={"REMOTE_TASK_TOKEN": "tok"}, gw_env_path=missing, pgrep_raises=True)
    check("pgrep raises → warn (down)", r is not None and r["status"] == "warn", f"got {r!r}")

    # 7) configured, but reading the channel .env raises OSError → treated as
    #    not-configured (None), covering the config-read except branch. Use a
    #    mock Path whose exists() is True but read_text() raises.
    bad_env = unittest.mock.MagicMock()
    bad_env.exists.return_value = True
    bad_env.read_text.side_effect = OSError("cannot read .env")
    r = _run(env={}, gw_env_path=bad_env)
    check("config read raises → None", r is None, f"got {r!r}")

    # 6) sidecar precedence — a live PROCESS is not a serving CONNECTION.
    r = _run(env={"REMOTE_TASK_TOKEN": "x"}, pgrep_rc=0, pgrep_out="4242\n", serving=False)
    check("running process + sidecar NOT connected → warn",
          r["status"] == "warn" and "NOT serving" in r["detail"], f"got {r!r}")

    r = _run(env={"REMOTE_TASK_TOKEN": "x"}, pgrep_rc=0, pgrep_out="4242\n", serving=True)
    check("running process + sidecar connected → ok",
          r["status"] == "ok" and "connected" in r["detail"], f"got {r!r}")

    r = _run(env={"REMOTE_TASK_TOKEN": "x"}, pgrep_rc=0, pgrep_out="4242\n", serving=None)
    check("running process + no sidecar opinion → ok (pre-sidecar behaviour)",
          r["status"] == "ok" and r["detail"] == "running", f"got {r!r}")

    r = _run(env={"REMOTE_TASK_TOKEN": "x"}, pgrep_rc=1, pgrep_out="", serving=True)
    check("no process still warns even if a sidecar claims connected",
          r["status"] == "warn" and "NOT running" in r["detail"], f"got {r!r}")

    # 7) _gateway_serving() parsing branches
    import json as _json, time as _time, tempfile as _tf
    def _sc(body):
        f = Path(_tf.mkdtemp()) / "gateway-status.json"
        f.write_text(_json.dumps(body))
        return f
    now = _time.time()
    check("_gateway_serving: fresh + connected → True",
          hc._gateway_serving(_sc({"connected": True, "ts": now}), now) is True)
    check("_gateway_serving: fresh + disconnected → False",
          hc._gateway_serving(_sc({"connected": False, "ts": now}), now) is False)
    check("_gateway_serving: stale → None (no opinion)",
          hc._gateway_serving(_sc({"connected": True, "ts": now - 10000}), now) is None)
    check("_gateway_serving: missing ts → None",
          hc._gateway_serving(_sc({"connected": True}), now) is None)
    check("_gateway_serving: absent file → None",
          hc._gateway_serving(Path(_tf.mkdtemp()) / "nope.json", now) is None)

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall check_gateway_bridge cases passed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
