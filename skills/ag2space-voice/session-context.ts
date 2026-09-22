// The `session.context` v1 wire contract with the AG2 Space client: the room the
// session is docked in, what the client can answer, and the ack it is sent back.

export const SESSION_CONTEXT_TYPE = 'session.context';
export const SESSION_CONTEXT_ACK_TYPE = 'session.context.ack';

/** Matrix room id as the gateway's `_proactive_route` accepts it. */
export const MATRIX_ROOM_ID_RE = /^![^\s:]+:\S+$/;
/** Display names are prose from the client; capped before they reach a prompt. */
export const ROOM_NAME_MAX_CHARS = 120;
export const SESSION_CONTEXT_CAPABILITY_MAX_CHARS = 64;
export const SESSION_CONTEXT_MAX_CAPABILITIES = 32;

export interface VoiceSessionRoom { id: string; name?: string }

/** The room a frame binds, null for a DM/absent/malformed room, undefined when
 *  `msg` is not a session.context frame. */
export function parseSessionContextFrame(msg: Record<string, unknown> | null | undefined): VoiceSessionRoom | null | undefined {
	if (!msg || msg.type !== SESSION_CONTEXT_TYPE) return undefined;
	const id = typeof msg.room_id === 'string' ? msg.room_id.trim() : '';
	if (!id || !MATRIX_ROOM_ID_RE.test(id)) return null;
	const rawName = typeof msg.room_name === 'string' ? msg.room_name.replace(/[\r\n]+/g, ' ').trim() : '';
	const name = rawName.slice(0, ROOM_NAME_MAX_CHARS);
	return name ? { id, name } : { id };
}

/** Capabilities a frame announces: trimmed, deduplicated, bounded; `[]` when absent
 *  or malformed; undefined when `msg` is not a session.context frame. */
export function parseSessionContextCapabilities(msg: unknown): string[] | undefined {
	const m = msg as Record<string, unknown> | null | undefined;
	if (!m || typeof m !== 'object' || m.type !== SESSION_CONTEXT_TYPE) return undefined;
	if (!Array.isArray(m.capabilities)) return [];
	const out: string[] = [];
	for (const c of m.capabilities) {
		if (typeof c !== 'string') continue;
		const name = c.replace(/[\r\n]+/g, ' ').trim().slice(0, SESSION_CONTEXT_CAPABILITY_MAX_CHARS);
		if (name && !out.includes(name)) out.push(name);
		if (out.length >= SESSION_CONTEXT_MAX_CAPABILITIES) break;
	}
	return out;
}

export interface SessionContextAckFrame {
	type: typeof SESSION_CONTEXT_ACK_TYPE;
	version: 1;
	/** The room the frame named; null for a DM frame. */
	room_id: string | null;
	/** True when tasks and results now follow `room_id`. */
	bound: boolean;
	surface: 'dm' | 'room' | 'refused';
	reason?: string;
}

export function buildSessionContextAckFrame(roomId: string | null, bound: boolean, reason?: string): SessionContextAckFrame {
	const frame: SessionContextAckFrame = {
		type: SESSION_CONTEXT_ACK_TYPE,
		version: 1,
		room_id: roomId,
		bound: bound && !!roomId,
		surface: !roomId ? 'dm' : bound ? 'room' : 'refused',
	};
	if (reason) frame.reason = reason;
	return frame;
}
