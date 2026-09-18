import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Asked to open a pull request in the second notch, the voice model called `work` instead of
// the inline notch tool: it answered "Working on it." and the page appeared 29s later. The
// instruction telling it not to already shipped in show_triage_in_notch's result and had been
// delivered 26s earlier; it was ignored. `work` refusing the call is the correction that lands
// at the moment of the mistake, the same shape as the describe_screen redirect beside it.
//
// The gate must stay narrow: work ON the notch — its skill, process, code, packaging — is real
// background work, and refusing that would strand it with no route at all.

// Fixtures use a tmp workspace, never the live queue: SUTANDO_TEST_MODE=1 must be set before
// the source-ordered `await import` binds the bridge's paths.
process.env.SUTANDO_WORKSPACE = mkdtempSync(join(tmpdir(), 'sutando-notch-gate-test-'));
process.env.SUTANDO_TEST_MODE = '1';

const { _isNotchOpenRequest } = await import('../src/task-bridge.js');

describe('_isNotchOpenRequest — putting something IN a notch never goes to work', () => {
	// Every one of these is a real utterance from the two recurrences, ASR output included.
	for (const task of [
		'Open PR 20 in another notch.',
		'Can Can you open PR20 in another notch?',
		'Can you open the PR-20 in another notch?',
		'Hi, open the triage in the notch.',
		'show the triage queue in the second notch',
		'bring up issue 183 in the other notch',
		'display the PR in a notch',
	]) {
		it(`refuses: ${task}`, () => assert.equal(_isNotchOpenRequest(task), true));
	}

	it('refuses a pasted URL — the dots in a hostname must not end the match early', () => {
		assert.equal(
			_isNotchOpenRequest('put github.com/sonichi/sutando/pull/4269 in the notch'), true);
		assert.equal(
			_isNotchOpenRequest('open https://ag2.space/home/x in the web notch'), true);
	});
});

describe('_isNotchOpenRequest — work ON the notch still routes to work', () => {
	// The first two are verbatim asks from the live triage queue the day the gate was added.
	for (const task of [
		'Should Susan’s bot restart the Notch process to see if the UI panel displays?',
		'Should Susan’s bot update the voice notch skill import path to use .ts?',
		'fix the voice-notch import path',
		'rebuild the notch binary',
		'debug why the notch overlay is blank',
	]) {
		it(`passes through: ${task}`, () => assert.equal(_isNotchOpenRequest(task), false));
	}

	it('does not span a sentence boundary', () => {
		assert.equal(
			_isNotchOpenRequest('Open the dist files. Then restart the notch process.'), false);
	});

	it('ignores tasks with no notch in them at all', () => {
		assert.equal(_isNotchOpenRequest('open a PR against sutando-skills'), false);
		assert.equal(_isNotchOpenRequest('what is waiting on me'), false);
	});
});
