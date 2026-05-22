/**
 * Overlay Manager view for the Sutando web UI.
 *
 * A standalone page served at `/overlays` by web-client.ts. It lists the
 * overlay applications exposed by the benchmark-overlay Electron app and
 * provides open / close / show / hide / reconfigure controls.
 *
 * The page talks to same-origin `/api/overlays/*`, which web-client.ts proxies
 * to the overlay app's control server (located via its discovery file). This
 * keeps the browser free of CORS and port-discovery concerns.
 */

export const OVERLAY_MANAGER_HTML = /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sutando — Overlay Manager</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a12; color: #c0c0d0; min-height: 100vh; padding: 24px;
  }
  h1 { font-size: 18px; font-weight: 600; color: #e9ecf1; margin-bottom: 4px; }
  .sub { font-size: 12px; color: #6b7280; margin-bottom: 20px; }
  #status { font-size: 12px; margin-bottom: 16px; min-height: 16px; }
  .err { color: #ff7a7a; }
  .ok { color: #4ecca3; }
  .card {
    background: #0e0e18; border: 1px solid #1a1a2e; border-radius: 12px;
    padding: 16px; margin-bottom: 14px; max-width: 560px;
  }
  .card-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px;
  }
  .card-name { font-size: 14px; font-weight: 600; color: #e9ecf1; }
  .badge {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
    padding: 3px 8px; border-radius: 999px;
  }
  .badge.open { background: rgba(78,204,163,0.16); color: #4ecca3; }
  .badge.closed { background: rgba(255,255,255,0.06); color: #6b7280; }
  .badge.hidden { background: rgba(251,191,36,0.16); color: #fbbf24; }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
  button {
    font-family: inherit; font-size: 12px; padding: 6px 12px;
    border-radius: 7px; border: 1px solid #2a2a40; background: #15152a;
    color: #c0c0d0; cursor: pointer; transition: background .12s, opacity .12s;
  }
  button:hover { background: #1f1f3a; }
  button:disabled { opacity: 0.35; cursor: not-allowed; }
  .config { border-top: 1px solid #1a1a2e; padding-top: 12px; }
  .config-row {
    display: flex; align-items: center; gap: 10px;
    font-size: 12px; margin-top: 8px;
  }
  .config-row label { width: 110px; color: #8b93a3; }
  input[type=range] { flex: 1; accent-color: #4ecca3; }
  .val { width: 42px; text-align: right; font-variant-numeric: tabular-nums; }
  input[type=checkbox] { accent-color: #4ecca3; width: 15px; height: 15px; }
  .refresh {
    font-size: 11px; color: #6b7280; background: none; border: none;
    cursor: pointer; padding: 0; margin-bottom: 16px;
  }
  .refresh:hover { color: #c0c0d0; }
</style>
</head>
<body>
  <h1>Overlay Manager</h1>
  <div class="sub">Control the desktop overlay applications</div>
  <button class="refresh" id="refresh">&#x21bb; refresh</button>
  <div id="status"></div>
  <div id="list"></div>

<script>
const listEl = document.getElementById('list');
const statusEl = document.getElementById('status');

function setStatus(msg, cls) {
  statusEl.textContent = msg || '';
  statusEl.className = cls || '';
}

async function api(path, opts) {
  const res = await fetch('/api/overlays' + path, opts);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

async function act(id, action, body) {
  setStatus('…', '');
  try {
    await api('/' + id + '/' + action, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    setStatus(id + ': ' + action + ' ok', 'ok');
    await load();
  } catch (e) {
    setStatus('Failed: ' + e.message, 'err');
  }
}

function card(o) {
  const state = !o.open ? 'closed' : (o.visible ? 'open' : 'hidden');
  const label = !o.open ? 'closed' : (o.visible ? 'open' : 'hidden');
  const opacityPct = Math.round((o.config.opacity ?? 1) * 100);
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML =
    '<div class="card-head">' +
      '<span class="card-name"></span>' +
      '<span class="badge ' + state + '">' + label + '</span>' +
    '</div>' +
    '<div class="controls">' +
      '<button data-a="open">Open</button>' +
      '<button data-a="close">Close</button>' +
      '<button data-a="show">Show</button>' +
      '<button data-a="hide">Hide</button>' +
    '</div>' +
    '<div class="config">' +
      '<div class="config-row">' +
        '<label>Opacity</label>' +
        '<input type="range" min="20" max="100" value="' + opacityPct + '" data-cfg="opacity">' +
        '<span class="val">' + opacityPct + '%</span>' +
      '</div>' +
      '<div class="config-row">' +
        '<label>Always on top</label>' +
        '<input type="checkbox" data-cfg="alwaysOnTop"' +
          (o.config.alwaysOnTop ? ' checked' : '') + '>' +
      '</div>' +
    '</div>';
  el.querySelector('.card-name').textContent = o.name;

  el.querySelectorAll('.controls button').forEach((b) => {
    const a = b.dataset.a;
    if (a === 'open') b.disabled = o.open;
    if (a === 'close') b.disabled = !o.open;
    if (a === 'show') b.disabled = !o.open || o.visible;
    if (a === 'hide') b.disabled = !o.open || !o.visible;
    b.addEventListener('click', () => act(o.id, a));
  });

  const range = el.querySelector('[data-cfg=opacity]');
  range.addEventListener('input', () => {
    el.querySelector('.val').textContent = range.value + '%';
  });
  range.addEventListener('change', () => {
    act(o.id, 'config', { opacity: Number(range.value) / 100 });
  });
  el.querySelector('[data-cfg=alwaysOnTop]').addEventListener('change', (e) => {
    act(o.id, 'config', { alwaysOnTop: e.target.checked });
  });
  return el;
}

async function load() {
  try {
    const data = await api('', {});
    listEl.innerHTML = '';
    (data.overlays || []).forEach((o) => listEl.appendChild(card(o)));
    if (!data.overlays || !data.overlays.length) {
      listEl.innerHTML = '<div class="sub">No overlays reported.</div>';
    }
  } catch (e) {
    listEl.innerHTML = '';
    setStatus('Overlay app not reachable — is benchmark-overlay running? (' + e.message + ')', 'err');
  }
}

document.getElementById('refresh').addEventListener('click', load);
load();
setInterval(load, 5000);
</script>
</body>
</html>`;
