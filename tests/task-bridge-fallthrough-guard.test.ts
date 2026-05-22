import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { _shouldFallthrough } from '../src/task-bridge.js';

// Regression for issue #1035 (follow-up to PR #1033, per-channel pull path).
//
// PR #1033 introduced a new filename namespace `<channel-key>.task-{id}.txt`
// in results/ for the discord-voice + phone pull path. The new-namespace
// files are NOT meant to reach task-bridge's `onResult()` — the per-channel
// scanner inside the voice surfaces consumes them via read-and-delete.
//
// task-bridge's result-watcher has an unconditional fallthrough that, when
// the voice client is connected, fires `onResult(result)` for any non-empty
// `.txt` file past the `voice-*` / dedup / offline-forward branches. Without
// a guard, a `<key>.task-foo.txt` file could race the per-channel scanner
// and *also* be injected into the voice agent. PR #1033 accepted this race
// (mitigated by the scanner usually winning); issue #1035 closes it with a
// belt-suspenders allowlist gate at the fallthrough.
//
// `_shouldFallthrough(file)` is the exported predicate the watcher consults.

describe('_shouldFallthrough — belt-suspenders guard for result-watcher fallthrough (#1035)', () => {
	it('accepts canonical task-* result files (existing behavior)', () => {
		assert.equal(_shouldFallthrough('task-1234567890.txt'), true);
		assert.equal(_shouldFallthrough('task-chat-1234567890.txt'), true);
		assert.equal(_shouldFallthrough('task-abc-xyz.txt'), true);
	});

	it('accepts voice-* push-channel files (existing behavior)', () => {
		// voice-*.txt files are short-circuited earlier in the watcher (line
		// ~608), so the fallthrough is not strictly reached for them — but
		// the predicate still accepts them defensively so future refactors
		// can't silently regress voice delivery.
		assert.equal(_shouldFallthrough('voice-1234567890.txt'), true);
		assert.equal(_shouldFallthrough('voice-draft-abc.txt'), true);
	});

	it('REJECTS the PR #1033 per-channel-pull namespace (the bug this guard closes)', () => {
		// Discord voice channel id (17-20 digits)
		assert.equal(_shouldFallthrough('1485653767402553457.task-1234567890.txt'), false);
		// Twilio call SID (per-call unique)
		assert.equal(_shouldFallthrough('CAabcdef0123456789abcdef0123456789.task-foo.txt'), false);
		// Generic channel-key prefix
		assert.equal(_shouldFallthrough('some-channel.task-foo.txt'), false);
	});

	it('allows proactive-* (voice-spoken proactive delivery via the fallthrough path)', () => {
		// Per the proactive_voice rule, proactive messages are spoken by the voice agent
		// when the client is connected. That delivery has no explicit handler upstream in
		// this watcher — the fallthrough IS the path — so proactive-* must pass the guard
		// or voice-spoken proactive messages silently break. (discord-bridge's poll_proactive
		// runs in parallel for DM-delivery; the two consumers coexist.)
		assert.equal(_shouldFallthrough('proactive-1234567890.txt'), true);
		assert.equal(_shouldFallthrough('proactive-result-task-abc-1234.txt'), true);
		assert.equal(_shouldFallthrough('proactive-timeout-task-abc-1234.txt'), true);
	});

	it('rejects unknown / unfamiliar prefixes', () => {
		assert.equal(_shouldFallthrough('question-1234567890.txt'), false);
		assert.equal(_shouldFallthrough('something-else.txt'), false);
		assert.equal(_shouldFallthrough('.hidden.txt'), false);
		assert.equal(_shouldFallthrough('readme.txt'), false);
	});

	it('rejects filenames that merely CONTAIN task- / voice- but do not start with them', () => {
		// The guard is anchored to the start of the filename — any prefix
		// before task- / voice- (channel id, dot-separator, whatever) is
		// excluded by design.
		assert.equal(_shouldFallthrough('prefix-task-1234.txt'), false);
		assert.equal(_shouldFallthrough('prefix.voice-1234.txt'), false);
		assert.equal(_shouldFallthrough('xtask-1234.txt'), false);
	});
});
