/**
 * find/describe/run_room_action — the rest of the AG2 Space action catalog, by voice.
 *
 * Pins: the policy for a catalog action comes from the server's describe, never the model —
 * a read runs through room.action.read, a change through room.action.execute, high risk or an
 * outside effect is refused before anything runs; every failure is an answer, never a throw.
 */
import { describe, it, test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'room-actions.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const {
	voicePolicy, findActions, describeAction, runAction, compactResult, canonicalJson, resetPendingConfirmations, noteUserSpoke, armSpeechGate,
	createFindActionsTool, createDescribeActionTool, createRunActionTool,
	ROOM_ACTIONS_PROMPT_RULE, ROOM_ACTIONS_TURN_CHECK_RULE, ACTION_RESULT_MAX_CHARS, CONFIRM_MIN_AGE_MS, CONFIRM_TTL_MS,
} = await import('../skills/ag2space-voice/room-actions.ts');

type Call = { name: string; args: Record<string, unknown> };
const ok = (v: unknown) => ({ structuredContent: v });
const err = (code: string) => ({ isError: true, structuredContent: { code } });

function fake(handler: (name: string, args: Record<string, unknown>) => unknown) {
	const calls: Call[] = [];
	const client = { async callTool(name: string, args: Record<string, unknown>) { calls.push({ name, args }); const r = handler(name, args); if (r instanceof Error) throw r; return r as never; }, close() {} };
	const index = { rooms: () => [], refresh: async () => [], fetchedAt: () => 0 };
	const deps = { client: () => client, currentRoom: () => ({ id: '!cur:ag2.space', name: 'Current' }), index, log: () => {} };
	return { calls, client, deps };
}


describe('the policy comes from the effect and risk the server declares', () => {
	it('reads run, changes need a yes, high risk and outside effects are not by voice', () => {
		assert.equal(voicePolicy('zero_effect', 'low'), 'read');
		assert.equal(voicePolicy('zero_effect', 'medium'), 'read');
		assert.equal(voicePolicy('mutation', 'low'), 'confirm');
		assert.equal(voicePolicy('mutation', 'medium'), 'confirm');
		assert.equal(voicePolicy('mutation', 'high'), 'no');
		assert.equal(voicePolicy('zero_effect', 'critical'), 'no');
		assert.equal(voicePolicy('external_effect', 'low'), 'no');
		assert.equal(voicePolicy(undefined, undefined), 'no');
	});

	it('a missing, new or differently spelled risk is refused, like an unknown effect', () => {
		for (const risk of [undefined, null, '', 'severe', 'HIGH', 'Low', 'unknown']) {
			assert.equal(voicePolicy('mutation', risk), 'no', String(risk));
			assert.equal(voicePolicy('zero_effect', risk), 'no', String(risk));
		}
		assert.equal(voicePolicy('weird', 'low'), 'no');
	});
});

describe('find_room_actions / describe_room_action', () => {
	it('searches the room catalog by the current room, or the account catalog', async () => {
		const f = fake(() => ok({ items: [
			{ action: 'room.commons.comments.resolve', title: 'Resolve a comment thread', summary: 's'.repeat(500), effect: 'mutation', risk: 'medium', target_kind: 'room', required_arguments: ['root_event_id'] },
			{ action: 'room.member.invite', title: 'Invite', summary: '', effect: 'mutation', risk: 'high', target_kind: 'room' },
			{ title: 'no action name' },
		] }));
		const r = await findActions({ query: 'resolve comment' }, f.deps);
		assert.equal(f.calls[0].name, 'room.actions.search');
		assert.equal(f.calls[0].args.room_id, '!cur:ag2.space');
		assert.equal(f.calls[0].args.query, 'resolve comment');
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.room, 'Current');
			assert.equal(r.actions.length, 2);
			assert.equal(r.actions[0].by_voice, 'confirm');
			assert.deepEqual(r.actions[0].needs, ['root_event_id']);
			assert.ok(r.actions[0].summary.length <= 301);
			assert.equal(r.actions[1].by_voice, 'no');
		}
		const g = fake(() => ok({ items: [] }));
		const a = await findActions({ query: 'invites', scope: 'account', room: 'ignored' }, g.deps);
		assert.equal(g.calls[0].args.target_kind, 'account');
		assert.equal(g.calls[0].args.room_id, undefined);
		assert.deepEqual(a.ok && a.room, null);
	});

	it('no query, no room, no client, an error code', async () => {
		assert.deepEqual(await findActions({ query: ' ' }, fake(() => null).deps), { ok: false, error: 'no_query' });
		const noRoom = fake(() => null);
		assert.deepEqual(await findActions({ query: 'x' }, { ...noRoom.deps, currentRoom: () => null }), { ok: false, error: 'no_room' });
		assert.deepEqual(await findActions({ query: 'x' }, { ...noRoom.deps, client: () => null }), { ok: false, error: 'not_connected' });
		assert.deepEqual(await findActions({ query: 'x' }, fake(() => err('ROOM_ACCESS_DENIED')).deps), { ok: false, error: 'ROOM_ACCESS_DENIED' });
		assert.deepEqual(await findActions({ query: 'x' }, fake(() => new Error('boom')).deps), { ok: false, error: 'unavailable' });
	});

	it('describe hands back the argument schema and the policy', async () => {
		const schema = { type: 'object', properties: { root_event_id: { type: 'string' } }, required: ['root_event_id'] };
		const f = fake(() => ok({ action: 'room.commons.comments.resolve', title: 'Resolve', summary: 'Mark resolved.', effect: 'mutation', risk: 'medium', decision: 'allow', input_schema: schema }));
		const r = await describeAction({ action: 'room.commons.comments.resolve', room: '!r:ag2.space' }, f.deps);
		assert.equal(f.calls[0].name, 'room.actions.describe');
		assert.equal(f.calls[0].args.room_id, '!r:ag2.space');
		assert.deepEqual(r, { ok: true, action: 'room.commons.comments.resolve', title: 'Resolve', summary: 'Mark resolved.', by_voice: 'confirm', arguments: schema });
		assert.deepEqual(await describeAction({ action: '' }, f.deps), { ok: false, error: 'no_action' });
		assert.deepEqual(await describeAction({ action: 'x' }, fake(() => err('NOT_FOUND')).deps), { ok: false, error: 'NOT_FOUND' });
	});
});

describe('run_room_action: the server\'s description decides, before anything runs', () => {
	const describeAs = (effect: string, risk: string, decision = 'allow') => (name: string) =>
		name === 'room.actions.describe' ? ok({ effect, risk, decision }) : ok({ status: 'completed', result: { done: name } });

	it('a read runs through room.action.read with the parsed arguments', async () => {
		const f = fake(describeAs('zero_effect', 'low'));
		const r = await runAction({ action: 'room.members.list', arguments_json: '{"limit":5}' }, f.deps);
		assert.deepEqual(f.calls.map(c => c.name), ['room.actions.describe', 'room.action.read']);
		assert.deepEqual(f.calls[1].args.arguments, { limit: 5 });
		assert.equal(f.calls[1].args.room_id, '!cur:ag2.space');
		assert.ok(r.ok);
		if (r.ok) assert.deepEqual(r.result, { done: 'room.action.read' });
	});

	it('a read passes the revision it checked', async () => {
		const f = fake((n) => n === 'room.actions.describe' ? ok({ effect: 'zero_effect', risk: 'low', action_revision: 'sha256:r1' }) : ok({ result: {} }));
		await runAction({ action: 'room.members.list' }, f.deps);
		assert.equal(f.calls[1].args.expected_action_revision, 'sha256:r1');
	});
	it('an account action targets the account', async () => {
		const f = fake(describeAs('zero_effect', 'low'));
		const r = await runAction({ action: 'room.invites.list', scope: 'account' }, f.deps);
		assert.equal(f.calls[0].args.target_kind, 'account');
		assert.equal(f.calls[1].args.target_kind, 'account');
		assert.deepEqual(f.calls[1].args.arguments, {});
		assert.ok(r.ok && r.room === null);
	});

	it('high risk, an outside effect, or a denied action is refused and nothing runs', async () => {
		for (const [effect, risk, decision] of [['mutation', 'high', 'allow'], ['external_effect', 'medium', 'allow'], ['zero_effect', 'low', 'deny']]) {
			const f = fake(describeAs(effect, risk, decision));
			assert.deepEqual(await runAction({ action: 'x.y' }, f.deps), { ok: false, error: 'not_by_voice' });
			assert.deepEqual(f.calls.map(c => c.name), ['room.actions.describe']);
		}
	});

	it('bad arguments, no action, no client, and the server\'s refusals are answers', async () => {
		const f = fake(describeAs('mutation', 'low'));
		assert.deepEqual(await runAction({ action: 'x', arguments_json: '{nope' }, f.deps), { ok: false, error: 'bad_arguments' });
		assert.deepEqual(await runAction({ action: 'x', arguments_json: '[1]' }, f.deps), { ok: false, error: 'bad_arguments' });
		assert.deepEqual(await runAction({ action: ' ' }, f.deps), { ok: false, error: 'no_action' });
		assert.deepEqual(await runAction({ action: 'x' }, { ...f.deps, client: () => null }), { ok: false, error: 'not_connected' });
		assert.equal(f.calls.length, 0);
		assert.deepEqual(await runAction({ action: 'x' }, fake(() => err('NOT_FOUND')).deps), { ok: false, error: 'NOT_FOUND' });
		const noRoom = fake(describeAs('zero_effect', 'low'));
		assert.deepEqual(await runAction({ action: 'x' }, { ...noRoom.deps, currentRoom: () => null }), { ok: false, error: 'no_room' });
	});

	it('a long result is cut for the model', () => {
		assert.deepEqual(compactResult({ a: 1 }), { a: 1 });
		const long = compactResult({ text: 'x'.repeat(ACTION_RESULT_MAX_CHARS * 2) });
		assert.equal(typeof long, 'string');
		assert.equal((long as string).length, ACTION_RESULT_MAX_CHARS + 1);
	});
});

describe('a change runs only on a confirmation token bound to what was confirmed', () => {
	let t = 1_000_000;
	const now = () => t;
	beforeEach(() => { resetPendingConfirmations(); t = 1_000_000; });
	const change = (exec: (a: Record<string, unknown>) => unknown = () => ok({ status: 'completed', result: { resolved: true } }), extra: Record<string, unknown> = {}) =>
		fake((n, a) => n === 'room.actions.describe' ? ok({ effect: 'mutation', risk: 'medium', title: 'Resolve a comment thread', action_revision: 'sha256:r1', idempotency_requirement: 'required', ...extra }) : exec(a));
	const RUN = { action: 'room.commons.comments.resolve', arguments_json: '{"root_event_id":"$a"}', room: '!r:ag2.space' };
	const first = async (f: ReturnType<typeof change>) => {
		const r = await runAction(RUN, f.deps, now);
		assert.equal(r.ok, false);
		assert.equal((r as { error: string }).error, 'needs_confirmation');
		return r as { confirm_token: string; will_do: { action: string; title: string; room: string | null; arguments: Record<string, unknown> } };
	};

	it('the first call runs nothing and hands back what it will do, with a token', async () => {
		const f = change();
		const r = await first(f);
		assert.deepEqual(f.calls.map(c => c.name), ['room.actions.describe']);
		assert.match(r.confirm_token, /^[0-9a-f]{12}$/);
		assert.deepEqual(r.will_do, { action: RUN.action, title: 'Resolve a comment thread', room: '!r:ag2.space', arguments: { root_event_id: '$a' } });
		// Live 2026-10-15 20:38: after the yes the model said "added" without the second call.
		assert.equal((r as unknown as { done: boolean }).done, false);
		assert.match((r as unknown as { next: string }).next, /until that call returns ok:true, do not say it is done/);
	});

	it('the same call with the token, after a read-back\'s worth of time, runs once with the checked revision', async () => {
		const f = change();
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		const r = await runAction({ ...RUN, arguments_json: '{ "root_event_id" : "$a" }', confirm_token }, f.deps, now);
		assert.ok(r.ok);
		const ex = f.calls.filter(c => c.name === 'room.action.execute');
		assert.equal(ex.length, 1);
		assert.equal(ex[0].args.expected_action_revision, 'sha256:r1');
		assert.equal(typeof ex[0].args.operation_id, 'string');
		t += 1000;
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_expired' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
	});

	it('a token used at once was not confirmed by anyone: refused, and still usable after the read-back', async () => {
		const f = change();
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS - 1;
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_too_soon' });
		t += 1;
		assert.ok((await runAction({ ...RUN, confirm_token }, f.deps, now)).ok);
	});

	it('other arguments, another room or another action with the token are refused, and the token is spent', async () => {
		for (const other of [{ arguments_json: '{"root_event_id":"$b"}' }, { room: '!other:ag2.space' }, { action: 'room.commons.comments.reopen' }]) {
			resetPendingConfirmations();
			const f = change();
			const { confirm_token } = await first(f);
			t += CONFIRM_MIN_AGE_MS;
			assert.deepEqual(await runAction({ ...RUN, ...other, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_mismatch' });
			assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_expired' });
			assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
		}
	});

	it('a made-up or stale token runs nothing', async () => {
		const f = change();
		assert.deepEqual(await runAction({ ...RUN, confirm_token: 'abcabcabcabc' }, f.deps, now), { ok: false, error: 'confirmation_expired' });
		const { confirm_token } = await first(f);
		t += CONFIRM_TTL_MS + 1;
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_expired' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
	});

	it('an action updated since the yes is refused', async () => {
		let rev = 'sha256:r1';
		const f = fake((n) => n === 'room.actions.describe' ? ok({ effect: 'mutation', risk: 'low', action_revision: rev }) : ok({ status: 'completed', result: {} }));
		const { confirm_token } = await first(f as never);
		rev = 'sha256:r2';
		t += CONFIRM_MIN_AGE_MS;
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'action_changed' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
	});

	it('an unknown outcome keeps the token; a retry reuses the operation_id, so it cannot run twice', async () => {
		let n = 0;
		const f = change(() => (++n === 1 ? { isError: true, structuredContent: { code: 'ACTION_OUTCOME_UNKNOWN' } } : ok({ status: 'completed', result: {} })));
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		const r1 = await runAction({ ...RUN, confirm_token }, f.deps, now);
		assert.equal((r1 as { error: string }).error, 'outcome_unknown');
		const r2 = await runAction({ ...RUN, confirm_token }, f.deps, now);
		assert.ok(r2.ok);
		const ops = f.calls.filter(c => c.name === 'room.action.execute').map(c => c.args.operation_id);
		assert.equal(ops.length, 2);
		assert.equal(ops[0], ops[1]);
		assert.equal((r1 as { operation_id?: string }).operation_id, ops[0]);
	});

	it('a timeout after the call was sent is an unknown outcome too; without server dedup the token is spent', async () => {
		const f = change(() => new Error('tools/call timed out after 15000ms'), { idempotency_requirement: 'none' });
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		assert.equal(((await runAction({ ...RUN, confirm_token }, f.deps, now)) as { error: string }).error, 'outcome_unknown');
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_expired' });
	});

	it('with the host\'s speech signal, the token runs only after the user has spoken since it was issued', async () => {
		resetPendingConfirmations(true);
		const f = change();
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS * 4;
		// The model calls again on its own, long after: no speech, nothing runs (live 2026-10-15 19:40:51).
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_too_soon' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
		noteUserSpoke();
		assert.ok((await runAction({ ...RUN, confirm_token }, f.deps, now)).ok);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
	});

	it('a host with a speech signal arms the gate at registration: before the first words, a yes still needs speech', async () => {
		resetPendingConfirmations();
		const { setup } = await import('../skills/ag2space-voice/tools.ts');
		setup({ onUserSpoke: () => {} } as never);
		const f = change();
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS * 4;
		// A typed first request: no transcript ever arrived, and the model calls again on its own.
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_too_soon' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
		noteUserSpoke();
		assert.ok((await runAction({ ...RUN, confirm_token }, f.deps, now)).ok);
	});

	it('arming leaves an existing count alone, and the held call logs which gate held it', async () => {
		resetPendingConfirmations(true);
		noteUserSpoke();
		armSpeechGate();
		const logs: string[] = [];
		const f = change();
		const deps = { ...f.deps, log: (m: string) => { logs.push(m); } };
		const r = await runAction(RUN, deps, now) as { confirm_token: string };
		t += CONFIRM_MIN_AGE_MS;
		assert.deepEqual(await runAction({ ...RUN, confirm_token: r.confirm_token }, deps, now), { ok: false, error: 'confirmation_too_soon' });
		assert.ok(logs.some(l => /confirmation_too_soon \(age \d+ms, spoken since issue 0\)/.test(l)), logs.join('\n'));
	});

	it('speech before the token was issued does not count as the yes', async () => {
		resetPendingConfirmations(true);
		noteUserSpoke();
		const f = change();
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'confirmation_too_soon' });
	});

	it('an upstream failure on the write is an unknown outcome, kept for a retry the server deduplicates', async () => {
		let n = 0;
		const f = change(() => (++n === 1 ? { isError: true, structuredContent: { code: 'UPSTREAM_UNAVAILABLE' } } : ok({ status: 'completed', result: {} })));
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		assert.equal(((await runAction({ ...RUN, confirm_token }, f.deps, now)) as { error: string }).error, 'outcome_unknown');
		assert.ok((await runAction({ ...RUN, confirm_token }, f.deps, now)).ok);
		const ops = f.calls.filter(c => c.name === 'room.action.execute').map(c => c.args.operation_id);
		assert.equal(ops[0], ops[1]);
	});

	it('"open the page": a summon naming no one calls the owner, and the yes binds that invitee', async () => {
		const SUMMON = { action: 'room.commons.summon', arguments_json: '{"kind":"markdown-a1ud0njz"}', room: '!r:ag2.space' };
		const f = fake((n) => n === 'room.actions.describe' ? ok({ effect: 'mutation', risk: 'medium', title: 'Summon', action_revision: 'sha256:s1', idempotency_requirement: 'required' })
			: n === 'ag2.whoami' ? ok({ actor: { id: '@bot:ag2.space' }, runtime: { owner_id: '@owner:ag2.space' } })
			: ok({ status: 'completed', result: { event_id: '$card' } }));
		const r = await runAction(SUMMON, f.deps, now) as { error: string; confirm_token: string; will_do: { arguments: Record<string, unknown> } };
		assert.equal(r.error, 'needs_confirmation');
		assert.deepEqual(r.will_do.arguments, { kind: 'markdown-a1ud0njz', invitee: '@owner:ag2.space' });
		t += CONFIRM_MIN_AGE_MS;
		assert.ok((await runAction({ ...SUMMON, confirm_token: r.confirm_token }, f.deps, now)).ok);
		const exec = f.calls.filter(c => c.name === 'room.action.execute');
		assert.equal(exec.length, 1);
		assert.deepEqual((exec[0].args as { arguments: unknown }).arguments, { kind: 'markdown-a1ud0njz', invitee: '@owner:ag2.space' });
	});

	it('a summon names its invitee when given one, and is refused when the owner is unknown', async () => {
		const f = fake((n) => n === 'room.actions.describe' ? ok({ effect: 'mutation', risk: 'medium', title: 'Summon', action_revision: 'sha256:s1' }) : ok({ actor: { id: '@bot2:ag2.space' }, runtime: {} }));
		const named = await runAction({ action: 'room.commons.summon', arguments_json: '{"kind":"board","invitee":"@chi:ag2.space"}', room: '!r:ag2.space' }, f.deps, now) as { will_do: { arguments: Record<string, unknown> } };
		assert.equal(named.will_do.arguments.invitee, '@chi:ag2.space');
		assert.deepEqual(await runAction({ action: 'room.commons.summon', arguments_json: '{"kind":"board"}', room: '!r:ag2.space' }, f.deps, now), { ok: false, error: 'no_invitee' });
		assert.match(ROOM_ACTIONS_PROMPT_RULE, /room\.commons\.summon/);
	});

	it('the plugin listens for the host\'s speech signal, and the voice agent sends it from input transcription', async () => {
		const { setup } = await import('../skills/ag2space-voice/tools.ts');
		let handler: (() => void) | null = null;
		setup({ onUserSpoke: (h: () => void) => { handler = h; } } as never);
		assert.equal(handler, noteUserSpoke);
		const src = readFileSync(join(process.cwd(), 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /onUserSpoke: \(handler\) => \{ userSpokeHandlers\.push\(handler\); \}/);
		assert.match(src, /transport\.onInputTranscription = \(text: string\) => \{\s*orig\?\.\(text\);[\s\S]{0,120}for \(const h of userSpokeHandlers\)/);
	});

	it('two calls with one token at the same time run once', async () => {
		let release!: () => void;
		const gate = new Promise<void>(r => { release = r; });
		const f = change(async () => { await gate; return ok({ status: 'completed', result: {} }); }, { idempotency_requirement: 'none' });
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		const a = runAction({ ...RUN, confirm_token }, f.deps, now);
		const b = runAction({ ...RUN, confirm_token }, f.deps, now);
		await new Promise(r => setTimeout(r, 20));
		release();
		const results = await Promise.all([a, b]);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
		assert.deepEqual(results.map(r => r.ok).sort(), [false, true]);
		assert.ok(results.some(r => !r.ok && (r as { error: string }).error === 'confirmation_expired'));
	});

	it('an approval the room requires is said, and the token is spent', async () => {
		const f = change(() => ok({ status: 'pending_approval', approval_id: 'ap1' }));
		const { confirm_token } = await first(f);
		t += CONFIRM_MIN_AGE_MS;
		assert.deepEqual(await runAction({ ...RUN, confirm_token }, f.deps, now), { ok: false, error: 'needs_approval' });
	});

	it('the arguments are bound whatever their key order', () => {
		assert.equal(canonicalJson({ b: 1, a: [{ d: 2, c: 3 }] }), canonicalJson({ a: [{ c: 3, d: 2 }], b: 1 }));
		assert.notEqual(canonicalJson({ a: 1 }), canonicalJson({ a: '1' }));
	});
});

describe('an action the code has never named: whatever the server\'s catalog lists is reachable', () => {
	// A stand-in for an action the server adds later: no tool, no name, no list in the plugin knows it.
	const NEW = 'room.future.widgets.list';
	const NEW_CHANGE = 'room.future.widgets.pin';
	const catalog = (n: string, a: Record<string, unknown>) => {
		if (n === 'room.actions.search') return ok({ items: [
			{ action: NEW, title: 'List widgets', summary: 'List the room widgets.', effect: 'zero_effect', risk: 'low', target_kind: 'room', required_arguments: [] },
			{ action: NEW_CHANGE, title: 'Pin a widget', summary: 'Pin one widget.', effect: 'mutation', risk: 'low', target_kind: 'room', required_arguments: ['widget_id'] },
		] });
		if (n === 'room.actions.describe') return a.action === NEW
			? ok({ action: NEW, effect: 'zero_effect', risk: 'low', input_schema: { type: 'object', properties: {} } })
			: ok({ action: NEW_CHANGE, effect: 'mutation', risk: 'low', title: 'Pin a widget', input_schema: { type: 'object', properties: { widget_id: { type: 'string' } }, required: ['widget_id'] } });
		return ok({ status: 'completed', result: { widgets: ['w1'] } });
	};

	it('is found, described and run — a read at once, a change only after its token', async () => {
		resetPendingConfirmations();
		const f = fake(catalog);
		const found = await findActions({ query: 'widgets' }, f.deps);
		assert.ok(found.ok);
		if (found.ok) assert.deepEqual(found.actions.map(x => [x.action, x.by_voice]), [[NEW, 'read'], [NEW_CHANGE, 'confirm']]);
		const desc = await describeAction({ action: NEW_CHANGE }, f.deps);
		assert.ok(desc.ok && desc.by_voice === 'confirm');
		const read = await runAction({ action: NEW }, f.deps);
		assert.ok(read.ok);
		if (read.ok) assert.deepEqual(read.result, { widgets: ['w1'] });
		const reads = f.calls.filter(c => c.name === 'room.action.read');
		assert.equal(reads.length, 1);
		assert.equal(reads[0].args.action, NEW);
		let t = 5_000_000;
		const first = await runAction({ action: NEW_CHANGE, arguments_json: '{"widget_id":"w1"}' }, f.deps, () => t);
		assert.equal((first as { error: string }).error, 'needs_confirmation');
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
		t += CONFIRM_MIN_AGE_MS;
		const done = await runAction({ action: NEW_CHANGE, arguments_json: '{"widget_id":"w1"}', confirm_token: (first as { confirm_token: string }).confirm_token }, f.deps, () => t);
		assert.ok(done.ok);
		const ex = f.calls.filter(c => c.name === 'room.action.execute');
		assert.equal(ex.length, 1);
		assert.equal(ex[0].args.action, NEW_CHANGE);
		assert.deepEqual(ex[0].args.arguments, { widget_id: 'w1' });
	});

	it('the plugin names no catalog action of its own: only the catalog tools and the two ways to run', () => {
		const src = readFileSync(join(PLUGIN_DIR, 'room-actions.ts'), 'utf-8');
		const named = [...src.matchAll(/'((?:room|space|mentions|browser|commons|agent|devapp)\.[a-z_.]+)'/g)].map(m => m[1]);
		assert.ok(named.includes('room.actions.search') && named.includes('room.actions.describe'));
		// The one exception: a summon naming no one is filled with the owner; its policy still comes from describe.
		for (const n of named) assert.ok(/^room\.actions?\.(search|describe|read|execute)$/.test(n) || n === 'room.commons.summon', n);
	});
});

describe('the description fails closed: nothing runs when the server cannot describe the action', () => {
	it('an error, a timeout, NOT_FOUND or an empty answer from describe runs nothing', async () => {
		for (const d of [err('NOT_FOUND'), err('INTERNAL'), new Error('tools/call timed out after 6000ms'), new Error('boom'), {}]) {
			const f = fake((n) => n === 'room.actions.describe' ? d : ok({ status: 'completed', result: {} }));
			const r = await runAction({ action: 'room.commons.comments.resolve', arguments_json: '{"root_event_id":"$a"}' }, f.deps);
			assert.equal(r.ok, false);
			assert.deepEqual(f.calls.map(c => c.name), ['room.actions.describe']);
		}
	});
});

describe('the tools as the voice model sees them', () => {
	it('each tool answers through its function', async () => {
		const f = fake((name) => {
			if (name === 'room.actions.describe') return ok({ effect: 'zero_effect', risk: 'low', input_schema: {} });
			if (name === 'room.actions.search') return ok({ items: [] });
			return ok({ result: {} });
		});
		const tools = [createFindActionsTool(f.deps), createDescribeActionTool(f.deps), createRunActionTool(f.deps)];
		assert.deepEqual(tools.map(t => t.name), ['find_room_actions', 'describe_room_action', 'run_room_action']);
		for (const t of tools) assert.equal(t.execution, 'inline');
		const [find, desc, run] = tools;
		assert.equal(((await find.execute({ query: 'x' }, {} as never)) as { ok: boolean }).ok, true);
		assert.equal(((await desc.execute({ action: 'x' }, {} as never)) as { ok: boolean }).ok, true);
		assert.equal(((await run.execute({ action: 'x' }, {} as never)) as { ok: boolean }).ok, true);
		const down = { ...f.deps, client: () => null };
		for (const t of [createFindActionsTool(down), createDescribeActionTool(down), createRunActionTool(down)]) {
			assert.equal(((await t.execute({ query: 'x', action: 'x' }, {} as never)) as { ok: boolean }).ok, false);
		}
	});

	it('the run description: call before asking, done only on ok:true, emoji as the character (live 2026-10-15 20:07–20:09)', () => {
		const d = createRunActionTool(fake(() => null).deps).description;
		assert.match(d, /do not ask the user first/);
		assert.match(d, /a yes given before the first call does not count/);
		assert.match(d, /Say it is done only after a call returned ok:true/);
		assert.match(d, /the emoji character itself \(👍/);
		assert.match(ROOM_ACTIONS_PROMPT_RULE, /say it is done only after ok:true/);
	});

	it('a no means call nothing, and a message is acted on by its read_room event_id', () => {
		assert.match(ROOM_ACTIONS_PROMPT_RULE, /not cancel_task/);
		assert.match(ROOM_ACTIONS_PROMPT_RULE, /event_id from read_room/);
	});

	it('the rules name the tools', () => {
		for (const name of ['find_room_actions', 'describe_room_action', 'run_room_action']) assert.ok(ROOM_ACTIONS_PROMPT_RULE.includes(name), name);
		assert.match(ROOM_ACTIONS_TURN_CHECK_RULE, /^- none:/);
	});
});
}
