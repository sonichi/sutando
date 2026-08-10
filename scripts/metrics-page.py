#!/usr/bin/env python3
"""Sutando metrics page — a local server that pulls LIVE from the PostHog API
(server-side, key from the vault) and renders one clean single page. No PostHog UI.

Run:  python3 scripts/metrics-page.py            # serves http://localhost:8787
      PORT=9000 python3 scripts/metrics-page.py

Real-user filter: distinct_ids that did task_processed >=100x in 30d (the real
recurring cores) — excludes the ephemeral test-runner ids that inflate raw counts.
The PostHog project and key resolve at request time so importing this module for
tests needs neither a network nor the vault.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = os.environ.get("POSTHOG_PROJECT", "504955")
PORT = int(os.environ.get("PORT", "8787"))

# Real recurring users = a task_processed heavy-hitter over 30d. One definition,
# shared by every metric below.
REAL = ("distinct_id in (select distinct_id from events where event='task_processed' "
        "and timestamp > now() - interval 30 day group by distinct_id having count() >= 100)")

QUERIES = {
    "dau_today": "select count(distinct distinct_id) from events "
                 f"where toDate(timestamp)=today() and {REAL}",
    "wau": "select count(distinct distinct_id) from events "
           f"where timestamp > now() - interval 7 day and {REAL}",
    "mau": "select count(distinct distinct_id) from events "
           f"where timestamp > now() - interval 30 day and {REAL}",
    "tasks_30d": "select count() from events where event='task_processed' "
                 f"and timestamp > now() - interval 30 day and {REAL}",
    "dau_series": "select toDate(timestamp) as d, count(distinct distinct_id) as v from events "
                  f"where timestamp > now() - interval 30 day and {REAL} group by d order by d",
    "tasks_by_source": "select properties.source as k, count() as v from events "
                       f"where event='task_processed' and timestamp > now() - interval 30 day and {REAL} "
                       "group by k order by v desc",
    # explicit LIMIT — PostHog defaults to 100 rows, which (with order by d asc)
    # would silently drop the most recent days once days*sources exceeds 100.
    "daily_by_source": "select toDate(timestamp) as d, properties.source as k, count() as v from events "
                       f"where event='task_processed' and timestamp > now() - interval 30 day and {REAL} "
                       "group by d, k order by d limit 10000",
    "user_tasks_series": "select toDate(timestamp) as d, count() as v from events "
                         f"where event='task_processed' and timestamp > now() - interval 30 day and {REAL} "
                         "and properties.source != 'cron' group by d order by d",
    "feature_usage": "select properties.feature as k, count() as v from events "
                     f"where event='feature_used' and timestamp > now() - interval 30 day and {REAL} "
                     "group by k order by v desc limit 15",
}


def _api_key():
    """Resolve the PostHog personal key from the vault (repo-root relative)."""
    repo = next(p for p in Path(__file__).resolve().parents
                if (p / "src" / "vault_intercept.py").is_file())
    sys.path.insert(0, str(repo / "src"))
    from vault_intercept import get_vault_key
    return get_vault_key("POSTHOG_PERSONAL_APIKEY")


def hogql(query, key=None):
    """Run one HogQL query against PostHog, retrying transient errors."""
    key = key or _api_key()
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode()
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"https://us.posthog.com/api/projects/{PROJECT}/query/",
                data=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r).get("results", [])
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503, 504):
                time.sleep(2 + attempt * 2)
                continue
            raise
    raise last


def collect(runner=None):
    """Run every metric and return a name->results dict. `runner(query)` is
    injectable so tests never hit the network; one failed metric doesn't blank
    the page."""
    runner = runner or hogql
    out = {}
    for name, query in QUERIES.items():
        try:
            out[name] = runner(query)
        except Exception as e:
            out[name] = {"error": str(e)[:200]}
    out["_generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Sutando Metrics</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--fg:#e7e9ee;--mut:#8b93a3;--acc:#5b9dff;--grid:#242833}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
 header{padding:20px 24px;border-bottom:1px solid var(--grid);display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
 h1{font-size:18px;margin:0;font-weight:600}
 .sub{color:var(--mut);font-size:13px}
 .wrap{padding:24px;max-width:1100px;margin:0 auto}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:24px}
 .card{background:var(--card);border:1px solid var(--grid);border-radius:10px;padding:16px}
 .card .n{font-size:30px;font-weight:700}
 .card .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:4px}
 .panel{background:var(--card);border:1px solid var(--grid);border-radius:10px;padding:16px 18px;margin-bottom:18px}
 .panel h2{font-size:14px;margin:0 0 14px;font-weight:600;color:var(--fg)}
 .row{display:flex;align-items:center;gap:10px;margin:6px 0}
 .row .k{width:150px;color:var(--mut);font-size:13px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .row .barwrap{flex:1;background:#0c0e12;border-radius:5px;overflow:hidden;height:20px}
 .row .bar{height:100%;background:linear-gradient(90deg,var(--acc),#8f6bff);border-radius:5px}
 .row .v{width:64px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
 svg{width:100%;height:120px;display:block}
 svg.tall{height:220px}
 .err{color:#ff7a7a;font-size:13px}
 .refresh{color:var(--mut);font-size:12px}
 button{background:var(--card);color:var(--fg);border:1px solid var(--grid);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
 button:hover{border-color:var(--acc)}
 .row:hover .bar{filter:brightness(1.15)}
 rect.seg:hover{opacity:.82}
 #tip{position:fixed;pointer-events:none;background:#0b0d11;border:1px solid var(--grid);border-radius:8px;
   padding:8px 10px;font-size:12px;color:var(--fg);box-shadow:0 6px 24px rgba(0,0,0,.5);opacity:0;transition:opacity .08s;z-index:9;max-width:240px}
 #tip .t{color:var(--mut);margin-bottom:4px}
 #tip .lg{display:flex;align-items:center;gap:6px;margin:2px 0}
 #tip .sw{width:9px;height:9px;border-radius:2px;flex:none}
 .legend{display:flex;flex-wrap:wrap;gap:10px 16px;margin-top:12px}
 .legend span{display:flex;align-items:center;gap:6px;color:var(--mut);font-size:12px}
 .legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
</style></head><body>
<div id=tip></div>
<header>
 <h1>Sutando Metrics</h1>
 <span class=sub>real recurring users only — test traffic removed</span>
 <span style=flex:1></span>
 <span class=refresh id=gen>loading…</span>
 <button onclick=load()>Refresh</button>
</header>
<div class=wrap>
 <div class=cards id=cards></div>
 <div class=panel><h2>Daily task completions by source — 30d</h2><svg id=stacked class=tall></svg><div class=legend id=leg></div></div>
 <div class=panel><h2>Daily active users — 30d</h2><svg id=dau></svg></div>
 <div class=panel><h2>User tasks/day (excl cron) — 30d</h2><svg id=utasks></svg></div>
 <div class=panel><h2>Tasks by source — 30d</h2><div id=src></div></div>
 <div class=panel><h2>Feature usage — 30d</h2><div id=feat></div></div>
</div>
<script>
const num=n=>n==null?'–':(+n).toLocaleString()
const PAL=['#5b9dff','#8f6bff','#42c9a0','#ffb454','#ff6b8b','#c77dff','#4dd4ff','#a3e635','#f472b6','#fbbf24','#38bdf8','#f87171','#a78bfa','#34d399']
const COL={}
const tip=document.getElementById('tip')
function showTip(html,ev){tip.innerHTML=html;tip.style.opacity=1
  let x=ev.clientX+14,y=ev.clientY+14
  if(x+tip.offsetWidth>innerWidth)x=ev.clientX-tip.offsetWidth-14
  if(y+tip.offsetHeight>innerHeight)y=ev.clientY-tip.offsetHeight-14
  tip.style.left=x+'px';tip.style.top=y+'px'}
function hideTip(){tip.style.opacity=0}
function cell(rows,label){
  if(rows&&rows.error) return '<div class=err>'+rows.error+'</div>'
  if(!rows||!rows.length) return '<div class=err>no data</div>'
  const max=Math.max(...rows.map(r=>+r[1]||0))||1, tot=rows.reduce((s,r)=>s+(+r[1]||0),0)
  return rows.map(r=>{const v=+r[1]||0,pct=(100*v/tot).toFixed(1)
    return `<div class=row data-k="${r[0]??'—'}" data-v="${v}" data-p="${pct}" data-l="${label}">`
    +`<div class=k title="${r[0]??''}">${r[0]??'—'}</div>`
    +`<div class=barwrap><div class=bar style=width:${(100*v/max).toFixed(1)}%></div></div>`
    +`<div class=v>${num(v)}</div></div>`}).join('')
}
function wireRows(container){container.querySelectorAll('.row').forEach(el=>{
  el.onmousemove=e=>showTip(`<div class=t>${el.dataset.l}</div><b>${el.dataset.k}</b><br>${num(el.dataset.v)} (${el.dataset.p}% of shown)`,e)
  el.onmouseleave=hideTip})}
const SVGNS='http://www.w3.org/2000/svg'
function mk(tag,attrs){const e=document.createElementNS(SVGNS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e}
function spark(id,series,label){
  const el=document.getElementById(id)
  if(series&&series.error){el.outerHTML='<div class=err>'+series.error+'</div>';return}
  if(!series||!series.length){el.outerHTML='<div class=err>no data</div>';return}
  const W=1000,H=120,p=8,vals=series.map(r=>+r[1]||0),max=Math.max(...vals,1)
  const x=i=>p+i*(W-2*p)/Math.max(series.length-1,1), y=v=>H-p-(v/max)*(H-2*p)
  const pts=vals.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const area=`${p},${H-p} `+pts+` ${(W-p)},${H-p}`
  const ns=mk('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'none'})
  ns.appendChild(mk('polygon',{points:area,fill:'rgba(91,157,255,.15)'}))
  ns.appendChild(mk('polyline',{points:pts,fill:'none',stroke:'#5b9dff','stroke-width':2}))
  const cap=mk('text',{x:p,y:14,fill:'#8b93a3','font-size':13});cap.textContent=`peak ${num(max)} · latest ${num(vals[vals.length-1])}`;ns.appendChild(cap)
  vals.forEach((v,i)=>{const c=mk('circle',{cx:x(i).toFixed(1),cy:y(v).toFixed(1),r:12,fill:'transparent'})
    c.onmousemove=e=>showTip(`<div class=t>${series[i][0]}</div><b>${num(v)}</b> ${label}`,e);c.onmouseleave=hideTip;ns.appendChild(c)})
  el.replaceWith(ns);ns.id=id
}
function stacked(id,legId,rows){
  const el=document.getElementById(id),lg=document.getElementById(legId)
  if(rows&&rows.error){el.outerHTML='<div class=err>'+rows.error+'</div>';return}
  if(!rows||!rows.length){el.outerHTML='<div class=err>no data</div>';return}
  const days=[...new Set(rows.map(r=>r[0]))].sort()
  const srcs=[...new Set(rows.map(r=>r[1]))]
  srcs.forEach(s=>{if(!COL[s])COL[s]=PAL[Object.keys(COL).length%PAL.length]})
  const byDay={};days.forEach(d=>byDay[d]={total:0,parts:{}})
  rows.forEach(r=>{const o=byDay[r[0]];o.parts[r[1]]=(o.parts[r[1]]||0)+(+r[2]||0);o.total+=(+r[2]||0)})
  const max=Math.max(...days.map(d=>byDay[d].total),1)
  const W=1000,H=220,pl=44,pb=18,pt=10,pr=6
  const bw=(W-pl-pr)/days.length, y=v=>pt+(1-v/max)*(H-pt-pb)
  const ns=mk('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'none','class':'tall'})
  ;[0,.25,.5,.75,1].forEach(f=>{const v=Math.round(max*f),yy=y(v)
    ns.appendChild(mk('line',{x1:pl,y1:yy.toFixed(1),x2:W-pr,y2:yy.toFixed(1),stroke:'#242833','stroke-width':1}))
    const t=mk('text',{x:pl-6,y:(yy+4).toFixed(1),fill:'#8b93a3','font-size':11,'text-anchor':'end'});t.textContent=num(v);ns.appendChild(t)})
  days.forEach((d,i)=>{let yc=H-pb,o=byDay[d]
    const x=pl+i*bw+1,w=Math.max(bw-2,1)
    srcs.forEach(s=>{const v=o.parts[s]||0;if(!v)return;const h=(v/max)*(H-pt-pb);yc-=h
      ns.appendChild(mk('rect',{'class':'seg',x:x.toFixed(1),y:yc.toFixed(1),width:w.toFixed(1),height:h.toFixed(1),fill:COL[s]}))})
    const legendHtml=srcs.filter(s=>o.parts[s]).sort((a,b)=>o.parts[b]-o.parts[a])
       .map(s=>`<div class=lg><span class=sw style=background:${COL[s]}></span>${s}: ${num(o.parts[s])}</div>`).join('')
    const hit=mk('rect',{x:x.toFixed(1),y:pt,width:w.toFixed(1),height:(H-pt-pb).toFixed(1),fill:'transparent'})
    hit.onmousemove=e=>showTip(`<div class=t>${d} · total ${num(o.total)}</div>${legendHtml}`,e);hit.onmouseleave=hideTip
    ns.appendChild(hit)})
  el.replaceWith(ns);ns.id=id
  lg.innerHTML=srcs.map(s=>`<span><i style=background:${COL[s]}></i>${s}</span>`).join('')
}
async function load(){
  document.getElementById('gen').textContent='loading…'
  const d=await (await fetch('/api/metrics')).json()
  const one=r=>Array.isArray(r)&&r[0]?r[0][0]:(r&&r.error?r.error:'–')
  document.getElementById('cards').innerHTML=[
    ['DAU today',num(one(d.dau_today))],['WAU',num(one(d.wau))],
    ['MAU',num(one(d.mau))],['Tasks 30d',num(one(d.tasks_30d))]
  ].map(c=>`<div class=card><div class=n>${c[1]}</div><div class=l>${c[0]}</div></div>`).join('')
  stacked('stacked','leg',d.daily_by_source)
  spark('dau',d.dau_series,'active users');spark('utasks',d.user_tasks_series,'tasks')
  const src=document.getElementById('src');src.innerHTML=cell(d.tasks_by_source,'tasks 30d');wireRows(src)
  const feat=document.getElementById('feat');feat.innerHTML=cell(d.feature_usage,'uses 30d');wireRows(feat)
  document.getElementById('gen').textContent='updated '+(d._generated_at||'')
}
load();setInterval(load,60000)
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/metrics"):
            self._send(json.dumps(collect()).encode(), "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")


def main():
    print(f"Sutando metrics page → http://localhost:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
