'use strict';

const contentEl = document.getElementById('content');
const footerEl = document.getElementById('footer');

document.getElementById('close').addEventListener('click', () => window.overlay.close());
document.getElementById('hide').addEventListener('click', () => window.overlay.hide());

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function gb(bytes) {
  return (bytes / 1e9).toFixed(1);
}

function rate(bytesPerSec) {
  if (bytesPerSec >= 1e6) return (bytesPerSec / 1e6).toFixed(1) + ' MB/s';
  return (bytesPerSec / 1e3).toFixed(0) + ' KB/s';
}

function uptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

// A labelled metric with a proportional fill bar.
function meter(label, value, pct, hot) {
  const p = Math.max(1, Math.min(100, pct));
  return `
    <div class="meter ${hot ? 'hot' : ''}">
      <div class="meter-fill" style="width:${p}%"></div>
      <span class="meter-label">${escapeHtml(label)}</span>
      <span class="meter-value">${escapeHtml(value)}</span>
    </div>`;
}

function render(s) {
  const memPct = (s.memory.used / s.memory.total) * 100;
  const diskPct = s.disk ? (s.disk.used / s.disk.total) * 100 : 0;

  const blocks = [];

  blocks.push(`<div class="stat-group">`);
  blocks.push(meter(`CPU · ${s.cpu.cores} cores`, `${s.cpu.percent}%`, s.cpu.percent, s.cpu.percent > 85));
  blocks.push(meter('Memory', `${gb(s.memory.used)} / ${gb(s.memory.total)} GB`, memPct, memPct > 90));
  if (s.disk) {
    blocks.push(meter('Disk /', `${gb(s.disk.used)} / ${gb(s.disk.total)} GB`, diskPct, diskPct > 90));
  }
  blocks.push(`</div>`);

  blocks.push(`
    <div class="kv-row">
      <div class="kv"><span>Network</span><b>&#x2193; ${rate(s.network.rxRate)} &nbsp; &#x2191; ${rate(s.network.txRate)}</b></div>
      <div class="kv"><span>Load avg</span><b>${s.load.map((x) => x.toFixed(2)).join('  ')}</b></div>
      <div class="kv"><span>Uptime</span><b>${uptime(s.uptime)}</b></div>
    </div>`);

  contentEl.innerHTML = blocks.join('');
  footerEl.textContent = 'Updated ' + new Date(s.ts).toLocaleTimeString();
}

async function tick() {
  const res = await window.overlay.getStats();
  if (res.ok) render(res.data);
  else contentEl.innerHTML = `<div class="error">${escapeHtml(res.error)}</div>`;
}

tick();
setInterval(tick, 2500);
