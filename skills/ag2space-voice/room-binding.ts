// Room binding for AG2 Space voice sessions: a room the client names becomes the
// session's origin only on the gateway bridge's membership verdict.

import { writeFileSync, readFileSync, mkdirSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { resolveWorkspace } from '../../src/workspace_default.js';
import type { VoiceSessionOrigin } from '../../src/task-bridge.js';
import { MATRIX_ROOM_ID_RE, parseSessionContextFrame, type VoiceSessionRoom } from './session-context.js';

/** The bridge tag: results are claimed by the gateway bridge as `.to-ag2space`. */
export const ORIGIN_CHANNEL = 'ag2space';

export const VOICE_ROOM_CHECK_DIR = join(resolveWorkspace(), 'state', 'voice-room-checks');
/** Mirrors VERDICT_TTL_S on the gateway bridge's side of the handoff. */
export const VOICE_ROOM_VERDICT_TTL_S = 60;
export const VOICE_ROOM_VERDICT_TIMEOUT_MS = 6000;

export interface VoiceRoomVerdict {
	room_id: string;
	verified: boolean;
	reason: string;
	checked_at: number;
	agent_joined?: boolean;
	owner_joined?: boolean;
}

export type VoiceRoomVerifier = (room: string) => Promise<VoiceRoomVerdict>;

/** Filesystem-safe key for a room id: readable prefix plus a hash so two ids
 *  that flatten alike never share a verdict file. */
export function voiceRoomCheckKey(room: string): string {
	const flat = room.replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 80);
	return `${flat}-${createHash('sha256').update(room).digest('hex').slice(0, 16)}`;
}

/** The verifier's answer for `room` while it is fresh, else null. A verdict
 *  naming another room (a key collision or a tampered file) is null too. */
export function readVoiceRoomVerdict(room: string, nowSec = Date.now() / 1000): VoiceRoomVerdict | null {
	try {
		const raw = JSON.parse(readFileSync(join(VOICE_ROOM_CHECK_DIR, `${voiceRoomCheckKey(room)}.verdict.json`), 'utf-8'));
		if (!raw || raw.room_id !== room || typeof raw.checked_at !== 'number') return null;
		if (nowSec - raw.checked_at > VOICE_ROOM_VERDICT_TTL_S || raw.checked_at - nowSec > 5) return null;
		return {
			room_id: room, verified: raw.verified === true, reason: typeof raw.reason === 'string' ? raw.reason : '',
			checked_at: raw.checked_at, agent_joined: raw.agent_joined === true, owner_joined: raw.owner_joined === true,
		};
	} catch {
		return null;
	}
}

const _unverified = (room: string, reason: string): VoiceRoomVerdict =>
	({ room_id: room, verified: false, reason, checked_at: Date.now() / 1000 });

/** Ask the gateway bridge whether owner and agent are joined in `room`. A fresh
 *  verdict on disk answers at once; silence until `timeoutMs` is a refusal. */
export async function requestVoiceRoomVerdict(room: string, opts: { timeoutMs?: number; pollMs?: number } = {}): Promise<VoiceRoomVerdict> {
	if (!MATRIX_ROOM_ID_RE.test(room)) return _unverified(room, 'not a matrix room id');
	const cached = readVoiceRoomVerdict(room);
	if (cached) return cached;
	const timeoutMs = opts.timeoutMs ?? VOICE_ROOM_VERDICT_TIMEOUT_MS;
	const pollMs = opts.pollMs ?? 100;
	try {
		mkdirSync(VOICE_ROOM_CHECK_DIR, { recursive: true });
		const key = voiceRoomCheckKey(room);
		const tmp = join(VOICE_ROOM_CHECK_DIR, `${key}.request.json.tmp`);
		writeFileSync(tmp, JSON.stringify({ room_id: room, requested_at: Date.now() / 1000 }));
		renameSync(tmp, join(VOICE_ROOM_CHECK_DIR, `${key}.request.json`));
	} catch (e) {
		return _unverified(room, `request not written: ${e instanceof Error ? e.message : String(e)}`);
	}
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		await new Promise(r => setTimeout(r, pollMs));
		const verdict = readVoiceRoomVerdict(room);
		if (verdict) return verdict;
	}
	return _unverified(room, 'no verdict from the gateway bridge');
}

let _voiceRoomVerifier: VoiceRoomVerifier = requestVoiceRoomVerdict;

/** Test seam: replace (or with null restore) the membership verifier. */
export function setVoiceRoomVerifier(fn: VoiceRoomVerifier | null): void {
	_voiceRoomVerifier = fn ?? requestVoiceRoomVerdict;
}

async function _verdict(room: string): Promise<VoiceRoomVerdict> {
	try {
		return await _voiceRoomVerifier(room);
	} catch (e) {
		return _unverified(room, `verifier failed: ${e instanceof Error ? e.message : String(e)}`);
	}
}

/** The body line under `task:` that tells the core how to answer a task
 *  delegated while docked in a room. The drain honours the marker it names. */
export function voiceRoomTaskGuidance(room: string): string {
	return `room_context: ${room} — post there only what its members are meant to read; for anything the owner asked for themselves start the result with [dm-only]`;
}

/** What voice hears under a result kept to the DM, so the model says "in your DM"
 *  rather than the docked room's "in this room". */
export const DM_ONLY_DELIVERY_NOTE = 'That result was for the owner alone: its written copy went to their DM, not the room this session is docked in. Tell them it is in their DM ("I sent it to your DM"), never "in this room".';

/** The origin the core carries for a verified room. `verify` re-asks the gateway
 *  bridge at delivery: membership can change while the task runs. */
export function roomOrigin(room: VoiceSessionRoom): VoiceSessionOrigin {
	const origin: VoiceSessionOrigin = {
		channel: ORIGIN_CHANNEL,
		target: room.id,
		headers: { channel_kind: 'room', source_room_id: room.id },
		contextLine: voiceRoomTaskGuidance(room.id),
		dmOnlyNote: DM_ONLY_DELIVERY_NOTE,
		verify: async () => {
			const verdict = await _verdict(room.id);
			if (!verdict.verified) console.log(`[SessionRoom] ${room.id} not verified at delivery: ${verdict.reason}`);
			return verdict.verified;
		},
	};
	if (room.name) origin.label = room.name;
	return origin;
}

/** Rebuild a room origin from a voice task's header (a task written before a restart). */
export function roomOriginFromTaskHeader(headerLines: string[]): VoiceSessionOrigin | null {
	const line = headerLines.find(l => l.startsWith('source_room_id:'));
	const room = line ? line.slice('source_room_id:'.length).trim() : '';
	return MATRIX_ROOM_ID_RE.test(room) ? roomOrigin({ id: room }) : null;
}

/** What a frame did to the binding: `entered` a room, `left` for the DM, or `none`. */
export type SessionRoomChange = 'none' | 'entered' | 'left';

export interface SessionRoomBinding {
	change: SessionRoomChange;
	room: VoiceSessionRoom | null;
	/** Set when the frame named a room the gateway bridge would not vouch for. */
	refused?: { id: string; reason: string };
}

/** The host's origin slot, as the plugin seam hands it over. */
export interface OriginPort {
	set(origin: VoiceSessionOrigin | null): void;
}

export interface RoomBinding {
	current(): VoiceSessionRoom | null;
	/** Apply an already-verified room (or null). Live frames go through `bind`. */
	apply(msg: Record<string, unknown> | null | undefined): { change: SessionRoomChange; room: VoiceSessionRoom | null } | undefined;
	bind(msg: Record<string, unknown> | null | undefined): Promise<SessionRoomBinding | undefined>;
	release(): void;
}

export function createRoomBinding(port: OriginPort): RoomBinding {
	let bound: VoiceSessionRoom | null = null;
	// The room released while another room's verdict is pending; change reporting still counts it as left.
	let released: VoiceSessionRoom | null = null;
	let seq = 0;

	const setBound = (room: VoiceSessionRoom | null): void => {
		bound = room;
		port.set(room ? roomOrigin(room) : null);
	};
	const applyVerified = (room: VoiceSessionRoom | null): { change: SessionRoomChange; room: VoiceSessionRoom | null } => {
		const prev = bound ?? released;
		setBound(room);
		released = null;
		if (room && room.id !== prev?.id) return { change: 'entered', room };
		if (!room && prev) return { change: 'left', room: null };
		return { change: 'none', room };
	};

	return {
		current: () => bound,
		apply(msg) {
			const room = parseSessionContextFrame(msg);
			return room === undefined ? undefined : applyVerified(room);
		},
		// A DM frame applies at once. A room frame applies only on a verified verdict; a
		// different bound room is released before the wait, and a newer frame supersedes this one.
		async bind(msg) {
			const room = parseSessionContextFrame(msg);
			if (room === undefined) return undefined;
			const mine = ++seq;
			if (!room) return applyVerified(null);
			if (bound && bound.id !== room.id) {
				released = bound;
				setBound(null);
			}
			const verdict = await _verdict(room.id);
			if (mine !== seq) return { change: 'none', room: bound };
			if (verdict.verified) return applyVerified(room);
			console.log(`[SessionRoom] refused ${room.id}: ${verdict.reason} — session stays on the DM`);
			return { ...applyVerified(null), refused: { id: room.id, reason: verdict.reason } };
		},
		release() {
			seq++;
			released = null;
			setBound(null);
		},
	};
}

/** The one system line the model hears when the docked room changes; null when nothing changed. */
export function sessionRoomNotice(change: SessionRoomChange, room: VoiceSessionRoom | null): string | null {
	if (change === 'entered' && room) {
		const label = room.name ? `"${room.name}"` : room.id;
		return `You are docked in room ${label}; what you delegate is answered there only when it is for the room's members, otherwise in the owner's DM; say where it went. No reply is needed.`;
	}
	if (change === 'left') {
		return 'You are back in your DM. Work you delegate answers there; say "in your DM" or "here", never "in this room". No reply is needed.';
	}
	return null;
}

/** The voice-context line for the docked room; none in the DM. */
export function roomContextLines(room: VoiceSessionRoom | null): string[] {
	if (!room) return [];
	const label = room.name ? `"${room.name}" (${room.id})` : room.id;
	return [`ROOM: You are docked in room ${label}. What you delegate is answered there only when it is for the room's members, otherwise in the owner's DM; say where it went.`];
}
