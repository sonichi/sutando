// skills/mentra-channel/scripts/core.ts — pure logic for the Mentra lane.
// Design: skills/mentra-channel/DESIGN.md (PR #2707). This module has ZERO
// imports so the repo's tsx test runner exercises it without installing the
// MentraOS SDK; scripts/server.ts owns all SDK + network wiring and stays
// thin. The v1 trigger contract: only FINAL transcriptions that start with
// the wake phrase become tasks — raw transcription is a firehose and ambient
// mode is explicitly out of scope (see DESIGN.md).

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
    // consume the matched word from the ORIGINAL string (case-preserving tail)
    const idx = rest.toLowerCase().indexOf(w, rest.length - n.length);
    rest = rest.slice(idx + w.length);
  }
  const payload = rest.replace(/^[\s,.!?;:—-]+/, '').trim();
  return { text: payload || null };
}

// Sparrow's task-id contract ([A-Za-z0-9._-]{1,64}, _TID_RE in
// remote_gateway_bridge.py) — same rule the Teams and Bee adapters enforce.
const SAFE_RUN = /[^A-Za-z0-9._-]/g;

/**
 * Deterministic, in-alphabet, bounded task id: task-mentra-<session>-<seq>.
 * sessionId is sanitized (opaque MentraOS ids may carry any charset) and
 * truncated so the whole id stays under 64 chars even with a large seq.
 */
export function safeTaskId(sessionId: string, seq: number): string {
  const sess = (sessionId || 'nosession').replace(SAFE_RUN, '_').slice(0, 32);
  const id = `task-mentra-${sess}-${seq}`;
  return id.length <= 64 ? id : `task-mentra-${sess.slice(0, 64 - 13 - String(seq).length - 1)}-${seq}`;
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
  gatedText: string, userId: string, sessionId: string, seq: number,
): MentraTask {
  return {
    id: safeTaskId(sessionId, seq),
    task: gatedText,
    source: 'mentra',
    user_id: userId || 'mentra-user',
    sender_name: 'Mentra',
    channel_id: sessionId,
    room_name: 'Mentra',
    interaction_type: 'message',
  };
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
