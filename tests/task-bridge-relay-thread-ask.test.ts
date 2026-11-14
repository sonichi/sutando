import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Relay mode (CORE_API_URL): results come from the core host's agent-api. The bare
// `[thread]` control line must not reach the voice callback or the task-status log.
const BODIES: Record<string, string> = {
	'task-relay-thread.txt': '[thread]\nanswer body\n',
	'task-relay-skip-after-thread.txt': '[thread]\n[no-send]\nvisible after\n',
};
const archived: string[] = [];
const server = createServer((req, res) => {
	const url = req.url ?? '';
	res.setHeader('Content-Type', 'application/json');
	if (req.method === 'GET' && url === '/delegation/results') {
		res.end(JSON.stringify({ files: Object.keys(BODIES).filter(f => !archived.includes(f)) }));
	} else if (req.method === 'GET' && url.startsWith('/delegation/results/')) {
		res.end(JSON.stringify({ body: BODIES[decodeURIComponent(url.split('/').pop()!)] ?? '' }));
	} else if (req.method === 'POST' && url === '/delegation/archive') {
		let raw = '';
		req.on('data', c => { raw += c; });
		req.on('end', () => { archived.push(JSON.parse(raw).name); res.end('{}'); });
	} else {
		res.end('{}');
	}
});
await new Promise<void>(r => server.listen(0, '127.0.0.1', () => r()));
const port = (server.address() as { port: number }).port;
process.env.SUTANDO_WORKSPACE = mkdtempSync(join(tmpdir(), 'sutando-relay-thread-'));
process.env.SUTANDO_TEST_MODE = '1';
process.env.CORE_API_URL = `http://127.0.0.1:${port}`;

const { startResultWatcher, _pendingTasksForTest, setTaskStatusCallback } = await import('../src/task-bridge.js');

const until = async (cond: () => boolean, ms: number) => {
	const end = Date.now() + ms;
	while (Date.now() < end) {
		if (cond()) return true;
		await new Promise(r => setTimeout(r, 100));
	}
	return cond();
};

describe('relay-mode result watcher', () => {
	it('speaks and logs the answer without the bare [thread] line; a skip right after it is a skip', async () => {
		for (const f of Object.keys(BODIES)) {
			_pendingTasksForTest.set(f.replace('.txt', ''), { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: f });
		}
		const statuses: string[] = [];
		setTaskStatusCallback((_id: string, _s: string, _t: string, result?: string) => { if (result) statuses.push(result); });
		const spoken: string[] = [];
		startResultWatcher((result: string) => { spoken.push(result); }, () => true);
		assert.ok(await until(() => archived.length === 2 && spoken.length === 1, 10000),
			`spoken=${JSON.stringify(spoken)} archived=${JSON.stringify(archived)}`);
		server.close();
		await new Promise(r => setTimeout(r, 2500));
		assert.deepEqual(spoken, ['[Task result for task-relay-thread]\nanswer body']);
		assert.ok(!statuses.some(s => s.includes('[thread]')), `status log carried the marker: ${JSON.stringify(statuses)}`);
	});
});
