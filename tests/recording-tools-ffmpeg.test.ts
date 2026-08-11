import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { dirname, join } from 'node:path';
import { ffmpegSubtitleCandidates, ffprobeCandidates, selectFfprobe } from '../src/recording-tools.js';

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

	// Named per ARCH deliberately. This case previously read "both Homebrew
	// locations" while asserting two /opt/homebrew paths — both Apple-Silicon.
	// That wording is why a missing Intel prefix went unnoticed: the suite looked
	// like it covered Homebrew broadly when it covered one architecture twice.
	it('probes PATH and BOTH Apple-Silicon Homebrew formulas', () => {
		const cands = ffmpegSubtitleCandidates('/usr/bin/node');
		assert.ok(cands.includes('ffmpeg'));
		assert.ok(cands.includes('/opt/homebrew/bin/ffmpeg'));
		assert.ok(cands.includes('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'));
	});

	it('probes the Intel Homebrew prefix (/usr/local)', () => {
		const cands = ffmpegSubtitleCandidates('/usr/bin/node');
		assert.ok(cands.includes('/usr/local/bin/ffmpeg'), 'Intel Homebrew bin');
	});
});

/**
 * ffprobeCandidates derives from ffmpegSubtitleCandidates, so a prefix absent
 * upstream is absent here too — that is exactly how this PR first shipped an
 * "Intel portability fix" containing no /usr/local path. These assert the
 * derived list directly rather than trusting the mapping.
 */
describe('ffprobeCandidates', () => {
	const cands = () => ffprobeCandidates('/usr/bin/node');

	it('keeps a bare name so PATH resolution still applies', () => {
		assert.equal(cands()[0], 'ffprobe');
	});

	it('includes the Apple-Silicon Homebrew ffprobe', () => {
		assert.ok(cands().includes('/opt/homebrew/bin/ffprobe'));
	});

	it('includes the Intel Homebrew ffprobe (the regression this PR exists for)', () => {
		assert.ok(cands().includes('/usr/local/bin/ffprobe'));
	});

	it('includes the bundled-runtime sibling ffprobe', () => {
		const execPath = '/Applications/Sutando.app/Contents/Resources/runtime/bin/node';
		assert.ok(
			ffprobeCandidates(execPath).includes(
				'/Applications/Sutando.app/Contents/Resources/runtime/bin/ffprobe',
			),
		);
	});

	// The rename is anchored to the LAST path segment: a directory named
	// `ffmpeg-full` must survive intact. An unanchored replace would produce
	// `/opt/homebrew/opt/ffprobe-full/bin/ffprobe`, a path that exists nowhere.
	it('renames only the final segment, leaving an ffmpeg-full DIRECTORY intact', () => {
		const c = cands();
		assert.ok(c.includes('/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'), 'arm64 anchored');
		assert.ok(
			!c.some((p) => p.includes('ffprobe-full')),
			'no candidate may contain ffprobe-full',
		);
	});
});

// The finder must actually REACH the absolute candidates. A prior version
// (`find((p) => !p.includes('/') || existsSync(p))`) accepted the leading bare
// `ffprobe` immediately, so the absolute Homebrew/bundled paths were never
// tried — dead code on the exact install (Intel / PATH-less launchd) the PR
// targets. This pins the ordering that fix restores (review of #2370).
describe('selectFfprobe', () => {
	const cands = ffprobeCandidates('/usr/bin/node'); // [ 'ffprobe', '/opt/homebrew/bin/ffprobe', ... ]

	it('returns an existing ABSOLUTE candidate before the bare name', () => {
		const exists = (p: string) => p === '/opt/homebrew/bin/ffprobe';
		assert.equal(selectFfprobe(cands, exists), '/opt/homebrew/bin/ffprobe');
	});

	it('does not short-circuit on the leading bare name when an absolute exists', () => {
		// The Intel path exists but arm64 does not — the finder must skip the bare
		// name AND the absent arm64 path and land on the one that exists.
		const exists = (p: string) => p === '/usr/local/bin/ffprobe';
		assert.equal(selectFfprobe(cands, exists), '/usr/local/bin/ffprobe');
	});

	it('falls back to the bare name for PATH resolution when no absolute exists', () => {
		assert.equal(selectFfprobe(cands, () => false), 'ffprobe');
	});

	it('returns the literal ffprobe when there is neither an absolute nor a bare candidate', () => {
		assert.equal(selectFfprobe(['/opt/x/ffprobe'], () => false), 'ffprobe');
	});
});
