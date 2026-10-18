/**
 * edit_project_file — change a LaTeX or Typst project file by voice without the voice model writing the file.
 *
 * The voice model passes a short instruction; a text model turns it into old_text/new_text edits against the
 * file it was read from, and room.commons.changes.apply applies them on that content_hash.
 */
import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveCredential } from '../../src/credential-resolver.js';
import { WORK_CHECK_MODEL } from '../../src/work-request-check.js';
import {
	OUTCOME_UNKNOWN, OUTCOME_UNKNOWN_ERROR, ROOM_COMMONS_TIMEOUT_MS, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_NAME_ERRORS,
	roomExecute, roomRead, targetRoom, type McpClient, type ReadRoomDeps, type RoomRef,
} from './room-read.js';

/** The most of a file read in one call; a longer file is refused rather than edited from a part. */
export const EDIT_FILE_MAX_BYTES = 262_144;
export const EDIT_PLAN_TIMEOUT_MS = 30_000;

export interface Edit { old_text: string; new_text: string }
export type EditPlan = { ok: true; edits: Edit[]; summary: string } | { ok: false; error: string };
/** Turns an instruction into edits against `text`; `problem` says why the previous plan could not be applied. */
export type PlanEdits = (input: { title: string; text: string; instruction: string; problem?: string }) => Promise<EditPlan>;

export type EditFileResult =
	| { ok: true; room: string; title: string; summary: string; edits: number; build: string; problem?: string; ms: number; next: string }
	| { ok: false; error: string; detail?: string; done?: false; next?: string; candidates?: string[]; rooms?: string[] };

export const EDIT_DONE_NEXT = 'Done. Tell the user in one sentence what you changed ("I have …"), in which file.';
export const EDIT_REVERTED_NEXT = 'Nothing was kept: the change broke the build, so it was undone. Tell the user in a sentence which error it caused (`detail`), and offer to try it another way.';
export const EDIT_NOT_DONE_NEXT = 'Nothing was changed. Tell the user in a sentence what went wrong; do not say it is done.';

/** Files a Typst or LaTeX source pulls in: image("…"), #include/#import "…", bibliography("…"), \\includegraphics{…}, \\input{…}… */
const FILE_REF_RES = [
	/\b(?:image|bibliography|read|csv|json|yaml|toml|xml|cbor)\(\s*"([^"]+)"/g,
	/#(?:include|import)\s+"([^"]+)"/g,
	/\\(?:includegraphics|input|include|bibliography|addbibresource|lstinputlisting)\*?(?:\[[^\]]*\])?\{([^}]+)\}/g,
];
export function fileRefs(text: string): Set<string> {
	const out = new Set<string>();
	for (const re of FILE_REF_RES) for (const m of text.matchAll(re)) for (const f of m[1].split(',')) if (f.trim()) out.add(f.trim());
	return out;
}
/** A file the edit references that the file did not and the user did not name: it would not be found in the build. */
export function newFileRefs(before: string, after: string, instruction: string): string[] {
	const had = fileRefs(before);
	return [...fileRefs(after)].filter((f) => !had.has(f) && !instruction.includes(f));
}

/** The edits for the local log: each side cut, so a refusal or a broken build can be traced to what was sent. */
export function editsForLog(edits: readonly Edit[]): string {
	const cut = (t: string) => JSON.stringify(t.length > 120 ? `${t.slice(0, 120)}…(${t.length})` : t);
	return edits.map((e) => `${cut(e.old_text)} → ${cut(e.new_text)}`).join('; ');
}

const FIGURE_RE = /#figure\(|\\begin\{figure\*?\}/g;
/** The user asked to take something out: only then may an edit drop a figure. */
export const REMOVE_RE = /\b(delete|remove|replace|swap|drop|instead|get rid|change|turn)\b|删|去掉|换|替换|改成|移除|不要/i;

/** The user asked for something more: an existing figure must then survive the edit unchanged. */
export const ADD_RE = /\b(add|another|new|insert|append|more|also)\b|再|加|新|插入|另/i;

/** An edit that drops a figure the instruction did not ask to remove: "add another figure" once replaced the last one. */
export function droppedFigure(edits: readonly Edit[], instruction: string): string | null {
	if (REMOVE_RE.test(instruction)) return null;
	for (const [i, e] of edits.entries()) {
		const before = (e.old_text.match(FIGURE_RE) ?? []).length;
		const after = (e.new_text.match(FIGURE_RE) ?? []).length;
		if (after < before) return `edit ${i + 1} removes an existing figure the instruction did not ask to remove: keep its old_text unchanged at the start of new_text and add after it`;
		if (before > 0 && !e.new_text.includes(e.old_text) && after === before && ADD_RE.test(instruction)) {
			return `edit ${i + 1} rewrites an existing figure the instruction did not ask to change: keep its old_text unchanged at the start of new_text and add the new figure after it`;
		}
	}
	return null;
}

/** Each edit's old_text must occur exactly once in the text the edits before it left. */
export function applyEdits(text: string, edits: readonly Edit[]): { ok: true; text: string } | { ok: false; problem: string } {
	let out = text;
	for (const [i, e] of edits.entries()) {
		const at = out.indexOf(e.old_text);
		if (!e.old_text || at < 0) return { ok: false, problem: `edit ${i + 1}: old_text does not occur in the file` };
		if (out.indexOf(e.old_text, at + 1) >= 0) return { ok: false, problem: `edit ${i + 1}: old_text occurs more than once; quote more of it` };
		out = out.slice(0, at) + e.new_text + out.slice(at + e.old_text.length);
	}
	return { ok: true, text: out };
}

/** The user asked for a drawing, not a picture file: "draw a puppy", "画一只小狗", "sketch a tree". */
export const DRAW_RE = /\b(draw|drawing|drawn|sketch)\b|画/i;

export function editPlanPrompt(input: Parameters<PlanEdits>[0]): string {
	const draw = DRAW_RE.test(input.instruction);
	return [
		'You edit one source file of a LaTeX or Typst project. Make the smallest change that does what the instruction asks, and keep the file compiling.',
		`Instruction: ${input.instruction}`,
		...(draw ? ['The user asked you to DRAW it: draw it in this file with the language\'s own shapes, recognisable and simple. Never answer that a picture or file is missing for it, and reference no image file.'] : []),
		...(input.problem ? [`Your previous edits could not be applied: ${input.problem}. Copy old_text exactly from the file.`] : []),
		'Reply with JSON only:',
		'{"edits":[{"old_text":"…","new_text":"…"}],"summary":"<what you changed, one short sentence in the instruction\'s language>"}',
		'Each old_text is copied exactly from the file (whitespace included), long enough to occur exactly once, and no longer than that needs.',
		'To add something (a figure, a sentence, a section) after existing text, new_text starts with that old_text unchanged: never drop or rewrite existing content the instruction did not ask to change.',
		'Never reference a file the project may not have (an image, a .bib, an #include or #import) unless this file already references it or the instruction gives its exact name: an invented file name breaks the build.',
		'If the instruction needs such a file (a picture of something, a new bibliography), reply {"error":"<which file is missing, in the instruction\'s language>"}: the user uploads it first. Do not put a drawing, a shape or a placeholder in its place unless the instruction asks you to draw it.',
		'When the instruction asks you to draw something ("draw a mushroom", "画一个蘑菇"), draw it in the file with the language\'s own shapes (Typst: circle, ellipse, rect, polygon, path, curve with fill colors; LaTeX: TikZ when the file loads it), recognisable and simple, inside the figure the instruction names.',
		'If the instruction cannot be done on this file for another reason: {"error":"<one sentence why>"}.',
		`File "${input.title}":`,
		'<<<FILE',
		input.text,
		'FILE>>>',
	].join('\n');
}

export function parseEditPlan(raw: string): EditPlan {
	let v: unknown;
	try { v = JSON.parse(raw); } catch { return { ok: false, error: 'the editor gave no usable answer' }; }
	const o = (v ?? {}) as { edits?: unknown; summary?: unknown; error?: unknown };
	if (typeof o.error === 'string' && o.error.trim()) return { ok: false, error: o.error.trim() };
	if (!Array.isArray(o.edits) || o.edits.length === 0) return { ok: false, error: 'no change was made: this tool only changes the file. To answer a question about it, read it with read_page' };
	const edits = o.edits.filter((e): e is Edit => !!e && typeof (e as Edit).old_text === 'string' && typeof (e as Edit).new_text === 'string');
	if (edits.length !== o.edits.length) return { ok: false, error: 'the editor gave a malformed edit' };
	return { ok: true, edits, summary: typeof o.summary === 'string' ? o.summary.trim() : '' };
}

/** The relay agent's text model, over REST: JSON out, thinking off. */
export const geminiPlanEdits: PlanEdits = async (input) => {
	const apiKey = resolveCredential('gemini-voice').key;
	if (!apiKey) return { ok: false, error: 'no Gemini key' };
	try {
		// The key in a header, never the URL, so it cannot end up in a logged URL.
		const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${WORK_CHECK_MODEL}:generateContent`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', 'x-goog-api-key': apiKey },
			body: JSON.stringify({
				contents: [{ parts: [{ text: editPlanPrompt(input) }] }],
				generationConfig: { maxOutputTokens: 4_000, temperature: 0, responseMimeType: 'application/json', thinkingConfig: { thinkingBudget: 0 } },
			}),
			signal: AbortSignal.timeout(EDIT_PLAN_TIMEOUT_MS),
		});
		const data = await res.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>; error?: { message?: string } };
		const text = data.candidates?.[0]?.content?.parts?.map((p) => p.text ?? '').join('') ?? '';
		return text ? parseEditPlan(text) : { ok: false, error: `the editor gave no answer (${data.error?.message ?? 'empty'})` };
	} catch (err) {
		return { ok: false, error: `the editor failed (${err instanceof Error ? err.message : String(err)})` };
	}
};

/** Where a file builds: its folder (and the folder's main file and images), or the file alone. */
export interface BuildTarget { project: string; main: string; tool: 'typst' | 'latex'; media: Array<{ id: string; path: string }> }

export async function buildTarget(client: McpClient, room: RoomRef, kind: string, deps: ReadRoomDeps): Promise<{ folder: string | null; target: BuildTarget | null }> {
	const pages = await roomRead(client, room, 'room.commons.pages.list', { surface: 'doc' }, deps, ROOM_COMMONS_TIMEOUT_MS);
	const page = pages.ok ? ((pages.result.pages ?? []) as Array<{ kind: string; title: string; folder_id: string | null }>).find((p) => p.kind === kind) : undefined;
	const folder = page?.folder_id ?? null;
	const toolOf = (title: string) => /\.typ$/i.test(title) ? 'typst' as const : /\.tex$/i.test(title) ? 'latex' as const : null;
	if (!folder) {
		const tool = page ? toolOf(page.title) : null;
		return { folder, target: tool ? { project: kind, main: kind, tool, media: [] } : null };
	}
	const folders = await roomRead(client, room, 'room.commons.folders.list', {}, deps, ROOM_COMMONS_TIMEOUT_MS);
	if (!folders.ok) return { folder, target: null };
	const f = ((folders.result.folders ?? []) as Array<{ id: string; main?: string | null }>).find((x) => x.id === folder);
	const siblings = pages.ok ? ((pages.result.pages ?? []) as Array<{ kind: string; title: string; folder_id: string | null }>).filter((p) => p.folder_id === folder) : [];
	const main = f?.main || (page && toolOf(page.title) ? kind : siblings.find((p) => /^main\.(typ|tex)$/i.test(p.title))?.kind);
	const mainTitle = siblings.find((p) => p.kind === main || p.kind === `markdown-${main}`)?.title ?? page?.title ?? '';
	const tool = toolOf(mainTitle);
	if (!main || !tool) return { folder, target: null };
	const inFolder = ((folders.result.placements ?? []) as Array<{ surface: string; item_id: string; folder_id: string }>).filter((p) => p.surface === 'media' && p.folder_id === folder).map((p) => p.item_id);
	let media: BuildTarget['media'] = [];
	if (inFolder.length) {
		const list = await roomRead(client, room, 'room.media.list', {}, deps, ROOM_COMMONS_TIMEOUT_MS);
		const names = new Map(list.ok ? ((list.result.files ?? []) as Array<{ id: string; name: string | null }>).map((m) => [m.id, m.name ?? ''] as const) : []);
		media = inFolder.filter((id) => names.get(id)).map((id) => ({ id, path: names.get(id)! }));
	}
	return { folder, target: { project: folder, main, tool, media } };
}

/** One server build: its error messages, or null when it could not run (budget, timeout…), which blocks nothing. */
export async function buildErrors(client: McpClient, room: RoomRef, t: BuildTarget, deps: ReadRoomDeps): Promise<string[] | null> {
	const action = t.tool === 'typst' ? 'room.commons.typst.compile' : 'room.commons.latex.compile';
	const r = await roomExecute(client, room, action, { project: t.project, main: t.main, trigger: 'manual', ...(t.tool === 'typst' && t.media.length ? { media: t.media } : {}) }, deps, { operationId: randomUUID() });
	if (!r.ok) return null;
	const status = String(r.result.status ?? '');
	if (status !== 'ok' && status !== 'errors') return null;
	return ((r.result.problems ?? []) as Array<{ severity: string; message: string }>).filter((p) => p.severity === 'error').map((p) => p.message);
}

export async function editProjectFile(
	args: { kind: string; instruction: string; room?: string; project?: string },
	deps: ReadRoomDeps,
	plan: PlanEdits = geminiPlanEdits,
): Promise<EditFileResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const kind = (args.kind ?? '').trim();
	if (!/^markdown-[a-z0-9]{8}$/.test(kind)) return { ok: false, error: 'no_file' };
	const instruction = (args.instruction ?? '').trim();
	if (!instruction) return { ok: false, error: 'no_instruction' };
	const target = await targetRoom(args.room, deps);
	if (!target.ok) return target;
	const room = target.room;
	const where = await buildTarget(client, room, kind, deps);
	const project = (args.project ?? '').trim() || where.folder || kind;
	const operationId = randomUUID();
	let problem: string | undefined;
	// A second round only after a plan that did not fit the file, or a file changed under us.
	for (let round = 0; round < 2; round++) {
		const read = await roomRead(client, room, 'room.commons.pages.read', { kind, max_bytes: EDIT_FILE_MAX_BYTES }, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) return read;
		const text = typeof read.result.text === 'string' ? read.result.text : '';
		const hash = typeof read.result.content_hash === 'string' ? read.result.content_hash : '';
		const title = typeof read.result.title === 'string' ? read.result.title : '';
		if (read.result.next_offset != null) return { ok: false, error: 'file_too_long', done: false, next: EDIT_NOT_DONE_NEXT };
		if (!hash) return { ok: false, error: 'no_content_hash', done: false, next: EDIT_NOT_DONE_NEXT };
		const p = await plan({ title, text, instruction, problem });
		if (!p.ok) {
			deps.log?.(`[EditFile] ${title || kind}: not edited: ${p.error}`);
			return { ok: false, error: 'cannot_edit', detail: p.error, done: false, next: EDIT_NOT_DONE_NEXT };
		}
		const fit = applyEdits(text, p.edits);
		if (!fit.ok) { problem = fit.problem; continue; }
		const dropped = droppedFigure(p.edits, instruction);
		if (dropped) { deps.log?.(`[EditFile] ${title || kind}: planned again: ${dropped}`); problem = dropped; continue; }
		const missing = newFileRefs(text, fit.text, instruction);
		if (missing.length) {
			deps.log?.(`[EditFile] ${title || kind}: not edited: references ${missing.join(', ')}, which the project may not have`);
			return { ok: false, error: 'missing_file', detail: `The change needs ${missing.join(', ')}, which this file does not use yet; the user uploads it to the project first, or asks for a drawing instead.`, done: false, next: EDIT_NOT_DONE_NEXT };
		}
		const summary = p.summary || instruction;
		const r = await roomExecute(client, room, 'room.commons.changes.apply', {
			project, message: summary.slice(0, 500), files: [{ page_id: kind, base_hash: hash, edits: p.edits }],
		}, deps, { operationId });
		if (r.ok) {
			deps.log?.(`[EditFile] ${title || kind} in ${room.name}: ${p.edits.length} edit(s) in ${Date.now() - t0}ms: ${editsForLog(p.edits)}`);
			const done = (build: string, problem?: string): EditFileResult => ({ ok: true, room: room.name, title, summary, edits: p.edits.length, build, ...(problem ? { problem } : {}), ms: Date.now() - t0, next: EDIT_DONE_NEXT });
			if (!where.target) return done('not_checked');
			// Build once; a change that adds an error is undone, so a broken file is never left behind.
			const after = await buildErrors(client, room, where.target, deps);
			if (after === null) return done('not_checked');
			if (after.length === 0) return done('ok');
			const undone = await writeWhole(client, room, project, kind, text, deps);
			if (!undone.ok) {
				deps.log?.(`[EditFile] ${title || kind}: the change breaks the build and could not be undone: ${undone.error}`);
				return { ok: false, error: 'breaks_build_not_undone', detail: after[0], done: false, next: 'The change was applied but breaks the build, and undoing it failed. Tell the user which error (`detail`), and that the file needs fixing in the app.' };
			}
			const before = await buildErrors(client, room, where.target, deps);
			const added = before === null ? after : after.filter((m) => !before.includes(m));
			if (added.length === 0) {
				// The errors were there before the change: it is not the cause, so it goes back in.
				const again = await writeWhole(client, room, project, kind, fit.text, deps);
				if (again.ok) { deps.log?.(`[EditFile] ${title || kind}: build already had errors; change kept`); return done('errors', after[0]); }
			}
			deps.log?.(`[EditFile] ${title || kind}: undone, it broke the build: ${added[0] ?? after[0]}`);
			return { ok: false, error: 'breaks_build', detail: added[0] ?? after[0], done: false, next: EDIT_REVERTED_NEXT };
		}
		if (r.error === OUTCOME_UNKNOWN) return r;
		if (/stale_base/i.test(r.error) && round === 0) { problem = undefined; continue; }
		return { ...r, done: false, next: EDIT_NOT_DONE_NEXT };
	}
	return { ok: false, error: 'cannot_edit', detail: problem, done: false, next: EDIT_NOT_DONE_NEXT };
}

/** The whole file written back on its current hash (to undo a change, or redo one). */
async function writeWhole(client: McpClient, room: RoomRef, project: string, kind: string, text: string, deps: ReadRoomDeps): Promise<{ ok: true } | { ok: false; error: string }> {
	const read = await roomRead(client, room, 'room.commons.pages.read', { kind, max_bytes: EDIT_FILE_MAX_BYTES }, deps, ROOM_COMMONS_TIMEOUT_MS);
	if (!read.ok) return read;
	const hash = typeof read.result.content_hash === 'string' ? read.result.content_hash : '';
	if (!hash) return { ok: false, error: 'no_content_hash' };
	const r = await roomExecute(client, room, 'room.commons.changes.apply', { project, message: 'Undo a change that broke the build', files: [{ page_id: kind, base_hash: hash, text }] }, deps, { operationId: randomUUID() });
	return r.ok ? { ok: true } : r;
}

export function createEditFileTool(deps: ReadRoomDeps, plan: PlanEdits = geminiPlanEdits): ToolDefinition {
	return {
		name: 'edit_project_file',
		description:
			'Change what a LaTeX or Typst project file says (.typ, .tex, .bib…) in an AG2 Space room: "make the title green", "add an abstract", "fix the typo in the intro". ' +
			'Pass `kind` = the file\'s kind from search_commons (e.g. "markdown-qop83g48"), `instruction` = what to change, in the user\'s words, and `room` = the same room. ' +
			'Never pass the file\'s text: this tool reads the file and writes the change itself. Call it at once: do not read the change back or ask for a yes. ' +
			'Pass `project` (the folder id) only when a previous call\'s `detail` says the file belongs to a folder project. ' +
			'It compiles the project after the change; a change that adds a build error is undone at once. ' +
			'Returns {ok:true, title, summary, build} — then tell the user in one sentence what you changed ("I have …"); if build is "errors", also say the build already had `problem`; never say it is done before ok:true. ' +
			'"breaks_build" — the change caused `detail` and was undone: say so and offer another way; ' +
			'Errors: "cannot_edit" — `detail` says why: say it to the user; "missing_file" — the change needs a file (a picture…) the project may not have: say which; they can upload it, ask you to draw it instead, or have it generated — then submit work with the whole request in one task: what to generate, the room and project to put it in, and what to change in which file (e.g. "Generate a picture of a pineapple, add it to the TypeScript test project in susan\'s agents, and replace figure 3 in typst.typ with it"); "file_too_long" — too long to change by voice; "no_file" — the kind is not a file kind; ' + ROOM_NAME_ERRORS +
			'"FORBIDDEN" — you may not change it there; "not_connected", "timeout" or "unavailable" — it could not be done right now; ' + OUTCOME_UNKNOWN_ERROR,
		parameters: z.object({
			kind: z.string().describe('The file\'s kind from a search_commons hit, e.g. "markdown-qop83g48".'),
			instruction: z.string().min(1).max(1_000).describe('What to change, as the user said it, e.g. "make the title green".'),
			room: z.string().optional().describe('The room name, e.g. "Sutando Triage". Omit for the current room.'),
			project: z.string().optional().describe('The folder id, only when a refusal said the file is in a folder project.'),
		}),
		execution: 'inline',
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + 2 * (2 * ROOM_COMMONS_TIMEOUT_MS + EDIT_PLAN_TIMEOUT_MS) + 1000,
		async execute(args) {
			return editProjectFile(args as { kind: string; instruction: string; room?: string; project?: string }, deps, plan);
		},
	};
}

/** The rule the voice model reads: a project file is changed through this tool, never by writing it out. */
export const EDIT_FILE_PROMPT_RULE = '- PROJECT FILES: a question about what a project file holds ("how many figures are there", "what does the abstract say") → search_commons, then read_page, and answer from it; edit_project_file only changes files and answers no questions. To change what a LaTeX or Typst project file says ("make the title green", "add an abstract", "改一下引言", even "write the paper"), including a figure the user asks you to draw ("add figure 3, draw a mushroom", "画一个蘑菇": it is drawn with the language\'s own shapes) → search_commons for the file, then edit_project_file with its kind and the user\'s instruction, at once. For a change, never read the file first and never write its text out yourself: the tool does both. After ok:true say what you changed ("I have …"). Never submit work for it — except a photo or generated image, or another file the project does not have: submit work once with the whole request (make it, put it in that room\'s project, change the file), never only "generate an image".';
