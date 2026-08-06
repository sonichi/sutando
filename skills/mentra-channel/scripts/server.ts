// skills/mentra-channel/scripts/server.ts — the Sutando MentraOS app server.
// THIN wiring: all decision logic lives in core.ts (pure, unit-tested); this
// file owns the SDK AppServer lifecycle + broker HTTP. Deploys on the EKS
// cluster (Deployment/Service + ingress for https://chat.ag2.space/mentra —
// git-tracked manifests; see DESIGN.md "Registration / deployment").
//
//   MentraOS Cloud ── /webhook (owned by the SDK AppServer) ── onSession()
//        └─ session.events.onTranscription(final)
//             └─ gateTranscript(wake) → buildTask → POST <broker>/v1/ingest
//                   └─ delivery per core.deliveryMode (ONE owner):
//                        room    → broker fallback room; glasses ack only
//                        glasses → poll GET /v1/result/<id> → showTextWall
//
// Config (manifest `config` block; env overrides; see core.resolveConfig):
//   MENTRA_PACKAGE_NAME / MENTRA_API_KEY   console registration (key: vault)
//   MENTRA_WAKE_PHRASE                     default "hey sutando"
//   MENTRA_BROKER_URL / MENTRA_BROKER_TOKEN / MENTRA_AGENT_ID   the lane
//   MENTRA_DELIVERY                        room (default) | glasses
//   MENTRA_PORT                            AppServer listen port

import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
// @mentra/sdk ships CommonJS: named ESM imports type-check but fail at
// runtime ('does not provide an export named'). Default-import the CJS
// namespace for VALUES; types import separately (erased at runtime).
import mentraSdk from '@mentra/sdk';
import type { AppSession } from '@mentra/sdk';

const { AppServer } = mentraSdk;
import {
  ackText, buildTask, deliveryMode, gateTranscript, resolveConfig,
  shouldPollResults,
} from './core.js';

const HERE = dirname(fileURLToPath(import.meta.url));

function loadManifestConfig(): Record<string, string> {
  try {
    return JSON.parse(readFileSync(join(HERE, '..', 'manifest.json'), 'utf8')).config ?? {};
  } catch {
    return {};
  }
}

export function requiredConfigMissing(cfg: Record<string, string>): string[] {
  return ['MENTRA_PACKAGE_NAME', 'MENTRA_API_KEY', 'MENTRA_BROKER_URL',
          'MENTRA_BROKER_TOKEN'].filter((k) => !cfg[k]);
}

async function postIngest(cfg: Record<string, string>, task: object): Promise<boolean> {
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

async function pollResult(
  cfg: Record<string, string>, taskId: string, timeoutS = 120,
): Promise<string | null> {
  const deadline = Date.now() + timeoutS * 1000;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(
        `${cfg.MENTRA_BROKER_URL.replace(/\/$/, '')}/v1/result/${encodeURIComponent(taskId)}?wait=25`,
        { headers: { Authorization: `Bearer ${cfg.MENTRA_BROKER_TOKEN}`,
                     'User-Agent': 'sutando-mentra-app/1.0' } },
      );
      if (r.ok) {
        const body = (await r.json()) as { body?: string };
        if (body?.body) return body.body;
      }
    } catch { /* transient — keep polling until deadline */ }
  }
  return null;
}

/**
 * The SDK owns /webhook + session lifecycle; we own per-session behavior.
 * Exported (and construction side-effect free) so the test seam can import
 * and instantiate it without starting network listeners.
 */
export class SutandoMentraServer extends AppServer {
  private readonly cfg: Record<string, string>;

  private readonly bootNonce: string;

  private readonly seqBySession = new Map<string, number>();

  constructor(cfg: Record<string, string>) {
    super({
      packageName: cfg.MENTRA_PACKAGE_NAME,
      apiKey: cfg.MENTRA_API_KEY,
      port: Number(cfg.MENTRA_PORT || 8093),
    });
    this.cfg = cfg;
    // Restart-safe id discriminator (core.taskId): a re-minted (session, seq)
    // pair after a crash must never equal a pre-crash id, or broker enqueue
    // idempotency silently drops the task.
    this.bootNonce = Date.now().toString(36);
  }

  protected override async onSession(
    session: AppSession, sessionId: string, userId: string,
  ): Promise<void> {
    const mode = deliveryMode(this.cfg.MENTRA_DELIVERY);
    session.events.onTranscription(async (t: { text: string; isFinal: boolean }) => {
      if (!t.isFinal) return;                              // finals only (v1)
      const gated = gateTranscript(t.text, this.cfg.MENTRA_WAKE_PHRASE);
      if (!gated.text) return;
      const seq = (this.seqBySession.get(sessionId) ?? 0) + 1;
      this.seqBySession.set(sessionId, seq);
      const task = buildTask(gated.text, userId, sessionId, this.bootNonce, seq);
      if (!(await postIngest(this.cfg, task))) {
        session.layouts.showTextWall('Sutando: could not reach the broker — try again.');
        return;
      }
      session.layouts.showTextWall(ackText(mode));
      if (!shouldPollResults(mode)) return;                // room mode: broker owns delivery
      const result = await pollResult(this.cfg, task.id);
      if (result) {
        session.layouts.showTextWall(result.slice(0, 400));
      }
      // glasses mode + no result before timeout: recorded server-side
      // (GET /v1/result) — quiet here, no false failure on the HUD.
    });
    console.log(`[mentra-app] session ${sessionId} up for ${userId} (delivery=${mode})`);
  }
}

export function buildServer(
  env: Record<string, string | undefined> = process.env,
): SutandoMentraServer {
  const cfg = resolveConfig(env, loadManifestConfig());
  const missing = requiredConfigMissing(cfg);
  if (missing.length) {
    throw new Error(`mentra-channel not configured (${missing.join(', ')}) — see manifest.json`);
  }
  return new SutandoMentraServer(cfg);
}

// Run only when executed directly (node/tsx scripts/server.ts) — importing
// this module (tests, tooling) has no side effects.
const invokedDirectly = process.argv[1]
  && fileURLToPath(import.meta.url) === process.argv[1];
if (invokedDirectly) {
  try {
    void buildServer().start();
  } catch (e) {
    console.error(`[mentra-app] ${(e as Error).message}`);
    process.exit(2);
  }
}
