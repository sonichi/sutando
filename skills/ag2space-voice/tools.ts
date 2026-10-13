// AG2 Space voice plugin: binds the room the client announces to the session's origin
// and moves the client by voice. Loaded through the manifest seam; the host names none of this.

import type { ToolDefinition } from 'bodhi-realtime-agent';
import type { SkillSetupCtx, VoiceSurfaceContribution } from '../../src/skill-setup-runner.js';
import { SESSION_CONTEXT_TYPE, buildSessionContextAckFrame, parseSessionContextCapabilities } from './session-context.js';
import { NAVIGATION_PROMPT_RULE, failPendingNavigations, installVoiceNavigateClient, navigateUiAvailable, navigateUiTool, resolveUiNavigated } from './navigate.js';
import { createRoomBinding, roomContextLines, roomOriginFromTaskHeader, sessionRoomNotice, type RoomBinding } from './room-binding.js';
import { ROOM_READ_PROMPT_RULE, createMcpStdioClient, createReadRoomTool, findAg2SpaceMcpEntry, type McpClient } from './room-read.js';

export const tools: ToolDefinition[] = [];

let _binding: RoomBinding | null = null;
let _mcp: McpClient | null = null;

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
	if (typeof ctx?.onClientFrame !== 'function' || typeof ctx.clientAttached !== 'function' || typeof ctx.setVoiceSessionOrigin !== 'function') {
		console.warn('[ag2space-voice] host has no client-frame seam — room binding disabled');
		return;
	}
	const binding = createRoomBinding({ set: ctx.setVoiceSessionOrigin });
	_binding = binding;
	ctx.setVoiceTaskOriginResolver(roomOriginFromTaskHeader);
	// What the attached client can answer; a client that omits the list announces none.
	let capabilities: ReadonlySet<string> = new Set();
	installVoiceNavigateClient({
		attached: () => ctx.clientAttached(),
		supports: (capability) => capabilities.has(capability),
		send: (frame) => ctx.sendClientFrame(frame),
	});
	ctx.onClientFrame((frame) => {
		if (resolveUiNavigated(frame)) return true;
		if (frame.type !== SESSION_CONTEXT_TYPE) return false;
		const caps = parseSessionContextCapabilities(frame) ?? [];
		const same = caps.length === capabilities.size && caps.every(c => capabilities.has(c));
		capabilities = new Set(caps);
		if (!same) console.log(`${ts()} [SessionRoom] client capabilities: ${caps.length ? caps.join(', ') : 'none'}`);
		void handleSessionContextFrame(ctx, binding, frame);
		return true;
	});
	// Binding, capabilities and in-flight moves belong to the client that announced them.
	ctx.onClientDisconnected(() => {
		binding.release();
		capabilities = new Set();
		failPendingNavigations();
	});
}

/** read_room is declared only where the core has the ag2-space MCP registered; the voice agent starts the same server. */
function roomReadSurface(): { tools: ToolDefinition[]; promptRules: string[] } {
	const entry = findAg2SpaceMcpEntry();
	if (!entry) return { tools: [], promptRules: [] };
	if (!_mcp) {
		_mcp = createMcpStdioClient(entry, { log: (m) => console.log(`${ts()} ${m}`) });
		// Mint the token now so the first question does not wait for it.
		_mcp.callTool('room.list', { limit: 1 }).then(
			() => console.log(`${ts()} [RoomRead] ag2-space MCP ready`),
			(err) => console.warn(`${ts()} [RoomRead] ag2-space MCP warm-up failed: ${err instanceof Error ? err.message : err}`),
		);
	}
	const tool = createReadRoomTool({
		client: () => _mcp,
		currentRoom: () => _binding?.current() ?? null,
		log: (m) => console.log(`${ts()} ${m}`),
	});
	return { tools: [tool], promptRules: [ROOM_READ_PROMPT_RULE] };
}

export function voiceSurface(): VoiceSurfaceContribution {
	// navigate_ui is declared only where a client can answer it: the gateway channel is provisioned.
	const navigate = navigateUiAvailable();
	const roomRead = roomReadSurface();
	return {
		tools: [...(navigate ? [navigateUiTool] : []), ...roomRead.tools],
		promptRules: [...(navigate ? [NAVIGATION_PROMPT_RULE] : []), ...roomRead.promptRules],
		contextLines: () => roomContextLines(_binding?.current() ?? null),
		answeringTools: roomRead.tools.map((t) => t.name),
	};
}
