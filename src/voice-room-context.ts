// In-room context layer — room association (US: in-room context, 2026-07-09;
// design of record: notes/voicestack-in-room-context-layer-2026-07-09.md).
//
// A voice call can originate from a room/channel in an embedding chat client.
// The client reports which room via web-client's `/room-context`
// route → this JSON state file. buildInstructions() (system prompt) reads it
// so the agent knows "I'm being called in room X"; search_knowledge + the
// prep-artifact fetch (slice 2) will read the same state to scope to the room.
//
// Mirrors the voice-state.json / mute-state pattern: the client POSTs, the
// agent reads a small JSON state file — no bodhi/transport changes needed.

import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

export interface VoiceSessionRoom {
	room_id: string;
	room_name?: string;
	prep?: string; // optional pre-fetched prep/context artifact body (slice 2)
	ts: number; // epoch SECONDS when set (matches voice-state.json convention)
}

export function roomStatePath(workspaceDir: string): string {
	return join(workspaceDir, 'state', 'voice-session-room.json');
}

// Max age for a room association to still apply. A stale file must NOT leak an
// old room's context into an unrelated later call — room_id is set right before
// connect, so 15 min is ample headroom without cross-session bleed.
const ROOM_TTL_MS = 15 * 60 * 1000;

export function readVoiceSessionRoom(workspaceDir: string, now: number = Date.now()): VoiceSessionRoom | null {
	const p = roomStatePath(workspaceDir);
	if (!existsSync(p)) return null;
	try {
		const raw = JSON.parse(readFileSync(p, 'utf-8')) as Partial<VoiceSessionRoom>;
		if (!raw || typeof raw.room_id !== 'string' || !raw.room_id) return null;
		const tsMs = typeof raw.ts === 'number' ? raw.ts * 1000 : 0;
		if (tsMs && now - tsMs > ROOM_TTL_MS) return null; // stale → treat as no active room
		return { room_id: raw.room_id, room_name: raw.room_name, prep: raw.prep, ts: raw.ts ?? 0 };
	} catch {
		return null;
	}
}

// The system-prompt block injected when a call originates in a room. Empty
// string when no (fresh) room is set — safe to always include in the prompt
// array (joins to a no-op blank line, same as the other conditional blocks).
export function roomContextBlock(workspaceDir: string, now: number = Date.now()): string {
	const room = readVoiceSessionRoom(workspaceDir, now);
	if (!room) return '';
	const label = room.room_name ? `"${room.room_name}" (${room.room_id})` : room.room_id;
	const lines = [
		`IN-ROOM CONTEXT: You are being called from room ${label}. Scope your focus to this room — its topic, artifacts, and events. When the user says "this", "here", "my notes", or "search", prefer this room's context first.`,
	];
	if (room.prep && room.prep.trim()) {
		lines.push(`Room prep/context:\n${room.prep.trim().slice(0, 1500)}`);
	}
	return lines.join('\n');
}
