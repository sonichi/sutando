import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';

const {
	registerSource,
	listSources,
	registerVisionOnContributor,
	_getVisionOnContributorCount,
	setVisionSession,
	isStreaming,
	getVisionState,
} = await import('../src/vision-tools.js');

after(() => {
	// Clear session so streaming ticker is stopped
	setVisionSession(null);
});

// ── registerSource / listSources ──────────────────────────────────────────────

describe('registerSource / listSources', () => {
	it('registered source appears in listSources()', () => {
		registerSource({ name: 'TestSourceA', async capture() { return { data: Buffer.alloc(0), mimeType: 'image/jpeg' }; } });
		assert.ok(listSources().includes('testsourcea'), 'testsourcea should be in sources');
	});

	it('source name is stored lowercased', () => {
		registerSource({ name: 'TestSourceB', async capture() { return { data: Buffer.alloc(0), mimeType: 'image/jpeg' }; } });
		const names = listSources();
		assert.ok(names.includes('testsourceb'), 'should store as lowercase');
		assert.ok(!names.includes('TestSourceB'), 'should not store mixed-case');
	});

	it('re-registering same name (case-insensitive) overwrites, does not duplicate', () => {
		const before = listSources().filter(n => n === 'dedup-source').length;
		registerSource({ name: 'dedup-source', async capture() { return { data: Buffer.alloc(1), mimeType: 'image/png' }; } });
		registerSource({ name: 'dedup-source', async capture() { return { data: Buffer.alloc(2), mimeType: 'image/jpeg' }; } });
		const after = listSources().filter(n => n === 'dedup-source').length;
		assert.equal(after, 1, 'should have exactly one entry after re-register');
	});

	it('listSources returns an array', () => {
		const sources = listSources();
		assert.ok(Array.isArray(sources), 'listSources should return an array');
	});
});

// ── registerVisionOnContributor / _getVisionOnContributorCount ────────────────

describe('registerVisionOnContributor', () => {
	it('increases contributor count when registered', () => {
		const before = _getVisionOnContributorCount();
		const unregister = registerVisionOnContributor(() => 'screen companion active');
		const after = _getVisionOnContributorCount();
		assert.equal(after, before + 1, 'count should increase by 1');
		unregister(); // cleanup
	});

	it('returns an unregister function that decreases count', () => {
		const before = _getVisionOnContributorCount();
		const unregister = registerVisionOnContributor(() => 'temp contributor');
		assert.equal(_getVisionOnContributorCount(), before + 1, 'should be +1 after register');
		unregister();
		assert.equal(_getVisionOnContributorCount(), before, 'should return to original after unregister');
	});

	it('multiple registrations are independent', () => {
		const before = _getVisionOnContributorCount();
		const u1 = registerVisionOnContributor(() => 'contrib-1');
		const u2 = registerVisionOnContributor(() => 'contrib-2');
		assert.equal(_getVisionOnContributorCount(), before + 2, 'should have +2 contributors');
		u1();
		assert.equal(_getVisionOnContributorCount(), before + 1, 'should have +1 after first unregister');
		u2();
		assert.equal(_getVisionOnContributorCount(), before, 'should return to original after both unregistered');
	});
});

// ── setVisionSession / isStreaming / getVisionState ───────────────────────────

describe('isStreaming', () => {
	it('returns false initially (no streaming started)', () => {
		setVisionSession(null); // ensure clean state
		assert.equal(isStreaming(), false);
	});
});

describe('getVisionState', () => {
	it('returns streaming:false initially', () => {
		setVisionSession(null);
		const state = getVisionState();
		assert.equal(state.streaming, false);
	});

	it('returns frames:0 when no frames sent', () => {
		setVisionSession(null);
		const state = getVisionState();
		assert.equal(state.frames, 0);
	});

	it('returns sessionReady:false when session is null', () => {
		setVisionSession(null);
		const state = getVisionState();
		assert.equal(state.sessionReady, false);
	});

	it('returns sessionReady:false when session has no sendFile', () => {
		setVisionSession({ transport: {} });
		const state = getVisionState();
		assert.equal(state.sessionReady, false);
	});

	it('returns sessionReady:true when session transport has sendFile', () => {
		setVisionSession({ transport: { sendFile: () => { /* noop */ } } });
		const state = getVisionState();
		assert.equal(state.sessionReady, true);
		setVisionSession(null); // cleanup
	});

	it('returns fps:0 when not streaming', () => {
		setVisionSession(null);
		const state = getVisionState();
		assert.equal(state.fps, 0);
	});

	it('returns source:null when not streaming', () => {
		setVisionSession(null);
		const state = getVisionState();
		assert.equal(state.source, null);
	});
});

describe('setVisionSession', () => {
	it('can be called with null safely', () => {
		assert.doesNotThrow(() => setVisionSession(null));
	});

	it('can be called with an empty object', () => {
		assert.doesNotThrow(() => setVisionSession({}));
		setVisionSession(null);
	});
});
