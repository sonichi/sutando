// navigate_ui: the voice agent asks the attached AG2 Space client to open the DM, a
// room or home, and waits for its `ui.navigated` reply. The client resolves room names.

import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import {
	buildUiNavigateFrame,
	parseUiNavigatedFrame,
	UI_NAVIGATE_CAPABILITY,
	UI_NAVIGATE_TARGETS,
	type UiNavigatedError,
	type UiNavigatedFrame,
	type UiNavigateTarget,
} from './navigate-protocol.js';
import { claudeHomePath } from '../../src/util_paths.js';

/** How long the tool waits for the client's `ui.navigated` before giving up. */
export const NAVIGATE_UI_TIMEOUT_MS = 6000;

export const NAVIGATE_UI_UNSUPPORTED_MESSAGE =
	'No desktop client is connected to this session. Navigation works in the desktop app; tell the user that and move on.';
export const NAVIGATE_UI_TIMEOUT_MESSAGE =
	'The desktop did not confirm the move in time. Tell the user it did not go through and that they can ask again.';
/** An attached client that never announced `ui.navigate`: it cannot answer
 *  the frame, so nothing is sent and the owner hears this at once. */
export const NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE =
	"This app can't be navigated by voice yet. If it is the desktop app, tell the user to update it.";
/** `target:'room'` needs the spoken room words; without them there is nothing
 *  for the desktop to resolve, so no frame goes out. */
export const NAVIGATE_UI_ROOM_QUERY_MISSING_MESSAGE =
	"Say which room, for example 'take me to GTM in Investors'.";

const _GATEWAY_TOKEN_KEYS = ['REMOTE_TASK_TOKEN', 'AG2_REMOTE_TOKEN'];

/** Whether this install has the ag2space gateway channel provisioned: the only
 *  client that answers `ui.navigate`. Same detection as runtime-health.py. */
export function navigateUiAvailable(env: NodeJS.ProcessEnv = process.env): boolean {
	if (_GATEWAY_TOKEN_KEYS.some(k => env[k])) return true;
	try {
		const lines = readFileSync(claudeHomePath('channels', 'ag2space', '.env'), 'utf-8').split('\n');
		return lines.some(l => _GATEWAY_TOKEN_KEYS.some(k => l.startsWith(`${k}=`) && l.length > k.length + 1));
	} catch {
		return false;
	}
}

export interface VoiceNavigateClient {
	/** True while a real client is attached and can receive frames. */
	attached(): boolean;
	/** True when the attached client announced `capability` in its `session.context` frame. */
	supports(capability: string): boolean;
	/** Throwing or returning false means the frame did not go out. */
	send(frame: Record<string, unknown>): boolean | void;
}

let _client: VoiceNavigateClient | null = null;
const _pending = new Map<string, (reply: UiNavigatedFrame | null) => void>();

/** Bind (or, with null, remove) the client the tool talks to. */
export function installVoiceNavigateClient(client: VoiceNavigateClient | null): void {
	_client = client;
}

/** Number of requests still waiting on a reply (tests, diagnostics). */
export function pendingNavigationCount(): number {
	return _pending.size;
}

/** Route a client frame: settles the matching in-flight request and returns
 *  true; false for any other frame, including a reply nobody is waiting on. */
export function resolveUiNavigated(msg: unknown): boolean {
	const reply = parseUiNavigatedFrame(msg);
	if (!reply) return false;
	const settle = _pending.get(reply.request_id);
	if (!settle) return false;
	_pending.delete(reply.request_id);
	settle(reply);
	return true;
}

/** The client left: every in-flight request answers `unsupported` now
 *  rather than at its timeout. Returns how many were failed. */
export function failPendingNavigations(): number {
	const settlers = Array.from(_pending.values());
	_pending.clear();
	for (const settle of settlers) settle(null);
	return settlers.length;
}

export type NavigateUiResult =
	| { ok: true; target: UiNavigateTarget; room_id?: string; room_name?: string }
	| { ok: false; target: UiNavigateTarget; error: UiNavigatedError | 'timeout'; message: string; candidates?: string[] };

export interface NavigateUiOptions {
	timeoutMs?: number;
	/** Fixed request id (tests); default a UUID. */
	requestId?: string;
}

function describeTarget(target: UiNavigateTarget, query?: string): string {
	if (target === 'dm') return 'the DM';
	if (target === 'home') return 'home';
	return query ? `a room called "${query}"` : 'a room';
}

/** Send one `ui.navigate` and wait for its `ui.navigated`. Never throws: every
 *  outcome is a result the model can speak. */
export async function navigateUi(args: { target: UiNavigateTarget; query?: string }, opts: NavigateUiOptions = {}): Promise<NavigateUiResult> {
	const { target, query } = args;
	const client = _client;
	if (!client || !client.attached()) {
		return { ok: false, target, error: 'unsupported', message: NAVIGATE_UI_UNSUPPORTED_MESSAGE };
	}
	if (!client.supports(UI_NAVIGATE_CAPABILITY)) {
		return { ok: false, target, error: 'unsupported', message: NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE };
	}
	const frame = buildUiNavigateFrame(opts.requestId ?? `nav-${randomUUID()}`, target, query);
	if (target === 'room' && !frame.query) {
		return { ok: false, target, error: 'not_found', message: NAVIGATE_UI_ROOM_QUERY_MISSING_MESSAGE };
	}
	const timeoutMs = opts.timeoutMs ?? NAVIGATE_UI_TIMEOUT_MS;

	const reply = await new Promise<UiNavigatedFrame | null | 'timeout'>((resolve) => {
		const timer = setTimeout(() => {
			_pending.delete(frame.request_id);
			resolve('timeout');
		}, timeoutMs);
		_pending.set(frame.request_id, (r) => {
			clearTimeout(timer);
			resolve(r);
		});
		let sent = false;
		try {
			sent = client.send({ ...frame }) !== false;
		} catch { /* not sent */ }
		if (!sent) {
			clearTimeout(timer);
			_pending.delete(frame.request_id);
			resolve(null);
		}
	});

	if (reply === 'timeout') {
		return { ok: false, target, error: 'timeout', message: NAVIGATE_UI_TIMEOUT_MESSAGE };
	}
	if (reply === null) {
		return { ok: false, target, error: 'unsupported', message: NAVIGATE_UI_UNSUPPORTED_MESSAGE };
	}
	if (reply.ok) {
		const out: NavigateUiResult = { ok: true, target };
		if (reply.room_id) out.room_id = reply.room_id;
		if (reply.room_name) out.room_name = reply.room_name;
		return out;
	}
	const where = describeTarget(target, frame.query);
	switch (reply.error) {
		case 'ambiguous': {
			const names = reply.candidates ?? [];
			const list = names.length ? names.join(', ') : 'several rooms';
			return {
				ok: false, target, error: 'ambiguous', candidates: names,
				message: `More than one room matched ${where}: ${list}. Ask the user which one, then call navigate_ui again with the name they pick.`,
			};
		}
		case 'not_found':
			return { ok: false, target, error: 'not_found', message: `The desktop found no room matching ${where}. Tell the user you could not find it and ask for the name.` };
		default:
			return { ok: false, target, error: 'unsupported', message: NAVIGATE_UI_UNSUPPORTED_MESSAGE };
	}
}

export const navigateUiTool: ToolDefinition = {
	name: 'navigate_ui',
	description:
		'Move the desktop app to the DM, a room, or home. Instant. ' +
		'Call it when the user says "let\'s talk in my DM", "go to my DM", "take me to <room>", "go to / open <room> (in <space>)", or "go home". ' +
		'Pass target "dm", "room" or "home"; for a room pass `query` = the room and space words exactly as spoken (the desktop resolves the name). ' +
		'Returns {ok:true, room_name} when the desktop moved — say one short line ("Taking you to GTM.") and continue. ' +
		'Returns error "ambiguous" with `candidates` — read them and ask which one; "not_found" — say you could not find that room; ' +
		'"unsupported" or "timeout" — say navigation works in the desktop app. Never route these phrases to work.',
	parameters: z.object({
		target: z.enum(UI_NAVIGATE_TARGETS as [UiNavigateTarget, ...UiNavigateTarget[]]).describe('Where to go: "dm" (the owner\'s DM with you), "room" (a room by name), or "home".'),
		query: z.string().optional().describe('For target "room": the room and, if said, the space, as spoken — e.g. "GTM in Investors".'),
	}),
	execution: 'inline',
	timeout: NAVIGATE_UI_TIMEOUT_MS + 2000,
	async execute(args) {
		const { target, query } = (args ?? {}) as { target: UiNavigateTarget; query?: string };
		const result = await navigateUi({ target, query });
		console.log(`[NavigateUI] ${target}${query ? ` "${query}"` : ''} → ${result.ok ? `ok${result.room_name ? ` (${result.room_name})` : ''}` : result.error}`);
		return result;
	},
};

/** The prompt rule that goes with the tool; present only when the tool is declared. */
export const NAVIGATION_PROMPT_RULE = '- NAVIGATION: "let\'s talk in my DM", "go to my DM", "take me to <room>", "go to / open <room> (in <space>)", "go home" → call navigate_ui (target dm | room | home; query = the room and space words as spoken) — never work, never press_key. On ok, say ONE short line ("Taking you to GTM.") and carry on; the desktop then sends the new room context, and from there your replies and delegated work follow that room. On error "ambiguous", read the candidates and ask which one ("I found two rooms: GTM and GTM planning — which one?"), then call navigate_ui again with the name they pick. On "not_found", say you could not find a room called that. On "unsupported" or "timeout", say navigation works in the desktop app and move on.';
