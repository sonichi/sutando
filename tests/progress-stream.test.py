#!/usr/bin/env python3
"""Tests for src/progress_stream.py — the pure progress-streaming helpers.

Run directly: `python3 tests/progress-stream.test.py` (no pytest dependency,
matching the repo's other *.test.py suites).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import progress_stream as ps  # noqa: E402

_fails = []


def check(name, cond):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}")
        _fails.append(name)


# --- stream_enabled (feature flag, default OFF) ---
os.environ.pop("SUTANDO_PROGRESS_STREAM", None)
check("flag default OFF", ps.stream_enabled() is False)
os.environ["SUTANDO_PROGRESS_STREAM"] = "1"
check("flag on when =1", ps.stream_enabled() is True)
os.environ["SUTANDO_PROGRESS_STREAM"] = "true"
check("flag off when !=1 (strict)", ps.stream_enabled() is False)
os.environ.pop("SUTANDO_PROGRESS_STREAM", None)

# --- stream_enabled config-file fallback (env unset → bridges.progress_stream) ---
import sutando_config as sc  # noqa: E402

_orig_resolve = sc.resolve_progress_stream
try:
    os.environ.pop("SUTANDO_PROGRESS_STREAM", None)
    sc.resolve_progress_stream = lambda *a, **k: True
    check("config True → ON when env unset", ps.stream_enabled() is True)
    sc.resolve_progress_stream = lambda *a, **k: False
    check("config False → OFF when env unset", ps.stream_enabled() is False)
    sc.resolve_progress_stream = lambda *a, **k: None
    check("config unset → OFF (default)", ps.stream_enabled() is False)
    # env is the override — it wins over config either way
    os.environ["SUTANDO_PROGRESS_STREAM"] = "0"
    sc.resolve_progress_stream = lambda *a, **k: True
    check("env=0 overrides config True → OFF", ps.stream_enabled() is False)
    os.environ["SUTANDO_PROGRESS_STREAM"] = "1"
    sc.resolve_progress_stream = lambda *a, **k: False
    check("env=1 overrides config False → ON", ps.stream_enabled() is True)
    # config reader raising must fall through to the safe default (OFF), never
    # break the caller — exercises the except branch in stream_enabled().
    os.environ.pop("SUTANDO_PROGRESS_STREAM", None)

    def _boom(*a, **k):
        raise RuntimeError("config unreadable")

    sc.resolve_progress_stream = _boom
    check("config read raises → OFF (safe)", ps.stream_enabled() is False)
finally:
    sc.resolve_progress_stream = _orig_resolve
    os.environ.pop("SUTANDO_PROGRESS_STREAM", None)

# --- resolve_progress_stream: exercise the REAL config reader (not mocked) ---
with tempfile.TemporaryDirectory() as _cd:
    _cfgp = Path(_cd) / "sutando.config.json"

    def _resolve_with(val):
        sc._reset_cache_for_tests()
        body = {"bridges": {"progress_stream": val}} if val is not None else {"core": {"runtime": "claude"}}
        _cfgp.write_text(json.dumps(body))
        return sc.resolve_progress_stream(repo_root=Path(_cd))

    check("resolve_progress_stream True", _resolve_with(True) is True)
    check("resolve_progress_stream False", _resolve_with(False) is False)
    check("resolve_progress_stream unset → None", _resolve_with(None) is None)

    # --- ONLY a real JSON boolean counts (#2308 review, qingyun + john) ---
    # The old `bool(val)` inverted intent on exactly the shapes people type
    # when DISABLING something: `"false"` and `{"enabled": false}` both became
    # True and switched owner-channel progress messages ON. The loader is
    # deliberately schema-lenient at runtime, so the JSON schema does not
    # protect a hand-edited sutando.config.local.json — the guard lives in the
    # resolver and this is its regression.
    #
    # Deliberately the whole TYPE AXIS, not just the three values the reviewers
    # named: the property is "non-bool ⇒ unset", so a fixture that only pins
    # str/dict/int would stay green if a future branch special-cased lists or
    # floats. Truthy AND falsy of each type, because the bug was a truthiness
    # bug and a falsy-only fixture would have passed against the broken code.
    def _resolve_capturing_stderr(val):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = _resolve_with(val)
        return got, ("must be a JSON boolean" in err.getvalue())

    for _label, _val in [
        ('string "false"', "false"),          # the headline case
        ('string "true"', "true"),
        ('string "no"', "no"),
        ("empty string", ""),
        ('object {"enabled": false}', {"enabled": False}),
        ("empty object", {}),
        ("list [1]", [1]),
        ("empty list", []),
        ("int 1", 1),
        ("int 0", 0),
        ("float 1.0", 1.0),
    ]:
        _got, _warned = _resolve_capturing_stderr(_val)
        check(f"non-bool {_label} → None (unset, streamer stays OFF)", _got is None)
        check(f"non-bool {_label} emits a diagnostic", _warned)

    # A real boolean must NOT draw the diagnostic — otherwise the warning is
    # noise on every valid config and gets tuned out.
    for _label, _val in (("True", True), ("False", False)):
        _got, _warned = _resolve_capturing_stderr(_val)
        check(f"bool {_label} still honored", _got is _val)
        check(f"bool {_label} emits NO diagnostic", not _warned)

    # The diagnostic is ONE-TIME, matching _warn_unknown_top_level_keys. The
    # resolver is called per bridge message, so a per-call warning would spam
    # the log for as long as the bad value sits in the config. Every case above
    # resets the flag via _reset_cache_for_tests(), so without this the
    # already-warned early-return never executes.
    sc._reset_cache_for_tests()
    _cfgp.write_text(json.dumps({"bridges": {"progress_stream": "false"}}))
    _errs = []
    for _ in range(3):
        _e = io.StringIO()
        with contextlib.redirect_stderr(_e):
            sc.resolve_progress_stream(repo_root=Path(_cd))   # no reset between calls
        _errs.append("must be a JSON boolean" in _e.getvalue())
    check("diagnostic fires on the FIRST bad read", _errs[0])
    check("diagnostic is one-time, not once per call", _errs[1:] == [False, False])
sc._reset_cache_for_tests()

# --- should_stream_task (owner-only) ---
check("owner streams", ps.should_stream_task("owner") is True)
check("collaborator streams despite team tier",
      ps.should_stream_task("team", True) is True)
check("team WITHOUT collaborator does not stream",
      ps.should_stream_task("team", False) is False)
check("collaborator flag defaults off (unchanged callers keep owner-only)",
      ps.should_stream_task("team") is False)
check("other tier never streams even as collaborator=False",
      ps.should_stream_task("other") is False)
check("owner streams (caps/space)", ps.should_stream_task("  Owner ") is True)
check("None tier streams (legacy owner)", ps.should_stream_task(None) is True)
check("team does NOT stream", ps.should_stream_task("team") is False)
check("other does NOT stream", ps.should_stream_task("other") is False)

# --- read_core_status (never raises) ---
with tempfile.TemporaryDirectory() as d:
    sd = Path(d)
    check("missing file -> None", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text("")
    check("empty file -> None", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text("{not json")
    check("malformed -> None (no raise)", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text("[1,2,3]")
    check("non-dict json -> None", ps.read_core_status(sd) is None)
    (sd / "core-status.json").write_text(json.dumps({"status": "running", "step": "scanning"}))
    got = ps.read_core_status(sd)
    check("valid -> dict", isinstance(got, dict) and got.get("step") == "scanning")

# read_core_status legacy fallback: state_dir/../core-status.json (un-migrated)
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    state = root / "state"
    state.mkdir()
    # only the legacy workspace-root file exists, not state/core-status.json
    (root / "core-status.json").write_text(json.dumps({"status": "running", "step": "legacy"}))
    got = ps.read_core_status(state)
    check("legacy root fallback read", isinstance(got, dict) and got.get("step") == "legacy")
    # primary takes precedence over legacy when both exist
    (state / "core-status.json").write_text(json.dumps({"status": "running", "step": "primary"}))
    got2 = ps.read_core_status(state)
    check("primary wins over legacy", got2.get("step") == "primary")

# --- current_step ---
check("idle -> None (no narration)", ps.current_step({"status": "idle", "step": "x"}) is None)
check("running+step -> step", ps.current_step({"status": "running", "step": "scanning"}) == "scanning")
check("running+blank step -> None", ps.current_step({"status": "running", "step": "   "}) is None)
check("running+non-str step -> None", ps.current_step({"status": "running", "step": 42}) is None)
check("None status dict -> None", ps.current_step(None) is None)
check("missing step -> None", ps.current_step({"status": "running"}) is None)

# --- thresholds / rate-limit / expiry ---
check("no placeholder before threshold", ps.should_post_placeholder(3, 8) is False)
check("placeholder at threshold", ps.should_post_placeholder(8, 8) is True)
check("placeholder past threshold", ps.should_post_placeholder(20, 8) is True)
check("no edit within interval", ps.should_edit(10.0, 8.0, 4) is False)
check("edit after interval", ps.should_edit(12.5, 8.0, 4) is True)
check("not expired before max age", ps.placeholder_expired(100, 1800) is False)
check("expired at max age", ps.placeholder_expired(1800, 1800) is True)

# --- format_progress ---
check("format includes step + secs", ps.format_progress("scanning Gmail", 12) == "⏳ scanning Gmail (12s)")
check("format None step -> working", ps.format_progress(None, 9) == "⏳ working… (9s)")
check("format blank step -> working", ps.format_progress("   ", 9) == "⏳ working… (9s)")
check("format negative elapsed clamps to 0", ps.format_progress("x", -5) == "⏳ x (0s)")
long_step = "z" * 500
out = ps.format_progress(long_step, 3, max_len=180)
check("format truncates long step", len(out) < 220 and out.endswith("(3s)") and "…" in out)

# --- is_progress_placeholder (ingestion-gate flood guard) ---
check("placeholder: working…", ps.is_progress_placeholder("⏳ working… (9s)") is True)
check("placeholder: with step", ps.is_progress_placeholder("⏳ Checked 42 open PRs (137s)") is True)
check("placeholder: leading/trailing ws", ps.is_progress_placeholder("  ⏳ working… (0s)  ") is True)
check("placeholder: round-trips format_progress", ps.is_progress_placeholder(ps.format_progress("reviewing", 45)) is True)
check("placeholder: round-trips None step", ps.is_progress_placeholder(ps.format_progress(None, 3)) is True)
# false positives — real owner tasks must NOT be dropped
check("not placeholder: emoji mid-sentence", ps.is_progress_placeholder("check the ⏳ status? (see #1)") is False)
check("not placeholder: no (Ns) tail", ps.is_progress_placeholder("⏳ waiting on review") is False)
check("not placeholder: no leading marker", ps.is_progress_placeholder("Please review PR (3s)") is False)
check("not placeholder: multi-line", ps.is_progress_placeholder("⏳ a\nreal task (5s)") is False)
check("not placeholder: empty", ps.is_progress_placeholder("") is False)
check("not placeholder: None", ps.is_progress_placeholder(None) is False)
check("not placeholder: non-str", ps.is_progress_placeholder(42) is False)

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed")


# --- outage-aware placeholder (sonichi#2398) ---
NOW = 1_000_000.0
_run = {"status": "running", "step": "SESSION RESTART in flight", "ts": NOW - 300}
_fresh_run = {"status": "running", "step": "building", "ts": NOW - 30}
_idle_old = {"status": "idle", "ts": NOW - 9999}

check("status_age_s computes age", ps.status_age_s(_run, NOW) == 300)
check("status_age_s None on missing ts", ps.status_age_s({"status": "running", "step": "x"}, NOW) is None)
check("status_age_s None on bool ts", ps.status_age_s({"status": "running", "step": "x", "ts": True}, NOW) is None)
check("status_age_s None on non-dict", ps.status_age_s(None, NOW) is None)

check("frozen non-idle status is stale", ps.status_is_stale(_run, NOW))
check("fresh non-idle status not stale", not ps.status_is_stale(_fresh_run, NOW))
check("old IDLE status never stale", not ps.status_is_stale(_idle_old, NOW))
check("unknowable age never stale", not ps.status_is_stale({"status": "running", "step": "x"}, NOW))

check("absent heartbeat is stale", ps.heartbeat_is_stale(None, NOW))
check("old heartbeat is stale", ps.heartbeat_is_stale(NOW - 120, NOW))
check("fresh heartbeat not stale", not ps.heartbeat_is_stale(NOW - 30, NOW))

check("down = frozen status AND stale heartbeat", ps.core_looks_down(_run, None, NOW))
check("long step + fresh heartbeat NOT down", not ps.core_looks_down(_run, NOW - 10, NOW))
check("fresh status + dead heartbeat NOT down", not ps.core_looks_down(_fresh_run, None, NOW))
check("idle + dead heartbeat NOT down (nothing to misreport)", not ps.core_looks_down(_idle_old, None, NOW))

_out = ps.format_outage(300, 35)
check("outage copy names frozen minutes", "5m" in _out)
check("outage copy names queue depth", "35 task(s) queued" in _out)
check("outage copy names the restart remedy", "restart.sh" in _out and "Restart Core" in _out)
check("outage copy warns, not progress", _out.startswith("⚠️") and "in flight" not in _out)
check("outage copy unknown age renders ?", "frozen for ?m" in ps.format_outage(None, 0))
check("outage copy sub-minute clamps to 1m", "frozen for 1m" in ps.format_outage(45, 1))
check("outage copy caps length", len(ps.format_outage(300, 10**9, max_len=120)) <= 120)

# Re-gate: the block above runs after the original summary, so it needs its
# own exit check — otherwise a failing outage-test could still exit 0.
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("outage-block tests passed")


# --- bridge-side wiring (in-process, covers the discord-bridge helpers) ---
# Same load pattern as tests/bridge-audit-wiring.test.py: stub `discord`,
# hermetic token via env, spec_from_file_location for the hyphenated module.
# The three helpers under test are pure over module-level dirs, which we
# repoint at a temp workspace after load.
import importlib.util  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("SUTANDO_TEST_MODE", "1")

# Hermetic config isolation (qingyun P1 on #2426): the bridge's import-time
# token hardening resolves claude_home_path("channels", "discord", ".env") and
# chmods it if it exists. Without isolation that touches the OPERATOR'S REAL
# token file. Point both resolution env vars at a temp root BEFORE exec_module
# so every config path the module computes stays inside it.
_cfg_root = tempfile.mkdtemp(prefix="progress-stream-test-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _cfg_root
os.environ["CLAUDE_HOME"] = _cfg_root

# Seed a canonical access.json under the temp root BEFORE import (qingyun P1
# round 2 on #2426): channel_access_path falls back to the operator's real
# ~/.claude/channels/discord/access.json (30-day legacy reader-fallback, with
# a deprecation warning) when the temp root lacks the file — which imports the
# operator's live allowlist and makes the test machine-dependent. A present
# file pins resolution inside the temp root.
_chan_dir = Path(_cfg_root) / "channels" / "discord"
_chan_dir.mkdir(parents=True)
(_chan_dir / "access.json").write_text(json.dumps(
    {"dmPolicy": "pairing", "allowFrom": ["0"], "groups": {}}))

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dbridge_outage", _REPO / "src" / "discord-bridge.py")
_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_db)

# Assert the isolation actually took — every config path the module resolved
# at import time must live under the temp root, never the operator's real
# config dir (token file AND access file).
check("bridge: channels_env confined to temp config root",
      str(_db.channels_env).startswith(_cfg_root))
check("bridge: ACCESS_FILE confined to temp config root",
      str(_db.ACCESS_FILE).startswith(_cfg_root))

with tempfile.TemporaryDirectory() as d:
    ws = Path(d)
    (ws / "state" / "cores").mkdir(parents=True)
    (ws / "tasks").mkdir()
    _db.STATE_DIR = ws / "state"
    _db.TASKS_DIR = ws / "tasks"
    now = time.time()

    # _newest_alive_mtime
    check("bridge: no .alive files -> None", _db._newest_alive_mtime() is None)
    a = ws / "state" / "cores" / "hostA.alive"
    a.write_text("{}")
    os.utime(a, (now - 200, now - 200))
    b = ws / "state" / "cores" / "hostB.alive"
    b.write_text("{}")
    os.utime(b, (now - 20, now - 20))
    got = _db._newest_alive_mtime()
    check("bridge: newest .alive mtime wins", got is not None and abs(got - (now - 20)) < 2)

    # _queued_task_count
    check("bridge: empty tasks dir -> 0", _db._queued_task_count() == 0)
    (ws / "tasks" / "task-1.txt").write_text("x")
    (ws / "tasks" / "task-2.txt").write_text("x")
    (ws / "tasks" / "not-a-task.log").write_text("x")
    check("bridge: counts only task-*.txt", _db._queued_task_count() == 2)

    # _render_progress_content — live core → normal progress copy
    (ws / "state" / "core-status.json").write_text(
        json.dumps({"status": "running", "step": "building", "ts": now - 10}))
    out_live = _db._render_progress_content(now, 42)
    check("bridge: live core renders progress copy", out_live.startswith("⏳") and "building" in out_live and "(42s)" in out_live)

    # _render_progress_content — frozen status + only-stale heartbeats → outage copy
    (ws / "state" / "core-status.json").write_text(
        json.dumps({"status": "running", "step": "processing task", "ts": now - 300}))
    b.unlink()  # newest remaining .alive is 200s old -> stale
    out_down = _db._render_progress_content(now, 42)
    check("bridge: down core renders outage copy", out_down.startswith("⚠️") and "unresponsive" in out_down)
    check("bridge: outage copy carries live queue depth", "2 task(s) queued" in out_down)
    check("bridge: outage copy never claims progress", "in flight" not in out_down and "(42s)" not in out_down)

    # Fail-soft branches: helper errors must degrade, never raise into the
    # gateway loop (a broken dirs object stands in for any resolution failure).
    _db.STATE_DIR = None
    _db.TASKS_DIR = None
    check("bridge: alive-mtime fail-soft -> None", _db._newest_alive_mtime() is None)
    check("bridge: queue-count fail-soft -> 0", _db._queued_task_count() == 0)
    _db.STATE_DIR = ws / "state"
    _db.TASKS_DIR = ws / "tasks"

# --- resolve_team_collaborator: the hoist ---

# Collaborator status must not depend on WHICH arm produced team tier: the
# globally-allowlisted arm never ran the old arm-local check.
_ACC = {"groups": {"123": {"allowFrom": ["u1"], "collaborators": ["u1"]}}}
check("hoist: globally-allowlisted team sender IS collaborator",
      _db.resolve_team_collaborator(_ACC, "team", "u1", 123) is True)
check("hoist: owner/other/guest never collaborate",
      not any(_db.resolve_team_collaborator(_ACC, t_, "u1", 123)
              for t_ in ("owner", "other", "guest", "ambient")))
check("hoist: unlisted team sender is NOT collaborator",
      _db.resolve_team_collaborator(_ACC, "team", "u2", 123) is False)

# --- should_stream_task: the collaborator flag must not bypass the tier ---

# The reviewer's exact table: with is_collaborator=True, only team streams
# (owner streams via its own arm; None is the platform's missing-field=owner).
for _tier, _want in [("owner", True), ("team", True), ("other", False),
                     ("guest", False), ("ambient", False), (None, True)]:
    check(f"tier-gate: collab flag with tier={_tier} -> {_want}",
          ps.should_stream_task(_tier, is_collaborator=True) is _want)
check("tier-gate: plain team (no flag) does NOT stream",
      ps.should_stream_task("team") is False)

if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("bridge-wiring tests passed")
