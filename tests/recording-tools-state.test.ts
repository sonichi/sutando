import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync, rmSync, existsSync } from 'node:fs';

const { resetDemoState, recordingState, isRecordingActive, isRecordingMuted } =
	await import('../src/recording-tools.js');

const { demoStateRef } = await import('../src/recording-state.js');

const PID_FILE = '/tmp/sutando-screen-record.pid';

after(() => {
	// Restore test mutations
	recordingState.muted = false;
	demoStateRef.value = 'idle';
	try { rmSync(PID_FILE); } catch { /* ignore */ }
});

// ── resetDemoState ────────────────────────────────────────────────────────────

describe('resetDemoState', () => {
	it('does nothing when state is already idle', () => {
		demoStateRef.value = 'idle';
		resetDemoState();
		assert.equal(demoStateRef.value, 'idle');
	});

	it('resets recording state to idle', () => {
		demoStateRef.value = 'recording';
		resetDemoState();
		assert.equal(demoStateRef.value, 'idle');
	});

	it('resets done state to idle', () => {
		demoStateRef.value = 'done';
		resetDemoState();
		assert.equal(demoStateRef.value, 'idle');
	});

	it('is idempotent — calling twice leaves state idle', () => {
		demoStateRef.value = 'recording';
		resetDemoState();
		resetDemoState();
		assert.equal(demoStateRef.value, 'idle');
	});
});

// ── isRecordingMuted / recordingState ─────────────────────────────────────────

describe('isRecordingMuted', () => {
	it('returns false by default', () => {
		recordingState.muted = false;
		assert.equal(isRecordingMuted(), false);
	});

	it('returns true when muted flag is set', () => {
		recordingState.muted = true;
		assert.equal(isRecordingMuted(), true);
	});

	it('returns false after clearing muted flag', () => {
		recordingState.muted = true;
		recordingState.muted = false;
		assert.equal(isRecordingMuted(), false);
	});
});

// ── isRecordingActive ─────────────────────────────────────────────────────────

describe('isRecordingActive', () => {
	it('returns false when pid file is absent', () => {
		try { rmSync(PID_FILE); } catch { /* ignore */ }
		assert.equal(isRecordingActive(), false);
	});

	it('returns true when pid file exists', () => {
		writeFileSync(PID_FILE, '99999');
		assert.equal(isRecordingActive(), true);
	});

	it('returns false after pid file is removed', () => {
		writeFileSync(PID_FILE, '99999');
		rmSync(PID_FILE);
		assert.equal(isRecordingActive(), false);
	});
});
