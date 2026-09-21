// AG2 Space voice plugin: binds the room the desktop client announces to the
// session's origin. Loaded through the manifest seam; the host names none of this.

import type { ToolDefinition } from 'bodhi-realtime-agent';
import type { SkillSetupCtx, VoiceSurfaceContribution } from '../../src/skill-setup-runner.js';
import { SESSION_CONTEXT_TYPE, buildSessionContextAckFrame } from './session-context.js';
import { createRoomBinding, roomContextLines, roomOriginFromTaskHeader, sessionRoomNotice, type RoomBinding } from './room-binding.js';

export const tools: ToolDefinition[] = [];

let _binding: RoomBinding | null = null;

const ts = () => new Date().toISOString().slice(11, 19);

/** One `session.context` frame: bind on the verdict, ack the client, tell the model once per change. */
export async function handleSessionContextFrame(ctx: Pick<SkillSetupCtx, 'sendClientFrame' | 'injectContext'>, binding: RoomBinding, frame: Record<string, unknown>): Promise<void> {
	const applied = await binding.bind(frame);
	if (!applied) return;
	const { change, room, refused } = applied;
	console.log(`${ts()} [SessionRoom] session.context: ${room ? `${room.id}${room.name ? ` (${room.name})` : ''}` : 'DM'} — ${change}${refused ? ` (refused ${refused.id}: ${refused.reason})` : ''}`);
	const ack = refused
		? buildSessionContextAckFrame(refused.id, false, refused.reason)
		: buildSessionContextAckFrame(room?.id ?? null, !!room);
	ctx.sendClientFrame({ ...ack });
	const notice = sessionRoomNotice(change, room);
	if (notice) ctx.injectContext(notice);
}

export function setup(ctx: SkillSetupCtx): void {
	// A host that predates the client-frame seam gets no room binding rather than a throw.
	if (typeof ctx?.onClientFrame !== 'function' || typeof ctx.setVoiceSessionOrigin !== 'function') {
		console.warn('[ag2space-voice] host has no client-frame seam — room binding disabled');
		return;
	}
	const binding = createRoomBinding({ set: ctx.setVoiceSessionOrigin });
	_binding = binding;
	ctx.setVoiceTaskOriginResolver(roomOriginFromTaskHeader);
	ctx.onClientFrame((frame) => {
		if (frame.type !== SESSION_CONTEXT_TYPE) return false;
		void handleSessionContextFrame(ctx, binding, frame);
		return true;
	});
	// The binding belongs to the client that announced it; the next client announces its own.
	ctx.onClientDisconnected(() => binding.release());
}

export function voiceSurface(): VoiceSurfaceContribution {
	return { contextLines: () => roomContextLines(_binding?.current() ?? null) };
}
