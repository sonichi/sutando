/**
 * navigate_ui — the voice agent moves the desktop by voice.
 *
 * Pins the wire contract shared with the desktop client (frame builders and
 * parsers round-trip, bounds hold; the `capabilities` field of session.context
 * and its parser), the capability gate (an attached client that never said
 * `ui.navigate` gets `unsupported` at once and no frame), the room-without-
 * query short-circuit, the tool's every outcome: resolved by the client's
 * reply, timed out cleanly, `unsupported` with no client, failed fast when the
 * client leaves, a stray reply ignored — and the voice-agent / inline-tools
 * wiring by source pin, so the seam cannot silently drop out.
 */
import { describe, it, beforeEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

// The plugin is optional: with skills/ag2space-voice/ removed this file skips.
const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'tools.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const plugin = (f: string) => import(pathToFileURL(join(PLUGIN_DIR, f)).href);
const {
	UI_NAVIGATE_TYPE, UI_NAVIGATED_TYPE, UI_NAVIGATE_CAPABILITY, UI_NAVIGATE_QUERY_MAX_CHARS, UI_NAVIGATED_NAME_MAX_CHARS, UI_NAVIGATED_MAX_CANDIDATES,
	buildUiNavigateFrame, parseUiNavigateFrame, buildUiNavigatedFrame, parseUiNavigatedFrame,
} = await plugin('navigate-protocol.ts');
const { SESSION_CONTEXT_TYPE, SESSION_CONTEXT_CAPABILITY_MAX_CHARS, SESSION_CONTEXT_MAX_CAPABILITIES, parseSessionContextCapabilities } = await plugin('session-context.ts');
const {
	navigateUi, navigateUiTool, navigateUiAvailable, installVoiceNavigateClient, resolveUiNavigated, failPendingNavigations, pendingNavigationCount,
	NAVIGATE_UI_TIMEOUT_MS, NAVIGATE_UI_UNSUPPORTED_MESSAGE, NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE, NAVIGATE_UI_ROOM_QUERY_MISSING_MESSAGE, NAVIGATION_PROMPT_RULE,
} = await plugin('navigate.ts');
const pluginTools = await plugin('tools.ts');
type SessionContextFrame = Record<string, unknown> & { version: number };
/** The frame the AG2 Space client sends (webapp voiceRoomBinding.ts), before capabilities are added. */
const buildSessionContextFrame = (ctx: { roomId: string; roomName?: string } | null): SessionContextFrame => ctx
	? { type: 'session.context', version: 1, room_id: ctx.roomId, room_name: ctx.roomName ?? null, surface: 'room' }
	: { type: 'session.context', version: 1, room_id: null, room_name: null, surface: 'dm' };

const src = (rel: string) => readFileSync(fileURLToPath(new URL(`../src/${rel}`, import.meta.url)), 'utf8');

describe('session.context capabilities — how the client says it speaks ui.navigate', () => {
	it('the capability name is the frame type it answers, and the field is additive on the v1 frame', () => {
		assert.equal(UI_NAVIGATE_CAPABILITY, UI_NAVIGATE_TYPE);
		const frame: SessionContextFrame = { ...buildSessionContextFrame(null), capabilities: [UI_NAVIGATE_CAPABILITY] };
		assert.equal(frame.version, 1, 'still v1: a client that omits the field is a valid v1 sender');
		assert.deepEqual(parseSessionContextCapabilities(frame), ['ui.navigate']);
		assert.deepEqual(parseSessionContextCapabilities(JSON.parse(JSON.stringify(frame))), ['ui.navigate']);
	});

	it('absent, null or malformed capabilities read as none — never as an error', () => {
		assert.deepEqual(parseSessionContextCapabilities(buildSessionContextFrame(null)), []);
		assert.deepEqual(parseSessionContextCapabilities({ type: SESSION_CONTEXT_TYPE, version: 1, surface: 'dm', capabilities: null }), []);
		assert.deepEqual(parseSessionContextCapabilities({ type: SESSION_CONTEXT_TYPE, version: 1, surface: 'dm', capabilities: 'ui.navigate' }), []);
		assert.deepEqual(parseSessionContextCapabilities({ type: SESSION_CONTEXT_TYPE, version: 1, surface: 'dm', capabilities: [42, null, {}, '  '] }), []);
	});

	it('undefined for anything that is not a session.context frame', () => {
		assert.equal(parseSessionContextCapabilities({ type: UI_NAVIGATED_TYPE, capabilities: ['ui.navigate'] }), undefined);
		assert.equal(parseSessionContextCapabilities(null), undefined);
		assert.equal(parseSessionContextCapabilities('session.context'), undefined);
		assert.equal(parseSessionContextCapabilities({}), undefined);
	});

	it('names are trimmed, flattened, deduplicated and bounded', () => {
		const caps = parseSessionContextCapabilities({
			type: SESSION_CONTEXT_TYPE, version: 1, surface: 'dm',
			capabilities: [' ui.navigate ', 'ui.navigate', 'a\nb', 'x'.repeat(200), ...[...Array(50)].map((_, i) => `c${i}`)],
		})!;
		assert.equal(caps[0], 'ui.navigate');
		assert.equal(caps[1], 'a b');
		assert.equal(caps[2].length, SESSION_CONTEXT_CAPABILITY_MAX_CHARS);
		assert.equal(caps.length, SESSION_CONTEXT_MAX_CAPABILITIES);
		assert.equal(new Set(caps).size, caps.length);
	});
});

describe('ui.navigate / ui.navigated frame contract', () => {
	it('request: builder → parser round trip keeps every field', () => {
		const f = buildUiNavigateFrame('r1', 'room', 'GTM in Investors');
		assert.deepEqual(f, { type: UI_NAVIGATE_TYPE, version: 1, request_id: 'r1', target: 'room', query: 'GTM in Investors' });
		assert.deepEqual(parseUiNavigateFrame(JSON.parse(JSON.stringify(f))), f);
	});

	it('request: query is dropped for dm/home, flattened and capped for room', () => {
		assert.equal(buildUiNavigateFrame('r', 'dm', 'ignored').query, undefined);
		assert.equal(buildUiNavigateFrame('r', 'home', 'ignored').query, undefined);
		assert.equal(buildUiNavigateFrame('r', 'room', '  GTM\nInvestors ').query, 'GTM Investors');
		assert.equal(buildUiNavigateFrame('r', 'room', 'x'.repeat(500)).query?.length, UI_NAVIGATE_QUERY_MAX_CHARS);
		assert.equal(buildUiNavigateFrame('r', 'room', '   ').query, undefined);
	});

	it('request parser: rejects other types, other versions, bad targets, missing ids', () => {
		assert.equal(parseUiNavigateFrame({ type: 'session.context', version: 1, request_id: 'r', target: 'dm' }), null);
		assert.equal(parseUiNavigateFrame({ type: UI_NAVIGATE_TYPE, version: 2, request_id: 'r', target: 'dm' }), null);
		assert.equal(parseUiNavigateFrame({ type: UI_NAVIGATE_TYPE, version: 1, request_id: 'r', target: 'space' }), null);
		assert.equal(parseUiNavigateFrame({ type: UI_NAVIGATE_TYPE, version: 1, request_id: '', target: 'dm' }), null);
		assert.equal(parseUiNavigateFrame(null), null);
		assert.equal(parseUiNavigateFrame('ui.navigate'), null);
	});

	it('reply: ok round trip with room fields', () => {
		const f = buildUiNavigatedFrame('r1', { room_id: '!abc:ag2.space', room_name: 'GTM' });
		assert.deepEqual(f, { type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r1', ok: true, room_id: '!abc:ag2.space', room_name: 'GTM' });
		assert.deepEqual(parseUiNavigatedFrame(JSON.parse(JSON.stringify(f))), f);
	});

	it('reply: error round trip with candidates; ok is derived from error', () => {
		const f = buildUiNavigatedFrame('r2', { error: 'ambiguous', candidates: ['GTM', 'GTM planning'] });
		assert.deepEqual(f, { type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r2', ok: false, error: 'ambiguous', candidates: ['GTM', 'GTM planning'] });
		assert.deepEqual(parseUiNavigatedFrame(JSON.parse(JSON.stringify(f))), f);
		assert.deepEqual(buildUiNavigatedFrame('r3', { error: 'not_found' }), { type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r3', ok: false, error: 'not_found' });
	});

	it('reply: names and candidates are flattened, capped and bounded in count', () => {
		const f = buildUiNavigatedFrame('r', {
			room_name: 'a\nb ' + 'x'.repeat(300),
			candidates: [...Array(20)].map((_, i) => ` c${i}\n`).concat(['', 42 as unknown as string]),
		});
		assert.equal(f.room_name?.length, UI_NAVIGATED_NAME_MAX_CHARS);
		assert.ok(f.room_name?.startsWith('a b '));
		assert.equal(f.candidates?.length, UI_NAVIGATED_MAX_CANDIDATES);
		assert.deepEqual(f.candidates?.slice(0, 2), ['c0', 'c1']);
	});

	it('reply parser: a claimed ok with an error is not ok; unknown errors read as unsupported; not-ok without error too', () => {
		const lying = parseUiNavigatedFrame({ type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r', ok: true, error: 'not_found' });
		assert.equal(lying?.ok, false);
		assert.equal(lying?.error, 'not_found');
		const odd = parseUiNavigatedFrame({ type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r', ok: false, error: 'exploded' });
		assert.equal(odd?.error, 'unsupported');
		const bare = parseUiNavigatedFrame({ type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r', ok: false });
		assert.equal(bare?.error, 'unsupported');
		assert.equal(parseUiNavigatedFrame({ type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r' })?.ok, false);
		assert.equal(parseUiNavigatedFrame({ type: UI_NAVIGATE_TYPE, version: 1, request_id: 'r', ok: true }), null);
		assert.equal(parseUiNavigatedFrame({ type: UI_NAVIGATED_TYPE, version: 1, ok: true }), null);
		assert.equal(parseUiNavigatedFrame({ type: UI_NAVIGATED_TYPE, version: 1, request_id: 'r', ok: true, candidates: 'GTM' })?.candidates, undefined);
	});
});

describe('navigate_ui tool', () => {
	const sent: Record<string, unknown>[] = [];
	let attached = true;
	let capabilities = new Set<string>([UI_NAVIGATE_CAPABILITY]);

	beforeEach(() => {
		sent.length = 0;
		attached = true;
		capabilities = new Set([UI_NAVIGATE_CAPABILITY]);
		failPendingNavigations();
		installVoiceNavigateClient({ attached: () => attached, supports: (c) => capabilities.has(c), send: (f) => { sent.push(f); } });
	});

	it('is declared as an inline tool with the shared target enum', () => {
		assert.equal(navigateUiTool.name, 'navigate_ui');
		assert.equal(navigateUiTool.execution, 'inline');
		assert.ok((navigateUiTool.timeout ?? 0) > NAVIGATE_UI_TIMEOUT_MS, 'bodhi must not kill the tool before its own wait ends');
		assert.equal(navigateUiTool.parameters.safeParse({ target: 'room', query: 'GTM' }).success, true);
		assert.equal(navigateUiTool.parameters.safeParse({ target: 'dm' }).success, true);
		assert.equal(navigateUiTool.parameters.safeParse({ target: 'space', query: 'GTM' }).success, false);
	});

	it('sends ui.navigate and resolves with the client\'s ok reply', async () => {
		const p = navigateUi({ target: 'room', query: 'GTM in Investors' }, { requestId: 'req-ok' });
		assert.equal(sent.length, 1);
		assert.deepEqual(sent[0], { type: UI_NAVIGATE_TYPE, version: 1, request_id: 'req-ok', target: 'room', query: 'GTM in Investors' });
		assert.equal(pendingNavigationCount(), 1);
		assert.equal(resolveUiNavigated(buildUiNavigatedFrame('req-ok', { room_id: '!gtm:ag2.space', room_name: 'GTM' })), true);
		assert.deepEqual(await p, { ok: true, target: 'room', room_id: '!gtm:ag2.space', room_name: 'GTM' });
		assert.equal(pendingNavigationCount(), 0);
	});

	it('goes through the tool\'s execute the same way (dm target, no query on the wire)', async () => {
		const p = navigateUiTool.execute({ target: 'dm', query: 'ignored' }, {} as never) as Promise<{ ok: boolean; target: string }>;
		assert.equal((sent[0] as { query?: string }).query, undefined);
		resolveUiNavigated({ ...buildUiNavigatedFrame(String(sent[0].request_id), {}), extra: 'ignored' });
		assert.deepEqual(await p, { ok: true, target: 'dm' });
	});

	it('returns ambiguous with the candidates and a question for the model', async () => {
		const p = navigateUi({ target: 'room', query: 'GTM' }, { requestId: 'req-amb' });
		resolveUiNavigated(buildUiNavigatedFrame('req-amb', { error: 'ambiguous', candidates: ['GTM', 'GTM planning'] }));
		const r = await p;
		assert.equal(r.ok, false);
		if (r.ok) return;
		assert.equal(r.error, 'ambiguous');
		assert.deepEqual(r.candidates, ['GTM', 'GTM planning']);
		assert.match(r.message, /GTM, GTM planning/);
		assert.match(r.message, /which one/i);
	});

	it('returns not_found naming the spoken query', async () => {
		const p = navigateUi({ target: 'room', query: 'Marketing' }, { requestId: 'req-nf' });
		resolveUiNavigated(buildUiNavigatedFrame('req-nf', { error: 'not_found' }));
		const r = await p;
		assert.equal(r.ok, false);
		if (r.ok) return;
		assert.equal(r.error, 'not_found');
		assert.match(r.message, /"Marketing"/);
	});

	it('times out cleanly and forgets the request', async () => {
		const p = navigateUi({ target: 'home' }, { requestId: 'req-slow', timeoutMs: 20 });
		const r = await p;
		assert.equal(r.ok, false);
		if (r.ok) return;
		assert.equal(r.error, 'timeout');
		assert.equal(pendingNavigationCount(), 0);
		assert.equal(resolveUiNavigated(buildUiNavigatedFrame('req-slow', {})), false, 'a late reply matches nothing');
	});

	it('returns unsupported at once with no client installed, and with a detached client', async () => {
		installVoiceNavigateClient(null);
		const none = await navigateUi({ target: 'dm' });
		assert.deepEqual(none, { ok: false, target: 'dm', error: 'unsupported', message: NAVIGATE_UI_UNSUPPORTED_MESSAGE });
		installVoiceNavigateClient({ attached: () => false, supports: () => true, send: (f) => { sent.push(f); } });
		const gone = await navigateUi({ target: 'room', query: 'GTM' });
		assert.equal(gone.ok, false);
		assert.equal(sent.length, 0, 'nothing is sent to a client that is not there');
		assert.match(NAVIGATE_UI_UNSUPPORTED_MESSAGE, /desktop app/);
	});

	it('capability gate: an attached client that never announced ui.navigate gets unsupported at once, and no frame', async () => {
		capabilities = new Set();
		const started = Date.now();
		const r = await navigateUi({ target: 'room', query: 'GTM in Investors' }, { requestId: 'req-old', timeoutMs: 5000 });
		assert.ok(Date.now() - started < 1000, 'no wait on a reply the client cannot give');
		assert.deepEqual(r, { ok: false, target: 'room', error: 'unsupported', message: NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE });
		assert.equal(sent.length, 0, 'nothing on the wire');
		assert.equal(pendingNavigationCount(), 0);
		assert.match(NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE, /update it/);
		// Other announced capabilities do not stand in for this one.
		capabilities = new Set(['something.else']);
		assert.equal((await navigateUi({ target: 'dm' })).ok, false);
		assert.equal(sent.length, 0);
	});

	it('capability gate: with ui.navigate announced the frame goes out and the reply settles it', async () => {
		capabilities = new Set([UI_NAVIGATE_CAPABILITY, 'other']);
		const p = navigateUi({ target: 'dm' }, { requestId: 'req-cap' });
		assert.equal(sent.length, 1);
		assert.deepEqual(sent[0], buildUiNavigateFrame('req-cap', 'dm'));
		resolveUiNavigated(buildUiNavigatedFrame('req-cap', {}));
		assert.deepEqual(await p, { ok: true, target: 'dm' });
	});

	it('target room with no query is not_found at once, with a prompt for the room, and no frame', async () => {
		for (const query of [undefined, '', '   ', '\n']) {
			const r = await navigateUi({ target: 'room', query }, { timeoutMs: 5000 });
			assert.deepEqual(r, { ok: false, target: 'room', error: 'not_found', message: NAVIGATE_UI_ROOM_QUERY_MISSING_MESSAGE }, `query=${JSON.stringify(query)}`);
		}
		assert.equal(sent.length, 0, 'nothing on the wire');
		assert.equal(pendingNavigationCount(), 0);
		assert.match(NAVIGATE_UI_ROOM_QUERY_MISSING_MESSAGE, /which room/);
		// The same through the tool's execute (the model's path).
		const viaTool = await (navigateUiTool.execute({ target: 'room' }, {} as never) as Promise<{ ok: boolean; error?: string }>);
		assert.equal(viaTool.ok, false);
		assert.equal(viaTool.error, 'not_found');
		assert.equal(sent.length, 0);
		// dm / home never needed a query.
		const home = navigateUi({ target: 'home' }, { requestId: 'req-home' });
		assert.equal(sent.length, 1);
		resolveUiNavigated(buildUiNavigatedFrame('req-home', {}));
		assert.equal((await home).ok, true);
	});

	it('a send that throws answers unsupported rather than waiting', async () => {
		installVoiceNavigateClient({ attached: () => true, supports: () => true, send: () => { throw new Error('socket closed'); } });
		const r = await navigateUi({ target: 'dm' }, { timeoutMs: 5000 });
		assert.equal(r.ok, false);
		if (r.ok) return;
		assert.equal(r.error, 'unsupported');
		assert.equal(pendingNavigationCount(), 0);
	});

	it('failPendingNavigations settles every in-flight request as unsupported (client left)', async () => {
		const a = navigateUi({ target: 'dm' }, { requestId: 'a', timeoutMs: 5000 });
		const b = navigateUi({ target: 'home' }, { requestId: 'b', timeoutMs: 5000 });
		assert.equal(pendingNavigationCount(), 2);
		assert.equal(failPendingNavigations(), 2);
		const [ra, rb] = await Promise.all([a, b]);
		assert.equal(ra.ok, false);
		assert.equal(rb.ok, false);
		if (!ra.ok) assert.equal(ra.error, 'unsupported');
		if (!rb.ok) assert.equal(rb.error, 'unsupported');
		assert.equal(pendingNavigationCount(), 0);
	});

	it('ignores frames that are not ui.navigated and replies for unknown ids', async () => {
		const p = navigateUi({ target: 'dm' }, { requestId: 'live', timeoutMs: 200 });
		assert.equal(resolveUiNavigated({ type: 'session.context', version: 1, room_id: null }), false);
		assert.equal(resolveUiNavigated(buildUiNavigatedFrame('someone-else', {})), false);
		assert.equal(pendingNavigationCount(), 1, 'the live request is untouched');
		await delay(5);
		assert.equal(resolveUiNavigated(buildUiNavigatedFrame('live', {})), true);
		assert.equal((await p).ok, true);
	});
});

// The plugin's setup() is the wiring: driven here through a fake host ctx.
// The client half ships separately (webapp voiceNavigate.ts / voiceRoomBinding.ts), so the
// contract is pinned here as literal values, not by importing the other side.
describe('wire contract literals', () => {
	it('frame types, version, targets, errors and the capability name', () => {
		assert.deepEqual([UI_NAVIGATE_TYPE, UI_NAVIGATED_TYPE, UI_NAVIGATE_CAPABILITY, SESSION_CONTEXT_TYPE], ['ui.navigate', 'ui.navigated', 'ui.navigate', 'session.context']);
		assert.deepEqual(buildUiNavigateFrame('r1', 'room', 'GTM in Investors'), { type: 'ui.navigate', version: 1, request_id: 'r1', target: 'room', query: 'GTM in Investors' });
		for (const target of ['dm', 'room', 'home']) assert.equal(parseUiNavigateFrame({ type: 'ui.navigate', version: 1, request_id: 'r', target })?.target, target);
		assert.deepEqual(parseUiNavigatedFrame({ type: 'ui.navigated', version: 1, request_id: 'r1', ok: true, room_id: '!a:s', room_name: 'GTM' }),
			{ type: 'ui.navigated', version: 1, request_id: 'r1', ok: true, room_id: '!a:s', room_name: 'GTM' });
		for (const error of ['not_found', 'ambiguous', 'unsupported']) {
			assert.equal(parseUiNavigatedFrame({ type: 'ui.navigated', version: 1, request_id: 'r1', ok: false, error })?.error, error);
		}
		assert.deepEqual(parseUiNavigatedFrame({ type: 'ui.navigated', version: 1, request_id: 'r1', ok: false, error: 'ambiguous', candidates: ['GTM', 'GTM planning'] })?.candidates, ['GTM', 'GTM planning']);
	});
});

describe('navigate_ui wiring — setup(ctx) over the plugin seam', () => {
	const host = () => {
		const frameHandlers: Array<(f: Record<string, unknown>) => unknown> = [];
		const goneHandlers: Array<() => void> = [];
		const sent: Record<string, unknown>[] = [];
		const state = { attached: true };
		const ctx = {
			session: {}, injectText: () => {}, injectContext: () => {},
			clientAttached: () => state.attached,
			sendClientFrame: (f: Record<string, unknown>) => { if (!state.attached) return false; sent.push(f); return true; },
			onClientFrame: (h: (f: Record<string, unknown>) => unknown) => { frameHandlers.push(h); },
			onClientDisconnected: (h: () => void) => { goneHandlers.push(h); },
			setVoiceSessionOrigin: () => {}, getVoiceSessionOrigin: () => null, setVoiceTaskOriginResolver: () => {},
		};
		pluginTools.setup(ctx);
		return { sent, state, frame: (f: Record<string, unknown>) => frameHandlers.map(h => h(f)), gone: () => goneHandlers.forEach(h => h()) };
	};
	const dmFrame = (capabilities?: string[]) => ({ ...buildSessionContextFrame(null), ...(capabilities ? { capabilities } : {}) });
	const navFrames = (sent: Record<string, unknown>[]) => sent.filter(f => f.type === UI_NAVIGATE_TYPE);

	it('a client that announced ui.navigate is sent the frame and its reply settles the call', async () => {
		const h = host();
		assert.deepEqual(h.frame(dmFrame(['ui.navigate'])), [true], 'the session.context frame is claimed');
		const p = navigateUi({ target: 'home' }, { requestId: 'wired', timeoutMs: 500 });
		await delay(5);
		assert.deepEqual(navFrames(h.sent), [{ type: 'ui.navigate', version: 1, request_id: 'wired', target: 'home' }]);
		assert.deepEqual(h.frame(buildUiNavigatedFrame('wired', {})), [true]);
		assert.deepEqual(await p, { ok: true, target: 'home' });
	});

	it('an attached client that never announced it hears "update" at once and is sent nothing; none attached hears "no client"', async () => {
		const h = host();
		h.frame(dmFrame());
		const outdated = await navigateUi({ target: 'dm' }, { timeoutMs: 500 });
		assert.deepEqual([outdated.ok, (outdated as { message: string }).message], [false, NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE]);
		h.state.attached = false;
		const none = await navigateUi({ target: 'dm' }, { timeoutMs: 500 });
		assert.equal((none as { message: string }).message, NAVIGATE_UI_UNSUPPORTED_MESSAGE);
		assert.deepEqual(navFrames(h.sent), []);
	});

	it('the client leaving clears its capabilities and fails the in-flight move', async () => {
		const h = host();
		h.frame(dmFrame(['ui.navigate']));
		const p = navigateUi({ target: 'home' }, { requestId: 'leaving', timeoutMs: 2000 });
		await delay(5);
		h.gone();
		assert.equal((await p).ok, false);
		assert.equal(pendingNavigationCount(), 0);
		const after = await navigateUi({ target: 'home' }, { timeoutMs: 200 });
		assert.equal((after as { message: string }).message, NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE, 'the next client announces its own list');
	});

	it('a frame that did not go out answers at once', async () => {
		installVoiceNavigateClient({ attached: () => true, supports: () => true, send: () => false });
		const r = await navigateUi({ target: 'home' }, { timeoutMs: 2000 });
		assert.deepEqual([r.ok, (r as { message: string }).message], [false, NAVIGATE_UI_UNSUPPORTED_MESSAGE]);
		assert.equal(pendingNavigationCount(), 0);
	});

	it('a host without the seam gets no wiring and no throw', () => {
		assert.doesNotThrow(() => pluginTools.setup({ session: {}, injectText: () => {} }));
	});

	it('the core carries none of it', () => {
		for (const f of ['voice-agent.ts', 'voice-agent-config.ts', 'inline-tools.ts']) {
			assert.doesNotMatch(src(f), /navigate_ui|navigateUi|ui\.navigate|NAVIGATION:/, f);
		}
		assert.ok(!existsSync(fileURLToPath(new URL('../src/voice-navigate.ts', import.meta.url))));
	});
});

describe('navigate_ui exposure — declared only where a client can answer it', () => {
	const ctx = (voiceSurface?: unknown) => ({
		resolveCurrentMode: () => ({ marker: '', isMeeting: false, isPresenter: false }),
		isMeetingActive: () => false,
		googleSearch: false,
		voiceSurface,
		resetSessionGates: () => {},
		resetNoteViewingDebounce: () => {},
		getRecentConversation: () => '',
		getSecondsSinceLastTurn: () => null,
	});
	const OVERRIDES = { standIdentityJson: '{}', voiceContext: '', repoUrl: 'https://example.invalid', voiceAgentContext: '' };
	const surfaceWith = (env: Record<string, string>) => {
		const home = mkdtempSync(join(tmpdir(), 'sutando-navigate-surface-'));
		const saved = { ...process.env };
		process.env.CLAUDE_CONFIG_DIR = home;
		for (const k of ['REMOTE_TASK_TOKEN', 'AG2_REMOTE_TOKEN']) delete process.env[k];
		Object.assign(process.env, env);
		try { return pluginTools.voiceSurface(); } finally {
			for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
			Object.assign(process.env, saved);
			rmSync(home, { recursive: true, force: true });
		}
	};

	it('the shared tool tables never carry it: phone calls and other installs get no such tool', async () => {
		const { inlineTools, ownerOnlyTools, anyCallerTools } = await import('../src/inline-tools.js');
		for (const table of [inlineTools, ownerOnlyTools, anyCallerTools]) {
			assert.ok(!table.some((t: { name: string }) => t.name === 'navigate_ui'));
		}
		assert.deepEqual(pluginTools.tools, [], 'the skill adds nothing to the shared tables');
	});

	it('without the channel the skill contributes no tool and no rule; with it, both reach the prompt', async () => {
		const { buildInstructions } = await import('../src/voice-agent-config.js');
		const offSurface = surfaceWith({});
		assert.deepEqual([offSurface.tools, offSurface.promptRules], [[], []]);
		for (const off of [buildInstructions(ctx(offSurface) as never, OVERRIDES), buildInstructions(ctx() as never, OVERRIDES)]) {
			assert.ok(!off.includes('NAVIGATION:'));
			assert.ok(!off.includes('navigate_ui'));
		}
		const onSurface = surfaceWith({ REMOTE_TASK_TOKEN: 'tok' });
		assert.deepEqual(onSurface.tools.map((t: { name: string }) => t.name), ['navigate_ui']);
		assert.deepEqual(onSurface.promptRules, [NAVIGATION_PROMPT_RULE]);
		const lines = buildInstructions(ctx(onSurface) as never, OVERRIDES).split('\n');
		assert.equal(lines.filter(l => l.startsWith('- NAVIGATION: ')).length, 1);
		const rule = lines.findIndex(l => l.startsWith('- NAVIGATION: '));
		assert.ok(lines[rule - 1].startsWith('- For SIMPLE actions') && lines[rule + 1].startsWith('- For IN-PLACE EDITS'), 'where the rule has always sat');
		assert.ok(lines.some(l => l.startsWith('- navigate_ui: ') && l.endsWith('. Instant.')), 'listed with the instant tools');
		assert.ok(lines.some(l => l.includes(', navigate_ui — call these directly')), 'and in the joined names line');
	});

	it('navigateUiAvailable: a gateway token in the env or in channels/ag2space/.env, nothing else', () => {
		const home = mkdtempSync(join(tmpdir(), 'sutando-navigate-available-'));
		const saved = process.env.CLAUDE_CONFIG_DIR;
		process.env.CLAUDE_CONFIG_DIR = home;
		try {
			assert.equal(navigateUiAvailable({}), false, 'no channel dir at all');
			mkdirSync(join(home, 'channels', 'ag2space'), { recursive: true });
			writeFileSync(join(home, 'channels', 'ag2space', '.env'), 'REMOTE_TASK_URL=https://gw.example\nREMOTE_TASK_TOKEN=\n');
			assert.equal(navigateUiAvailable({}), false, 'a channel file with no token is not a provisioned channel');
			writeFileSync(join(home, 'channels', 'ag2space', '.env'), 'REMOTE_TASK_URL=https://gw.example\nAG2_REMOTE_TOKEN=tok\n');
			assert.equal(navigateUiAvailable({}), true);
			rmSync(join(home, 'channels'), { recursive: true, force: true });
			assert.equal(navigateUiAvailable({ REMOTE_TASK_TOKEN: 'tok' }), true, 'env wins without a file');
		} finally {
			if (saved === undefined) delete process.env.CLAUDE_CONFIG_DIR; else process.env.CLAUDE_CONFIG_DIR = saved;
			rmSync(home, { recursive: true, force: true });
		}
	});

	it('what a user can hear names no product', () => {
		for (const text of [NAVIGATE_UI_UNSUPPORTED_MESSAGE, NAVIGATE_UI_CLIENT_OUTDATED_MESSAGE, navigateUiTool.description as string]) {
			assert.doesNotMatch(text, /AG2/i);
		}
	});
});
}
