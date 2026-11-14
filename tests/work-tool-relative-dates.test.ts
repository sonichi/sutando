// Live 2026-10-21 21:46 and 21:57 PT (xueqing): "how many PRs did I merge yesterday / the day before yesterday" went to
// the core as "…yesterday, October 21, 2026" and "…the day before yesterday, October 20, 2026", each a day late (the
// voice model's own date is UTC's). The core resolved the relative day from local_time and answered right, but a task
// carrying only the wrong date would be counted on the wrong day. The voice model passes the user's words; the core,
// which knows the owner's zone, turns them into dates.
// Run: npx tsx --test tests/work-tool-relative-dates.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const { workTool } = await import('../src/task-bridge.js');

describe('a relative date goes to the core as the user said it', () => {
	it('the work tool tells the voice model to keep "yesterday" and never add a calendar date', () => {
		const task = (workTool.parameters as unknown as { shape: { task: { description?: string } } }).shape.task.description ?? '';
		assert.match(task, /yesterday/i, 'names the relative words to keep');
		assert.match(task, /never .*calendar date|calendar date.* never/i, 'forbids converting them to a date');
	});

	it('the core lets the relative day win over a calendar date that disagrees, and takes the weekday from the date', () => {
		for (const f of ['CLAUDE.md', 'AGENTS.md']) {
			const md = readFileSync(new URL(`../${f}`, import.meta.url), 'utf-8');
			const line = md.split('\n').find((l) => l.startsWith('- Dates in a task are the owner')) ?? '';
			assert.match(line, /relative day wins/, `${f}: a disagreeing calendar date loses`);
			assert.match(line, /weekday/i, `${f}: the weekday comes from the date`);
		}
	});
});
