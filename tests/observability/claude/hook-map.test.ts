import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { hookMap } from '../../../src/observability/claude/hook-map.js';
import type { MapContext } from '../../../src/observability/claude/cc-records.js';

const ctx: MapContext = { node: 'mac-studio', receivedAt: 1_717_900_000 };

describe('hookMap — obs only, with file-op pairing', () => {
	it('PostToolUse Read → tool.result AND a paired file.read carrying source_file', () => {
		const out = hookMap({ hook_event_name: 'PostToolUse', session_id: 'sess-9', cwd: '/repo', tool_name: 'Read', tool_input: { file_path: 'tasks/task-1.txt' }, tool_output: 'contents' }, ctx);
		assert.equal(out.usage.length, 0); // hooks NEVER produce usage
		assert.equal(out.events.length, 2);
		assert.equal(out.events[0].kind, 'tool.result');
		assert.equal(out.events[1].kind, 'file.read');
		assert.equal(out.events[1].source_file, 'tasks/task-1.txt');
		assert.equal(out.events[1].outcome, 'ok');
	});

	it('PostToolUseFailure → outcome error (and Write pairs file.change)', () => {
		const out = hookMap({ hook_event_name: 'PostToolUseFailure', session_id: 'sess-9', tool_name: 'Write', tool_input: { file_path: 'notes/x.md' }, error: 'EACCES' }, ctx);
		assert.equal(out.events[0].kind, 'tool.result');
		assert.equal(out.events[0].outcome, 'error');
		const fe = out.events.find((e) => e.kind === 'file.change');
		assert.ok(fe);
		assert.equal(fe!.source_file, 'notes/x.md');
		assert.equal((fe!.data as Record<string, unknown>).op, 'written');
	});

	it('SessionEnd → cc.hook.session_end with end_reason', () => {
		const out = hookMap({ hook_event_name: 'SessionEnd', session_id: 'sess-9', end_reason: 'clear' }, ctx);
		assert.equal(out.events[0].kind, 'cc.hook.session_end');
		assert.equal((out.events[0].data as Record<string, unknown>).end_reason, 'clear');
	});

	it('unknown event → forward-compatible cc.hook.<snake> default branch', () => {
		const out = hookMap({ hook_event_name: 'CwdChanged', session_id: 'sess-9', previous_cwd: '/old' }, ctx);
		assert.equal(out.events[0].kind, 'cc.hook.cwd_changed');
		assert.equal((out.events[0].data as Record<string, unknown>).previous_cwd, '/old');
	});

	it('non-file tool (Bash) PostToolUse → only tool.result, no file event', () => {
		const out = hookMap({ hook_event_name: 'PostToolUse', session_id: 'sess-9', tool_name: 'Bash', tool_input: { command: 'ls' } }, ctx);
		assert.equal(out.events.length, 1);
		assert.equal(out.events[0].kind, 'tool.result');
	});
});
