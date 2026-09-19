/**
 * navigate_ui — the voice agent moves the desktop by voice.
 *
 * Pins the wire contract shared with the desktop client (frame builders and
 * parsers round-trip, bounds hold), and the tool's every outcome: resolved by
 * the client's reply, timed out cleanly, `unsupported` with no client, failed
 * fast when the client leaves, and a stray reply ignored.
 */
import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import {
	UI_NAVIGATE_TYPE,
	UI_NAVIGATED_TYPE,
	UI_NAVIGATE_QUERY_MAX_CHARS,
	UI_NAVIGATED_NAME_MAX_CHARS,
	UI_NAVIGATED_MAX_CANDIDATES,
	buildUiNavigateFrame,
	parseUiNavigateFrame,
	buildUiNavigatedFrame,
	parseUiNavigatedFrame,
} from '../src/web-voice-transport.js';
import {
	navigateUi,
	navigateUiTool,
	installVoiceNavigateClient,
	resolveUiNavigated,
	failPendingNavigations,
	pendingNavigationCount,
	NAVIGATE_UI_TIMEOUT_MS,
	NAVIGATE_UI_UNSUPPORTED_MESSAGE,
} from '../src/voice-navigate.js';

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

	beforeEach(() => {
		sent.length = 0;
		attached = true;
		failPendingNavigations();
		installVoiceNavigateClient({ attached: () => attached, send: (f) => { sent.push(f); } });
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
		installVoiceNavigateClient({ attached: () => false, send: (f) => { sent.push(f); } });
		const gone = await navigateUi({ target: 'room', query: 'GTM' });
		assert.equal(gone.ok, false);
		assert.equal(sent.length, 0, 'nothing is sent to a client that is not there');
		assert.match(NAVIGATE_UI_UNSUPPORTED_MESSAGE, /desktop app/);
	});

	it('a send that throws answers unsupported rather than waiting', async () => {
		installVoiceNavigateClient({ attached: () => true, send: () => { throw new Error('socket closed'); } });
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
