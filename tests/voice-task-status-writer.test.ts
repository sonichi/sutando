import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createVoiceTaskStatusWriter } from '../src/voice-task-status-writer.js';
import { createVoiceTaskStore } from '../src/relay-agent.js';

const dir = mkdtempSync(join(tmpdir(), 'vts-'));

describe('the task status writer (the table in memory, a copy written for the page)', () => {
	it('writes asynchronously after a change, and the copy is the table as it is now', async () => {
		const store = createVoiceTaskStore();
		const path = join(dir, 'state', 'a.json');
		let snaps = 0;
		const w = createVoiceTaskStatusWriter({ path, snapshot: () => { snaps++; return store.list(); } });
		store.onChange(() => w.changed());
		store.add('task-1', 'check PR 1');
		assert.equal(existsSync(path), false, 'nothing written synchronously: the table never waits');
		await w.flush();
		assert.deepEqual(JSON.parse(readFileSync(path, 'utf-8')).map((r: [string]) => r[0]), ['task-1']);
		assert.equal(snaps, 1);
	});

	it('changes that arrive during a write are coalesced: the last state wins, in few writes', async () => {
		const store = createVoiceTaskStore();
		const path = join(dir, 'state', 'b.json');
		let snaps = 0;
		const w = createVoiceTaskStatusWriter({ path, snapshot: () => { snaps++; return store.list(); } });
		store.onChange(() => w.changed());
		for (let i = 0; i < 50; i++) store.add(`task-${i}`, `q ${i}`);
		await w.flush();
		assert.equal(JSON.parse(readFileSync(path, 'utf-8')).length, 50);
		assert.ok(snaps <= 3, `50 changes took ${snaps} snapshots`);
	});

	it('a failed write is logged, not thrown, and the next change writes again', async () => {
		const blocker = join(dir, 'file-not-dir');
		writeFileSync(blocker, 'x');
		const logs: string[] = [];
		let target = join(blocker, 'c.json');
		const w = createVoiceTaskStatusWriter({ path: target, snapshot: () => ({ n: 1 }), log: (m) => logs.push(m) });
		w.changed();
		await w.flush();
		assert.ok(logs.some((l) => l.includes('task status not written')));
		target = join(dir, 'ok.json');
		const w2 = createVoiceTaskStatusWriter({ path: target, snapshot: () => ({ n: 2 }) });
		w2.changed();
		await w2.flush();
		assert.deepEqual(JSON.parse(readFileSync(target, 'utf-8')), { n: 2 });
	});
});

describe('voice-agent wiring', () => {
	it('the status page reads the copy the voice agent writes on each change of the table', () => {
		const src = (f: string) => readFileSync(join(import.meta.dirname ?? '.', '..', 'src', f), 'utf-8');
		const agent = src('voice-agent.ts');
		assert.match(agent, /voiceTaskStore\.onChange\(\(\) => voiceTaskStatus\.changed\(\)\)/);
		assert.ok(agent.includes("statusPath('voice-task-status.json', WORKSPACE_DIR)"));
		assert.ok(!agent.includes('setInterval(publishVoiceTaskStatus'), 'no polling');
		const web = src('web-client.ts');
		assert.ok(web.includes("url.pathname === '/voice-tasks'") && web.includes("'voice-task-status.json'") && web.includes("url.pathname === '/voice-tasks/view'"));
	});
});
