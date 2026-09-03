#!/usr/bin/env python3
"""OS-supervised cron runner — emits task files for due crons.json entries.

Why this exists
---------------
Sutando's recurring prompts (morning briefing, the loop-eng
digest, etc.) were scheduled purely as in-session ``CronCreate`` jobs. Those
are best-effort: they only fire while the Claude REPL is idle at the fire
minute, they carry scheduler jitter, and they die with the session. On
2026-07-02 the 6:02 loop-engineering digest never delivered and the miss was
silent — the owner asked to "make the schedule reliably run".

This runner is the reliable path. It is invoked by launchd
(``com.sutando.cron-runner``) every 60s, independent of any Claude session.
Each tick it reads the per-host ``crons.json``, decides which entries are DUE
since their last recorded fire, and emits a task file into ``tasks/`` for each.
A ``shell_command`` entry runs directly from the repository root with output
logged; a prompt-backed entry goes through the watcher pipeline. Same OS-level → emit-task → process
pipeline the launchd health-check fallback already uses.

Ownership / no double-fire
--------------------------
Only entries explicitly flagged ``"launchd": true`` in crons.json are handled
here. The session ``/schedule-crons`` path skips those same entries, so exactly
one scheduler owns each cron — no duplicate deliveries. The session driver
(``main-loop`` / ``/proactive-loop``) is never launchd-owned; it drives the
session itself and is not a task.

Missed-fire recovery is bounded: if the machine was asleep/off across one or
more scheduled times, the entry fires exactly once on the next tick (catch-up),
never a backlog storm.
"""
from __future__ import annotations

import calendar
import fcntl
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# --- workspace + host resolution (mirror the rest of the codebase) ----------
# This file lives in src/, so its own directory IS the src/ dir — reach the
# sanctioned loader without walking up from __file__ (that anti-pattern is
# refused by scripts/lint-workspace-resolution.sh).
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

# Defang any header-field-/fence-like line a crons.json `prompt` body might
# contain before it is interpolated into the emitted task file's `task:` field
# (same guard the channel bridges apply). Belt-and-suspenders with `task:` being
# last: confine_user_content() neutralizes forged fields even in a multi-line body.
from task_body_guard import confine_user_content  # noqa: E402
import cron_task_id  # noqa: E402
import cron_eval  # noqa: E402

try:
    from workspace_default import resolve_workspace  # type: ignore
    WORKSPACE = Path(resolve_workspace())
except Exception:  # pragma: no cover — only fires outside a checkout (can't import workspace_default)
    # Defensive fallback for non-checkout installs — matches CLAUDE.md default.
    WORKSPACE = SRC_DIR.parent / "workspace"  # pragma: no cover


def host_slug() -> str:
    """Per-host label for the ``hosts/<host>/crons.json`` path. Delegates to
    ``util_paths._host_label()`` — the single source of truth (honors
    ``$SUTANDO_HOST_LABEL``/``$SUTANDO_HOST_OVERRIDE``, then scutil
    LocalHostName, then short hostname) — so the launchd runner resolves the
    SAME per-host dir as the rest of the stack. A raw
    ``gethostname().split(".")[0]`` here would misresolve on hosts with a label
    override or a drifting DHCP hostname, reading a phantom
    ``hosts/<wrong-label>/crons.json``. ``util_paths`` is importable because
    ``SRC_DIR`` is already on ``sys.path`` above."""
    try:
        from util_paths import _host_label  # type: ignore
        return _host_label()
    except Exception:  # pragma: no cover — only outside a checkout
        import socket
        return socket.gethostname().split(".")[0]


CRONS_FILE = WORKSPACE / "hosts" / host_slug() / "crons.json"
TASKS_DIR = WORKSPACE / "tasks"
STATE_FILE = WORKSPACE / "state" / "cron-runner-state.json"
CORE_ALIVE_FILE = WORKSPACE / "state" / "cores" / f"{host_slug()}.alive"
REPO_ROOT = SRC_DIR.parent

# Look back at most this far when catching up a missed fire. Bounds work after
# long downtime and guarantees at most one catch-up emission per entry.
MAX_CATCHUP_SECONDS = 24 * 3600
# A short core restart may recover a recent slot, but a morning briefing or
# other time-sensitive task must not execute hours after its intended time.
# FLOOR: sub-hourly jobs keep exactly the old budget, so none gets stricter.
MAX_EMIT_LATENESS_SECONDS = 15 * 60
# CAP: honours the "never hours late" intent above, whatever the period is.
MAX_EMIT_LATENESS_CAP_SECONDS = 60 * 60
# Two fires of a weekly job can be ~13 days apart when today is midweek.
PERIOD_SCAN_MAX_SECONDS = 15 * 24 * 3600
# Widest UTC offset in the tz database, historical LMT included. Bounds where a
# wall time's real epoch can land relative to its offset-free encoding.
MAX_UTC_OFFSET_SECONDS = 16 * 3600
# A rollback across midnight leaves ELAPSED epochs on a lexically LATER local
# date, which a backward-only day walk would never visit.
DATE_LOOKAHEAD_DAYS = 2
CORE_ALIVE_MAX_AGE_SECONDS = 90


# --- 5-field cron matcher: thin bindings over the shared contract -----------
@functools.lru_cache(maxsize=512)
def _parse_field(field: str, lo: int, hi: int) -> frozenset[int]:
    """Expand one cron field via cron_eval; out-of-range values are dropped
    (this runner's historical contract), syntax errors raise ValueError."""
    # Frozen: the lru_cache hands every caller the SAME object.
    return cron_eval.field_values(field, lo, hi, clamp=True)


def cron_matches(expr: str, t: time.struct_time) -> bool:
    """True if the 5-field cron expression matches the given local time."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute, hour, dom, month, dow = fields
    if t.tm_min not in _parse_field(minute, 0, 59):
        return False
    if t.tm_hour not in _parse_field(hour, 0, 23):
        return False
    return _day_matches(dom, month, dow, t)


def _day_matches(dom: str, month: str, dow: str, t: time.struct_time) -> bool:
    """Date part of ``t`` against dom/month/dow — Vixie OR/AND and the Sunday=7
    fold both live in cron_eval so no second copy can drift."""
    spec = cron_eval.CronSpec(
        minutes=frozenset(), hours=frozenset(),
        doms=_parse_field(dom, 1, 31), months=_parse_field(month, 1, 12),
        dows=cron_eval.fold_sunday(_parse_field(dow, 0, 7)),
        dom_restricted=dom != "*", dow_restricted=dow != "*")
    return spec.date_matches(t.tm_year, t.tm_mon, t.tm_mday)


def _day_offsets(y: int, mo: int, d: int) -> "tuple[int, ...]":
    """Every UTC offset the zone can be at during this local date, with margin.

    Enumerated from real offsets, never from DST polarity: Antarctica/Casey
    rolls back three hours with tm_isdst=0 reported on BOTH sides.
    """
    naive = calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0))
    lo = naive - MAX_UTC_OFFSET_SECONDS
    hi = naive + 86400 + MAX_UTC_OFFSET_SECONDS

    def _off(e: int) -> "Optional[int]":
        # A zone can make an instant unrepresentable on some libc; skip that
        # probe rather than fail the tick.
        try:
            return time.localtime(e).tm_gmtoff
        except (OverflowError, ValueError, OSError):
            return None

    # Seed coarsely, then bisect only between probes that disagree: an ordinary
    # date costs 5 calls and only a transition date pays for the search.
    seeds = [lo + (hi - lo) * i // 4 for i in range(5)]
    probed = [(e, _off(e)) for e in seeds]
    offsets = {o for _, o in probed if o is not None}
    pending = [(probed[i], probed[i + 1]) for i in range(len(probed) - 1)
               if probed[i][1] != probed[i + 1][1]]
    while pending:
        (a_e, a_v), (b_e, b_v) = pending.pop()
        if b_e - a_e <= 60:
            continue
        m_e = (a_e + b_e) // 2
        m_v = _off(m_e)
        if m_v is not None:
            offsets.add(m_v)
        if m_v != a_v:
            pending.append(((a_e, a_v), (m_e, m_v)))
        if m_v != b_v:
            pending.append(((m_e, m_v), (b_e, b_v)))
    return tuple(sorted(offsets))


def _local_epochs(y: int, mo: int, d: int, h: int, mi: int,
                  offsets: "Optional[tuple[int, ...]]" = None) -> "list[int]":
    """Every real epoch whose local wall-clock is this minute, newest first.

    A rollback repeats a wall minute so an ambiguous one has TWO epochs and a
    skipped one has none. ``offsets`` is hoisted per day by the caller.
    """
    if offsets is None:
        offsets = _day_offsets(y, mo, d)
    naive = calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))
    out = []
    for off in offsets:
        e = naive - off
        try:
            lt = time.localtime(e)
        except (OverflowError, ValueError, OSError):
            continue
        if (lt.tm_year, lt.tm_mon, lt.tm_mday,
                lt.tm_hour, lt.tm_min) == (y, mo, d, h, mi) and e not in out:
            out.append(e)
    return sorted(out, reverse=True)


def _offsets_on(day_utc: int) -> "tuple[int, ...]":
    """``_day_offsets`` for the local date whose offset-free midnight is given."""
    g = time.gmtime(day_utc)
    return _day_offsets(g.tm_year, g.tm_mon, g.tm_mday)


def cron_period_seconds(expr: str, now_epoch: int) -> "Optional[int]":
    """Seconds between the two most recent fire-minutes of ``expr``, or None.

    Derived from the expression itself rather than declared, so it stays right
    when a schedule is edited. Bounded by MAX_CATCHUP_SECONDS like the scan below.
    """
    # Walk by DAY, not by minute: a whole day is rejected with one date test,
    # and only a matching day expands its hour x minute candidates.
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    hours = sorted(_parse_field(hour_f, 0, 23), reverse=True)
    minutes = sorted(_parse_field(minute_f, 0, 59), reverse=True)
    if not hours or not minutes:
        return None

    floor = now_epoch - PERIOD_SCAN_MAX_SECONDS
    fires = []
    window = []
    cur = time.localtime(now_epoch)
    # Start AHEAD of today's local date, then walk backward: a rollback across
    # midnight puts already-elapsed epochs on a lexically later date.
    day_utc = calendar.timegm((cur.tm_year, cur.tm_mon, cur.tm_mday,
                               0, 0, 0, 0, 0, 0)) + DATE_LOOKAHEAD_DAYS * 86400
    # Three dates bound the westmost offset any UNVISITED date can be at; two
    # dates further back are already more than one offset-span in the past.
    ring = [_offsets_on(day_utc - i * 86400) for i in range(3)]

    def _drain(barrier: int) -> "Optional[int]":
        """Emit buffered epochs above ``barrier``; period once two are found."""
        while window and window[0] > barrier:
            epoch = window.pop(0)
            if epoch < floor:
                return -1
            fires.append(epoch)
            if len(fires) == 2:
                return fires[0] - fires[1]
        return None

    days = PERIOD_SCAN_MAX_SECONDS // 86400 + 4 + DATE_LOOKAHEAD_DAYS
    for _ in range(days):
        g = time.gmtime(day_utc)
        y, mo, d = g.tm_year, g.tm_mon, g.tm_mday
        offsets = ring[0]
        seen = [o for t in ring for o in t]
        west = min(seen) if seen else -MAX_UTC_OFFSET_SECONDS
        if _day_matches(dom_f, month_f, dow_f, g):
            east = max(offsets) if offsets else MAX_UTC_OFFSET_SECONDS
            for h in hours:
                base = calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0))
                # Every real epoch of this wall hour is at least base - east,
                # so an hour still wholly ahead cannot contribute.
                if base - east > now_epoch:
                    continue
                for mi in minutes:
                    window.extend(e for e in _local_epochs(y, mo, d, h, mi, offsets)
                                  if e <= now_epoch)
                window.sort(reverse=True)
                # Nothing unexpanded (lower hours, older dates) can exceed this,
                # so anything above it is final and safe to consume in order.
                got = _drain(base - west)
                if got is not None:
                    return None if got == -1 else got
        window.sort(reverse=True)
        got = _drain(day_utc - west)
        if got is not None:
            return None if got == -1 else got
        day_utc -= 86400
        ring = [ring[1], ring[2], _offsets_on(day_utc - 2 * 86400)]
    window.sort(reverse=True)
    got = _drain(floor - 1)
    return None if got in (None, -1) else got


def emit_lateness_budget(expr: str, now_epoch: int) -> int:
    """How late a slot of ``expr`` may be and still run.

    A FLAT budget spends a half-period on a 30-minute job and 1/96th of one on a
    daily job, so an equal delay costs the daily job an entire day. Scale with the
    period, never below the old constant, never above the cap.
    """
    period = cron_period_seconds(expr, now_epoch)
    if period is None:
        return MAX_EMIT_LATENESS_SECONDS
    return min(max(MAX_EMIT_LATENESS_SECONDS, period // 8),
               MAX_EMIT_LATENESS_CAP_SECONDS)


def latest_due_since(expr: str, last_epoch: int, now_epoch: int) -> Optional[int]:
    """Latest fire-minute of ``expr`` in (last_epoch, now_epoch], if any.

    Iterates whole minutes across the window (bounded by MAX_CATCHUP_SECONDS)
    so a recent fire that landed during a short restart can still be recovered.
    Returning the exact slot lets :func:`run` reject stale catch-up work rather
    than executing a day-old briefing.
    """
    window_start = max(last_epoch, now_epoch - MAX_CATCHUP_SECONDS)
    # Align to the next whole minute after window_start.
    m = (window_start // 60 + 1) * 60
    latest = None
    while m <= now_epoch:
        if cron_matches(expr, time.localtime(m)):
            latest = m
        m += 60
    return latest


def due_since(expr: str, last_epoch: int, now_epoch: int) -> bool:
    """Compatibility predicate for callers/tests that only need due/not-due."""
    return latest_due_since(expr, last_epoch, now_epoch) is not None


def local_core_alive(now_epoch: Optional[int] = None) -> bool:
    """Whether this host's core heartbeat is fresh enough to accept work."""
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    try:
        age = now_epoch - CORE_ALIVE_FILE.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < CORE_ALIVE_MAX_AGE_SECONDS


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


@contextmanager
def _state_lock(state_file: Path) -> Iterator[None]:
    """Exclusive lock serializing the ``cron-runner-state.json`` read-modify-write
    against the Codex reconciler.

    Both this runner's :func:`run` and
    ``skills/schedule-crons/scripts/reconcile_launchd.py`` lock the SAME path
    (``<state_file>.lock``). Without it, a launchd tick that read state *before*
    the reconciler seeded a migration boundary would write its stale full-dict
    snapshot back *after*, silently dropping the boundary — the next tick then
    sees ``launchd: true`` with no recorded fire and replays a whole
    ``MAX_CATCHUP_SECONDS`` window of daily crons (the backlog storm the
    reconciler's docstring promises to prevent). The lock makes the two
    read-modify-write sections mutually exclusive, so the boundary always
    survives regardless of ordering. Peer: ``reconcile_launchd._state_lock``."""
    lock_path = state_file.parent / (state_file.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write_text(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` atomically (temp file + ``os.replace``) so a
    crash mid-write can never leave a torn/empty state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _sanitize_name(name: str) -> str:
    """Slugify a cron name for use in a task ID and filename.

    Delegates so readers can bind the same contract instead of re-deriving it.
    """
    return cron_task_id.sanitize_name(name)


def _shell_log_path() -> Path:
    """Return the durable log path for direct shell-command jobs."""
    # Derive this from the state path so tests and callers that inject a
    # workspace by replacing STATE_FILE keep all runner state together.
    return STATE_FILE.parent.parent / "logs" / "cron-runner.log"


# A hung or chatty job must not stall the tick that holds the state lock, nor
# grow the log unboundedly. Per-entry override: `shell_timeout_s`.
SHELL_COMMAND_TIMEOUT_S = 300
SHELL_OUTPUT_LIMIT = 64 * 1024


def _bounded_output(text: str) -> str:
    """Cap one stream so a chatty command cannot grow the log without limit."""
    if len(text) <= SHELL_OUTPUT_LIMIT:
        return text
    dropped = len(text) - SHELL_OUTPUT_LIMIT
    return f"{text[:SHELL_OUTPUT_LIMIT]}\n[truncated {dropped} more characters]\n"


def _shell_timeout_for(entry: dict) -> int:
    """Per-entry `shell_timeout_s`; a non-positive or non-integer value falls back
    to the default rather than disabling the bound."""
    raw = entry.get("shell_timeout_s")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return SHELL_COMMAND_TIMEOUT_S
    return raw if raw > 0 else SHELL_COMMAND_TIMEOUT_S


def _kill_process_tree(process: "subprocess.Popen[str]") -> None:
    """Signal the whole group: with shell=True the child is a shell, so killing
    only its pid leaves the grandchildren that hold the pipes running."""
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_shell_command(name: str, command: str, timeout_s: int = SHELL_COMMAND_TIMEOUT_S) -> int:
    """Run one mechanical cron command and persist all output.

    Shell jobs deliberately bypass the core heartbeat: their purpose is to
    perform work without waking a model session. The command is configuration
    owned by the user and runs from the repository root, matching the cwd a
    task-backed cron receives when the core executes it.

    Bounded in time and output: the caller holds the shared state lock for the
    whole tick, so an unbounded command would suppress every later job.
    """
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        # start_new_session gives the shell its own process group, which is what
        # makes killing the whole tree possible on timeout.
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            returncode = 124
            stderr = (stderr or "") + (
                f"cron-runner: {name} exceeded {timeout_s}s; process tree killed\n"
            )
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = f"{exc.__class__.__name__}: {exc}"
    stdout = _bounded_output(stdout or "")
    stderr = _bounded_output(stderr or "")

    log = (
        f"[{started}] shell_command job={name!r} exit_code={returncode}\n"
        f"command: {command}\n"
        f"stdout:\n{stdout}"
        f"stderr:\n{stderr}"
        "\n"
    )
    log_path = _shell_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(log)

    if stdout:
        print(f"cron-runner: shell_command {name} stdout:\n{stdout}", end="")
    if stderr:
        print(f"cron-runner: shell_command {name} stderr:\n{stderr}", end="", file=sys.stderr)
    if returncode:
        print(
            f"cron-runner: shell_command {name} failed with exit code {returncode}",
            file=sys.stderr,
        )
    return returncode


def emit_task(name: str, entry: dict) -> Path:
    now_ms = int(time.time() * 1000)
    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # A launchd cron either carries a direct `prompt` or a `prompt_skill`
    # (invoked as a slash command), matching the schedule-crons contract.
    if entry.get("prompt_skill"):
        body_task = f"/{entry['prompt_skill']}"
    else:
        body_task = entry.get("prompt", "")
    safe_name = _sanitize_name(name)
    task_id = cron_task_id.task_id(name, now_ms)
    # Defang forged header/fence lines in the (config-supplied) body, then place
    # `task:` last so a multi-line prompt body cannot forge the structured
    # header fields above it (source, user_id, access_tier, priority).
    body_task = confine_user_content(body_task)
    body = (
        f"id: {task_id}\n"
        f"timestamp: {ts_iso}\n"
        f"source: cron\n"
        f"user_id: cron-runner\n"
        f"access_tier: owner\n"
        f"priority: low\n"
        f"task: {body_task}\n"
    )
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    # Coalesce pending fires: if a prior emission for this same entry is still
    # unconsumed (the core was down or busy), remove it before writing the new
    # one, so a long outage leaves exactly ONE task per entry — the newest —
    # instead of one file per missed slot (a */30 entry over a 6h outage would
    # otherwise queue 12). Design converged with Chi + Sutando-Pro in #dev
    # 2026-07-18. We keep the per-fire timestamped id (rather than a literally
    # stable filename) so the orphan-check completion-marker contract
    # (results/<id>.txt) is untouched — a consumed+archived result can never
    # collide with a future fire's unique id. The suffix.isdigit() guard keeps
    # the sweep from matching a different entry whose slug shares this prefix
    # (e.g. cleaning "sync" must not delete "sync-workspace"'s pending task).
    prefix = f"{cron_task_id.TASK_PREFIX}{safe_name}-"
    for stale in TASKS_DIR.glob(f"{prefix}*.txt"):
        if stale.name[len(prefix):-4].isdigit():
            try:
                stale.unlink()
            except OSError:
                pass
    path = TASKS_DIR / f"{task_id}.txt"
    # HMAC envelope (#3014 writer census): stamp at this writer's edge, fail-open
    # so a stamping error costs the stamp and never the fire.
    try:
        from task_envelope import stamp_text  # sibling module (src/ on sys.path)
        body = stamp_text(body, WORKSPACE)
    except Exception:
        pass
    path.write_text(body)
    _emit_cron_telemetry()
    return path


def _emit_cron_telemetry() -> None:
    """Fire-and-forget product telemetry: count `cron` as a task source so
    DAU/WAU includes cron-driven activity. PR #2274 added `cron` to the
    telemetry allowlist but this writer never emitted, so the bucket could
    never fire (CR by liususan091219). Mirrors the discord/slack/telegram
    bridges + agent-api, which emit at their own accept points. Never blocks or
    breaks task emission; no-op when telemetry is opted out. Never carries task
    content or ids.
    """
    try:  # pragma: no cover — fire-and-forget glue; logic in tests/telemetry.test.py
        from telemetry import task_processed  # sibling module (src/ on sys.path)

        # cron-runner is a one-shot launchd process, so a daemon-thread send
        # can be killed as soon as this process exits. Bound the synchronous
        # flush in telemetry.capture() so the event is handed off first.
        task_processed("cron", flush=True)
    except Exception:  # pragma: no cover — telemetry must never break cron emission
        pass


def run(now_epoch: Optional[int] = None) -> list:
    """One tick. Returns the list of cron names emitted this tick."""
    now_epoch = int(now_epoch if now_epoch is not None else time.time())
    crons = _load_json(CRONS_FILE, [])
    emitted = []
    core_alive = local_core_alive(now_epoch)

    # Hold the shared state lock across the whole read-modify-write so a
    # concurrent reconciler (Codex boot) can neither observe a half-written
    # state nor have its just-seeded migration boundary clobbered by our
    # write-back. See _state_lock for the race this closes.
    with _state_lock(STATE_FILE):
        state = _load_json(STATE_FILE, {})
        for entry in crons:
            if not entry.get("launchd"):
                continue  # session-owned or not reliability-critical — skip
            name = entry.get("name")
            expr = entry.get("cron")
            if not name or not expr:
                continue
            has_shell_command = "shell_command" in entry
            shell_command = entry.get("shell_command")
            if has_shell_command and (
                not isinstance(shell_command, str) or not shell_command.strip()
            ):
                print(
                    f"cron-runner: skipping {name}: shell_command must be a non-empty string",
                    file=sys.stderr,
                )
                state[name] = now_epoch
                continue
            # When state is absent (first run or after reinstall), look back the
            # full catch-up window so a daily cron missed during a restart or
            # sleep cycle is still emitted on the next tick.
            last = int(state.get(name, now_epoch - MAX_CATCHUP_SECONDS))
            try:
                due_epoch = latest_due_since(expr, last, now_epoch)
            except ValueError as e:
                print(f"cron-runner: skipping {name}: {e}", file=sys.stderr)
                continue
            if due_epoch is not None:
                # Direct shell jobs must stay claimable and idempotent like prompt jobs; only
                # the execution differs.
                if shell_command is not None:
                    _run_shell_command(
                        name, shell_command, _shell_timeout_for(entry))
                    emitted.append(name)
                elif not core_alive:
                    # Preserve the previous boundary so a short outage can
                    # recover this slot after the heartbeat returns.
                    continue
                else:
                    lateness = now_epoch - due_epoch
                    budget = emit_lateness_budget(expr, now_epoch)
                    if lateness <= budget:
                        emit_task(name, entry)
                        emitted.append(name)
                    else:
                        # The drop is the only record a slot was skipped; undated,
                        # it cannot be tied to a sleep window or counted per day.
                        _ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        print(
                            f"{_ts} cron-runner: dropping stale slot for {name} "
                            f"({lateness}s late, budget {budget}s)",
                            file=sys.stderr,
                        )
            state[name] = now_epoch

        if crons:  # only persist once we've actually read a config
            _atomic_write_text(STATE_FILE, json.dumps(state))
    return emitted


if __name__ == "__main__":
    names = run()
    if names:
        _ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"{_ts} cron-runner emitted: " + ", ".join(names))
