/**
 * Typst voice probe — the real gemini-3.8-live, the real voice prompt (buildInstructions), the real
 * ag2space-voice tools and the real relay work check, against a mock room holding one Typst project.
 * Each case is a fresh session: one user request, then the probe judges the tool path, the file
 * afterwards, the mock build, whether anything went to the core, and the time taken.
 *
 *   VARIANT=edit-tool (default) — edit_project_file is offered, as on #5696.
 *   VARIANT=generic             — no edit_project_file: changes go through run_room_action changes.apply with short edits.
 *
 * Run on a host with a Gemini key:
 *   set -a; . ./.env; set +a
 *   node node_modules/tsx/dist/cli.mjs tests/probes/typst-voice.probe.ts [case-id …]
 */
import { createHash } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { VoiceSession } from 'bodhi-realtime-agent';
import type { MainAgent, ToolDefinition } from 'bodhi-realtime-agent';
import { workTool } from '../../src/task-bridge.js';
import { RelayAgent, createVoiceTaskStore, relayAgentSubagentConfig } from '../../src/relay-agent.js';
import { createGeminiWorkCheck } from '../../src/work-request-check.js';
import { buildInstructions } from '../../src/voice-agent-config.js';
import {
	ROOM_READ_PROMPT_RULE, ROOM_TURN_CHECK_RULE, createListRoomsTool, createReadPageTool, createSearchCommonsTool, createReadRoomTool,
	roomIndexContextLines, type McpClient, type RoomIndex, type RoomRef,
} from '../../skills/ag2space-voice/room-read.js';
import { ROOM_ACTIONS_PROMPT_RULE, ROOM_ACTIONS_TURN_CHECK_RULE, createDescribeActionTool, createFindActionsTool, createRunActionTool } from '../../skills/ag2space-voice/room-actions.js';
import { EDIT_FILE_PROMPT_RULE, createEditFileTool } from '../../skills/ag2space-voice/edit-file.js';

const API_KEY = process.env.GEMINI_VOICE_API_KEY ?? process.env.GEMINI_API_KEY ?? '';
if (!API_KEY) { console.error('GEMINI_VOICE_API_KEY or GEMINI_API_KEY required'); process.exit(2); }
process.env.GEMINI_VOICE_API_KEY ||= API_KEY;
const MODEL = process.env.GEMINI_LIVE_MODEL || 'gemini-3.8-live';
const VARIANT = (process.env.VARIANT || 'edit-tool') as 'edit-tool' | 'generic';
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// ── The mock room ───────────────────────────────────────────────────────────
const ROOM: RoomRef = { room_id: '!gSLOfaslDLrlnahRpi:ag2.space', name: 'susan’s agents' };
const KIND = 'markdown-qop83g48';
const FOLDER = 'a499a739';
const MEDIA = [{ id: 'apl00001', name: 'apple.jpg' }, { id: 'ban00001', name: 'banana.jpg' }];
const ORIGINAL = `#set page(paper: "us-letter", margin: 1in)
#set text(font: "New Computer Modern", size: 11pt)

#align(center)[#text(size: 20pt, weight: "bold")[TypeScript MCP Test Project]]

= Abstract
We test how a voice assistant edits a Typst project through the AG2 Space MCP server.

= Introduction
Voice editing must be fast and must never leave the project broken.

= Results
#figure(image("apple.jpg", width: 40%), caption: [An apple.]) <fig1>

#figure(image("banana.jpg", width: 40%), caption: [A banana.]) <fig2>

#figure(circle(radius: 20pt, fill: red), caption: [A mushroom cap.]) <fig3>

= Conclusion
The pipeline works.
`;
const hashOf = (t: string) => `sha256:${createHash('sha256').update(t).digest('hex').slice(0, 16)}`;

/** A rough Typst check: every image the file names must be in the folder, brackets must balance. */
export function mockBuild(text: string): string[] {
	const errs: string[] = [];
	for (const m of text.matchAll(/image\(\s*"([^"]+)"/g)) if (!MEDIA.some((x) => x.name === m[1])) errs.push(`file not found (searched at ${m[1]})`);
	for (const [o, c] of [['(', ')'], ['[', ']'], ['{', '}']]) {
		const n = (s: string) => text.split(s).length - 1;
		if (n(o) !== n(c)) errs.push(`unclosed delimiter ${o}${c} (${n(o)} vs ${n(c)})`);
	}
	return errs;
}

type Call = { at: number; action: string; args: Record<string, unknown> };
function mockRoom() {
	let text = ORIGINAL;
	const calls: Call[] = [];
	const t0 = Date.now();
	const ok = (v: unknown) => ({ structuredContent: v });
	const fail = (code: string, message: string) => ({ isError: true, structuredContent: { code, message } });
	const describe = (action: string) => {
		const zero = /\.(read|list|search|latest|describe)$/.test(action);
		return ok({ action, effect: zero ? 'zero_effect' : 'mutation', risk: zero ? 'low' : 'medium', decision: 'allow', action_revision: `sha256:${action}`, idempotency_requirement: zero ? 'none' : 'required', title: action });
	};
	function run(action: string, a: Record<string, unknown>) {
		switch (action) {
			case 'room.commons.search': return ok({ hits: [{ surface: 'doc', kind: KIND, title: 'typst.typ', snippet: 'TypeScript MCP Test Project — a Typst project (folder typescript-test)', link: 'https://ag2.space/x' }] });
			case 'room.commons.pages.read':
				if (a.kind !== KIND && a.kind !== 'qop83g48') return fail('NOT_FOUND', 'no such page');
				return ok({ kind: KIND, surface: 'doc', title: 'typst.typ', text, offset: 0, next_offset: null, total_bytes: text.length, content_hash: hashOf(text), trust: 'untrusted_room_content' });
			case 'room.commons.pages.list': return ok({ pages: [{ kind: KIND, surface: 'doc', page_id: 'qop83g48', title: 'typst.typ', folder_id: FOLDER }] });
			case 'room.commons.folders.list': return ok({ folders: [{ id: FOLDER, name: 'typescript-test', main: 'qop83g48' }], placements: MEDIA.map((m) => ({ surface: 'media', item_id: m.id, folder_id: FOLDER })) });
			case 'room.media.list': return ok({ files: MEDIA.map((m) => ({ id: m.id, kind: 'image', name: m.name })) });
			case 'room.commons.typst.compile': case 'room.commons.typst.latest': {
				const errs = mockBuild(text);
				return ok({ status: 'completed', result: { build_id: 'b', status: errs.length ? 'errors' : 'ok', problems: errs.map((m) => ({ severity: 'error', message: m, file: 'typst.typ' })) } });
			}
			case 'room.commons.summon': return ok({ status: 'completed', result: { event_id: '$card' } });
			case 'room.commons.changes.list': return ok({ changes: [] });
			case 'room.commons.changes.apply': {
				const files = (a.files ?? []) as Array<{ page_id?: string; path?: string; kind?: string; base_hash?: string; text?: string; edits?: Array<{ old_text: string; new_text: string }> }>;
				const f = files[0];
				if (!f) return fail('INVALID_INPUT', 'name the file: send one of page_id, kind or path');
				if (f.base_hash !== hashOf(text)) return fail('stale_base', `the file changed since you read it; read it again (content_hash ${hashOf(text)})`);
				let next = text;
				if (typeof f.text === 'string') next = f.text;
				else for (const [i, e] of (f.edits ?? []).entries()) {
					const at = next.indexOf(e.old_text);
					if (at < 0) return fail('INVALID_INPUT', `edits[${i}].old_text does not occur in the file`);
					if (next.indexOf(e.old_text, at + 1) >= 0) return fail('INVALID_INPUT', `edits[${i}].old_text occurs more than once`);
					next = next.slice(0, at) + e.new_text + next.slice(at + e.old_text.length);
				}
				text = next;
				return ok({ status: 'completed', result: { change_seq: calls.length, files: [{ page_id: 'qop83g48', content_hash: hashOf(text) }] } });
			}
		}
		return fail('NOT_FOUND', `no such action ${action}`);
	}
	const client: McpClient = {
		async callTool(name, args) {
			const action = String(args.action ?? '');
			if (name === 'room.actions.search') {
				calls.push({ at: Date.now() - t0, action: 'room.actions.search', args });
				return ok({ items: ['room.commons.changes.apply', 'room.commons.pages.read', 'room.commons.typst.compile', 'room.commons.summon'].map((x) => ({ action: x, title: x, summary: x, target_kind: 'room', effect: /read|compile/.test(x) ? 'zero_effect' : 'mutation', risk: 'low' })) });
			}
			if (name === 'room.actions.describe') return describe(action);
			if (name === 'ag2.whoami') return ok({ actor: { id: '@bot:ag2.space' }, runtime: { owner_id: '@liususan091219:ag2.space' } });
			if (name === 'room.action.read' || name === 'room.action.execute') {
				calls.push({ at: Date.now() - t0, action, args: (args.arguments ?? {}) as Record<string, unknown> });
				return run(action, (args.arguments ?? {}) as Record<string, unknown>) as never;
			}
			return fail('NOT_FOUND', `no tool ${name}`) as never;
		},
		close() {},
	};
	const index: RoomIndex = { rooms: () => [ROOM], refresh: async () => [ROOM], fetchedAt: () => Date.now() };
	return { client, index, calls, text: () => text };
}

// ── The variant's tools and rules ────────────────────────────────────────────
const GENERIC_RULE = '- PROJECT FILES: a question about what a project file holds → search_commons, then read_page, and answer from it. To change what a LaTeX or Typst project file says ("make the title green", "add a sentence", "delete figure 2", "draw a mushroom as figure 4": draw it with Typst shapes such as circle, ellipse, rect, polygon, never an image file) → search_commons, read_page (it gives page_id and content_hash), then at once run_room_action room.commons.changes.apply with arguments_json {"project": the page_id, "message": what you changed, "files": [{"page_id": the page_id, "base_hash": the content_hash, "edits": [{"old_text": a short exact quote from the file, occurring once, "new_text": its replacement}]}]} — a few short edits, never the whole file. After ok:true say what you changed ("I have …"). A photo or generated image → submit work once with the whole request. Never submit work for a text change.';

function surfaceFor(room: ReturnType<typeof mockRoom>) {
	const deps = { client: () => room.client, currentRoom: () => ({ id: ROOM.room_id, name: ROOM.name }), index: room.index, log: (m: string) => console.log(`  ${m}`) };
	const tools: ToolDefinition[] = [
		createReadRoomTool(deps), createSearchCommonsTool(deps), createReadPageTool(deps), createListRoomsTool({ index: room.index }),
		createFindActionsTool(deps), createDescribeActionTool(deps), createRunActionTool(deps),
	];
	let rules = [ROOM_READ_PROMPT_RULE, ROOM_ACTIONS_PROMPT_RULE];
	if (VARIANT === 'edit-tool') { tools.push(createEditFileTool(deps)); rules = [ROOM_READ_PROMPT_RULE, EDIT_FILE_PROMPT_RULE, ROOM_ACTIONS_PROMPT_RULE]; }
	else {
		rules = [ROOM_READ_PROMPT_RULE.replace(/to change one, do not read it first: call edit_project_file with its kind/g, 'to change one, read it here, then changes.apply'), GENERIC_RULE,
			ROOM_ACTIONS_PROMPT_RULE.replace(/it goes through edit_project_file \(see PROJECT FILES\), never run_room_action room\.commons\.changes\.apply with text you wrote out yourself/, 'see PROJECT FILES')];
	}
	return { tools, promptRules: rules, turnCheckRules: [ROOM_TURN_CHECK_RULE, ROOM_ACTIONS_TURN_CHECK_RULE], contextLines: () => roomIndexContextLines(room.index), answeringTools: tools.map((t) => t.name) };
}

// ── One case ─────────────────────────────────────────────────────────────────
interface Case { id: string; say: string | string[]; expect: 'read' | 'change' | 'core' | 'open' | 'any'; judge: (r: Outcome) => string[] }
interface Outcome { tools: Array<{ at: number; name: string }>; mcp: Call[]; core: string[]; spoken: string; before: string; after: string; ms: number; firstToolMs: number | null; changeMs: number | null }

async function runCase(c: Case): Promise<Outcome> {
	const room = mockRoom();
	const surface = surfaceFor(room);
	const store = createVoiceTaskStore();
	const core: string[] = [];
	const userLines: string[] = [];
	const t0 = Date.now();
	const tools: Outcome['tools'] = [];
	const relay = new RelayAgent({
		store,
		submit: async (args) => { core.push(String(args.task ?? '')); const id = `task-${core.length}`; store.add(id, String(args.task ?? '')); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true }; },
		check: createGeminiWorkCheck({ apiKey: () => API_KEY, ownRules: surface.turnCheckRules }),
		// As voice-agent.ts: the check sees the user's own words, not only the task the voice model wrote.
		recentConversation: () => userLines.map((l) => `user: ${l}`).join('\n'),
		// As voice-agent.ts: the acknowledgement for a work call goes to the model as a system notice.
		notice: (text) => { sessionRef?.tryPublishSystemNotification(text); },
		log: (m) => console.log(`  ${m}`),
	});
	let sessionRef: VoiceSession | null = null;
	// A background tool (work) never reaches onToolCall: bodhi's own log line is the record of it.
	const origLog = console.log;
	console.log = (...a: unknown[]) => {
		const line = a.map(String).join(' ');
		const m = /Background task started: (\w+)/.exec(line);
		if (m) { tools.push({ at: Date.now() - t0, name: m[1] }); }
		origLog(...a);
	};
	const agent: MainAgent = {
		name: 'main',
		instructions: () => buildInstructions({
			resolveCurrentMode: () => ({ mode: 'active', marker: '' }) as never, isMeetingActive: () => false, googleSearch: false,
			resetSessionGates: () => {}, resetNoteViewingDebounce: () => {}, getRecentConversation: () => '', getSecondsSinceLastTurn: () => null,
			voiceSurface: surface,
		}, { standIdentityJson: '{}', voiceContext: '', repoUrl: '', voiceAgentContext: roomIndexContextLines(room.index).join('\n') }),
		tools: [workTool, ...surface.tools],
	} as MainAgent;
	const transcript: string[] = [];
	const session = new VoiceSession({
		sessionId: `typst_${c.id}_${Date.now()}`, userId: 'probe_user', apiKey: API_KEY, agents: [agent], initialAgent: 'main',
		subagentConfigs: { work: relayAgentSubagentConfig(relay) },
		model: createGoogleGenerativeAI({ apiKey: API_KEY })('gemini-2.5-flash'), geminiModel: MODEL,
		clientSender: { sendAudio: () => {}, sendJson: () => {} }, inputAudioTranscription: true, orchestrationMode: 'actor',
		responseWatchdogMs: 60_000, speechConfig: { voiceName: 'Puck' },
		hooks: { onToolCall: (e: { toolName: string }) => { tools.push({ at: Date.now() - t0, name: e.toolName }); console.log(`  +${Date.now() - t0}ms tool ${e.toolName}`); } },
	} as ConstructorParameters<typeof VoiceSession>[0]);
	sessionRef = session;
	await session.start();
	session.notifyClientConnected();
	const transport = (session as unknown as { transport: { onOutputTranscription?: (t: string) => void } }).transport;
	const prev = transport.onOutputTranscription;
	transport.onOutputTranscription = (t: string) => { transcript.push(t); prev?.(t); };
	await sleep(1500);
	const turns = Array.isArray(c.say) ? c.say : [c.say];
	let said = Date.now(), last = Date.now(), seen = 0;
	const firstSaid = said;
	for (const [i, line] of turns.entries()) {
	said = Date.now();
	console.log(`  +${said - t0}ms user: ${line}`);
	userLines.push(line);
	await session.injectText(line, { mode: 'live' });
	// Done when nothing new happened for 8 s (a tool call, a word, an MCP call), or after 90 s.
	last = Date.now(); seen = tools.length + transcript.length + room.calls.length + core.length;
	const before = seen;
	while (Date.now() - said < 90_000) {
		const now = tools.length + transcript.length + room.calls.length + core.length;
		if (now !== seen) { seen = now; last = Date.now(); }
		// A work call is done when the core got it, or the relay gave up; it waits for the user's turn first.
		const waitingOnCore = tools.some((t) => t.name === 'work') && !core.length && Date.now() - said < 30_000;
		if (Date.now() - last > 8_000 && seen > before && !waitingOnCore) break;
		await sleep(300);
	}
	if (seen === before) console.log(`  turn ${i + 1}: no reaction in 90 s`);
	}
	await session.close('user_hangup').catch(() => {});
	console.log = origLog;
	const apply = room.calls.find((x) => x.action === 'room.commons.changes.apply');
	return {
		tools, mcp: room.calls, core, spoken: transcript.join(''), before: ORIGINAL, after: room.text(),
		ms: last - said, firstToolMs: tools.length ? tools[0].at - (firstSaid - t0) : null, changeMs: apply ? apply.at - (firstSaid - t0) : null,
	};
}

// ── The cases ────────────────────────────────────────────────────────────────
const figures = (t: string) => (t.match(/#figure\(/g) ?? []).length;
const common = (r: Outcome, c: Case): string[] => {
	const f: string[] = [];
	const errs = mockBuild(r.after);
	if (errs.length) f.push(`build broken: ${errs.join('; ')}`);
	if (c.expect !== 'core' && c.expect !== 'any' && r.core.length) f.push(`sent to the core: ${r.core.join(' | ').slice(0, 160)}`);
	if (c.expect === 'core' && !r.core.length) f.push('not sent to the core');
	if (c.expect === 'read' && r.after !== r.before) f.push('a question changed the file');
	if (c.expect === 'change' && r.after === r.before) f.push('the file was not changed');
	if (!r.spoken.trim()) f.push('nothing was said');
	return f;
};
const CASES: Case[] = [
	{ id: 'A1', say: 'How many figures are there in typst.typ in the TypeScript test project?', expect: 'read', judge: (r) => /\b(three|3)\b/i.test(r.spoken) ? [] : [`answer not "three": ${r.spoken.slice(0, 120)}`] },
	{ id: 'A2', say: 'TypeScript test 项目里的 typst.typ 有几个 figure？', expect: 'read', judge: (r) => /(三|3|three)/i.test(r.spoken) ? [] : [`answer not 3: ${r.spoken.slice(0, 120)}`] },
	{ id: 'A3', say: 'What does the abstract of typst.typ say?', expect: 'read', judge: (r) => /voice|typst|MCP|edit/i.test(r.spoken) ? [] : [`not from the file: ${r.spoken.slice(0, 120)}`] },
	{ id: 'B1', say: 'In the TypeScript test project, make the title of typst.typ green.', expect: 'change', judge: (r) => /green[\s\S]{0,80}TypeScript MCP Test Project|TypeScript MCP Test Project[\s\S]{0,5}\]?[\s\S]{0,40}green/i.test(r.after) || /fill:\s*green/.test(r.after) ? [] : ['title not green'] },
	{ id: 'B2', say: '把 TypeScript test 项目里 typst.typ 的标题改成蓝色', expect: 'change', judge: (r) => /fill:\s*blue/.test(r.after) ? [] : ['title not blue'] },
	{ id: 'B3', say: 'Add the sentence "This is a great paper." at the end of the abstract in typst.typ.', expect: 'change', judge: (r) => /This is a great paper\.[\s\S]*= Introduction/.test(r.after) ? [] : ['sentence not at the end of the abstract'] },
	{ id: 'C1', say: 'Add a figure four to typst.typ and draw a puppy in it.', expect: 'change', judge: (r) => figures(r.after) === 4 && !/image\("[^"]*(puppy|dog)/i.test(r.after) ? [] : [`figures ${figures(r.after)}; drawn with an image file?`] },
	{ id: 'C2', say: '在 typst.typ 里再画一个 figure，画一个房子', expect: 'change', judge: (r) => figures(r.after) === 4 ? [] : [`figures ${figures(r.after)}`] },
	{ id: 'F1', say: 'Delete figure 2 from typst.typ in the TypeScript test project.', expect: 'change', judge: (r) => !/banana/i.test(r.after) && figures(r.after) === 2 ? [] : [`banana still there or figures ${figures(r.after)}`] },
	{ id: 'F2', say: '把 typst.typ 里的 figure 1 删掉', expect: 'change', judge: (r) => !/apple/i.test(r.after) && figures(r.after) === 2 ? [] : [`apple still there or figures ${figures(r.after)}`] },
	{ id: 'D1', say: 'Add a photo of a pineapple as a new figure in typst.typ in the TypeScript test project.', expect: 'core', judge: (r) => r.core.some((t) => /pineapple/i.test(t) && /(typst|TypeScript|project|figure)/i.test(t)) ? [] : [`core task lacks the whole request: ${r.core.join(' | ').slice(0, 160)}`] },
	{ id: 'H1', say: 'Open typst.typ in the TypeScript test project.', expect: 'open', judge: (r) => r.mcp.some((x) => x.action === 'room.commons.summon') ? [] : ['no summon'] },
	// Susan's own words from the live runs on 10-18.
	{ id: 'C3', say: '在 TypeScript test 项目的 typst.typ 里再画一个 figure，是一只小狗', expect: 'change', judge: (r) => figures(r.after) === 4 ? [] : [`figures ${figures(r.after)}`] },
	{ id: 'C4', say: 'In typst.typ in the TypeScript test project, can you draw another figure? Which is a puppy.', expect: 'change', judge: (r) => figures(r.after) === 4 ? [] : [`figures ${figures(r.after)}`] },
	// No "draw" and no picture in the project: a generated picture is core work by design, or a drawing; never nothing.
	{ id: 'E1', say: 'Add a figure of an orange to typst.typ in the TypeScript test project.', expect: 'any', judge: (r) => r.after === r.before && !r.core.some((t) => /orange/i.test(t)) ? ['neither drawn nor sent to the core'] : [] },
	{ id: 'M1', say: [
		'How many figures are in typst.typ in the TypeScript test project?',
		'Make the title of typst.typ green.',
		'Add the sentence "This is a great paper." at the end of the abstract.',
		'Now draw a mushroom as a new figure.',
		'Delete figure 2.',
		'How many figures are there now?',
	], expect: 'change', judge: (r) => {
		const f: string[] = [];
		if (!/fill:\s*green/.test(r.after)) f.push('title not green');
		if (!/This is a great paper\./.test(r.after)) f.push('sentence missing');
		if (/banana/i.test(r.after)) f.push('figure 2 not deleted');
		if (figures(r.after) !== 3) f.push(`figures ${figures(r.after)}, expected 3`);
		if (!/\b(three|3)\b[^.]*\.?\s*$/i.test(r.spoken.trim()) && !/(three|3) figures/i.test(r.spoken.slice(-200))) f.push(`last answer not "three": ${r.spoken.slice(-120)}`);
		return f;
	} },
];

async function main() {
	const REPEAT = Number(process.env.REPEAT || 1);
	const only = process.argv.slice(2);
	const results: Array<{ id: string; pass: boolean; failures: string[]; tools: string[]; ms: number; firstToolMs: number | null; changeMs: number | null; spoken: string; core: string[] }> = [];
	for (let k = 0; k < REPEAT; k++) for (const c of CASES.filter((x) => !only.length || only.includes(x.id))) {
		console.log(`\n════ ${c.id} [${VARIANT}] ${c.say}`);
		let failures: string[]; let r: Outcome | null = null;
		try { r = await runCase(c); failures = [...common(r, c), ...c.judge(r)]; }
		catch (err) { failures = [`threw: ${err instanceof Error ? err.message : String(err)}`]; }
		const row = { id: c.id, pass: failures.length === 0, failures, tools: r?.tools.map((t) => t.name) ?? [], ms: r?.ms ?? -1, firstToolMs: r?.firstToolMs ?? null, changeMs: r?.changeMs ?? null, spoken: r?.spoken.slice(0, 300) ?? '', core: r?.core ?? [] };
		results.push(row);
		console.log(`${row.pass ? 'PASS' : 'FAIL'} ${c.id} tools=[${row.tools.join(',')}] first=${row.firstToolMs}ms change=${row.changeMs}ms done=${row.ms}ms ${failures.join(' / ')}`);
		console.log(`  said: ${row.spoken.slice(0, 200)}`);
	}
	mkdirSync('logs', { recursive: true });
	const out = `logs/typst-voice-probe-${VARIANT}-${Date.now()}.json`;
	writeFileSync(out, JSON.stringify(results, null, 1));
	console.log(`\n${results.filter((r) => r.pass).length}/${results.length} passed (${VARIANT}); log ${out}`);
	process.exit(0);
}
main();
