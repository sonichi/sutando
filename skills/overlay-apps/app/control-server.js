'use strict';

// Localhost HTTP control surface for the overlay app. Lets the Sutando web UI
// list overlays and open / close / show / hide / reconfigure them.
//
// Binds 127.0.0.1 only. Writes a discovery file so the web UI's proxy finds
// the port without hardcoding it.

const { createServer } = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');

const HOST = '127.0.0.1';
const DEFAULT_PORT = 7849;
const PORT_RANGE = 20;

function resolveWorkspace() {
  const env = process.env.SUTANDO_WORKSPACE;
  if (env) {
    return path.resolve(env.replace(/^~(?=$|\/)/, os.homedir()));
  }
  return path.join(os.homedir(), '.sutando', 'workspace');
}

const DISCOVERY_PATH = path.join(
  resolveWorkspace(),
  'state',
  'overlay-control.json'
);

let server = null;

function send(res, payload, code = 200) {
  const data = JSON.stringify(payload);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(data),
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (c) => {
      raw += c;
      if (raw.length > 64 * 1024) req.destroy(); // guard
    });
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        resolve({});
      }
    });
    req.on('error', () => resolve({}));
  });
}

function writeDiscovery(port) {
  try {
    fs.mkdirSync(path.dirname(DISCOVERY_PATH), { recursive: true });
    const tmp = DISCOVERY_PATH + '.tmp';
    fs.writeFileSync(
      tmp,
      JSON.stringify({
        host: HOST,
        port,
        pid: process.pid,
        url: `http://${HOST}:${port}`,
        started_at: Date.now() / 1000,
      })
    );
    fs.renameSync(tmp, DISCOVERY_PATH);
  } catch (err) {
    console.error('[control-server] discovery write failed:', err.message);
  }
}

function removeDiscovery() {
  try {
    fs.unlinkSync(DISCOVERY_PATH);
  } catch {
    /* already gone */
  }
}

// manager: { list, open, close, show, hide, setConfig }
function startControlServer(manager) {
  server = createServer(async (req, res) => {
    if (req.method === 'OPTIONS') {
      send(res, {});
      return;
    }

    const url = new URL(req.url, `http://${HOST}`);
    const parts = url.pathname.split('/').filter(Boolean);

    try {
      // GET /overlays  | GET /health
      if (req.method === 'GET' && parts[0] === 'overlays' && !parts[1]) {
        send(res, { overlays: manager.list() });
        return;
      }
      if (req.method === 'GET' && parts[0] === 'health') {
        send(res, { ok: true, overlays: manager.list().length });
        return;
      }
      // GET /displays — connected monitors
      if (req.method === 'GET' && parts[0] === 'displays') {
        send(res, { displays: manager.displays() });
        return;
      }

      // POST /overlays/display — move all overlays to display {index}
      if (
        req.method === 'POST' &&
        parts[0] === 'overlays' &&
        parts[1] === 'display' &&
        !parts[2]
      ) {
        const body = await readBody(req);
        const result = manager.moveToDisplay(Number(body.index));
        send(res, result, result.ok ? 200 : 400);
        return;
      }

      // POST /overlays/:id/:action
      if (req.method === 'POST' && parts[0] === 'overlays' && parts[1]) {
        const id = parts[1];
        const action = parts[2];
        const known = manager.list().some((o) => o.id === id);
        if (!known) {
          send(res, { ok: false, error: `unknown overlay: ${id}` }, 404);
          return;
        }
        if (action === 'open') manager.open(id);
        else if (action === 'close') manager.close(id);
        else if (action === 'show') manager.show(id);
        else if (action === 'hide') manager.hide(id);
        else if (action === 'config') {
          const body = await readBody(req);
          manager.setConfig(id, body);
        } else {
          send(res, { ok: false, error: `unknown action: ${action}` }, 400);
          return;
        }
        // Echo the fresh state of the affected overlay.
        const state = manager.list().find((o) => o.id === id);
        send(res, { ok: true, overlay: state });
        return;
      }

      send(res, { ok: false, error: 'not found' }, 404);
    } catch (err) {
      send(res, { ok: false, error: err.message }, 500);
    }
  });

  // server.listen reports EADDRINUSE asynchronously via the 'error' event,
  // so port-scan by retrying on error rather than try/catch.
  let port = DEFAULT_PORT;
  server.on('error', (err) => {
    if (err.code === 'EADDRINUSE' && port < DEFAULT_PORT + PORT_RANGE - 1) {
      port += 1;
      setTimeout(() => server.listen(port, HOST), 0);
    } else {
      console.error('[control-server] listen failed:', err.message);
    }
  });
  server.on('listening', () => {
    const p = server.address().port;
    writeDiscovery(p);
    console.log(`[control-server] listening on http://${HOST}:${p}`);
  });
  server.listen(port, HOST);
}

function stopControlServer() {
  removeDiscovery();
  if (server) {
    server.close();
    server = null;
  }
}

module.exports = { startControlServer, stopControlServer };
