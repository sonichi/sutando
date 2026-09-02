import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
	pauseKeywordGuard,
	pauseRetryAuthorization,
	endPlaybackAuthorization,
	KEYWORD_BLOCK_RETRY_MS,
} from '../src/recording-tools.js';

/**
 * Regression guard for pause_video refusing a real pause request.
 *
 * The keyword check stops a Gemini hallucination outside the 8s audio cooldown
 * from pausing on its own. But it reads a rolling ASR transcript, and ASR is
 * least reliable on short commands repeated in frustration — so it failed closed
 * against the user exactly when he needed it. Observed live, four consecutive
 * refusals while the user said "pause it" and then "stop it", 13-20s apart:
 *
 *   [PauseVideo] BLOCKED — no pause keyword in recent user speech:
 *     "user: i saw you already played it. [15:13:49] user: <noise>"
 *     "[15:13:49] user: <noise> [15:14:13] user: พยายาม"
 *     "user: พยายาม [15:14:22] user: خاص <noise>"
 *
 * An earlier version of this file simulated the elapsed time arithmetically and
 * therefore passed against a build with every reset call site deleted. These
 * tests drive the real `pauseRetryAuthorization` instead.
 */
const GARBLED = 'user: <noise> [15:14:13] user: พยายาม [15:14:22] user: خاص <noise>';
const GARBLED_LATER = 'user: พยายาม [15:14:22] user: خاص <noise> [15:14:31] user: <noise>';
const T0 = 1_000_000;

beforeEach(() => pauseRetryAuthorization.clear());

describe('pauseKeywordGuard — pure policy', () => {
	it('allows a pause when the user plainly said it', () => {
		assert.equal(pauseKeywordGuard('user: stop it', 0), 'allow');
		assert.equal(pauseKeywordGuard('user: can you pause it?', 0), 'allow');
	});

	it('fails open when there is no recent speech at all', () => {
		// No fresh transcript is not evidence against the user.
		assert.equal(pauseKeywordGuard('', 0), 'allow');
	});

	it('blocks a keyword-less pause once the retry window has lapsed', () => {
		assert.equal(pauseKeywordGuard(GARBLED, KEYWORD_BLOCK_RETRY_MS + 1, GARBLED), 'block');
	});

	it('blocks an immediate re-call against the SAME transcript', () => {
		// The hallucinating caller retries at once, still hearing the same audio,
		// so its speech window has not moved. This is Susan's 2026-04-16 report.
		assert.equal(pauseKeywordGuard(GARBLED, 0, GARBLED), 'block');
		assert.equal(pauseKeywordGuard(GARBLED, 1_000, GARBLED), 'block');
	});

	it('honours a repeat once the transcript has moved on', () => {
		// The user asking again always produces new ASR text.
		assert.equal(pauseKeywordGuard(GARBLED_LATER, 5_000, GARBLED), 'allow');
	});
});

describe('pauseRetryAuthorization — real state machine', () => {
	it('a block on one video does not authorize a keyword-less pause on the next', () => {
		// Video A: keyword-less pause is blocked and the stamp is recorded.
		assert.equal(
			pauseKeywordGuard(GARBLED, pauseRetryAuthorization.msSinceBlock(T0), pauseRetryAuthorization.transcript),
			'block',
		);
		pauseRetryAuthorization.recordBlock(T0, GARBLED);

		// Same video, 5s later, new speech: the repeat is honoured.
		assert.equal(
			pauseKeywordGuard(GARBLED_LATER, pauseRetryAuthorization.msSinceBlock(T0 + 5_000), pauseRetryAuthorization.transcript),
			'allow',
		);

		// A playback lifecycle transition clears it — the SAME call is now blocked.
		// This assertion fails if endPlaybackAuthorization() stops resetting.
		endPlaybackAuthorization();
		assert.equal(
			pauseKeywordGuard(GARBLED_LATER, pauseRetryAuthorization.msSinceBlock(T0 + 5_000), pauseRetryAuthorization.transcript),
			'block',
		);
	});

	it('a cleared stamp reads as far outside the window, not as zero elapsed', () => {
		// Guards the subtle bug: `now - 0` is enormous and happens to block, but
		// only by accident of the epoch. Make the intent explicit.
		pauseRetryAuthorization.clear();
		assert.ok(pauseRetryAuthorization.msSinceBlock(T0) > KEYWORD_BLOCK_RETRY_MS);
	});
});

describe('playback lifecycle wiring', () => {
	// The behavioural tests above cannot see the CALL SITES. A reviewer deleted all
	// four and the previous suite still passed 7/7, so this pins them directly.
	const src = readFileSync(
		join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'recording-tools.ts'),
		'utf8',
	);

	for (const tool of ['playVideoTool', 'resumeVideoTool', 'replayVideoTool', 'closeVideoTool']) {
		it(`${tool} clears the pause-retry authorization`, () => {
			const start = src.indexOf(`export const ${tool}`);
			assert.ok(start > 0, `${tool} not found`);
			const body = src.slice(start, src.indexOf('\n};', start));
			assert.ok(
				body.includes('endPlaybackAuthorization()'),
				`${tool} must call endPlaybackAuthorization() — a block on one playback must not authorize a pause on the next`,
			);
		});
	}
});
