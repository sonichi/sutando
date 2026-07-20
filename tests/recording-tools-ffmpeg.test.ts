import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { dirname, join } from 'node:path';
import { ffmpegSubtitleCandidates } from '../src/recording-tools.js';

/**
 * Regression guard for the ffmpeg subtitle-probe bundled-runtime fix.
 *
 * When the voice-agent runs inside Sutando.app it executes under the bundled
 * node at `…/Contents/Resources/runtime/bin/node`, and a libass-capable ffmpeg
 * ships as its sibling `…/runtime/bin/ffmpeg`. Before this fix the probe only
 * checked PATH + Homebrew, so on an install with neither, subtitles silently
 * failed even though a capable ffmpeg sat right next to the running node.
 */
describe('ffmpegSubtitleCandidates', () => {
	it('includes the bundled ffmpeg as a sibling of the node exec path', () => {
		const execPath = '/Applications/Sutando.app/Contents/Resources/runtime/bin/node';
		const cands = ffmpegSubtitleCandidates(execPath);
		const expected = join(dirname(execPath), 'ffmpeg');
		assert.equal(expected, '/Applications/Sutando.app/Contents/Resources/runtime/bin/ffmpeg');
		assert.ok(cands.includes(expected), 'bundled-runtime sibling ffmpeg must be a candidate');
	});

	it('keeps the bundled candidate LAST so working installs are unaffected', () => {
		const cands = ffmpegSubtitleCandidates('/opt/homebrew/bin/node');
		assert.equal(cands[0], 'ffmpeg', 'PATH ffmpeg stays first');
		assert.equal(cands[cands.length - 1], '/opt/homebrew/bin/ffmpeg', 'bundled sibling is last');
	});

	it('still probes PATH and both Homebrew locations', () => {
		const cands = ffmpegSubtitleCandidates('/usr/bin/node');
		assert.ok(cands.includes('ffmpeg'));
		assert.ok(cands.includes('/opt/homebrew/bin/ffmpeg'));
		assert.ok(cands.includes('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'));
	});
});
