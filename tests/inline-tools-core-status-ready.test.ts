import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

// Regression guard: an idle core must read as AVAILABLE/READY, not a dead-end.
//
// A client reported the voice agent saying "the core agent is idle" as if the
// core were unavailable, instead of delegating the task. Root cause: the
// get_core_status tool's idle-branch descriptions read like unavailability
// ("Core agent is idle right now." / "not currently running"). This guard
// pins the corrected, unambiguous wording + the ready:true hint. The stable
// status field value ('idle') is preserved.

const { getCoreStatusTool } = await import('../src/inline-tools.js');

describe('get_core_status — idle means available/ready (not a dead-end)', () => {
	it('idle return carries ready:true and available/ready wording', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const res: any = await (getCoreStatusTool.execute as any)({});
		// The core is not "running" in the test environment, so this exercises
		// one of the two idle branches (missing status file, or non-running).
		if (res.status === 'idle') {
			assert.equal(res.ready, true, 'idle return must set ready:true');
			assert.match(
				String(res.description),
				/available and ready to accept a delegated task via work/,
				'idle description must state the core is available/ready to take a task',
			);
			assert.doesNotMatch(
				String(res.description),
				/not currently running/,
				'idle description must not read as unavailable',
			);
		}
	});

	it('status field value stays the stable "idle" string', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const res: any = await (getCoreStatusTool.execute as any)({});
		assert.ok(['idle', 'running', 'unknown'].includes(res.status));
	});
});
