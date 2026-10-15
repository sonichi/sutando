/**
 * find/describe/run_room_action — the rest of the AG2 Space action catalog, by voice.
 *
 * Pins: the policy for a catalog action comes from the server's describe, never the model —
 * a read runs through room.action.read, a change through room.action.execute, high risk or an
 * outside effect is refused before anything runs; every failure is an answer, never a throw.
 */
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'room-actions.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const {
	voicePolicy, findActions, describeAction, runAction, compactResult,
	createFindActionsTool, createDescribeActionTool, createRunActionTool,
	ROOM_ACTIONS_PROMPT_RULE, ROOM_ACTIONS_TURN_CHECK_RULE, ACTION_RESULT_MAX_CHARS,
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

	it('a change runs through room.action.execute with a fresh operation_id', async () => {
		const f = fake(describeAs('mutation', 'medium'));
		const r = await runAction({ action: 'room.commons.comments.resolve', arguments_json: '{"root_event_id":"$a"}', room: '!r:ag2.space' }, f.deps);
		assert.deepEqual(f.calls.map(c => c.name), ['room.actions.describe', 'room.action.execute']);
		assert.equal(typeof f.calls[1].args.operation_id, 'string');
		assert.equal(f.calls[1].args.room_id, '!r:ag2.space');
		assert.ok(r.ok);
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
		const approval = fake((n) => n === 'room.actions.describe' ? ok({ effect: 'mutation', risk: 'low' }) : ok({ status: 'pending_approval', approval_id: 'ap1' }));
		assert.deepEqual(await runAction({ action: 'x' }, approval.deps), { ok: false, error: 'needs_approval' });
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

	it('the rules name the tools', () => {
		for (const name of ['find_room_actions', 'describe_room_action', 'run_room_action']) assert.ok(ROOM_ACTIONS_PROMPT_RULE.includes(name), name);
		assert.match(ROOM_ACTIONS_TURN_CHECK_RULE, /^- none:/);
	});
});
}
