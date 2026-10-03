import { after, afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { VoiceSessionOrigin } from '../src/task-bridge.js';

const workspace = mkdtempSync(join(tmpdir(), 'sutando-task-attribution-'));
process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_WORKSPACE = workspace;
process.env.DO_NOT_TRACK = '1';
const tasks = join(workspace, 'tasks');
const results = join(workspace, 'results');
const conversation = join(workspace, 'logs', 'conversation.log');
mkdirSync(join(workspace, 'logs'), { recursive: true });

const {
	workTool, setVoiceSessionOrigin, setVoiceTurnsProvider, voiceTaskOrigin,
	forwardOfflineVoiceResult, logSessionBoundary,
} = await import('../src/task-bridge.js');

afterEach(() => {
	setVoiceSessionOrigin(null);
	setVoiceTurnsProvider(null);
	writeFileSync(conversation, '');
});
after(() => rmSync(workspace, { recursive: true, force: true }));

const origin = (target: string, verify = async () => true): VoiceSessionOrigin => ({
	channel: 'fakechan', target, headers: { source_room_id: target }, verify,
});
const delegate = async (task: string) => await workTool.execute({ task }, null as never) as { status: string; taskId: string };
const taskBody = (id: string) => readFileSync(join(tasks, `${id}.txt`), 'utf-8');

function pendingTranscript() {
	let signalRead!: () => void;
	const read = new Promise<void>((resolve) => { signalRead = resolve; });
	let input = '';
	let reads = 0;
	return {
		read,
		setInput(value: string) { input = value; },
		get reads() { return reads; },
		provider: () => {
			reads++;
			signalRead();
			return { items: [{ role: 'assistant', content: 'Previous turn.' }], pendingInput: input, lastUserSpeechAt: Date.now() };
		},
	};
}

async function assertOrigin(id: string, target: string) {
	assert.match(taskBody(id), new RegExp(`^channel_id: ${target}$`, 'm'));
	assert.match(taskBody(id), new RegExp(`^source_room_id: ${target}$`, 'm'));
	assert.equal(voiceTaskOrigin(id)?.target, target);
	const file = await forwardOfflineVoiceResult(id, `answer for ${target}`);
	assert.equal(readFileSync(join(results, file), 'utf-8'), `[channel: ${target}]\nanswer for ${target}`);
}

describe('a voice task retains the context that invoked it', () => {
	it('a room switch during the initial asynchronous watcher probe cannot rebind the invocation', { timeout: 5000 }, async () => {
		setVoiceSessionOrigin(origin('invocation-room-A'));
		setVoiceTurnsProvider(() => [{ role: 'user', content: 'original invocation speech' }]);
		const pending = delegate('work captured before the first await');
		setVoiceSessionOrigin(origin('invocation-room-B'));
		let newProviderReads = 0;
		setVoiceTurnsProvider(() => {
			newProviderReads++;
			return [{ role: 'user', content: 'speech after the first await' }];
		});
		const { taskId } = await pending;
		await assertOrigin(taskId, 'invocation-room-A');
		assert.equal(newProviderReads, 0);
		assert.doesNotMatch(taskBody(taskId), /speech after the first await/);
	});

	it('room A → B during transcription keeps A in the task, remembered origin and result', { timeout: 5000 }, async () => {
		let verifiedA = 0;
		let verifiedB = 0;
		const a = origin('room-A', async () => { verifiedA++; return true; });
		const speech = pendingTranscript();
		setVoiceSessionOrigin(a);
		setVoiceTurnsProvider(speech.provider);
		writeFileSync(conversation, '2026-01-01T00:00:00Z|user|original room context\n');
		const existing = readdirSync(tasks);
		const pending = delegate('collect the room A notes');
		await speech.read;
		assert.deepEqual(readdirSync(tasks), existing, 'the task is still awaiting transcription');
		const readsBeforeSwitch = speech.reads;
		setVoiceSessionOrigin(origin('room-B', async () => { verifiedB++; return true; }));
		speech.setInput('private speech in the next room');
		writeFileSync(conversation, '2026-01-01T00:00:01Z|user|private next-room context\n');
		const { taskId } = await pending;
		await assertOrigin(taskId, 'room-A');
		assert.equal(verifiedA, 1);
		assert.equal(verifiedB, 0);
		assert.match(taskBody(taskId), /original room context/);
		assert.doesNotMatch(taskBody(taskId), /private speech|private next-room|--- spoken/);
		assert.equal(speech.reads, readsBeforeSwitch, 'a reused provider is not read after the room changes');
	});

	it('disconnect/reconnect cannot substitute the new client or its transcript', { timeout: 5000 }, async () => {
		const speech = pendingTranscript();
		setVoiceSessionOrigin(origin('client-A'));
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('finish work requested by client A');
		await speech.read;
		setVoiceSessionOrigin(null);
		setVoiceTurnsProvider(null);
		setVoiceSessionOrigin(origin('client-B'));
		let newClientReads = 0;
		setVoiceTurnsProvider(() => {
			newClientReads++;
			return [{ role: 'user', content: 'new client confidential words' }];
		});
		const { taskId } = await pending;
		await assertOrigin(taskId, 'client-A');
		assert.equal(newClientReads, 0);
		assert.doesNotMatch(taskBody(taskId), /new client confidential|--- spoken/);
	});

	it('disconnect/reconnect on the same room and provider still closes the old transcript window', { timeout: 5000 }, async () => {
		const speech = pendingTranscript();
		const a = origin('same-room');
		setVoiceSessionOrigin(a);
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('work before the same-room reconnect');
		await speech.read;
		setVoiceSessionOrigin(null);
		setVoiceSessionOrigin(a);
		speech.setInput('speech from the reconnected client');
		const { taskId } = await pending;
		await assertOrigin(taskId, 'same-room');
		assert.doesNotMatch(taskBody(taskId), /reconnected client|--- spoken/);
	});

	it('a new provider without a room change cannot supply the preceding session transcript', { timeout: 5000 }, async () => {
		const speech = pendingTranscript();
		setVoiceSessionOrigin(origin('unchanged-room'));
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('work before the provider replacement');
		await speech.read;
		let replacementReads = 0;
		setVoiceTurnsProvider(() => {
			replacementReads++;
			return [{ role: 'user', content: 'replacement session speech' }];
		});
		const { taskId } = await pending;
		await assertOrigin(taskId, 'unchanged-room');
		assert.equal(replacementReads, 0);
		assert.doesNotMatch(taskBody(taskId), /replacement session|--- spoken/);
	});

	it('a logical session boundary invalidates late words even when the provider is reused', { timeout: 5000 }, async () => {
		const speech = pendingTranscript();
		setVoiceSessionOrigin(origin('session-room'));
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('work before goodbye');
		await speech.read;
		logSessionBoundary('test-goodbye');
		speech.setInput('words after goodbye');
		const { taskId } = await pending;
		await assertOrigin(taskId, 'session-room');
		assert.doesNotMatch(taskBody(taskId), /words after goodbye|--- spoken/);
	});

	it('a task begun with no origin does not acquire one during the wait', { timeout: 5000 }, async () => {
		const speech = pendingTranscript();
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('work requested without a room');
		await speech.read;
		setVoiceSessionOrigin(origin('later-room'));
		speech.setInput('later room words');
		const { taskId } = await pending;
		assert.match(taskBody(taskId), /^channel_id: local-voice$/m);
		assert.doesNotMatch(taskBody(taskId), /^source_room_id:|later room words/m);
		assert.equal(voiceTaskOrigin(taskId), null);
		const file = await forwardOfflineVoiceResult(taskId, 'owner answer');
		assert.equal(readFileSync(join(results, file), 'utf-8'), 'owner answer');
	});

	it('origin values are copied and a failed original membership check still falls back to the owner', { timeout: 5000 }, async () => {
		const speech = pendingTranscript();
		let allowed = true;
		const a = origin('immutable-room', async () => allowed);
		setVoiceSessionOrigin(a);
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('work before the adapter mutates its record');
		await speech.read;
		a.target = 'mutated-room';
		a.headers!.source_room_id = 'mutated-room';
		speech.setInput('original session words arrive late');
		const { taskId } = await pending;
		assert.match(taskBody(taskId), /^channel_id: immutable-room$/m);
		assert.match(taskBody(taskId), /^source_room_id: immutable-room$/m);
		assert.match(taskBody(taskId), /user: original session words arrive late/);
		assert.equal(voiceTaskOrigin(taskId)?.target, 'immutable-room');
		allowed = false;
		const file = await forwardOfflineVoiceResult(taskId, 'membership changed');
		assert.equal(readFileSync(join(results, file), 'utf-8'), 'membership changed');
	});

	it('a fresh bridge process reconstructs the original result destination from the written task', { timeout: 10000 }, async () => {
		const speech = pendingTranscript();
		setVoiceSessionOrigin(origin('persisted-room-A'));
		setVoiceTurnsProvider(speech.provider);
		const pending = delegate('work completed after the bridge restarts');
		await speech.read;
		setVoiceSessionOrigin(origin('persisted-room-B'));
		speech.setInput('new room transcript');
		const { taskId } = await pending;
		const bridgeUrl = new URL('../src/task-bridge.ts', import.meta.url).href;
		const script = `
			const bridge = await import(${JSON.stringify(bridgeUrl)});
			bridge.setVoiceSessionOrigin({ channel: 'fakechan', target: 'restart-room-C' });
			bridge.setVoiceTaskOriginResolver(lines => {
				const target = lines.find(line => line.startsWith('source_room_id: '))?.slice(16);
				return target ? { channel: 'fakechan', target, verify: async () => true } : null;
			});
			const file = await bridge.forwardOfflineVoiceResult(${JSON.stringify(taskId)}, 'result after restart');
			console.log('RESTART_RESULT=' + file);
		`;
		const child = spawnSync(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', script], {
			cwd: process.cwd(), env: process.env, encoding: 'utf-8', timeout: 7000,
		});
		assert.equal(child.status, 0, `${child.error ?? ''}\n${child.stderr}`);
		const file = child.stdout.split('\n').find(line => line.startsWith('RESTART_RESULT='))?.slice(15);
		assert.ok(file, child.stdout);
		assert.equal(readFileSync(join(results, file), 'utf-8'), '[channel: persisted-room-A]\nresult after restart');
	});
});
