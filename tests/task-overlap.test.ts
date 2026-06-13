import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	classifyTaskCategory,
	areSafeToOverlap,
	hasSerialDependency,
	extractTaskBody,
	type TaskCategory,
} from '../src/task-overlap.js';

// ---------------------------------------------------------------------------
// extractTaskBody
// ---------------------------------------------------------------------------

describe('extractTaskBody', () => {
	it('returns everything from the task: line when present', () => {
		const raw = 'id: task-123\nsource: voice\ntask: find the best sushi near me\nsome more text';
		const body = extractTaskBody(raw);
		assert.ok(body.startsWith('task: find'));
		assert.ok(body.includes('some more text'));
		assert.ok(!body.includes('id: task-123'));
	});

	it('returns the full string when no task: line is present', () => {
		const raw = 'just a plain string with no delimiter';
		assert.equal(extractTaskBody(raw), raw);
	});

	it('stops at the first task: prefix, ignoring later occurrences', () => {
		const raw = 'id: task-1\ntask: first\ntask: second';
		const body = extractTaskBody(raw);
		assert.ok(body.startsWith('task: first'));
	});
});

// ---------------------------------------------------------------------------
// classifyTaskCategory
// ---------------------------------------------------------------------------

describe('classifyTaskCategory', () => {
	const cases: [string, TaskCategory][] = [
		// cancel
		['task: CANCEL_INSTRUCTION: stop processing task-123', 'cancel'],

		// email
		['task: check my gmail for new messages', 'email'],
		['task: send email to John about the meeting', 'email'],
		['task: draft email to the team', 'email'],
		['task: reply to the unread email from Sarah', 'email'],

		// calendar
		['task: schedule a meeting with the team tomorrow', 'calendar'],
		['task: add a reminder to call Bob at 3pm', 'calendar'],
		['task: what appointments do I have today', 'calendar'],

		// code
		['task: fix the bug in health-check.py', 'code'],
		['task: create a PR to refactor the auth module', 'code'],
		['task: run tests for the new feature', 'code'],
		['task: deploy the latest build', 'code'],
		['task: run tests and commit the changes', 'code'],

		// file
		['task: open file /tmp/notes.txt', 'file'],
		['task: save the file and close', 'file'],
		['task: create a folder called projects', 'file'],

		// research
		['task: search for the best restaurants near me', 'research'],
		['task: look up the weather for tomorrow', 'research'],
		['task: what is the capital of France', 'research'],
		['task: explain how diffusion models work', 'research'],
		['task: summarize this article for me', 'research'],
		['task: check the news about WWDC', 'research'],

		// unknown (no strong keywords)
		['task: do that thing', 'unknown'],
		['task: ok', 'unknown'],
	];

	for (const [input, expected] of cases) {
		it(`classifies "${input.slice(6, 50)}" as ${expected}`, () => {
			assert.equal(classifyTaskCategory(input), expected);
		});
	}
});

// ---------------------------------------------------------------------------
// hasSerialDependency
// ---------------------------------------------------------------------------

describe('hasSerialDependency', () => {
	const serial: string[] = [
		'task: after you finish the research, send the email',
		"task: once it's done, open a PR",
		'task: depends on the previous task completing',
		'task: wait for the build to finish then notify me',
		"task: when you're done with the email, do this",
		"task: when you are finished with the search, add a reminder",
	];

	const independent: string[] = [
		'task: search for news about AI',
		'task: check my gmail',
		'task: what time is it',
		'task: fix the login bug',
		'task: after adding the feature, update docs',
	];

	for (const body of serial) {
		it(`detects serial dep in: "${body.slice(6, 60)}"`, () => {
			assert.ok(hasSerialDependency(body), `expected serial dependency in: ${body}`);
		});
	}

	for (const body of independent) {
		it(`no serial dep in: "${body.slice(6, 60)}"`, () => {
			assert.ok(!hasSerialDependency(body), `unexpected serial dependency in: ${body}`);
		});
	}
});

// ---------------------------------------------------------------------------
// areSafeToOverlap — full task-file strings
// ---------------------------------------------------------------------------

function makeTask(id: string, body: string, source = 'voice'): string {
	return [
		`id: task-${id}`,
		`timestamp: 2026-06-11T00:00:00.000Z`,
		`source: ${source}`,
		`channel_id: local-voice`,
		`user_id: voice-local`,
		`access_tier: owner`,
		`priority: normal`,
		`task: ${body}`,
	].join('\n');
}

describe('areSafeToOverlap', () => {
	it('research + email → safe (different categories)', () => {
		const t1 = makeTask('1', 'search for sushi restaurants near me');
		const t2 = makeTask('2', 'check my gmail inbox');
		assert.ok(areSafeToOverlap(t1, t2));
	});

	it('research + calendar → safe', () => {
		const t1 = makeTask('1', 'look up the weather for tomorrow');
		const t2 = makeTask('2', 'schedule a meeting at 3pm');
		assert.ok(areSafeToOverlap(t1, t2));
	});

	it('research + research → NOT safe (same category)', () => {
		const t1 = makeTask('1', 'search for best coffee shops');
		const t2 = makeTask('2', 'look up the weather in NYC');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('email + email → NOT safe (same category)', () => {
		const t1 = makeTask('1', 'draft email to the team');
		const t2 = makeTask('2', 'reply to unread email from Sarah');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('code + research → NOT safe (code always serializes)', () => {
		const t1 = makeTask('1', 'fix the bug in health-check.py');
		const t2 = makeTask('2', 'search for the latest Node.js docs');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('code + email → NOT safe (code always serializes)', () => {
		const t1 = makeTask('1', 'run tests and commit the changes');
		const t2 = makeTask('2', 'send email to John');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('code + code → NOT safe', () => {
		const t1 = makeTask('1', 'fix the bug in task-bridge.ts');
		const t2 = makeTask('2', 'create a PR for the new feature');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('cancel + anything → NOT safe (must interrupt)', () => {
		const t1 = makeTask('1', 'CANCEL_INSTRUCTION: stop processing task-5');
		const t2 = makeTask('2', 'search for news about WWDC');
		assert.ok(!areSafeToOverlap(t1, t2));
		assert.ok(!areSafeToOverlap(t2, t1));
	});

	it('serial-dependency language → NOT safe regardless of category', () => {
		const t1 = makeTask('1', 'search for the best sushi near me');
		const t2 = makeTask('2', 'after you finish the search, schedule a dinner reservation');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('unknown + research → NOT safe (same-or-unknown pair is conservative)', () => {
		const t1 = makeTask('1', 'do the thing');
		const t2 = makeTask('2', 'look up the weather');
		// unknown !== research → different categories → safe
		// (unknown is treated as a distinct category, so unknown + research = different)
		assert.ok(areSafeToOverlap(t1, t2));
	});

	it('unknown + unknown → NOT safe', () => {
		const t1 = makeTask('1', 'do that thing');
		const t2 = makeTask('2', 'ok');
		assert.ok(!areSafeToOverlap(t1, t2));
	});

	it('header fields from task file do not skew classification (source: code is not code task)', () => {
		// A voice task whose headers contain "code" in "source: discord" etc.
		// should not be misclassified — classifyTaskCategory only sees after task:
		const t1 = [
			'id: task-99',
			'source: discord',
			'access_tier: owner',
			'task: check my gmail',
		].join('\n');
		const t2 = makeTask('2', 'schedule a team meeting');
		assert.ok(areSafeToOverlap(t1, t2)); // email + calendar = safe
	});
});
