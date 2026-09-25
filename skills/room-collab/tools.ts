// Room-collab voice tools: present an AG2 Space room's HTML page for everyone in the room.
// They call the local stage relay (`room_collab.py --kind html relay <room>`), which holds the page open.

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

// Read at call time: manifest config lands in process.env after imports are hoisted.
const relayUrl = () => (process.env.ROOM_COLLAB_RELAY_URL || 'http://127.0.0.1:7877').replace(/\/$/, '');
const START_HINT =
	'The room relay is not running. Ask the core to start it: ' +
	"python3 skills/room-collab/scripts/room_collab.py --kind html relay '<room id>'";

async function relay(method: 'GET' | 'POST', path: string): Promise<Record<string, unknown>> {
	let res: Response;
	try {
		res = await fetch(`${relayUrl()}${path}`, { method, signal: AbortSignal.timeout(2_000) });
	} catch {
		return { error: START_HINT };
	}
	const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
	return res.ok ? body : { error: String(body.error ?? `relay answered ${res.status}`) };
}

export const roomSlideTool: ToolDefinition = {
	name: 'room_slide',
	description:
		'Move the slide deck shown in the AG2 Space room for EVERYONE watching: next, previous, or go to a slide number. ' +
		'Use while presenting a deck that is open as the room\'s HTML page (the room relay is running). ' +
		'For a deck open only in this computer\'s browser, use slide_control instead. Instant.',
	parameters: z.object({
		action: z.enum(['next', 'previous', 'goto']).describe('Which way to move'),
		slideNumber: z.number().int().min(1).max(999).optional().describe('1-based slide number, for goto'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, slideNumber } = args as { action: 'next' | 'previous' | 'goto'; slideNumber?: number };
		if (action === 'goto' && !slideNumber) return { error: 'goto needs slideNumber' };
		const move = action === 'goto' ? String(slideNumber) : action === 'next' ? 'next' : 'prev';
		return relay('POST', `/slide/${move}`);
	},
};

export const roomHighlightTool: ToolDefinition = {
	name: 'room_highlight',
	description:
		'Highlight a topic on the room\'s HTML page for everyone watching — a data-topic key the page defines ' +
		'(e.g. a card or step on a slide); decks usually jump to the slide that holds it. ' +
		'Pass "clear" to remove the highlight. Call it just before you talk about that topic. Instant.',
	parameters: z.object({
		topic: z.string().describe('A data-topic key from the page, or "clear"'),
	}),
	execution: 'inline',
	async execute(args) {
		const { topic } = args as { topic: string };
		if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(topic)) return { error: `not a topic key: ${topic}` };
		return relay('POST', `/highlight/${encodeURIComponent(topic.toLowerCase())}`);
	},
};

export const roomStageTool: ToolDefinition = {
	name: 'room_stage',
	description:
		'Read what the room\'s HTML page is showing to everyone: the highlighted topic and whether a presenter is speaking. ' +
		'Also tells you whether the room relay is running (an error means it is not). Instant.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		return relay('GET', '/state');
	},
};

export const tools: ToolDefinition[] = [roomSlideTool, roomHighlightTool, roomStageTool];
