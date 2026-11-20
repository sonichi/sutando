import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { clearActivePlaybackProcess, PlaybackOwner } from '../skills/phone-conversation/scripts/playback_binding.js';
import { OwnerVisionBinding } from '../skills/phone-conversation/scripts/vision_call_binding.js';

describe('phone playback ownership', () => {
	it('allows only the bound call to control playback and releases it by owner', () => {
		const owner = new PlaybackOwner();
		owner.bind('CA-A', '/tmp/sutando-recording-a.mov');
		assert.equal(owner.controlledBy('CA-B'), false);
		assert.equal(owner.controlledBy('CA-A'), true);
		assert.equal(owner.releaseCall('CA-B'), false);
		assert.deepEqual(owner.current(), { callSid: 'CA-A', path: '/tmp/sutando-recording-a.mov' });
		assert.equal(owner.releaseCall('CA-A'), true);
		assert.equal(owner.current(), null);
	});

	it('preserves a replacement ffmpeg process when an older process closes', () => {
		const oldProcess = {};
		const replacement = {};
		assert.equal(clearActivePlaybackProcess(replacement, oldProcess), replacement);
		assert.equal(clearActivePlaybackProcess(replacement, replacement), null);
	});
});

describe('owner vision call binding', () => {
	it('reattaches vision to the most recent remaining owner call', () => {
		const binding = new OwnerVisionBinding<string>();
		binding.attach('CA-A', 'session-A');
		binding.attach('CA-B', 'session-B');
		assert.deepEqual(binding.remove('CA-B'), { callSid: 'CA-A', session: 'session-A' });
		assert.equal(binding.remove('CA-A'), null);
	});

	it('restores the prior session after the final owner call ends', () => {
		const binding = new OwnerVisionBinding('prior-session');
		binding.attach('CA-A', 'owner-session');
		assert.deepEqual(binding.remove('CA-A'), { callSid: null, session: 'prior-session' });
	});
});
