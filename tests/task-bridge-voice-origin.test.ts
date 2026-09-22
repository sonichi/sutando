import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// The voice session origin, driven by a made-up adapter: the bridge carries an opaque
// channel + target from the session to the task header and on to the result file.

const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-origin-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
const TASK_DIR = join(TMP, 'tasks');
mkdirSync(RESULT_DIR, { recursive: true });

const {
	setVoiceSessionOrigin, getVoiceSessionOrigin, voiceTaskOrigin, resolveVoiceResultOrigin, forwardVoiceResultToOrigin, forwardVoiceResultToOwnerDm,
	keepVoiceResultToDm, forwardOfflineVoiceResult, startResultWatcher, workTool, DM_ONLY_DELIVERY_NOTE, LEADING_REDIRECT_RE, DM_ONLY_RE,
	_isDeliveredResult, _shouldFallthrough, _shouldRegisterTaskRow,
} = await import('../src/task-bridge.js');

after(() => {
	setVoiceSessionOrigin(null);
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

const origin = (target: string, extra: Record<string, unknown> = {}) => ({ channel: 'fakechan', target, ...extra });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const delegate = async (task: string) => (await (workTool.execute as any)({ task }, null)) as { taskId: string };
const until = async (cond: () => boolean, ms: number) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise(r => setTimeout(r, 100)); return cond(); };
const proactiveFor = (id: string) => readdirSync(RESULT_DIR).filter(f => f.startsWith(`proactive-result-${id}-`));

describe('setVoiceSessionOrigin — one origin per live client, opaque to the bridge', () => {
	it('binds, reads back and releases', () => {
		assert.equal(getVoiceSessionOrigin(), null);
		const o = origin('place-1', { label: 'The Place' });
		setVoiceSessionOrigin(o);
		assert.equal(getVoiceSessionOrigin(), o);
		setVoiceSessionOrigin(null);
		assert.equal(getVoiceSessionOrigin(), null);
	});

	it('an origin that could not be written as a filename tag or a [channel:] marker binds nothing', () => {
		for (const bad of [{ channel: 'Fake Chan', target: 'x' }, { channel: '', target: 'x' }, { channel: 'fakechan', target: '' }, { channel: 'fakechan', target: 'a b' },
			{ channel: 'fakechan', target: 'x]\n[channel: y' }, { channel: 'fakechan', target: 7 }, { target: 'x' }]) {
			setVoiceSessionOrigin(origin('place-1'));
			setVoiceSessionOrigin(bad as never);
			assert.equal(getVoiceSessionOrigin(), null, JSON.stringify(bad));
		}
	});

	it('a task carries the origin current at write time, and a task written with none carries none', async () => {
		setVoiceSessionOrigin(origin('place-1'));
		const t1 = await delegate('origin probe one');
		setVoiceSessionOrigin(origin('place-2'));
		const t2 = await delegate('origin probe two');
		setVoiceSessionOrigin(null);
		const t3 = await delegate('origin probe three');
		assert.equal(voiceTaskOrigin(t1.taskId)?.target, 'place-1');
		assert.equal(voiceTaskOrigin(t2.taskId)?.target, 'place-2');
		assert.equal(voiceTaskOrigin(t3.taskId), null);
		assert.match(readFileSync(join(TASK_DIR, `${t1.taskId}.txt`), 'utf-8'), /^channel_id: place-1$/m);
		assert.match(readFileSync(join(TASK_DIR, `${t3.taskId}.txt`), 'utf-8'), /^channel_id: local-voice$/m);
		for (const t of [t1, t2, t3]) rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
	});
});

describe('forwardVoiceResultToOrigin — the result reaches its origin, and voice never speaks it twice', () => {
	it('writes proactive-result-<id>-<ts>.to-<channel>.txt with [channel: <target>] first, claimed at once', () => {
		const file = forwardVoiceResultToOrigin('task-1700000000000', 'Three listings.\nSecond line.', origin('place-1'), 1_800_000_000);
		assert.equal(file, 'proactive-result-task-1700000000000-1800000000.to-fakechan.txt');
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), '[channel: place-1]\nThree listings.\nSecond line.');
		assert.equal(_shouldFallthrough(file), true, 'the drain would otherwise speak this file on its next tick');
		assert.equal(_shouldRegisterTaskRow(file), false);
		assert.equal(_isDeliveredResult(file), true);
		const py = spawnSync('python3', ['-c', `import sys; sys.path.insert(0, "src")\nfrom proactive_routing import proactive_destination\nprint(proactive_destination(${JSON.stringify(file)}))`], { cwd: process.cwd(), encoding: 'utf-8' });
		assert.equal(py.status, 0, py.stderr);
		assert.equal(py.stdout.trim(), 'fakechan', 'the name tag is the claim grammar every bridge reads');
	});

	it('a result that opens with its own [channel:] redirect keeps it; one further down, or an empty one, does not', () => {
		const own = '\n[channel: elsewhere]\nFor the other place.';
		const file = forwardVoiceResultToOrigin('task-1700000000005', own, origin('place-1'), 1_800_000_005);
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), own);
		assert.equal(_isDeliveredResult(file), true);
		for (const [i, body] of ['Intro.\n[channel: elsewhere]\nbody', '[channel: ]\nbody', '[channel:]\nbody'].entries()) {
			const f = forwardVoiceResultToOrigin(`task-170000000001${i}`, body, origin('place-1'), 1_800_000_010 + i);
			assert.equal(readFileSync(join(RESULT_DIR, f), 'utf-8'), `[channel: place-1]\n${body}`);
		}
		assert.match('  [channel: 123]', LEADING_REDIRECT_RE);
	});

	it('the owner-DM shape: same bridge tag, no [channel:] line, [dm-only] on top, claimed at once', () => {
		const file = forwardVoiceResultToOwnerDm('task-1700000000700', 'Private findings.', 'fakechan', 1_800_000_700);
		assert.equal(file, 'proactive-result-task-1700000000700-1800000700.to-fakechan.txt');
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), '[dm-only]\nPrivate findings.');
		assert.equal(_isDeliveredResult(file), true);
		assert.match('findings\n[DM-only]', DM_ONLY_RE);
		assert.doesNotMatch('dm only, please', DM_ONLY_RE);
	});
});

describe('the result leg re-checks the origin and fails closed', () => {
	it('verify true → the origin; false or a throw → null; no verify → the origin; no origin → null', async () => {
		const cases: Array<[string, Record<string, unknown>, boolean]> = [
			['ok', { verify: async () => true }, true], ['refused', { verify: async () => false }, false],
			['throws', { verify: async () => { throw new Error('down'); } }, false], ['truthy', { verify: async () => 'yes' as never }, false], ['none', {}, true],
		];
		for (const [name, extra, expected] of cases) {
			setVoiceSessionOrigin(origin(`place-${name}`, extra));
			const t = await delegate(`verify probe ${name}`);
			assert.equal((await resolveVoiceResultOrigin(t.taskId))?.target ?? null, expected ? `place-${name}` : null, name);
			rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
		}
		setVoiceSessionOrigin(null);
		assert.equal(await resolveVoiceResultOrigin('task-does-not-exist'), null);
	});

	it('offline: verified → the origin file; refused → the untagged owner-DM shape, left unclaimed; [dm-only] → the DM shape with no verify', async () => {
		let asked = 0;
		setVoiceSessionOrigin(origin('place-ok', { verify: async () => { asked++; return true; } }));
		const ok = await delegate('offline probe ok');
		setVoiceSessionOrigin(origin('place-no', { verify: async () => false }));
		const no = await delegate('offline probe refused');
		setVoiceSessionOrigin(null);
		const toOrigin = await forwardOfflineVoiceResult(ok.taskId, 'the answer', 1_800_000_101);
		assert.equal(toOrigin, `proactive-result-${ok.taskId}-1800000101.to-fakechan.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, toOrigin), 'utf-8'), '[channel: place-ok]\nthe answer');
		const dm = await forwardOfflineVoiceResult(no.taskId, 'the answer', 1_800_000_100);
		assert.equal(dm, `proactive-result-${no.taskId}-1800000100.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, dm), 'utf-8'), 'the answer');
		assert.equal(_isDeliveredResult(dm), false, 'left unclaimed: with no bridge to take it, the drain speaks it on reconnect');
		asked = 0;
		const kept = await forwardOfflineVoiceResult(ok.taskId, 'private', 1_800_000_102, true);
		assert.equal(readFileSync(join(RESULT_DIR, kept), 'utf-8'), '[dm-only]\nprivate');
		assert.equal(asked, 0, 'the origin is not a destination for a dm-only result');
		assert.equal(keepVoiceResultToDm(ok.taskId, 'for the place', false, 1_800_000_103), null);
		assert.equal(keepVoiceResultToDm('task-with-no-origin', 'private', true, 1_800_000_104), null);
		assert.ok(!existsSync(join(RESULT_DIR, 'proactive-result-task-with-no-origin-1800000104.to-fakechan.txt')));
		for (const t of [ok, no]) rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
	});
});

describe('the drain: a plain origin-bound result is written to its origin, a [dm-only] one to the DM and voice is told', () => {
	it('three results through the drain', async () => {
		const spoken: Array<{ result: string; note?: string }> = [];
		setVoiceSessionOrigin(origin('place-1', { verify: async () => true }));
		const plain = await delegate('drain probe plain');
		const priv = await delegate('drain probe private');
		setVoiceSessionOrigin(origin('place-2', { verify: async () => true, dmOnlyNote: 'It went to the DM, not the place.' }));
		const noted = await delegate('drain probe noted');
		setVoiceSessionOrigin(null);
		const { writeFileSync } = await import('node:fs');
		writeFileSync(join(RESULT_DIR, `${plain.taskId}.txt`), 'Three listings.');
		writeFileSync(join(RESULT_DIR, `${priv.taskId}.txt`), '[dm-only]\nPrivate one.');
		writeFileSync(join(RESULT_DIR, `${noted.taskId}.txt`), '[dm-only]\nPrivate two.');
		startResultWatcher((result, note) => spoken.push({ result, note }), () => true);
		assert.ok(await until(() => [plain, priv, noted].every(t => proactiveFor(t.taskId).length > 0), 8000), readdirSync(RESULT_DIR).join(', '));
		assert.equal(readFileSync(join(RESULT_DIR, proactiveFor(plain.taskId)[0]), 'utf-8'), '[channel: place-1]\nThree listings.');
		assert.equal(readFileSync(join(RESULT_DIR, proactiveFor(priv.taskId)[0]), 'utf-8'), '[dm-only]\nPrivate one.');
		for (const t of [plain, priv, noted]) assert.match(proactiveFor(t.taskId)[0], /\.to-fakechan\.txt$/);
		assert.equal(spoken.find(s => s.result === 'Three listings.')?.note, undefined);
		assert.equal(spoken.find(s => s.result === 'Private one.')?.note, DM_ONLY_DELIVERY_NOTE, 'the default note when the adapter supplies none');
		assert.equal(spoken.find(s => s.result === 'Private two.')?.note, 'It went to the DM, not the place.');
		assert.equal(spoken.filter(s => s.result === 'Private one.').length, 1, 'spoken once: the DM file is claimed before the next tick');
	});
});

describe('the core names no product', () => {
	it('no adapter grammar, tag or wording in the bridge, the context builder or the prompt factory', () => {
		for (const f of ['src/task-bridge.ts', 'src/voice-context.ts', 'src/voice-agent-config.ts', 'src/inline-tools.ts']) {
			const src = readFileSync(join(process.cwd(), f), 'utf-8');
			assert.doesNotMatch(src, /\.to-ag2space|MATRIX_ROOM|docked in room|session\.context|voice-room-checks|channel_kind: room/, f);
		}
	});
});
