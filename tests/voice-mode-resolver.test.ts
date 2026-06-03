// Unit tests for resolveCurrentMode (src/voice-mode-resolver.ts) — the
// unified base-mode resolver introduced to address issue #1410. Verifies the
// priority order (presenter > meeting > active), that each mode produces a
// `[BASE MODE: <name>` marker the model can recognize, and that the legacy
// substrate combinations (Zoom + presenter simultaneously, neither, etc.)
// resolve to a single canonical descriptor.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveCurrentMode } from '../src/voice-mode-resolver.ts';

const presenterOn = () => true;
const presenterOff = () => false;

test('active mode when neither meeting nor presenter is active', () => {
	const r = resolveCurrentMode({ meetingActive: false, isPresenterActive: presenterOff });
	assert.equal(r.mode, 'active');
	assert.equal(r.isMeeting, false);
	assert.equal(r.isPresenter, false);
	assert.match(r.marker, /\[BASE MODE: active/);
	// Critical: active marker tells the model NOT to fabricate silence tokens.
	assert.match(r.marker, /Do NOT infer or self-declare a meeting\/recording\/silent mode/);
	assert.match(r.marker, /\[System: …\], \[Silence\], or any variant of "produce zero audio"/);
});

test('meeting mode when meetingActive=true and presenter off', () => {
	const r = resolveCurrentMode({ meetingActive: true, isPresenterActive: presenterOff });
	assert.equal(r.mode, 'meeting');
	assert.equal(r.isMeeting, true);
	assert.equal(r.isPresenter, false);
	assert.match(r.marker, /\[BASE MODE: meeting/);
	assert.match(r.marker, /listen and take notes silently/);
	assert.match(r.marker, /Produce ZERO audio output/);
});

test('presenter mode when presenter HTTP returns active', () => {
	const r = resolveCurrentMode({ meetingActive: false, isPresenterActive: presenterOn });
	assert.equal(r.mode, 'presenter');
	assert.equal(r.isMeeting, false);
	assert.equal(r.isPresenter, true);
	assert.match(r.marker, /\[BASE MODE: presenter/);
	assert.match(r.marker, /CO-PRESENTER protocol/);
	assert.match(r.marker, /highlight_slide\(topic\) FIRST/);
});

test('presenter takes precedence over meeting (both active)', () => {
	// If Zoom is somehow detected while the user is presenting, the talk wins
	// — the user is on stage and needs CO-PRESENTER protocol, not silent
	// note-taking.
	const r = resolveCurrentMode({ meetingActive: true, isPresenterActive: presenterOn });
	assert.equal(r.mode, 'presenter');
	assert.equal(r.isPresenter, true);
	// isMeeting reports the CANONICAL mode only — presenter wins, so isMeeting
	// is false even though meetingActive=true. Callers that branch on
	// "should we silently take notes?" must use mode/isPresenter/isMeeting, not
	// the substrate inputs directly.
	assert.equal(r.isMeeting, false);
});

test('marker always begins with a leading space so callers concatenate inline', () => {
	for (const meetingActive of [false, true]) {
		for (const presenter of [presenterOff, presenterOn]) {
			const r = resolveCurrentMode({ meetingActive, isPresenterActive: presenter });
			assert.equal(r.marker.charAt(0), ' ',
				`marker for mode=${r.mode} must start with leading space (got ${JSON.stringify(r.marker.slice(0, 20))})`);
		}
	}
});

test('marker is non-empty for every mode (regression: never silently emit empty)', () => {
	// Before this resolver landed, `getPresenterStateMarker()` returned ''
	// when presenter was off — which left the system prompt with no base-mode
	// signal and let gemini-3.1 infer meeting silence. Every mode must emit
	// an explicit [BASE MODE: ...] marker so the inference path is closed.
	for (const meetingActive of [false, true]) {
		for (const presenter of [presenterOff, presenterOn]) {
			const r = resolveCurrentMode({ meetingActive, isPresenterActive: presenter });
			assert.ok(r.marker.trim().length > 0,
				`marker must be non-empty for mode=${r.mode}`);
			assert.match(r.marker, /\[BASE MODE: /,
				`marker must contain canonical token for mode=${r.mode}`);
		}
	}
});

test('isPresenterActive callback default falls back gracefully (smoke)', () => {
	// When `isPresenterActive` is omitted, the resolver uses the real
	// `isPresenterActiveDefault` which curl's localhost:7877. In CI / test
	// environments the server is almost certainly not running, so the call
	// returns false. Either way the function must not throw.
	const r = resolveCurrentMode({ meetingActive: false });
	assert.ok(['active', 'presenter'].includes(r.mode),
		`smoke test should resolve to active (no server) or presenter (server up); got ${r.mode}`);
});
