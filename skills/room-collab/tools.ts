// Room-collab voice tools: present an AG2 Space room's HTML page, whiteboard or Doc for everyone in the room.
// They call the local stage relay (`room_collab.py --kind html relay <room>`), which holds one surface open.

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import {
	anchorPaths,
	cuePath,
	describeAnchor,
	lineInstruction,
	toBeats,
	type Beat,
	type ScriptItem,
} from './present.js';
import { matchRoom, originRoom, ROOM_ID_RE, type RoomEntry } from './room-match.js';

// Read at call time: manifest config lands in process.env after imports are hoisted.
const relayUrl = () => (process.env.ROOM_COLLAB_RELAY_URL || 'http://127.0.0.1:7877').replace(/\/$/, '');
const START_HINT =
	'The room relay is not running. Ask the core to start it: ' +
	"python3 skills/room-collab/scripts/room_collab.py --kind html relay '<room id>'";
// Covers the relay's own wait for a switched room to connect.
const SWITCH_TIMEOUT_MS = 12_000;

// One talk at a time per voice agent: which beat is next, and whether turn ends advance it.
// `synced` is false once anything but the talk may have moved the deck (start, resume,
// goto, a manual move); the next beat then re-applies its position before speaking.
const talk: { beats: Beat[]; pos: number; active: boolean; synced: boolean; titles: Record<number, string> } = {
	beats: [],
	pos: 0,
	active: false,
	synced: false,
	titles: {},
};

/** Where the talk is, for the model: the line it is on and what the deck shows. */
function talkPosition(): Record<string, unknown> | null {
	if (!talk.beats.length) return null;
	const at = Math.max(0, talk.pos - 1);
	return {
		state: talk.active ? 'presenting' : talk.pos >= talk.beats.length ? 'finished' : 'paused',
		line: at + 1,
		of: talk.beats.length,
		step: talk.beats[at].step + 1,
		showing: describeAnchor(talk.beats[at].anchor, talk.titles),
	};
}

// The session's context channel, from setup(), to restate the slide rule when a talk starts.
let injectContext: ((text: string) => void) | null = null;

// The host's voice-session origin, from setup(), when it has one; the room last followed from it.
let voiceOrigin: (() => unknown) | null = null;
let followed: string | null = null;

async function call(method: 'GET' | 'POST', path: string, ms = 2_000): Promise<Record<string, unknown>> {
	let res: Response;
	try {
		res = await fetch(`${relayUrl()}${path}`, { method, signal: AbortSignal.timeout(ms) });
	} catch {
		return { error: START_HINT };
	}
	const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
	return res.ok ? body : { error: String(body.error ?? `relay answered ${res.status}`) };
}

/** Move the relay to the room this session is docked in, once per change of that room,
 *  so a room chosen with room_use holds until the owner moves to another room. */
async function follow(): Promise<void> {
	if (!voiceOrigin) return;
	let room: RoomEntry | null;
	try {
		room = originRoom(voiceOrigin());
	} catch {
		return;
	}
	if (!room || room.id === followed) return;
	const res = await call('POST', `/room/${encodeURIComponent(room.id)}`, SWITCH_TIMEOUT_MS);
	if (res.error !== START_HINT) followed = room.id;
	talk.synced = false;
}

async function relay(method: 'GET' | 'POST', path: string, timeoutMs = 2_000): Promise<Record<string, unknown>> {
	await follow();
	return call(method, path, timeoutMs);
}

// Which slide tool to use is the model's most common mistake: slide_control moves a local tab.
export const ROOM_SLIDE_RULE =
	'When the deck being presented is open as an AG2 Space room page (room_present, or the room relay is up), ' +
	'every slide request — "next", "go back", "go to slide N" — uses room_slide; to find where something is, room_outline; ' +
	'to point at it, room_highlight (topic key) or room_point (its words). ' +
	'The same room_slide / room_point / room_outline work on the room\'s whiteboard (frames are slides) and Doc (headings are slides) after room_surface switches to it. ' +
	'slide_control moves only a browser tab on this computer and does nothing in the room. ' +
	'When the user names a room ("present in the Design room"), call room_use with that name first.';

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
		'Works on the surface room_surface selected: the HTML page\'s slides, the whiteboard\'s frames (in the board\'s Present order), ' +
		'or the Doc\'s headings (scrolls everyone there). Numbers come from room_outline. ' +
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
		'Highlight a topic on the room\'s HTML page for everyone watching — a data-topic key the page defines (the HTML page only; ' +
		'on the whiteboard or Doc use room_point) ' +
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
		'Read what the room\'s HTML page is showing to everyone: the highlighted topic, whether a presenter is speaking, and — during a ' +
		'talk — which script line you are on and which slide the deck shows. Check it before answering a question about "this slide". ' +
		'On the whiteboard or Doc (see room_surface) it also names that surface. ' +
		'Also tells you whether the room relay is running (an error means it is not). Instant.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		const stage = await relay('GET', '/state');
		const position = talkPosition();
		return position ? { ...stage, talk: position } : stage;
	},
};

export const roomOutlineTool: ToolDefinition = {
	name: 'room_outline',
	description:
		'See what the room surface room_surface selected contains before navigating. The HTML page: for a deck, every slide\'s number and title, and its ' +
		'highlightable parts (topic keys with the words they label); for another page, its headings. The whiteboard: every frame\'s number, ' +
		'name and the texts in it. The Doc: every heading\'s number, level and title. Call it when asked to ' +
		'go to or show a particular part, then use room_slide (by number), room_highlight (by topic, page only) or room_point (by words). Instant.',
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
		'(e.g. "Liveness isn\'t health"). On the whiteboard it selects and zooms to the shape or frame saying them; on the Doc it scrolls ' +
		'to and flashes the passage. Use it to point at what a question is about. Pass "clear" to remove it. Instant.',
	parameters: z.object({ words: z.string().min(1).max(200).describe('A few exact words from the page, or "clear"') }),
	execution: 'inline',
	async execute(args) {
		const { words } = args as { words: string };
		return relay('POST', `/spot/${encodeURIComponent(words)}`);
	},
};

export const roomSurfaceTool: ToolDefinition = {
	name: 'room_surface',
	description:
		'Choose which of the room\'s surfaces room_slide, room_point, room_outline and room_stage act on: "html" (the HTML page, the default), ' +
		'"board" (the whiteboard) or "doc" (the Doc). Call it when the user wants to present or point at the whiteboard or the Doc, ' +
		'then room_outline to see its parts. A room can have several HTML pages: surface "pages" lists them (id and title), and ' +
		'surface "html" with a page id presents that page (omit page for the main one). ' +
		'Without a surface, says which one is selected. Takes ~1–2 s to switch.',
	parameters: z.object({
		surface: z
			.enum(['html', 'board', 'doc', 'pages'])
			.optional()
			.describe('The surface to act on, or "pages" to list the HTML pages; omit to ask which one is selected'),
		page: z
			.string()
			.regex(/^[a-z0-9]{8}$/)
			.optional()
			.describe('With surface "html": the id of one of the room\'s extra HTML pages, from surface "pages"'),
	}),
	execution: 'inline',
	timeout: 20_000,
	async execute(args) {
		const { surface, page } = args as { surface?: 'html' | 'board' | 'doc' | 'pages'; page?: string };
		if (!surface) return relay('GET', '/surface');
		if (surface === 'pages') return relay('GET', '/pages', 17_000);
		const note = pauseForManualMove();
		const target = surface === 'html' && page ? `html-${page}` : surface;
		const res = await relay('POST', `/surface/${target}`, 17_000);
		return note && !res.error ? { ...res, note } : res;
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
		await follow();
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
async function nextLine(): Promise<{ say: string; n: number; showing?: string } | null> {
	while (talk.pos < talk.beats.length) {
		const before = talk.pos > 0 ? talk.beats[talk.pos - 1] : null;
		const beat = talk.beats[talk.pos++];
		const say = await runBeat(beat, before);
		if (say) return { say, n: talk.pos, showing: describeAnchor(beat.anchor, talk.titles) };
	}
	talk.active = false;
	return null;
}

export const roomUseTool: ToolDefinition = {
	name: 'room_use',
	description:
		'Choose which AG2 Space room the room_* tools act in. Call it FIRST whenever the user names a room — ' +
		'"present in the Qingyun Group room", "show the deck in Design", "switch to <room>". ' +
		'Pass the room\'s name as the user said it (or its id, !abc:server). Returns the room\'s id and name and ' +
		'whether it has an HTML page to present; if the name is ambiguous or unknown, ask the user which room. Takes ~1–5 s.',
	parameters: z.object({ room: z.string().min(1).max(255).describe('A room name, or a room id like !abc:server') }),
	execution: 'inline',
	timeout: 20_000,
	async execute(args) {
		const { room } = args as { room: string };
		let rooms: RoomEntry[] = [];
		if (!ROOM_ID_RE.test(room.trim())) {
			const listed = await call('GET', '/rooms', 8_000);
			if (listed.error) return listed;
			rooms = (listed.rooms as RoomEntry[]) ?? [];
		}
		const match = matchRoom(room, rooms);
		if ('error' in match) return match;
		const res = await call('POST', `/room/${encodeURIComponent(match.room.id)}`, SWITCH_TIMEOUT_MS);
		if (res.error && res.connected !== false) return res;
		talk.synced = false;
		return {
			ok: true,
			room: match.room.id,
			name: match.room.name,
			connected: res.connected,
			has_page: res.has_page,
			...(res.connected === false ? { error: res.error } : {}),
			...(res.has_page === false ? { note: 'this room has no HTML page yet, so there is nothing to present' } : {}),
		};
	},
};

export const roomPresentTool: ToolDefinition = {
	name: 'room_present',
	description:
		'Give the talk in the room\'s "Talk script", for everyone watching the room\'s deck. ' +
		'If the user names a room to present in, call room_use with it first. ' +
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
			talk.titles = {};
			((outline.slides as { n: number; title: string | null; topics: { topic: string }[] }[]) ?? []).forEach((s) => {
				if (s.title) talk.titles[s.n] = s.title;
				s.topics.forEach((tp) => (topicSlide[tp.topic] ??= s.n));
			});
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
			showing: line.showing,
			instruction: 'Say ONLY this line, then stop.',
			...(action === 'start' ? { rules: ROOM_SLIDE_RULE } : {}),
		};
	},
};

export const tools: ToolDefinition[] = [
	roomUseTool,
	roomSlideTool,
	roomHighlightTool,
	roomPointTool,
	roomOutlineTool,
	roomStageTool,
	roomSurfaceTool,
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
	getVoiceSessionOrigin?: () => unknown;
}): void {
	if (typeof ctx?.injectContext === 'function') injectContext = ctx.injectContext;
	// Hosts older than the voice-session origin have no getter: the relay keeps its room.
	if (typeof ctx?.getVoiceSessionOrigin === 'function') voiceOrigin = ctx.getVoiceSessionOrigin;
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
				ctx.injectText(ctx.session, lineInstruction(line.say, line.n, talk.beats.length, line.showing));
			} catch (err) {
				console.warn(`[room-collab] present inject failed: ${err instanceof Error ? err.message : err}`);
			}
		}, 750);
	});
}
