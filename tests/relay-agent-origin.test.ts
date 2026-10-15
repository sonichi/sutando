import { after, afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { VoiceSessionOrigin } from '../src/task-bridge.js';

const workspace = mkdtempSync(join(tmpdir(), 'sutando-relay-origin-'));
process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_WORKSPACE = workspace;
process.env.DO_NOT_TRACK = '1';
mkdirSync(join(workspace, 'logs'), { recursive: true });

const { submitWorkTask, captureWorkOrigin, setVoiceSessionOrigin, voiceTaskStore, voiceTaskOrigin } = await import('../src/task-bridge.js');
const { RelayAgent, createSpeechGate } = await import('../src/relay-agent.js');

afterEach(() => setVoiceSessionOrigin(null));
after(() => rmSync(workspace, { recursive: true, force: true }));

const origin = (target: string): VoiceSessionOrigin => ({
	channel: 'fakechan', target, headers: { source_room_id: target }, verify: async () => true,
});
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
// The user just stopped speaking: a call is held for the 200ms quiet that ends their turn.
function holdingGate() {
	const speech = createSpeechGate({ quietMs: 200 });
	speech.onUserSpeechStarted();
	speech.onUserSpeechEnded();
	return speech;
}

// The relay agent wired as voice-agent.ts wires it, recording the task ids the core was given.
function relayAgent(extra: Partial<ConstructorParameters<typeof RelayAgent>[0]> = {}) {
	const ids: string[] = [];
	const relay = new RelayAgent({
		submit: async (args, at) => {
			const r = await submitWorkTask(args, at as never);
			if (typeof r.taskId === 'string') ids.push(r.taskId);
			return r;
		},
		captureOrigin: captureWorkOrigin,
		store: voiceTaskStore,
		...extra,
	});
	return { relay, ids };
}

async function untilSubmitted(ids: string[]): Promise<string> {
	for (let i = 0; i < 200 && ids.length === 0; i++) await sleep(10);
	await sleep(50);
	assert.equal(ids.length, 1, 'one task was submitted');
	return ids[0];
}

function assertRoom(id: string, target: string) {
	const body = readFileSync(join(workspace, 'tasks', `${id}.txt`), 'utf-8');
	assert.match(body, new RegExp(`^channel_id: ${target}$`, 'm'));
	assert.match(body, new RegExp(`^source_room_id: ${target}$`, 'm'));
	assert.equal(voiceTaskOrigin(id)?.target, target);
}

describe('the relay agent keeps the room a request was made in', () => {
	it('a room switch while the call waits for the user to finish speaking', { timeout: 5000 }, async () => {
		setVoiceSessionOrigin(origin('room-A'));
		const { relay, ids } = relayAgent({ speech: holdingGate() });
		void relay.invoke('', { task: 'summarize the room A thread' });
		await sleep(50);
		assert.deepEqual(ids, [], 'still held');
		setVoiceSessionOrigin(origin('room-B'));
		const id = await untilSubmitted(ids);
		assertRoom(id, 'room-A');
		relay.endCall(id, 'continuing');
	});

	it('a room switch while the work check runs', { timeout: 5000 }, async () => {
		setVoiceSessionOrigin(origin('room-A'));
		const { relay, ids } = relayAgent({
			check: async () => { setVoiceSessionOrigin(origin('room-B')); return { action: 'submit' }; },
		});
		void relay.invoke('', { task: 'list the open questions in room A' });
		const id = await untilSubmitted(ids);
		assertRoom(id, 'room-A');
		relay.endCall(id, 'continuing');
	});

	it('a cut-off call joined by the next one keeps the room the request began in', { timeout: 5000 }, async () => {
		setVoiceSessionOrigin(origin('room-A'));
		const { relay, ids } = relayAgent({ speech: holdingGate() });
		void relay.invoke('', { task: 'draft a reply to' });
		await sleep(50);
		setVoiceSessionOrigin(origin('room-B'));
		void relay.invoke('', { task: 'the room A question about the release' });
		const id = await untilSubmitted(ids);
		assertRoom(id, 'room-A');
		relay.endCall(id, 'continuing');
	});

	it('a turn the turn check sends, with a room switch during the hold', { timeout: 5000 }, async () => {
		setVoiceSessionOrigin(origin('room-A'));
		const { relay, ids } = relayAgent({
			speech: holdingGate(),
			classify: async () => ({ action: 'work' as const, task: 'check the room A build status' }),
		});
		relay.onUserTurn('check the build status');
		await sleep(50);
		setVoiceSessionOrigin(origin('room-B'));
		const id = await untilSubmitted(ids);
		assertRoom(id, 'room-A');
	});
});
