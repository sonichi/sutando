// Recent phone calls are read from the workspace, where the phone server writes calls.jsonl.
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Set before the import: voice-context binds its workspace path at module load.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-context-calls-'));
const WORKSPACE = join(TMP, 'workspace');
process.env.SUTANDO_MEMORY_DIR = join(TMP, 'memory');
process.env.SUTANDO_WORKSPACE = WORKSPACE;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(WORKSPACE, 'results', 'calls'), { recursive: true });

const { buildVoiceAgentContext } = await import('../src/voice-context.js');
after(() => rmSync(TMP, { recursive: true, force: true }));

describe('buildVoiceAgentContext — recent phone calls', () => {
	it('lists the last calls from <workspace>/results/calls/calls.jsonl, newest first', () => {
		const rows = [
			{ caller: 'Alice', start_time: '2026-11-01T10:00:00Z', summary: 'call one' },
			{ caller: 'Bob', start_time: '2026-11-02T10:00:00Z', summary: 'call two' },
		];
		writeFileSync(join(WORKSPACE, 'results', 'calls', 'calls.jsonl'), rows.map((r) => JSON.stringify(r)).join('\n') + '\n');
		const lines = buildVoiceAgentContext().split('\n');
		const at = lines.indexOf('RECENT PHONE CALLS:');
		assert.ok(at >= 0, 'the section is present');
		assert.match(lines[at + 1], /Bob: call two$/);
		assert.match(lines[at + 2], /Alice: call one$/);
	});

	it('no calls file in the workspace: no section', () => {
		rmSync(join(WORKSPACE, 'results', 'calls', 'calls.jsonl'));
		assert.ok(!buildVoiceAgentContext().includes('RECENT PHONE CALLS:'));
	});
});
