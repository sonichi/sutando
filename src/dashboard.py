#!/usr/bin/env python3
"""
Sutando dashboard — current system status for the local agent.

Combines: capability matrix, service health, activity feed, quick links, and system stats.

Usage:
  python3 src/dashboard.py              # serve on port 7844
  Open http://localhost:7844 in browser

Auto-refreshes every 15 seconds.
"""

from __future__ import annotations


import http.server
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
import urllib.parse
from urllib.parse import urlparse

# Two-variable split (see docs/workspace-contract.md):
#   - REPO_DIR      = source tree (this file's parent.parent) — for source paths
#   - WORKSPACE_DIR = runtime state (resolve_workspace()) — for build_log, etc.
# Matches PR #775's pattern for agent-api.py + github-webhook.py + task-bridge.ts.
REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from workspace_default import resolve_workspace, status_read_path  # noqa: E402
from sutando_config import config_get  # noqa: E402
from util_paths import personal_path, shared_personal_path, _host_label  # noqa: E402
from pending_questions_md import active_region  # noqa: E402
import dashboard_schedules  # noqa: E402
import quota_projection  # noqa: E402
WORKSPACE_DIR = resolve_workspace()
PORT = 7844


def _resolve_note_path(raw_slug: str):
    """Resolve `notes/{slug}.md` with path-injection sanitization.

    Returns the resolved Path, or None if the slug is invalid.

    Uses the CodeQL-recognized sanitizer pair:
    1. Whitelist the slug to `[\\w-]+` (reject if any char was stripped).
    2. `os.path.realpath` to normalize (Path::PathNormalization).
    3. `.startswith(base + sep)` prefix check (Path::SafeAccessCheck).
    `Path.resolve` and `Path.is_relative_to` are NOT modeled by CodeQL's
    path-injection query, so the previous refactor didn't close the alerts.
    """
    slug = re.sub(r"[^\w-]", "", raw_slug)
    if not slug or slug != raw_slug:
        return None
    notes_real = os.path.realpath(shared_personal_path("notes"))
    note_file_str = os.path.realpath(os.path.join(notes_real, f"{slug}.md"))
    if not note_file_str.startswith(notes_real + os.sep):
        return None
    return Path(note_file_str)


def get_outbox(limit: int = 10) -> list[dict]:
    """Return recent outbox entries for the dashboard card."""
    try:
        import outbox_log
        return outbox_log.read_recent(limit)
    except Exception:
        return []


def get_health() -> list[dict]:
    # Use sys.executable so the subprocess uses the same Python that's
    # running dashboard itself (typically homebrew 3.11). When launchd
    # spawns dashboard with a minimal PATH, bare `python3` resolves to
    # /usr/bin/python3 (3.9.6), which can't parse 3.10+ union syntax
    # (str | None) in health-check.py — causing a silent TypeError that
    # empties the services panel. Regression introduced by PR #263.
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_DIR / "src/health-check.py"), "--json"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout.strip())
        return data.get("checks", [])
    except Exception:
        return []


def get_activity(max_items: int = 10) -> list[dict]:
    """Get recent activity from git log — always fresh, no manual maintenance."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_items}", "--format=%h|%ci|%s"],
            capture_output=True, text=True, timeout=5, cwd=REPO_DIR,
        )
        entries = []
        for line in result.stdout.strip().split('\n'):
            if not line: continue
            parts = line.split('|', 2)
            if len(parts) < 3: continue
            sha, date_str, msg = parts
            # Format date: "2026-03-29 16:22:32 -0700" → "Mar 29 16:22"
            try:
                from datetime import datetime
                dt = datetime.strptime(date_str.strip()[:19], '%Y-%m-%d %H:%M:%S')
                time_str = dt.strftime('%b %d %H:%M')
            except Exception:
                time_str = date_str[:16]
            entries.append({'time': time_str, 'title': msg.strip(), 'body': sha})
        return entries
    except Exception:
        return []


def get_pending_count() -> dict:
    pending_file = Path(personal_path("pending-questions.md"))
    if not pending_file.exists():
        return {"open": 0, "done": 0}
    content = pending_file.read_text()
    # Questions are filed as free-form `## ` sections (no **Status:** field — see
    # #1265) and moved below a top-level `# Resolved` divider once answered. The
    # old `**Status:** Waiting/Answered` regex matched neither and always returned
    # 0/0 for the format actually in use — count `## ` sections per region instead.
    # Must use the shared locator, not a bare partition: a line-initial
    # `# Resolved` inside the file's own HTML banner (which documents the divider)
    # matches first, so the active region collapses and every open question is
    # counted as resolved. Measured on the decoy shape: partition gave open=0
    # done=3 where the truth is open=2 done=1 — and this surface is public via
    # /json, so it was reporting a confident zero.
    active = active_region(content)
    resolved = content[len(active):]
    open_count = len(re.findall(r'^## ', active, flags=re.MULTILINE))
    done_count = len(re.findall(r'^## ', resolved, flags=re.MULTILINE))
    return {"open": open_count, "done": done_count}


def get_score() -> str:
    build_log = WORKSPACE_DIR / "build_log.md"
    if not build_log.exists():
        return "?"
    content = build_log.read_text()
    m = re.search(r'\*\*Score: (.+?)\*\*', content)
    return m.group(1) if m else "?"


def get_quota_status() -> dict:
    """Read quota state from quota-state.json (written by credential proxy).

    Quota state IS runtime state; the canonical (and only) home is
    <workspace>/state/quota-state.json. The skill-dir fallback was removed:
    a stale leftover copy under skills/quota-tracker/ silently shadowed the
    fresh file and froze this dashboard's quota panel for ~12h (2026-05-21).
    One path, one source of truth.

    The file is only as fresh as its writer. When the credential proxy is not
    in the boot path (sonichi#2211) nothing rewrites it, and the panel keeps
    rendering the last snapshot as if it were current — Chi hit this with a
    file **332 hours** old still showing "4% used, resets 16:40 Jul 17".
    A MISSING file degrades honestly (`available: True`, no numbers); a STALE
    one is confidently wrong, which is the worse failure. So the age travels
    with the data: `age_h` always, `stale` past QUOTA_STALE_HOURS, and the
    caller renders it instead of implying freshness it cannot vouch for.
    """
    quota_file = status_read_path("quota-state.json", WORKSPACE_DIR)
    if not quota_file.exists():
        return {"available": True}
    try:
        data = json.loads(quota_file.read_text())
        headers = data.get("headers", {})
        # Parse reset timestamps PER WINDOW: one malformed value degrades
        # its own tile to unknown, never the sibling or the whole panel.
        for w in ("5h", "7d"):
            raw = headers.get(f"anthropic-ratelimit-unified-{w}-reset", "")
            try:
                data[f"reset_{w}"] = datetime.fromtimestamp(int(raw)).strftime("%H:%M %b %d")
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        data.update(_quota_freshness(data, quota_file))
        # Feed the history the chart reads; value-dedup'd, so the 15s refresh
        # costs nothing while quota stands still. Never let it break the panel.
        try:
            quota_projection.record_sample(
                data, WORKSPACE_DIR / "state" / "quota-history.jsonl",
                datetime.now().timestamp())
        except OSError:
            pass
        return data
    except Exception:
        return {"available": True}


# Past this, the reading is old enough that acting on it is a mistake. Matches
# the 6h "down" threshold the comm-sweep freshness probe already uses, so the
# fleet has one staleness vocabulary rather than a per-panel invention.
QUOTA_STALE_HOURS = 6.0


def quota_chart_response() -> tuple[int, bytes]:
    """The /api/quota-chart decision: (status, body).

    Serializes BEFORE returning a status so a strict-JSON failure surfaces as
    a 500, never a 200 with an empty body. `live` is the same observation the
    quota tile renders, so the chart cannot publish a current point the tile
    contradicts.
    """
    try:
        payload = quota_projection.chart_payload(
            WORKSPACE_DIR / "state" / "quota-history.jsonl",
            datetime.now().timestamp(), live=get_quota_status())
        return 200, json.dumps(payload, allow_nan=False).encode()
    except ValueError:
        return 500, b'{"error": "non-finite value in chart payload"}'


def _quota_freshness(data: dict, quota_file) -> dict:
    """Age of the reading, from `last_checked` — falling back to file mtime.

    `last_checked` is what the WRITER observed; mtime is only when the file was
    last touched. Prefer the writer's own timestamp and fall back, rather than
    trusting mtime, so a rewrite that carries an old reading still reads old.
    Unparseable/absent timestamps yield `age_h: None` + `stale: True` — unknown
    age is treated as stale, because the whole point is to stop presenting
    unverified numbers as current.
    """
    checked = data.get("last_checked")
    ts = None
    if isinstance(checked, str) and checked:
        try:
            ts = datetime.fromisoformat(checked.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
    if ts is None:
        try:
            ts = quota_file.stat().st_mtime
        except OSError:
            return {"age_h": None, "stale": True}
    age_h = max(0.0, (datetime.now().timestamp() - ts) / 3600.0)
    return {"age_h": round(age_h, 1), "stale": age_h >= QUOTA_STALE_HOURS}



def _quota_has_data(quota: dict) -> bool:
    """Whether a reading actually exists, as opposed to defaulting to zero.

    The tiles below format utilization with `.get(..., 0)`, so an ABSENT file
    rendered as "0% used" plus a green check — absence shown as "healthy,
    nothing consumed", which is the confidently-wrong failure get_quota_status
    exists to avoid. Same discriminator as the age label.
    """
    return bool(quota.get("headers")) or quota.get("age_h") is not None


# Glyph is a THREE-way split, not two: no reading -> "—", a reading the API
# refused -> "✗", a good reading -> "✓". Collapsing the last two hides a real
# rate-limit behind a check.


def _quota_tile_pct(quota: dict, window: str) -> str:
    """One tile's percentage, or an em dash for unknown.

    Per-window on purpose: a missing, non-finite, negative or unparseable
    value degrades ITS tile to unknown; the sibling window still renders.
    """
    import math as _math
    raw = quota.get(f"utilization_{window}")
    if raw in (None, "", 0):
        raw = (quota.get("headers") or {}).get(
            f"anthropic-ratelimit-unified-{window}-utilization")
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not _math.isfinite(v) or v < 0:
        return "—"
    pct = v * 100.0
    # The module leaves utilization unbounded; THIS consumer's clamp is the
    # triple-digit cap, so a huge finite value can never overflow int().
    if pct > 999:
        return "999%+"
    return f"{int(pct)}%"


def _quota_age_label(quota: dict) -> str:
    """One short string for the panel: how old this reading is.

    Rendered for EVERY state, not only the bad one — a panel that says nothing
    when fresh and something when stale trains the eye to ignore the absence.
    """
    if not quota.get("headers") and quota.get("age_h") is None:
        return "no data"
    age = quota.get("age_h")
    if age is None:
        return "age unknown"
    if age >= 24:
        return f"STALE {age/24:.1f}d old"
    if quota.get("stale"):
        return f"STALE {age:.1f}h old"
    if age >= 1:
        return f"{age:.1f}h ago"
    return f"{int(age*60)}m ago"

def get_system_stats() -> dict:
    import shutil
    free_gb = shutil.disk_usage(WORKSPACE_DIR).free / (1024 ** 3)

    try:
        result = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
        battery_output = result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        battery_output = ""
    battery_m = re.search(r'(\d+)%', battery_output)
    if battery_m:
        battery = f"{battery_m.group(1)}%"
        # \b keeps "discharging" (battery power) from substring-matching "charging".
        charging = bool(re.search(r'\bcharging\b', battery_output.lower())) or "ac power" in battery_output.lower()
    else:
        # Battery-less Mac (mini / Studio / Pro): pmset reports "AC Power" with no
        # percentage line. The old "?" + charging=True combo rendered as "? ⚡".
        battery = "—"
        charging = False

    return {
        "disk_free": f"{free_gb:.0f}GB",
        "battery": battery,
        "charging": charging,
        "uptime": datetime.now().strftime("%H:%M"),
        "quota": get_quota_status(),
    }


HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sutando Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#0a0a12;color:#c0c0d0;min-height:100vh;padding:20px}
.grid{max-width:900px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
.card{background:#12121e;border:1px solid #1e1e30;border-radius:10px;padding:16px}
.card.full{grid-column:1/-1}
h1{font-size:16px;color:#fff;margin-bottom:2px}
.sub{font-size:11px;color:#444;margin-bottom:16px}
h2{font-size:12px;color:#555;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px}
.score{font-size:28px;font-weight:600;color:#4ecca3;margin-bottom:4px}
.stat-row{display:flex;gap:16px;flex-wrap:wrap}
.stat{text-align:center;flex:1;min-width:60px}
.stat-val{font-size:18px;font-weight:600;color:#fff}
.stat-label{font-size:10px;color:#555;text-transform:uppercase}
.check{display:flex;align-items:center;gap:6px;font-size:12px;padding:3px 0;color:#888}
.check .ok{color:#4ecca3}.check .bad{color:#e94560}
.activity-item{padding:6px 0;border-bottom:1px solid #1a1a2a}
.activity-item:last-child{border:none}
.activity-time{font-size:10px;color:#444}
.activity-title{font-size:12px;color:#aaa}
.pending-badge{display:inline-block;background:#2a2a1a;color:#aa8;padding:2px 8px;border-radius:10px;font-size:11px}
.pending-badge.done{background:#1a2a1a;color:#5a9a6a}
.refresh{font-size:10px;color:#333;text-align:center;margin-top:12px}
.intro{max-width:900px;margin:12px auto 0;color:#7b7b90;font-size:12px;line-height:1.45}
.quick-links{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}
.quick-links a{color:#4a8aaa;text-decoration:none}
</style>
<script>
function openQuickLink(event, link){
  event.preventDefault();
  window.open(link.href,'_blank','noopener,noreferrer');
}
</script>
</head><body>
<div style="max-width:900px;margin:0 auto">
<div style="display:flex;align-items:center;gap:14px">
<img id="stand-avatar" src="/avatar" style="width:56px;height:56px;border-radius:50%;border:2px solid #4ecca3;display:none;object-fit:cover">
<div><h1 id="stand-name">Sutando</h1>
<p class="sub" id="stand-sub">Operational view of use cases, health, activity, and quota</p></div></div>
<script>
fetch('/stand-identity').then(r=>r.json()).then(s=>{
  if(s.name){document.getElementById('stand-name').textContent='Sutando — '+s.name;
  document.getElementById('stand-sub').textContent='Stand awakened '+s.awakened+' · live status for '+(s.capabilities?.primary?.split('—')[0]?.trim()||'active systems')}
  if(s.avatarGenerated){var img=document.getElementById('stand-avatar');img.style.display='block'}
}).catch(()=>{});
</script>
</div>
<p class="intro">Tracks current system status alongside the latest capability matrix, recent activity, local endpoints, and quota pressure.</p>
<div class="grid" id="content">__CONTENT__</div>
<p class="refresh">Auto-refreshes every 15s</p>
<script>
let _schedBusy=false;
setInterval(()=>{if(!_schedBusy && !(document.activeElement&&document.activeElement.tagName==='INPUT'))location.reload();},15000);
function _schedMsg(t,ok){const m=document.getElementById('sched-msg');if(m){m.textContent=t;m.style.color=ok?'#7d9':'#d99';}}
async function _post(job){
  _schedBusy=true;
  try{const r=await fetch('/api/schedules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(job)});
    const d=await r.json();
    if(r.ok){_schedMsg(d.note||'Saved.',true);setTimeout(()=>location.reload(),900);}
    else{_schedMsg(d.error||'Save failed',false);}
  }catch(e){_schedMsg('Request failed: '+e,false);}
  finally{_schedBusy=false;}
}
function saveCron(btn){
  const tr=btn.closest('tr');const name=tr.dataset.name;
  const cron=tr.querySelector('.cron-in').value.trim();
  _post({name:name,cron:cron});
}
async function delCron(btn){
  const tr=btn.closest('tr');const name=tr.dataset.name;
  if(!confirm('Delete schedule "'+name+'"?'))return;
  _schedBusy=true;
  try{const r=await fetch('/api/schedules/'+encodeURIComponent(name),{method:'DELETE'});
    const d=await r.json();
    if(r.ok){_schedMsg(d.note||'Removed.',true);setTimeout(()=>location.reload(),900);}
    else{_schedMsg('Delete failed',false);}
  }catch(e){_schedMsg('Request failed: '+e,false);}finally{_schedBusy=false;}
}
function addCron(){
  const name=document.getElementById('ns-name').value.trim();
  const cron=document.getElementById('ns-cron').value.trim();
  const body=document.getElementById('ns-body').value.trim();
  if(!name||!cron||!body){_schedMsg('name, cron, and body are all required',false);return;}
  const job={name:name,cron:cron};
  if(body.startsWith('/'))job.prompt_skill=body.slice(1); else job.prompt=body;
  _post(job);
}
</script>
</body></html>"""


TESTED_USE_CASES = {
    "Speaking while you work",          # Screen capture tested via voice multiple times
    "The agent as your second brain",   # Note-taking tested via voice ("take a note...")
    "The agent that meets you where you are",  # Context-drop shortcut set up and tested
    "The agent that never sleeps",      # Feed monitor email confirmed (A1 done)
    "One instruction, ten steps done",  # Voice task delegation + context drop tested 2026-03-19
    "The agent that attends meetings for you",  # Phone call from sutando-core verified 2026-03-20
    "Stay focused while agent handles logistics",  # Daily briefing + reminders tested 2026-03-21
    "Building a side income while you sleep",  # Newsletter pipeline + feed monitor tested 2026-03-21
    "The agent that closes the loop on its own mistakes",  # Crisis monitor + health check tested 2026-03-21
    "The agent that notices what you don't",  # Pattern detector + user model tested 2026-03-21
    "The agent that knows how you learn",  # Learning tracker tested 2026-03-21
    "The agent that amplifies your creative work",  # Browser automation + screen capture tested 2026-03-21
    "The agent that handles your bills",  # Bill tracker add/list/pay tested 2026-03-21
    "The agent that grows with you",  # User model + notes search + teaching flow tested 2026-03-21
    "Your agent and friend's agent coordinate",  # Agent API POST /task tested 2026-03-21
    "The agent that follows you from device to device",  # Agent API + tunnel script tested 2026-03-21
    "The agent that levels itself up",  # Proactive loop + health check + auto-fix tested 2026-03-21
    "Learning your taste over time",  # Teaching flow + user model + memory tested 2026-03-22
    "The agent that learns from demonstration",  # Teaching protocol + voice routing tested 2026-03-22
}

def get_use_case_matrix() -> str:
    build_log = WORKSPACE_DIR / "build_log.md"
    if not build_log.exists():
        return ""
    content = build_log.read_text()
    rows = []
    for m in re.finditer(r'\| (.+?) \| (✓|~|✗) \| (.+?) \|', content):
        name, status, detail = m.group(1).strip(), m.group(2), m.group(3).strip()
        if name == "Use case":
            continue
        color = "#4ecca3" if status == "✓" else "#f0ad4e" if status == "~" else "#e94560"
        tested = '<span style="color:#4a8aaa;font-size:9px"> tested</span>' if name in TESTED_USE_CASES else ''
        anchor = name.lower().replace(" ", "-").replace("'", "").replace(",", "").replace(":", "")
        link = f'<a href="https://github.com/sonichi/sutando/blob/main/README.md#{anchor}" target="_blank" style="color:inherit;text-decoration:none;border-bottom:1px dotted #333">{name}</a>'
        rows.append(f'<tr><td style="color:{color}">{status}</td><td>{link}{tested}</td><td style="color:#555;font-size:10px">{detail[:60]}</td></tr>')
    if not rows:
        return ""
    return '<table style="width:100%;font-size:11px;border-collapse:collapse"><tr style="color:#555;text-align:left"><th></th><th>Use Case</th><th>Details</th></tr>' + ''.join(rows) + '</table>'


# ── Schedule domain/storage: delegated to dashboard_schedules ────────────────
# Cron parsing, schedule validation and atomic crons.json persistence live in
# src/dashboard_schedules.py. What remains here are thin presentation-layer
# wrappers that supply the resolved path via _crons_path(); dashboard.py owns
# path resolution, HTTP adaptation and rendering, nothing else.
# The wrappers are kept (rather than deleted) so existing callers and the
# integration tests keep their current names.

_CRON_BOUNDS = dashboard_schedules.CRON_BOUNDS


def _cron_field_match(spec: str, value: int) -> bool:
    """Delegates to dashboard_schedules.cron_field_match."""
    return dashboard_schedules.cron_field_match(spec, value)


def _cron_field_valid(spec: str, lo: int, hi: int) -> bool:
    """Delegates to dashboard_schedules.cron_field_valid."""
    return dashboard_schedules.cron_field_valid(spec, lo, hi)


def _cron_next_run(expr: str, now: datetime, horizon_days: int = 8):
    """Delegates to dashboard_schedules.next_run."""
    return dashboard_schedules.next_run(expr, now, horizon_days)


def _html_attr(v: str) -> str:
    """Escape a string for safe use inside a double-quoted HTML attribute."""
    return (str(v).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _crons_path():
    """This host's crons.json — canonical host-label (matches schedule-crons +
    where the file is actually written), NOT the bare hostname (which drifts on
    DHCP lease changes; #1745). Read and write MUST agree, so both go through
    this helper."""
    return WORKSPACE_DIR / "hosts" / _host_label() / "crons.json"


def _read_crons() -> list:
    """Delegates to dashboard_schedules.read_crons for this host's path."""
    return dashboard_schedules.read_crons(_crons_path())


# The transaction lock now lives with the transactions it protects, in
# dashboard_schedules. Re-exported so anything reaching for the old name still
# gets THE lock rather than silently creating a second, unrelated one.
_CRONS_LOCK = dashboard_schedules._CRONS_LOCK


def _write_crons(jobs: list) -> None:
    """Delegates to dashboard_schedules.write_crons for this host's path."""
    dashboard_schedules.write_crons(_crons_path(), jobs)


def _validate_job(job: dict) -> str | None:
    """Delegates to dashboard_schedules.validate_job."""
    return dashboard_schedules.validate_job(job)


def upsert_schedule(body: dict) -> tuple[int, dict]:
    """Add/edit a schedule. Path resolution stays here; the read→merge→validate
    →write transaction (and its lock) belongs to dashboard_schedules."""
    return dashboard_schedules.upsert_schedule(_crons_path(), body)


def delete_schedule(name: str) -> tuple[int, dict]:
    """Delete a schedule by name. Path resolution stays here; the locked
    read→delete→write transaction belongs to dashboard_schedules."""
    return dashboard_schedules.delete_schedule(_crons_path(), name)


def get_schedules() -> list[dict]:
    """This host's cron schedules + computed next-run time.

    Source: <workspace>/hosts/<hostname>/crons.json (see skills/schedule-crons).
    Status is 'active' + next run; last-run history isn't tracked on disk.
    """
    # Reads via _read_crons() → _crons_path(), which keys off the scutil-first
    # canonical `_host_label()` (NOT bare hostname) so this panel matches the
    # WRITER (schedule-crons) and doesn't read the wrong hosts/<host>/ dir under
    # a DHCP hostname drift (#1745).
    jobs = _read_crons()
    now = datetime.now()
    out = []
    for job in jobs:
        expr = job.get("cron", "")
        if job.get("shell_command"):
            kind = "shell"
        elif job.get("prompt_skill"):
            kind = f'skill:{job["prompt_skill"]}'
        else:
            kind = "prompt"
        nxt = _cron_next_run(expr, now) if expr else None
        if nxt:
            mins = int((nxt - now).total_seconds() // 60)
            if mins < 60:
                rel = f"in {mins}m"
            elif mins < 1440:
                rel = f"in {mins // 60}h{mins % 60:02d}m"
            else:
                rel = f"in {mins // 1440}d{(mins % 1440) // 60}h"
            next_str = f'{nxt.strftime("%a %H:%M")} ({rel})'
        else:
            next_str = ">7d" if expr else "invalid"
        if job.get("description"):
            desc = job["description"]
        elif job.get("shell_command"):
            desc = f'Runs shell command: {job["shell_command"]}'
        elif job.get("prompt_skill"):
            desc = f'Runs the /{job["prompt_skill"]} skill'
        else:
            _p = re.sub(r"^Run:?\s*", "", (job.get("prompt") or "").strip())
            desc = (_p[:100] + "…") if len(_p) > 100 else _p
        desc = desc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out.append({"name": job.get("name", "?"), "cron": expr, "kind": kind,
                    "next": next_str, "desc": desc})
    return out


# Sparklines for the two quota stat cells: current window vs even-pace
# diagonal. Plain string, NOT an f-string — the inline JS is brace-heavy.
_QUOTA_SPARK_JS = """<script>
(async()=>{try{
const d=await (await fetch('/api/quota-chart')).json();
for(const[k,el]of[['5h','qs-5h'],['7d','qs-7d']]){
  const svg=document.getElementById(el);if(!svg)continue;
  const segs=d.windows[k].segments.filter(s=>s.current);
  if(!segs.length)continue;
  const s=segs[segs.length-1],W=160,H=60,X=f=>f*W,Y=u=>H-Math.min(u,1.2)/1.2*H;
  // ring meter: fill = usage, tick = even pace; red once usage passes the tick
  const ring=document.getElementById(el.replace('qs-','qr-'));
  if(ring){
    const lastP=s.points[s.points.length-1],uRaw=lastP.y,pc=lastP.x;
    // Same degradation as _quota_tile_pct, so both render paths agree:
    // unusable -> em dash, huge -> 999%+. Unguarded this printed Infinity%.
    const ok=Number.isFinite(uRaw)&&uRaw>=0,u=ok?uRaw:0;
    const label=!ok?'\u2014':(uRaw*100>999?'999%+':Math.trunc(uRaw*100)+'%');
    const C=22,R=17,TAU=2*Math.PI,a0=-TAU/4;
    const arc=(frac,color,w)=>{
      const a1=a0+frac*TAU,large=frac>0.5?1:0;
      return `<path d="M ${C+R*Math.cos(a0)} ${C+R*Math.sin(a0)} A ${R} ${R} 0 ${large} 1 ${C+R*Math.cos(a1)} ${C+R*Math.sin(a1)}" fill="none" stroke="${color}" stroke-width="${w}"/>`;};
    let ro=`<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="#2a2a45" stroke-width="5"/>`;
    ro+=arc(Math.min(u,1),u>pc?'#e94560':'#4ecca3',5);
    const ta=a0+pc*TAU;
    ro+=`<line x1="${C+(R-4)*Math.cos(ta)}" y1="${C+(R-4)*Math.sin(ta)}" x2="${C+(R+4)*Math.cos(ta)}" y2="${C+(R+4)*Math.sin(ta)}" stroke="#8888aa" stroke-width="1.5"/>`;
    ro+=`<text x="${C}" y="${C+3.5}" text-anchor="middle" fill="#e8e8f0" font-size="10" font-weight="600">${label}</text>`;
    ring.innerHTML=ro;
  }
  let out=`<line x1="0" y1="${Y(0)}" x2="${W}" y2="${Y(1)}" stroke="#555" stroke-dasharray="2,2"/>`;
  const stroke=(p,q,over)=>{out+=`<line x1="${X(p.x)}" y1="${Y(p.y)}" x2="${X(q.x)}" y2="${Y(q.y)}" stroke="${over?'#e94560':'#4ecca3'}" stroke-width="1.5"/>`;};
  for(let j=1;j<s.points.length;j++){
    const a=s.points[j-1],b=s.points[j];
    const d0=a.y-a.x,d1=b.y-b.x;
    if((d0>0)!==(d1>0)&&d0!==d1){
      const f=d0/(d0-d1),c={x:a.x+f*(b.x-a.x),y:a.y+f*(b.y-a.y)};
      stroke(a,c,d0>0);stroke(c,b,d1>0);
    }else{
      stroke(a,b,(d0+d1)/2>0);
    }
  }
  const last=s.points[s.points.length-1];
  out+=`<circle cx="${X(last.x)}" cy="${Y(last.y)}" r="2" fill="${last.y>last.x?'#e94560':'#4ecca3'}"/>`;
  if(s.projected_end!==undefined)
    out+=`<line x1="${X(last.x)}" y1="${Y(last.y)}" x2="${X(1)}" y2="${Y(s.projected_end)}" stroke="${s.projected_end>1?'#e94560':'#4ecca3'}" stroke-dasharray="2,2"/>`;
  svg.innerHTML=out;
}}catch(e){}})();
</script>"""


def render_dashboard() -> str:
    health = get_health()
    activity = get_activity(5)
    pending = get_pending_count()
    score = get_score()
    stats = get_system_stats()

    services_only = [c for c in health if "port" in c.get("detail", "") or "running" in c.get("detail", "") or c.get("name", "").startswith("com.sutando.")]
    ok_count = sum(1 for c in services_only if c.get("status") in ("ok", "warn"))
    total_count = len(services_only)

    # Score card. A missing/unparseable score used to render as a bare "?" —
    # glyph soup for new installs whose build_log.md has no **Score:** marker
    # yet. Show a real empty state instead of pretending "?" is a value.
    if score == "?":
        score_html = ('<p style="font-size:12px;color:#667;line-height:1.5;margin-top:4px">'
                      'Nothing scored yet. Use cases appear here once the build log '
                      'records one (a <code style="color:#889">**Score: …**</code> line in '
                      '<code style="color:#889">build_log.md</code>).</p>')
    else:
        score_html = f'<div class="score">{score}</div>'
    cards = [f"""<div class="card">
<h2>Use Cases</h2>
{score_html}
</div>"""]

    # System stats
    charge = " ⚡" if stats["charging"] else ""
    cards.append(f"""<div class="card">
<h2>System</h2>
<div class="stat-row">
<div class="stat"><div class="stat-val">{stats['disk_free']}</div><div class="stat-label">Disk Free</div></div>
<div class="stat"><div class="stat-val">{stats['battery']}{charge}</div><div class="stat-label">Battery</div></div>
<div class="stat"><div class="stat-val">{ok_count}/{total_count}</div><div class="stat-label">Services OK</div></div>
<div class="stat"><div class="stat-val">{pending['open']}</div><div class="stat-label">Pending</div></div>
<div class="stat"><div class="stat-val">{"⚠" if stats["quota"].get("stale") else ("—" if not _quota_has_data(stats["quota"]) else ("✓" if stats["quota"].get("available", True) else "✗"))}</div><div class="stat-label">Quota<br><span style="font-size:9px;color:{"#b45309" if stats["quota"].get("stale") else "#444"}">{_quota_age_label(stats["quota"])}</span></div></div>
<div class="stat"><div class="stat-val" style="display:flex;align-items:center;justify-content:center;gap:8px"><svg id="qr-5h" width="44" height="44" viewBox="0 0 44 44" style="flex:none"><text x="22" y="26" text-anchor="middle" fill="#e8e8f0" font-size="10">{_quota_tile_pct(stats["quota"], "5h") if _quota_has_data(stats["quota"]) else "—"}</text></svg><svg id="qs-5h" width="160" height="60" viewBox="0 0 160 60" style="flex:none"></svg></div><div class="stat-label">5h Used<br><span style="font-size:9px;color:#444">↻ {stats["quota"].get("reset_5h", "?")}</span></div></div>
<div class="stat"><div class="stat-val" style="display:flex;align-items:center;justify-content:center;gap:8px"><svg id="qr-7d" width="44" height="44" viewBox="0 0 44 44" style="flex:none"><text x="22" y="26" text-anchor="middle" fill="#e8e8f0" font-size="10">{_quota_tile_pct(stats["quota"], "7d") if _quota_has_data(stats["quota"]) else "—"}</text></svg><svg id="qs-7d" width="160" height="60" viewBox="0 0 160 60" style="flex:none"></svg></div><div class="stat-label">7d Used<br><span style="font-size:9px;color:#444">↻ {stats["quota"].get("reset_7d", "?")}</span></div></div>
</div>""" + _QUOTA_SPARK_JS + """</div>""")

    # Services (ports + daemons only)
    services = [c for c in health if "port" in c.get("detail", "") or "running" in c.get("detail", "") or c.get("name", "").startswith("com.sutando.")]
    services_html = ""
    for c in services:
        st = c.get("status")
        if st == "ok":
            icon = '<span class="ok">✓</span>'
        elif st == "warn":
            icon = '<span style="color:#f0ad4e">~</span>'
        elif st == "stale":
            icon = '<span style="color:#9b59b6">♻</span>'
        else:
            icon = '<span class="bad">✗</span>'
        services_html += f'<div class="check">{icon} {c.get("name", "?")} <span style="color:#333;margin-left:auto">{c.get("detail", "")}</span></div>\n'
    cards.append(f'<div class="card"><h2>Services</h2>{services_html}</div>')

    # Activity
    activity_html = ""
    for a in activity:
        activity_html += f'<div class="activity-item"><span class="activity-time">{a["time"]}</span> <span class="activity-title">{a["title"]}</span></div>\n'
    cards.append(f'<div class="card"><h2>Recent Activity</h2>{activity_html or "<span style=color:#333>No activity</span>"}</div>')

    # Capabilities matrix
    matrix_html = get_use_case_matrix()
    if matrix_html:
        cards.append(f'<div class="card full"><h2>Capabilities Matrix</h2>{matrix_html}</div>')

    # Outbox (recent outbound messages)
    outbox = get_outbox(10)
    if outbox:
        _channel_icon = {
            "discord_dm": "💬", "discord_channel": "📢",
            "slack_dm": "💬", "slack_channel": "📢",
            "telegram": "✈️", "imessage": "💬", "whatsapp": "📱",
            "email": "📧", "x": "𝕏",
        }
        outbox_html = ""
        for e in reversed(outbox):
            icon = _channel_icon.get(e.get("channel_type", ""), "→")
            ts_str = e.get("iso_ts", "")[:16].replace("T", " ")
            label = e.get("recipient_label") or e.get("recipient", "?")[:20]
            preview = e.get("body_preview", "")[:80]
            outbox_html += (
                f'<div class="activity-item">'
                f'<span class="activity-time">{ts_str} {icon} {label}</span> '
                f'<span class="activity-title" style="color:#666">{preview}</span>'
                f'</div>\n'
            )
        cards.append(f'<div class="card full"><h2>Outbox</h2>{outbox_html}</div>')

    # Keyboard shortcuts
    # Match both the dev-built binary (`<repo>/src/Sutando/Sutando`) and the
    # distributed .app (`/Applications/Sutando.app/Contents/MacOS/Sutando`).
    sutando_running = subprocess.run(["/usr/bin/pgrep", "-f", "(Sutando|MacOS)/Sutando"], capture_output=True).returncode == 0
    shortcut_status = '<span class="ok">✓</span> Sutando app running' if sutando_running else '<span class="bad">✗</span> Sutando app not running'
    # Shortcuts come from <workspace>/state/hotkeys.json (published by the
    # Sutando app from its resolved config — single source of truth). Only the
    # human descriptions are local UI copy, keyed by the stable action name.
    _hk_desc = {
        "drop_context": "Context drop (text/image/file)",
        "drop_screenshot": "Drop screenshot",
        "drop_video_clip": "Drop video clip",
        "toggle_voice": "Toggle voice",
        "toggle_mute": "Toggle mute",
    }
    try:
        _hk = json.loads((WORKSPACE_DIR / "state" / "hotkeys.json").read_text())
    except (OSError, ValueError):
        _hk = []  # app hasn't published yet — show the header only
    _hk_rows = "".join(
        f'<div style="margin:4px 0"><kbd style="background:#222;color:#aaa;padding:2px 6px;border-radius:3px;font-family:monospace">{e.get("label","")}</kbd> {_hk_desc.get(e.get("action"), e.get("action",""))}</div>'
        for e in _hk
    )
    cards.append(f"""<div class="card">
<h2>Keyboard Shortcuts</h2>
<div class="check">{shortcut_status}</div>
<div style="margin-top:8px;font-size:12px;color:#555">
{_hk_rows}
</div></div>""")

    # Schedules (cron jobs from this host's crons.json)
    schedules = get_schedules()
    sched_rows = ""
    for s in schedules:
        nm = _html_attr(s["name"])
        sched_rows += (
            f'<tr data-name="{nm}">'
            f'<td style="color:#8ab">{s["name"]}'
            f'<div style="font-size:9px;color:#555">{s.get("desc","")}</div></td>'
            f'<td><input class="cron-in" value="{_html_attr(s["cron"])}" '
            f'style="width:110px;font-family:monospace;font-size:10px;background:#12121c;'
            f'color:#9cf;border:1px solid #2a2a3e;border-radius:3px;padding:2px 4px"></td>'
            f'<td style="color:#555;font-size:10px">{s["kind"]}</td>'
            f'<td style="color:#4a8aaa;font-size:10px">{s["next"]}</td>'
            f'<td style="white-space:nowrap">'
            f'<button onclick="saveCron(this)" style="font-size:10px;cursor:pointer;'
            f'background:#1a3a2a;color:#7d9;border:none;border-radius:3px;padding:2px 6px;margin-right:3px">Save</button>'
            f'<button onclick="delCron(this)" style="font-size:10px;cursor:pointer;'
            f'background:#3a1a1a;color:#d99;border:none;border-radius:3px;padding:2px 6px">Del</button>'
            f'</td></tr>\n'
        )
    _in = ('background:#12121c;color:#ccd;border:1px solid #2a2a3e;'
           'border-radius:3px;padding:2px 4px;font-size:10px')
    add_row = (
        '<tr style="border-top:1px solid #2a2a3e">'
        f'<td><input id="ns-name" placeholder="name" style="width:90px;{_in}"></td>'
        f'<td><input id="ns-cron" placeholder="*/10 * * * *" style="width:110px;font-family:monospace;{_in}"></td>'
        f'<td colspan="2"><input id="ns-body" placeholder="/skill-name  or  Run: ..." style="width:100%;{_in}"></td>'
        '<td><button onclick="addCron()" style="font-size:10px;cursor:pointer;'
        'background:#1a2a3a;color:#9cf;border:none;border-radius:3px;padding:2px 8px">Add</button></td>'
        '</tr>'
    )
    cards.append(
        '<div class="card full" id="schedules"><h2>Schedules</h2>'
        '<table style="width:100%;font-size:11px;border-collapse:collapse">'
        '<tr style="color:#555;text-align:left"><th>Name</th><th>Cron</th>'
        '<th>Type</th><th>Next run</th><th></th></tr>'
        + sched_rows + add_row +
        '</table>'
        '<div id="sched-msg" style="font-size:10px;margin-top:4px;min-height:12px"></div>'
        '<div style="font-size:9px;color:#444;margin-top:2px">'
        f'{len(schedules)} active. Edits persist to crons.json and take effect on the '
        'next /schedule-crons run (restart). New job body: a <code>/skill-name</code> '
        '(→ prompt_skill) or free text (→ prompt).</div></div>'
    )

    # Quick links
    cards.append("""<div class="card full">
<h2>Quick Links</h2>
<div class="quick-links">
<a href="http://localhost:8080" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Voice UI :8080</a>
<a href="http://localhost:7843" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Task API :7843</a>
<a href="http://localhost:7844" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Dashboard :7844</a>
<a href="http://localhost:7845" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Screen Capture :7845</a>
<a href="/notes-ui" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Notes Browser</a>
<a href="https://github.com/sonichi/sutando" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">GitHub</a>
<a href="https://sutando.ai" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Website</a>
<a href="https://discord.gg/uZHWXXmrCS" target="_blank" rel="noopener noreferrer" onclick="openQuickLink(event,this)">Discord</a>
</div></div>""")

    return HTML.replace("__CONTENT__", "\n".join(cards))


# ── Mutating-request gate ────────────────────────────────────────────────────

# CORS hides a cross-origin response but does not stop the request, and a
# safelisted text/plain POST reaches the handler with no preflight.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# A wildcard bind names no host, so the legitimate Host set cannot be derived
# from it; DASHBOARD_ALLOWED_HOSTS supplies it explicitly for LAN mode.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})


def _authority_host(value: str) -> str:
    """Hostname from a Host/Origin authority, minus port and IPv6 brackets."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _allowed_mutation_hosts(bind: str, declared: str) -> "set[str] | None":
    """Host names a mutation may carry. None = wildcard bind with none declared."""
    names = {h for h in (_authority_host(x) for x in (declared or "").split(",")) if h}
    if (bind or "").strip().lower() in _WILDCARD_BINDS:
        return (_LOOPBACK_HOSTS | names) if names else None
    return _LOOPBACK_HOSTS | {bind.strip().lower()} | names


def mutation_request_allowed(origin, host, content_type, *, expect_body,
                             bind="127.0.0.1", allowed_hosts=""):
    """Fail-closed gate for state-changing dashboard requests → (ok, reason)."""
    permitted = _allowed_mutation_hosts(bind, allowed_hosts)
    if permitted is None:
        # Refusing loudly beats 403-ing every save with "host not allowed": on a
        # wildcard bind the operator must name the hosts, or rebinding is free.
        return False, ("wildcard DASHBOARD_BIND requires DASHBOARD_ALLOWED_HOSTS "
                       "(comma-separated) before mutations are accepted")
    if _authority_host(host) not in permitted:
        # DNS rebinding: the attacker's name resolves to us, so only the Host
        # header still carries it.
        return False, "host not allowed"
    if not origin:
        return False, "missing Origin"
    if urlparse(origin).netloc.strip().lower() != (host or "").strip().lower():
        return False, "cross-origin request refused"
    if expect_body and (content_type or "").split(";", 1)[0].strip().lower() != "application/json":
        # Non-safelisted type: a cross-origin sender must preflight, which this
        # server never grants.
        return False, "Content-Type must be application/json"
    return True, None


class Handler(http.server.BaseHTTPRequestHandler):
    # Drop connections that go silent (e.g. browser speculative preconnects
    # that open TCP and never send a request line). Without this, readline()
    # in handle_one_request blocks forever holding a server thread.
    timeout = 30

    def log_message(self, fmt, *args): pass

    # No wildcard CORS. The dashboard UI is same-origin (served from this same
    # loopback origin), so it needs no Access-Control-Allow-Origin. Sending
    # `*` on every response — while advertising POST/DELETE — let a cross-origin
    # browser tab mutate loopback schedules in browsers without Private Network
    # Access enforcement (CR #2164, qingyun-wu). Omitting the header makes the
    # browser block any cross-origin read or state-changing request; same-origin
    # calls are unaffected.
    def do_OPTIONS(self):  # pragma: no cover — HTTP preflight; no cross-origin grant
        # Same-origin requests never preflight; answer without granting cross-
        # origin access (no Access-Control-Allow-Origin → browser denies).
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def _json_body(self):  # pragma: no cover — reads the HTTP request body
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return None

    def _reply_json(self, code, obj):  # pragma: no cover — writes the HTTP response
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _gate(self, *, expect_body):  # pragma: no cover — reads request headers
        """Refuse a state-changing request that fails mutation_request_allowed."""
        ok, why = mutation_request_allowed(
            self.headers.get("Origin"), self.headers.get("Host", ""),
            self.headers.get("Content-Type"), expect_body=expect_body,
            bind=os.environ.get("DASHBOARD_BIND", "127.0.0.1"),
            allowed_hosts=os.environ.get("DASHBOARD_ALLOWED_HOSTS", ""))
        if not ok:
            # Drain first: replying and closing with the body still unread makes
            # the peer see a connection reset instead of the 403.
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                self.rfile.read(n)
            self._reply_json(403, {"error": why})
        return ok

    def do_POST(self):  # pragma: no cover — thin HTTP glue over upsert_schedule()
        """Upsert a cron job. Business logic is the unit-tested pure
        upsert_schedule(); the gate is mutation_request_allowed()."""
        if urlparse(self.path).path != "/api/schedules":
            self.send_response(404); self.end_headers(); return
        if not self._gate(expect_body=True):
            return
        code, obj = upsert_schedule(self._json_body())
        self._reply_json(code, obj)

    def do_GET(self):
        if urlparse(self.path).path == "/":
            html = render_dashboard()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif urlparse(self.path).path == "/avatar":
            avatar_file = personal_path("stand-avatar.png")
            if avatar_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(avatar_file.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif urlparse(self.path).path == "/stand-identity":
            si_file = personal_path("stand-identity.json")
            data = json.loads(si_file.read_text()) if si_file.exists() else {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif urlparse(self.path).path == "/api/quota-chart":
            code, body = quota_chart_response()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif urlparse(self.path).path == "/json":
            data = {
                "score": get_score(),
                "health": get_health(),
                "activity": get_activity(5),
                "pending": get_pending_count(),
                "system": get_system_stats(),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif urlparse(self.path).path == "/notes-ui":
            html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sutando Notes</title>
<style>
body{font-family:-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0;margin:0;padding:20px;max-width:900px;margin:0 auto}
a{color:#7c83ff;text-decoration:none}a:hover{text-decoration:underline}
h1{color:#fff;border-bottom:1px solid #333;padding-bottom:10px}
.note-list{list-style:none;padding:0}.note-list li{padding:8px 12px;border-bottom:1px solid #2a2a3e}
.note-list li:hover{background:#2a2a3e;border-radius:4px}
.note-content{background:#2a2a3e;padding:20px;border-radius:8px;font-size:14px;line-height:1.6}
.note-content h1,.note-content h2,.note-content h3{color:#fff;margin-top:20px}
.note-content code{background:#1a1a2e;padding:2px 6px;border-radius:3px;font-size:13px}
.note-content pre{background:#1a1a2e;padding:12px;border-radius:6px;overflow-x:auto}
.note-content ul,.note-content ol{padding-left:20px}
.note-content li{margin:4px 0}
.note-content a{color:#7c83ff}
.note-content blockquote{border-left:3px solid #555;padding-left:12px;color:#aaa;margin:10px 0}
.back{display:inline-block;margin-bottom:15px;padding:5px 12px;background:#333;border-radius:4px}
.date{color:#888;font-size:12px;float:right}
</style></head><body>
<h1>Sutando Notes</h1>
<div id="app"><ul class="note-list" id="list"></ul></div>
<div id="viewer" style="display:none"><a href="#" class="back" onclick="showList();return false">&larr; Back</a><div class="note-content" id="content"></div></div>
<script>
async function load(){const r=await fetch('/notes');const notes=await r.json();const ul=document.getElementById('list');
ul.innerHTML=notes.map(n=>`<li><a href="#" onclick="showNote('${n.slug}');return false">${n.title}</a><span class="date">${new Date(n.modified*1000).toLocaleDateString()}</span></li>`).join('')}
function md(t){
t=t.replace(/^---[\\s\\S]*?---\\n/,'');
t=t.replace(/^### (.+)$/gm,'<h3>$1</h3>');
t=t.replace(/^## (.+)$/gm,'<h2>$1</h2>');
t=t.replace(/^# (.+)$/gm,'<h1>$1</h1>');
t=t.replace(/```([\\s\\S]*?)```/g,'<pre><code>$1</code></pre>');
t=t.replace(/`([^`]+)`/g,'<code>$1</code>');
t=t.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>');
t=t.replace(/\\*(.+?)\\*/g,'<em>$1</em>');
t=t.replace(/^[\\-\\*] (.+)$/gm,'<li>$1</li>');
t=t.replace(/(<li>.*<\\/li>\\n?)+/g,'<ul>$&</ul>');
t=t.replace(/^> (.+)$/gm,'<blockquote>$1</blockquote>');
t=t.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,'<a href="$2">$1</a>');
t=t.replace(/\\n\\n/g,'<br><br>');
return t}
async function showNote(slug){const r=await fetch('/notes/'+slug);const text=await r.text();document.getElementById('content').innerHTML=md(text);
document.getElementById('app').style.display='none';document.getElementById('viewer').style.display='block'}
function showList(){document.getElementById('app').style.display='block';document.getElementById('viewer').style.display='none'}
load()
</script></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif urlparse(self.path).path == "/notes":
            notes_dir = Path(shared_personal_path("notes"))
            notes = []
            for f in sorted(notes_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                title = f.stem.replace("-", " ").title()
                # Try to extract title from frontmatter
                try:
                    content = f.read_text()
                    for line in content.splitlines():
                        if line.startswith("title:"):
                            title = line.split(":", 1)[1].strip()
                            break
                except Exception:
                    pass
                notes.append({"slug": f.stem, "title": title, "modified": f.stat().st_mtime})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(notes).encode())
        elif urlparse(self.path).path.startswith("/notes/"):
            raw_slug = urlparse(self.path).path.split("/notes/", 1)[1]
            note_file = _resolve_note_path(raw_slug)
            if note_file is None:
                self.send_response(400)
                self.end_headers()
                return
            if note_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.end_headers()
                self.wfile.write(note_file.read_text().encode())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


    def do_DELETE(self):
        """Handle DELETE requests. Every branch here mutates, so the whole
        method is gated — /notes/ deletion was reachable the same way."""
        if not self._gate(expect_body=False):
            return
        path = urlparse(self.path).path
        if path.startswith("/notes/"):
            raw_slug = path.split("/notes/", 1)[1]
            note_file = _resolve_note_path(raw_slug)
            if note_file is None:
                self.send_response(400)
                self.end_headers()
                return
            if note_file.exists():
                note_file.unlink()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"deleted": note_file.stem}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        elif path.startswith("/api/schedules/"):  # pragma: no cover — thin glue over delete_schedule()
            name = urllib.parse.unquote(path.split("/api/schedules/", 1)[1])
            code, obj = delete_schedule(name)
            self._reply_json(code, obj)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    # Default loopback-only. The dashboard exposes owner notes, recent
    # activity, system stats, and the owner's avatar/identity — all
    # privacy-sensitive. The pre-fix `0.0.0.0` bind made every detail
    # readable by any device on the LAN with no auth. Set
    # `DASHBOARD_BIND=0.0.0.0` to opt back into LAN exposure when you
    # know you want it. Same env-override shape as `AGENT_API_BIND` in
    # agent-api.py.
    #
    # A wildcard bind ALSO requires `DASHBOARD_ALLOWED_HOSTS` (comma-separated
    # host[:port] the UI is reached by) or every mutation 403s: with 0.0.0.0
    # there is no host to infer, so the DNS-rebinding gate cannot fail open.
    bind = config_get("DASHBOARD_BIND", "127.0.0.1")
    # ThreadingHTTPServer: the single-threaded HTTPServer wedged whenever one
    # client held a connection without completing a request — every later
    # request (and the dashboard UI) hung on a port that still looked open
    # to startup.sh's lsof guard (2026-06-08/10 incidents).
    server = http.server.ThreadingHTTPServer((bind, PORT), Handler)
    print(f"Sutando Dashboard → http://{bind}:{PORT}", flush=True)
    if bind != "127.0.0.1":
        print(
            f"  (LAN access enabled via DASHBOARD_BIND={bind} — "
            f"the dashboard has NO authentication; anyone on this network "
            f"can read your notes, activity, and identity)",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
