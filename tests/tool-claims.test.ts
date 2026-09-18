import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

// A tool declares the request shapes it owns; the work bridge refuses those and names the
// tool. The declaration is data, so everything here runs against a fake table — no skill is
// installed, and core names nothing.

const { parseToolClaims, registerToolClaims, claimFor, clearToolClaims, registeredToolClaims } =
	await import('../src/tool_claims.js');

// Shaped like the one voice-notch ships, because that is the case this exists for.
const NOTCH = {
	tool: 'show_web_in_notch',
	match: '\\b(open|show|put|bring\\s+up|pull\\s+up|display)\\b(?:(?!\\.\\s)[^?!]){0,60}?\\bnotch\\b',
	unless: '\\bnotch\\b[\\s-]*(skill|process|binary|code|import|path|panel|overlay|repo)'
		+ '|voice[\\s-]?notch|\\b(fix|restart|rebuild|install|update|debug|patch)\\b[^.?!]{0,40}?\\bnotch\\b',
	message: 'Never open a notch through work. Call show_web_in_notch yourself.',
};

beforeEach(() => clearToolClaims());

describe('claimFor — a claimed shape is refused and the owning tool named', () => {
	for (const task of [
		'Open PR 20 in another notch.',
		'Can Can you open PR20 in another notch?',
		'Hi, open the triage in the notch.',
		'bring up issue 183 in the other notch',
	]) {
		it(`claims: ${task}`, () => {
			assert.equal(claimFor(task, [NOTCH])?.tool, 'show_web_in_notch');
		});
	}

	it('a pasted URL does not end the match early — dots are not a sentence break', () => {
		assert.equal(claimFor('put github.com/sonichi/sutando/pull/4269 in the notch', [NOTCH])?.tool,
			'show_web_in_notch');
	});

	it('`unless` withdraws the claim so real work on the subject still routes to work', () => {
		// Both are verbatim asks from the live triage queue the day this was written.
		assert.equal(claimFor('Should the bot restart the Notch process to see if it displays?', [NOTCH]), null);
		assert.equal(claimFor('Should the bot update the voice notch skill import path to use .ts?', [NOTCH]), null);
		assert.equal(claimFor('Open the dist files. Then restart the notch process.', [NOTCH]), null);
	});

	it('an unrelated task is claimed by nobody', () => {
		assert.equal(claimFor('open a PR against sutando-skills', [NOTCH]), null);
		assert.equal(claimFor('what is waiting on me', [NOTCH]), null);
	});

	it('the first claim wins and its own message is returned', () => {
		const other = { tool: 'other_tool', match: 'notch', message: 'second' };
		assert.equal(claimFor('open the triage in the notch', [NOTCH, other])?.message,
			NOTCH.message);
	});

	it('an empty table claims nothing', () => {
		assert.equal(claimFor('open the triage in the notch', []), null);
	});
});

describe('parseToolClaims — a manifest is untrusted data', () => {
	const quiet = () => {};

	it('drops a claim whose tool did not load, so it cannot name an uncallable tool', () => {
		assert.deepEqual(parseToolClaims([NOTCH], ['some_other_tool'], quiet), []);
		assert.equal(parseToolClaims([NOTCH], ['show_web_in_notch'], quiet).length, 1);
	});

	it('drops entries missing tool, match or message', () => {
		const raw = [
			{ tool: 'show_web_in_notch', match: 'x' },
			{ tool: 'show_web_in_notch', message: 'x' },
			{ match: 'x', message: 'x' },
			{ tool: 'show_web_in_notch', match: '', message: 'x' },
			'not an object',
			null,
		];
		assert.deepEqual(parseToolClaims(raw, ['show_web_in_notch'], quiet), []);
	});

	it('drops a non-string unless rather than coercing it', () => {
		assert.deepEqual(
			parseToolClaims([{ ...NOTCH, unless: 7 }], ['show_web_in_notch'], quiet), []);
	});

	it('a non-array claims field is simply absent', () => {
		assert.deepEqual(parseToolClaims(undefined, ['show_web_in_notch'], quiet), []);
		assert.deepEqual(parseToolClaims({ tool: 'x' }, ['show_web_in_notch'], quiet), []);
	});
});

describe('registry — a host without the skill keeps the unclaimed behaviour', () => {
	it('is empty until a loaded skill registers, and claimFor then claims nothing', () => {
		assert.deepEqual(registeredToolClaims(), []);
		assert.equal(claimFor('Open PR 20 in another notch.'), null);
	});

	it('registering makes the shape claimed process-wide', () => {
		registerToolClaims([NOTCH]);
		assert.equal(claimFor('Open PR 20 in another notch.')?.tool, 'show_web_in_notch');
		assert.equal(registeredToolClaims().length, 1);
	});

	it('an unusable pattern is dropped, never thrown — a bad manifest must not kill the agent', () => {
		registerToolClaims([{ tool: 't', match: '([unclosed', message: 'm' }], () => {});
		assert.deepEqual(registeredToolClaims(), []);
		assert.doesNotThrow(() => claimFor('anything'));
	});
});
