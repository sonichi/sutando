// skills/mentra-channel/scripts/server.ts — the Sutando MentraOS app server.
// THIN wiring only: all decision logic lives in core.ts (pure, unit-tested);
// this file owns the SDK session lifecycle + HTTP. Runs where a public URL
// terminates (EC2 behind Caddy at https://chat.ag2.space/mentra — deploy is
// git-tracked; see DESIGN.md "Registration / deployment").
//
//   MentraOS Cloud ── POST /webhook/session-start {sessionId, userId}
//        └─ AppSession(packageName, apiKey) ── onTranscription(final)
//              └─ gateTranscript(wake) → buildTask → POST <broker>/v1/ingest
//                    └─ long-poll GET <broker>/v1/result/<id>
//                          ├─ session live → layouts.showTextWall + audio.speak
//                          └─ session gone → fallback room delivers (broker
//                             INTEGRATION_FALLBACK_ROOM_MENTRA — config only)
//
// Config (manifest `config` block; env overrides; see resolveConfig):
//   MENTRA_PACKAGE_NAME  registered app package (owner registered: "sutando")
//   MENTRA_API_KEY       from the vault (vault get MENTRAOS_API_KEY at launch)
//   MENTRA_WAKE_PHRASE   default "hey sutando"
//   MENTRA_BROKER_URL / MENTRA_BROKER_TOKEN / MENTRA_AGENT_ID  the lane
//   MENTRA_PORT          local listen port Caddy proxies to (default 8093)

import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gateTranscript, buildTask, resolveConfig } from './core.ts';

const HERE = dirname(fileURLToPath(import.meta.url));

function loadManifestConfig(): Record<string, string> {
  try {
    return JSON.parse(readFileSync(join(HERE, '..', 'manifest.json'), 'utf8')).config ?? {};
  } catch {
    return {};
  }
}

const cfg = resolveConfig(process.env, loadManifestConfig());
const seqBySession = new Map<string, number>();

async function postIngest(task: object): Promise<boolean> {
  try {
    const r = await fetch(`${cfg.MENTRA_BROKER_URL.replace(/\/$/, '')}/v1/ingest`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${cfg.MENTRA_BROKER_TOKEN}`,
        'Content-Type': 'application/json',
        'User-Agent': 'sutando-mentra-app/1.0',
      },
      body: JSON.stringify({ agent_id: cfg.MENTRA_AGENT_ID, task }),
    });
    return r.ok;
  } catch (e) {
    console.error('[mentra-app] ingest POST failed:', e);
    return false;
  }
}

async function pollResult(taskId: string, timeoutS = 120): Promise<string | null> {
  const deadline = Date.now() + timeoutS * 1000;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(
        `${cfg.MENTRA_BROKER_URL.replace(/\/$/, '')}/v1/result/${encodeURIComponent(taskId)}?wait=25`,
        { headers: { Authorization: `Bearer ${cfg.MENTRA_BROKER_TOKEN}`, 'User-Agent': 'sutando-mentra-app/1.0' } },
      );
      if (r.ok) {
        const body = (await r.json()) as { body?: string };
        if (body?.body) return body.body;
      }
    } catch { /* transient — keep polling until deadline */ }
  }
  return null;
}

// SDK import is dynamic + lazy so `--check` config validation and the pure
// tests never require the dependency; the server refuses to start without it.
async function startSession(sessionId: string, userId: string): Promise<void> {
  const sdk = await import('@mentraos/sdk');
  const session = new sdk.AppSession({
    packageName: cfg.MENTRA_PACKAGE_NAME,
    apiKey: cfg.MENTRA_API_KEY,
    cloudUrl: 'wss://cloud.mentraos.com/app-ws',
    sessionId,
    userId,
  });
  session.events.onTranscription(async (t: { text: string; isFinal?: boolean }) => {
    if (t.isFinal === false) return;                       // finals only (v1)
    const gated = gateTranscript(t.text, cfg.MENTRA_WAKE_PHRASE);
    if (!gated.text) return;
    const seq = (seqBySession.get(sessionId) ?? 0) + 1;
    seqBySession.set(sessionId, seq);
    const task = buildTask(gated.text, userId, sessionId, seq);
    session.layouts.showTextWall('Sutando: on it…');
    if (!(await postIngest(task))) {
      session.layouts.showTextWall('Sutando: could not reach the broker — try again.');
      return;
    }
    const result = await pollResult(task.id);
    if (result) {
      session.layouts.showTextWall(result.slice(0, 400));
      try { await session.audio.speak(result.slice(0, 280)); } catch { /* display-only glasses */ }
    }
    // No result before timeout: the broker's fallback room delivers it —
    // stay quiet rather than showing a false failure.
  });
  console.log(`[mentra-app] session ${sessionId} up for ${userId}`);
}

export function requiredConfigMissing(): string[] {
  return ['MENTRA_PACKAGE_NAME', 'MENTRA_API_KEY', 'MENTRA_BROKER_URL',
          'MENTRA_BROKER_TOKEN'].filter((k) => !cfg[k]);
}

if (process.argv.includes('--check')) {
  const missing = requiredConfigMissing();
  console.log(missing.length ? `not configured: ${missing.join(', ')}` : 'config ok');
  process.exit(missing.length ? 2 : 0);
}

const missing = requiredConfigMissing();
if (missing.length) {
  console.error(`[mentra-app] not configured (${missing.join(', ')}) — see manifest.json`);
  process.exit(2);
}

createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost');
  // Caddy strips nothing: we serve under /mentra/* as registered.
  if (req.method === 'POST' && url.pathname.endsWith('/webhook/session-start')) {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      try {
        const { sessionId, userId } = JSON.parse(raw || '{}');
        if (!sessionId || !userId) { res.writeHead(400).end('{"error":"missing ids"}'); return; }
        void startSession(String(sessionId), String(userId));
        res.writeHead(200, { 'Content-Type': 'application/json' }).end('{"ok":true}');
      } catch {
        res.writeHead(400).end('{"error":"bad json"}');
      }
    });
    return;
  }
  if (url.pathname.endsWith('/webview')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sutando</title><body style="font-family:system-ui;padding:1rem">
<h3>Sutando on Mentra</h3><p>Sessions this boot: ${seqBySession.size}</p>
<p>Say “${cfg.MENTRA_WAKE_PHRASE} …” and the reply lands on your glasses;
if you take them off first, it lands in your Mentra room on ag2.space.</p>`);
    return;
  }
  res.writeHead(200, { 'Content-Type': 'application/json' }).end('{"ok":true,"app":"sutando-mentra"}');
}).listen(Number(cfg.MENTRA_PORT || 8093), () => {
  console.log(`[mentra-app] listening on :${cfg.MENTRA_PORT || 8093} (wake: "${cfg.MENTRA_WAKE_PHRASE}")`);
});
