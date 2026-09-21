// The `ui.navigate` / `ui.navigated` v1 wire contract with the AG2 Space client:
// builders, parsers and the bounds on client-supplied prose.

/** The capability a client announces in `session.context` when it answers `ui.navigate`. */
export const UI_NAVIGATE_CAPABILITY = 'ui.navigate';

/** Agent → client frame asking the desktop to move: the DM, a room found by
 *  the spoken words in `query`, or home. The client resolves names — it has
 *  the room list and the user's spaces — and answers with `ui.navigated`
 *  carrying the same `request_id`. */
export const UI_NAVIGATE_TYPE = 'ui.navigate';
export const UI_NAVIGATED_TYPE = 'ui.navigated';

export type UiNavigateTarget = 'dm' | 'room' | 'home';
export const UI_NAVIGATE_TARGETS: readonly UiNavigateTarget[] = ['dm', 'room', 'home'];

export interface UiNavigateFrame {
	type: typeof UI_NAVIGATE_TYPE;
	version: 1;
	request_id: string;
	target: UiNavigateTarget;
	/** The spoken room (and space) words; only meaningful for `target:'room'`. */
	query?: string;
}

export type UiNavigatedError = 'not_found' | 'ambiguous' | 'unsupported';
export const UI_NAVIGATED_ERRORS: readonly UiNavigatedError[] = ['not_found', 'ambiguous', 'unsupported'];

export interface UiNavigatedFrame {
	type: typeof UI_NAVIGATED_TYPE;
	version: 1;
	request_id: string;
	ok: boolean;
	room_id?: string;
	room_name?: string;
	error?: UiNavigatedError;
	/** On `ambiguous`: the display names that matched, for the agent to read back. */
	candidates?: string[];
}

/** Bounds on client-supplied prose before it reaches a prompt. */
export const UI_NAVIGATE_QUERY_MAX_CHARS = 200;
export const UI_NAVIGATED_NAME_MAX_CHARS = 120;
export const UI_NAVIGATED_MAX_CANDIDATES = 8;

function flatProse(v: unknown, max: number): string {
	return typeof v === 'string' ? v.replace(/[\r\n]+/g, ' ').trim().slice(0, max) : '';
}

/** Pure: the request frame. An empty or non-room `query` is omitted. */
export function buildUiNavigateFrame(requestId: string, target: UiNavigateTarget, query?: string): UiNavigateFrame {
	const frame: UiNavigateFrame = { type: UI_NAVIGATE_TYPE, version: 1, request_id: requestId, target };
	const q = target === 'room' ? flatProse(query, UI_NAVIGATE_QUERY_MAX_CHARS) : '';
	if (q) frame.query = q;
	return frame;
}

/** Pure: a `ui.navigate` v1 frame, or null for anything else (client side). */
export function parseUiNavigateFrame(msg: unknown): UiNavigateFrame | null {
	const m = msg as Record<string, unknown> | null | undefined;
	if (!m || m.type !== UI_NAVIGATE_TYPE || m.version !== 1) return null;
	if (typeof m.request_id !== 'string' || !m.request_id) return null;
	if (!(UI_NAVIGATE_TARGETS as readonly unknown[]).includes(m.target)) return null;
	return buildUiNavigateFrame(m.request_id, m.target as UiNavigateTarget, typeof m.query === 'string' ? m.query : undefined);
}

/** Pure: the reply frame (client side). `ok` is derived: true exactly when
 *  no `error` is given. Names and candidates are flattened and capped. */
export function buildUiNavigatedFrame(
	requestId: string,
	result: { room_id?: string; room_name?: string; error?: UiNavigatedError; candidates?: string[] },
): UiNavigatedFrame {
	const frame: UiNavigatedFrame = { type: UI_NAVIGATED_TYPE, version: 1, request_id: requestId, ok: !result.error };
	const roomId = flatProse(result.room_id, UI_NAVIGATED_NAME_MAX_CHARS);
	const roomName = flatProse(result.room_name, UI_NAVIGATED_NAME_MAX_CHARS);
	if (roomId) frame.room_id = roomId;
	if (roomName) frame.room_name = roomName;
	if (result.error) frame.error = result.error;
	const candidates = (result.candidates ?? [])
		.map(c => flatProse(c, UI_NAVIGATED_NAME_MAX_CHARS))
		.filter(Boolean)
		.slice(0, UI_NAVIGATED_MAX_CANDIDATES);
	if (candidates.length) frame.candidates = candidates;
	return frame;
}

/** Pure: a `ui.navigated` v1 frame, or null for anything else (agent side).
 *  An unknown `error` value reads as `unsupported`; a frame that says `ok`
 *  while carrying an error is not ok. */
export function parseUiNavigatedFrame(msg: unknown): UiNavigatedFrame | null {
	const m = msg as Record<string, unknown> | null | undefined;
	if (!m || m.type !== UI_NAVIGATED_TYPE || m.version !== 1) return null;
	if (typeof m.request_id !== 'string' || !m.request_id) return null;
	let error: UiNavigatedError | undefined;
	if (m.error !== undefined && m.error !== null) {
		error = (UI_NAVIGATED_ERRORS as readonly unknown[]).includes(m.error) ? (m.error as UiNavigatedError) : 'unsupported';
	} else if (m.ok !== true) {
		error = 'unsupported';
	}
	return buildUiNavigatedFrame(m.request_id, {
		room_id: typeof m.room_id === 'string' ? m.room_id : undefined,
		room_name: typeof m.room_name === 'string' ? m.room_name : undefined,
		error,
		candidates: Array.isArray(m.candidates) ? m.candidates.filter((c): c is string => typeof c === 'string') : undefined,
	});
}
