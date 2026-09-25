// Room-collab voice tools: present an AG2 Space room's HTML page for everyone in the room.
// They call the local stage relay (`room_collab.py --kind html relay <room>`), which holds the page open.

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { anchorPaths, cuePath, lineInstruction, toBeats, type Beat, type ScriptItem } from './present.js';

// Read at call time: manifest config lands in process.env after imports are hoisted.
const relayUrl = () => (process.env.ROOM_COLLAB_RELAY_URL || 'http://127.0.0.1:7877').replace(/\/$/, '');
const START_HINT =
	'The room relay is not running. Ask the core to start it: ' +
	"python3 skills/room-collab/scripts/room_collab.py --kind html relay '<room id>'";

// One talk at a time per voice agent: which beat is next, and whether turn ends advance it.
// `synced` is false once anything but the talk may have moved the deck (start, resume,
// goto, a manual move); the next beat then re-applies its position before speaking.
const talk: { beats: Beat[]; pos: number; active: boolean; synced: boolean } = {
	beats: [],
	pos: 0,
	active: false,
	synced: false,
};

// The session's context channel, from setup(), to restate the slide rule when a talk starts.
let injectContext: ((text: string) => void) | null = null;

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

// Which slide tool to use is the model's most common mistake: slide_control moves a local tab.
export const ROOM_SLIDE_RULE =
	'When the deck being presented is open as an AG2 Space room page (room_present, or the room relay is up), ' +
	'every slide request — "next", "go back", "go to slide N" — uses room_slide; to find where something is, room_outline; ' +
	'to point at it, room_highlight (topic key) or room_point (its words). ' +
	'slide_control moves only a browser tab on this computer and does nothing in the room.';

/** A user-directed move during a talk pauses it, or the next beat would move the deck away again. */
function pauseForManualMove(): string | undefined {
	talk.synced = false;
	if (!talk.active) return undefined;
	talk.active = false;
	return 'The talk is paused so this move sticks. Call room_present resume to continue from where you were.';
}

export const roomSlideTool: ToolDefinition = {
	name: 'room_slide',
	description:
		'Move the slide deck shown in the AG2 Space room for EVERYONE watching: next, previous, or go to a slide number. ' +
		'ALWAYS use this (never slide_control) for a deck you are presenting with room_present, or any deck in the room. ' +
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
		const note = pauseForManualMove();
		const res = await relay('POST', `/slide/${move}`);
		return note && !res.error ? { ...res, note } : res;
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
		const note = pauseForManualMove();
		const res = await relay('POST', `/highlight/${encodeURIComponent(topic.toLowerCase())}`);
		return note && !res.error ? { ...res, note } : res;
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

export const roomOutlineTool: ToolDefinition = {
	name: 'room_outline',
	description:
		'See what the room\'s HTML page contains before navigating: for a deck, every slide\'s number and title, and its ' +
		'highlightable parts (topic keys with the words they label); for another page, its headings. Call it when asked to ' +
		'go to or show a particular part, then use room_slide (by number), room_highlight (by topic) or room_point (by words). Instant.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		return relay('GET', '/outline');
	},
};

export const roomPointTool: ToolDefinition = {
	name: 'room_point',
	description:
		'Spotlight a passage on the room\'s page for everyone, by its words — any text on the slide showing now, even with no topic key ' +
		'(e.g. "Liveness isn\'t health"). Use it to point at what a question is about. Pass "clear" to remove it. Instant.',
	parameters: z.object({ words: z.string().min(1).max(200).describe('A few exact words from the page, or "clear"') }),
	execution: 'inline',
	async execute(args) {
		const { words } = args as { words: string };
		return relay('POST', `/spot/${encodeURIComponent(words)}`);
	},
};

export const roomScriptTool: ToolDefinition = {
	name: 'room_script',
	description:
		'Load the talk script from the room\'s Doc (its "Talk script" section) when the user asks you to present or give the talk. ' +
		'Returns steps; each step is a list of items in order: {say} is what you speak (natural, close to the words), ' +
		'and a cue is an action you take at exactly that point — {cue:"slide", move:"next"|"prev"|N} → room_slide, ' +
		'{cue:"highlight", topic} → room_highlight (topic "clear" clears), {cue:"pause", seconds} → a short silent pause. ' +
		'Never read a cue aloud. Go step by step; if someone interrupts, answer, then resume from the step you were on. Takes ~1–2 s.',
	parameters: z.object({}),
	execution: 'inline',
	timeout: 15_000,
	async execute() {
		let res: Response;
		try {
			res = await fetch(`${relayUrl()}/script`, { signal: AbortSignal.timeout(12_000) });
		} catch {
			return { error: START_HINT };
		}
		const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
		return res.ok ? body : { error: String(body.error ?? `relay answered ${res.status}`) };
	},
};


/** Take a beat's actions (in order, waiting out pauses), then hand back its line.
 *  Out of sync, the deck is first put where the script was before this beat. */
async function runBeat(beat: Beat, before: Beat | null): Promise<string | null> {
	if (!talk.synced) {
		for (const path of anchorPaths(before?.anchor ?? null)) await relay('POST', path);
		talk.synced = true;
	}
	for (const cue of beat.cues) {
		if (cue.cue === 'pause') await new Promise((r) => setTimeout(r, Math.min(cue.seconds, 10) * 1000));
		const path = cuePath(cue);
		if (path) await relay('POST', path);
	}
	return beat.say || null;
}

/** Run beats until one has a line to say (a beat of only actions just runs). */
async function nextLine(): Promise<{ say: string; n: number } | null> {
	while (talk.pos < talk.beats.length) {
		const before = talk.pos > 0 ? talk.beats[talk.pos - 1] : null;
		const beat = talk.beats[talk.pos++];
		const say = await runBeat(beat, before);
		if (say) return { say, n: talk.pos };
	}
	talk.active = false;
	return null;
}

export const roomPresentTool: ToolDefinition = {
	name: 'room_present',
	description:
		'Give the talk in the room\'s "Talk script", for everyone watching the room\'s deck. ' +
		'"start" loads the script and runs the first beat (it moves the slides and highlights ITSELF), then returns the first line: say it. ' +
		'After each line you say, the next beat arrives by itself as a silent control message: say only its line. Never narrate the script ahead. ' +
		'If someone interrupts or asks a question: call "pause", answer them, then call "resume" to continue from the same place. ' +
		'"goto" jumps to a script step (1-based); "stop" ends the talk. Use this, not room_script, whenever the user asks you to present.',
	parameters: z.object({
		action: z.enum(['start', 'pause', 'resume', 'goto', 'stop']),
		step: z.number().int().min(1).optional().describe('For goto: the 1-based script step'),
	}),
	execution: 'inline',
	timeout: 20_000,
	async execute(args) {
		const { action, step } = args as { action: 'start' | 'pause' | 'resume' | 'goto' | 'stop'; step?: number };
		if (action === 'pause' || action === 'stop') {
			talk.active = false;
			if (action === 'stop') talk.pos = talk.beats.length;
			return { ok: true, state: action === 'pause' ? 'paused — call resume to continue' : 'stopped' };
		}
		if (action === 'start' || talk.beats.length === 0) {
			const script = await relay('GET', '/script');
			if (script.error) return script;
			const outline = await relay('GET', '/outline');
			const topicSlide: Record<string, number> = {};
			((outline.slides as { n: number; topics: { topic: string }[] }[]) ?? []).forEach((s) =>
				s.topics.forEach((tp) => (topicSlide[tp.topic] ??= s.n)),
			);
			talk.beats = toBeats((script.steps as ScriptItem[][]) ?? [], topicSlide);
			talk.pos = 0;
			await relay('POST', '/highlight/clear'); // a talk starts from a clean stage
			if (!talk.beats.length) return { error: 'the Talk script is empty' };
		}
		if (action === 'goto') {
			const at = talk.beats.findIndex((b) => b.step === (step ?? 1) - 1);
			if (at < 0) return { error: `no script step ${step}` };
			talk.pos = at;
		} else if (action === 'resume' && talk.pos > 0) {
			talk.pos -= 1; // re-deliver the beat that was interrupted
		}
		talk.active = true;
		talk.synced = false; // whatever moved the deck meanwhile, the next beat puts it back
		if (action === 'start') injectContext?.(ROOM_SLIDE_RULE);
		const line = await nextLine();
		if (!line) return { ok: true, state: 'the talk is over' };
		return {
			ok: true,
			say: line.say,
			line: line.n,
			of: talk.beats.length,
			instruction: 'Say ONLY this line, then stop.',
			...(action === 'start' ? { rules: ROOM_SLIDE_RULE } : {}),
		};
	},
};

export const tools: ToolDefinition[] = [
	roomSlideTool,
	roomHighlightTool,
	roomPointTool,
	roomOutlineTool,
	roomStageTool,
	roomScriptTool,
	roomPresentTool,
];

/** The slide rule, in the voice prompt of every session this skill is loaded into. */
export function voiceSurface(): { promptRules: string[] } {
	return { promptRules: [ROOM_SLIDE_RULE] };
}

/** Pace the talk on turn ends: after the model finishes a line, take the next beat. */
export function setup(ctx: {
	session: unknown;
	injectText: (session: unknown, text: string) => void;
	injectContext?: (text: string) => void;
}): void {
	if (typeof ctx?.injectContext === 'function') injectContext = ctx.injectContext;
	const sess = ctx?.session as { eventBus?: { subscribe?: (ev: string, fn: () => void) => void } } | undefined;
	if (!sess?.eventBus?.subscribe || typeof ctx.injectText !== 'function') {
		console.warn('[room-collab] no turn events on this session; room_present runs one line per call');
		return;
	}
	sess.eventBus.subscribe('turn.end', () => {
		if (!talk.active) return;
		// Deferred past the turn's finalization, or the cue merges into the ended turn.
		setTimeout(async () => {
			if (!talk.active) return;
			const line = await nextLine();
			if (!line) return;
			try {
				ctx.injectText(ctx.session, lineInstruction(line.say, line.n, talk.beats.length));
			} catch (err) {
				console.warn(`[room-collab] present inject failed: ${err instanceof Error ? err.message : err}`);
			}
		}, 750);
	});
}
