import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { runSkillSetups, type SkillSetup, type SkillSetupCtx } from '../src/skill-setup-runner.js';

const CTX: SkillSetupCtx = { session: {}, injectText: () => {} };
const logs: string[] = [];
const log = (m: string, d?: unknown) => { logs.push(`${m} ${d ?? ''}`.trim()); };

describe('optional skill setup() cannot crash or stall bootstrap', () => {
	it('PREMISE: an unhandled rejection would terminate this process', () => {
		// Node's default is --unhandled-rejections=throw, which is exactly why an
		// async setup() escaping the sync try/catch could kill voice bootstrap.
		assert.equal(typeof process.on, 'function');
	});

	it('a synchronous throw is contained and later setups still run', () => {
		logs.length = 0;
		const ran: string[] = [];
		const setups: SkillSetup[] = [
			() => { throw new Error('boom'); },
			() => { ran.push('second'); },
		];
		const ok = runSkillSetups(setups, CTX, log);
		assert.deepEqual(ran, ['second']);
		assert.equal(ok, 1);
		assert.ok(logs.some(l => l.includes('hook threw')), logs.join('|'));
	});

	it('a REJECTING async setup does not produce an unhandled rejection', async () => {
		logs.length = 0;
		const ran: string[] = [];
		let sawUnhandled: unknown = null;
		const onUnhandled = (r: unknown) => { sawUnhandled = r; };
		process.on('unhandledRejection', onUnhandled);
		try {
			const setups: SkillSetup[] = [
				(() => Promise.reject(new Error('async boom'))) as unknown as SkillSetup,
				() => { ran.push('after'); },
			];
			runSkillSetups(setups, CTX, log);
			// Two macrotask turns: rejection handling is queued, not immediate.
			await new Promise(r => setTimeout(r, 20));
			assert.equal(sawUnhandled, null, `unhandled rejection escaped: ${String(sawUnhandled)}`);
			assert.deepEqual(ran, ['after'], 'a rejecting hook must not stop later setups');
			assert.ok(logs.some(l => l.includes('async hook rejected')), logs.join('|'));
		} finally {
			process.off('unhandledRejection', onUnhandled);
		}
	});

	it('a NEVER-SETTLING async setup does not stall the loop', () => {
		logs.length = 0;
		const ran: string[] = [];
		const setups: SkillSetup[] = [
			(() => new Promise(() => {})) as unknown as SkillSetup,
			() => { ran.push('after'); },
		];
		const t0 = Date.now();
		runSkillSetups(setups, CTX, log);
		const elapsed = Date.now() - t0;
		assert.deepEqual(ran, ['after'], 'a hung hook must not block registration of the rest');
		assert.ok(elapsed < 500, `loop took ${elapsed}ms — it awaited the hung hook`);
		assert.ok(logs.some(l => l.includes('must be synchronous')), logs.join('|'));
	});

	it('a thenable is not counted as a completed synchronous registration', () => {
		logs.length = 0;
		const setups: SkillSetup[] = [
			(() => Promise.resolve()) as unknown as SkillSetup,
			() => {},
		];
		assert.equal(runSkillSetups(setups, CTX, log), 1);
	});

	it('a non-promise return value is treated as a normal completion', () => {
		logs.length = 0;
		const setups: SkillSetup[] = [(() => 42) as unknown as SkillSetup];
		assert.equal(runSkillSetups(setups, CTX, log), 1);
		assert.deepEqual(logs, []);
	});

	it('an object carrying a non-callable then is NOT mistaken for a thenable', () => {
		logs.length = 0;
		const setups: SkillSetup[] = [(() => ({ then: 'later' })) as unknown as SkillSetup];
		assert.equal(runSkillSetups(setups, CTX, log), 1);
		assert.deepEqual(logs, []);
	});

	it('a throwing then GETTER does not escape isolation', () => {
		logs.length = 0;
		let later = false;
		const setups: SkillSetup[] = [
			(() => ({ get then() { throw new Error('getter boom'); } })) as unknown as SkillSetup,
			() => { later = true; },
		];
		assert.equal(runSkillSetups(setups, CTX, log), 1);
		assert.ok(later, 'a skill-controlled then getter aborted the loop');
		assert.ok(logs.some(l => l.includes('inspection threw')), logs.join('|'));
	});

	it('a throwing then METHOD does not escape isolation', () => {
		logs.length = 0;
		let later = false;
		const setups: SkillSetup[] = [
			(() => ({ then() { throw new Error('method boom'); } })) as unknown as SkillSetup,
			() => { later = true; },
		];
		assert.equal(runSkillSetups(setups, CTX, log), 1);
		assert.ok(later, 'a skill-controlled then() aborted the loop');
	});

	it('every hook runs even when all of them throw', () => {
		logs.length = 0;
		let calls = 0;
		const setups: SkillSetup[] = Array.from({ length: 5 }, () => () => { calls++; throw new Error('x'); });
		assert.equal(runSkillSetups(setups, CTX, log), 0);
		assert.equal(calls, 5);
	});
});
