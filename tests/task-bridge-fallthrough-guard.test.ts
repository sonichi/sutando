import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { _shouldFallthrough, _shouldRegisterTaskRow } from '../src/task-bridge.js';

// Regression for issue #1035 (follow-up to PR #1033, per-channel pull path).
//
// PR #1033 introduced a new filename namespace `<channel-key>.task-{id}.txt`
// in results/ for the phone + plugin-surface pull path. The new-namespace
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
		assert.equal(_shouldFallthrough('1234567890123456789.task-1234567890.txt'), false);
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

// Regression for issue #1786 — duplicate pending-question reminders in the
// Task list. proactive-* notification files legitimately pass _shouldFallthrough
// (so they get SPOKEN), but they are NOT tasks: registering them (via
// _sendTaskStatus + POST /task-done) keys a task_history row by the file stem,
// so every re-fire of a proactive notification with a fresh timestamp adds a
// DUPLICATE Task row. _shouldRegisterTaskRow gates those two side-effects to
// genuine task-*.txt results — the general fix superseding the narrow #1784.
describe('_shouldRegisterTaskRow — only genuine task-* results register a Task row (#1786)', () => {
	it('accepts canonical task-* result files (these ARE tasks)', () => {
		assert.equal(_shouldRegisterTaskRow('task-1234567890.txt'), true);
		assert.equal(_shouldRegisterTaskRow('task-chat-1234567890.txt'), true);
		assert.equal(_shouldRegisterTaskRow('task-discord-voice-1234567890.txt'), true);
	});

	it('REJECTS proactive-* notification files (spoken, but not Task rows — the dup bug)', () => {
		assert.equal(_shouldRegisterTaskRow('proactive-1234567890.txt'), false);
		assert.equal(_shouldRegisterTaskRow('proactive-pending-q-1782424563.txt'), false);
		assert.equal(_shouldRegisterTaskRow('proactive-pending-q-1782424565.txt'), false);
		// every re-fire is a distinct stem; none should register a row
		assert.equal(_shouldRegisterTaskRow('proactive-result-task-abc-1234.txt'), false);
		assert.equal(_shouldRegisterTaskRow('proactive-timeout-task-abc-1234.txt'), false);
	});

	it('REJECTS voice-* push-channel files (defensive; they short-circuit earlier anyway)', () => {
		assert.equal(_shouldRegisterTaskRow('voice-1234567890.txt'), false);
		assert.equal(_shouldRegisterTaskRow('voice-draft-abc.txt'), false);
	});

	it('rejects question-* and other non-task prefixes', () => {
		assert.equal(_shouldRegisterTaskRow('question-1234567890.txt'), false);
		assert.equal(_shouldRegisterTaskRow('something-else.txt'), false);
		assert.equal(_shouldRegisterTaskRow('prefix-task-1234.txt'), false);
	});
});
