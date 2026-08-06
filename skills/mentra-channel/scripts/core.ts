// skills/mentra-channel/scripts/core.ts — pure logic for the Mentra lane.
// Design: skills/mentra-channel/DESIGN.md (this PR). SDK-free (node:crypto
// only) so the repo's tsx suite exercises every decision here without the
// MentraOS SDK; scripts/server.ts owns SDK + network wiring and stays thin.
// The v1 trigger contract: only FINAL transcriptions that start with the
// wake phrase become tasks — raw transcription is a firehose and ambient
// mode is explicitly out of scope (see DESIGN.md).

import { createHash } from 'node:crypto';

export interface GateResult {
  /** The task text with the wake phrase stripped, or null when gated out. */
  text: string | null;
}

/** Normalize for wake matching: lowercase, strip leading punctuation/space. */
function norm(s: string): string {
  return s.toLowerCase().replace(/^[\s,.!?;:]+/, '');
}

/**
 * Wake-phrase gate over a FINAL transcription. Case-insensitive; tolerates
 * punctuation between/after the phrase ("Hey, Sutando — do X"). Interim
 * (non-final) segments must be gated out by the caller before this runs.
 */
export function gateTranscript(text: string, wakePhrase: string): GateResult {
  const t = (text || '').trim();
  if (!t) return { text: null };
  const words = wakePhrase.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let rest = t;
  for (const w of words) {
    const n = norm(rest);
    if (!n.startsWith(w)) return { text: null };
    const idx = rest.toLowerCase().indexOf(w, rest.length - n.length);
    rest = rest.slice(idx + w.length);
  }
  const payload = rest.replace(/^[\s,.!?;:—-]+/, '').trim();
  return { text: payload || null };
}

// Sparrow's task-id contract ([A-Za-z0-9._-]{1,64}, _TID_RE in
// remote_gateway_bridge.py) — same rule the Teams and Bee adapters enforce.
//
// Review P1 (2026-08-06): sanitizing the raw session id collides distinct
// opaque ids ('sess:a/b' and 'sess:a:b' both slugged to sess_a_b), and an
// in-memory seq resets on restart — under broker enqueue idempotency both
// cases silently DROP tasks as duplicates. So: hash the ORIGINAL opaque
// session id (the Bee adapter's approach) and include a per-process boot
// nonce so a restarted server can never re-mint an old id. Ids stay
// deterministic per utterance instance (ingest retries reuse them) without
// being collidable.
export function sessionSlug(sessionId: string): string {
  return createHash('sha256').update(sessionId || 'nosession').digest('hex').slice(0, 16);
}

export function taskId(sessionId: string, bootNonce: string, seq: number): string {
  const nonce = (bootNonce || '0').replace(/[^A-Za-z0-9]/g, '').slice(0, 10);
  return `task-mentra-${sessionSlug(sessionId)}-${nonce}-${seq}`;
}

export interface MentraTask {
  id: string;
  task: string;
  source: 'mentra';
  user_id: string;
  sender_name: string;
  channel_id: string;
  room_name: 'Mentra';
  interaction_type: 'message';
}

/** Relay task shape for one gated utterance (same fields the other lanes emit). */
export function buildTask(
  gatedText: string, userId: string, sessionId: string,
  bootNonce: string, seq: number,
): MentraTask {
  return {
    id: taskId(sessionId, bootNonce, seq),
    task: gatedText,
    source: 'mentra',
    user_id: userId || 'mentra-user',
    sender_name: 'Mentra',
    channel_id: sessionId,
    room_name: 'Mentra',
    interaction_type: 'message',
  };
}

// Review P1 (2026-08-06): result delivery must have ONE owner. Backend#444
// sends every source=mentra result to INTEGRATION_FALLBACK_ROOM_MENTRA the
// moment that env is set broker-side (this app server is not a broker
// deliverer), so an app server that ALSO polls and renders would duplicate
// every live answer into Matrix. The owner is therefore explicit config:
//   room    (default) — the broker fallback room owns ALL replies; glasses
//           show an acknowledgement only. Safe with the room env set.
//   glasses — this app server owns replies (poll + render); the operator
//           MUST leave INTEGRATION_FALLBACK_ROOM_MENTRA unset broker-side.
//           Glasses-off before the poll window closes leaves the reply
//           recorded server-side (GET /v1/result serves it) but unrendered.
// A future broker claim/availability contract can make this dynamic; v1
// pins one owner per deployment and tests both paths at the server seam.
export type DeliveryMode = 'room' | 'glasses';

export function deliveryMode(raw: string | undefined): DeliveryMode {
  return raw === 'glasses' ? 'glasses' : 'room';
}

export function shouldPollResults(mode: DeliveryMode): boolean {
  return mode === 'glasses';
}

export function ackText(mode: DeliveryMode): string {
  return mode === 'glasses'
    ? 'Sutando: on it…'
    : 'Sutando: on it — reply lands in your Mentra room.';
}

/** Config resolution order (CLI handled by server argv): env > manifest. */
export function resolveConfig(
  env: Record<string, string | undefined>,
  manifest: Record<string, string>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const k of Object.keys(manifest)) {
    out[k] = (env[k] || '').trim() || manifest[k];
  }
  return out;
}
