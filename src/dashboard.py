#!/usr/bin/env python3
"""
Sutando dashboard — current system status for the local agent.

Combines: capability matrix, service health, activity feed, quick links, and system stats.

Usage:
  python3 src/dashboard.py              # serve on port 7844
  Open http://localhost:7844 in browser

Auto-refreshes every 15 seconds.
"""

import http.server
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

REPO_DIR = Path(__file__).parent.parent
PORT = 7844


def get_health() -> list[dict]:
    try:
        result = subprocess.run(
            ["python3", str(REPO_DIR / "src/health-check.py"), "--json"],
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
            except:
                time_str = date_str[:16]
            entries.append({'time': time_str, 'title': msg.strip(), 'body': sha})
        return entries
    except:
        return []


def get_pending_count() -> dict:
    pending_file = REPO_DIR / "pending-questions.md"
    if not pending_file.exists():
        return {"open": 0, "done": 0}
    content = pending_file.read_text()
    open_count = len(re.findall(r'\*\*Status:\*\* Waiting', content))
    done_count = len(re.findall(r'\*\*Status:\*\* Answered', content))
    return {"open": open_count, "done": done_count}


def get_score() -> str:
    build_log = REPO_DIR / "build_log.md"
    if not build_log.exists():
        return "?"
    content = build_log.read_text()
    m = re.search(r'\*\*Score: (.+?)\*\*', content)
    return m.group(1) if m else "?"


def get_quota_status() -> dict:
    """Read quota state from quota-state.json (written by credential proxy)."""
    quota_file = REPO_DIR / "quota-state.json"
    if not quota_file.exists():
        return {"available": True}
    try:
        data = json.loads(quota_file.read_text())
        headers = data.get("headers", {})
        # Parse reset timestamps
        reset_5h = headers.get("anthropic-ratelimit-unified-5h-reset", "")
        reset_7d = headers.get("anthropic-ratelimit-unified-7d-reset", "")
        if reset_5h:
            data["reset_5h"] = datetime.fromtimestamp(int(reset_5h)).strftime("%H:%M %b %d")
        if reset_7d:
            data["reset_7d"] = datetime.fromtimestamp(int(reset_7d)).strftime("%H:%M %b %d")
        return data
    except Exception:
        return {"available": True}


def get_system_stats() -> dict:
    import os
    st = os.statvfs("/")
    free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)

    result = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
    battery_m = re.search(r'(\d+)%', result.stdout)
    battery = f"{battery_m.group(1)}%" if battery_m else "?"
    charging = "charging" in result.stdout.lower() or "ac power" in result.stdout.lower()

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
</style></head><body>
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
<script>setInterval(()=>location.reload(),15000)</script>
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
    build_log = REPO_DIR / "build_log.md"
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


def render_dashboard() -> str:
    health = get_health()
    activity = get_activity(5)
    pending = get_pending_count()
    score = get_score()
    stats = get_system_stats()

    services_only = [c for c in health if "port" in c.get("detail", "") or "running" in c.get("detail", "") or c.get("name", "").startswith("com.sutando.")]
    ok_count = sum(1 for c in services_only if c.get("status") in ("ok", "warn"))
    total_count = len(services_only)

    # Score card
    cards = [f"""<div class="card">
<h2>Use Cases</h2>
<div class="score">{score}</div>
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
<div class="stat"><div class="stat-val">{"✓" if stats["quota"].get("available", True) else "✗"}</div><div class="stat-label">Quota</div></div>
<div class="stat"><div class="stat-val">{int(float(stats["quota"].get("utilization_5h", 0) or stats["quota"].get("headers", {}).get("anthropic-ratelimit-unified-5h-utilization", 0)) * 100)}%</div><div class="stat-label">5h Used<br><span style="font-size:9px;color:#444">↻ {stats["quota"].get("reset_5h", "?")}</span></div></div>
<div class="stat"><div class="stat-val">{int(float(stats["quota"].get("utilization_7d", 0) or stats["quota"].get("headers", {}).get("anthropic-ratelimit-unified-7d-utilization", 0)) * 100)}%</div><div class="stat-label">7d Used<br><span style="font-size:9px;color:#444">↻ {stats["quota"].get("reset_7d", "?")}</span></div></div>
</div></div>""")

    # Services (ports + daemons only)
    services = [c for c in health if "port" in c.get("detail", "") or "running" in c.get("detail", "") or c.get("name", "").startswith("com.sutando.")]
    services_html = ""
    for c in services:
        icon = '<span class="ok">✓</span>' if c.get("status") == "ok" else ('<span style="color:#f0ad4e">~</span>' if c.get("status") == "warn" else '<span class="bad">✗</span>')
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

    # Keyboard shortcuts
    sutando_running = subprocess.run(["pgrep", "-f", "src/Sutando/Sutando"], capture_output=True).returncode == 0
    shortcut_status = '<span class="ok">✓</span> Sutando app running' if sutando_running else '<span class="bad">✗</span> Sutando app not running'
    cards.append(f"""<div class="card">
<h2>Keyboard Shortcuts</h2>
<div class="check">{shortcut_status}</div>
<div style="margin-top:8px;font-size:12px;color:#555">
<div style="margin:4px 0"><kbd style="background:#222;color:#aaa;padding:2px 6px;border-radius:3px;font-family:monospace">⌃C</kbd> Context drop (text/image/file)</div>
<div style="margin:4px 0"><kbd style="background:#222;color:#aaa;padding:2px 6px;border-radius:3px;font-family:monospace">⌃V</kbd> Toggle voice</div>
<div style="margin:4px 0"><kbd style="background:#222;color:#aaa;padding:2px 6px;border-radius:3px;font-family:monospace">⌃M</kbd> Toggle mute</div>
</div></div>""")

    # Quick links
    cards.append(f"""<div class="card full">
<h2>Quick Links</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;font-size:12px">
<a href="http://localhost:8080" style="color:#4a8aaa;text-decoration:none">Voice UI :8080</a>
<a href="http://localhost:7843" style="color:#4a8aaa;text-decoration:none">Task API :7843</a>
<a href="http://localhost:7844" style="color:#4a8aaa;text-decoration:none">Dashboard :7844</a>
<a href="http://localhost:7845" style="color:#4a8aaa;text-decoration:none">Screen Capture :7845</a>
<a href="/notes-ui" style="color:#4a8aaa;text-decoration:none">Notes Browser</a>
<a href="https://github.com/sonichi/sutando" style="color:#4a8aaa;text-decoration:none">GitHub</a>
<a href="https://sutando.ai" style="color:#4a8aaa;text-decoration:none">Website</a>
<a href="https://discord.gg/uZHWXXmrCS" style="color:#4a8aaa;text-decoration:none">Discord</a>
</div></div>""")

    return HTML.replace("__CONTENT__", "\n".join(cards))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        if urlparse(self.path).path == "/":
            html = render_dashboard()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif urlparse(self.path).path == "/avatar":
            avatar_file = REPO_DIR / "docs" / "stand-avatar.png"
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
            si_file = REPO_DIR / "stand-identity.json"
            data = json.loads(si_file.read_text()) if si_file.exists() else {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
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
.note-content{background:#2a2a3e;padding:20px;border-radius:8px;white-space:pre-wrap;font-size:14px;line-height:1.6}
.back{display:inline-block;margin-bottom:15px;padding:5px 12px;background:#333;border-radius:4px}
.date{color:#888;font-size:12px;float:right}
</style></head><body>
<h1>Sutando Notes</h1>
<div id="app"><ul class="note-list" id="list"></ul></div>
<div id="viewer" style="display:none"><a href="#" class="back" onclick="showList();return false">&larr; Back</a><div class="note-content" id="content"></div></div>
<script>
async function load(){const r=await fetch('/notes');const notes=await r.json();const ul=document.getElementById('list');
ul.innerHTML=notes.map(n=>`<li><a href="#" onclick="showNote('${n.slug}');return false">${n.title}</a><span class="date">${new Date(n.modified*1000).toLocaleDateString()}</span></li>`).join('')}
async function showNote(slug){const r=await fetch('/notes/'+slug);const text=await r.text();document.getElementById('content').textContent=text;
document.getElementById('app').style.display='none';document.getElementById('viewer').style.display='block'}
function showList(){document.getElementById('app').style.display='block';document.getElementById('viewer').style.display='none'}
load()
</script></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif urlparse(self.path).path == "/notes":
            notes_dir = REPO_DIR / "notes"
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
            slug = urlparse(self.path).path.split("/notes/", 1)[1]
            # Sanitize slug — only allow alphanumeric, hyphens, underscores
            import re
            if not re.match(r'^[\w-]+$', slug):
                self.send_response(400)
                self.end_headers()
                return
            note_file = REPO_DIR / "notes" / f"{slug}.md"
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


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Sutando Dashboard → http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
